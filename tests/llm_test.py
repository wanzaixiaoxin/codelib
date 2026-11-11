"""大模型配置与请求构造测试（不联网）。运行：python tests/llm_test.py"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.config import LLMConfig  # noqa: E402
from app.llm import LLMClient  # noqa: E402


def main() -> int:
    # 未配置 -> 不可用
    assert not LLMConfig.from_dict({}).available

    # OpenAI 兼容
    c = LLMConfig.from_dict({"type": "openai_compatible",
                             "base_url": "https://api.deepseek.com/v1",
                             "api_key": "k", "model": "deepseek-chat"})
    assert c.available
    method, url, headers, body = LLMClient(c).build_request("sys", "hi")
    print("[openai_compatible]", method, url)
    assert url.endswith("/chat/completions") and headers["Authorization"] == "Bearer k"
    assert body["model"] == "deepseek-chat"

    # Azure OpenAI
    az = LLMConfig.from_dict({"type": "azure_openai", "base_url": "https://r.openai.azure.com",
                              "api_key": "kaz", "model": "gpt-4o", "deployment": "gpt-4o",
                              "api_version": "2024-02-01"})
    method, url, headers, body = LLMClient(az).build_request("sys", "hi")
    print("[azure_openai]", method, url)
    assert "/openai/deployments/gpt-4o/chat/completions" in url and "api-version=2024-02-01" in url
    assert headers["api-key"] == "kaz"

    # Anthropic
    cl = LLMConfig.from_dict({"type": "anthropic", "base_url": "https://api.anthropic.com",
                              "api_key": "kcl", "model": "claude-3-5-sonnet-latest"})
    method, url, headers, body = LLMClient(cl).build_request("sys", "hi")
    print("[anthropic]", method, url)
    assert url.endswith("/v1/messages") and headers["x-api-key"] == "kcl" and body["system"] == "sys"

    # Gemini
    gm = LLMConfig.from_dict({"type": "gemini",
                              "base_url": "https://generativelanguage.googleapis.com",
                              "api_key": "kgm", "model": "gemini-1.5-flash"})
    method, url, headers, body = LLMClient(gm).build_request("sys", "hi")
    print("[gemini]", method, url)
    assert ":generateContent" in url and "key=kgm" in url and "contents" in body

    # 本地模型无需 key
    local = LLMConfig.from_dict({"type": "openai_compatible",
                                 "base_url": "http://127.0.0.1:11434/v1",
                                 "model": "qwen2.5-coder:7b"})
    assert local.available and not local.requires_key

    print("\n大模型配置与请求构造测试通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
