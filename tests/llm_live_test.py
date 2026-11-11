"""大模型端到端测试：本地假 OpenAI 兼容服务验证真实调用与失败降级。

运行：python tests/llm_live_test.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app import indexer  # noqa: E402
from app.bot import BotService  # noqa: E402
from app.config import AppConfig, IndexConfig, LLMConfig, WorkspaceConfig  # noqa: E402
from app.store import Store  # noqa: E402

ANSWER = "假模型回答：消息网关通过 MessageGateway.route 按命令前缀路由到处理器。"


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            req = {}
        max_tokens = int(req.get("max_tokens", 0) or 0)
        if max_tokens and max_tokens < 2000:
            # 模拟推理模型把 max_tokens 用光：finish=length 且 content 为空
            payload = {"choices": [{"finish_reason": "length",
                                    "message": {"role": "assistant", "content": "",
                                                "reasoning_content": "思考中…"}}]}
        else:
            payload = {"choices": [{"finish_reason": "stop",
                                    "message": {"role": "assistant", "content": ANSWER}}]}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def _cfg(tmp: Path, llm: LLMConfig) -> AppConfig:
    return AppConfig(
        data_dir=str(tmp / "data"), default_workspace="demo",
        workspaces=[WorkspaceConfig(id="demo", name="demo",
                                    path=str(ROOT / "sample_workspace"),
                                    index=IndexConfig(token_budget=800))],
        memory={"enabled": True}, llm=llm,
    )


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"假大模型已启动: 127.0.0.1:{port}")
    try:
        tmp = Path(tempfile.mkdtemp(prefix="codelib_llm_"))

        print("\n== 正常调用（含 max_tokens 截断自动重试）==")
        cfg = _cfg(tmp, LLMConfig.from_dict({
            "type": "openai_compatible",
            "base_url": f"http://127.0.0.1:{port}/v1", "model": "fake-1",
            "max_tokens": 900}))
        store = Store(cfg.db_path)
        indexer.index_workspace(store, cfg.workspace(), full=True)
        ans = asyncio.run(BotService(cfg, store).handle("", "消息网关如何路由消息？", "t1"))
        print("llm_used:", ans.llm_used, "provider:", ans.llm_provider)
        assert ans.llm_used and "假模型回答" in ans.text

        print("\n== 调用失败自动降级 ==")
        cfg2 = _cfg(tmp, LLMConfig.from_dict({
            "type": "openai_compatible",
            "base_url": "http://127.0.0.1:1/v1", "model": "x"}))
        store2 = Store(cfg2.db_path)
        indexer.index_workspace(store2, cfg2.workspace(), full=True)
        ans2 = asyncio.run(BotService(cfg2, store2).handle("", "消息网关如何路由消息？", "t2"))
        print("llm_used:", ans2.llm_used, "| 仍有答案:", bool(ans2.text))
        assert not ans2.llm_used and ans2.text

        print("\n大模型端到端（含降级）测试通过 [OK]")
        return 0
    finally:
        srv.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
