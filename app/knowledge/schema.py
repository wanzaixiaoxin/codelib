"""知识层存储：单 SQLite 文件（FTS5 + 邻接表 + 元数据）。"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..utils import tokenize

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_file(
    workspace TEXT NOT NULL, path TEXT NOT NULL, hash TEXT NOT NULL,
    lang TEXT DEFAULT '', mtime REAL DEFAULT 0,
    PRIMARY KEY(workspace, path)
);
CREATE TABLE IF NOT EXISTS symbol(
    id TEXT PRIMARY KEY, workspace TEXT NOT NULL, path TEXT NOT NULL, lang TEXT DEFAULT '',
    kind TEXT DEFAULT 'function', qualname TEXT DEFAULT '', start_line INTEGER DEFAULT 0,
    end_line INTEGER DEFAULT 0, signature TEXT DEFAULT '', doc TEXT DEFAULT '',
    body TEXT DEFAULT '', hash TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_symbol_ws ON symbol(workspace);
CREATE TABLE IF NOT EXISTS edge(
    workspace TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL, type TEXT NOT NULL,
    weight REAL DEFAULT 1, PRIMARY KEY(workspace, src, dst, type)
);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edge(workspace, dst);
CREATE TABLE IF NOT EXISTS doc_chunk(
    id TEXT PRIMARY KEY, workspace TEXT NOT NULL, path TEXT NOT NULL,
    section TEXT DEFAULT '', page INTEGER DEFAULT 0, text TEXT DEFAULT '', hash TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_doc_ws ON doc_chunk(workspace);
CREATE TABLE IF NOT EXISTS card(
    id TEXT PRIMARY KEY, workspace TEXT NOT NULL, type TEXT DEFAULT '', title TEXT DEFAULT '',
    body TEXT DEFAULT '', links TEXT DEFAULT '', source_refs TEXT DEFAULT '', hash TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS chunk(
    rid INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL, kind TEXT NOT NULL,
    ref_id TEXT NOT NULL, path TEXT NOT NULL, lines TEXT DEFAULT '', symbol TEXT DEFAULT '',
    text TEXT NOT NULL, tokens TEXT DEFAULT '', hash TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_chunk_ws ON chunk(workspace);
CREATE INDEX IF NOT EXISTS idx_chunk_ref ON chunk(workspace, ref_id);
CREATE TABLE IF NOT EXISTS boost(
    workspace TEXT NOT NULL, kind TEXT NOT NULL, pattern TEXT NOT NULL,
    weight REAL DEFAULT 1.0, hits INTEGER DEFAULT 0, source TEXT DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY(workspace, kind, pattern)
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(text, path, symbol, tokens);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


class KnowledgeStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # ---------- meta / file hash ----------
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        conn = self._connect()
        try:
            r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return r["value"] if r else default
        finally:
            conn.close()

    def set_meta(self, key: str, value: str) -> None:
        conn = self._connect()
        try:
            conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, str(value)))
            conn.commit()
        finally:
            conn.close()

    def file_hash(self, workspace: str, path: str) -> Optional[str]:
        conn = self._connect()
        try:
            r = conn.execute("SELECT hash FROM kb_file WHERE workspace=? AND path=?",
                             (workspace, path)).fetchone()
            return r["hash"] if r else None
        finally:
            conn.close()

    def set_file(self, workspace: str, path: str, fhash: str,
                 lang: str = "", mtime: float = 0) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO kb_file(workspace,path,hash,lang,mtime) VALUES(?,?,?,?,?) "
                "ON CONFLICT(workspace,path) DO UPDATE SET hash=excluded.hash,"
                "lang=excluded.lang,mtime=excluded.mtime",
                (workspace, path, fhash, lang, mtime))
            conn.commit()
        finally:
            conn.close()

    # ---------- chunks ----------
    def _delete_ref_chunks(self, conn: sqlite3.Connection, workspace: str, ref_id: str) -> None:
        rids = [r[0] for r in conn.execute(
            "SELECT rid FROM chunk WHERE workspace=? AND ref_id=?", (workspace, ref_id))]
        if rids:
            conn.executemany("DELETE FROM chunk_fts WHERE rowid=?", [(r,) for r in rids])
        conn.execute("DELETE FROM chunk WHERE workspace=? AND ref_id=?", (workspace, ref_id))

    def insert_chunk(self, workspace: str, kind: str, ref_id: str, path: str,
                     lines: str, symbol: str, text: str, chash: str) -> int:
        toks = " ".join(tokenize(f"{symbol} {path} {text}"))
        conn = self._connect()
        try:
            self._delete_ref_chunks(conn, workspace, ref_id)
            cur = conn.execute(
                "INSERT INTO chunk(workspace,kind,ref_id,path,lines,symbol,text,tokens,hash) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (workspace, kind, ref_id, path, lines, symbol, text, toks, chash))
            rid = int(cur.lastrowid)
            conn.execute("INSERT INTO chunk_fts(rowid,text,path,symbol,tokens) VALUES(?,?,?,?,?)",
                         (rid, text, path, symbol, toks))
            conn.commit()
            return rid
        finally:
            conn.close()

    def delete_file(self, workspace: str, path: str) -> None:
        conn = self._connect()
        try:
            sym_ids = [r[0] for r in conn.execute(
                "SELECT id FROM symbol WHERE workspace=? AND path=?", (workspace, path))]
            if sym_ids:
                q = ",".join("?" * len(sym_ids))
                conn.execute(f"DELETE FROM edge WHERE workspace=? AND (src IN ({q}) OR dst IN ({q}))",
                             [workspace, *sym_ids, *sym_ids])
            for r in conn.execute("SELECT rid FROM chunk WHERE workspace=? AND path=?",
                                  (workspace, path)).fetchall():
                conn.execute("DELETE FROM chunk_fts WHERE rowid=?", (r[0],))
            conn.execute("DELETE FROM chunk WHERE workspace=? AND path=?", (workspace, path))
            conn.execute("DELETE FROM symbol WHERE workspace=? AND path=?", (workspace, path))
            conn.execute("DELETE FROM doc_chunk WHERE workspace=? AND path=?", (workspace, path))
            conn.execute("DELETE FROM kb_file WHERE workspace=? AND path=?", (workspace, path))
            conn.commit()
        finally:
            conn.close()

    # ---------- boost（评估闭环沉淀的召回加权）----------
    def add_boost(self, workspace: str, kind: str, pattern: str,
                  weight: float = 1.0, source: str = "") -> None:
        """新增/加强一条召回提示。kind: path|symbol|dir|token。"""
        pattern = (pattern or "").strip()
        if not pattern:
            return
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO boost(workspace,kind,pattern,weight,hits,source,created_at,updated_at) "
                "VALUES(?,?,?,?,0,?,?,?) ON CONFLICT(workspace,kind,pattern) DO UPDATE SET "
                "weight=min(boost.weight+0.5, 5.0), updated_at=excluded.updated_at, "
                "source=CASE WHEN boost.source='' THEN excluded.source ELSE boost.source END",
                (workspace, kind, pattern, float(weight), source or "", now, now))
            conn.commit()
        finally:
            conn.close()

    def list_boosts(self, workspace: str) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT kind,pattern,weight,hits,source,updated_at FROM boost "
                "WHERE workspace=? ORDER BY weight DESC, updated_at DESC", (workspace,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_boost_rules(self, workspace: str) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT kind,pattern,weight FROM boost WHERE workspace=?", (workspace,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def delete_boost(self, workspace: str, kind: str, pattern: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM boost WHERE workspace=? AND kind=? AND pattern=?",
                         (workspace, kind, pattern))
            conn.commit()
        finally:
            conn.close()

    def search(self, workspace: str, tokens: List[str], limit: int = 40) -> List[Dict[str, Any]]:
        toks = [t for t in tokens if t and len(t) <= 40][:40]
        if not toks:
            return []
        match = " OR ".join('"' + t.replace('"', "") + '"' for t in toks)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT c.rid, c.kind, c.ref_id, c.path, c.lines, c.symbol, c.text, "
                "bm25(chunk_fts, 2.0, 4.0, 5.0, 1.0) AS rank "
                "FROM chunk_fts JOIN chunk c ON c.rid = chunk_fts.rowid "
                "WHERE chunk_fts MATCH ? AND c.workspace=? ORDER BY rank LIMIT ?",
                (match, workspace, limit)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["score"] = -float(d.pop("rank", 0.0))
                out.append(d)
            return out
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    # ---------- symbols / edges ----------
    def upsert_symbol(self, workspace: str, sym: Dict[str, Any]) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO symbol(id,workspace,path,lang,kind,qualname,"
                "start_line,end_line,signature,doc,body,hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (sym["id"], workspace, sym["path"], sym.get("lang", ""), sym.get("kind", ""),
                 sym.get("qualname", ""), sym.get("start_line", 0), sym.get("end_line", 0),
                 sym.get("signature", ""), sym.get("doc", ""), sym.get("body", ""),
                 sym.get("hash", "")))
            conn.commit()
        finally:
            conn.close()

    def all_symbols(self, workspace: str) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id,path,lang,kind,qualname,body FROM symbol WHERE workspace=?",
                (workspace,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def replace_edges(self, workspace: str, edges: List[Tuple[str, str, str, float]]) -> int:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM edge WHERE workspace=?", (workspace,))
            conn.executemany(
                "INSERT OR IGNORE INTO edge(workspace,src,dst,type,weight) VALUES(?,?,?,?,?)",
                [(workspace, s, d, t, w) for (s, d, t, w) in edges])
            conn.commit()
            return len(edges)
        finally:
            conn.close()

    def neighbors(self, workspace: str, node_ids: List[str], limit: int = 30) -> List[str]:
        if not node_ids:
            return []
        q = ",".join("?" * len(node_ids))
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT dst AS id FROM edge WHERE workspace=? AND src IN ({q}) "
                f"UNION SELECT src AS id FROM edge WHERE workspace=? AND dst IN ({q}) LIMIT ?",
                [workspace, *node_ids, workspace, *node_ids, limit]).fetchall()
            return [r["id"] for r in rows if r["id"] not in node_ids]
        finally:
            conn.close()

    def chunks_by_refs(self, workspace: str, ref_ids: List[str], limit: int = 10) -> List[Dict[str, Any]]:
        if not ref_ids:
            return []
        q = ",".join("?" * len(ref_ids))
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT rid,kind,ref_id,path,lines,symbol,text FROM chunk "
                f"WHERE workspace=? AND ref_id IN ({q}) LIMIT ?",
                [workspace, *ref_ids, limit]).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def counts(self, workspace: str) -> Dict[str, int]:
        conn = self._connect()
        try:
            def c(sql: str) -> int:
                return int(conn.execute(sql, (workspace,)).fetchone()[0])
            return {
                "files": c("SELECT COUNT(*) FROM kb_file WHERE workspace=?"),
                "symbols": c("SELECT COUNT(*) FROM symbol WHERE workspace=?"),
                "doc_chunks": c("SELECT COUNT(*) FROM doc_chunk WHERE workspace=?"),
                "chunks": c("SELECT COUNT(*) FROM chunk WHERE workspace=?"),
                "edges": c("SELECT COUNT(*) FROM edge WHERE workspace=?"),
            }
        finally:
            conn.close()

    def reset(self, workspace: str) -> None:
        conn = self._connect()
        try:
            for tbl in ("edge", "symbol", "doc_chunk", "card", "chunk", "kb_file"):
                conn.execute(f"DELETE FROM {tbl} WHERE workspace=?", (workspace,))
            # fts rows for this workspace
            rids = [r[0] for r in conn.execute(
                "SELECT rid FROM chunk WHERE workspace=?", (workspace,)).fetchall()]
            if rids:
                conn.executemany("DELETE FROM chunk_fts WHERE rowid=?", [(r,) for r in rids])
            conn.commit()
        finally:
            conn.close()
