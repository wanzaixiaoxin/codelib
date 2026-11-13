"""L1 策略闭环（检索层 A/B）：lexicon 开/关对 expected_paths 命中率的影响。

只测召回，不调 LLM（rerank.mode=none），确定性、秒级、可重复。
两轮重复跑验证确定性；只有平均命中率提升 >= 0.02 且提升题数 >= 下降题数才落地，
否则不写 data/lexicon.db（即回滚）。

用法: python -B loop/eval_lexicon.py
"""
from __future__ import annotations
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.config import AppConfig, IndexConfig, WorkspaceConfig  # noqa: E402
from app.knowledge.schema import KnowledgeStore  # noqa: E402
from app.knowledge.query import build_context  # noqa: E402
import app.lexicon as lexmod  # noqa: E402

TMP = ROOT / "loop" / "runs" / "lexicon_ab"
CANDIDATE = ROOT / "loop" / "runs" / "lexicon_candidate.db"
TARGET = ROOT / "data" / "lexicon.db"

WS_SPEC = {
    "nlp": ("nlp", "前置NLP", ROOT.parent / "code" / "pri_nlp_worker_svr"),
    "tdx_msg_bus_svr": ("tdx_msg_bus_svr", "", ROOT.parent / "code" / "tdx_msg_bus_svr"),
    "msg_gateway": ("tdx_msg_bus_svr", "", ROOT.parent / "code" / "tdx_msg_bus_svr"),
    "sample": ("sample", "", ROOT / "sample_workspace"),
}


def cpath(c):
    if isinstance(c, dict):
        return str(c.get("path", "") or "")
    return str(getattr(c, "path", "") or "")


def prepare():
    TMP.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "data" / "knowledge.db", TMP / "knowledge.db")
    base = AppConfig.load(str(ROOT / "config" / "config.yaml"))
    knowledge = dict(base.knowledge or {})
    knowledge.pop("db", None)
    knowledge["rerank"] = {"mode": "none", "top_n": 10}
    ws_objects = [WorkspaceConfig(id=v[0], name=v[1], path=str(v[2]),
                                  index=IndexConfig(auto_reindex_on_query=False))
                  for v in WS_SPEC.values()]
    cfg = AppConfig(data_dir=str(TMP), read_only=True, workspaces=ws_objects,
                    llm=base.llm, memory=base.memory, knowledge=knowledge,
                    auth=base.auth, default_workspace="nlp")
    kb = KnowledgeStore(cfg.knowledge_db_path)
    return cfg, kb


def ws_for(workspace):
    v = WS_SPEC.get(workspace)
    if not v:
        return None
    return WorkspaceConfig(id=v[0], name=v[1], path=str(v[2]),
                           index=IndexConfig(auto_reindex_on_query=False))


def hit_rate(expected, cites):
    if not expected:
        return None
    return sum(1 for p in expected
               if any(str(p) in cp or cp in str(p) for cp in cites)) / len(expected)


async def _ask(cfg, kb, ws, question):
    _ctx, cites, _n = await build_context(kb, cfg, ws, question, budget_tokens=4000,
                                          top_k=40, llm=None, deep=False)
    return [cpath(c) for c in (cites or [])]


def measure(cfg, kb, cases, lex_path, label):
    os.environ["CODELIB_LEXICON_DB"] = str(lex_path)
    lexmod.reset_default()
    size = len(lexmod.default_store())
    out = {}
    for c in cases:
        ws = ws_for(c["workspace"])
        if ws is None:
            continue
        try:
            cites = asyncio.run(_ask(cfg, kb, ws, c["question"]))
            out[c["task_id"]] = {
                "hit": hit_rate([str(x) for x in (c.get("expected_paths") or [])], cites),
                "n_cites": len(cites), "cites": cites[:5],
            }
        except Exception as exc:
            out[c["task_id"]] = {"hit": None, "error": f"{type(exc).__name__}: {exc}"[:120]}
    print(f"  [{label}] lexicon={size} 条, 完成 {len(out)} 题")
    return out, size


def mean(results):
    vals = [r["hit"] for r in results.values() if r.get("hit") is not None]
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def main():
    import yaml
    cases = [c for c in yaml.safe_load(
        (HERE / "frozen" / "failure_cases.yaml").read_text(encoding="utf-8"))
        if c.get("expected_paths")]
    cfg, kb = prepare()
    empty = TMP / "empty_lexicon.db"
    if empty.exists():
        empty.unlink()
    print(f"A/B 题目 {len(cases)} 道（均有 expected_paths）")
    A1, s_empty = measure(cfg, kb, cases, empty, "A1 空词表")
    B1, s_full = measure(cfg, kb, cases, CANDIDATE, "B1 候选词表")
    A2, _ = measure(cfg, kb, cases, empty, "A2 空词表(重复)")
    B2, _ = measure(cfg, kb, cases, CANDIDATE, "B2 候选词表(重复)")
    deterministic = (all(A1[k] == A2.get(k) for k in A1)
                     and all(B1[k] == B2.get(k) for k in B1))

    a, b = mean(A1), mean(B1)
    changed = []
    for k in A1:
        x, y = A1[k].get("hit"), B1.get(k, {}).get("hit")
        if x is None or y is None:
            continue
        if abs(y - x) > 1e-9:
            changed.append((k, x, y))
    improved = [t for t in changed if t[2] > t[1]]
    degraded = [t for t in changed if t[2] < t[1]]
    accept = (b - a >= 0.02) and len(improved) >= len(degraded) and deterministic
    if accept:
        shutil.copy2(CANDIDATE, TARGET)
        action = f"已落地 -> {TARGET}（服务按 mtime 热读生效）"
    else:
        if TARGET.exists():
            TARGET.unlink()
        action = "未落地（回滚，默认词表保持为空）"

    L = ["# L1 策略闭环 A/B：术语映射（只测召回）", "",
         f"- 题目 {len(cases)} 道（均有 `expected_paths`）｜词表 {s_full} 条 / 空表 {s_empty} 条",
         f"- 空表平均命中率 **{a:.4f}** → 候选词表 **{b:.4f}**（Δ {b - a:+.4f}）",
         f"- 提升 {len(improved)} 题 / 下降 {len(degraded)} 题 / 无变化 {len(cases) - len(changed)} 题",
         f"- 两轮重复确定性：{'PASS' if deterministic else 'FAIL'}",
         f"- 判定：**{'接受' if accept else '不接受'}**；{action}", "",
         "## 逐题变化", "", "| task | 空表 | 候选 | Δ |", "|---|---|---|---|"]
    for k, x, y in changed:
        L.append(f"| {k} | {x:.2f} | {y:.2f} | {y - x:+.2f} |")
    if not changed:
        L.append("| （无变化） | — | — | — |")
    L += ["", "## 边界", "",
          "- 只测检索召回（`rerank.mode=none`、不调 LLM），不等同于答案质量。",
          "- 命中率 = 期望路径出现在 citations 中的比例，是自动评估标注的相对指标。",
          "- 落地后服务按文件 mtime 热读；当前已运行的进程需重启才会加载新代码。",
          "- 正式 L1 接受/回滚仍须在线复测并多次采样取中位数。", ""]
    (HERE / "reports" / "lexicon_ab.md").write_text("\n".join(L), encoding="utf-8")
    print(f"\n空表 {a:.4f} -> 候选 {b:.4f} (Δ{b - a:+.4f}) | 提升{len(improved)} 下降{len(degraded)}")
    print("确定性:", "PASS" if deterministic else "FAIL", "| 判定:", "接受" if accept else "不接受")
    print(action)
    print("报告: loop/reports/lexicon_ab.md")
    os.environ.pop("CODELIB_LEXICON_DB", None)
    try:
        shutil.rmtree(TMP)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
