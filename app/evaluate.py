"""过程评估：对一次问答 trace 做体检，判断请求/回答是否合理，并给出 harness 改进建议。

两段式：
  A. 规则检查（零成本、确定性）：检索是否命中、上下文是否为空、是否被截断、
     token 预算是否够、回答是否为空/过短、是否带引用、模型是否失败/降级、
     回答里引用的文件是否真在检索结果里（接地性）、耗时是否异常。
  B. LLM 评判（可选，1 次调用）：给"忠实度/相关性/完整性/是否答非所问"打分，
     并输出可落地的 harness 改进建议。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .evidence import evidence_coverage, identifier_hit

JUDGE_SYSTEM = (
    "你是严格的问答质量评审员。只依据给出的『问题、检索片段、发给模型的提示、模型回答』做判断，"
    "不得臆测没有的信息。输出必须是 JSON 对象，字段："
    '{"verdict":"good|fair|poor","score":0-100,'
    '"faithful":true/false,"relevant":true/false,"complete":true/false,'
    '"off_topic":true/false,"reasons":["..."],'
    '"harness_improvements":["..."]}。'
    "其中 harness_improvements 要具体可执行（例如：检索应召回哪个文件/符号、上下文应补充什么、"
    "提示词应如何调整、是否需要提高 top_k/开启重排/增加某类知识卡），每条一句话。"
)


def _events(trace: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for e in trace.get("events", []):
        out[e.get("stage")] = e.get("data")
    return out


def _check_rules(trace: Dict[str, Any], ev: Dict[str, Any]) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, severity: str = "warn", hint: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail,
                       "severity": "ok" if ok else severity, "hint": hint})

    ret = ev.get("retrieve") or {}
    llm = ev.get("llm") or {}
    ans = ev.get("answer") or {}
    prompt = ev.get("prompt") or {}
    gate = ev.get("gate") or {}
    cache = ev.get("cache") or {}
    gated = bool(gate.get("action") == "insufficient_evidence")
    cached = bool(cache.get("hit"))

    ctx_tokens = int(ret.get("context_tokens") or 0)
    citations = ret.get("citations") or []
    answer_text = (ans.get("text") or llm.get("content") or "").strip()
    intent = ev.get("intent") or {}
    q = (intent.get("effective_question") or trace.get("question") or "").strip()
    cand_text = ((ret.get("context_preview") or "") + " " +
                 " ".join(str(c.get("path", "")) + " " + str(c.get("symbol", ""))
                          for c in citations if isinstance(c, dict))).lower()
    # 新 trace 使用对完整上下文计算的报告，旧 trace 用共享算法重算。
    evidence = ev.get("evidence") or evidence_coverage(
        q, ret.get("context_preview") or "", citations,
        original_question=(ev.get("sanitized") or {}).get("cleaned", trace.get("question")),
        scope_terms=(trace.get("workspace_id", ""),))
    terms, covered = evidence["terms"], evidence["covered"]
    coverage = float(evidence["coverage"])
    min_coverage = float(evidence.get("min_coverage", gate.get("min_coverage", 0.2)))
    false_gate = gated and ctx_tokens > 0 and bool(citations) and coverage >= min_coverage

    add("检索命中", ctx_tokens > 0 and len(citations) > 0,
        f"上下文 {ctx_tokens} token，{len(citations)} 条片段",
        "high" if ctx_tokens == 0 else "warn",
        "召回为空：检查库是否已建（kb-build）、换更具体的类名/函数名/术语")

    budget = int(ret.get("budget_tokens") or 0)
    near_full = budget and ctx_tokens >= budget * 0.95
    add("上下文预算", not near_full,
        f"用量 {ctx_tokens}/{budget}" + ("（接近上限，可能被截断）" if near_full else ""),
        "warn", "上下文接近上限：可提高 knowledge.retrieve.budget_tokens，或缩小片段粒度")

    if gated:
        add("调用模型", not false_gate,
            "未调用（门槛误拦，已有相关证据）" if false_gate else "未调用（词面覆盖不足，触发证据门槛）",
            "high", "修正证据门槛的分词和工程名处理；相关片段已召回，调权重无效")
    elif cached:
        add("调用模型", True, "未调用（命中同问题缓存，符合预期）", "warn", "")
    else:
        add("调用模型", bool(llm.get("ok")),
            "成功" if llm.get("ok") else f"失败/降级：{llm.get('error','')}",
            "high", "模型未生效：检查 llm 配置/额度；或开启重排前先把基础链跑通")

    fin = (llm.get("finish_reason") or "").lower()
    add("未被长度截断", fin not in ("length", "max_tokens"),
        f"finish_reason={fin or 'n/a'}",
        "warn", "回答可能被 max_tokens 截断：提高 max_tokens（推理型模型建议≥4096）")

    add("回答非空", len(answer_text) >= 20,
        f"回答长度 {len(answer_text)} 字",
        "high", "回答过短/为空：检查检索片段是否相关、提示词是否要求过度精简")

    used_cites = ans.get("citations") or []
    add("带引用来源", len(used_cites) > 0,
        f"{len(used_cites)} 个来源",
        "warn", "回答未标注来源：可在提示词中强制要求关键结论标注 path:line")

    # 接地性：回答里出现的文件是否真在检索结果（citations 或 context 原文）中
    if answer_text and citations:
        cited_paths = {c.get("path", "") for c in citations if isinstance(c, dict)}
        ctx_low = (ret.get("context_preview") or "").lower()
        mentioned = set(re.findall(r"([\w./\\-]+\.(?:go|py|java|ts|js|md|sql|yaml|yml|json|proto))", answer_text))
        unsupported = []
        for m in mentioned:
            ml = m.lower()
            if any(ml.endswith(str(p).lower()) or str(p).lower().endswith(ml) for p in cited_paths):
                continue
            if ml in ctx_low:      # 在上下文中出现过（被截断未列入 citations）也算可追溯
                continue
            unsupported.append(m)
        add("引用可追溯", len(unsupported) == 0,
            "所有引用文件都在检索结果中" if not unsupported else f"疑似编造/未在检索结果中：{unsupported[:3]}",
            "high", "出现检索结果里没有的文件：需提高忠实度约束，或补充检索覆盖")

    sys_t = int(prompt.get("system_tokens") or 0)
    usr_t = int(prompt.get("user_tokens") or 0)
    if gated or cached:
        add("提示词完整", True, "未组装（未调用模型，符合预期）", "warn", "")
    else:
        add("提示词完整", sys_t > 0 and usr_t > 0,
            f"system {sys_t} / user {usr_t} token", "warn", "提示词为空：检查 prompt 组装")

    add("证据覆盖问题", ctx_tokens > 0 and bool(citations) and coverage >= min_coverage,
        f"词面覆盖 {coverage:.0%}，命中 {len(covered)}/{len(terms)}（{', '.join(covered[:5]) or '无'}）；不代表证据完整",
        "high", "候选主题覆盖不足：需补召回/扩库，并检查查询改写是否偏离原问题，调权重无效")
    if gated:
        add("证据门槛", not false_gate,
            "已有相关片段却未生成回答，属于门槛误拦" if false_gate else "现有词面证据不足，保持拒答",
            "high", "修正证据门槛的分词和工程名处理；相关片段已召回，调权重无效")

    # 忠实度（规则近似）：回答里的数字/标识符是否能在候选里找到
    if gated:
        add("回答有据可依", True, "返回门槛提示，未生成事实性回答；不对拒答模板作标识符忠实度评分")
    elif answer_text and citations:
        idents = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", answer_text))
        idents -= {"this", "that", "func", "string", "return", "type", "struct", "package", "import"}
        grounded = {i for i in idents if identifier_hit(i, cand_text)}
        rd = (len(grounded) / len(idents)) if idents else 1.0
        add("回答有据可依", rd >= 0.5,
            f"回答标识符 {len(grounded)}/{len(idents)} 可在候选找到（{rd*100:.0f}%）",
            "high",
            "回答大量内容不在候选里：加强对证据的约束、或补充相关的候选片段")

    return {"checks": checks,
            "failed": [c for c in checks if not c["ok"]],
            "score_rule": max(0, 100 - 15 * len([c for c in checks if not c["ok"] and c["severity"] == "high"])
                              - 6 * len([c for c in checks if not c["ok"] and c["severity"] == "warn"]))}


def _compact_prompt_for_judge(ev: Dict[str, Any]) -> str:
    ret = ev.get("retrieve") or {}
    prompt = ev.get("prompt") or {}
    llm = ev.get("llm") or {}
    q = (ev.get("intent") or {}).get("effective_question") or ""
    cites = ret.get("citations") or []
    cite_lines = "\n".join(
        f"- {c.get('path')}:{c.get('start_line')}-{c.get('end_line')} ({c.get('symbol','')})"
        for c in cites[:10]) or "（无）"
    ctx = (ret.get("context_preview") or "")[:3000]
    answer = ((ev.get("answer") or {}).get("text") or llm.get("content") or "")[:2500]
    return (
        f"【问题】{q}\n\n"
        f"【检索来源({len(cites)})】\n{cite_lines}\n\n"
        f"【检索片段节选】\n{ctx}\n\n"
        f"【提示词 system】\n{prompt.get('system','')[:600]}\n\n"
        f"【提示词 user（节选）】\n{prompt.get('user','')[:1200]}\n\n"
        f"【模型回答】\n{answer}\n"
    )


def _classify_improvement(text: str) -> str:
    """把一条改进建议归到三类之一，决定它是否能被 harness 自动吸收。
    - boost   : 只需调整召回顺序（我们知道该召回谁）→ 可由 boost 规则解决
    - ingest  : 需要把某些文件/目录纳入索引、或补全知识（数据缺失）
    - config  : 需要改检索参数/提示词/模型参数（非单条规则能解）
    """
    t = str(text).lower()
    ingest_kw = ("未纳入", "未建", "补充召回", "补召回", "扩库", "纳入库", "扩充", "缺失", "缺少",
                 "没有收录", "未收录", "不在库", "建库", "扩大行数", "被截断", "补充相关的候选")
    config_kw = ("top_k", "预算", "budget", "重排", "rerank", "max_tokens", "提示词",
                 "prompt", "过滤", "无关片段", "类型过滤", "温度", "temperature", "门槛", "误拦",
                 "分词", "改写", "约束")
    if any(k in t for k in config_kw):
        return "config"
    if any(k in t for k in ingest_kw):
        return "ingest"
    # 只有明确指向目标文件/符号的排序建议才可能靠加权解决；未知建议不默认学习。
    from .feedback import extract_boost_candidates
    if "调权重无效" not in t and extract_boost_candidates({"improvements": [str(text)]}):
        return "boost"
    return "config"


def _attach_actions(result: Dict[str, Any]) -> None:
    """给每条改进建议打上 action 分类，并给出可行动作。"""
    actions: List[Dict[str, str]] = []
    for imp in result.get("improvements", []):
        kind = _classify_improvement(imp)
        act = {
            "boost": "已/可自动沉淀为召回提示（下次检索加权）",
            "ingest": "需补充召回：把相关目录/文件纳入库（改 include 或 kb-build），"
                      "或扩大片段行数——调权重无效",
            "config": "需修正门槛、查询改写、检索参数或提示词；不会自动生成加权规则",
        }[kind]
        actions.append({"improvement": imp, "kind": kind, "action": act})
    result["improvement_actions"] = actions
    # 命中缓存并不豁免其他缺陷。
    root_causes = [a for a in actions if a["kind"] in ("ingest", "config")]
    result["weight_only_insufficient"] = bool(root_causes)
    if root_causes:
        result["root_cause_hint"] = (
            "当前问题不是排序能解决的："
            + ("候选覆盖不足（需补充召回/扩库）。" if any(a["kind"] == "ingest" for a in root_causes) else "")
            + ("需检查门槛、改写或提示词。" if any(a["kind"] == "config" for a in root_causes) else "")
        )


async def judge_trace(trace: Dict[str, Any], llm, provider_note: str = "") -> Dict[str, Any]:
    """返回 {rule:..., llm:..., verdict, score, improvements[]}。"""
    ev = _events(trace)
    rule = _check_rules(trace, ev)

    result: Dict[str, Any] = {"rule": rule, "llm": None,
                              "verdict": "fair", "score": rule["score_rule"],
                              "retrieved_paths": [c.get("path", "") for c in (ev.get("retrieve") or {}).get("citations", [])],
                              "improvements": list(dict.fromkeys(c["hint"] for c in rule["failed"] if c.get("hint")))}

    if llm is not None and getattr(llm, "available", False) and (ev.get("llm") or {}).get("ok"):
        user = _compact_prompt_for_judge(ev)
        out = await llm.chat(JUDGE_SYSTEM, user)
        if out and not str(out).startswith("__LLM_ERROR__"):
            jd = _parse_json(out)
            if jd:
                result["llm"] = jd
                result["verdict"] = str(jd.get("verdict", "fair"))
                try:
                    result["score"] = int(jd.get("score", result["score"]))
                except (TypeError, ValueError):
                    pass
                imps = [str(x) for x in (jd.get("harness_improvements") or []) if str(x).strip()]
                result["improvements"] = list(dict.fromkeys(imps + result["improvements"]))
        else:
            result["llm"] = {"error": getattr(llm, "last_error", "") or "judge failed"}

    # 若证据覆盖不足/回答无据，分数上限压低（避免"格式没问题就给高分"）
    for ch in rule["checks"]:
        if ch["name"] in ("证据覆盖问题", "回答有据可依", "证据门槛") and not ch["ok"]:
            try:
                result["score"] = min(int(result.get("score", 100)), 55)
            except (TypeError, ValueError):
                result["score"] = 55
            if result.get("verdict") == "good":
                result["verdict"] = "fair"
            if ch["name"] == "证据门槛":
                result["verdict"] = "poor"

    _attach_actions(result)
    return result


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    text = str(text).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
