"""渠道包。"""
from .base import ChannelAdapter, IncomingMessage
from .feishu import FeishuChannel
from .local import LocalChannel
from .wecom import WeComChannel

__all__ = ["ChannelAdapter", "IncomingMessage", "WeComChannel", "FeishuChannel", "LocalChannel"]
