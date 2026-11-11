"""内部数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FileRow:
    path: str
    sha256: str
    size: int
    mtime: float
    lang: str = ""


@dataclass
class Chunk:
    path: str
    start_line: int
    end_line: int
    kind: str          # module|class|function|signature|heading|comment|paragraph|key
    symbol: str
    text: str
    tokens: int


@dataclass
class Citation:
    path: str
    start_line: int
    end_line: int
    symbol: str = ""
    kind: str = ""
    score: float = 0.0

    def label(self) -> str:
        loc = f"{self.path}:{self.start_line}-{self.end_line}" if self.end_line > self.start_line else f"{self.path}:{self.start_line}"
        return f"{loc}" + (f" ({self.symbol})" if self.symbol else "")

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "start_line": self.start_line,
                "end_line": self.end_line, "symbol": self.symbol,
                "kind": self.kind, "score": round(self.score, 4)}


@dataclass
class Answer:
    text: str
    workspace: str = ""
    trace_id: str = ""
    citations: List[Citation] = field(default_factory=list)
    memory_hits: List[str] = field(default_factory=list)
    changes: Dict[str, Any] = field(default_factory=dict)
    used_context_tokens: int = 0
    full_corpus_tokens: int = 0
    llm_used: bool = False
    llm_provider: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        saved = max(0, self.full_corpus_tokens - self.used_context_tokens)
        ratio = (saved / self.full_corpus_tokens) if self.full_corpus_tokens else 0.0
        return {
            "text": self.text,
            "workspace": self.workspace,
            "trace_id": self.trace_id,
            "citations": [c.to_dict() for c in self.citations],
            "memory_hits": self.memory_hits,
            "changes": self.changes,
            "usage": {
                "context_tokens": self.used_context_tokens,
                "full_corpus_tokens": self.full_corpus_tokens,
                "saved_tokens": saved,
                "saved_ratio": round(ratio, 4),
                "llm_used": self.llm_used,
                "llm_provider": self.llm_provider,
                "elapsed_ms": self.elapsed_ms,
            },
        }
