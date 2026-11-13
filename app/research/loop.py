"""Research Loop：plan → reader 循环（工具+熔断+压缩）→ final → verify。

依据 docs/refactor-plan.md §4。全部只读；预算四维；STOP 文件每步检查。
"""
from __future__ import annotations
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from app.research.evidence import EvidenceBoard
from app.research.profiles import PLANNER_ROLE, READER_ROLE, READER_TOOLS, VERIFIER_ROLE
from app.research.tools import ResearchTools

DEFAULT_LIMITS = {"max_steps": 12, "max_chars": 24000, "max_llm_calls": 8,
                  "max_wall_ms": 120000}
STOP_FILE = Path(__file__).resolve().parents[1] / "loop" / "STOP"


def extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        try:  # 宽松二次：去掉尾逗号
            return json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0)))
        except Exception:
            return None


def _stopped() -> bool:
    return STOP_FILE.exists()


def _trim_messages(messages: list[dict], board_text: str, max_chars: int = 60000) -> list[dict]:
    total = sum(len(str(m.get("content", ""))) for m in messages)
    if total <= max_chars:
        return messages
    head = [m for m in messages[:3]]                     # system + 首轮
    tail = messages[-6:]                                 # 最近交互
    reminder = {"role": "user",
                "content": "（上下文已压缩：早前的工具观察被裁剪，断言板如下是唯一权威状态）\n"
                           + board_text}
    return head + [reminder] + tail


async def plan(question: str, llm) -> EvidenceBoard:
    resp = await llm.chat(PLANNER_ROLE, f"用户问题：\n{question}")
    data = extract_json(resp or "") or {}
    texts = [str(a).strip() for a in (data.get("assertions") or []) if str(a).strip()]
    if not texts:
        texts = ["回答该问题所需的全部代码事实（planner 未产出结构化断言）"]
    return EvidenceBoard(texts[:6])


async def run_research(question: str, llm, tools: ResearchTools,
                       limits: dict | None = None, tracer=None) -> dict:
    lim = {**DEFAULT_LIMITS, **(limits or {})}
    board = tools.board
    t0 = time.time()
    stop_reason = "steps_exhausted"

    board = await plan(question, llm)
    tools.board = board
    if tracer:
        tracer.event("research_step", {"phase": "plan", "assertions": board.stats()})

    system = READER_ROLE
    messages: list[dict] = [{"role": "system", "content": system},
                            {"role": "user", "content": question}]
    specs = tools.tool_specs(READER_TOOLS)
    steps = llm_calls = chars = 0
    final_text = ""
    for step in range(int(lim["max_steps"])):
        if _stopped():
            stop_reason = "stop_file"; break
        messages = _trim_messages(messages, board.render())
        messages[-1] = {"role": "user", "content":
                        f"用户问题：{question}\n\n断言板（唯一权威状态，用 update_evidence 更新）：\n"
                        + board.render()}
        try:
            resp = await llm.chat_messages(messages, specs)
        except Exception as e:
            stop_reason = f"llm_error:{type(e).__name__}"; break
        llm_calls += 1
        steps += 1
        tcs = resp.get("tool_calls") or []
        if tracer:
            tracer.event("research_step", {"step": steps, "tool_calls": len(tcs),
                                           "chars": chars, "board": board.stats()})
        if not tcs:
            stop_reason = "no_tool_calls"; final_text = resp.get("content") or ""; break
        asst = {"role": "assistant", "content": resp.get("content") or "",
                "tool_calls": [{"id": t["id"], "type": "function",
                                "function": {"name": t["name"], "arguments": t["arguments"]}}
                               for t in tcs]}
        messages.append(asst)
        for t in tcs:
            out = tools.dispatch(t["name"], t["arguments"])
            chars += len(out)
            messages.append({"role": "tool", "tool_call_id": t["id"], "content": out[:6000]})
        if tools.breaker.stop_requested:
            stop_reason = "repeat_breaker"; break
        if chars > int(lim["max_chars"]):
            stop_reason = "chars_exhausted"; break
        if (time.time() - t0) * 1000 > int(lim["max_wall_ms"]):
            stop_reason = "wall_time"; break
        if llm_calls >= int(lim["max_llm_calls"]):
            stop_reason = "llm_calls"; break

    # 最终交付：要求 JSON 契约
    messages.append({"role": "user", "content":
        "现在基于以上全部工具证据给出最终交付，只输出 JSON：\n"
        '{"conclusions": ["每条结论一句话，因果必须引用 path :: symbol"],\n'
        ' "evidence": [{"symbol": "...", "path": "...", "quote": "关键代码行"}],\n'
        ' "open_assertions": ["本次确实没查到的，逐条列出"],\n'
        ' "answer": "给用户的完整中文回答"}'})
    try:
        resp = await llm.chat_messages(messages)
        final_text = resp.get("content") or ""
        llm_calls += 1
        final_json = extract_json(final_text) or {}
    except Exception as e:
        final_json = {"answer": f"最终交付失败 {type(e).__name__}: {e}",
                      "conclusions": [], "evidence": [], "open_assertions": board.open_assertions()}

    return {
        "question": question, "answer": final_json.get("answer", ""),
        "conclusions": final_json.get("conclusions", []),
        "evidence": final_json.get("evidence", []),
        "open_assertions": final_json.get("open_assertions", board.open_assertions()),
        "board": board.stats(), "board_dump": board.dump(),
        "steps": steps, "llm_calls": llm_calls, "chars": chars,
        "stop_reason": stop_reason, "wall_ms": int((time.time() - t0) * 1000),
        "tool_events": tools.event_log,
        "fingerprint": None,
    }
