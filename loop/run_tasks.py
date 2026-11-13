"""在线补跑题集：用临时库副本真实调用问答链路，产出新的 trace 供 L0 归因。

不碰主库、不碰知识库工程、不需要 admin 登录：
  - 复制 data/codelib.db + data/knowledge.db 到独立 run 目录（副本可写）
  - 清空副本里的历史留痕/记忆表，让新 trace 干净可归因
  - 复用已建好的索引（auto_reindex_on_query=False），零重建
  - 直接调 BotService.handle，绕过 HTTP 鉴权

用法:
    python -B loop/run_tasks.py --only-missing --limit 3 --dry-run
    python -B loop/run_tasks.py --only-missing --limit 3
    python -B loop/run_tasks.py --only-missing --limit 0
    python -B loop/run_tasks.py --ids fc_003,fc_006
"""
from __future__ import annotations
import argparse
import asyncio
import json
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.config import AppConfig, IndexConfig, WorkspaceConfig  # noqa: E402
from app.store import Store  # noqa: E402
from app.bot import BotService  # noqa: E402

CLEAR_TABLES = ("trace_event", "trace", "audit", "conversations", "qa_memory",
                "facts", "chat_sessions", "auth_sessions")


def load_cases(path: Path):
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [d for d in data if isinstance(d, dict) and d.get("question")]


def workspace_map(main_db: Path):
    """从 managed_workspaces 读真实工程路径；缺的按同级 code/<id> 补。"""
    con = sqlite3.connect(f"file:{main_db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = {}
    for r in con.execute("SELECT id,name,path FROM managed_workspaces"):
        out[r["id"]] = {"name": r["name"] or "", "path": r["path"] or ""}
    con.close()
    for key in ("tdx_msg_bus_svr", "nlp"):
        guess = ROOT.parent / "code" / key
        if key not in out or not Path(out[key]["path"]).is_dir():
            if guess.is_dir():
                out[key] = {"name": out.get(key, {}).get("name", ""), "path": str(guess)}
    # 示例库：config.yaml 里 gateway 指向它，knowledge.db 的键为 "sample"（7 个文件）
    sample_dir = ROOT / "sample_workspace"
    if sample_dir.is_dir():
        out.setdefault("sample", {"name": "", "path": str(sample_dir)})
    return out


def prepare(out: Path, main_db: Path, main_kb: Path):
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(main_db, out / "codelib.db")
    shutil.copy2(main_kb, out / "knowledge.db")
    con = sqlite3.connect(out / "codelib.db")
    cleared = []
    for t in CLEAR_TABLES:
        try:
            con.execute(f"DELETE FROM {t}")
            cleared.append(t)
        except sqlite3.OperationalError:
            pass
    con.commit()
    con.close()
    # 副本里的索引数据是已建好的，但 meta 可能只标了部分 workspace；
    # 补上 index_format 标记，避免 _ensure_index 误判为「需要全量重建」。
    from app.knowledge.build import INDEX_FORMAT_VERSION
    kb = sqlite3.connect(out / "knowledge.db")
    keys = [r[0] for r in kb.execute("SELECT DISTINCT workspace FROM chunk")]
    for k in keys:
        kb.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                   (f"index_format:{k}", str(INDEX_FORMAT_VERSION)))
    kb.commit()
    kb.close()
    return cleared, keys


def build_cfg(out: Path, wsmap: dict, needed_ids: list):
    base = AppConfig.load(str(ROOT / "config" / "config.yaml"))
    ws_list, skipped = [], []
    for wid in dict.fromkeys(needed_ids):
        info = wsmap.get(wid)
        if not info or not Path(info["path"]).is_dir():
            skipped.append(wid)
            continue
        ws_list.append(WorkspaceConfig(
            id=wid, name=info["name"], path=info["path"],
            index=IndexConfig(auto_reindex_on_query=False)))
    cfg = AppConfig(
        host=base.host, port=base.port, log_level=base.log_level,
        data_dir=str(out), read_only=True,
        default_workspace=ws_list[0].id if ws_list else "",
        workspaces=ws_list, llm=base.llm, channels=base.channels,
        memory=base.memory, knowledge=base.knowledge, auth=base.auth)
    return cfg, skipped


async def run_all(bot, tasks, per_task_timeout):
    results = []
    for i, t in enumerate(tasks, 1):
        t0 = time.time()
        rec = {"task_id": t.get("task_id", ""), "question": t["question"],
               "workspace": t.get("workspace", ""),
               "original_workspace": t.get("_orig_workspace", t.get("workspace", "")),
               "session_id": f"l0:{t.get('task_id','')}", "error": ""}
        try:
            ans = await asyncio.wait_for(
                bot.handle(t.get("workspace", ""), t["question"], rec["session_id"]),
                timeout=per_task_timeout)
            try:
                usage = ans.to_dict().get("usage", {})
            except Exception:
                usage = {}
            rec.update({
                "trace_id": getattr(ans, "trace_id", "") or "",
                "answer_chars": len(ans.text or ""),
                "answer_excerpt": (ans.text or "")[:160].replace("\n", " "),
                "citations": [getattr(c, "label", lambda: str(c))() for c in (ans.citations or [])][:8],
                "usage": usage,
            })
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
        rec["elapsed_ms"] = int((time.time() - t0) * 1000)
        results.append(rec)
        flag = "ERR" if rec["error"] else "OK"
        print(f"  [{i}/{len(tasks)}] {flag} {rec['task_id']} "
              f"{rec['elapsed_ms']}ms chars={rec.get('answer_chars', 0)} "
              f"trace={rec.get('trace_id','')[:14]} | {t['question'][:26]}", flush=True)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(HERE / "failure_cases.draft.yaml"))
    ap.add_argument("--attribution", default=str(ROOT / "loop" / "reports" / "attribution.json"))
    ap.add_argument("--main-db", default=str(ROOT / "data" / "codelib.db"))
    ap.add_argument("--main-kb", default=str(ROOT / "data" / "knowledge.db"))
    ap.add_argument("--only-missing", action="store_true", help="只跑归因报告里无 trace 留痕的题")
    ap.add_argument("--ids", default="", help="逗号分隔指定 task_id（优先于 --only-missing）")
    ap.add_argument("--limit", type=int, default=3, help="0 = 全部")
    ap.add_argument("--per-task-timeout", type=int, default=180)
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cases_path, main_db, main_kb = Path(args.cases), Path(args.main_db), Path(args.main_kb)
    if not all(p.exists() for p in (cases_path, main_db, main_kb)):
        print("题集或数据库不存在", file=sys.stderr)
        return 1
    cases = [c for c in load_cases(cases_path) if c.get("split") != "drop"]

    ids = None
    if args.ids:
        ids = {x.strip() for x in args.ids.split(",") if x.strip()}
        cases = [c for c in cases if c.get("task_id") in ids]
    elif args.only_missing:
        attr = json.loads(Path(args.attribution).read_text(encoding="utf-8"))
        ids = {r["task_id"] for r in attr["rows"] if r["signals"]["trace_count"] == 0}
        cases = [c for c in cases if c.get("task_id") in ids]
    if args.limit > 0:
        cases = cases[:args.limit]

    wsmap = workspace_map(main_db)
    # 实测：knowledge.db 里 gateway:消息网关 与 tdx_msg_bus_svr 的 230 个文件路径
    # 完全相同（交集 230 / 差集 0），故 msg_gateway 视为同一工程的历史别名。
    WS_ALIAS = {"msg_gateway": "tdx_msg_bus_svr"}
    tasks = []
    for c in cases:
        orig = c.get("workspace", "")
        wid = WS_ALIAS.get(orig, orig)
        if wid in wsmap and Path(wsmap[wid]["path"]).is_dir():
            t = dict(c)
            t["_orig_workspace"] = orig
            t["workspace"] = wid
            tasks.append(t)
    alias_used = sorted({t["_orig_workspace"] for t in tasks
                         if t["_orig_workspace"] != t["workspace"]})
    if alias_used:
        print("历史库名别名:", {a: WS_ALIAS[a] for a in alias_used})
    kept_ids = {t.get("task_id") for t in tasks}
    skipped_ws = sorted({c.get("workspace", "") for c in cases
                         if c.get("task_id") not in kept_ids})
    print(f"待跑 {len(tasks)} 题" + (f"｜跳过无工程目录的 workspace: {skipped_ws}" if skipped_ws else ""))
    for t in tasks:
        print(f"  {t.get('task_id')} [{t.get('workspace')}] {t['question'][:44]}")
    if args.dry_run:
        print("--dry-run：未复制数据库、未调用 LLM")
        return 0
    if not tasks:
        print("没有可跑的题", file=sys.stderr)
        return 1

    out = Path(args.out_dir) if args.out_dir else (
        ROOT / "loop" / "runs" / ("l0_" + datetime.now().strftime("%Y%m%d_%H%M%S")))
    print("\n准备临时库（只写副本）:", out)
    cleared, kb_keys = prepare(out, main_db, main_kb)
    cfg, _ = build_cfg(out, wsmap, [t["workspace"] for t in tasks])
    cfg.knowledge = {k: v for k, v in cfg.knowledge.items() if k != "db"}  # 强制指向副本
    print("清空副本留痕表:", ", ".join(cleared))
    print("知识库副本已标记的 workspace:", kb_keys)
    print("知识库:", [(w.id, w.name, w.path) for w in cfg.workspaces])
    print("模型:", cfg.llm.model, "｜每题超时", args.per_task_timeout, "s\n")

    store = Store(cfg.db_path)
    bot = BotService(cfg, store)
    t0 = time.time()
    results = asyncio.run(run_all(bot, tasks, args.per_task_timeout))
    total_ms = int((time.time() - t0) * 1000)

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cases": str(cases_path), "run_db": str(out / "codelib.db"),
        "llm_model": cfg.llm.model, "base_url_configured": bool(cfg.llm.base_url),
        "workspaces": [{"id": w.id, "name": w.name, "path": w.path} for w in cfg.workspaces],
        "cleared_tables": cleared, "count": len(results), "total_ms": total_ms,
        "results": results,
    }
    (out / "run.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                             default=str) + "\n", encoding="utf-8")
    ok = sum(1 for r in results if not r["error"] and r.get("trace_id"))
    print(f"\n完成 {len(results)} 题｜成功留痕 {ok}｜失败 {len(results) - ok}｜总耗时 {total_ms}ms")
    print("run.json:", out / "run.json")
    print("下一步归因：")
    print(f"  python -B loop/quality_runner.py --db \"{out / 'codelib.db'}\" "
          f"--cases \"{cases_path}\" --out loop/reports/backfill.md --json loop/reports/backfill.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
