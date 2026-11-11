"""本地/通用通道：用于自测、Web 调试或任意系统以 JSON 接入。

POST /channels/local/message  {"text": "...", "workspace": "xxx", "session_id": "..."}
"""
from __future__ import annotations

from .base import ChannelAdapter, IncomingMessage


class LocalChannel(ChannelAdapter):
    name = "local"

    def parse(self, payload: dict) -> IncomingMessage:
        user_id = str(payload.get("user_id") or "local-user")
        return IncomingMessage(
            channel="local",
            user_id=user_id,
            text=str(payload.get("text") or "").strip(),
            chat_id=str(payload.get("chat_id") or "local"),
            workspace_id=str(payload.get("workspace") or ""),
            session_id=str(payload.get("session_id") or f"local:{user_id}"),
            raw=payload,
        )
