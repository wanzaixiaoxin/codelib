"""对任意真实目录做一次只读索引 + 提问，用于评估 token 节省效果。

用法：
    python scripts/eval_real.py --path D:\\tdx\\zq\\tx_msg_svr --question "消息服务如何启动？"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app import indexer  # noqa: E402
from app.bot import BotService  # noqa: E402
from app.config import AppConfig, WorkspaceConfig  # noqa: E402
from app.store import Store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", required=True, help="要索引的工程目录")
    ap.add_argument("--question", default="这个工程的主要入口和核心逻辑在哪里？")
    ap.add_argument("--budget", type=int, default=4000,
                    help="全局检索上下文 token 预算（knowledge.retrieve.budget_tokens）")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="codelib_eval_"))
    ws = WorkspaceConfig(id="target", name=Path(args.path).name, path=args.path)
    cfg = AppConfig(data_dir=str(tmp), default_workspace="target",
                    workspaces=[ws], memory={"enabled": True},
                    knowledge={"retrieve": {"budget_tokens": args.budget}})
    store = Store(cfg.db_path)

    t0 = time.time()
    res = indexer.index_workspace(store, ws, full=True)
    idx_s = time.time() - t0
    print("== 索引结果 ==")
    print(json.dumps({k: res[k] for k in
                      ("scanned_files", "indexed", "failed", "chunks_written",
                       "chunk_tokens", "corpus_tokens", "token_saving_ratio")},
                     ensure_ascii=False, indent=2))
    print(f"索引耗时：{idx_s:.2f}s；索引体积约为全量的 {res['chunk_tokens']/max(1,res['corpus_tokens'])*100:.1f}%")

    bot = BotService(cfg, store)
    t1 = time.time()
    ans = asyncio.run(bot.handle("target", args.question, "eval"))
    print("\n== 提问 ==")
    print("Q:", args.question)
    print(ans.text[:800])
    print("\n== 引用（前 8）==")
    for c in ans.citations[:8]:
        print(" -", c.label())
    print("\n== 用量 ==")
    print(json.dumps(ans.to_dict()["usage"], ensure_ascii=False, indent=2))
    print(f"问答耗时：{(time.time()-t1)*1000:.0f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
