"""记忆存储：轻量的键值 + 事实列表，用于演示。"""
from __future__ import annotations

from typing import List


class MemoryStore:
    """按用户维度保存事实的记忆存储。"""

    def __init__(self):
        self._facts: List[str] = []

    def add_fact(self, user_id: str, text: str) -> None:
        """追加一条事实。"""
        if text:
            self._facts.append(f"{user_id}:{text}")

    def search(self, query: str) -> List[str]:
        """按关键词模糊匹配事实。"""
        q = (query or "").lower()
        return [f for f in self._facts if q and q in f.lower()]

    def count(self) -> int:
        """返回已保存的事实条数。"""
        return len(self._facts)
