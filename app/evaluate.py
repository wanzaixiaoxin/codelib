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

    add("检索命中", ctx_tokens > 0 and len(citations) > 0,
        f"上下文 {ctx_tokens} token，{len(citations)} 条片段",
        "high" if ctx_tokens == 0 else "warn",
        "召回为空：检查库是否已建（kb-build）、换更具体的类名/函数名/术语")

    budget = int(ret.get("budget_tokens") or 0)
    near_full = budget and ctx_tokens >= budget * 0.95
    add("上下文预算", not near_full,
        f"用量 {ctx_tokens}/{budget}" + ("（接近上限，可能被截断）" if near_full else ""),
        "warn", "上下文接近上限：可提高 token_budget，或缩小片段粒度")

    if gated:
        add("调用模型", True, "未调用（证据不足，主动不硬答，符合预期）", "warn", "")
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

    # 证据充分性：候选里是否真有一条“覆盖问题关键实体”的片段（仅排序无关）
    # 复用与门控一致的中文多粒度算法（此处内联一份，保持评估独立、可单测）
    intent = ev.get("intent") or {}
    q = (intent.get("effective_question") or trace.get("question") or "").strip()
    cand_text = ((ret.get("context_preview") or "") + " " +
                 " ".join(str(c.get("path", "")) + " " + str(c.get("symbol", ""))
                          for c in citations if isinstance(c, dict))).lower()

    _cmp_split = re.compile(r"的|了|吗|呢|吧|啊|是|在|把|被|和|与|及|或|对|从|向|为|这|那|个|"
                            r"请|问|如|何|怎|样|什|么|哪|些|能|可|要|会|就|都|也|还|又|很|再|"
                            r"所|以|而|但|并|等|中|上|下|里|后|前|先|一共|多少|几个|这个|那个")
    _cmp_stop = {"函数", "方法", "参数", "时候", "问题", "一下", "东西", "情况"}
    ascii_terms = re.findall(r"[A-Za-z_][A-Za-z0-9_.]{2,}", q)
    zh_units: List[str] = []
    for seg in re.findall(r"[\u4e00-\u9fff]+", q):
        for part in _cmp_split.split(seg):
            part = part.strip()
            if len(part) >= 2 and part not in _cmp_stop and part not in zh_units:
                zh_units.append(part)

    def _unit_hit(u: str) -> bool:
        if u.lower() in cand_text:
            return True
        if re.fullmatch(r"[\u4e00-\u9fff]+", u):
            for n in (3, 2):
                for i in range(len(u) - n + 1):
                    sub = u[i:i + n]
                    if sub not in _cmp_stop and sub in cand_text:
                        return True
        return False

    if ascii_terms:
        terms: List[str] = list(dict.fromkeys(ascii_terms))[:10]
        covered = [t for t in terms if t.lower() in cand_text]
    else:
        terms = zh_units[:8]
        covered = [t for t in terms if _unit_hit(t)]
    coverage = (len(covered) / len(terms)) if terms else 1.0
    # 门控拦截 = 系统有意不硬答；此时把“证据覆盖”记为已识别（不算缺陷），但给出提示
    if gated:
        add("证据覆盖问题", False,
            f"系统已识别覆盖率不足并主动不硬答（命中 {len(covered)}/{len(terms)}）",
            "warn", "这正是期望行为：需要补召回/扩库才能作答，调权重无效")
    else:
        add("证据覆盖问题", coverage >= 0.5,
            f"问题关键词命中 {len(covered)}/{len(terms)}（{', '.join(covered[:5]) or '无'}）",
            "high",
            "候选缺少能回答问题的主体内容：需补充召回（换词/扩大检索/把该目录纳入库），仅调权重无效")

    # 忠实度（规则近似）：回答里的数字/标识符是否能在候选里找到
    if answer_text and citations:
        idents = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", answer_text))
        idents -= {"this", "that", "func", "string", "return", "type", "struct", "package", "import"}
        grounded = {i for i in idents if i.lower() in cand_text}
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
    answer = (llm.get("content") or (ev.get("answer") or {}).get("text") or "")[:2500]
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
    t = str(text)
    ingest_kw = ("未纳入", "未建", "补充召回", "应召回", "纳入库", "扩充", "缺失", "缺少",
                 "没有收录", "未收录", "不在库", "建库", "扩大行数", "被截断")
    config_kw = ("top_k", "预算", "budget", "重排", "rerank", "max_tokens", "提示词",
                 "prompt", "过滤", "无关片段", "类型过滤", "温度", "temperature")
    if any(k in t for k in ingest_kw):
        return "ingest"
    if any(k in t for k in config_kw):
        return "config"
    return "boost"


def _attach_actions(result: Dict[str, Any]) -> None:
    """给每条改进建议打上 action 分类，并给出可行动作。"""
    actions: List[Dict[str, str]] = []
    for imp in result.get("improvements", []):
        kind = _classify_improvement(imp)
        act = {
            "boost": "已/可自动沉淀为召回提示（下次检索加权）",
            "ingest": "需补充召回：把相关目录/文件纳入库（改 include 或 kb-build），"
                      "或扩大片段行数——调权重无效",
            "config": "需改配置/提示词：如提高 top_k/token_budget、开启重排、"
                      "加过滤或调整提示词——调权重无效",
        }[kind]
        actions.append({"improvement": imp, "kind": kind, "action": act})
    result["improvement_actions"] = actions
    # 诊断：是否存在“调权重治不了”的根因（命中缓存不算缺陷）
    checks_by_name = {c["name"]: c for c in result.get("rule", {}).get("checks", [])}
    _cached = checks_by_name.get("调用模型", {}).get("detail", "").find("命中同问题缓存") >= 0
    if _cached:
        result["weight_only_insufficient"] = False
        return
    root_causes = [a for a in actions if a["kind"] in ("ingest", "config")]
    result["weight_only_insufficient"] = bool(root_causes)
    if root_causes:
        result["root_cause_hint"] = (
            "当前问题不是排序能解决的："
            + ("候选覆盖不足（需补充召回/扩库）。" if any(a["kind"] == "ingest" for a in root_causes) else "")
            + ("需调整检索参数或提示词。" if any(a["kind"] == "config" for a in root_causes) else "")
        )


async def judge_trace(trace: Dict[str, Any], llm, provider_note: str = "") -> Dict[str, Any]:
    """返回 {rule:..., llm:..., verdict, score, improvements[]}。"""
    ev = _events(trace)
    rule = _check_rules(trace, ev)

    result: Dict[str, Any] = {"rule": rule, "llm": None,
                              "verdict": "fair", "score": rule["score_rule"],
                              "improvements": [c["hint"] for c in rule["failed"] if c.get("hint")]}

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
        if ch["name"] in ("证据覆盖问题", "回答有据可依") and not ch["ok"]:
            try:
                result["score"] = min(int(result.get("score", 100)), 55)
            except (TypeError, ValueError):
                result["score"] = 55
            if result.get("verdict") == "good":
                result["verdict"] = "fair"

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
