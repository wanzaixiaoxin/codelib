"""消息网关：负责渠道适配与命令路由。

Gateway 是服务的中枢：它从不同渠道（企业微信、飞书、本地）接收 IncomingMessage，
根据命令前缀路由到 Handler，并把结果交给 Responder 回发。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .handlers import CommandHandler, QueryHandler
from .storage import MemoryStore


@dataclass
class IncomingMessage:
    """统一的入站消息结构。"""
    channel: str          # wecom | feishu | local
    user_id: str
    text: str
    chat_id: str = ""


class MessageGateway:
    """多渠道消息网关。

    职责：
    - 维护渠道到回复器的映射；
    - 按命令前缀选择处理器；
    - 记录一次请求的处理耗时。
    """

    def __init__(self, store: MemoryStore):
        self.store = store
        self.command_handler = CommandHandler(store)
        self.query_handler = QueryHandler(store)
        self.responders: Dict[str, callable] = {}
        self.stats = {"received": 0, "replied": 0}

    def register_responder(self, channel: str, responder) -> None:
        """注册某渠道的回复函数，例如企业微信的 send_text。"""
        self.responders[channel] = responder

    def route(self, msg: IncomingMessage) -> str:
        """根据消息内容路由并返回回复文本。

        规则：以 `/` 开头的消息交给 CommandHandler，其余交给 QueryHandler。
        """
        self.stats["received"] += 1
        text = (msg.text or "").strip()
        if text.startswith("/"):
            reply = self.command_handler.handle(msg, text)
        else:
            reply = self.query_handler.handle(msg, text)
        self.stats["replied"] += 1
        return reply

    def reply(self, msg: IncomingMessage, text: str) -> bool:
        """通过对应渠道的 responder 回发消息。"""
        responder = self.responders.get(msg.channel)
        if responder is None:
            return False
        responder(msg.chat_id, text)
        return True
