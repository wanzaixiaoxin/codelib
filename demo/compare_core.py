"""人工体验页的核心：同一问题，对比「空词表」与「已落地词表」的检索召回。

不调 LLM、不写主库、不碰知识库工程；只读 knowledge.db 副本。
"""
from __future__ import annotations
import asyncio
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import AppConfig, IndexConfig, WorkspaceConfig  # noqa: E402
from app.knowledge.schema import KnowledgeStore  # noqa: E402
from app.knowledge.query import build_context  # noqa: E402
from app.utils import tokenize  # noqa: E402
import app.lexicon as lexmod  # noqa: E402
from app.lexicon import DEFAULT_PATH  # noqa: E402

TMP = Path(tempfile.gettempdir()) / "codelib_demo_kb"
NO_LEXICON = TMP / "no_lexicon.db"
LOCK = threading.Lock()
WS_SPEC = {
    "nlp": ("nlp", "前置NLP", ROOT.parent / "code" / "pri_nlp_worker_svr"),
    "tdx_msg_bus_svr": ("tdx_msg_bus_svr", "", ROOT.parent / "code" / "tdx_msg_bus_svr"),
    "msg_gateway": ("tdx_msg_bus_svr", "", ROOT.parent / "code" / "tdx_msg_bus_svr"),
    "sample": ("sample", "", ROOT / "sample_workspace"),
}


def cpath(c):
    if isinstance(c, dict):
        return str(c.get("path", "") or "")
    return str(getattr(c, "path", "") or "")


def ws_for(workspace):
    v = WS_SPEC.get(workspace, WS_SPEC["nlp"])
    return WorkspaceConfig(id=v[0], name=v[1], path=str(v[2]),
                           index=IndexConfig(auto_reindex_on_query=False))


def build_cfg():
    TMP.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "data" / "knowledge.db", TMP / "knowledge.db")
    base = AppConfig.load(str(ROOT / "config" / "config.yaml"))
    kn = dict(base.knowledge or {})
    kn.pop("db", None)
    kn["rerank"] = {"mode": "none", "top_n": 10}
    return AppConfig(
        data_dir=str(TMP), read_only=True,
        workspaces=[WorkspaceConfig(id=v[0], name=v[1], path=str(v[2]),
                                    index=IndexConfig(auto_reindex_on_query=False))
                    for v in WS_SPEC.values()],
        llm=base.llm, memory=base.memory, knowledge=kn, auth=base.auth,
        default_workspace="nlp")


async def _ask(cfg, kb, ws, question):
    _ctx, cites, _n = await build_context(kb, cfg, ws, question, budget_tokens=4000,
                                          top_k=40, llm=None, deep=False)
    return [cpath(c) for c in (cites or [])]


def sections(kb, key, path, limit=2):
    con = None
    try:
        con = kb._connect()
        return [str(r[0]).lstrip("# ").strip()[:34]
                for r in con.execute(
                    "SELECT section FROM doc_chunk WHERE workspace=? AND path=? AND section!='' LIMIT ?",
                    (key, path, limit)) if r[0]]
    except Exception:
        return []
    finally:
        if con is not None:
            con.close()


def hit(expected, cites):
    if not expected:
        return None
    return round(sum(1 for p in expected
                     if any(str(p) in c or c in str(p) for c in cites)) / len(expected), 3)


def compare(cfg, question, workspace, expected=None):
    """返回双条件召回 + 命中的期望路径 + 被触发的词表条目。"""
    expected = [str(x) for x in (expected or [])]
    ws = ws_for(workspace)
    with LOCK:
        out = {}
        for label, lex in (("baseline", NO_LEXICON), ("current", DEFAULT_PATH)):
            os.environ["CODELIB_LEXICON_DB"] = str(lex)
            lexmod.reset_default()
            store = lexmod.default_store()
            triggered = []
            if label == "current":
                toks = [str(t).lower() for t in tokenize(question)]
                triggered = sorted({k for k in store._map
                                    if any(k == t or (len(k) >= 2 and (k in t or t in k))
                                           for t in toks)})
            kb = KnowledgeStore(cfg.knowledge_db_path)
            cites = asyncio.run(_ask(cfg, kb, ws, question))
            key = "%s:%s" % (ws.id, ws.name) if ws.name and ws.name != ws.id else ws.id
            out[label] = {
                "paths": cites[:12],
                "detail": [{"path": p, "sections": sections(kb, key, p)} for p in cites[:12]],
                "hit": hit(expected, cites),
                "triggered": triggered,
                "lexicon_size": len(store),
            }
        os.environ.pop("CODELIB_LEXICON_DB", None)
        lexmod.reset_default()
    a = set(out["baseline"]["paths"])
    b = set(out["current"]["paths"])
    out["question"] = question
    out["workspace"] = workspace
    out["expected"] = expected
    out["added"] = sorted(b - a)
    out["removed"] = sorted(a - b)
    return out
