"""账号、角色、知识库授权和会话隔离测试。运行：python tests/auth_test.py"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="codelib_auth_"))


def make_workspace(name: str) -> Path:
    path = tmp / name
    (path / "src").mkdir(parents=True)
    (path / "src" / "service.py").write_text(
        f'"""{name} service."""\n\nclass Service:\n'
        f'    def run(self):\n        return "{name}"\n', encoding="utf-8")
    return path


alpha = make_workspace("alpha")
beta = make_workspace("beta")
gamma = make_workspace("gamma")
config_file = tmp / "config.yaml"
config_file.write_text(yaml.safe_dump({
    "app": {"host": "127.0.0.1", "data_dir": str(tmp / "data")},
    "auth": {"bootstrap_admin": {"username": "rootadmin", "password": "initial-admin-pass"}},
    "workspaces": [
        {"id": "alpha", "name": "Alpha", "path": str(alpha)},
        {"id": "beta", "name": "Beta", "path": str(beta)},
    ],
    "llm": {"type": "openai_compatible", "base_url": "", "api_key": "", "model": ""},
    "knowledge": {"enabled": True}, "memory": {"enabled": True}, "channels": {},
}, allow_unicode=True), encoding="utf-8")
os.environ["CODELIB_CONFIG"] = str(config_file)

from fastapi.testclient import TestClient  # noqa: E402
from app.config import AppConfig  # noqa: E402
from app.knowledge.build import build_workspace  # noqa: E402
from app.main import app  # noqa: E402
from app.store import Store  # noqa: E402


def login(client: TestClient, username: str, password: str) -> None:
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text


def main() -> int:
    cfg = AppConfig.load(str(config_file))
    for ws in cfg.workspaces:
        build_workspace(cfg, ws, full=True)

    with TestClient(app) as admin, TestClient(app) as ordinary:
        assert ordinary.get("/api/workspaces").status_code == 401
        login(admin, "rootadmin", "initial-admin-pass")
        assert {w["id"] for w in admin.get("/api/workspaces").json()["workspaces"]} == {"alpha", "beta"}

        created = admin.post("/api/admin/users", json={
            "username": "alice", "password": "ordinary-pass-1", "role": "user",
            "workspace_ids": ["Alpha"],
        })
        assert created.status_code == 200, created.text
        alice_id = created.json()["user"]["id"]
        assert created.json()["user"]["workspace_ids"] == ["alpha"]

        admin_conv = admin.post("/api/conversations", json={"workspace": "beta"}).json()["conversation"]
        admin_alpha_conv = admin.post("/api/conversations", json={"workspace": "alpha"}).json()["conversation"]
        new_ws = admin.post("/api/admin/workspaces", json={
            "id": "gamma", "name": "Gamma", "path": str(gamma),
        })
        assert new_ws.status_code == 200, new_ws.text
        grant = admin.put(f"/api/admin/users/{alice_id}/workspaces",
                          json={"workspace_ids": ["alpha", "gamma"]})
        assert grant.status_code == 200, grant.text

        login(ordinary, "alice", "ordinary-pass-1")
        visible = ordinary.get("/api/workspaces").json()["workspaces"]
        assert {w["id"] for w in visible} == {"alpha", "gamma"}
        assert all("path" not in w for w in visible)
        assert ordinary.post("/api/conversations", json={"workspace": "beta"}).status_code == 403
        own_conv = ordinary.post("/api/conversations", json={"workspace": "alpha"})
        assert own_conv.status_code == 200, own_conv.text
        own_id = own_conv.json()["conversation"]["id"]
        assert ordinary.get(f"/api/conversations/{own_id}").status_code == 200
        assert ordinary.post(f"/api/conversations/{own_id}/messages", json={"content": "/index"}).status_code == 403
        assert ordinary.get(f"/api/conversations/{admin_conv['id']}").status_code == 404
        assert ordinary.get("/api/admin/users").status_code == 403
        assert ordinary.get("/api/admin/workspaces").status_code == 403
        assert ordinary.post("/api/admin/users", json={
            "username": "mallory", "password": "ordinary-pass-2",
        }).status_code == 403
        assert ordinary.post("/ask", json={"workspace": "beta", "question": "explain"}).status_code == 403
        assert ordinary.post("/ask", json={"workspace": "alpha", "question": "/remember secret"}).status_code == 403
        assert ordinary.post("/ask", json={"workspace": "alpha", "question": "Service.run 是什么？"}).status_code == 200
        listed = ordinary.get("/api/conversations").json()["conversations"]
        listed_ids = {c["id"] for c in listed}
        assert own_id in listed_ids
        assert admin_conv["id"] not in listed_ids
        admin_listed_ids = {c["id"] for c in admin.get("/api/conversations").json()["conversations"]}
        assert own_id in admin_listed_ids and admin_alpha_conv["id"] in admin_listed_ids

        revoked = admin.put(f"/api/admin/users/{alice_id}/workspaces",
                            json={"workspace_ids": ["gamma"]})
        assert revoked.status_code == 200, revoked.text
        assert all(c["id"] != own_id for c in ordinary.get("/api/conversations").json()["conversations"])
        assert ordinary.get(f"/api/conversations/{own_id}").status_code == 403

        reset = admin.put(f"/api/admin/users/{alice_id}/password", json={"password": "replacement-pass"})
        assert reset.status_code == 200, reset.text
        assert ordinary.get("/api/auth/me").status_code == 401
        assert ordinary.post("/api/auth/login", json={
            "username": "alice", "password": "ordinary-pass-1",
        }).status_code == 401
        login(ordinary, "alice", "replacement-pass")

        disabled = admin.patch(f"/api/admin/users/{alice_id}/active", json={"active": False})
        assert disabled.status_code == 200, disabled.text
        assert ordinary.get("/api/auth/me").status_code == 401

    # Existing installations gain an owner column without losing saved conversations.
    legacy_db = tmp / "legacy.db"
    conn = sqlite3.connect(legacy_db)
    conn.execute("CREATE TABLE chat_sessions (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, title TEXT DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL)")
    conn.commit()
    conn.close()
    Store(legacy_db)
    conn = sqlite3.connect(legacy_db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(chat_sessions)")}
    conn.close()
    assert "owner_user_id" in cols

    print("账号角色、知识库授权、会话隔离测试通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
