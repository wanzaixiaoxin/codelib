"""在线检索：FTS5 BM25（+中英同义词扩写）→ 多查询融合/可选重排 → 图扩展 → 全局预算打包。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import AppConfig, WorkspaceConfig
from ..models import Citation
from ..retrieval import _expand_query
from ..utils import clip, estimate_tokens, read_text_safe, tokenize
from .schema import KnowledgeStore

_RRF_K = 60


def _read_symbol_body(root: str, rel: str, start: int, end: int,
                      max_lines: int = 160) -> Optional[str]:
    """按行区间从源文件精确回填代码片段，确保完整（不被库内截断）。"""
    p = Path(root) / rel
    try:
        text = read_text_safe(p, 4 * 1024 * 1024)
    except Exception:
        return None
    lines = text.splitlines()
    lo = max(1, start)
    hi = min(len(lines), max(end, start))
    if hi - lo + 1 > max_lines:
        hi = lo + max_lines - 1
    seg = "\n".join(lines[lo - 1: hi]).strip()
    return seg or None


def _parse_lines(s: str) -> Tuple[int, int]:
    m = re.match(r"(\d+)\s*-\s*(\d+)", s or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"(\d+)", s or "")
    if m:
        return int(m.group(1)), int(m.group(1))
    return 1, 1


def _apply_boosts(kb: KnowledgeStore, workspace: str,
                  cands: List[Dict[str, Any]]) -> int:
    """用 boost 规则给候选加权（path/symbol 命中即乘权重）。返回命中规则数。"""
    try:
        rules = kb.get_boost_rules(workspace)
    except Exception:
        return 0
    if not rules or not cands:
        return 0
    hit_count = 0
    for c in cands:
        path = (c.get("path") or "").lower()
        sym = (c.get("symbol") or "").lower()
        factor = 1.0
        for r in rules:
            pat = (r.get("pattern") or "").lower()
            if not pat:
                continue
            kind = r.get("kind")
            w = float(r.get("weight") or 1.0)
            if kind == "path" and pat in path:
                factor = max(factor, w)
            elif kind == "dir" and path.startswith(pat):
                factor = max(factor, w)
            elif kind == "symbol" and pat == sym:
                factor = max(factor, w)
        if factor > 1.0:
            c["score"] = float(c.get("score", 0.0)) * factor
            c["boosted"] = True
            hit_count += 1
    if hit_count:
        cands.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
    return hit_count


def _query_tokens(question: str, cfg: AppConfig) -> List[str]:
    toks = tokenize(question)
    mode = str((cfg.knowledge or {}).get("retrieve", {}).get("query_expand", "synonym"))
    if mode == "synonym":
        toks = toks + _expand_query(toks)
    return list(dict.fromkeys(toks))


def _query_variants(question: str) -> List[str]:
    """Search the full question and substantive comma-separated clauses independently."""
    clauses = [x.strip() for x in re.split(r"[，,；;。\n]+", question or "") if x.strip()]
    out = [question.strip()] if question and question.strip() else []
    for clause in clauses:
        if len(clause) >= 6 and clause not in out:
            out.append(clause)
        if len(out) >= 4:
            break
    return out


def _rrf(hits_list: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    scores: Dict[int, float] = {}
    keep: Dict[int, Dict[str, Any]] = {}
    for hits in hits_list:
        for rank, c in enumerate(hits):
            rid = int(c["rid"])
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (_RRF_K + rank + 1)
            keep[rid] = c
    ordered = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    out = []
    for rid, sc in ordered:
        d = dict(keep[rid])
        d["score"] = sc
        out.append(d)
    return out


async def _llm_rerank(llm, question: str, cands: List[Dict[str, Any]],
                      top_n: int) -> List[Dict[str, Any]]:
    if not cands or len(cands) <= top_n or llm is None or not getattr(llm, "available", False):
        return cands[:top_n]
    system = ("你是代码检索相关性裁判。根据用户问题，从候选里选出真正能回答问题的前 N 个，"
              "忽略只是关键词相近但无关的。只输出编号（从1开始），逗号分隔，按相关度降序。")
    lines = []
    for i, c in enumerate(cands, 1):
        snippet = clip((c.get("text") or "").replace("\n", " "), 160)
        lines.append(f"{i}. {c.get('path','')} | {c.get('symbol','')} | {snippet}")
    user = f"问题：{question}\n\n候选：\n" + "\n".join(lines) + f"\n\n只输出前 {top_n} 个编号："
    out = await llm.chat(system, user)
    if out and not str(out).startswith("__LLM_ERROR__"):
        nums = [int(x) for x in re.findall(r"\d+", str(out))]
        picked: List[int] = []
        for n in nums:
            if 1 <= n <= len(cands) and (n - 1) not in picked:
                picked.append(n - 1)
            if len(picked) >= top_n:
                break
        if picked:
            return [cands[i] for i in picked]
    return cands[:top_n]


async def build_context(kb: KnowledgeStore, cfg: AppConfig, ws: WorkspaceConfig,
                        question: str, budget_tokens: int = 4000, top_k: int = 40,
                        deep_top_k: int = 3, llm=None, deep: bool = False
                        ) -> Tuple[str, List[Citation], int]:
    from .build import ws_kb_id
    kbkey = ws_kb_id(ws.id, ws.name)
    root_dir = ws.path
    if kb.counts(kbkey)["chunks"] == 0:
        return "", [], 0

    variants = _query_variants(question)
    hits = [kb.search(kbkey, _query_tokens(q, cfg), limit=max(top_k * 2, 30))
            for q in variants]
    fused = _rrf(hits)

    # 评估闭环：用已沉淀的“召回提示”对候选加权
    boost_hits = _apply_boosts(kb, kbkey, fused)
    kb_cfg = cfg.knowledge or {}
    rerank_cfg = kb_cfg.get("rerank", {}) or {}
    mode = str(rerank_cfg.get("mode", "none"))
    top_n = max(1, int(rerank_cfg.get("top_n", 10))) * (2 if deep else 1)
    if mode == "llm":
        cands = await _llm_rerank(llm, question, fused[:max(top_k, top_n)], top_n)
    else:
        cands = fused[:top_n]

    # 图扩展 1 跳（调用方/被调用）
    hops = int((kb_cfg.get("retrieve", {}) or {}).get("graph_hops", 1))
    expanded: List[Dict[str, Any]] = list(cands)
    if hops >= 1:
        ref_ids = [c["ref_id"] for c in cands if c.get("kind") == "code"]
        neigh = kb.neighbors(kbkey, ref_ids, limit=12)
        for c in kb.chunks_by_refs(kbkey, neigh, limit=8):
            d = dict(c)
            d["score"] = 0.0
            expanded.append(d)
    max_parts = top_n + min(4, max(0, len(expanded) - len(cands)))

    parts: List[str] = []
    citations: List[Citation] = []
    used = 0
    seen = set()
    for rank, c in enumerate(expanded, 1):
        ckey = (c.get("path", ""), c.get("lines", ""), c.get("symbol", ""))
        if ckey in seen:
            continue
        seen.add(ckey)
        a, b = _parse_lines(c.get("lines", ""))
        text = c.get("text") or ""
        # 代码片段：从源文件按行区间精确回填（保证完整、不被建库时截断）
        if c.get("kind") == "code" and b > a:
            deeper = _read_symbol_body(root_dir, c.get("path", ""), a, b, max_lines=160)
            if deeper:
                text = deeper
        header = f"[片段{rank}] {c.get('path','')}:{c.get('lines','')} ({c.get('kind','')}:{c.get('symbol','')})"
        block = header + "\n" + text
        bt = estimate_tokens(block)
        if used + bt > budget_tokens and parts:
            break
        parts.append(block)
        used += bt
        citations.append(Citation(path=c.get("path", ""), start_line=a, end_line=b,
                                  symbol=c.get("symbol", ""), kind=c.get("kind", ""),
                                  score=float(c.get("score", 0.0))))
        if len(parts) >= max(max_parts, 8):
            break
    return "\n\n".join(parts), citations, used
