"""长期记忆：历史问答 + 用户确认的事实，检索后注入上下文。

机器人「整理出来的东西」会以问答对/事实两种形态落库，下次遇到相似问题自动召回，
从而避免每次都全量重读代码。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .store import Store
from .utils import clip, tokenize


def _score(query_tokens: List[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    low = text.lower()
    toks = set(tokenize(low))
    hit = sum(1 for t in set(query_tokens) if t in toks)
    score = float(hit)
    q = " ".join(query_tokens)
    if q and q in low:
        score += 3.0
    return score


class MemoryManager:
    def __init__(self, store: Store, memory_cfg: Dict[str, Any] | None = None):
        self.store = store
        self.cfg = memory_cfg or {}
        self.max_turns = int(self.cfg.get("max_turns_per_user", 20))
        self.max_items = int(self.cfg.get("max_items_in_context", 5))

    # ---- 写入 ----
    def add_qa(self, workspace_id: str, session_id: str, question: str,
               answer: str, citations: List[Dict[str, Any]] | None = None) -> int:
        return self.store.add_qa(workspace_id, session_id, question, answer, citations)

    def add_fact(self, workspace_id: str, text: str, source: str = "user") -> int:
        return self.store.add_fact(workspace_id, text, source)

    # ---- 召回 ----
    def relevant(self, workspace_id: str, query: str,
                 limit: int | None = None) -> Tuple[str, List[str]]:
        limit = limit or self.max_items
        q_tokens = tokenize(query)
        items: List[Tuple[float, str]] = []

        for f in self.store.all_facts(workspace_id, limit=200):
            s = _score(q_tokens, f["text"]) + 0.5  # 事实略有加权
            if s >= 1.0:
                items.append((s, f"事实: {clip(f['text'], 200)}"))

        for qa in self.store.all_qa(workspace_id, limit=200):
            text = f"{qa['question']} {qa['answer']}"
            s = _score(q_tokens, text)
            # 只保留较强相关的历史问答，避免“关键词撞上”把无关旧答案带进来
            if s >= 2.5:
                items.append((s, f"Q: {clip(qa['question'], 120)}\n   A: {clip(qa['answer'], 220)}"))

        items.sort(key=lambda x: x[0], reverse=True)
        top = items[:limit]
        if not top:
            return "", []
        lines = ["【历史记忆（仅供参考，若与当前代码冲突以代码为准）】"]
        for _, t in top:
            lines.append("- " + t)
        return "\n".join(lines), [t for _, t in top]
