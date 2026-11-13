"""FastAPI 应用 —— 极简接口。

只保留必要的几组：
  GET  /healthz                     健康检查
  POST /ask                         问答（本地/通用/程序调用都走它）
  GET  /channels/wecom/callback     企业微信 URL 验证
  POST /channels/wecom/callback     企业微信消息回调
  POST /channels/feishu/event       飞书事件回调

索引、变更、记忆等管理能力通过对话命令完成（/index、/changes、/remember、/status），
不再单独暴露大量管理接口。
"""
from __future__ import annotations

import asyncio
import logging
import time
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .bot import BotService
from .channels import FeishuChannel, WeComChannel
from .config import AppConfig
from .im import IMConversation
from .config import WorkspaceConfig
from .memory import MemoryManager
from .store import Store
from .security import SESSION_COOKIE, SESSION_TTL_SECONDS, issue_session, session_user, token_hash
from .watcher import watch_loop

log = logging.getLogger("codelib")
alog = logging.getLogger("codelib.access")   # 网页/HTTP 访问日志（console + data/logs/access.log）


class AskRequest(BaseModel):
    question: str
    workspace: Optional[str] = None
    session_id: Optional[str] = "api"
    deep: bool = False
    debug: bool = False          # true 时返回 citations/memory/usage 等诊断信息
    research: Optional[bool] = None  # None=自动（enabled 且分析类问题走 Research Loop）


class ResearchAskRequest(BaseModel):
    question: str
    workspace: Optional[str] = None

class NewConversationRequest(BaseModel):
    workspace: Optional[str] = None
    title: Optional[str] = None


class MessageRequest(BaseModel):
    content: str
    deep: bool = False


class LoginRequest(BaseModel):
    username: str
    password: str


class NewUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"
    workspace_ids: list[str] = Field(default_factory=list)


class WorkspaceCreateRequest(BaseModel):
    name: str
    path: str
    id: Optional[str] = None
    description: str = ""


class WorkspaceUpdateRequest(BaseModel):
    name: str
    path: str
    description: str = ""


class WorkspaceRebuildRequest(BaseModel):
    full: bool = True


class ConversationUpdateRequest(BaseModel):
    title: str


class WorkspaceAccessRequest(BaseModel):
    workspace_ids: list[str]


class UserActiveRequest(BaseModel):
    active: bool


class UserPasswordRequest(BaseModel):
    password: str


_WEB_INDEX = Path(__file__).parent / "web" / "index.html"


def _web_html() -> str:
    try:
        return _WEB_INDEX.read_text(encoding="utf-8")
    except Exception:
        return "<h1>web/index.html 未找到</h1>"


def _source_labels(citations, limit: int = 5):
    out = []
    for c in citations:
        loc = f"{c.path}:{c.start_line}" if c.end_line <= c.start_line else f"{c.path}:{c.start_line}-{c.end_line}"
        if loc not in out:
            out.append(loc)
        if len(out) >= limit:
            break
    return out


def _format_answer(ans) -> str:
    return ans.text


_ANALYSIS_HINTS = ("分析", "为什么", "能否", "能不能", "顺序", "架构", "对比", "设计",
                   "实现", "机制", "流程", "怎么", "如何", "保证", "评估", "风险")


def _is_analysis_question(q: str) -> bool:
    """分析类问题走 Research Loop；简单查询仍走旧 FTS 快通道。"""
    return len(q or "") >= 20 or any(h in (q or "") for h in _ANALYSIS_HINTS)


def create_app(config_path: Optional[str] = None) -> FastAPI:
    cfg = AppConfig.load(config_path)
    store = Store(cfg.db_path)
    bootstrap = cfg.auth.get("bootstrap_admin", {}) or {}
    created_admin = store.bootstrap_admin(str(bootstrap.get("username", "")),
                                         str(bootstrap.get("password", "")))
    if created_admin:
        log.warning("Bootstrapped initial administrator account: %s", created_admin["username"])
    elif not store.list_users():
        log.error("No accounts exist. Configure auth.bootstrap_admin before first startup.")
    configured_ids = {w.id for w in cfg.workspaces}
    for row in store.list_managed_workspaces():
        existing = next((w for w in cfg.workspaces if w.id == row["id"]), None)
        if row.get("disabled"):
            if existing is not None:
                cfg.workspaces.remove(existing)
            continue
        if existing is not None:
            existing.name = row["name"]
            existing.path = row["path"]
            existing.description = row.get("description", "")
        else:
            cfg.workspaces.append(WorkspaceConfig(
                id=row["id"], name=row["name"], path=row["path"],
                description=row.get("description", "")))
    if not any(w.id == cfg.default_workspace for w in cfg.workspaces):
        cfg.default_workspace = cfg.workspaces[0].id if cfg.workspaces else ""
    bot = BotService(cfg, store, MemoryManager(store, cfg.memory))
    wecom = WeComChannel(cfg.channel("wecom"))
    feishu = FeishuChannel(cfg.channel("feishu"))
    im = IMConversation(cfg, store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO),
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        task = None
        if any(w.index.watch_interval_seconds > 0 for w in cfg.workspaces):
            task = asyncio.create_task(watch_loop(cfg, store))
        _research_on = bool((cfg.research or {}).get("enabled", False))
        log.info("Research Loop: %s（/ask 分析类问题自动分流 + /api/research/ask）",
                 "已启用" if _research_on else "未启用（research.enabled=false）")
        try:
            yield
        finally:
            if task:
                task.cancel()

    app = FastAPI(title="codelib bot", version="0.3.0", lifespan=lifespan)
    app.state.bot = bot

    # ---------- 网页/HTTP 访问日志（R3.5） ----------
    def _setup_access_log() -> None:
        try:
            from logging.handlers import RotatingFileHandler
            log_dir = Path(cfg.data_dir) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            h = RotatingFileHandler(log_dir / "access.log", maxBytes=5 * 1024 * 1024,
                                    backupCount=3, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            alog.addHandler(h)
            alog.setLevel(logging.INFO)
        except Exception as e:
            log.warning("access.log 初始化失败: %s", e)

    _SKIP_ACCESS = ("/healthz",)

    @app.middleware("http")
    async def access_log_mw(request: Request, call_next):
        path = request.url.path
        if path in _SKIP_ACCESS:
            return await call_next(request)
        t0 = time.time()
        tok = request.cookies.get(SESSION_COOKIE, "")
        _auth = request.headers.get("authorization", "")
        if _auth.lower().startswith("bearer "):
            tok = _auth[8:]
        who = ("u:" + tok[:8]) if tok else "-"
        status = 500
        error = ""
        try:
            resp = await call_next(request)
            status = resp.status_code
            return resp
        except HTTPException as e:
            status = e.status_code
            raise
        except Exception as e:
            error = type(e).__name__ + ": " + str(e)[:120]
            raise
        finally:
            alog.info('%s "%s %s" %d %dms %s%s',
                      request.client.host if request.client else "-",
                      request.method, path, status,
                      int((time.time() - t0) * 1000), who,
                      (" ERR " + error) if error else "")

    _setup_access_log()
    app.state.store = store

    def current_user(request: Request) -> Dict[str, Any]:
        request.state.user = None
        user = session_user(store, request)
        if not user:
            raise HTTPException(status_code=401, detail="请先登录")
        return user

    def require_admin(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="需要管理员权限")
        return user

    def managed_workspace(workspace_id: str) -> WorkspaceConfig:
        target = next((w for w in cfg.workspaces if w.id == workspace_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="知识库不存在")
        return target

    def user_workspace(user: Dict[str, Any], workspace_id: Optional[str] = None):
        if user["role"] == "admin":
            try:
                return cfg.workspace(workspace_id or None)
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        allowed = store.user_workspace_ids(user["id"])
        if not allowed:
            raise HTTPException(status_code=403, detail="管理员尚未为该账号分配知识库")
        if workspace_id:
            try:
                ws = cfg.workspace(workspace_id)
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=404, detail="知识库不存在") from exc
            if ws.id not in allowed:
                raise HTTPException(status_code=403, detail="该账号没有访问此知识库的权限")
            return ws
        target = cfg.default_workspace if cfg.default_workspace in allowed else allowed[0]
        return cfg.workspace(target)

    def resolve_workspace_ids(workspace_ids: list[str]) -> list[str]:
        resolved = []
        for requested_id in workspace_ids:
            ws = next((item for item in cfg.workspaces
                       if requested_id in (item.id, item.name, Path(item.path).name)), None)
            if ws is None:
                raise HTTPException(status_code=400, detail=f"未知知识库：{requested_id}")
            resolved.append(ws.id)
        return list(dict.fromkeys(resolved))

    def check_session_access(user: Dict[str, Any], session: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not session:
            raise HTTPException(status_code=404, detail="conversation not found")
        if user["role"] != "admin" and int(session.get("owner_user_id") or 0) != int(user["id"]):
            raise HTTPException(status_code=404, detail="conversation not found")
        user_workspace(user, session["workspace_id"])
        return session

    @app.middleware("http")
    async def readonly_guard(request: Request, call_next):
        """只读守卫：本服务不提供任何修改工程文件的入口。

        - 对 /api/conversations、/ask 等对话类路径只允许 GET/POST/DELETE（仅动会话数据，不动工程文件）
        - 任何指向“写文件/改代码/执行命令”的路径一律 403，防止误接其它路由或脚本。
        """
        if cfg.read_only:
            path = request.url.path
            blocked_prefixes = ("/api/files", "/api/write", "/api/edit", "/api/apply",
                                "/api/patch", "/api/exec", "/api/shell", "/api/git", "/fs/")
            if any(path.startswith(p) for p in blocked_prefixes):
                return JSONResponse(status_code=403,
                                    content={"detail": "服务为只读模式：不允许修改工程文件"})
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def root() -> HTMLResponse:
        return HTMLResponse(_web_html())

    @app.get("/static/markdown-it.min.js")
    async def markdown_vendor() -> FileResponse:
        vendor_path = Path(__file__).parent / "web" / "vendor" / "markdown-it.min.js"
        return FileResponse(vendor_path, media_type="text/javascript")

    @app.post("/api/auth/login")
    async def login(req: LoginRequest, request: Request, response: Response) -> Dict[str, Any]:
        user = store.authenticate_user(req.username, req.password)
        if not user:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = issue_session(store, user)
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL_SECONDS,
                            httponly=True, secure=request.url.scheme == "https",
                            samesite="lax", path="/")
        return {"user": user}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response) -> Dict[str, Any]:
        token = request.cookies.get(SESSION_COOKIE, "")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if token:
            store.revoke_auth_session(token_hash(token))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    async def auth_me(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
        return {"user": user}

    @app.get("/api/admin/users")
    async def admin_users(user: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        return {"users": store.list_users()}

    @app.post("/api/admin/users")
    async def admin_create_user(req: NewUserRequest,
                                admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        if req.role not in ("admin", "user"):
            raise HTTPException(status_code=400, detail="角色只能是 admin 或 user")
        workspace_ids = resolve_workspace_ids(req.workspace_ids) if req.role == "user" else []
        try:
            created = store.create_user(req.username, req.password, req.role)
            if req.role == "user":
                store.set_user_workspaces(created["id"], workspace_ids, admin["id"])
                created["workspace_ids"] = store.user_workspace_ids(created["id"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"user": created}

    @app.put("/api/admin/users/{user_id}/workspaces")
    async def admin_set_user_workspaces(user_id: int, req: WorkspaceAccessRequest,
                                        admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        workspace_ids = resolve_workspace_ids(req.workspace_ids)
        try:
            store.set_user_workspaces(user_id, workspace_ids, admin["id"])
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="用户不存在") from exc
        return {"user_id": user_id, "workspace_ids": store.user_workspace_ids(user_id)}

    @app.patch("/api/admin/users/{user_id}/active")
    async def admin_set_user_active(user_id: int, req: UserActiveRequest,
                                    admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        try:
            store.set_user_active(user_id, req.active)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="用户不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.put("/api/admin/users/{user_id}/password")
    async def admin_reset_user_password(user_id: int, req: UserPasswordRequest,
                                        admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        try:
            store.reset_user_password(user_id, req.password)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="用户不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/api/admin/workspaces")
    async def admin_workspaces(admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        return {"workspaces": list_workspace_info(admin, include_path=True)}

    def list_workspace_info(user: Dict[str, Any], include_path: bool = False) -> list[Dict[str, Any]]:
        from .knowledge import KnowledgeStore
        from .knowledge.build import ws_kb_id
        kb = KnowledgeStore(cfg.knowledge_db_path)
        allowed = None if user["role"] == "admin" else set(store.user_workspace_ids(user["id"]))
        items = []
        for w in cfg.workspaces:
            if allowed is not None and w.id not in allowed:
                continue
            try:
                counts = kb.counts(ws_kb_id(w.id, w.name))
            except Exception:
                counts = {"chunks": 0, "symbols": 0, "files": 0, "edges": 0, "doc_chunks": 0}
            item = {"id": w.id, "name": w.name, "desc": w.description or "",
                    "ready": counts["chunks"] > 0, "counts": counts}
            if include_path:
                legacy = store.stats(w.id)
                item.update({
                    "path": w.path,
                    "source": "config" if w.id in configured_ids else "managed",
                    "index_format": kb.get_meta(f"index_format:{ws_kb_id(w.id, w.name)}", "legacy"),
                    "legacy_files": legacy["indexed_files"],
                    "auto_reindex_on_query": w.index.auto_reindex_on_query,
                })
            items.append(item)
        return items

    @app.post("/api/admin/workspaces")
    async def admin_create_workspace(req: WorkspaceCreateRequest,
                                     admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="知识库名称不能为空")
        raw_id = (req.id or "").strip().lower()
        workspace_id = re.sub(r"[^a-z0-9_-]+", "-", raw_id or name.lower()).strip("-_")[:64]
        if not workspace_id:
            raise HTTPException(status_code=400, detail="无法从名称生成知识库 ID，请手动指定 ID")
        if any(w.id == workspace_id for w in cfg.workspaces):
            raise HTTPException(status_code=409, detail="知识库 ID 已存在")
        path = Path(os.path.expanduser(req.path)).resolve()
        if not path.is_dir():
            raise HTTPException(status_code=400, detail="目录不存在或不是目录")
        ws = WorkspaceConfig(id=workspace_id, name=name, path=str(path),
                             description=req.description.strip())
        try:
            store.add_managed_workspace(ws.id, ws.name, ws.path, ws.description)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        cfg.workspaces.append(ws)
        try:
            from . import indexer
            from .knowledge.build import build_workspace
            await asyncio.to_thread(indexer.index_workspace, store, ws, True)
            result = await asyncio.to_thread(build_workspace, cfg, ws, True)
        except Exception as exc:
            log.exception("Knowledge base indexing failed: %s", ws.id)
            raise HTTPException(status_code=500, detail=f"知识库已登记，但建库失败：{exc}") from exc
        return {"workspace": {"id": ws.id, "name": ws.name, "path": ws.path,
                              "ready": True, "counts": result.get("totals", {})}}

    @app.put("/api/admin/workspaces/{workspace_id}")
    async def admin_update_workspace(workspace_id: str, req: WorkspaceUpdateRequest,
                                     admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        ws = managed_workspace(workspace_id)
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="知识库名称不能为空")
        path = Path(os.path.expanduser(req.path)).resolve()
        if not path.is_dir():
            raise HTTPException(status_code=400, detail="目录不存在或不是目录")
        from .knowledge import KnowledgeStore
        from .knowledge.build import build_workspace, ws_kb_id
        old_key = ws_kb_id(ws.id, ws.name)
        new_key = ws_kb_id(ws.id, name)
        path_changed = str(path) != ws.path
        store.add_managed_workspace(ws.id, name, str(path), req.description.strip())
        kb = KnowledgeStore(cfg.knowledge_db_path)
        await asyncio.to_thread(kb.rename_workspace, old_key, new_key)
        ws.name = name
        ws.path = str(path)
        ws.description = req.description.strip()
        result = None
        if path_changed:
            from . import indexer
            await asyncio.to_thread(indexer.index_workspace, store, ws, True)
            result = await asyncio.to_thread(build_workspace, cfg, ws, True)
        counts = kb.counts(new_key)
        return {"workspace": {"id": ws.id, "name": ws.name, "path": ws.path,
                              "description": ws.description, "ready": counts["chunks"] > 0,
                              "counts": counts}, "rebuilt": result}

    @app.post("/api/admin/workspaces/{workspace_id}/rebuild")
    async def admin_rebuild_workspace(workspace_id: str, req: WorkspaceRebuildRequest,
                                      admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        ws = managed_workspace(workspace_id)
        from . import indexer
        from .knowledge.build import build_workspace
        legacy = await asyncio.to_thread(indexer.index_workspace, store, ws, req.full)
        result = await asyncio.to_thread(build_workspace, cfg, ws, req.full)
        return {"workspace_id": ws.id, "full": req.full,
                "legacy": legacy, "knowledge": result}

    @app.delete("/api/admin/workspaces/{workspace_id}")
    async def admin_delete_workspace(workspace_id: str,
                                     admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        ws = managed_workspace(workspace_id)
        from .knowledge import KnowledgeStore
        from .knowledge.build import ws_kb_id
        kb = KnowledgeStore(cfg.knowledge_db_path)
        await asyncio.to_thread(kb.delete_workspace, ws_kb_id(ws.id, ws.name))
        removed = store.delete_workspace_data(ws.id)
        if ws.id in configured_ids:
            store.disable_managed_workspace(ws.id, ws.name, ws.path, ws.description)
        else:
            store.delete_managed_workspace(ws.id)
        cfg.workspaces.remove(ws)
        if cfg.default_workspace == ws.id:
            cfg.default_workspace = cfg.workspaces[0].id if cfg.workspaces else ""
        return {"ok": True, "workspace_id": ws.id, "removed": removed}

    @app.get("/healthz")
    async def healthz() -> Dict[str, Any]:
        return {"ok": True, "llm": cfg.llm.available, "read_only": cfg.read_only,
                "workspace_count": len(cfg.workspaces),
                "research": bool((cfg.research or {}).get("enabled", False))}

    # ---------------- 网页对话：会话与历史 ----------------
    @app.get("/api/workspaces")
    async def api_workspaces(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
        return {"workspaces": list_workspace_info(user, include_path=user["role"] == "admin"),
                "read_only": cfg.read_only, "default_workspace": cfg.default_workspace}

    @app.get("/api/knowledge/status")
    async def api_knowledge_status(admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        from .knowledge import KnowledgeStore
        from .knowledge.build import ws_kb_id
        kb = KnowledgeStore(cfg.knowledge_db_path)
        return {w.id: kb.counts(ws_kb_id(w.id, w.name)) for w in cfg.workspaces}

    # ---------------- 全链路留痕 / 过程评估 ----------------
    @app.get("/api/traces")
    async def api_traces(workspace: Optional[str] = None, limit: int = 100,
                         admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        wid = None
        if workspace:
            try:
                wid = cfg.workspace(workspace).id
            except Exception:
                wid = workspace
        return {"traces": store.list_traces(wid, limit=limit)}

    @app.get("/api/traces/{trace_id}")
    async def api_trace(trace_id: str,
                        admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        t = store.get_trace(trace_id)
        if not t:
            raise HTTPException(status_code=404, detail="trace not found")
        return t

    @app.post("/api/traces/{trace_id}/evaluate")
    async def api_evaluate(trace_id: str, learn: bool = True,
                           admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        t = store.get_trace(trace_id)
        if not t:
            raise HTTPException(status_code=404, detail="trace not found")
        from .evaluate import judge_trace
        result = await judge_trace(t, bot.llm)
        aid = store.add_audit(trace_id, t.get("workspace_id", ""),
                              result.get("verdict", "fair"),
                              float(result.get("score", 0)), result)
        learned: List[Dict[str, Any]] = []
        if learn:
            try:
                from .feedback import apply_report_to_boosts
                from .knowledge import KnowledgeStore
                from .knowledge.build import ws_kb_id
                w = cfg.workspace(t.get("workspace_id", "") or None)
                kb = KnowledgeStore(cfg.knowledge_db_path)
                learned = apply_report_to_boosts(kb, ws_kb_id(w.id, w.name), result,
                                                 source=f"trace:{trace_id[:10]}")
            except Exception:
                learned = []
        return {"audit_id": aid, "learned_boosts": learned, **result}

    @app.get("/api/boosts")
    async def api_boosts(workspace: Optional[str] = None,
                         admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        from .knowledge import KnowledgeStore
        from .knowledge.build import ws_kb_id
        kb = KnowledgeStore(cfg.knowledge_db_path)
        w = cfg.workspace(workspace or None)
        return {"workspace": w.id, "boosts": kb.list_boosts(ws_kb_id(w.id, w.name))}

    @app.delete("/api/boosts")
    async def api_boost_delete(kind: str, pattern: str, workspace: Optional[str] = None,
                               admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        from .knowledge import KnowledgeStore
        from .knowledge.build import ws_kb_id
        kb = KnowledgeStore(cfg.knowledge_db_path)
        w = cfg.workspace(workspace or None)
        kb.delete_boost(ws_kb_id(w.id, w.name), kind, pattern)
        return {"ok": True}

    @app.get("/api/traces/{trace_id}/audits")
    async def api_trace_audits(trace_id: str,
                               admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        t = store.get_trace(trace_id)
        if not t:
            raise HTTPException(status_code=404, detail="trace not found")
        return {"audits": t.get("audits", [])}

    @app.post("/api/conversations")
    async def create_conversation(req: NewConversationRequest,
                                  user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
        ws = user_workspace(user, req.workspace)
        # 规则：同一库最多只有一个“空的新对话”。
        #   - 已有空对话 -> 直接复用，不再新建
        #   - 没有空对话 -> 建一个
        existing = store.find_empty_session(ws.id, owner_user_id=user["id"])
        reused = existing is not None
        if existing:
            conv = existing
        else:
            sid = uuid.uuid4().hex
            conv = store.create_session(sid, ws.id, req.title or "新对话", owner_user_id=user["id"])
        store.cleanup_empty_sessions(owner_user_id=user["id"])
        return {"conversation": conv, "reused": reused,
                "workspace": {"id": ws.id, "name": ws.name}}

    @app.get("/api/conversations")
    async def list_conversations(workspace: Optional[str] = None,
                                 user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
        owner_id = None if user["role"] == "admin" else user["id"]
        if workspace:
            ws = user_workspace(user, workspace)
            items = store.list_sessions(ws.id, owner_user_id=owner_id)
        else:
            if owner_id is None:
                items = store.list_sessions(owner_user_id=None)
            else:
                allowed = store.user_workspace_ids(user["id"])
                items = []
                for wid in allowed:
                    items.extend(store.list_sessions(wid, owner_user_id=owner_id))
                items.sort(key=lambda item: item["updated_at"], reverse=True)
                items = items[:200]
        store.cleanup_empty_sessions(owner_user_id=owner_id)
        # 标记哪些是空对话，前端据此置灰/复用
        for it in items:
            it["empty"] = store.is_empty_session(it["id"])
        return {"conversations": items}

    @app.get("/api/conversations/{sid}")
    async def get_conversation(sid: str,
                               user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
        sess = check_session_access(user, store.get_session(sid))
        try:
            w = cfg.workspace(sess["workspace_id"])
            ws = {"id": w.id, "name": w.name}
        except Exception:
            ws = {"id": sess["workspace_id"], "name": sess["workspace_id"]}
        # 会话绑定的工作空间必须存在（多库场景下防止指向已删除的库）
        return {"conversation": sess, "workspace": ws,
                "messages": store.get_all_messages(sid)}

    @app.post("/api/conversations/{sid}/messages")
    async def post_message(sid: str, req: MessageRequest,
                           user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
        sess = check_session_access(user, store.get_session(sid))
        if not (req.content or "").strip():
            raise HTTPException(status_code=400, detail="content 不能为空")
        if user["role"] != "admin" and req.content.lstrip().startswith("/"):
            raise HTTPException(status_code=403, detail="普通账号只能进行知识库问答")
        ws = cfg.workspace(sess["workspace_id"])
        history = store.get_all_messages(sid)[-8:]          # 先取历史（不含本次）
        _res = await maybe_run_research(ws, req.content)
        if _res is not None:
            store.append_message(sid, ws.id, "user", req.content)
            store.append_message(sid, ws.id, "assistant", _res["answer"])
            new_title = None
            if (sess.get("title") or "新对话") in ("新对话", ""):
                new_title = (req.content or "").strip().replace(chr(10), " ")[:30] or "新对话"
            store.touch_session(sid, new_title)
            return {"answer": _res["answer"], "sources": _res["sources"],
                    "trace_id": _res.get("trace_id"),
                    "conversation": store.get_session(sid),
                    "messages": store.get_all_messages(sid),
                    "research": _res.get("research")}
        new_title = None
        if (sess.get("title") or "新对话") in ("新对话", ""):
            new_title = (req.content or "").strip().replace("\n", " ")[:30] or "新对话"
        store.touch_session(sid, new_title)
        ans = await bot.handle(ws.id, req.content, sid, req.deep, history)
        return {"answer": ans.text, "sources": _source_labels(ans.citations),
                "trace_id": ans.trace_id,
                "conversation": store.get_session(sid),
                "messages": store.get_all_messages(sid)}

    @app.patch("/api/conversations/{sid}")
    async def rename_conversation(sid: str, req: ConversationUpdateRequest,
                                  user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
        check_session_access(user, store.get_session(sid))
        title = req.title.strip()
        if not title or len(title) > 80:
            raise HTTPException(status_code=400, detail="对话标题需为 1-80 个字符")
        store.touch_session(sid, title)
        return {"conversation": store.get_session(sid)}

    @app.delete("/api/conversations/{sid}")
    async def delete_conversation(sid: str,
                                  user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
        check_session_access(user, store.get_session(sid))
        store.delete_session(sid)
        store.cleanup_empty_sessions(owner_user_id=user["id"])
        return {"ok": True}

    async def maybe_run_research(ws, question: str, force: bool = False) -> Optional[dict]:
        """研究循环统一入口：enabled + 分析类判定（force 可绕过判定）。
        返回统一 dict（answer/sources/trace_id/research），None = 走旧快通道。"""
        _rcfg = cfg.research or {}
        if not _rcfg.get("enabled", False):
            return None
        if not force and not _is_analysis_question(question):
            return None
        from .knowledge.build import ws_kb_id
        from .research.evidence import EvidenceBoard
        from app.chain import anchor_context
        _pre = ""
        try:
            _pre = anchor_context(cfg.knowledge_db_path, ws_kb_id(ws.id, ws.name), question)[:12000]
        except Exception:
            _pre = ""
        from .research.loop import run_research
        from .research.tools import ResearchTools
        from pathlib import Path as _Path
        _ws_root = str(_Path(ws.path)) if ws.path and _Path(ws.path).is_dir() else None
        _ws_key = ws_kb_id(ws.id, ws.name)
        _board = EvidenceBoard()
        _tools = ResearchTools(cfg.knowledge_db_path, _ws_key, _ws_root, _board)
        try:
            _res = await run_research(question, bot.llm, _tools,
                                      limits=(_rcfg.get("limits") or {}),
                                      kb_path=cfg.knowledge_db_path,
                                      workspace=_ws_key,
                                      preloaded_context=_pre,                                      memory_cfg=(_rcfg.get("memory") or {}))
        finally:
            _tools.close()
        return {"answer": _res.get("answer", ""),
                "sources": [{"symbol": e.get("symbol", ""), "path": e.get("path", "")}
                            for e in (_res.get("evidence") or [])],
                "trace_id": None,
                "research": {"steps": _res.get("steps"), "llm_calls": _res.get("llm_calls"),
                             "stop_reason": _res.get("stop_reason"),
                             "board": _res.get("board", {}),
                             "open_assertions": _res.get("open_assertions", []),
                             "tool_events": _res.get("tool_events", [])}}

    @app.post("/ask")
    async def ask(req: AskRequest, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
        ws = user_workspace(user, req.workspace)
        if user["role"] != "admin" and (req.question or "").lstrip().startswith("/"):
            raise HTTPException(status_code=403, detail="普通账号只能进行知识库问答")
        session_id = f"api:{user['id']}:{req.session_id or 'default'}"
        _res = await maybe_run_research(ws, req.question, force=(req.research is True))
        if _res is not None:
            _rd = _res.get("research") or {}
            alog.info('research user=%s steps=%s stop=%s q=%.60s',
                      (request.state.user if hasattr(request, "state") else "-"),
                      _rd.get("steps"), _rd.get("stop_reason"), req.question)
        if _res is not None and req.research is not False:
            return {"answer": _res["answer"], "sources": _res["sources"],
                    "research": _res.get("research")}
        ans = await bot.handle(ws.id, req.question, session_id, req.deep)
        payload: Dict[str, Any] = {"answer": ans.text,
                                   "sources": _source_labels(ans.citations)}
        if req.debug:
            payload["debug"] = ans.to_dict()
        return payload

    # ---------------- Research Loop（R3 灰度：/api/research/ask，只读研究循环） ----------------
    @app.post("/api/research/ask")
    async def research_ask(req: ResearchAskRequest,
                           user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
        if not (cfg.research or {}).get("enabled", False):
            raise HTTPException(status_code=404, detail="research 未启用（research.enabled=false）")
        from .knowledge.build import ws_kb_id
        from .research.evidence import EvidenceBoard
        from .research.loop import run_research
        from app.chain import anchor_context
        _pre = ""
        try:
            _pre = anchor_context(cfg.knowledge_db_path, ws_kb_id(ws.id, ws.name), req.question)[:12000]
        except Exception:
            _pre = ""
        from .research.tools import ResearchTools
        ws = user_workspace(user, req.workspace)
        ws_root = None
        if ws.path:
            from pathlib import Path as _P
            _root = _P(ws.path)
            ws_root = str(_root) if _root.is_dir() else None
        _ws_key = ws_kb_id(ws.id, ws.name)
        board = EvidenceBoard()
        tools = ResearchTools(cfg.knowledge_db_path, _ws_key, ws_root, board)
        limits = (cfg.research or {}).get("limits") or {}
        try:
            result = await run_research(req.question, bot.llm, tools, limits=limits,
                                        kb_path=cfg.knowledge_db_path,
                                        workspace=_ws_key,
                                        memory_cfg=((cfg.research or {}).get("memory") or {}),
                                        preloaded_context=_pre,
                                        tracer=None)
        finally:
            tools.close()
        return {"question": req.question, "workspace": ws.id,
                "answer": result.get("answer", ""),
                "conclusions": result.get("conclusions", []),
                "evidence": result.get("evidence", []),
                "open_assertions": result.get("open_assertions", []),
                "board": result.get("board", {}),
                "steps": result.get("steps"), "llm_calls": result.get("llm_calls"),
                "stop_reason": result.get("stop_reason"),
                "wall_ms": result.get("wall_ms"),
                "tool_events": result.get("tool_events", [])}

    @app.get("/channels/wecom/callback")
    async def wecom_verify(request: Request):
        q = dict(request.query_params)
        plain = wecom.verify_url(q.get("msg_signature", ""), q.get("timestamp", ""),
                                 q.get("nonce", ""), q.get("echostr", ""))
        if plain is None:
            raise HTTPException(status_code=403, detail="signature mismatch")
        return PlainTextResponse(plain)

    @app.post("/channels/wecom/callback")
    async def wecom_message(request: Request):
        body = await request.body()
        msg = wecom.parse_message(body, dict(request.query_params))
        if msg is None:
            return PlainTextResponse("success")
        # 先走 IM 交互（选库/菜单），再决定是否提问
        decision = im.handle("wecom", msg.user_id, msg.text)
        if decision["action"] in ("menu", "need_select", "selected"):
            reply = decision["reply"]
            ans = None
        else:
            # 会话按“用户+库”隔离，避免切换库后串上下文
            sid = f"wecom:{msg.user_id}:{decision['workspace_id']}"
            ans = await bot.handle(decision["workspace_id"], msg.text, sid)
            reply = _format_answer(ans)
        if wecom.cfg.get("corp_id") and wecom.cfg.get("secret") and wecom.cfg.get("agent_id"):
            try:
                if await wecom.send_text(msg.user_id, reply):
                    return PlainTextResponse("success")
            except Exception:
                log.exception("企业微信主动发送失败")
        xml = wecom.build_passive_reply(msg.user_id, wecom.cfg.get("corp_id", ""), reply)
        return PlainTextResponse(xml, media_type="application/xml")

    # ---------------- 飞书 ----------------
    @app.post("/channels/feishu/event")
    async def feishu_event(request: Request):
        body = await request.body()
        kind, payload = feishu.handle(body, dict(request.headers))
        if kind == "challenge":
            return {"challenge": payload}
        if kind == "error":
            raise HTTPException(status_code=403, detail=str(payload))
        if kind == "ignore":
            return {"code": 0}
        msg = payload

        async def _process():
            try:
                decision = im.handle("feishu", msg.user_id, msg.text)
                if decision["action"] in ("menu", "need_select", "selected"):
                    reply = decision["reply"]
                else:
                    sid = f"feishu:{msg.user_id}:{decision['workspace_id']}"
                    ans = await bot.handle(decision["workspace_id"], msg.text, sid)
                    reply = _format_answer(ans)
                ok = await feishu.reply_message(msg.message_id, reply)
                if not ok and feishu.cfg.get("webhook_url"):
                    await feishu.send_webhook(reply)
            except Exception:
                log.exception("飞书消息处理失败")

        asyncio.create_task(_process())   # 3 秒内先回执，异步回复
        return {"code": 0}

    return app


app = create_app(os.environ.get("CODELIB_CONFIG"))
