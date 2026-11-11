"""飞书（Lark）渠道：事件订阅 v2 + 自定义机器人 webhook。

- URL 验证：type=url_verification 返回 challenge；
- 事件接收：im.message.receive_v1 文本消息；
- 回复：tenant_access_token 调用 im/v1/messages 接口，或走自定义机器人 webhook。
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any, Dict, Optional, Tuple

from .base import ChannelAdapter, IncomingMessage

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except Exception:  # pragma: no cover
    Cipher = None  # type: ignore

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

FEISHU_API = "https://open.feishu.cn/open-apis"
_TOKEN_CACHE: Dict[str, Tuple[str, float]] = {}


def _unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > 16:
        return data
    return data[:-pad]


def decrypt(encrypt_b64: str, encrypt_key: str) -> bytes:
    if Cipher is None:
        raise RuntimeError("缺少 cryptography 依赖，无法解密飞书事件")
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    raw = base64.b64decode(encrypt_b64)
    iv, ct = raw[:16], raw[16:]
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    dec = cipher.decryptor()
    return _unpad(dec.update(ct) + dec.finalize())


class FeishuChannel(ChannelAdapter):
    name = "feishu"

    def verify_signature(self, headers: Dict[str, str], body: bytes) -> bool:
        key = self.cfg.get("encrypt_key", "")
        if not key:
            return True  # 未配置加密则跳过
        h = {k.lower(): v for k, v in headers.items()}
        ts = h.get("x-lark-request-timestamp", "")
        nonce = h.get("x-lark-request-nonce", "")
        sig = h.get("x-lark-signature", "")
        if not sig:
            return True
        raw = ts + nonce + key + body.decode("utf-8", "ignore")
        expect = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return expect == sig

    def decode_body(self, body: bytes) -> Dict[str, Any]:
        data = json.loads(body.decode("utf-8"))
        if "encrypt" in data and self.cfg.get("encrypt_key"):
            plain = decrypt(data["encrypt"], self.cfg["encrypt_key"])
            data = json.loads(plain.decode("utf-8"))
        return data

    def handle(self, body: bytes, headers: Dict[str, str]) -> Tuple[str, Any]:
        """返回 (kind, payload)：
        kind = challenge -> payload 为要返回的 challenge 字符串
        kind = message   -> payload 为 IncomingMessage
        kind = ignore    -> 忽略
        """
        if not self.verify_signature(headers, body):
            return "error", "invalid signature"
        data = self.decode_body(body)
        if data.get("type") == "url_verification":
            return "challenge", data.get("challenge", "")
        header = data.get("header", {}) or {}
        event = data.get("event", {}) or {}
        if header.get("event_type") != "im.message.receive_v1":
            return "ignore", None
        msg = event.get("message", {}) or {}
        if msg.get("message_type") != "text":
            return "ignore", None
        try:
            content = json.loads(msg.get("content", "{}"))
        except json.JSONDecodeError:
            return "ignore", None
        text = (content.get("text") or "").strip()
        if not text:
            return "ignore", None
        sender = event.get("sender", {}) or {}
        sid = sender.get("sender_id", {}) or {}
        user_id = sid.get("open_id") or sid.get("user_id") or "unknown"
        return "message", IncomingMessage(
            channel="feishu",
            user_id=user_id,
            text=text,
            chat_id=msg.get("chat_id", ""),
            message_id=msg.get("message_id", ""),
            session_id=f"feishu:{user_id}",
            raw={"event_type": header.get("event_type")},
        )

    async def _tenant_token(self) -> Optional[str]:
        cache_key = self.cfg.get("app_id", "")
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        if httpx is None:
            return None
        url = f"{FEISHU_API}/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self.cfg.get("app_id", ""),
                   "app_secret": self.cfg.get("app_secret", "")}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()
        if data.get("code") == 0:
            token = data["tenant_access_token"]
            _TOKEN_CACHE[cache_key] = (token, time.time() + int(data.get("expire", 7200)))
            return token
        return None

    async def reply_message(self, message_id: str, text: str) -> bool:
        token = await self._tenant_token()
        if not token or httpx is None or not message_id:
            return False
        url = f"{FEISHU_API}/im/v1/messages/{message_id}/reply"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {"content": json.dumps({"text": text[:4000]}, ensure_ascii=False),
                   "msg_type": "text"}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=headers, json=payload)
            data = resp.json()
        return data.get("code") == 0

    async def send_webhook(self, text: str) -> bool:
        url = self.cfg.get("webhook_url", "")
        if not url or httpx is None:
            return False
        payload = {"msg_type": "text", "content": {"text": text[:4000]}}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        return resp.status_code == 200 and data.get("code", 0) in (0, None)
