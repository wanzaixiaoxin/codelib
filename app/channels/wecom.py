"""企业微信渠道。

支持两种形态：
1) 应用回调（推荐，可问答）：GET 验证 URL + POST 接收消息；被动回复或主动调用 message/send。
2) 群机器人 webhook（仅出站）：把答案推送到群。
加解密遵循企业微信 WXBizMsgCrypt（AES-256-CBC + 32 字节 PKCS7 + receiveid 校验）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import time
import xml.etree.ElementTree as ET
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

WECOM_API = "https://qyapi.weixin.qq.com"
_TOKEN_CACHE: Dict[str, Tuple[str, float]] = {}


def _pkcs7_unpad(data: bytes, block: int = 32) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > block:
        return data
    return data[:-pad]


def _pkcs7_pad(data: bytes, block: int = 32) -> bytes:
    pad = block - (len(data) % block)
    return data + bytes([pad]) * pad


class WeComCrypto:
    def __init__(self, token: str, encoding_aes_key: str, corp_id: str = ""):
        self.token = token or ""
        self.corp_id = corp_id or ""
        self.key = base64.b64decode(encoding_aes_key + "=") if encoding_aes_key else b""
        self.iv = self.key[:16]

    def signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        items = sorted([self.token, str(timestamp), str(nonce), encrypt or ""])
        return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()

    def decrypt(self, encrypt_b64: str) -> Tuple[str, str]:
        if Cipher is None:
            raise RuntimeError("缺少 cryptography 依赖，无法解密企业微信消息")
        raw = base64.b64decode(encrypt_b64)
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.iv))
        dec = cipher.decryptor()
        plain = dec.update(raw) + dec.finalize()
        plain = _pkcs7_unpad(plain)
        msg_len = struct.unpack("!I", plain[16:20])[0]
        msg = plain[20:20 + msg_len].decode("utf-8")
        receiveid = plain[20 + msg_len:].decode("utf-8", "ignore")
        return msg, receiveid

    def encrypt(self, msg: str) -> str:
        if Cipher is None:
            raise RuntimeError("缺少 cryptography 依赖，无法加密企业微信消息")
        msg_b = msg.encode("utf-8")
        raw = os.urandom(16) + struct.pack("!I", len(msg_b)) + msg_b + self.corp_id.encode("utf-8")
        raw = _pkcs7_pad(raw, 32)
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.iv))
        enc = cipher.encryptor()
        out = enc.update(raw) + enc.finalize()
        return base64.b64encode(out).decode("ascii")


class WeComChannel(ChannelAdapter):
    name = "wecom"

    def _crypto(self) -> WeComCrypto:
        return WeComCrypto(self.cfg.get("token", ""),
                           self.cfg.get("encoding_aes_key", ""),
                           self.cfg.get("corp_id", ""))

    # ---- 回调验证 ----
    def verify_url(self, msg_signature: str, timestamp: str, nonce: str,
                   echostr: str) -> Optional[str]:
        crypto = self._crypto()
        if crypto.token and msg_signature:
            expect = crypto.signature(timestamp, nonce, echostr)
            if expect != msg_signature:
                return None
        if self.cfg.get("encoding_aes_key") and echostr:
            plain, _ = crypto.decrypt(echostr)
            return plain
        return echostr

    # ---- 消息接收 ----
    def parse_message(self, body: bytes, query: Dict[str, str]) -> Optional[IncomingMessage]:
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return None
        encrypt = root.findtext("Encrypt")
        crypto = self._crypto()
        msg_sig = query.get("msg_signature", "")
        if encrypt and crypto.token and msg_sig:
            if crypto.signature(query.get("timestamp", ""), query.get("nonce", ""), encrypt) != msg_sig:
                return None
        if encrypt and self.cfg.get("encoding_aes_key"):
            try:
                plain, _ = crypto.decrypt(encrypt)
                root = ET.fromstring(plain)
            except Exception:
                return None
        msg_type = (root.findtext("MsgType") or "").lower()
        if msg_type not in ("text", ""):
            # 非文本消息暂不处理
            return None
        content = root.findtext("Content") or ""
        from_user = root.findtext("FromUserName") or ""
        if not content.strip():
            return None
        return IncomingMessage(
            channel="wecom",
            user_id=from_user,
            text=content.strip(),
            chat_id=from_user,
            session_id=f"wecom:{from_user}",
            raw={"MsgType": msg_type},
        )

    def build_passive_reply(self, to_user: str, from_user: str, content: str) -> str:
        """构造被动回复 XML（若配置了 aes key 则加密）。"""
        ts = str(int(time.time()))
        inner = (
            "<xml><ToUserName><![CDATA[%s]]></ToUserName>"
            "<FromUserName><![CDATA[%s]]></FromUserName>"
            "<CreateTime>%s</CreateTime>"
            "<MsgType><![CDATA[text]]></MsgType>"
            "<Content><![CDATA[%s]]></Content></xml>"
        ) % (to_user, from_user, ts, content)
        crypto = self._crypto()
        if self.cfg.get("encoding_aes_key") and crypto.key:
            enc = crypto.encrypt(inner)
            sig = crypto.signature(ts, "nonce", enc)
            return (
                "<xml><Encrypt><![CDATA[%s]]></Encrypt>"
                "<MsgSignature><![CDATA[%s]]></MsgSignature>"
                "<TimeStamp>%s</TimeStamp><Nonce><![CDATA[nonce]]></Nonce></xml>"
            ) % (enc, sig, ts)
        return inner

    # ---- 主动发送 ----
    async def _access_token(self) -> Optional[str]:
        cache_key = self.cfg.get("corp_id", "") + ":" + self.cfg.get("agent_id", "")
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        if httpx is None:
            return None
        url = f"{WECOM_API}/cgi-bin/gettoken"
        params = {"corpid": self.cfg.get("corp_id", ""),
                  "corpsecret": self.cfg.get("secret", "")}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            data = resp.json()
        if data.get("errcode") == 0:
            token = data["access_token"]
            _TOKEN_CACHE[cache_key] = (token, time.time() + int(data.get("expires_in", 7200)))
            return token
        return None

    async def send_text(self, to_user: str, content: str) -> bool:
        token = await self._access_token()
        if not token or httpx is None:
            return False
        url = f"{WECOM_API}/cgi-bin/message/send?access_token={token}"
        payload = {
            "touser": to_user,
            "msgtype": "text",
            "agentid": int(self.cfg.get("agent_id", 0) or 0),
            "text": {"content": content[:2000]},
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()
        return data.get("errcode") == 0

    async def send_webhook(self, content: str) -> bool:
        url = self.cfg.get("webhook_url", "")
        if not url or httpx is None:
            return False
        payload = {"msgtype": "text", "text": {"content": content[:4000]}}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
        return resp.status_code == 200
