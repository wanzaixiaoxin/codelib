"""评估闭环：把评估结论里可落地的“应召回某文件/符号/目录”抽成 boost 规则。

输入：一次 trace + 它的评估报告（judge_trace 的返回）
输出：写入知识库 boost 表，下次检索自动加权命中。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .knowledge.schema import KnowledgeStore

# 从建议文本里抽“文件/目录”引用（含扩展名或目录形态）
_FILE_RE = re.compile(
    r"([\w./\\-]+?\.(?:go|py|java|kt|ts|tsx|js|jsx|c|cc|cpp|h|hpp|cs|rs|rb|php|scala|lua|sql|proto|"
    r"md|markdown|yaml|yml|json|toml|ini|conf|cfg))", re.I)
_DIR_RE = re.compile(r"\b([a-z_][\w-]*/)\b")
_IDENT_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]{2,})`")

# 只认“缺失/应召回/建议补充/未纳入”这类语义，避免把已命中的也加成 boost
_MISSING_HINTS = ("缺少", "未包含", "未纳入", "应召回", "建议召回", "建议补充", "可补充",
                  "未被检索", "未被召回", "未出现在检索", "缺失", "补充召回", "应纳入",
                  "没有召回", "截断")
_NO_BOOST_HINTS = ("调权重无效", "无关", "不应", "无需", "已召回", "已命中", "已经召回",
                   "门槛", "误拦", "分词", "提示词", "约束")


def extract_boost_candidates(report: Dict[str, Any]) -> List[Dict[str, str]]:
    """从评估报告提取 boost 候选：{kind, pattern, why}。去重保序。"""
    texts: List[str] = []
    if "improvement_actions" in report:
        # 结构化分流优先：门槛修复和扩库建议不能再从原文里被反向抽成加权。
        texts = [str(action.get("improvement", ""))
                 for action in report["improvement_actions"] if action.get("kind") == "boost"]
    else:
        for check in (report.get("rule") or {}).get("checks", []) or []:
            if not check.get("ok") and check.get("hint"):
                texts.append(str(check["hint"]))
        texts.extend(str(x) for x in (report.get("llm") or {}).get("harness_improvements", []) or [])
        texts.extend(str(x) for x in report.get("improvements", []) or [])

    out: List[Dict[str, str]] = []
    seen = set()
    retrieved_paths = {str(path).replace("\\", "/").lower() for path in report.get("retrieved_paths", [])}

    def push(kind: str, pattern: str, why: str) -> None:
        pattern = pattern.strip().strip("`'\"")
        if not pattern or len(pattern) < 3:
            return
        if kind == "path" and pattern.lower() in retrieved_paths:
            return
        if kind == "dir" and any(path.startswith(pattern.lower()) for path in retrieved_paths):
            return
        k = (kind, pattern.lower())
        if k in seen:
            return
        seen.add(k)
        out.append({"kind": kind, "pattern": pattern, "why": why[:200]})

    sentences = [part.strip() for text in texts for part in re.split(r"[。！？；;\n]+", text) if part.strip()]
    for t in sentences:
        if any(h in t for h in _NO_BOOST_HINTS) or not any(h in t for h in _MISSING_HINTS):
            continue
        # 文件
        files = _FILE_RE.findall(t)
        for m in files:
            push("path", m.replace("\\", "/"), t)
        # 只在明确推荐目录时加目录权重，避免一个文件名污染整个父目录。
        if not files:
            for m in _DIR_RE.findall(t):
                push("dir", m, t)
        # 反引号标识符（多为函数/符号名）
        for m in _IDENT_RE.findall(t):
            if "." in m and _FILE_RE.match(m):
                continue
            push("symbol", m, t)
    return out


def apply_report_to_boosts(kb: KnowledgeStore, workspace: str,
                           report: Dict[str, Any], source: str = "eval",
                           max_items: int = 12) -> List[Dict[str, str]]:
    """把报告里的候选写入 boost（每项 +权重，重复则累加）。返回写入的候选。"""
    cands = extract_boost_candidates(report)[:max_items]
    for c in cands:
        try:
            kb.add_boost(workspace, c["kind"], c["pattern"], weight=1.5, source=source)
        except Exception:
            pass
    return cands
