"""自动评估标注：用知识库事实 + 客观留痕，独立判定，不抄系统自己的归因。

与 build_labeled.py 的区别：那版只给「候选」等人核对；这版自行给出结论 + 置信度。
证据来源全部独立于模型生成链路：
  - expected_paths        knowledge.db 的文件名 + doc_chunk/chunk 正文命中
  - expected_answer_points doc_chunk.section（章节标题）/ symbol.qualname
  - failure_layer         gate/coverage/missing 留痕 + 期望路径与要点命中率
  - 不使用当前答案生成 expected_answer_points，不使用 attributed_layer 生成 failure_layer

产出: loop/frozen/failure_cases.yaml（覆盖）, loop/frozen/baseline.json（重建）,
      loop/reports/auto_eval.md
用法: python -B loop/auto_evaluate.py
"""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
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
from export_failures import collect, norm, as_list  # noqa: E402

WS_KEYS = {"nlp": "nlp:前置NLP", "tdx_msg_bus_svr": "tdx_msg_bus_svr",
           "msg_gateway": "tdx_msg_bus_svr", "sample": "sample"}
SPLIT_RE = re.compile(
    r"如何|怎么|怎样|什么|为什么|哪些|请问|的|了|吗|呢|吧|啊|是|在|把|被|和|与|及|或|对|"
    r"从|向|为|这|那|个|请|问|有|一下|介绍|详细|当前|是否|可以|希望|回答|结果|关心|"
    r"包含|太多|机制|实现|逻辑|管理|处理|问题|代码|文档|系统|主要|一下|请|总")
STOP = {"工程", "项目", "知识库", "场景", "情况", "东西", "时候", "参数", "方法", "函数",
        "信息", "普通", "具体", "支持", "希望", "回答", "结果", "包含", "一下", "当前"}
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{2,}")
OVERVIEW_WORDS = ("做什么", "介绍一下", "主要解决", "是什么", "概览", "整体", "哪些渠道",
                  "你做什么", "这个工程")
OVERVIEW_FILES = ("README.md", "AGENTS.md", "BUSINESS.md")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def q(v):
    return json.dumps(str(v), ensure_ascii=False)


def arr(v):
    return "[" + ", ".join(q(x) for x in v) + "]"


def subject_terms(question: str):
    """把长中文问句切成有意义的主题词（不再用整串做词面匹配）。"""
    out = []
    for part in SPLIT_RE.split(question):
        part = part.strip()
        if 2 <= len(part) <= 10 and re.search(r"[\u4e00-\u9fff]", part) and part not in STOP:
            out.append(part)
    for ident in IDENT_RE.findall(question):
        if ident.lower() not in ("the", "and", "for"):
            out.append(ident)
    return list(dict.fromkeys(out))[:8]


def grams_of(terms):
    out = []
    for t in terms:
        if re.search(r"[\u4e00-\u9fff]", t):
            out.extend(t[i:i + 2] for i in range(len(t) - 1))
            out.append(t)
        else:
            out.append(t.lower())
    return list(dict.fromkeys(g for g in out if len(g) >= 2))


def doc_text(kb: sqlite3.Connection, ws_key: str, path: str, limit=1200):
    chunks = [r[0] for r in kb.execute(
        "SELECT section || ' ' || text FROM doc_chunk WHERE workspace=? AND path=? LIMIT 6",
        (ws_key, path))]
    if not chunks:
        chunks = [r[0] for r in kb.execute(
            "SELECT text FROM chunk WHERE workspace=? AND path=? LIMIT 4", (ws_key, path))]
    return " ".join(str(x or "") for x in chunks)[:limit].lower()


def path_score(kb, ws_key, path, grams):
    hay = path.lower() + " " + doc_text(kb, ws_key, path)
    return sum(1 for g in grams if g in hay)


def candidate_paths(kb, ws_key, question, grams, limit=4):
    """独立检索：按文件名 + 正文命中打分（不经过 BotService 检索链路）。"""
    scored = {}
    for (p,) in kb.execute("SELECT path FROM kb_file WHERE workspace=?", (ws_key,)):
        s = sum(1 for g in grams if g in p.lower()) * 2
        if s:
            s += path_score(kb, ws_key, p, grams)
        if s:
            scored[p] = s
    if any(w in question for w in OVERVIEW_WORDS):
        for (p,) in kb.execute("SELECT path FROM kb_file WHERE workspace=?", (ws_key,)):
            if p in OVERVIEW_FILES or any(w in p for w in ("概览", "介绍", "业务概览", "README")):
                scored[p] = scored.get(p, 0) + 3
    return [p for p, _ in sorted(scored.items(), key=lambda x: -x[1])[:limit]]


def answer_points(kb, ws_key, paths, limit=4):
    """从文档章节标题 / 符号名抽「该讲清的要点」，绝不抄当前答案。"""
    pts = []
    for p in paths:
        for (sec,) in kb.execute(
                "SELECT section FROM doc_chunk WHERE workspace=? AND path=? AND section!='' LIMIT 8",
                (ws_key, p)):
            s = str(sec or "").strip().lstrip("#").strip()
            if 3 <= len(s) <= 40 and s not in pts:
                pts.append(s)
        for (sym,) in kb.execute(
                "SELECT qualname FROM symbol WHERE workspace=? AND path=? LIMIT 4", (ws_key, p)):
            s = str(sym or "").strip()
            if s and s not in pts:
                pts.append(s)
        if len(pts) >= limit:
            break
    return pts[:limit]


HEADER = """# 自动评估标注（loop/auto_evaluate.py 生成，未经人工核对）
#
# 方法：期望路径/要点来自 knowledge.db 的文件名、正文与章节标题；
#       失败层由 gate/coverage/missing 留痕 + 期望路径与要点命中率独立判定。
# 不使用当前答案生成要点，不使用系统自己的 attributed_layer 回填 failure_layer。
#
# 重要：这是「独立证据的自动评估」，不是人工标注的 ground truth。
#       可用于 L1 改动的相对比较；auto_eval.md 里的两套判定分歧才是真信号，
#       不要拿本文件的 failure_layer 去算「归因一致率」当验收（那是自评）。
#
# 字段：label_confidence = high|medium|low（证据强度）
#       human_reviewed = false
#
"""


def judge(c, verified, points, terms):
    s, cross = c["stage"], c["cross_round"]
    ev, intent = s.get("evidence", {}) or {}, s.get("intent", {}) or {}
    ret, ans = s.get("retrieve", {}) or {}, s.get("answer", {}) or {}
    qr = s.get("quality_review", {}) or {}
    rq = (str(intent.get("retrieval_query", "") or "") + " " +
          str(intent.get("effective_question", "") or ""))
    cites = [x.get("path", "") for x in as_list(ret.get("citations"))
             if isinstance(x, dict) and x.get("path")]
    answer = str(ans.get("text", "") or "")
    try:
        coverage = float(ev.get("coverage", -1))
    except Exception:
        coverage = -1.0
    missing = [str(x) for x in as_list(ev.get("missing"))]
    unsupported = as_list(qr.get("unsupported"))

    ph = (sum(1 for p in verified
              if any(p in cp or cp in p for cp in cites)) / len(verified)) if verified else None
    ah = (sum(1 for pt in points if pt.lower() in answer.lower()) / len(points)) if points else None
    ih = (sum(1 for t in terms if t.lower() in rq.lower()) / len(terms)) if terms else None

    layers, why = set(), []
    if cross["gate_blocked_any"]:
        layers.add("retrieval"); why.append("证据门槛拦截")
    if 0 <= coverage < 0.4:
        layers.add("retrieval"); why.append(f"覆盖{coverage:.2f}")
    if any(re.search(r"[\u4e00-\u9fff]", m) for m in missing):
        layers.add("retrieval"); why.append("中文主题词未命中")
    if unsupported:
        layers.add("retrieval"); why.append(f"{len(unsupported)}处无证据支撑")
    if ph is not None and ph < 0.5:
        layers.add("retrieval"); why.append(f"期望路径命中{ph:.2f}")
    if ih is not None and ih < 0.5:
        layers.add("intent"); why.append(f"改写丢主题词{ih:.2f}")
    if ah is not None and ah < 0.5:
        layers.add("presentation"); why.append(f"要点命中{ah:.2f}")
    layer = "ok" if not layers else ("mixed" if len(layers) > 1 else next(iter(layers)))
    return layer, {"path_hit": ph, "point_hit": ah, "intent_hit": ih}, why


def main():
    import yaml
    labeled_path = HERE / "frozen" / "failure_cases.yaml"
    cases = yaml.safe_load(labeled_path.read_text(encoding="utf-8"))
    kb = sqlite3.connect(f"file:{(ROOT / 'data' / 'knowledge.db').as_posix()}?mode=ro", uri=True)
    hist = collect(ROOT / "loop" / "runs" / "l0_merged" / "codelib.db")
    sys_attr = {r["task_id"]: r["attributed_layer"] for r in
                json.loads((HERE / "reports" / "l0_full.json").read_text(encoding="utf-8"))["rows"]}

    rows, conf, layers_cnt, disagreements = [], Counter(), Counter(), []
    for case in cases:
        question, ws = case["question"], case.get("workspace", "")
        key = WS_KEYS.get(ws, ws)
        terms = subject_terms(question)
        grams = grams_of(terms)
        prev = [str(x) for x in (case.get("expected_paths") or [])]
        audit_backed = "audit" in str(case.get("label_source") or "")
        cands = candidate_paths(kb, key, question, grams) if key else []
        pool = list(dict.fromkeys(prev + cands))
        verified = [p for p in pool if path_score(kb, key, p, grams) >= 1][:4]
        if audit_backed:  # 评审点名的路径是强证据，即使正文打分低也保留
            for p in prev:
                if p not in verified:
                    verified.append(p)
        verified = verified[:4]
        points = answer_points(kb, key, verified) if key else []
        symbols = []
        for p in verified:
            for (sym,) in kb.execute(
                    "SELECT qualname FROM symbol WHERE workspace=? AND path=? LIMIT 3", (key, p)):
                if sym and sym not in symbols:
                    symbols.append(sym)
        c = hist.get(norm(question)) or {
            "stage": {}, "cross_round": {"trace_count": 0, "gate_blocked_any": False,
                                         "revised_any": False, "llm_fail_any": False},
            "audit": None, "tids": [], "repeat_count": 0, "sources": {}, "rep_trace": ""}
        layer, hits, why = judge(c, verified, points, terms)
        strength = (2 if audit_backed else 0) + (1 if points else 0) + (1 if verified else 0)
        confidence = "high" if strength >= 3 else ("medium" if strength >= 2 else "low")
        conf[confidence] += 1
        layers_cnt[layer] += 1
        if sys_attr.get(case["task_id"]) != layer:
            disagreements.append((case["task_id"], question, layer, sys_attr.get(case["task_id"])))
        rows.append({
            "task_id": case["task_id"], "question": question, "workspace": ws,
            "sources": case.get("sources", {}), "signals": case.get("signals", {}),
            "split": case.get("split", "train"), "priority": case.get("priority", 0),
            "expected_intent_terms": terms, "expected_paths": verified,
            "expected_symbols": symbols[:6], "expected_answer_points": points,
            "failure_layer": layer, "label_source": "auto_eval",
            "label_confidence": confidence, "human_reviewed": False,
            "label_basis": {k: (None if v is None else round(v, 3)) for k, v in hits.items()},
            "notes": "; ".join(why) or "无明确失败信号",
        })
    kb.close()
    labeled_path.write_text(HEADER + yaml.safe_dump(rows, allow_unicode=True,
                                                   sort_keys=False), encoding="utf-8")

    # 重建基线（代理指标的 l0_full 仍是改动前的对照锚点）
    full = json.loads((HERE / "reports" / "l0_full.json").read_text(encoding="utf-8"))
    frozen_files = sorted(p for p in (HERE / "frozen").glob("*")
                          if p.is_file() and p.name != "baseline.json")
    frozen_hash = hashlib.sha256(
        "".join(f"{p.name}:{sha(p)}\n" for p in frozen_files).encode("utf-8")).hexdigest()
    baseline = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "label_method": "auto_eval (非人工标注)",
        "source_report": "loop/reports/l0_full.json",
        "report_sha256": full["sha"], "cases_sha256": sha(labeled_path),
        "runner_sha256": sha(HERE / "quality_runner.py"), "frozen_sha256": frozen_hash,
        "metrics": full["summary"],
        "per_case": {r["task_id"]: {"intent": r["intent_score"],
                                    "retrieval": r["retrieval_score"],
                                    "presentation": r["presentation_score"],
                                    "layer": r["attributed_layer"]}
                     for r in full["rows"]},
        "notes": ["代理指标基线，仅作 L1 改动前的对照锚点。",
                  "标注已由 auto_evaluate.py 自行评估，未经人工核对。",
                  "L1 判定须多次采样取中位数，不可用单次分数做接受/回滚。"],
    }
    (HERE / "frozen" / "baseline.json").write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    L = ["# 自动评估标注报告", "", f"生成时间：{baseline['generated_at']}", "",
         f"- 题目 {len(rows)}｜`expected_paths` 非空 {sum(1 for r in rows if r['expected_paths'])}"
         f"｜`expected_answer_points` 非空 {sum(1 for r in rows if r['expected_answer_points'])}",
         f"- `failure_layer` 分布：{dict(layers_cnt)}",
         f"- 置信度：{dict(conf)}", "",
         "## 与系统代理归因的分歧", "",
         f"两套判定不一致 {len(disagreements)}/{len(rows)} 题。"
         "这不是「谁对谁错」：系统归因看的是 coverage/missing 等代理信号，"
         "本评估看的是期望文件与要点是否真被召回/讲清。分歧题就是最该优先看的地方。", "",
         "| task | 问题 | auto_eval | 系统代理 |", "|---|---|---|---|"]
    for tid, ques, a, b in disagreements:
        L.append(f"| {tid} | {ques[:26]} | `{a}` | `{b}` |")
    L += ["", "## 自评风险声明", "",
          "failure_layer 由独立证据判定，未抄 attributed_layer；属自动评估，非人工 ground truth。",
          "期望路径与要点来自 knowledge.db 事实，从不取自当前召回或答案文本。",
          "用途：L1 改动的相对比较；不可拿本层算归因一致率当验收。", ""]
    (HERE / "reports" / "auto_eval.md").write_text(chr(10).join(L), encoding="utf-8")
    print(f"自动评估 {len(rows)} 题 | failure_layer {dict(layers_cnt)} | 置信度 {dict(conf)}")
    print(f"与系统归因分歧 {len(disagreements)} 题 -> loop/reports/auto_eval.md")
    print("基线已重建, frozen_sha256:", frozen_hash[:16])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
