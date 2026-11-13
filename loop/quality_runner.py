"""L0 三层归因引擎（只观察，不改动任何旋钮）。

读 data/codelib.db 的 trace_event + 题集，对「听懂 / 找对 / 讲清」分别出指标和归因。

用法:
    python -B loop/quality_runner.py                          # 用草稿题集 + 历史留痕
    python -B loop/quality_runner.py --cases loop/frozen/failure_cases.yaml
    python -B loop/quality_runner.py --json loop/reports/attribution.json

判据模式自动切换：
  - expected_paths / expected_intent_terms / expected_answer_points 没标 → 代理指标（词面/留痕信号）
  - 标好后 → 自动改用标注真判据（路径命中率是主判据）

只以 mode=ro 打开数据库；不调用 LLM、不启服务、不碰知识库工程。
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from export_failures import collect, norm, as_list, as_float  # noqa: E402

PREF_WORDS = ("不要", "别", "我不关心", "太多", "简短", "详细", "展开", "只要", "不用")
CODE_MARK = re.compile(r"```|^\s*def |^\s*class |->|\bself\.", re.M)
RETRIEVE_WORDS = ("召回", "补召回", "扩库", "候选", "片段", "检索")
ZH = re.compile(r"[\u4e00-\u9fff]{2,}")


def load_cases(path: Path):
    try:
        import yaml
    except ImportError:
        print("需要 PyYAML 才能读题集：pip install pyyaml", file=sys.stderr)
        raise SystemExit(2)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [d for d in data if isinstance(d, dict) and d.get("question")]


def listify(v):
    return [str(x) for x in (v or []) if str(x).strip()]


def hit_ratio(expected, text):
    """词面命中比例（用于标注真判据）。"""
    if not expected:
        return None
    t = (text or "").lower()
    return round(sum(1 for e in expected if e.lower() in t) / len(expected), 3)


def path_hit(expected, citations):
    if not expected:
        return None
    c = " ".join(citations).lower()
    return round(sum(1 for e in expected if e.lower() in c) / len(expected), 3)


def evaluate(case: dict, c: dict) -> dict:
    s, cross, au = c["stage"], c["cross_round"], (c["audit"] or {})
    intent = s.get("intent", {}) or {}
    ret = s.get("retrieve", {}) or {}
    ev = s.get("evidence", {}) or {}
    llm = s.get("llm", {}) or {}
    qr = s.get("quality_review", {}) or {}
    ans = s.get("answer", {}) or {}
    gate = s.get("gate", {}) or {}

    rq = str(intent.get("retrieval_query", "") or "")
    effective = str(intent.get("effective_question", "") or "")
    terms = [str(x) for x in as_list(ev.get("terms"))]
    missing = [str(x) for x in as_list(ev.get("missing"))]
    coverage = as_float(ev.get("coverage"), -1.0)
    cites = [x.get("path", "") for x in as_list(ret.get("citations"))
             if isinstance(x, dict) and x.get("path")]
    answer = str(ans.get("text", "") or "")
    answer_cites = [x.get("path", "") for x in as_list(ans.get("citations"))
                    if isinstance(x, dict) and x.get("path")]
    unsupported = as_list(qr.get("unsupported"))
    imps = [str(x) for x in (au.get("improvements") or [])]
    llm_ok = bool(llm.get("ok", False)) if llm else False
    gate_blocked = cross["gate_blocked_any"]
    finish = str(llm.get("finish_reason", "") or "")
    truncated = finish in ("length", "max_tokens")

    # ---- 标注真判据（标了就用，没标就走代理） ----
    exp_intent = listify(case.get("expected_intent_terms"))
    exp_paths = listify(case.get("expected_paths"))
    exp_points = listify(case.get("expected_answer_points"))
    exp_symbols = listify(case.get("expected_symbols"))
    labeled = bool(exp_paths or exp_intent or exp_points)

    intent_score = hit_ratio(exp_intent, rq + " " + effective)
    retrieval_score = path_hit(exp_paths, cites + answer_cites)
    presentation_score = hit_ratio(exp_points, answer)
    if exp_symbols and retrieval_score is not None:
        sym = path_hit(exp_symbols, " ".join(str(x) for x in as_list(ret.get("citations"))))
        retrieval_score = round((retrieval_score + (sym or 0)) / 2, 3)

    # ---- 代理信号 ----
    lost = [t for t in terms if t and t not in rq and t not in effective]
    topic_lost = bool(terms) and len(lost) / len(terms) > 0.5
    pref_violation = (any(w in case["question"] for w in PREF_WORDS)
                      and bool(CODE_MARK.search(answer)))
    has_structure = bool(re.search(r"(^|\n)#{1,4} |^\s*[-*] |^\s*\d+\. |结论|直接结论",
                                   answer, re.M))
    has_cites = bool(answer_cites or cites)
    zh_missing = any(ZH.search(m) for m in missing)

    # 关键：没有该层留痕就不判分（None），绝不默认「没问题」。
    has_intent_data = bool(rq or effective) and bool(terms)
    has_retrieval_data = bool(ret or ev or gate)
    has_answer_data = bool(answer) and bool(llm)

    if intent_score is None:
        intent_score = None if not has_intent_data else (0.0 if topic_lost else 1.0)
    if retrieval_score is None:
        retrieval_score = (None if not has_retrieval_data else
                           (0.0 if (gate_blocked or (0 <= coverage < 0.3)
                                    or zh_missing or unsupported) else 1.0))
    if presentation_score is None:
        if not has_answer_data or gate_blocked:
            presentation_score = None
        else:
            # 结构/易懂度交给标注 rubric + 独立裁判；代理层只判「明显坏掉」。
            # has_structure 靠正则猜格式，实测同题两次措辞不同就 1.0→0.0（误判）。
            bad = (not llm_ok or truncated or pref_violation or not has_cites
                   or len(answer) < 80 or bool(unsupported)
                   or qr.get("complete") is False or qr.get("relevant") is False)
            presentation_score = 0.0 if bad else 1.0

    # ---- 归因（retrieval 证据优先，其次 intent，最后 presentation） ----
    why, layers = [], set()
    if gate_blocked:
        layers.add("retrieval"); why.append("证据门槛拦截")
    if 0 <= coverage < 0.3 or zh_missing:
        layers.add("retrieval"); why.append(f"覆盖{coverage:.2f}/缺{','.join(missing[:2])}")
    if unsupported:
        layers.add("retrieval"); why.append(f"{len(unsupported)} 处无证据支撑")
    if any(w in " ".join(imps) for w in RETRIEVE_WORDS) or (au and au.get("score", 100) < 70):
        layers.add("retrieval"); why.append(f"评审{au.get('score')}分/建议补召回")
    if topic_lost:
        layers.add("intent"); why.append(f"改写丢主题词{','.join(lost[:2])}")
    if pref_violation:
        layers.add("presentation"); why.append("含展示偏好但答案仍有代码")
    if truncated:
        layers.add("presentation"); why.append("回答被截断")
    # 不用格式正则做归因：实测同题两次措辞不同即误判（见 docs/answer-quality-loop.md §7）
    scored = [x for x in (intent_score, retrieval_score, presentation_score) if x is not None]
    if not layers:
        layer = "ok" if len(scored) >= 2 else "no_data"
    elif len(layers) > 1:
        layer = "mixed"
    else:
        layer = next(iter(layers))

    return {
        "task_id": case.get("task_id", ""),
        "question": case["question"],
        "workspace": case.get("workspace", ""),
        "labeled": labeled,
        "split": case.get("split", "train"),
        "priority": case.get("priority", 0),
        "intent_score": intent_score,
        "retrieval_score": retrieval_score,
        "presentation_score": presentation_score,
        "attributed_layer": layer,
        "evidence_reasons": why,
        "signals": {
            "repeat_count": c["repeat_count"],
            "trace_count": cross["trace_count"],
            "data_coverage": {"intent": has_intent_data, "retrieval": has_retrieval_data,
                              "answer": has_answer_data},
            "audit_score": au.get("score"),
            "retrieval_query": rq,
            "topic_lost_terms": lost[:4],
            "coverage": None if coverage < 0 else round(coverage, 3),
            "missing": missing[:4],
            "gate_blocked": gate_blocked,
            "unsupported_count": len(unsupported),
            "citations": list(dict.fromkeys(cites))[:6],
            "answer_chars": len(answer),
            "llm_ok": llm_ok,
            "truncated": truncated,
            "pref_violation": pref_violation,
            "has_structure": has_structure,
        },
    }


def avg(vals):
    v = [x for x in vals if x is not None]
    return round(sum(v) / len(v), 3) if v else None


def fmt(v):
    return "—" if v is None else f"{v:.2f}"


def build_report(rows, meta) -> str:
    dist = Counter(r["attributed_layer"] for r in rows)
    labeled = sum(1 for r in rows if r["labeled"])
    no_trace = sum(1 for r in rows if r["signals"]["trace_count"] == 0)
    L = []
    L.append("# L0 三层归因报告（只观察，不改旋钮）")
    L.append("")
    L.append(f"- 生成时间：{meta['generated_at']}")
    L.append(f"- 数据库：`{meta['db']}`（mode=ro）／题集：`{meta['cases']}`")
    cov = meta.get("coverage", {})
    L.append(f"- 判据模式：**{meta['mode']}**（{labeled}/{len(rows)} 题至少填了一个标注字段）")
    L.append(f"- 标注明细：`expected_paths` {cov.get('expected_paths', 0)}/{len(rows)}、"
             f"`expected_answer_points` {cov.get('expected_answer_points', 0)}/{len(rows)}、"
             f"`failure_layer` {cov.get('failure_layer', 0)}/{len(rows)}；"
             "只有填了对应字段的那层才用真判据，其余仍是代理指标")
    L.append(f"- `report_sha256`：`{meta['sha']}` ← 再跑一次比对它即可验证可复现")
    L.append("")
    L.append("## 汇总")
    L.append("")
    L.append("| 层 | 均值 | 计分题数 | 说明 |")
    L.append("|---|---|---|---|")
    L.append(f"| 意图（听懂） | {fmt(avg([r['intent_score'] for r in rows]))} | "
             f"{sum(r['intent_score'] is not None for r in rows)} | 改写后主题词是否保留 |")
    L.append(f"| 检索（找对） | {fmt(avg([r['retrieval_score'] for r in rows]))} | "
             f"{sum(r['retrieval_score'] is not None for r in rows)} | **主判据**：期望文件命中率 |")
    L.append(f"| 编排（讲清） | {fmt(avg([r['presentation_score'] for r in rows]))} | "
             f"{sum(r['presentation_score'] is not None for r in rows)} | 结构/引用/截断/偏好 |")
    L.append("")
    L.append("归因分布：" + "，".join(f"`{k}` {v}" for k, v in dist.most_common()))
    L.append("")
    L.append(f"**数据覆盖**：{no_trace}/{len(rows)} 题没有全链路 trace 留痕，三层均记 `—`（不判分），"
             "标为 `no_data`。常见原因：旧对话（trace 功能上线前）、或 workspace 未映射到工程目录。"
             "`ok` 只表示「有数据且未发现问题」，不代表答得好。")
    for k, label in (("intent", "意图改写"), ("retrieval", "检索证据"), ("answer", "答案生成")):
        n = sum(1 for r in rows if r["signals"]["data_coverage"][k])
        L.append(f"- 有 {label} 留痕：{n}/{len(rows)} 题")
    L.append("")
    L.append("## 高优先样本（按 priority）")
    L.append("")
    for r in sorted(rows, key=lambda x: -x["priority"])[:6]:
        L.append(f"- **{r['task_id']}** `{r['attributed_layer']}` "
                 f"意图 {fmt(r['intent_score'])} / 检索 {fmt(r['retrieval_score'])} / "
                 f"编排 {fmt(r['presentation_score'])} — {r['question'][:38]}")
        why_hp = "；".join(r["evidence_reasons"]) or (
            "无 trace 留痕，不判分" if r["attributed_layer"] == "no_data" else "无明显失败信号")
        L.append(f"  - 依据：{why_hp}")
    L.append("")
    L.append("## 明细")
    L.append("")
    L.append("| task | 问题 | 意图 | 检索 | 编排 | 归因 | 关键依据 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in rows:
        why = ("；".join(r["evidence_reasons"])[:52]
               or ("无留痕·不判分" if r["attributed_layer"] == "no_data" else "—"))
        L.append(f"| {r['task_id']} | {r['question'][:30].replace('|', '/')} | "
                 f"{fmt(r['intent_score'])} | {fmt(r['retrieval_score'])} | "
                 f"{fmt(r['presentation_score'])} | `{r['attributed_layer']}` | "
                 f"{why.replace('|', '/')} |")
    L.append("")
    L.append("## 说明")
    L.append("")
    L.append("- **代理指标**（未标注时）：意图=改写后主题词是否保留；检索=gate 拦截 / "
             "coverage<0.3 / 中文词未命中 / 无证据支撑任一命中即 0；编排=LLM 成功且无截断、"
             "无偏好违反、有结构则 1。gate 拦下没调模型时编排层记 `—`（不适用）。")
    L.append("- `evidence_coverage` 对中文做 2/3 字子串匹配，**会骗人**，这里只当辅助；"
             "标好 `expected_paths` 后自动切到路径命中率当主判据。")
    L.append("- 标完 `loop/frozen/failure_cases.yaml` 后重跑本脚本，同一个脚本自动使用真判据，"
             "并可用 `failure_layer` 与 `attributed_layer` 算归因一致率（L0 验收 ≥70%）。")
    L.append("- `—` = 该层没有留痕，**不判分**；`no_data` = 三层都缺数据。别把不判分当成通过。")
    L.append("- 编排层代理只判明显故障（空/截断/无引用/违反偏好/无证据支撑）；"
             "结构是否易懂不靠正则猜，须用标注 `expected_answer_points` + 独立裁判 rubric。")
    L.append("- 本脚本不调用 LLM、不改任何旋钮；在线批量复测是 L1 的事。")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "codelib.db"))
    ap.add_argument("--cases", default=str(HERE / "failure_cases.draft.yaml"))
    ap.add_argument("--out", default=str(ROOT / "loop" / "reports" / "attribution.md"))
    ap.add_argument("--json", default=str(ROOT / "loop" / "reports" / "attribution.json"))
    ap.add_argument("--expect-hash", default="", help="比对上次 report_sha256，验证可复现")
    args = ap.parse_args()

    cases_path, db = Path(args.cases), Path(args.db)
    if not cases_path.exists() or not db.exists():
        print("题集或数据库不存在", file=sys.stderr)
        return 1
    cases = load_cases(cases_path)
    hist = collect(db)
    rows, missing = [], []
    for case in cases:
        key = norm(case.get("question"))
        c = hist.get(key)
        if not c:
            missing.append(case.get("question", ""))
            c = {"stage": {}, "cross_round": {"trace_count": 0, "gate_blocked_any": False,
                                              "revised_any": False, "llm_fail_any": False},
                 "audit": None, "tids": [], "repeat_count": 0, "sources": {},
                 "rep_trace": "", "ts": 0, "question": case.get("question", "")}
        rows.append(evaluate(case, c))
    rows.sort(key=lambda r: (-r["priority"], r["task_id"]))

    payload = {"rows": rows, "count": len(rows)}
    sha = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    default=str).encode("utf-8")).hexdigest()
    labeled = sum(1 for r in rows if r["labeled"])
    coverage = {
        "expected_paths": sum(1 for c in cases if listify(c.get("expected_paths"))),
        "expected_answer_points": sum(1 for c in cases if listify(c.get("expected_answer_points"))),
        "failure_layer": sum(1 for c in cases if str(c.get("failure_layer") or "").strip()),
    }
    n_labeled_fields = sum(coverage.values())
    meta = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "db": str(db), "cases": str(cases_path), "sha": sha,
        "coverage": coverage,
        "mode": ("标注真判据" if coverage["expected_paths"] == len(rows) and
                 coverage["failure_layer"] == len(rows) else
                 ("混合（部分字段已标注）" if n_labeled_fields else "代理指标（未标注）")),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_report(rows, meta) + "\n", encoding="utf-8")
    Path(args.json).write_text(json.dumps({**meta, "summary": {
        "count": len(rows), "labeled": labeled,
        "intent_avg": avg([r["intent_score"] for r in rows]),
        "retrieval_avg": avg([r["retrieval_score"] for r in rows]),
        "presentation_avg": avg([r["presentation_score"] for r in rows]),
        "layer_distribution": dict(Counter(r["attributed_layer"] for r in rows)),
        "cases_without_trace": len(missing),
    }, "rows": rows}, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    no_trace = sum(1 for r in rows if r["signals"]["trace_count"] == 0)
    print(f"归因 {len(rows)} 题｜已标注 {labeled}｜题集未匹配 {len(missing)}｜无 trace 留痕 {no_trace}")
    for key, label in (("intent_score", "意图"), ("retrieval_score", "检索"),
                       ("presentation_score", "编排")):
        n = sum(r[key] is not None for r in rows)
        print(f"  {label} 均值 {fmt(avg([r[key] for r in rows]))}（仅 {n} 题有留痕可判）")
    print("归因分布:", dict(Counter(r["attributed_layer"] for r in rows)))
    print("report_sha256:", sha)
    if args.expect_hash:
        print("可复现校验:", "PASS" if args.expect_hash.strip() == sha else "FAIL")
    print("报告:", out)
    print("JSON:", Path(args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
