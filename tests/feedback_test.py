"""评估闭环测试：评估结论 -> boost 规则 -> 下次检索加权命中。"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.config import AppConfig, IndexConfig, WorkspaceConfig  # noqa: E402
from app.feedback import extract_boost_candidates, apply_report_to_boosts  # noqa: E402
from app.knowledge import KnowledgeStore  # noqa: E402
from app.knowledge.build import build_workspace, ws_kb_id  # noqa: E402
from app.knowledge.query import _apply_boosts  # noqa: E402
from app.store import Store  # noqa: E402


def main() -> int:
    print("== 1. 规则抽取：只收“缺失/应召回”类文件/符号 ==")
    report = {
        "rule": {"checks": [
            {"name": "引用可追溯", "ok": False,
             "detail": "疑似编造/未在检索结果中：['config/kafka_config.go']",
             "hint": "应召回 config/kafka_config.go 中的解析实现"},
        ]},
        "llm": {
            "harness_improvements": [
                "建议补充召回 `ResolveTopic` 的实现",
                "heartbeat.go 与问题无关，可过滤",   # 不含缺失词 -> 不应进入 boost
            ],
            "reasons": [],
        },
        "improvements": [],
    }
    cands = extract_boost_candidates(report)
    print(json.dumps(cands, ensure_ascii=False, indent=2))
    kinds = {(c["kind"], c["pattern"]) for c in cands}
    assert ("path", "config/kafka_config.go") in kinds
    assert ("symbol", "ResolveTopic") in kinds
    assert not any("heartbeat" in p for _, p in kinds), "无关项不应成为 boost"

    print("\n== 2. 写入 boost 并检索加权 ==")
    tmp = Path(tempfile.mkdtemp(prefix="codelib_fb_"))
    lib = tmp / "lib"
    (lib / "config").mkdir(parents=True)
    (lib / "kafka").mkdir(parents=True)
    (lib / "config" / "kafka_config.go").write_text(
        'package config\n\n// ResolveTopic 解析 topic\nfunc ResolveTopic(t string) string { return t }\n',
        encoding="utf-8")
    (lib / "kafka" / "client.go").write_text(
        'package kafka\n\nfunc AsyncSendMessage() { /* send */ }\n', encoding="utf-8")

    ws = WorkspaceConfig(id="lib", name="lib", path=str(lib), index=IndexConfig())
    cfg = AppConfig(data_dir=str(tmp / "data"), workspaces=[ws], knowledge={"enabled": True})
    store = Store(cfg.db_path)
    build_workspace(cfg, ws, full=True)
    kb = KnowledgeStore(cfg.knowledge_db_path)
    key = ws_kb_id(ws.id, ws.name)

    tokens = ["topic", "resolve", "解析", "发", "送"]
    before = kb.search(key, tokens, limit=20)
    print("boost 前 top5:", [(c["path"], round(c["score"], 3)) for c in before[:5]])

    learned = apply_report_to_boosts(kb, key, report, source="test")
    print("写入 boosts:", [(b["kind"], b["pattern"]) for b in learned])
    assert learned

    after = [dict(c) for c in before]
    n = _apply_boosts(kb, key, after)
    print("加权命中条数:", n)
    print("boost 后 top5:", [(c["path"], round(c["score"], 3), c.get("boosted", False)) for c in after[:5]])
    assert n >= 1, "应至少命中一条 boost"
    assert after[0]["path"].endswith("kafka_config.go"), "目标文件应被加权到首位"

    print("\n== 3. boost 列表可查、可删 ==")
    print("boosts:", kb.list_boosts(key))
    assert len(kb.list_boosts(key)) >= 2
    kb.delete_boost(key, "path", "config/kafka_config.go")
    assert not any(b["pattern"] == "config/kafka_config.go" for b in kb.list_boosts(key))

    print("\n评估闭环测试通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
