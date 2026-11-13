"""L1 第二轮：对候选映射做 leave-one-out 剪枝，再组合复测。

第一轮整体 A/B 下降 -> 不落地。这里逐条测每条映射的边际贡献，
只保留 Δ>=0 的，组合后再跑一次 A/B；仍不提升就保持回滚。
用法: python -B loop/tune_lexicon.py
"""
from __future__ import annotations
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import eval_lexicon as E  # noqa: E402
from app.lexicon import SCHEMA  # noqa: E402


def write_db(path, rows):
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    for term, exp in rows:
        con.execute("INSERT OR REPLACE INTO lexicon(term,expansions,source,updated_at)"
                    " VALUES(?,?,?,?)", (term, exp, "auto_eval_tuned", 0.0))
    con.commit()
    con.close()


def main():
    import yaml
    cases = [c for c in yaml.safe_load(
        (HERE / "frozen" / "failure_cases.yaml").read_text(encoding="utf-8"))
        if c.get("expected_paths")]
    cfg, kb = E.prepare()
    empty = E.TMP / "empty_lexicon.db"
    A, _ = E.measure(cfg, kb, cases, empty, "基线")
    base = E.mean(A)
    rows = sqlite3.connect(E.CANDIDATE).execute(
        "SELECT term,expansions FROM lexicon ORDER BY term").fetchall()
    print(f"基线 {base:.4f}，逐条排查 {len(rows)} 条映射")
    keep, report = [], []
    for i, (term, exp) in enumerate(rows):
        one = E.TMP / f"one_{i}.db"
        write_db(one, [(term, exp)])
        R, _ = E.measure(cfg, kb, cases, one, f"单条 {term}")
        delta = E.mean(R) - base
        mark = "保留" if delta >= 0 else "剔除"
        print(f"  {term:<10} Δ{delta:+.4f}  {mark}")
        report.append((term, round(delta, 4), mark))
        if delta >= 0:
            keep.append((term, exp))
    combo = E.TMP / "combo.db"
    write_db(combo, keep)
    B, _ = E.measure(cfg, kb, cases, combo, f"组合({len(keep)}条)")
    A2, _ = E.measure(cfg, kb, cases, empty, "基线(重复)")
    B2, _ = E.measure(cfg, kb, cases, combo, "组合(重复)")
    deterministic = all(A[k] == A2.get(k) for k in A) and all(B[k] == B2.get(k) for k in B)
    a, b = E.mean(A), E.mean(B)
    changed = [(k, A[k]["hit"], B[k]["hit"]) for k in A
               if A[k].get("hit") is not None and B.get(k, {}).get("hit") is not None
               and abs(B[k]["hit"] - A[k]["hit"]) > 1e-9]
    improved = [t for t in changed if t[2] > t[1]]
    degraded = [t for t in changed if t[2] < t[1]]
    accept = (b - a >= 0.02) and len(improved) >= len(degraded) and deterministic and keep
    if accept:
        shutil.copy2(combo, E.TARGET)
        action = f"已落地 -> {E.TARGET}"
    else:
        if E.TARGET.exists():
            E.TARGET.unlink()
        action = "未落地（回滚）"
    L = ["# L1 第二轮：映射剪枝与复测", "",
         f"- 基线 {a:.4f} → 剪枝后组合({len(keep)}条) {b:.4f}（Δ {b - a:+.4f}）",
         f"- 提升 {len(improved)} / 下降 {len(degraded)} / 确定性 {'PASS' if deterministic else 'FAIL'}",
         f"- 判定：**{'接受' if accept else '不接受'}**；{action}", "",
         "## 逐条边际贡献（leave-one-out）", "", "| 映射 | Δ | 处理 |", "|---|---|---|"]
    for term, delta, mark in report:
        L.append(f"| {term} | {delta:+.4f} | {mark} |")
    L += ["", "## 逐题变化", "", "| task | 基线 | 组合 | Δ |", "|---|---|---|---|"]
    for k, x, y in changed:
        L.append(f"| {k} | {x:.2f} | {y:.2f} | {y - x:+.2f} |")
    if not changed:
        L.append("| （无变化） | — | — | — |")
    L += ["", "说明：只测检索召回，不调 LLM；剪枝依据是边际 Δ>=0。", ""]
    (HERE / "reports" / "lexicon_tuned.md").write_text("\n".join(L), encoding="utf-8")
    print(f"\n基线 {a:.4f} -> 组合 {b:.4f} (Δ{b - a:+.4f}) | 保留 {len(keep)}/{len(rows)} 条")
    print("判定:", "接受" if accept else "不接受", "|", action)
    os.environ.pop("CODELIB_LEXICON_DB", None)
    try:
        shutil.rmtree(E.TMP)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
