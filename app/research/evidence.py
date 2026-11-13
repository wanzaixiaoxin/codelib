"""断言表：外置 state，每轮注入（Kimi goal 形态——不靠对话历史）。"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import List


@dataclass
class Assertion:
    text: str
    status: str = "open"            # open | proven | refuted
    evidence: List[dict] = field(default_factory=list)   # [{symbol,path,quote}]


class EvidenceBoard:
    def __init__(self, assertions: List[str] | None = None):
        self.assertions: List[Assertion] = [Assertion(t) for t in (assertions or [])]

    def update(self, index: int, status: str, symbol: str = "", path: str = "",
               quote: str = "") -> str:
        if not (0 <= index < len(self.assertions)):
            return f"invalid assertion index {index} (共 {len(self.assertions)} 条)"
        if status not in ("proven", "refuted", "open"):
            return f"invalid status {status}"
        a = self.assertions[index]
        a.status = status
        if symbol or path:
            a.evidence.append({"symbol": symbol, "path": path, "quote": quote[:160]})
        return f"断言#{index} 已标记 {status}：{a.text[:50]}"

    def render(self) -> str:
        if not self.assertions:
            return "（断言表为空——planner 尚未运行或未产出断言）"
        lines = ["| # | 断言 | 状态 | 证据 |", "|---|---|---|---|"]
        for i, a in enumerate(self.assertions):
            ev = "; ".join(f"{e.get('symbol','')}@{e.get('path','')}"
                           for e in a.evidence[-2:]) or "—"
            lines.append(f"| {i} | {a.text[:70]} | {a.status} | {ev} |")
        return "\n".join(lines)

    def stats(self) -> dict:
        from collections import Counter
        return dict(Counter(a.status for a in self.assertions))

    def open_assertions(self) -> List[str]:
        return [a.text for a in self.assertions if a.status == "open"]

    def dump(self) -> str:
        return json.dumps([{"text": a.text, "status": a.status,
                            "evidence": a.evidence} for a in self.assertions],
                          ensure_ascii=False, indent=1)
