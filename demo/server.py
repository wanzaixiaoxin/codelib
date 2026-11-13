"""人工体验页服务：python -B demo/server.py [--port 8765]
另: python -B demo/server.py --gen-presets  # 生成静态离线数据 presets.js
"""
from __future__ import annotations
import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "loop"))
import compare_core as C  # noqa: E402
from export_failures import norm  # noqa: E402

PRESET_FILE = HERE / "presets.js"
CFG = None


def load_cases():
    import yaml
    rows = yaml.safe_load((ROOT / "loop" / "frozen" / "failure_cases.yaml")
                          .read_text(encoding="utf-8"))
    return rows


def expected_map(rows):
    return {norm(r["question"]): [str(x) for x in (r.get("expected_paths") or [])]
            for r in rows}


def pick_presets(rows, n=8):
    """覆盖不同 workspace 与失败层，优先有期望路径的题。"""
    seen_ws, out = set(), []
    ordered = sorted(rows, key=lambda r: (-len(r.get("expected_paths") or []),
                                          -int(r.get("priority") or 0)))
    for r in ordered:
        if len(out) >= n:
            break
        if not r.get("expected_paths"):
            continue
        ws = r.get("workspace", "")
        if ws in seen_ws and len(out) < n - 2:
            continue
        seen_ws.add(ws)
        out.append(r)
    return out


def gen_presets():
    cfg = C.build_cfg()
    rows = [r for r in load_cases() if r.get("expected_paths")]
    scored = []
    for r in rows:
        q, ws = r["question"], r.get("workspace", "nlp")
        exp = [str(x) for x in (r.get("expected_paths") or [])]
        res = C.compare(cfg, q, ws, exp)
        res["task_id"] = r["task_id"]
        res["failure_layer"] = r.get("failure_layer", "")
        a = res["baseline"]["hit"] or 0.0
        b = res["current"]["hit"] or 0.0
        score = (abs(b - a) * 3 + len(res["added"]) * 0.3
                 + len(res["removed"]) * 0.2 + len(res["current"]["triggered"]) * 0.1)
        scored.append((score, res))
    scored.sort(key=lambda x: -x[0])
    data = [res for _score, res in scored[:8]]
    for res in data:
        print(f"  {res["task_id"]} "
              f"{res["question"][:26]} | {res["baseline"]["hit"]} -> {res["current"]["hit"]}"
              f" | 触发 {res["current"]["triggered"]}")
    PRESET_FILE.write_text("window.PRESET_DATA = " + json.dumps(
        data, ensure_ascii=False, indent=1) + ";" + chr(10), encoding="utf-8")
    print(f"生成 {len(data)} 组预设 -> {PRESET_FILE}")

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._reply(200, (HERE / "index.html").read_text(encoding="utf-8"),
                        "text/html; charset=utf-8")
        elif path == "/presets.js":
            self._reply(200, PRESET_FILE.read_text(encoding="utf-8"),
                        "application/javascript; charset=utf-8")
        elif path == "/api/health":
            self._reply(200, json.dumps({"ok": True, "lexicon": C.DEFAULT_PATH.exists()}))
        else:
            self._reply(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path.split("?")[0] != "/api/compare":
            self._reply(404, json.dumps({"error": "not found"}))
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("gb18030", errors="replace")
            req = json.loads(text or "{}")
            q = str(req.get("question", "")).strip()
            if not q:
                self._reply(400, json.dumps({"error": "question required"}))
                return
            ws = str(req.get("workspace", "nlp"))
            exp = expected_map(load_cases()).get(norm(q), [])
            res = C.compare(CFG, q, ws, exp)
            self._reply(200, json.dumps(res, ensure_ascii=False))
        except Exception as exc:
            self._reply(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"},
                                        ensure_ascii=False))


def main():
    global CFG
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--gen-presets", action="store_true")
    args = ap.parse_args()
    if args.gen_presets:
        gen_presets()
        return 0
    CFG = C.build_cfg()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"体验页已启动: http://127.0.0.1:{args.port}")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
