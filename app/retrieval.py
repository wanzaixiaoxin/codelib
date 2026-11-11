"""检索层：BM25 + 预算打包 + 按需深读。

缓存按 workspace 的 generation 失效，索引一变自动重建，避免重复计算。
"""
from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import Citation
from .store import Store
from .utils import estimate_tokens, read_text_safe, tokenize

_K1 = 1.5
_B = 0.75
_CACHE: Dict[str, "_WSIndex"] = {}

# 轻量「中文概念 -> 英文标识符」同义词，缓解中文提问命中英文代码的问题（零额外 token）
_SYNONYMS: Dict[str, List[str]] = {
    "入口": ["main", "entry", "cmd"], "启动": ["main", "start", "run", "init", "serve", "boot", "listen"],
    "配置": ["config", "conf", "setting", "option"], "路由": ["route", "router", "dispatch", "mux"],
    "消息": ["message", "msg", "event"], "存储": ["store", "storage", "dao", "repository", "repo", "db"],
    "数据库": ["db", "database", "sql", "mysql", "gorm"], "缓存": ["cache", "redis"],
    "接口": ["api", "interface", "handler", "controller", "endpoint"], "日志": ["log", "logger", "logging"],
    "测试": ["test", "spec", "_test"], "服务": ["service", "server"], "客户端": ["client"],
    "网关": ["gateway", "proxy"], "消费者": ["consumer", "subscriber", "consume"],
    "生产者": ["producer", "publisher", "publish"], "队列": ["queue", "kafka", "mq", "topic"],
    "定时": ["cron", "schedule", "timer", "ticker"], "鉴权": ["auth", "token", "login", "jwt"],
    "部署": ["deploy", "startup", "docker"], "文档": ["doc", "readme"], "错误": ["error", "err"],
    "异常": ["exception", "panic", "fault"], "连接": ["connect", "dial", "conn"], "返回": ["return", "resp"],
    "请求": ["request", "req"], "响应": ["response", "resp"], "初始化": ["init", "setup", "new"],
    "处理": ["handle", "process", "deal"], "发送": ["send", "push", "publish"], "接收": ["receive", "recv", "pull"],
}


def _expand_query(q_tokens: List[str]) -> List[str]:
    extra: List[str] = []
    for t in q_tokens:
        for key, vals in _SYNONYMS.items():
            if t == key or (len(t) >= 1 and (t in key or key in t)):
                extra.extend(vals)
    return extra


class _WSIndex:
    __slots__ = ("gen", "rows", "docs", "idf", "avgdl")

    def __init__(self, gen: int, rows: List[Dict[str, Any]], docs: List[List[str]],
                 idf: Dict[str, float], avgdl: float):
        self.gen = gen
        self.rows = rows
        self.docs = docs
        self.idf = idf
        self.avgdl = avgdl


def _build(store: Store, workspace_id: str) -> _WSIndex:
    gen = store.get_generation(workspace_id)
    cached = _CACHE.get(workspace_id)
    if cached is not None and cached.gen == gen:
        return cached
    rows = store.get_chunks(workspace_id)
    docs: List[List[str]] = []
    df: Counter = Counter()
    total_len = 0
    for r in rows:
        text = f"{r.get('symbol', '')} {r.get('path', '')} {r.get('text', '')}"
        toks = tokenize(text)
        docs.append(toks)
        total_len += len(toks)
        for t in set(toks):
            df[t] += 1
    n = len(rows)
    avgdl = (total_len / n) if n else 1.0
    idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
    node = _WSIndex(gen, rows, docs, idf, avgdl or 1.0)
    _CACHE[workspace_id] = node
    return node


def invalidate(workspace_id: Optional[str] = None) -> None:
    if workspace_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(workspace_id, None)


def search(store: Store, workspace_id: str, query: str, top_k: int = 8) -> List[Tuple[Dict[str, Any], float]]:
    idx = _build(store, workspace_id)
    q_tokens = tokenize(query)
    if not q_tokens or not idx.rows:
        return []
    qtf = Counter(q_tokens)
    for e in _expand_query(q_tokens):
        if e not in qtf:
            qtf[e] = 1  # 同义词按较低权重(1)参与
    qlow = query.strip().lower()
    scored: List[Tuple[Dict[str, Any], float]] = []
    for row, toks in zip(idx.rows, idx.docs):
        if not toks:
            continue
        tf = Counter(toks)
        dl = len(toks)
        score = 0.0
        for term, qn in qtf.items():
            c = tf.get(term)
            if not c:
                continue
            idf = idx.idf.get(term, 0.0)
            denom = c + _K1 * (1 - _B + _B * dl / idx.avgdl)
            score += idf * (c * (_K1 + 1)) / denom * min(qn, 3)
        if score <= 0:
            continue
        # 命中路径/符号/原文短语加成
        hay = f"{row.get('path','')} {row.get('symbol','')} {row.get('text','')}".lower()
        if qlow and qlow in hay:
            score *= 1.6
        if row.get("symbol") and row["symbol"].lower() in qlow:
            score *= 1.2
        # 英文标识符命中文件路径/符号（如 main、gateway、kafka）时加强
        path_sym = f"{row.get('path', '')} {row.get('symbol', '')}".lower()
        for term in qtf:
            if len(term) > 2 and term.isascii() and term in path_sym:
                score *= 1.25
                break
        scored.append((row, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def _deep_read(root: str, rel: str, start: int, end: int, max_lines: int = 60) -> Optional[str]:
    p = Path(root) / rel
    try:
        text = read_text_safe(p, 2 * 1024 * 1024)
    except Exception:
        return None
    lines = text.splitlines()
    lo = max(1, start - 3)
    hi = min(len(lines), max(end, start) + 20)
    if hi - lo + 1 > max_lines:
        hi = lo + max_lines - 1
    seg = lines[lo - 1: hi]
    return "\n".join(seg).strip() or None


def build_context_from_hits(hits, root: str, budget_tokens: int = 3000,
                            deep_top_k: int = 2) -> Tuple[str, List[Citation], int]:
    """把已排好序的候选命中打包成上下文（片段按序标注）。"""
    if not hits:
        return "", [], 0
    parts: List[str] = []
    citations: List[Citation] = []
    used = 0
    for rank, (row, score) in enumerate(hits, start=1):
        text = row.get("text", "")
        if rank <= deep_top_k:
            deeper = _deep_read(root, row["path"], int(row["start_line"]),
                                int(row["end_line"]))
            if deeper:
                text = deeper
        header = f"[片段{rank}] {row['path']}:{row['start_line']}-{row['end_line']}"
        if row.get("symbol"):
            header += f" ({row['kind']}:{row['symbol']})"
        block = header + "\n" + text
        block_tokens = estimate_tokens(block)
        if used + block_tokens > budget_tokens and parts:
            break
        parts.append(block)
        used += block_tokens
        citations.append(Citation(
            path=row["path"], start_line=int(row["start_line"]),
            end_line=int(row["end_line"]), symbol=row.get("symbol", ""),
            kind=row.get("kind", ""), score=score,
        ))
        if used >= budget_tokens:
            break
    return "\n\n".join(parts), citations, used


def build_context(store: Store, workspace_id: str, root: str, query: str,
                  budget_tokens: int = 3000, top_k: int = 15,
                  deep_top_k: int = 3) -> Tuple[str, List[Citation], int]:
    """返回 (打包后的上下文文本, 引用列表, 实际使用的 token 数)。"""
    hits = search(store, workspace_id, query, top_k=top_k)
    return build_context_from_hits(hits, root, budget_tokens, deep_top_k)
