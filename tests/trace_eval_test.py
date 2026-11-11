"""留痕与评估测试。运行：python tests/trace_eval_test.py（不联网，使用假 LLM）"""
from __future__ import annotations

import asyncio
import json
import os
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

ANSWER = "结论：Service.run 用于启动服务。依据 src/app.py:4-9。"
JUDGE = json.dumps({
    "verdict": "good", "score": 88, "faithful": True, "relevant": True,
    "complete": False, "off_topic": False,
    "reasons": ["回答与检索片段一致"],
    "harness_improvements": ["应召回启动脚本 startup.sh", "可提高 knowledge.retrieve.budget_tokens"],
}, ensure_ascii=False)


class _H(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        content = JUDGE if "评审" in self._last_prompt() else ANSWER
        body = {"choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    _P = b""

    def _last_prompt(self) -> str:
        return ""

    def log_message(self, *a):
        pass


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="codelib_te_"))
    lib = tmp / "lib"
    (lib / "src").mkdir(parents=True, exist_ok=True)
    (lib / "src" / "app.py").write_text(
        '"""模块。"""\n\n\nclass Service:\n    """服务。"""\n\n    def run(self):\n'
        '        """启动服务。"""\n        return "ok"\n', encoding="utf-8")

    import yaml
    cfgf = tmp / "config.yaml"
    cfgf.write_text(yaml.safe_dump({
        "app": {"data_dir": str(tmp / "data")},
        "workspace": {"id": "lib", "name": "lib", "path": str(lib)},
        "llm": {"type": "openai_compatible", "base_url": f"http://127.0.0.1:{port}/v1",
                "api_key": "x", "model": "fake", "max_tokens": 800},
        "memory": {"enabled": True}, "knowledge": {"enabled": True}, "channels": {},
    }, allow_unicode=True), encoding="utf-8")

    from app.bot import BotService
    from app.config import AppConfig
    from app.evaluate import judge_trace
    from app.knowledge.build import build_workspace
    from app.store import Store

    cfg = AppConfig.load(str(cfgf))
    store = Store(cfg.db_path)
    ws = cfg.workspace("lib")
    build_workspace(cfg, ws, full=True)
    bot = BotService(cfg, store)

    print("== 1. 问答产生 trace ==")
    ans = asyncio.run(bot.handle("lib", "Service.run 是做什么的？", "s1"))
    print("trace_id:", ans.trace_id, "llm_used:", ans.llm_used)
    assert ans.trace_id, "应产生 trace_id"

    t = store.get_trace(ans.trace_id)
    stages = [e["stage"] for e in t["events"]]
    print("stages:", stages)
    for need in ("sanitized", "intent", "retrieve", "prompt", "llm", "answer"):
        assert need in stages, f"缺少阶段 {need}"

    ev = {e["stage"]: e["data"] for e in t["events"]}
    print("\n== 2. 记录内容完整 ==")
    assert ev["prompt"]["system"] and ev["prompt"]["user"], "应记录实际下发的 prompt"
    assert "Service.run 是做什么的" in ev["prompt"]["user"], "prompt 应含问题"
    assert ev["llm"]["content"], "应记录模型原文输出"
    assert ev["llm"]["usage"], "应记录 usage"
    print("prompt user_tokens:", ev["prompt"]["user_tokens"], "| llm usage:", ev["llm"]["usage"])

    print("\n== 3. 列表可见且带未评估状态 ==")
    lst = store.list_traces("lib")
    assert any(x["trace_id"] == ans.trace_id for x in lst)
    print("traces:", len(lst), "verdict:", [x["verdict"] for x in lst])

    print("\n== 4. 规则评估 ==")
    res = asyncio.run(judge_trace(t, bot.llm))
    print("rule checks:")
    for c in res["rule"]["checks"]:
        print(("  OK " if c["ok"] else "  X  ") + c["name"] + ": " + c["detail"])
    assert any(c["name"] == "检索命中" for c in res["rule"]["checks"])
    assert "verdict" in res and "score" in res

    print("\n== 5. 落库评估并回读 ==")
    aid = store.add_audit(ans.trace_id, "lib", res["verdict"], res["score"], res)
    t2 = store.get_trace(ans.trace_id)
    assert t2["audits"] and t2["audits"][-1]["id"] == aid
    print("audit_id:", aid, "verdict:", t2["audits"][-1]["verdict"], "score:", t2["audits"][-1]["score"])
    print("improvements:", json.dumps(res["improvements"], ensure_ascii=False)[:300])

    srv.shutdown()
    print("\n留痕与评估测试通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
