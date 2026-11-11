"""证据门槛 + 同问题缓存 + 评估分流的测试（假 LLM，不联网）。"""
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

ANSWER = "结论：Service.run 用于启动服务，见 src/app.py:4-9。"


class _H(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        body = {"choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": ANSWER}}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 30, "total_tokens": 110}}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="codelib_gate_"))
    lib = tmp / "lib"
    (lib / "src").mkdir(parents=True)
    (lib / "src" / "app.py").write_text(
        '"""应用。"""\n\n\nclass Service:\n    """服务。"""\n\n    def run(self):\n'
        '        """启动服务。"""\n        return "ok"\n', encoding="utf-8")

    import yaml
    cfgf = tmp / "config.yaml"
    cfgf.write_text(yaml.safe_dump({
        "app": {"data_dir": str(tmp / "data")},
        "workspace": {"id": "lib", "name": "lib", "path": str(lib)},
        "llm": {"type": "openai_compatible", "base_url": f"http://127.0.0.1:{port}/v1",
                "api_key": "x", "model": "fake", "max_tokens": 800},
        "memory": {"enabled": True}, "knowledge": {"enabled": True, "retrieve": {"min_coverage": 0.2}},
        "channels": {},
    }, allow_unicode=True), encoding="utf-8")

    from app.bot import BotService
    from app.config import AppConfig
    from app.evaluate import judge_trace
    from app.knowledge.build import build_workspace
    from app.store import Store

    cfg = AppConfig.load(str(cfgf))
    store = Store(cfg.db_path)
    build_workspace(cfg, cfg.workspace("lib"), full=True)
    bot = BotService(cfg, store)

    def run(q, sid):
        a = asyncio.run(bot.handle("lib", q, sid))
        ev = {e["stage"]: e["data"] for e in store.get_trace(a.trace_id)["events"]}
        return a, ev

    print("== 1. 有证据 -> 正常作答 ==")
    a, ev = run("Service.run 是做什么的？", "s1")
    print("llm_used:", a.llm_used, "| gate:", ev.get("gate"))
    assert a.llm_used and not ev.get("gate")

    print("\n== 2. 无证据（库里没有）-> 不调模型、不硬答 ==")
    a2, ev2 = run("ZebraQuantumFlux 的超频参数怎么调？", "s2")
    print("stages:", list(ev2.keys()))
    print("llm_used:", a2.llm_used, "| gate:", ev2.get("gate"))
    print("answer[:80]:", a2.text[:80])
    # 两种拦截路径：召回为空（无 retrieve/gate 事件）或证据覆盖不足（有 gate 事件）
    assert not a2.llm_used, "无证据时不应调用模型硬答"
    assert ("没有找到" in a2.text) or ("未找到" in a2.text) or ("没有在当前工作区索引中" in a2.text), a2.text

    print("\n== 3. 同一问题第二次 -> 命中缓存（不调模型）==")
    a3, ev3 = run("Service.run 是做什么的？", "s2")
    print("llm_used:", a3.llm_used, "| cache:", ev3.get("cache"))
    assert ev3.get("cache"), "应命中同问题缓存"

    print("\n== 4. 负结果不缓存：再问无证据问题仍不调模型 ==")
    a4, ev4 = run("ZebraQuantumFlux 的超频参数怎么调？", "s3")
    print("llm_used:", a4.llm_used, "| cache:", bool(ev4.get("cache")))
    assert not a4.llm_used and not ev4.get("cache"), "负结果不应被缓存复用"

    print("\n== 4.5 中文实用问题不应被误拦（多粒度覆盖）==")
    a45, ev45 = run("Service 是干什么用的？", "s45")
    print("llm_used:", a45.llm_used, "| gate:", bool(ev45.get("gate")))
    assert a45.llm_used and not ev45.get("gate"), "合理的中文问题不应被证据门槛误拦"

    print("\n== 5. 评估：低覆盖问题应标 weight_only_insufficient ==")
    t = store.get_trace(a2.trace_id)
    rep = asyncio.run(judge_trace(t, bot.llm))
    print("verdict:", rep["verdict"], "| score:", rep["score"],
          "| weight_only_insufficient:", rep.get("weight_only_insufficient"))
    kinds = {x["kind"] for x in rep.get("improvement_actions", [])}
    print("improvement kinds:", kinds)
    assert rep["score"] <= 60, "低覆盖问题不应给高分"
    assert rep.get("weight_only_insufficient") is True, "应判定调权重无效"

    srv.shutdown()
    print("\n证据门槛/缓存/评估分流测试通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
