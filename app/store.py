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
    owner_user_id INTEGER DEFAULT 0,
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
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','user')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry ON auth_sessions(expires_at);
CREATE TABLE IF NOT EXISTS user_workspace_access (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL,
    granted_by INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    PRIMARY KEY(user_id, workspace_id)
);
CREATE TABLE IF NOT EXISTS managed_workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at REAL NOT NULL
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
            cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_sessions)")}
            if "owner_user_id" not in cols:
                conn.execute("ALTER TABLE chat_sessions ADD COLUMN owner_user_id INTEGER DEFAULT 0")
            conn.commit()
        finally:
            conn.close()

    # ---------------- users / authentication ----------------
    @staticmethod
    def _password_digest(password: str, salt: bytes) -> str:
        import hashlib
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000).hex()

    def create_user(self, username: str, password: str, role: str = "user") -> Dict[str, Any]:
        import secrets
        username = (username or "").strip()
        if not 2 <= len(username) <= 64:
            raise ValueError("用户名长度必须为 2 到 64 个字符")
        if len(password or "") < 8:
            raise ValueError("密码至少需要 8 个字符")
        if role not in ("admin", "user"):
            raise ValueError("角色只能是 admin 或 user")
        salt = secrets.token_bytes(16)
        now = time.time()
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO users(username,password_hash,password_salt,role,created_at) VALUES(?,?,?,?,?)",
                (username, self._password_digest(password, salt), salt.hex(), role, now))
            conn.commit()
            return {"id": int(cur.lastrowid), "username": username, "role": role,
                    "active": True, "created_at": now, "workspace_ids": []}
        except sqlite3.IntegrityError as exc:
            raise ValueError("用户名已存在") from exc
        finally:
            conn.close()

    def bootstrap_admin(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        if not username or not password:
            return None
        conn = self._connect()
        try:
            count = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        finally:
            conn.close()
        if count:
            return None
        return self.create_user(username, password, "admin")

    def authenticate_user(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id,username,password_hash,password_salt,role,active,created_at "
                "FROM users WHERE username=? COLLATE NOCASE", ((username or "").strip(),)
            ).fetchone()
            if not row or not row["active"]:
                return None
            try:
                salt = bytes.fromhex(row["password_salt"])
            except ValueError:
                return None
            import hmac
            digest = self._password_digest(password or "", salt)
            if not hmac.compare_digest(digest, row["password_hash"]):
                return None
            return {"id": int(row["id"]), "username": row["username"],
                    "role": row["role"], "active": bool(row["active"]),
                    "created_at": row["created_at"]}
        finally:
            conn.close()

    def create_auth_session(self, user_id: int, token_hash: str, expires_at: float) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (time.time(),))
            conn.execute("INSERT INTO auth_sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                         (token_hash, int(user_id), float(expires_at), time.time()))
            conn.commit()
        finally:
            conn.close()

    def user_for_session(self, token_hash: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT u.id,u.username,u.role,u.active,u.created_at FROM auth_sessions s "
                "JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",
                (token_hash, time.time())).fetchone()
            if not row or not row["active"]:
                return None
            return {"id": int(row["id"]), "username": row["username"],
                    "role": row["role"], "active": bool(row["active"]),
                    "created_at": row["created_at"]}
        finally:
            conn.close()

    def revoke_auth_session(self, token_hash: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash,))
            conn.commit()
        finally:
            conn.close()

    def list_users(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            users = [dict(r) for r in conn.execute(
                "SELECT id,username,role,active,created_at FROM users ORDER BY username COLLATE NOCASE").fetchall()]
            for user in users:
                user["active"] = bool(user["active"])
                user["workspace_ids"] = [r[0] for r in conn.execute(
                    "SELECT workspace_id FROM user_workspace_access WHERE user_id=? ORDER BY workspace_id",
                    (user["id"],)).fetchall()]
            return users
        finally:
            conn.close()

    def user_workspace_ids(self, user_id: int) -> List[str]:
        conn = self._connect()
        try:
            return [r[0] for r in conn.execute(
                "SELECT workspace_id FROM user_workspace_access WHERE user_id=? ORDER BY workspace_id",
                (int(user_id),)).fetchall()]
        finally:
            conn.close()

    def has_workspace_access(self, user_id: int, workspace_id: str) -> bool:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT 1 FROM user_workspace_access WHERE user_id=? AND workspace_id=?",
                (int(user_id), workspace_id)).fetchone() is not None
        finally:
            conn.close()

    def set_user_workspaces(self, user_id: int, workspace_ids: List[str], granted_by: int) -> None:
        conn = self._connect()
        try:
            if not conn.execute("SELECT 1 FROM users WHERE id=?", (int(user_id),)).fetchone():
                raise KeyError("用户不存在")
            conn.execute("DELETE FROM user_workspace_access WHERE user_id=?", (int(user_id),))
            conn.executemany(
                "INSERT INTO user_workspace_access(user_id,workspace_id,granted_by,created_at) VALUES(?,?,?,?)",
                [(int(user_id), wid, int(granted_by), time.time()) for wid in dict.fromkeys(workspace_ids)])
            conn.commit()
        finally:
            conn.close()

    def set_user_active(self, user_id: int, active: bool) -> None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT role,active FROM users WHERE id=?", (int(user_id),)).fetchone()
            if not row:
                raise KeyError("用户不存在")
            if not active and row["role"] == "admin" and row["active"]:
                n_admins = int(conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0])
                if n_admins <= 1:
                    raise ValueError("不能停用最后一个管理员账号")
            conn.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, int(user_id)))
            if not active:
                conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (int(user_id),))
            conn.commit()
        finally:
            conn.close()

    def reset_user_password(self, user_id: int, password: str) -> None:
        if len(password or "") < 8:
            raise ValueError("密码至少需要 8 个字符")
        import secrets
        salt = secrets.token_bytes(16)
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE users SET password_hash=?,password_salt=? WHERE id=?",
                (self._password_digest(password, salt), salt.hex(), int(user_id)))
            if cur.rowcount == 0:
                raise KeyError("用户不存在")
            conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (int(user_id),))
            conn.commit()
        finally:
            conn.close()

    def add_managed_workspace(self, workspace_id: str, name: str, path: str,
                              description: str = "") -> Dict[str, Any]:
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO managed_workspaces(id,name,path,description,created_at) VALUES(?,?,?,?,?)",
                (workspace_id, name, path, description, now))
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("知识库 ID 已存在") from exc
        finally:
            conn.close()
        return {"id": workspace_id, "name": name, "path": path,
                "description": description, "created_at": now}

    def list_managed_workspaces(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT id,name,path,description,created_at FROM managed_workspaces ORDER BY created_at").fetchall()]
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
    def create_session(self, session_id: str, workspace_id: str, title: str = "新对话",
                       owner_user_id: int = 0) -> Dict[str, Any]:
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO chat_sessions(id,workspace_id,title,owner_user_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (session_id, workspace_id, title or "新对话", int(owner_user_id), now, now),
            )
            conn.commit()
        finally:
            conn.close()
        return {"id": session_id, "workspace_id": workspace_id, "owner_user_id": int(owner_user_id),
                "title": title or "新对话", "created_at": now, "updated_at": now}

    def list_sessions(self, workspace_id: Optional[str] = None, limit: int = 200,
                      owner_user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            sql = ("SELECT s.id, s.workspace_id, s.title, s.created_at, s.updated_at, "
                   "(SELECT content FROM conversations c WHERE c.session_id=s.id "
                   " ORDER BY c.id DESC LIMIT 1) AS last_message "
                   "FROM chat_sessions s ")
            params: List[Any] = []
            conditions = []
            if workspace_id:
                conditions.append("s.workspace_id=?")
                params.append(workspace_id)
            if owner_user_id is not None:
                conditions.append("s.owner_user_id=?")
                params.append(int(owner_user_id))
            if conditions:
                sql += "WHERE " + " AND ".join(conditions) + " "
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
                "SELECT id,workspace_id,title,owner_user_id,created_at,updated_at FROM chat_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def find_empty_session(self, workspace_id: str,
                           owner_user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """查找该库里「还没发过任何消息」的空对话（用于复用，避免无限新建）。"""
        conn = self._connect()
        try:
            sql = ("SELECT s.id, s.workspace_id, s.title, s.owner_user_id,s.created_at,s.updated_at "
                   "FROM chat_sessions s WHERE s.workspace_id=? ")
            params: List[Any] = [workspace_id]
            if owner_user_id is not None:
                sql += "AND s.owner_user_id=? "
                params.append(int(owner_user_id))
            sql += ("AND NOT EXISTS (SELECT 1 FROM conversations c WHERE c.session_id=s.id) "
                    "ORDER BY s.updated_at DESC LIMIT 1")
            row = conn.execute(sql, params).fetchone()
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

    def cleanup_empty_sessions(self, workspace_id: Optional[str] = None,
                               owner_user_id: Optional[int] = None) -> int:
        """删除多余空对话，每个用户在每个知识库保留一个。"""
        conn = self._connect()
        try:
            sql = "SELECT s.id,s.workspace_id,s.owner_user_id FROM chat_sessions s WHERE 1=1 "
            params: List[Any] = []
            if workspace_id:
                sql += "AND s.workspace_id=? "
                params.append(workspace_id)
            if owner_user_id is not None:
                sql += "AND s.owner_user_id=? "
                params.append(int(owner_user_id))
            sql += ("AND NOT EXISTS (SELECT 1 FROM conversations c WHERE c.session_id=s.id) "
                    "ORDER BY s.workspace_id, s.owner_user_id, s.updated_at DESC")
            rows = conn.execute(sql, params).fetchall()
            seen = set()
            removed = 0
            for r in rows:
                key = (r["workspace_id"], r["owner_user_id"])
                if key in seen:
                    conn.execute("DELETE FROM chat_sessions WHERE id=?", (r["id"],))
                    removed += 1
                else:
                    seen.add(key)
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
