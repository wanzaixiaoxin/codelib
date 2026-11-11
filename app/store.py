"""SQLite 持久化：文件清单、代码块索引、记忆、会话、变更事件。

设计要点（省 token）：
- 只保存从源码抽取出的「签名 / 文档串 / 标题 / 注释」小块，不保存整份文件全文；
- files 表记录 sha256 + mtime，用于增量索引与变更感知；
- 每次索引变更会 bump 一个 generation，供检索层缓存失效。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    workspace_id TEXT NOT NULL,
    path         TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    lang         TEXT DEFAULT '',
    full_tokens  INTEGER DEFAULT 0,
    indexed_at   REAL NOT NULL,
    PRIMARY KEY (workspace_id, path)
);
CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL,
    path         TEXT NOT NULL,
    start_line   INTEGER NOT NULL,
    end_line     INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    symbol       TEXT DEFAULT '',
    text         TEXT NOT NULL,
    tokens       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_ws ON chunks(workspace_id);
CREATE INDEX IF NOT EXISTS idx_chunks_ws_path ON chunks(workspace_id, path);
CREATE TABLE IF NOT EXISTS qa_memory (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL,
    session_id   TEXT DEFAULT '',
    question     TEXT NOT NULL,
    answer       TEXT NOT NULL,
    citations    TEXT DEFAULT '[]',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qa_ws ON qa_memory(workspace_id);
CREATE TABLE IF NOT EXISTS facts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL,
    text         TEXT NOT NULL,
    source       TEXT DEFAULT '',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_ws ON facts(workspace_id);
CREATE TABLE IF NOT EXISTS conversations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    workspace_id TEXT DEFAULT '',
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id);
CREATE TABLE IF NOT EXISTS chat_sessions (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    title        TEXT DEFAULT '',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS change_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL,
    path         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    detail       TEXT DEFAULT '',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_ws ON change_events(workspace_id);
CREATE TABLE IF NOT EXISTS trace (
    trace_id     TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    session_id   TEXT DEFAULT '',
    question     TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trace_ws ON trace(workspace_id);
CREATE INDEX IF NOT EXISTS idx_trace_session ON trace(session_id);
CREATE TABLE IF NOT EXISTS trace_event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id   TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    stage      TEXT NOT NULL,
    data       TEXT NOT NULL,
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trace_event ON trace_event(trace_id, seq);
CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id   TEXT DEFAULT '',
    workspace_id TEXT DEFAULT '',
    verdict    TEXT NOT NULL,
    score      REAL DEFAULT 0,
    report     TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);"""


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # ---------------- trace（全链路留痕）----------------
    def create_trace(self, trace_id: str, workspace_id: str, session_id: str, question: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO trace(trace_id,workspace_id,session_id,question,created_at) "
                "VALUES(?,?,?,?,?)",
                (trace_id, workspace_id, session_id or "", question, time.time()))
            conn.commit()
        finally:
            conn.close()

    def add_event(self, trace_id: str, seq: int, stage: str, data: Any) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO trace_event(trace_id,seq,stage,data,ts) VALUES(?,?,?,?,?)",
                (trace_id, seq, stage, json.dumps(data, ensure_ascii=False, default=str), time.time()))
            conn.commit()
        finally:
            conn.close()

    def list_traces(self, workspace_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            sql = ("SELECT t.trace_id, t.workspace_id, t.session_id, t.question, t.created_at, "
                   "(SELECT COUNT(*) FROM trace_event e WHERE e.trace_id=t.trace_id) AS event_count, "
                   "(SELECT verdict FROM audit a WHERE a.trace_id=t.trace_id ORDER BY a.id DESC LIMIT 1) AS verdict, "
                   "(SELECT score FROM audit a WHERE a.trace_id=t.trace_id ORDER BY a.id DESC LIMIT 1) AS score "
                   "FROM trace t ")
            params: List[Any] = []
            if workspace_id:
                sql += "WHERE t.workspace_id=? "
                params.append(workspace_id)
            sql += "ORDER BY t.created_at DESC LIMIT ?"
            params.append(limit)
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            t = conn.execute("SELECT * FROM trace WHERE trace_id=?", (trace_id,)).fetchone()
            if not t:
                return None
            events = conn.execute(
                "SELECT seq,stage,data,ts FROM trace_event WHERE trace_id=? ORDER BY seq",
                (trace_id,)).fetchall()
            out = dict(t)
            evs = []
            for e in events:
                d = dict(e)
                try:
                    d["data"] = json.loads(d["data"])
                except Exception:
                    pass
                evs.append(d)
            out["events"] = evs
            audits = conn.execute(
                "SELECT id,verdict,score,report,created_at FROM audit WHERE trace_id=? ORDER BY id",
                (trace_id,)).fetchall()
            al = []
            for a in audits:
                d = dict(a)
                try:
                    d["report"] = json.loads(d["report"])
                except Exception:
                    pass
                al.append(d)
            out["audits"] = al
            return out
        finally:
            conn.close()

    def add_audit(self, trace_id: str, workspace_id: str, verdict: str,
                  score: float, report: Any) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO audit(trace_id,workspace_id,verdict,score,report,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (trace_id, workspace_id, verdict, float(score),
                 json.dumps(report, ensure_ascii=False, default=str), time.time()))
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    # ---------------- meta / generation ----------------
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default
        finally:
            conn.close()

    def set_meta(self, key: str, value: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            conn.commit()
        finally:
            conn.close()

    def get_generation(self, workspace_id: str) -> int:
        try:
            return int(self.get_meta(f"gen:{workspace_id}", "0") or 0)
        except ValueError:
            return 0

    def bump_generation(self, workspace_id: str) -> int:
        gen = self.get_generation(workspace_id) + 1
        self.set_meta(f"gen:{workspace_id}", str(gen))
        return gen

    # ---------------- files ----------------
    def upsert_file(self, workspace_id: str, path: str, sha256: str,
                    size: int, mtime: float, lang: str = "",
                    full_tokens: int = 0) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO files(workspace_id,path,sha256,size,mtime,lang,full_tokens,indexed_at) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(workspace_id,path) DO UPDATE SET "
                "sha256=excluded.sha256,size=excluded.size,mtime=excluded.mtime,"
                "lang=excluded.lang,full_tokens=excluded.full_tokens,indexed_at=excluded.indexed_at",
                (workspace_id, path, sha256, size, mtime, lang, full_tokens, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def sum_full_tokens(self, workspace_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(full_tokens),0) AS t FROM files WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
            return int(row["t"])
        finally:
            conn.close()

    def get_file_map(self, workspace_id: str) -> Dict[str, sqlite3.Row]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT path,sha256,size,mtime,lang,full_tokens FROM files WHERE workspace_id=?",
                (workspace_id,),
            ).fetchall()
            return {r["path"]: r for r in rows}
        finally:
            conn.close()

    def delete_file(self, workspace_id: str, path: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM files WHERE workspace_id=? AND path=?", (workspace_id, path))
            conn.execute("DELETE FROM chunks WHERE workspace_id=? AND path=?", (workspace_id, path))
            conn.commit()
        finally:
            conn.close()

    # ---------------- chunks ----------------
    def replace_chunks(self, workspace_id: str, path: str, chunks: Iterable[Dict[str, Any]]) -> int:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM chunks WHERE workspace_id=? AND path=?", (workspace_id, path))
            payload = [
                (workspace_id, path, int(c["start_line"]), int(c["end_line"]),
                 c["kind"], c.get("symbol", ""), c["text"], int(c["tokens"]))
                for c in chunks
            ]
            if payload:
                conn.executemany(
                    "INSERT INTO chunks(workspace_id,path,start_line,end_line,kind,symbol,text,tokens) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    payload,
                )
            conn.commit()
            return len(payload)
        finally:
            conn.close()

    def get_chunks(self, workspace_id: str) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id,path,start_line,end_line,kind,symbol,text,tokens "
                "FROM chunks WHERE workspace_id=? ORDER BY path,start_line",
                (workspace_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def chunk_stats(self, workspace_id: str) -> Dict[str, int]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(tokens),0) AS toks, "
                "COUNT(DISTINCT path) AS files FROM chunks WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
            return {"chunks": int(row["n"]), "chunk_tokens": int(row["toks"]),
                    "indexed_files_with_chunks": int(row["files"])}
        finally:
            conn.close()

    # ---------------- change events ----------------
    def add_change(self, workspace_id: str, path: str, kind: str, detail: str = "") -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO change_events(workspace_id,path,kind,detail,created_at) VALUES(?,?,?,?,?)",
                (workspace_id, path, kind, detail, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def recent_changes(self, workspace_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT path,kind,detail,created_at FROM change_events "
                "WHERE workspace_id=? ORDER BY id DESC LIMIT ?",
                (workspace_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---------------- QA memory ----------------
    def add_qa(self, workspace_id: str, session_id: str, question: str,
               answer: str, citations: Optional[List[Dict[str, Any]]] = None) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO qa_memory(workspace_id,session_id,question,answer,citations,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (workspace_id, session_id or "", question, answer,
                 json.dumps(citations or [], ensure_ascii=False), time.time()),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def all_qa(self, workspace_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id,question,answer,citations,created_at FROM qa_memory "
                "WHERE workspace_id=? ORDER BY id DESC LIMIT ?",
                (workspace_id, limit),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["citations"] = json.loads(d.get("citations") or "[]")
                except json.JSONDecodeError:
                    d["citations"] = []
                out.append(d)
            return out
        finally:
            conn.close()

    # ---------------- facts ----------------
    def add_fact(self, workspace_id: str, text: str, source: str = "") -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO facts(workspace_id,text,source,created_at) VALUES(?,?,?,?)",
                (workspace_id, text, source, time.time()),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def all_facts(self, workspace_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id,text,source,created_at FROM facts WHERE workspace_id=? "
                "ORDER BY id DESC LIMIT ?",
                (workspace_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---------------- conversations ----------------
    def append_message(self, session_id: str, workspace_id: str, role: str, content: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO conversations(session_id,workspace_id,role,content,created_at) "
                "VALUES(?,?,?,?,?)",
                (session_id, workspace_id, role, content, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def get_messages(self, session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT role,content,created_at FROM conversations "
                "WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]
        finally:
            conn.close()

    def get_all_messages(self, session_id: str) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT role,content,created_at FROM conversations "
                "WHERE session_id=? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---------------- chat sessions（网页对话）----------------
    def create_session(self, session_id: str, workspace_id: str, title: str = "新对话") -> Dict[str, Any]:
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO chat_sessions(id,workspace_id,title,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                (session_id, workspace_id, title or "新对话", now, now),
            )
            conn.commit()
        finally:
            conn.close()
        return {"id": session_id, "workspace_id": workspace_id,
                "title": title or "新对话", "created_at": now, "updated_at": now}

    def list_sessions(self, workspace_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            sql = ("SELECT s.id, s.workspace_id, s.title, s.created_at, s.updated_at, "
                   "(SELECT content FROM conversations c WHERE c.session_id=s.id "
                   " ORDER BY c.id DESC LIMIT 1) AS last_message "
                   "FROM chat_sessions s ")
            params: List[Any] = []
            if workspace_id:
                sql += "WHERE s.workspace_id=? "
                params.append(workspace_id)
            sql += "ORDER BY s.updated_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id,workspace_id,title,created_at,updated_at FROM chat_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def find_empty_session(self, workspace_id: str) -> Optional[Dict[str, Any]]:
        """查找该库里「还没发过任何消息」的空对话（用于复用，避免无限新建）。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT s.id, s.workspace_id, s.title, s.created_at, s.updated_at FROM chat_sessions s "
                "WHERE s.workspace_id=? AND NOT EXISTS "
                "(SELECT 1 FROM conversations c WHERE c.session_id = s.id) "
                "ORDER BY s.updated_at DESC LIMIT 1",
                (workspace_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def is_empty_session(self, session_id: str) -> bool:
        conn = self._connect()
        try:
            n = conn.execute("SELECT COUNT(*) FROM conversations WHERE session_id=?",
                             (session_id,)).fetchone()[0]
            return int(n) == 0
        finally:
            conn.close()

    def cleanup_empty_sessions(self, workspace_id: Optional[str] = None) -> int:
        """删除多余的空对话，保证每个库最多保留一个空对话。"""
        conn = self._connect()
        try:
            if workspace_id:
                rows = conn.execute(
                    "SELECT s.id FROM chat_sessions s WHERE s.workspace_id=? AND NOT EXISTS "
                    "(SELECT 1 FROM conversations c WHERE c.session_id=s.id) "
                    "ORDER BY s.updated_at DESC", (workspace_id,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT s.id FROM chat_sessions s WHERE NOT EXISTS "
                    "(SELECT 1 FROM conversations c WHERE c.session_id=s.id) "
                    "ORDER BY s.updated_at DESC").fetchall()
            keep = rows[0]["id"] if rows else None
            removed = 0
            for r in rows[1:]:
                conn.execute("DELETE FROM chat_sessions WHERE id=?", (r["id"],))
                removed += 1
            conn.commit()
            return removed
        finally:
            conn.close()

    def touch_session(self, session_id: str, title: Optional[str] = None) -> None:
        conn = self._connect()
        try:
            if title:
                conn.execute("UPDATE chat_sessions SET updated_at=?, title=? WHERE id=?",
                             (time.time(), title, session_id))
            else:
                conn.execute("UPDATE chat_sessions SET updated_at=? WHERE id=?",
                             (time.time(), session_id))
            conn.commit()
        finally:
            conn.close()

    def delete_session(self, session_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM conversations WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))
            conn.commit()
        finally:
            conn.close()

    def stats(self, workspace_id: str) -> Dict[str, Any]:
        cs = self.chunk_stats(workspace_id)
        conn = self._connect()
        try:
            n_files = conn.execute(
                "SELECT COUNT(*) FROM files WHERE workspace_id=?", (workspace_id,)
            ).fetchone()[0]
            n_qa = conn.execute(
                "SELECT COUNT(*) FROM qa_memory WHERE workspace_id=?", (workspace_id,)
            ).fetchone()[0]
            n_facts = conn.execute(
                "SELECT COUNT(*) FROM facts WHERE workspace_id=?", (workspace_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        return {
            "indexed_files": int(n_files),
            "chunks": cs["chunks"],
            "chunk_tokens": cs["chunk_tokens"],
            "qa_memory": int(n_qa),
            "facts": int(n_facts),
            "generation": self.get_generation(workspace_id),
        }
