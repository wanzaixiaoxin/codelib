"""消息处理器：命令处理与代码问答处理。"""
from __future__ import annotations

from .storage import MemoryStore


class CommandHandler:
    """处理斜杠命令，例如 /help、/remember、/status。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    def handle(self, msg, text: str) -> str:
        """解析并执行命令，返回纯文本回复。"""
        cmd = text.split(" ", 1)[0].lower()
        if cmd == "/help":
            return "可用命令：/help /remember /status"
        if cmd == "/remember":
            content = text[len("/remember"):].strip()
            self.store.add_fact(msg.user_id, content)
            return f"已记住：{content}"
        if cmd == "/status":
            return f"已记忆 {self.store.count()} 条事实"
        return f"未知命令：{cmd}"


class QueryHandler:
    """处理自然语言问题：先查记忆，再检索代码。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    def handle(self, msg, text: str) -> str:
        """回答问题：优先命中记忆，否则给出去重后的检索提示。"""
        hits = self.store.search(text)
        if hits:
            return "根据记忆回答：" + "；".join(hits)
        return "没有找到相关记忆，建议提供更具体的关键词。"
