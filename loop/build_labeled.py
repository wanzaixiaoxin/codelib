"""从草稿生成「预标注题集」+ L1 基线。

只自动填客观可证的字段：
  - expected_intent_terms : 问题里的中文主题词 / ASCII 标识符（词面）
  - expected_symbols      : 问题里的标识符 + knowledge.db!symbol 的静态命中
  - expected_paths        : ① audit.harness_improvements 里点名的文件（强证据）
                            ② symbol 表按 workspace + 术语静态匹配到的文件（候选）
绝不把「当前系统实际召回的 citations」当期望值（那是自证循环）。
主观字段 expected_answer_points / failure_layer 一律留空，必须人工判断。

用法: python -B loop/build_labeled.py
产出: loop/frozen/failure_cases.yaml, loop/frozen/baseline.json,
      loop/reports/auto_label.md
"""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
from export_failures import collect, norm  # noqa: E402

WS_KEYS = {"nlp": "nlp:前置NLP", "tdx_msg_bus_svr": "tdx_msg_bus_svr",
           "msg_gateway": "tdx_msg_bus_svr", "sample": "sample"}
PATH_RE = re.compile(
    r"(?:docs|src|app|tests|scripts|config)/[^\s，。；：、）)\]】]+\.(?:md|py|go|ts|js|java|yaml|yml|json|txt)")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{2,}")
ZH_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
STOP_ZH = {"怎么", "如何", "哪些", "什么", "为什么", "当前", "介绍", "一下", "详细",
           "这个", "那个", "是否", "支持", "主要", "问题", "工程", "机制", "实现",
           "逻辑", "代码", "文档", "管理", "处理", "区别", "一致", "入口", "哪些",
           "希望", "回答", "结果", "关心", "包含", "太多"}
STOP_GRAMS = {"机制", "实现", "逻辑", "管理", "处理", "问题", "工程", "代码", "文档",
              "系统", "如何", "怎么", "哪些", "什么", "当前", "介绍", "一下", "详细",
              "主要", "支持", "区别", "一致", "入口", "回答", "结果", "包含", "业务",
              "信息", "数据", "方式", "情况", "可以", "是否", "一个", "这个", "那个"}


def informative_grams(terms):
    """中文词拆 2-gram（"券补全机制"->"券补"/"补全"），英文标识符整词保留。"""
    out = []
    for t in terms:
        if any("一" <= ch <= "鿿" for ch in t):
            for i in range(len(t) - 1):
                g = t[i:i + 2]
                if g not in STOP_GRAMS:
                    out.append(g)
        else:
            out.append(t.lower())
    return list(dict.fromkeys(out))


def terms_of(question: str):
    zh = [t for t in ZH_RE.findall(question) if t not in STOP_ZH]
    ident = [t for t in IDENT_RE.findall(question) if t.lower() not in ("the", "and")]
    return list(dict.fromkeys(zh + ident))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_paths(db: Path):
    """按问题归一化收集评审点名的期望文件。"""
    out = {}
    for c in collect(db).values():
        au = c.get("audit") or {}
        paths = []
        for imp in (au.get("improvements") or []):
            paths.extend(PATH_RE.findall(str(imp)))
        if paths:
            out[norm(c["question"])] = list(dict.fromkeys(paths))
    return out


def static_candidates(kb: Path, question: str, workspace: str, limit=3):
    """独立于问答链路：直接查 symbol 表，按术语命中给候选文件。"""
    key = WS_KEYS.get(workspace)
    if not key:
        return [], []
    terms = [t.lower() for t in terms_of(question) if len(t) >= 2]
    if not terms:
        return [], []
    con = sqlite3.connect(f"file:{kb.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    scored, syms = {}, []
    for r in con.execute("SELECT qualname,path,signature,doc FROM symbol WHERE workspace=?", (key,)):
        qn = str(r["qualname"] or "").lower()
        doc = str(r["doc"] or "").lower()
        sig = str(r["signature"] or "").lower()
        hay = qn + " " + sig + " " + doc
        hits = sum(1 for t in terms if t in hay)
        if not hits:
            continue
        p = str(r["path"] or "")
        scored[p] = scored.get(p, 0) + hits
        if any(t in qn for t in terms) and r["qualname"] not in syms:
            syms.append(r["qualname"])
    # 文档/源码文件名匹配：中文业务词往往不出现在英文符号名里，
    # 但会出现在中文文档文件名中（如「券补全」-> 券信息补全与校验逻辑.md）。
    grams = informative_grams(terms_of(question))
    for r in con.execute("SELECT path FROM kb_file WHERE workspace=?", (key,)):
        path = str(r["path"] or "")
        pl = path.lower()
        hit = sum(1 for g in grams if g in pl)
        if hit:
            scored[path] = scored.get(path, 0) + hit * 2
    con.close()
    paths = [p for p, _ in sorted(scored.items(), key=lambda x: -x[1])[:limit]]
    return paths, syms[:4]


def q(v):
    return json.dumps(str(v), ensure_ascii=False)


def arr(v):
    return "[" + ", ".join(q(x) for x in v) + "]"


HEADER = """# 预标注题集 —— loop/build_labeled.py 生成，仍需人工核对
#
# 自动填的（客观可证，但仍建议扫一眼）：
#   expected_intent_terms  问题里的中文主题词 / 标识符（词面抽取）
#   expected_paths         ① 评审 harness_improvements 点名的文件（强证据）
#                          ② knowledge.db!symbol 按术语静态匹配到的文件（候选，需核对）
#   expected_symbols       问题里的标识符 + symbol 表静态命中
#   notes                  标注每个期望路径的来源
#
# 必须人工填（主观，脚本绝不代填，否则就是自己评自己）：
#   expected_answer_points 一个满意答案必须讲清的要点
#   failure_layer          intent | retrieval | presentation | mixed | ok
#
# 绝不使用「当前系统实际召回的 citations」当期望值 —— 那会让指标永远满分。
# 标完可直接重跑：
#   python -B loop/quality_runner.py --cases loop/frozen/failure_cases.yaml
# 同一脚本会自动切到标注真判据，并可用 failure_layer 算归因一致率（L0 验收 ≥70%）。
#
"""


def main():
    import yaml
    draft_path = HERE / "failure_cases.draft.yaml"
    draft = draft_path.read_text(encoding="utf-8")
    cases = [c for c in (yaml.safe_load(draft) or []) if isinstance(c, dict)]
    ap = audit_paths(ROOT / "data" / "codelib.db")
    kb = ROOT / "data" / "knowledge.db"

    fills, stats = {}, {"audit": 0, "static": 0, "none": 0}
    for c in cases:
        question, ws = c["question"], c.get("workspace", "")
        audit_p = ap.get(norm(question), [])
        static_p, static_s = static_candidates(kb, question, ws)
        paths = list(dict.fromkeys(audit_p + static_p))
        ident = [t for t in IDENT_RE.findall(question) if t.lower() not in ("the", "and")]
        syms = list(dict.fromkeys(ident + static_s))[:6]
        if audit_p and static_p:
            src = "audit点名 + 静态符号匹配(候选)"
        elif audit_p:
            src = "audit评审点名"
        elif static_p:
            src = "静态符号匹配(候选,需核对)"
        else:
            src = "无客观证据,需人工填写"
        stats["audit" if audit_p else ("static" if static_p else "none")] += 1
        fills[c["task_id"]] = {
            "intent": terms_of(question)[:6], "paths": paths[:6],
            "symbols": syms, "src": src,
        }

    blocks = re.split(r"(?=\n- task_id: )", draft)
    out = [HEADER]
    for b in blocks[1:]:
        m = re.search(r'- task_id: "([^"]+)"', b)
        if not m or m.group(1) not in fills:
            out.append(b)
            continue
        f = fills[m.group(1)]
        b = re.sub(r"  expected_intent_terms: \[\]",
                   f"  expected_intent_terms: {arr(f['intent'])}", b)
        b = re.sub(r"  expected_paths: \[\]", f"  expected_paths: {arr(f['paths'])}", b)
        b = re.sub(r"  expected_symbols: \[\]", f"  expected_symbols: {arr(f['symbols'])}", b)
        b = re.sub(r"  expected_answer_points: \[\]",
                   "  # ↓ 主观字段，必须人工填 ↓\n  expected_answer_points: []", b)
        b = re.sub(r'  failure_layer: ""',
                   '  failure_layer: ""   # intent|retrieval|presentation|mixed|ok', b)
        src_line = "  label_source: " + q(f["src"]) + chr(10) + "  notes: " + chr(34) + chr(34)
        b = re.sub(r'  notes: ""', lambda _m, s=src_line: s, b)
        out.append(b)
    labeled_path = HERE / "frozen" / "failure_cases.yaml"
    labeled_path.write_text("".join(out), encoding="utf-8")

    # L1 基线：当前版本在 24 题上的指标 + 各工件 hash
    full = json.loads((HERE / "reports" / "l0_full.json").read_text(encoding="utf-8"))
    frozen_files = sorted(p for p in (HERE / "frozen").glob("*")
                          if p.is_file() and p.name != "baseline.json")
    frozen_hash = hashlib.sha256(
        "".join(f"{p.name}:{sha(p)}\n" for p in frozen_files).encode("utf-8")).hexdigest()
    baseline = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_report": "loop/reports/l0_full.json",
        "report_sha256": full["sha"],
        "cases_sha256": sha(labeled_path),
        "runner_sha256": sha(HERE / "quality_runner.py"),
        "frozen_sha256": frozen_hash,
        "metrics": full["summary"],
        "per_case": {r["task_id"]: {"intent": r["intent_score"],
                                    "retrieval": r["retrieval_score"],
                                    "presentation": r["presentation_score"],
                                    "layer": r["attributed_layer"]}
                     for r in full["rows"]},
        "notes": ["代理指标基线（题集尚未人工标注），只作 L1 改动前的对照锚点。",
                  "标好 failure_cases.yaml 后请重跑 runner 并重建基线。",
                  "L1 判定须多次采样取中位数，不可用单次分数做接受/回滚。"],
    }
    (HERE / "frozen" / "baseline.json").write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = ["# 自动预标注报告", "",
             f"生成时间：{baseline['generated_at']}", "",
             f"- 题目：{len(cases)}",
             f"- `expected_paths` 有 audit 点名证据：{stats['audit']} 题",
             f"- 仅静态符号匹配候选：{stats['static']} 题（**需人工核对**）",
             f"- 无客观证据、留空待填：{stats['none']} 题", "",
             "## 为什么两个字段不自动填", "",
             "- `failure_layer`：拿系统自己的 `attributed_layer` 回填，再算「归因一致率」就是自评，"
             "必然 100%，毫无意义。这是 L0 验收的唯一主观输入。",
             "- `expected_answer_points`：什么叫「满意答案」是产品判断，"
             "用模型生成或抄当前答案都会造成确认偏差。", "",
             "## 数据边界", "",
             "- 期望路径**从不**取自当前问答实际召回的 citations。",
             "- audit 路径来自历史评审的 `harness_improvements`（评审当时点名「应补充召回 X」）。",
             "- 静态匹配直接查 `knowledge.db!symbol`，不经过 BotService 检索链路。", "",
             "## 下一步", "",
             "1. 核对 `expected_paths`（尤其标注「静态符号匹配(候选)」的）。",
             "2. 填 `expected_answer_points` 与 `failure_layer`。",
             "3. `python -B loop/quality_runner.py --cases loop/frozen/failure_cases.yaml` 切到真判据。",
             "4. 用 `failure_layer` vs `attributed_layer` 算归因一致率（≥70% 才算 L0 通过）。", ""]
    (HERE / "reports" / "auto_label.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"预标注 {len(cases)} 题 -> {labeled_path}")
    print("  expected_paths 来源:", stats)
    print("  基线:", HERE / "frozen" / "baseline.json", "| frozen_sha256:", frozen_hash[:16])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
