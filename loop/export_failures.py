"""只读导出「问答失败候选」，生成待人工标注的 failure_cases.draft.yaml。

用法:
    python -B loop/export_failures.py
    python -B loop/export_failures.py --limit 30 --out loop/failure_cases.draft.yaml

零第三方依赖；数据库以 mode=ro 打开，绝不写入；不触碰知识库工程。
"""
from __future__ import annotations
import argparse
import collections
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def norm(s) -> str:
    """问题归一化：去空白与常见标点，用于跨表去重与重复提问统计。"""
    s = (s or "").strip().lower()
    return re.sub(r"[\s，。？?、！!,.;；：:]+", "", s)


def q(v) -> str:
    """YAML 1.2 兼容的双引号字符串（JSON 转义，中文不转义）。"""
    return json.dumps("" if v is None else str(v), ensure_ascii=False)


def arr(items) -> str:
    return "[" + ", ".join(q(x) for x in items) + "]"


def as_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip().startswith("["):
        try:
            return json.loads(v.replace("'", '"'))
        except Exception:
            return []
    return []


def as_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def collect(db_path: Path):
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cases: dict[str, dict] = {}

    def add(question, workspace, src, ref, ts=0):
        text = (question or "").strip()
        if not text:
            return
        c = cases.setdefault(norm(text), {
            "question": text, "workspace": workspace or "",
            "sources": collections.defaultdict(list), "ts": 0,
        })
        c["sources"][src].append(str(ref))
        c["ts"] = max(c["ts"], ts or 0)
        if workspace and not c["workspace"]:
            c["workspace"] = workspace

    for r in con.execute("SELECT id,workspace_id,content,created_at FROM conversations WHERE role='user'"):
        add(r["content"], r["workspace_id"], "conversation", r["id"], r["created_at"])
    for r in con.execute("SELECT trace_id,workspace_id,question,created_at FROM trace"):
        add(r["question"], r["workspace_id"], "trace", r["trace_id"], r["created_at"])
    for r in con.execute("SELECT id,workspace_id,question,created_at FROM qa_memory"):
        add(r["question"], r["workspace_id"], "qa_memory", r["id"], r["created_at"])

    # trace_id -> 归一化问题；events[(trace_id, stage)] = data
    tmap: dict[str, str] = {}
    for r in con.execute("SELECT trace_id,question FROM trace"):
        tmap[r["trace_id"]] = norm(r["question"])
    events: dict[tuple, dict] = {}
    for r in con.execute("SELECT trace_id,stage,data FROM trace_event ORDER BY seq"):
        try:
            d = json.loads(r["data"])
        except Exception:
            d = {}
        if isinstance(d, dict):
            events[(r["trace_id"], r["stage"])] = d

    audits: dict[str, dict] = {}
    for r in con.execute("SELECT id,trace_id,verdict,score,report FROM audit"):
        try:
            rep = json.loads(r["report"])
        except Exception:
            rep = {}
        if not isinstance(rep, dict):
            rep = {}
        audits[r["trace_id"]] = {
            "id": r["id"], "verdict": r["verdict"], "score": as_float(r["score"]),
            "improvements": list(rep.get("improvements") or rep.get("harness_improvements") or []),
            "root_cause_hint": rep.get("root_cause_hint", ""),
        }

    by_q_traces: dict[str, list] = collections.defaultdict(list)
    for tid, key in tmap.items():
        by_q_traces[key].append(tid)

    for key, c in cases.items():
        tids = by_q_traces.get(key, [])
        # 同一问题可能有多轮 trace：取「有评审且分数最低」的一轮做代表，
        # 避免把不同轮次的 stage 混在一起（例如 A 轮 coverage=1.0、B 轮被 gate 拦）。
        scored = [t for t in tids if t in audits]
        rep_tid = min(scored, key=lambda t: audits[t]["score"]) if scored else (tids[-1] if tids else None)
        stage = {st: d for (t, st), d in events.items()
                 if t == rep_tid and st not in ("prompt", "sanitized")}
        au = audits.get(rep_tid) if rep_tid else None
        c["tids"] = tids
        c["rep_trace"] = rep_tid or ""
        c["stage"] = stage
        c["audit"] = au
        c["cross_round"] = {
            "trace_count": len(tids),
            "gate_blocked_any": any(events.get((t, "gate"), {}).get("action") == "insufficient_evidence"
                                    for t in tids),
            "revised_any": any(bool(events.get((t, "quality_review"), {}).get("revised")) for t in tids),
            "llm_fail_any": any(not events.get((t, "llm"), {}).get("ok", False)
                                for t in tids if (t, "llm") in events),
        }
        c["repeat_count"] = len(c["sources"].get("conversation", []))
    con.close()
    return cases


PREF_WORDS = ("不要", "别", "我不关心", "太多", "简短", "详细", "展开", "只要", "不用")
RETRIEVE_WORDS = ("召回", "补召回", "扩库", "候选", "片段", "检索")


def build(c: dict, idx: int) -> dict:
    s = c["stage"]
    au = c["audit"] or {}
    intent = s.get("intent", {}) or {}
    ret = s.get("retrieve", {}) or {}
    ev = s.get("evidence", {}) or {}
    gate = s.get("gate", {}) or {}
    llm = s.get("llm", {}) or {}
    qr = s.get("quality_review", {}) or {}
    ans = s.get("answer", {}) or {}

    cites = [x.get("path", "") for x in as_list(ret.get("citations"))
             if isinstance(x, dict) and x.get("path")]
    missing = [str(x) for x in as_list(ev.get("missing"))]
    imps = [str(x) for x in (au.get("improvements") or [])]
    answer_text = str(ans.get("text", "") or "")
    coverage = as_float(ev.get("coverage"), -1.0)
    gate_action = str(gate.get("action", "") or "")
    llm_ok = bool(llm.get("ok", False)) if llm else False
    rq = str(intent.get("retrieval_query", "") or "")

    priority, reasons = 0, []
    if au and au.get("score", 100) < 70:
        priority += 4; reasons.append(f"评审 {au.get('score')} 分")
    elif au and au.get("score", 100) < 85:
        priority += 1
    if c["repeat_count"] >= 3:
        priority += 2; reasons.append(f"重复提问 {c['repeat_count']} 次")
    elif c["repeat_count"] >= 2:
        priority += 1; reasons.append(f"重复提问 {c['repeat_count']} 次")
    if c["cross_round"]["gate_blocked_any"]:
        priority += 2; reasons.append("被证据门槛拦截(任一轮)")
    if not llm_ok:
        priority += 2; reasons.append("LLM 未成功作答")
    if c["cross_round"]["revised_any"]:
        priority += 1; reasons.append("自检修订过(任一轮)")
    if 0 <= coverage < 0.3:
        priority += 1; reasons.append(f"证据覆盖 {coverage:.2f}")
    if missing:
        priority += 1
    if any(w in c["question"] for w in PREF_WORDS):
        priority += 1; reasons.append("含展示偏好词")
    if any(w in " ".join(imps) for w in ("调权重无效", "扩库", "补召回")):
        priority += 1; reasons.append("评审判定非排序问题")

    # 只作建议，最终以人工标注的 failure_layer 为准
    layer = "mixed"
    if any(w in c["question"] for w in PREF_WORDS):
        layer = "presentation"
    elif (c["cross_round"]["gate_blocked_any"] or (0 <= coverage < 0.4)
          or any(w in " ".join(imps) for w in RETRIEVE_WORDS)
          or any(re.search(r"[\u4e00-\u9fff]", m) for m in missing)):
        layer = "retrieval"
    elif rq and norm(rq) != norm(c["question"]) and intent.get("resolved_by") not in (None, "", "none"):
        layer = "intent"

    return {
        "task_id": f"fc_{idx:03d}",
        "question": c["question"],
        "workspace": c["workspace"],
        "sources": {k + "_ids": sorted(set(v)) for k, v in c["sources"].items()},
        "signals": {
            "repeat_count": c["repeat_count"],
            "audit_score": au.get("score"),
            "audit_verdict": au.get("verdict"),
            "retrieval_query": rq,
            "resolved_by": intent.get("resolved_by", ""),
            "evidence_coverage": None if coverage < 0 else round(coverage, 3),
            "missing_terms": missing,
            "gate_action": gate_action or None,
            "llm_ok": llm_ok,
            "quality_revised": c["cross_round"]["revised_any"],
            "gate_blocked_any": c["cross_round"]["gate_blocked_any"],
            "llm_fail_any": c["cross_round"]["llm_fail_any"],
            "trace_count": c["cross_round"]["trace_count"],
            "rep_trace": c["rep_trace"],
            "context_tokens": ret.get("context_tokens"),
            "answer_chars": len(answer_text),
            "citations": list(dict.fromkeys(cites))[:12],
            "answer_excerpt": answer_text[:140].replace("\n", " "),
            "harness_improvements": imps[:5],
            "root_cause_hint": au.get("root_cause_hint", ""),
        },
        "suggested_layer": layer,
        "layer_hint": "；".join(reasons),
        "priority": priority,
        "expected_intent_terms": [],
        "expected_paths": [],
        "expected_symbols": [],
        "expected_answer_points": [],
        "failure_layer": "",
        "split": "holdout" if idx % 4 == 0 else "train",
        "notes": "",
    }


HEADER = """# 问答失败候选（待人工标注）—— 由 loop/export_failures.py 只读导出
#
# 数据来源：data/codelib.db 的 conversations / trace / trace_event / audit / qa_memory
# 已按问题归一化去重；signals 是从留痕里自动抽出的诊断线索，供你判断，不是结论。
#
# 需要你填的 5 个字段：
#   expected_intent_terms   : 这个问题真正该抓住的主题词（中文业务词优先）
#   expected_paths          : 应该被召回的文件/文档路径（检索层主判据，最重要）
#   expected_symbols        : 应该命中的函数/类/符号（可空）
#   expected_answer_points  : 一个满意答案必须讲清的要点（编排层 rubric 锚点）
#   failure_layer           : intent | retrieval | presentation | mixed | ok
#                             ok = 当时其实答得可以，设 split: drop 剔除
#   split                   : train | holdout | drop（已按 1/4 自动预分 holdout，可改）
#   notes                   : 任意备注，比如"错别字：总一下→总结一下"
#
# suggested_layer / layer_hint / priority 只是脚本建议，可以无视。
# 标完把本文件另存为 loop/frozen/failure_cases.yaml，并纳入 hash 锁定。
# 若某题在知识库里根本没有对应代码/文档（该补语料而非调参），请在 notes 注明。
#
"""


def emit_yaml(rows) -> str:
    out = [HEADER]
    for r in rows:
        sig = r["signals"]
        out.append(f"- task_id: {q(r['task_id'])}")
        out.append(f"  question: {q(r['question'])}")
        out.append(f"  workspace: {q(r['workspace'])}")
        out.append("  sources:")
        for k, v in r["sources"].items():
            out.append(f"    {k}: {arr(v)}")
        out.append("  signals:")
        for k, v in sig.items():
            if isinstance(v, bool):
                out.append(f"    {k}: {'true' if v else 'false'}")
            elif v is None:
                out.append(f"    {k}: null")
            elif isinstance(v, (int, float)):
                out.append(f"    {k}: {v}")
            elif isinstance(v, list):
                out.append(f"    {k}: {arr(v)}")
            else:
                out.append(f"    {k}: {q(v)}")
        out.append(f"  suggested_layer: {q(r['suggested_layer'])}")
        out.append(f"  layer_hint: {q(r['layer_hint'])}")
        out.append(f"  priority: {r['priority']}")
        out.append("  # ↓↓↓ 以下由人工填写 ↓↓↓")
        for k in ("expected_intent_terms", "expected_paths", "expected_symbols",
                  "expected_answer_points"):
            out.append(f"  {k}: []")
        out.append(f'  failure_layer: ""')
        out.append(f"  split: {q(r['split'])}")
        out.append('  notes: ""')
        out.append("")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "codelib.db"))
    ap.add_argument("--out", default=str(ROOT / "loop" / "failure_cases.draft.yaml"))
    ap.add_argument("--limit", type=int, default=0, help="0 = 全部")
    args = ap.parse_args()
    db = Path(args.db)
    if not db.exists():
        print(f"找不到数据库: {db}", file=sys.stderr)
        return 1
    cases = collect(db)
    ordered = sorted(cases.values(), key=lambda c: (-c["ts"], c["question"]))
    built = [build(c, i + 1) for i, c in enumerate(ordered)]
    built.sort(key=lambda r: (-r["priority"], r["task_id"]))
    if args.limit > 0:
        built = built[:args.limit]
    for i, r in enumerate(built, 1):
        r["task_id"] = f"fc_{i:03d}"
        r["split"] = "holdout" if i % 4 == 0 else "train"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(emit_yaml(built), encoding="utf-8")

    layers = collections.Counter(r["suggested_layer"] for r in built)
    bad = [r for r in built if r["priority"] >= 4]
    print(f"导出 {len(built)} 条（去重前 {sum(c['repeat_count'] for c in cases.values())} 次提问，"
          f"共 {len(cases)} 个不同问题）")
    print("建议分层:", dict(layers))
    print(f"高优先坏样本(priority>=4): {len(bad)} 条")
    print("重复提问 top5:")
    for c in sorted(cases.values(), key=lambda x: -x["repeat_count"])[:5]:
        print(f"  {c['repeat_count']:>2}x  {c['question'][:48]}")
    print("输出:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
