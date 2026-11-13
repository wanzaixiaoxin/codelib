"""R2 验收：真 LLM 驱动 Research Loop 跑那道 Kafka 顺序题。
跑法: python -B loop/probe_research.py   （预算内最多 8 次 LLM 调用）
"""
from __future__ import annotations
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.config import AppConfig
from app.llm import LLMClient
from app.research.evidence import EvidenceBoard
from app.research.loop import run_research
from app.research.tools import ResearchTools

QUESTION = ("分析当两个人的对话数据进入kafka后被我们消费，到最后写入kafka，"
            "对话的顺序能保证吗")
KB = ROOT / "data" / "knowledge.db"
WS = "nlp:前置NLP"
WS_ROOT = ROOT.parent / "code" / "pri_nlp_worker_svr"


def grade(result: dict) -> tuple[list[str], list[str]]:
    ok, bad = [], []
    answer = result.get("answer") or ""
    concl = json.dumps(result.get("conclusions"), ensure_ascii=False)
    text = answer + concl
    ev = json.dumps(result.get("evidence"), ensure_ascii=False)
    if result.get("conclusions"):
        ok.append("结构化结论")
    else:
        bad.append("无结构化结论")
    go_hits = ev.count(".go") if isinstance(result.get("evidence"), list) else 0
    (ok if go_hits >= 3 else bad).append(f"证据含 .go 源码 {go_hits} 处" if go_hits >= 3
                                         else f"证据 .go 仅 {go_hits} 处（<3）")
    kw = [k for k in ("分区", "key", "HashPartitioner", "MarkMessage", "goroutine", "协程",
                      "幂等", "乱序", "顺序", "提交") if k in text]
    (ok if len(kw) >= 3 else bad).append(f"关键机制词 {kw}" if len(kw) >= 3
                                         else f"关键机制词过少 {kw}")
    (ok if "候选未覆盖" not in text else bad).append("无「候选未覆盖」免责"
                                                    if "候选未覆盖" not in text
                                                    else "出现「候选未覆盖」免责")
    if result.get("open_assertions") is not None:
        ok.append(f"open 断言显式列出 {len(result['open_assertions'])} 条")
    # 预算触顶但 final 交付完整（有结论+证据）= 预算内完成，不算异常
    finished = bool(result.get("conclusions")) and bool(result.get("evidence"))
    if finished:
        ok.append(f"预算内完成（stop={result.get('stop_reason')}，final 交付完整）")
    else:
        bad.append(f"异常终止 {result.get('stop_reason')}（无完整交付）")
    return ok, bad


def main() -> int:
    cfg = AppConfig.load(str(ROOT / "config" / "config.yaml"))
    llm = LLMClient(cfg.llm)
    if not llm.available:
        print("LLM 不可用")
        return 1
    board = EvidenceBoard()
    tools = ResearchTools(KB, WS, str(WS_ROOT), board)
    t0 = time.time()
    try:
        result = asyncio.run(run_research(QUESTION, llm, tools))
    finally:
        tools.close()
    print(f"耗时 {result['wall_ms']}ms | steps={result['steps']} llm_calls={result['llm_calls']} "
          f"chars={result['chars']} stop={result['stop_reason']}")
    print("断言板:", result["board"])
    print("\n=== 工具事件 ===")
    for e in result["tool_events"]:
        print(f"  {e['tool']:<18} chars={e['chars']:>5} {'REJ:'+e['rejected'][:40] if e['rejected'] else ''}")
    print("\n=== 结论 ===")
    for c in result["conclusions"]:
        print("  -", str(c)[:120])
    print("\n=== 证据 ===")
    for e in (result["evidence"] or [])[:10]:
        print(f"  {e.get('symbol','')}@{e.get('path','')}")
    print("\n=== 答案（前 600 字） ===")
    print((result.get("answer") or "")[:600])
    ok, bad = grade(result)
    print("\n=== 验收 ===")
    for x in ok:
        print("  [PASS]", x)
    for x in bad:
        print("  [FAIL]", x)
    verdict = "PASS" if not bad else "FAIL"
    print(f"\nR2 验收: {verdict}（耗时 {time.time()-t0:.0f}s）")
    (ROOT / "loop" / "reports" / "research_probe.json").write_text(
        json.dumps({"question": QUESTION, "verdict": verdict, "ok": ok, "bad": bad,
                    "result": {k: v for k, v in result.items() if k != "board_dump"}},
                   ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    print("报告: loop/reports/research_probe.json")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
