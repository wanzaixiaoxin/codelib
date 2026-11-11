"""命令行入口：python -m app <command>，也可用 console 脚本。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from . import indexer, retrieval
from .bot import BotService
from .config import AppConfig
from .llm import LLMClient
from .store import Store


def _cfg(args) -> AppConfig:
    return AppConfig.load(args.config)


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _lan_ip() -> str:
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def cmd_serve(args) -> int:
    cfg_path = str(Path(args.config).expanduser().resolve())
    os.environ["CODELIB_CONFIG"] = cfg_path
    import uvicorn
    cfg = AppConfig.load(cfg_path)
    host = args.host or cfg.host
    port = args.port or cfg.port
    print("=" * 62)
    print(f"  codelib 代码知识工作台  只读={'是' if cfg.read_only else '否'}")
    print(f"  知识库：{', '.join(w.id for w in cfg.workspaces)}")
    print(f"  本机访问：http://127.0.0.1:{port}/")
    if host in ("0.0.0.0", "::", ""):
        print(f"  内网访问：http://{_lan_ip()}:{port}/")
    print("=" * 62, flush=True)
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
    return 0


def cmd_index(args) -> int:
    cfg = _cfg(args)
    store = Store(cfg.db_path)
    targets = [cfg.workspace(args.workspace)] if args.workspace else cfg.workspaces
    results = []
    for ws in targets:
        res = indexer.index_workspace(store, ws, full=args.full)
        retrieval.invalidate(ws.id)
        results.append(res)
    _print(results if len(results) > 1 else results[0])
    return 0


def cmd_ask(args) -> int:
    cfg = _cfg(args)
    store = Store(cfg.db_path)
    bot = BotService(cfg, store)
    ans = asyncio.run(bot.handle(args.workspace or "", args.question, args.session or "cli",
                                 args.deep))
    if args.json:
        _print(ans.to_dict())
    else:
        print(ans.text)
        if ans.citations:
            print("\n依据：")
            for c in ans.citations[:10]:
                print(f"- {c.label()}")
        saved = max(0, ans.full_corpus_tokens - ans.used_context_tokens)
        ratio = (saved / ans.full_corpus_tokens * 100) if ans.full_corpus_tokens else 0
        llm_tag = f"是({ans.llm_provider})" if ans.llm_used else "否"
        print(f"\n[用量] 上下文约 {ans.used_context_tokens} token / 全量约 {ans.full_corpus_tokens} token，"
              f"节省约 {ratio:.1f}%；LLM={llm_tag}；耗时 {ans.elapsed_ms}ms")
    return 0


def cmd_changes(args) -> int:
    cfg = _cfg(args)
    store = Store(cfg.db_path)
    targets = [cfg.workspace(args.workspace)] if args.workspace else cfg.workspaces
    _print({ws.id: indexer.compute_changes(store, ws).to_dict() for ws in targets})
    return 0


def cmd_status(args) -> int:
    cfg = _cfg(args)
    store = Store(cfg.db_path)
    data = cfg.public_dict()
    data["stats"] = {w.id: store.stats(w.id) for w in cfg.workspaces}
    _print(data)
    return 0


def cmd_remember(args) -> int:
    cfg = _cfg(args)
    store = Store(cfg.db_path)
    ws = cfg.workspace(args.workspace)
    fid = store.add_fact(ws.id, args.text, source="cli")
    print(f"已写入事实 #{fid} 到工作区 {ws.id}")
    return 0


def cmd_kb_build(args) -> int:
    cfg = _cfg(args)
    from .knowledge.build import build_workspace
    targets = [cfg.workspace(args.workspace)] if args.workspace else cfg.workspaces
    results = [build_workspace(cfg, ws, full=args.full) for ws in targets]
    _print(results if len(results) > 1 else results[0])
    return 0


def cmd_kb_build_all(args) -> int:
    cfg = _cfg(args)
    from .knowledge.build import build_workspace
    results = []
    for ws in cfg.workspaces:
        print(f"== 建库 {ws.id} ({ws.name}) ==", flush=True)
        results.append(build_workspace(cfg, ws, full=args.full))
    _print(results)
    return 0


def cmd_kb_stats(args) -> int:
    cfg = _cfg(args)
    from .knowledge import KnowledgeStore
    from .knowledge.build import ws_kb_id
    kb = KnowledgeStore(cfg.knowledge_db_path)
    _print({w.id: kb.counts(ws_kb_id(w.id, w.name)) for w in cfg.workspaces})
    return 0


def cmd_llm(args) -> int:
    cfg = _cfg(args)
    if args.test:
        client = LLMClient(cfg.llm)
        res = asyncio.run(client.ping())
        _print(res)
        return 0 if res.get("ok") else 2
    _print(cfg.llm.public_dict())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codelib", description="可配置的代码问答机器人服务")
    p.add_argument("--config", default=os.environ.get("CODELIB_CONFIG", "config/config.yaml"),
                   help="配置文件路径")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("serve", help="启动 HTTP 服务")
    sp.add_argument("--host", default=None)
    sp.add_argument("--port", type=int, default=None)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("index", help="建立/更新索引")
    sp.add_argument("--workspace", default=None)
    sp.add_argument("--full", action="store_true", help="全量重建")
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("ask", help="向代码库提问")
    sp.add_argument("question")
    sp.add_argument("--workspace", default=None)
    sp.add_argument("--session", default="cli")
    sp.add_argument("--deep", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("changes", help="查看目录变更")
    sp.add_argument("--workspace", default=None)
    sp.set_defaults(func=cmd_changes)

    sp = sub.add_parser("status", help="查看状态")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("remember", help="写入一条记忆事实")
    sp.add_argument("text")
    sp.add_argument("--workspace", default=None)
    sp.set_defaults(func=cmd_remember)

    sp = sub.add_parser("llm", help="查看/测试大模型配置")
    sp.add_argument("--test", action="store_true", help="连通性测试")
    sp.set_defaults(func=cmd_llm)

    sp = sub.add_parser("kb-build", help="构建代码知识库（tree-sitter + 文档 → FTS5）")
    sp.add_argument("--workspace", default=None)
    sp.add_argument("--full", action="store_true", help="全量重建")
    sp.set_defaults(func=cmd_kb_build)

    sp = sub.add_parser("kb-build-all", help="为所有工作区构建知识库")
    sp.add_argument("--full", action="store_true", help="全量重建")
    sp.set_defaults(func=cmd_kb_build_all)

    sp = sub.add_parser("kb-stats", help="查看代码知识库统计")
    sp.set_defaults(func=cmd_kb_stats)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
