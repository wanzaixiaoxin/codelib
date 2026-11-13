"""独立进程检查：research.enabled=false 时 /api/research/ask 必须 404。
用法: python -B loop/probe_off_check.py <ROOT> <NLP_WS_PATH>
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
NLP_WS = sys.argv[2]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import yaml

tmp = Path(tempfile.mkdtemp(prefix="codelib_off_"))
(tmp / "data").mkdir(parents=True)
cfgp = tmp / "config.yaml"
cfgp.write_text(yaml.safe_dump({
    "app": {"host": "127.0.0.1", "port": 8000, "data_dir": str(tmp / "data")},
    "auth": {"bootstrap_admin": {"username": "admin", "password": "r3-test-pass"}},
    "workspace": {"id": "nlp", "name": "前置NLP", "path": NLP_WS},
    "llm": {"type": "openai_compatible", "base_url": "", "model": ""},
    "channels": {},
    "research": {"enabled": False},
}, allow_unicode=True), encoding="utf-8")
os.environ["CODELIB_CONFIG"] = str(cfgp)
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

c = TestClient(app)
c.post("/api/auth/login", json={"username": "admin", "password": "r3-test-pass"})
r = c.post("/api/research/ask", json={"question": "x"})
print(r.status_code)
