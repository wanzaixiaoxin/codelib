"""外部评分器：只看产物（结论/证据/断言板），不调 LLM、不采信模型自述。

依据 docs/integration-plan.md §16：「评测评分不能依赖模型最后一句'已完成'」。
"""
from __future__ import annotations
import json


def grade_research(result: dict) -> tuple[list[str], list[str]]:
    ok: list[str] = []
    bad: list[str] = []
    answer = result.get("answer") or ""
    concl_text = json.dumps(result.get("conclusions"), ensure_ascii=False)
    ev_text = json.dumps(result.get("evidence"), ensure_ascii=False)
    text = answer + concl_text + ev_text

    if result.get("conclusions"):
        ok.append("结构化结论")
    else:
        bad.append("无结构化结论")

    go_hits = text.count(".go")
    (ok if go_hits >= 3 else bad).append(
        f"证据含 .go 源码 {go_hits} 处" if go_hits >= 3 else f".go 引用仅 {go_hits} 处（<3）")

    kw = [k for k in ("分区", "key", "HashPartitioner", "MarkMessage", "goroutine",
                      "协程", "幂等", "乱序", "顺序", "提交") if k in text]
    (ok if len(kw) >= 3 else bad).append(
        f"关键机制词 {kw}" if len(kw) >= 3 else f"关键机制词过少 {kw}")

    if "候选未覆盖" not in text:
        ok.append("无「候选未覆盖」免责")
    else:
        bad.append("出现「候选未覆盖」免责")

    if result.get("open_assertions") is not None:
        ok.append(f"open 断言显式列出 {len(result.get('open_assertions'))} 条")

    finished = bool(result.get("conclusions"))
    if finished:
        ok.append(f"预算内完成（stop={result.get('stop_reason')}，final 交付完整）")
    else:
        bad.append(f"异常终止 {result.get('stop_reason')}（无完整交付）")
    return ok, bad


def verdict(ok: list[str], bad: list[str]) -> str:
    return "PASS" if not bad else "FAIL"
