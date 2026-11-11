"""管理员知识库生命周期、对话管理与重启持久化测试。"""
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

import yaml
from fastapi.testclient import TestClient

_tmp = Path(tempfile.mkdtemp(prefix="codelib_admin_ws_"))
alpha = _tmp / "alpha"
beta = _tmp / "beta"
for path, name in ((alpha, "alpha"), (beta, "beta")):
    path.mkdir()
    (path / "service.py").write_text(
        f"class Service:\n    def run(self):\n        return '{name}'\n", encoding="utf-8")
config = _tmp / "config.yaml"
config.write_text(yaml.safe_dump({
    "app": {"data_dir": str(_tmp / "data"), "read_only": True},
    "auth": {"bootstrap_admin": {"username": "admin", "password": "admin-pass-1"}},
    "default_workspace": "alpha",
    "workspaces": [{"id": "alpha", "name": "Alpha", "path": str(alpha)}],
    "llm": {"type": "openai_compatible", "base_url": "", "api_key": "", "model": ""},
    "knowledge": {"enabled": True}, "memory": {"enabled": True}, "channels": {},
}, allow_unicode=True), encoding="utf-8")
os.environ["CODELIB_CONFIG"] = str(config)

from app.main import create_app


def login(client: TestClient, username="admin", password="admin-pass-1"):
    result = client.post("/api/auth/login", json={"username": username, "password": password})
    assert result.status_code == 200, result.text


def main() -> int:
    with TestClient(create_app(str(config))) as admin:
        login(admin)
        rebuilt = admin.post("/api/admin/workspaces/alpha/rebuild", json={"full": True})
        assert rebuilt.status_code == 200, rebuilt.text
        assert rebuilt.json()["knowledge"]["totals"]["chunks"] > 0

        updated = admin.put("/api/admin/workspaces/alpha", json={
            "name": "Alpha 主库", "path": str(alpha), "description": "核心业务代码",
        })
        assert updated.status_code == 200, updated.text
        assert updated.json()["workspace"]["name"] == "Alpha 主库"

        created = admin.post("/api/admin/workspaces", json={
            "id": "beta", "name": "Beta", "path": str(beta), "description": "动态库",
        })
        assert created.status_code == 200, created.text
        incremental = admin.post("/api/admin/workspaces/beta/rebuild", json={"full": False})
        assert incremental.status_code == 200 and incremental.json()["full"] is False

        made_user = admin.post("/api/admin/users", json={
            "username": "reader", "password": "reader-pass-1", "role": "user",
            "workspace_ids": ["alpha", "beta"],
        })
        assert made_user.status_code == 200, made_user.text
        conversation = admin.post("/api/conversations", json={"workspace": "alpha"}).json()["conversation"]
        renamed = admin.patch(f"/api/conversations/{conversation['id']}", json={"title": "架构讨论"})
        assert renamed.status_code == 200 and renamed.json()["conversation"]["title"] == "架构讨论"
        conversations = admin.get("/api/conversations").json()["conversations"]
        assert conversations[0]["owner_username"] == "admin"
        assert "message_count" in conversations[0]

        with TestClient(create_app(str(config))) as ordinary:
            login(ordinary, "reader", "reader-pass-1")
            assert ordinary.put("/api/admin/workspaces/alpha", json={
                "name": "bad", "path": str(alpha),
            }).status_code == 403
            assert ordinary.post("/api/admin/workspaces/alpha/rebuild", json={}).status_code == 403
            assert ordinary.delete("/api/admin/workspaces/alpha").status_code == 403

        assert admin.delete("/api/admin/workspaces/beta").status_code == 200
        assert admin.post("/api/admin/workspaces/beta/rebuild", json={}).status_code == 404
        assert admin.delete("/api/admin/workspaces/alpha").status_code == 200
        assert admin.get("/api/admin/workspaces").json()["workspaces"] == []
        assert admin.get("/api/conversations").json()["conversations"] == []

    with TestClient(create_app(str(config))) as restarted:
        login(restarted)
        assert restarted.get("/api/admin/workspaces").json()["workspaces"] == []
        recreated = restarted.post("/api/admin/workspaces", json={
            "id": "alpha", "name": "Alpha 恢复", "path": str(alpha), "description": "重新启用",
        })
        assert recreated.status_code == 200, recreated.text

    with TestClient(create_app(str(config))) as restarted:
        login(restarted)
        workspaces = restarted.get("/api/admin/workspaces").json()["workspaces"]
        assert len(workspaces) == 1 and workspaces[0]["name"] == "Alpha 恢复"
        assert workspaces[0]["source"] == "config" and workspaces[0]["ready"] is True

    print("管理员知识库生命周期与对话管理测试通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
