"""HTTP 图层冒烟测试（极简接口）。运行：python tests/api_test.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import yaml  # noqa: E402

# 用临时配置，保证测试与本地真实工程/模型解耦
_tmp = Path(tempfile.mkdtemp(prefix="codelib_api_"))
_cfg = _tmp / "config.yaml"
_cfg.write_text(yaml.safe_dump({
    "app": {"host": "127.0.0.1", "port": 8000, "data_dir": str(_tmp / "data")},
    "workspace": {"id": "sample", "name": "sample", "path": str(ROOT / "sample_workspace")},
    "llm": {"type": "openai_compatible", "base_url": "", "api_key": "", "model": ""},
    "memory": {"enabled": True},
    "channels": {},
}, allow_unicode=True), encoding="utf-8")
os.environ["CODELIB_CONFIG"] = str(_cfg)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def main() -> int:
    from app import indexer
    from app.config import AppConfig
    from app.store import Store
    cfg = AppConfig.load(os.environ["CODELIB_CONFIG"])
    store = Store(cfg.db_path)
    for ws in cfg.workspaces:
        if store.stats(ws.id)["chunks"] == 0:
            indexer.index_workspace(store, ws, full=True)

    with TestClient(app) as client:
        print("== /healthz ==")
        r = client.get("/healthz")
        print(r.status_code, r.json())
        assert r.status_code == 200 and r.json()["ok"]

        print("\n== /ask ==")
        r = client.post("/ask", json={"question": "消息网关如何路由消息？"})
        d = r.json()
        print(r.status_code, "answer_len=", len(d.get("answer", "")), "sources=", d.get("sources"))
        # 无 LLM 且知识库为空时，返回检索摘要（answer 非空即可；sources 视是否命中而可能为空）
        assert r.status_code == 200 and d.get("answer")

        print("\n== /ask（用 name 当 workspace，应能命中）==")
        r = client.post("/ask", json={"question": "消息网关如何路由消息？", "workspace": "sample"})
        assert r.status_code == 200 and r.json().get("answer"), r.text

        print("\n== /ask?debug=true ==")
        r = client.post("/ask", json={"question": "消息网关如何路由消息？", "debug": True})
        assert r.status_code == 200 and "debug" in r.json()

        print("\n== /channels/wecom/callback (URL verify) ==")
        r = client.get("/channels/wecom/callback", params={"echostr": "hello-wecom"})
        print(r.status_code, r.text)
        assert r.text == "hello-wecom"

        print("\n== /channels/feishu/event (url_verification) ==")
        r = client.post("/channels/feishu/event", json={"type": "url_verification", "challenge": "abc123"})
        print(r.status_code, r.json())
        assert r.json()["challenge"] == "abc123"

        print("\n== /channels/feishu/event (文本消息) ==")
        event = {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "u1"}},
                "message": {"message_type": "text",
                            "content": "{\"text\": \"消息网关如何路由消息？\"}",
                            "chat_id": "c1", "message_id": "m1"},
            },
        }
        r = client.post("/channels/feishu/event", json=event)
        print(r.status_code, r.json())
        assert r.status_code == 200 and r.json()["code"] == 0

    print("\nHTTP 层测试通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
