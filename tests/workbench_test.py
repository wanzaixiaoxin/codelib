"""Web 工作台（多库 + 只读）测试。运行：python tests/workbench_test.py"""
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

_tmp = Path(tempfile.mkdtemp(prefix="codelib_wb_"))

# 造两个“库”：拷贝 sample_workspace，再加一个 docs 目录
def _mk_lib(name: str) -> Path:
    d = _tmp / name
    (d / "src").mkdir(parents=True, exist_ok=True)
    (d / "docs").mkdir(parents=True, exist_ok=True)
    (d / "src" / "app.py").write_text(
        f'"""模块 {name}。"""\n\n\nclass Service:\n    """服务 {name} 的入口。"""\n\n'
        f'    def run(self):\n        """启动 {name} 服务。"""\n        return "{name}"\n', encoding="utf-8")
    (d / "docs" / "readme.md").write_text(
        f"# {name} 说明\n\n## 用途\n\n这是 {name} 的用途说明。\n\n## 启动\n\n运行 Service.run 启动。\n",
        encoding="utf-8")
    return d


lib_a = _mk_lib("alpha")
lib_b = _mk_lib("beta")

_cfg = _tmp / "config.yaml"
_cfg.write_text(yaml.safe_dump({
    "app": {"host": "127.0.0.1", "port": 8000, "data_dir": str(_tmp / "data"), "read_only": True},
    "default_workspace": "alpha",
    "workspaces": [
        {"id": "alpha", "name": "Alpha 库", "path": str(lib_a)},
        {"id": "beta", "name": "Beta 库", "path": str(lib_b)},
    ],
    "llm": {"type": "openai_compatible", "base_url": "", "api_key": "", "model": ""},
    "memory": {"enabled": True},
    "knowledge": {"enabled": True},
    "channels": {},
}, allow_unicode=True), encoding="utf-8")
os.environ["CODELIB_CONFIG"] = str(_cfg)

from fastapi.testclient import TestClient  # noqa: E402

from app.config import AppConfig  # noqa: E402
from app.knowledge.build import build_workspace  # noqa: E402
from app.main import app  # noqa: E402


def main() -> int:
    cfg = AppConfig.load(str(_cfg))
    for ws in cfg.workspaces:
        r = build_workspace(cfg, ws, full=True)
        print("build", ws.id, "chunks", r["totals"]["chunks"])

    with TestClient(app) as client:
        print("\n== GET / ==")
        r = client.get("/")
        assert r.status_code == 200 and "只读" in r.text and "知识库" in r.text

        print("\n== /api/workspaces（多库 + 状态）==")
        d = client.get("/api/workspaces").json()
        print(d)
        assert d["read_only"] is True
        ids = [w["id"] for w in d["workspaces"]]
        assert ids == ["alpha", "beta"]
        assert all(w["ready"] for w in d["workspaces"])

        print("\n== 两个库分别建会话并提问（验证不串库）==")
        for lib in ("alpha", "beta"):
            conv = client.post("/api/conversations", json={"workspace": lib}).json()["conversation"]
            assert conv["workspace_id"] == lib
            m = client.post(f"/api/conversations/{conv['id']}/messages",
                            json={"content": "这个工程是做什么的？"}).json()
            print(lib, "-> answer_len", len(m["answer"]), "sources", m["sources"][:2])
            assert m["answer"] and m["sources"]
            # 来源必须来自该库自己的路径
            assert all(not s.startswith("alpha") or lib == "alpha" for s in m["sources"])

        print("\n== 历史列表 ==")
        convs = client.get("/api/conversations").json()["conversations"]
        print("count", len(convs), [(c["workspace_id"], c.get("empty")) for c in convs])
        assert len(convs) == 2

        print("\n== 空对话唯一性：重复新建不会产生多个空对话 ==")
        # 上面两个库各已有一条"已用"对话（发过消息）；此时各库里没有空对话
        for _ in range(3):
            c1 = client.post("/api/conversations", json={"workspace": "alpha"}).json()
        empties = [c for c in client.get("/api/conversations").json()["conversations"]
                   if c["workspace_id"] == "alpha" and c["empty"]]
        print("alpha 空对话数量:", len(empties))
        assert len(empties) == 1, "同一库最多只能有一个空的新对话"
        assert c1.get("reused") is True or len(empties) == 1

        print("\n== 空对话提问后自动变成正式对话，可再新建一个空对话 ==")
        empty_id = empties[0]["id"]
        client.post(f"/api/conversations/{empty_id}/messages", json={"content": "这个工程是做什么的？"})
        again = client.post("/api/conversations", json={"workspace": "alpha"}).json()
        print("新建后 reused:", again.get("reused"))
        empties2 = [c for c in client.get("/api/conversations").json()["conversations"]
                    if c["workspace_id"] == "alpha" and c["empty"]]
        print("alpha 空对话数量:", len(empties2))
        assert len(empties2) == 1

        print("\n== 只读守卫：写工程类路径应 403 ==")
        for p in ("/api/write", "/api/edit/file", "/api/apply", "/api/git/commit", "/fs/upload"):
            r = client.post(p, json={"x": 1})
            print(p, r.status_code)
            assert r.status_code == 403, p

        print("\n== 只读守卫不影响正常对话接口 ==")
        assert client.get("/api/workspaces").status_code == 200
        assert client.get("/healthz").json()["read_only"] is True

    print("\nWeb 工作台（多库+只读）测试通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
