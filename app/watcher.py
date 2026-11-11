"""后台变更巡检：按 workspace 配置的 watch_interval_seconds 周期比对并增量重建。"""
from __future__ import annotations

import asyncio
import logging

from . import indexer, retrieval
from .config import AppConfig
from .store import Store

log = logging.getLogger("codelib.watcher")


async def watch_loop(cfg: AppConfig, store: Store) -> None:
    intervals = [w.index.watch_interval_seconds for w in cfg.workspaces
                 if w.index.watch_interval_seconds > 0]
    if not intervals:
        log.info("未启用后台巡检（watch_interval_seconds 均为 0）")
        return
    interval = max(2, min(intervals))
    log.info("后台变更巡检已启动，每 %s 秒扫描一次", interval)
    while True:
        await asyncio.sleep(interval)
        for ws in cfg.workspaces:
            if ws.index.watch_interval_seconds <= 0:
                continue
            try:
                ch = indexer.compute_changes(store, ws)
                if ch.has_changes:
                    res = indexer.index_workspace(store, ws, full=False)
                    retrieval.invalidate(ws.id)
                    if cfg.knowledge_enabled:
                        from .knowledge.build import build_workspace
                        build_workspace(cfg, ws, full=False)
                    log.info("工作区 %s 变更已增量重建：+%d ~%d -%d",
                             ws.id, res["added"], res["modified"], res["deleted"])
            except Exception:
                log.exception("工作区 %s 巡检失败", ws.id)
