"""运行时可读的「中文业务术语 -> 检索词」映射，供自循环自动学习。

设计要点：
  - 独立于 app/retrieval.py 的静态 _SYNONYMS 常量，改动无需改代码、无需重启即可热读。
  - 存独立的 data/lexicon.db，不动 codelib.db / knowledge.db，也不碰知识库工程。
  - 表为空或文件不存在时 expand() 返回 []，线上行为与改造前完全一致。
  - 可通过环境变量 CODELIB_LEXICON_DB 指向别处（A/B 验证用）。
"""
from __future__ import annotations
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "lexicon.db"

SCHEMA = """CREATE TABLE IF NOT EXISTS lexicon(
  term TEXT PRIMARY KEY,
  expansions TEXT NOT NULL,
  source TEXT DEFAULT '',
  updated_at REAL
)"""


def resolve_path(path: Optional[Path] = None) -> Path:
    raw = os.environ.get("CODELIB_LEXICON_DB", "")
    return Path(path) if path else (Path(raw) if raw else DEFAULT_PATH)


class LexiconStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = resolve_path(path)
        self._mtime = 0.0
        self._map: Dict[str, List[str]] = {}
        self._lock = threading.Lock()
        self.reload(force=True)

    def reload(self, force: bool = False) -> None:
        """按 mtime 热重载；文件消失则清空（自然回滚）。"""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            with self._lock:
                self._map, self._mtime = {}, 0.0
            return
        if not force and mtime == self._mtime:
            return
        mapping: Dict[str, List[str]] = {}
        try:
            con = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
            for term, expansions in con.execute("SELECT term,expansions FROM lexicon"):
                try:
                    vals = json.loads(expansions or "[]")
                except Exception:
                    vals = []
                if isinstance(vals, list) and vals:
                    mapping[str(term).lower()] = [str(v) for v in vals if str(v).strip()]
            con.close()
        except Exception:
            mapping = {}
        with self._lock:
            self._map, self._mtime = mapping, mtime

    def expand(self, tokens: List[str]) -> List[str]:
        self.reload()
        with self._lock:
            mp = dict(self._map)
        if not mp:
            return []
        out: List[str] = []
        for t in tokens:
            tl = str(t).lower()
            for key, vals in mp.items():
                if key == tl or (len(key) >= 2 and (key in tl or tl in key)):
                    out.extend(vals)
        return list(dict.fromkeys(out))

    def __len__(self) -> int:
        with self._lock:
            return len(self._map)


_default: Optional[LexiconStore] = None


def default_store() -> LexiconStore:
    global _default
    if _default is None:
        _default = LexiconStore()
    return _default


def reset_default() -> None:
    """切换 CODELIB_LEXICON_DB 后调用，强制重建单例。"""
    global _default
    _default = None
