"""核心知识组织、检索覆盖与断言接地回归测试。"""
from __future__ import annotations

import asyncio
import json
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

from app.bot import BotService
from app.config import AppConfig, WorkspaceConfig
from app.knowledge.build import _rebuild_edges, build_workspace, ws_kb_id
from app.knowledge.query import build_context
from app.knowledge.schema import KnowledgeStore
from app.models import Citation
from app.store import Store


class FakeLLM:
    available = True
    name = "fake"

    def __init__(self):
        self.review_calls = 0
        self.last_raw_finish = "stop"
        self.last_raw_usage = {}
        self.last_error = ""

    async def chat(self, system: str, user: str):
        if "代码检索规划器" in system:
            return json.dumps({"queries": ["Service.run 的调用方", "服务启动配置"]}, ensure_ascii=False)
        if "代码问答质量检查器" in system:
            self.review_calls += 1
            if self.review_calls == 1:
                return json.dumps({
                    "relevant": True, "complete": False,
                    "missing": ["调用方"], "unsupported": [],
                    "claims": [{"claim": "Service.run 会初始化数据库", "status": "supported", "evidence": []}],
                }, ensure_ascii=False)
            return json.dumps({
                "relevant": True, "complete": True, "missing": [], "unsupported": [],
                "claims": [{"claim": "Service.run 返回 ok", "status": "supported",
                            "evidence": ["src/app.py:1-2"]}],
            }, ensure_ascii=False)
        return "Service.run 返回 ok，调用方仍需结合上下文确认。见 src/app.py:1-2。"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="codelib_core_quality_"))
    lib = tmp / "lib"
    lib.mkdir()
    (lib / "long.py").write_text(
        "def process():\n" + "\n".join(f"    value_{i} = compute({i})" for i in range(260))
        + "\n    return value_259\n", encoding="utf-8")
    (lib / "guide.md").write_text(
        "# 启动\n\n" + "\n".join(f"步骤 {i}：检查配置。" for i in range(150)), encoding="utf-8")
    (lib / "alpha.md").write_text("# Alpha 配置\n\nAlpha 使用 alpha_token 完成配置。", encoding="utf-8")
    (lib / "beta.md").write_text("# Beta 启动\n\nBeta 通过 beta_start 完成启动。", encoding="utf-8")

    ws = WorkspaceConfig(id="lib", name="lib", path=str(lib))
    cfg = AppConfig(data_dir=str(tmp / "data"), workspaces=[ws],
                    knowledge={"enabled": True, "rerank": {"mode": "none"},
                               "retrieve": {"graph_hops": 1, "refinement_rounds": 1}})
    result = build_workspace(cfg, ws, full=True)
    assert result["failed"] == 0, result
    kb = KnowledgeStore(cfg.knowledge_db_path)
    key = ws_kb_id(ws.id, ws.name)
    assert kb.get_meta(f"index_format:{key}") == "2"
    assert build_workspace(cfg, ws, full=False)["full"] is False
    conn = kb._connect()
    rows = conn.execute("SELECT kind,lines FROM chunk WHERE workspace=?", (key,)).fetchall()
    assert sum(r["kind"] == "code" for r in rows) >= 3
    assert sum(r["kind"] == "doc" for r in rows) >= 2
    assert sum(r["kind"] == "file" for r in rows) == 4
    fts_count = conn.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0]
    conn.close()
    assert fts_count == len(rows), (fts_count, len(rows))

    context, citations, _ = asyncio.run(build_context(
        kb, cfg, ws, "Alpha 怎么配置，Beta 如何启动？", budget_tokens=1500, top_k=12))
    paths = {c.path for c in citations}
    assert "alpha.md" in paths and "beta.md" in paths, (paths, context[:500])

    graph_key = "graph:graph"
    for sid, name, body in (
        ("sym_a", "Service", "def Service(): pass"),
        ("sym_b", "Runner", "def Runner(): return Service()"),
        ("sym_c", "Similar", "def Similar(): return ServiceRunner()"),
    ):
        kb.upsert_symbol(graph_key, {"id": sid, "path": f"{sid}.py", "qualname": name,
                                     "kind": "function", "body": body})
    assert _rebuild_edges(kb, graph_key) == 1

    fake = FakeLLM()
    bot = BotService(cfg, Store(cfg.db_path), llm=fake)
    planned = asyncio.run(bot._plan_retrieval("分析 Service.run 的启动链路"))
    assert "Service.run 的调用方" in planned and "服务启动配置" in planned

    citation = Citation(path="src/app.py", start_line=1, end_line=2)
    revised, report, _, _ = asyncio.run(bot._review_and_revise(
        ws, "分析 Service.run", "Service.run", "[片段1] src/app.py:1-2\ndef Service(): return 'ok'",
        [citation], "", None, "Service.run 会初始化数据库。", False, 0, 5, 2, False))
    assert report["rounds"] == 2 and report["revised"] is True
    assert report["complete"] is True and report["claims"][0]["status"] == "supported"
    assert "src/app.py:1-2" in revised

    (lib / "beta.md").write_text("# Beta 启动\n\nBeta 通过 beta_start_v2 完成启动。", encoding="utf-8")
    (lib / "alpha.md").unlink()
    sync = bot._ensure_index(ws)
    assert sync and sync.get("knowledge", {}).get("deleted") == 1
    assert "alpha.md" not in kb.file_paths(key)
    assert kb.search(key, ["beta_start_v2"]), "变更后的知识库应立即可检索"
    print("核心知识组织、检索覆盖与断言接地测试通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
