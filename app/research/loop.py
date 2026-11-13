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

DEFAULT_LIMITS = {"max_steps": 20, "max_chars": 40000, "max_llm_calls": 16,
                  "max_wall_ms": 300000}
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
                       limits: dict | None = None, tracer=None,
                       kb_path=None, workspace: str = "",
                       preloaded_context: str = "",
                       memory_cfg: dict | None = None) -> dict:
    lim = {**DEFAULT_LIMITS, **(limits or {})}
    board = tools.board
    cards: list = []
    card_text = ""
    if kb_path and workspace:
        try:
            from .memory import search_cards, render_cards
            cards = search_cards(kb_path, workspace, question)
            card_text = render_cards(cards) if cards else ""
        except Exception:
            cards = []
    t0 = time.time()
    findings: list = []      # driver 外置保存的工具输出（final 的证据来源）
    stop_reason = "steps_exhausted"

    board = await plan(question, llm)
    tools.board = board
    if tracer:
        tracer.event("research_step", {"phase": "plan", "assertions": board.stats()})

    system = READER_ROLE
    if preloaded_context:
        system += chr(10) * 2 + "## 预读上下文（自动调用链扫描）" + chr(10) * 2 + preloaded_context[:12000]
    messages: list[dict] = [{"role": "system", "content": system},
                            {"role": "user", "content": question + ((chr(10) * 2 + card_text) if card_text else "")}]
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
        last_assistant_text = resp.get("content") or ""
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
            findings.append({"tool": t["name"], "args": str(t["arguments"])[:150], "output": out})
            messages.append({"role": "tool", "tool_call_id": t["id"], "content": out[:6000]})
        if tools.breaker.stop_requested:
            stop_reason = "repeat_breaker"; break
        if chars > int(lim["max_chars"]):
            stop_reason = "chars_exhausted"; break
        if (time.time() - t0) * 1000 > int(lim["max_wall_ms"]):
            stop_reason = "wall_time"; break
        if llm_calls >= int(lim["max_llm_calls"]):
            stop_reason = "llm_calls"; break

    # 最终交付：全新短上下文（丢工具轨迹——Kimi compaction 语义），防 JSON 被 4096 截断
    tool_summary = "; ".join(
        e["tool"] + "(" + str(e["chars"]) + "c)" for e in tools.event_log[-12:])
    fparts = []
    for fi, f in enumerate(findings):
        if f["tool"] in ("read_body", "read_lines", "expand_chain", "grep_code") and f["output"]:
            fparts.append("[" + str(fi + 1) + "] " + f["tool"] + " " + f["args"][:120] + chr(10) + f["output"][:600])
    findings_text = (chr(10) * 2).join(fparts[-10:]) if fparts else "（无）"
    # 预读上下文的关键语句索引（go/MarkMessage/回调/分区器）并入 final 证据区
    _pre_idx = ""
    if preloaded_context and "关键语句索引" in preloaded_context:
        _pre_idx = preloaded_context.split("【相关函数体】")[0]
        findings_text = _pre_idx + (chr(10) * 2) + findings_text
    answer_contract = (
        "面向使用者直接给结论与依据；不要复述研究过程；不要出现「候选未覆盖/未知/需要进一步确认」；绝对不要让用户自己去查代码——那是你的工作；没查到的信息只放进 open_assertions。"
    )
    parts = [
        "用户问题：" + question,
        "",
        "断言板：",
        board.render(),
        "",
        "工具返回的代码证据（引用时必须用这里的真实 path :: symbol）：",
        findings_text,
        "",
        "现在给出最终交付，只输出 JSON：",
        '{"conclusions": ["每条结论一句话，因果必须引用 path :: symbol"],',
         ' "evidence": [{"symbol": "...", "path": "...", "quote": "关键代码行"}],',
         ' "open_assertions": ["本次确实没查到的，逐条列出"],',
         ' "answer": "给用户的完整中文回答。' + answer_contract + '}',
    ]
    final_user = chr(10).join(parts)
    final_json: dict = {}
    final_text = ""
    for attempt in (1, 2):
        try:
            resp = await llm.chat_messages(
                [{"role": "system",
                  "content": READER_ROLE + chr(10) + "现在是最终交付阶段：不要调用工具，只输出 JSON。"},
                 {"role": "user", "content": final_user}],
                max_tokens=4096)
            llm_calls += 1
            final_text = resp.get("content") or ""
            final_json = extract_json(final_text) or {}
            if final_json.get("answer"):
                break
        except Exception as e:
            final_text = "最终交付失败 " + type(e).__name__ + ": " + str(e)
    if not final_json:
        final_json = {"answer": final_text[:2000] or "最终交付为空",
                      "conclusions": [], "evidence": [],
                      "open_assertions": board.open_assertions()}
    # evidence 回填：模型常把 path :: symbol 写进结论而漏填结构化数组——从结论提取
    if not final_json.get("evidence"):
        refs, seen = [], set()
        for c in final_json.get("conclusions", []):
            for mp, ms in re.findall(r"([\w./\-]+\.\w+)\s*::\s*([\w.]+)", str(c)):
                key = (mp, ms)
                if key not in seen:
                    seen.add(key)
                    refs.append({"symbol": ms, "path": mp, "quote": ""})
        final_json["evidence"] = refs

    cards_saved: list = []
    if kb_path and workspace:
        try:
            from .memory import save_cards
            mc = memory_cfg or {}
            cards_saved = await save_cards(llm, kb_path, workspace, question, {"conclusions": final_json.get("conclusions", []), "evidence": final_json.get("evidence", []), "board_dump": board.dump()},
                                           max_cards=int(mc.get("max_cards_per_run", 3)),
                                           disable_write=bool(mc.get("disable_write", False)))
        except Exception as e:
            cards_saved = ["save_failed:" + type(e).__name__ + ":" + str(e)[:100]]

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
        "cards_injected": len(cards), "cards_saved": cards_saved,
    }
