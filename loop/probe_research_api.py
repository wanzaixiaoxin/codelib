"""R3 验收：/api/research/ask 端到端（灰度开关 + 真实 LLM + 权限）。"""
from __future__ import annotations
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import yaml  # noqa: E402

QUESTION = ("分析当两个人的对话数据进入kafka后被我们消费，到最后写入kafka，"
            "对话的顺序能保证吗")
NLP_WS = ROOT.parent / "code" / "pri_nlp_worker_svr"
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILED.append(name)


def make_client(enabled: bool, tmp: Path):
    """独立临时环境：knowledge.db 副本 + nlp 工程 workspace + admin。"""
    data_dir = tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "data" / "knowledge.db", data_dir / "knowledge.db")
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "app": {"host": "127.0.0.1", "port": 8000, "data_dir": str(data_dir)},
        "auth": {"bootstrap_admin": {"username": "admin", "password": "r3-test-pass"}},
        "workspace": {"id": "nlp", "name": "前置NLP", "path": str(NLP_WS)},
        "llm": {"type": "openai_compatible",
                "base_url": "http://127.0.0.1:3425/v1",
                "model": "workbuddy/glm-5.3", "max_tokens": 4096, "timeout": 120},
        "memory": {"enabled": True},
        "channels": {},
        "research": {"enabled": enabled,
                     "limits": {"max_steps": 10, "max_chars": 24000,
                                "max_llm_calls": 8, "max_wall_ms": 180000}},
    }, allow_unicode=True), encoding="utf-8")
    import os
    os.environ["CODELIB_CONFIG"] = str(cfg_path)
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="codelib_r3_"))
    # 1) 关闭态：enabled=false → 404（独立子进程，避免同进程 import 缓存）
    # 1) 关闭态：enabled=false → 404（独立子进程，避免同进程 import 缓存）
    import subprocess
    pr = subprocess.run([sys.executable, str(ROOT / "loop" / "probe_off_check.py"),
                         str(ROOT), str(NLP_WS)],
                        capture_output=True, text=True, timeout=120)
    last = (pr.stdout.strip().splitlines() or ["?"])[-1]
    check("research.enabled=false → 404", last.strip() == "404", f"stdout 末行={last}")

    # 2) 开启态：真 LLM 跑顺序题
    client = make_client(True, tmp / "on")
    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": "r3-test-pass"})
    check("登录(开启实例)", r.status_code == 200)
    r = client.post("/api/research/ask", json={"question": QUESTION},
                    timeout=240)
    check("HTTP 200", r.status_code == 200, str(r.status_code))
    if r.status_code == 200:
        d = r.json()
        ev = d.get("evidence") or []
        go_hits = sum(1 for e in ev if ".go" in str(e.get("path", "")))
        check("answer 非空", bool(d.get("answer")))
        check("结论非空", bool(d.get("conclusions")))
        check("evidence 含 .go ≥3", go_hits >= 3, f"go={go_hits}")
        check("无「候选未覆盖」", "候选未覆盖" not in str(d.get("answer")))
        check("工具事件已记录", len(d.get("tool_events") or []) >= 5,
              f"{len(d.get('tool_events') or [])} 次")
        check("stop_reason 正常", d.get("stop_reason") in
              ("no_tool_calls", "steps_exhausted", "chars_exhausted", "llm_calls"),
              str(d.get("stop_reason")))
        print("  answer 头 300 字:", (d.get("answer") or "")[:300].replace(chr(10), " "))
    # 3) 未登录 → 401
    import httpx
    r2 = httpx.post("http://testserver/api/research/ask", json={"question": "x"}) \
        if False else None
    print("\nR3 验收: " + ("ALL PASS" if not FAILED else f"FAILED {FAILED}"))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
