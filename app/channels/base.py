"""渠道抽象。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class IncomingMessage:
    channel: str
    user_id: str
    text: str
    chat_id: str = ""
    session_id: str = ""
    workspace_id: str = ""
    message_id: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


class ChannelAdapter:
    name = "base"

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))
