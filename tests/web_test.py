"""网页对话（会话/历史/多轮）测试。运行：python tests/web_test.py"""
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
except Exception:
    pass

import yaml  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="codelib_web_"))
_cfg = _tmp / "config.yaml"
_cfg.write_text(yaml.safe_dump({
    "app": {"host": "127.0.0.1", "port": 8000, "data_dir": str(_tmp / "data")},
    "auth": {"bootstrap_admin": {"username": "admin", "password": "test-admin-pass"}},
    "workspace": {"id": "sample", "name": "sample", "path": str(ROOT / "sample_workspace")},
    "llm": {"type": "openai_compatible", "base_url": "", "api_key": "", "model": ""},
    "memory": {"enabled": True},
    "channels": {},
}, allow_unicode=True), encoding="utf-8")
os.environ["CODELIB_CONFIG"] = str(_cfg)

from fastapi.testclient import TestClient  # noqa: E402

from app import indexer  # noqa: E402
from app.config import AppConfig  # noqa: E402
from app.main import app  # noqa: E402
from app.store import Store  # noqa: E402


def main() -> int:
    cfg = AppConfig.load(str(_cfg))
    store = Store(cfg.db_path)
    for ws in cfg.workspaces:
        indexer.index_workspace(store, ws, full=True)

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "test-admin-pass"})
        assert login.status_code == 200, login.text
        r = client.get("/")
        print("GET / ->", r.status_code, "含新建对话:", "新建对话" in r.text)
        assert r.status_code == 200 and "新建对话" in r.text
        assert '<script src="/static/markdown-it.min.js"></script>' in r.text
        assert "bub.innerHTML=renderMarkdown(text)" in r.text
        vendor = client.get("/static/markdown-it.min.js")
        assert vendor.status_code == 200 and "markdown-it 14.1.0" in vendor.text[:100]

        ws = client.get("/api/workspaces").json()["workspaces"]
        print("workspaces:", ws)
        assert ws
        wid = ws[0]["id"]

        r = client.post("/api/conversations", json={"workspace": wid})
        conv = r.json()["conversation"]
        sid = conv["id"]
        print("new conversation:", sid, "workspace:", conv["workspace_id"])
        assert conv["workspace_id"] == wid

        r = client.post(f"/api/conversations/{sid}/messages",
                        json={"content": "消息网关如何路由消息？"})
        d = r.json()
        print("msg1 -> answer_len:", len(d["answer"]), "sources:", d["sources"])
        assert d["answer"] and d["sources"] and len(d["messages"]) == 2

        r = client.post(f"/api/conversations/{sid}/messages",
                        json={"content": "它的入口在哪"})
        d2 = r.json()
        print("msg2(追问) -> messages:", len(d2["messages"]))
        assert len(d2["messages"]) == 4

        lst = client.get("/api/conversations").json()["conversations"]
        print("history count:", len(lst), "title:", lst[0]["title"])
        assert any(x["id"] == sid for x in lst)
        assert lst[0]["title"] != "新对话"

        detail = client.get(f"/api/conversations/{sid}").json()
        assert len(detail["messages"]) == 4
        assert detail["conversation"]["workspace_id"] == wid

        r = client.delete(f"/api/conversations/{sid}")
        assert r.json()["ok"]
        assert client.get(f"/api/conversations/{sid}").status_code == 404

    print("\n网页对话测试通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
