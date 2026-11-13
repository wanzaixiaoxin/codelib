"""单个大模型客户端（OpenAI 兼容 / Azure / Anthropic / Gemini）。

针对推理型模型做了兼容：若响应 finish_reason=length 且 content 为空（推理把
max_tokens 用光了），会自动放大 max_tokens 重试一次。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from .config import LLMConfig

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None  # type: ignore


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.last_error: str = ""
        self._min_tokens: int = 0   # 进程内记忆：该模型至少需要的 max_tokens
        self.last_raw_usage: dict = {}      # 最近一次调用的 usage（prompt/completion tokens）
        self.last_raw_finish: str | None = None  # 最近一次 finish_reason

    @property
    def available(self) -> bool:
        return self.cfg.available and httpx is not None

    @property
    def name(self) -> str:
        return self.cfg.type

    def build_request(self, system: str, user: str,
                      max_tokens: Optional[int] = None
                      ) -> Tuple[str, str, Dict[str, str], Dict[str, Any]]:
        c = self.cfg
        mt = int(max_tokens or c.max_tokens)
        ptype = (c.type or "openai_compatible").lower()
        base = (c.base_url or "").rstrip("/")

        if ptype == "azure_openai":
            deploy = c.deployment or c.model
            ver = c.api_version or "2024-02-01"
            url = f"{base}/openai/deployments/{deploy}/chat/completions?api-version={ver}"
            headers = {"api-key": c.api_key, "Content-Type": "application/json"}
            body = {"messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}],
                    "temperature": c.temperature, "max_tokens": mt}
            return "POST", url, headers, body

        if ptype == "anthropic":
            url = f"{base or 'https://api.anthropic.com'}/v1/messages"
            headers = {"x-api-key": c.api_key, "anthropic-version": "2023-06-01",
                       "Content-Type": "application/json"}
            body = {"model": c.model, "max_tokens": mt, "temperature": c.temperature,
                    "system": system, "messages": [{"role": "user", "content": user}]}
            return "POST", url, headers, body

        if ptype == "gemini":
            url = (f"{base or 'https://generativelanguage.googleapis.com'}"
                   f"/v1beta/models/{c.model}:generateContent?key={c.api_key}")
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"parts": [{"text": user}]}],
                    "generationConfig": {"temperature": c.temperature,
                                         "maxOutputTokens": mt}}
            return "POST", url, {"Content-Type": "application/json"}, body

        url = f"{base or 'https://api.openai.com/v1'}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if c.api_key:
            headers["Authorization"] = f"Bearer {c.api_key}"
        body = {"model": c.model, "temperature": c.temperature, "max_tokens": mt,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        return "POST", url, headers, body

    # ---------------- 解析 ----------------
    @staticmethod
    def _parse(ptype: str, data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        t = (ptype or "").lower()
        if t == "anthropic":
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict)).strip()
            return (text or None), data.get("stop_reason")
        if t == "gemini":
            cands = data.get("candidates") or []
            if not cands:
                return None, None
            parts = ((cands[0].get("content") or {}).get("parts") or [])
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
            return (text or None), cands[0].get("finishReason")
        choices = data.get("choices") or []
        if not choices:
            return None, None
        ch = choices[0]
        msg = ch.get("message") or {}
        content = (msg.get("content") or "").strip()
        return (content or None), ch.get("finish_reason")

    async def _send(self, system: str, user: str, max_tokens: int
                    ) -> Tuple[Optional[str], Optional[str]]:
        method, url, headers, body = self.build_request(system, user, max_tokens)
        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:  # type: ignore
            resp = await client.request(method, url, headers=headers,
                                        content=json.dumps(body))
            resp.raise_for_status()
            data = resp.json()
        self.last_raw_usage = data.get("usage") or {}
        self.last_raw_finish = None
        content, finish = self._parse(self.cfg.type, data)
        self.last_raw_finish = finish
        return content, finish

    async def chat(self, system: str, user: str) -> Optional[str]:
        if not self.available:
            return None
        self.last_raw_usage = {}
        self.last_raw_finish = None
        try:
            start = max(int(self.cfg.max_tokens), self._min_tokens)
            content, finish = await self._send(system, user, start)
            # 推理型模型可能把 max_tokens 用在 reasoning 上导致 content 为空
            if not content and (finish or "").lower() in ("length", "max_tokens"):
                bigger = min(8192, max(int(self.cfg.max_tokens) * 3, 4096))
                self._min_tokens = bigger          # 记住，后续请求直接用更大值
                content, finish = await self._send(system, user, bigger)
            if content:
                self.last_error = ""
                return content
            self.last_error = f"模型返回空内容（finish_reason={finish}）"
            return f"__LLM_ERROR__{self.last_error}"
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return f"__LLM_ERROR__{self.last_error}"

    async def ping(self) -> Dict[str, Any]:
        text = await self.chat("你是连通性测试助手。", "回复两个字：正常")
        ok = bool(text) and not str(text).startswith("__LLM_ERROR__")
        return {"ok": ok, "type": self.cfg.type, "model": self.cfg.model,
                "reply": text, "error": self.last_error}

    # ---------------- R2: function calling（仅 openai_compatible） ----------------
    async def chat_messages(self, messages: List[Dict[str, Any]],
                            tools: Optional[List[Dict[str, Any]]] = None,
                            max_tokens: Optional[int] = None) -> Dict[str, Any]:
        """OpenAI 兼容的多轮 + 工具调用。

        返回 {"content": str, "tool_calls": [{"id","name","arguments"}], "usage"}。
        其它协议返回 __UNSUPPORTED__ 供调用方降级（本地网关即 openai_compatible）。
        """
        c = self.cfg
        ptype = (c.type or "openai_compatible").lower()
        if ptype != "openai_compatible":
            return {"content": "__UNSUPPORTED__", "tool_calls": [], "usage": {}}
        mt = int(max_tokens or c.max_tokens)
        url = (c.base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if c.api_key:
            headers["Authorization"] = f"Bearer {c.api_key}"
        body: Dict[str, Any] = {"model": c.model, "temperature": c.temperature,
                                "max_tokens": mt, "messages": messages}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
            body["tool_choice"] = "auto"
        async with httpx.AsyncClient(timeout=c.timeout) as client:  # type: ignore
            resp = await client.request("POST", url, headers=headers,
                                        content=json.dumps(body))
            resp.raise_for_status()
            data = resp.json()
        self.last_raw_usage = data.get("usage") or {}
        msg = ((data.get("choices") or [{}])[0].get("message") or {})
        tcs = [{"id": t.get("id", ""), "name": (t.get("function") or {}).get("name", ""),
                "arguments": (t.get("function") or {}).get("arguments", "{}")}
               for t in (msg.get("tool_calls") or []) if isinstance(t, dict)]
        return {"content": (msg.get("content") or "").strip(),
                "tool_calls": tcs, "usage": self.last_raw_usage}
