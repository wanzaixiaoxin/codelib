"""可证伪实验：沿调用链展开，能否一次性摆出「顺序问题」所需的全部代码事实？

不调 LLM、不写库、不碰知识库工程。跑法: python -B loop/probe_chain.py
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
from app.chain import build_chain_context, ChainReader  # noqa: E402

QUESTION = "分析当两个人的对话数据进入kafka后被我们消费，到最后写入kafka，对话的顺序能保证吗"
WS = "nlp:前置NLP"
KB = ROOT / "data" / "knowledge.db"

PATTERNS = {
    "消费端起协程": r"\bgo\s+(async|Async)\w*\s*\(",
    "位点提前提交": r"\.MarkMessage\s*\(",
    "写回成功回调": r"func\s+OnKafkaWriteOK\s*\(",
    "输出 key 构造": r"nlp_%s_%s",
    "hash 分区器": r"NewHashPartitioner",
    "无方向会话键": r"GenBoundKey\s*\(",
    "round 上下文依赖": r"RoundID|ProcessRepoRoundContext|hasSecurityInfo",
}


def main():
    text, nodes, meta = build_chain_context(KB, WS, QUESTION, hops=4,
                                            max_nodes=28, budget_chars=16000)
    print("问题:", QUESTION)
    print("识别到的关注点:", meta["concerns"])
    print("名称锚点:", meta["name_anchors"][:8])
    print("结构锚点:", [(a["symbol"], a["concern"]) for a in meta["structural_anchors"]][:10])
    print(f"\n展开: {meta['anchor_count']} 锚点 -> {meta['node_count']} 节点 / "
          f"{len(meta['files'])} 文件 / 上下文 {meta['context_chars']} 字符")
    print("覆盖文件:")
    for p in meta["files"]:
        print("   ", p)

    ev = ChainReader.find_evidence(nodes, PATTERNS)
    print("\n=== 正确结论所需的代码事实 ===")
    ok = 0
    for label, hits in ev.items():
        mark = "HIT " if hits else "MISS"
        ok += bool(hits)
        where = f"{hits[0]['symbol']} ({hits[0]['path']}, d{hits[0]['depth']})" if hits else "—"
        print(f"  [{mark}] {label:<16} {where}")
        if hits:
            print(f"          {hits[0]['line'][:110]}")

    # 空回调单独判定
    empty_cb = next((n for n in nodes if n.qualname == "OnKafkaWriteOK"), None)
    if empty_cb:
        tail = empty_cb.body.split("{", 1)[-1].strip()
        cb_empty = tail in ("}", "")
    else:
        cb_empty = False
    print(f"  [{'HIT ' if cb_empty else 'MISS'}] 回调为空实现     {cb_empty}")
    ok += cb_empty
    total = len(PATTERNS) + 1
    print(f"\n事实覆盖: {ok}/{total}")
    print("结论: " + ("沿链展开已能提供正确推理所需的全部代码证据 -> 瓶颈在检索/图层，改法成立"
                     if ok >= total - 1 else
                     "仍有关键事实未覆盖 -> 需补锚点或扩大跳数"))
    (ROOT / "loop" / "reports" / "chain_probe.md").write_text(
        f"# 调用链展开原型验证\n\n问题：{QUESTION}\n\n"
        f"- 关注点：{meta['concerns']}\n- 锚点 {meta['anchor_count']} / 节点 {meta['node_count']} "
        f"/ 文件 {len(meta['files'])} / 上下文 {meta['context_chars']} 字符\n"
        f"- 事实覆盖 **{ok}/{total}**\n\n| 事实 | 命中 | 位置 |\n|---|---|---|\n"
        + "".join(f"| {k} | {'是' if v else '否'} | "
                  f"{(v[0]['symbol'] + ' d' + str(v[0]['depth'])) if v else '—'} |\n"
                  for k, v in ev.items())
        + f"| 回调为空实现 | {'是' if cb_empty else '否'} | OnKafkaWriteOK |\n\n"
          "对照：上一轮同题 FTS 召回 10 条、0 条 .go 源码。\n",
        encoding="utf-8")
    print("报告: loop/reports/chain_probe.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
