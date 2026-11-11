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
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from .bot import BotService
from .channels import FeishuChannel, WeComChannel
from .config import AppConfig
from .memory import MemoryManager
from .store import Store
from .watcher import watch_loop

log = logging.getLogger("codelib")


class AskRequest(BaseModel):
    question: str
    workspace: Optional[str] = None
    session_id: Optional[str] = "api"
    deep: bool = False
    debug: bool = False          # true 时返回 citations/memory/usage 等诊断信息


class NewConversationRequest(BaseModel):
    workspace: Optional[str] = None
    title: Optional[str] = None


class MessageRequest(BaseModel):
    content: str
    deep: bool = False


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
    txt = ans.text
    src = _source_labels(ans.citations)
    if src:
        txt += "\n\n来源：" + "；".join(src)
    return txt


def create_app(config_path: Optional[str] = None) -> FastAPI:
    cfg = AppConfig.load(config_path)
    store = Store(cfg.db_path)
    bot = BotService(cfg, store, MemoryManager(store, cfg.memory))
    wecom = WeComChannel(cfg.channel("wecom"))
    feishu = FeishuChannel(cfg.channel("feishu"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO),
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        task = None
        if any(w.index.watch_interval_seconds > 0 for w in cfg.workspaces):
            task = asyncio.create_task(watch_loop(cfg, store))
        try:
            yield
        finally:
            if task:
                task.cancel()

    app = FastAPI(title="codelib bot", version="0.3.0", lifespan=lifespan)
    app.state.bot = bot
    app.state.store = store

    def require_admin(token: str) -> None:
        if cfg.admin_token and token != cfg.admin_token:
            raise HTTPException(status_code=401, detail="invalid admin token")

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

    @app.get("/healthz")
    async def healthz() -> Dict[str, Any]:
        return {"ok": True, "llm": cfg.llm.available, "read_only": cfg.read_only,
                "workspaces": [w.id for w in cfg.workspaces]}

    # ---------------- 网页对话：会话与历史 ----------------
    @app.get("/api/workspaces")
    async def api_workspaces() -> Dict[str, Any]:
        from .knowledge import KnowledgeStore
        from .knowledge.build import ws_kb_id
        kb = KnowledgeStore(cfg.knowledge_db_path)
        items = []
        for w in cfg.workspaces:
            try:
                counts = kb.counts(ws_kb_id(w.id, w.name))
            except Exception:
                counts = {"chunks": 0, "symbols": 0, "files": 0, "edges": 0, "doc_chunks": 0}
            items.append({
                "id": w.id, "name": w.name, "desc": w.description or "",
                "path": w.path, "ready": counts["chunks"] > 0, "counts": counts,
            })
        return {"workspaces": items, "read_only": cfg.read_only,
                "default_workspace": cfg.default_workspace}

    @app.get("/api/knowledge/status")
    async def api_knowledge_status(x_admin_token: str = Header(default="")) -> Dict[str, Any]:
        require_admin(x_admin_token)
        from .knowledge import KnowledgeStore
        from .knowledge.build import ws_kb_id
        kb = KnowledgeStore(cfg.knowledge_db_path)
        return {w.id: kb.counts(ws_kb_id(w.id, w.name)) for w in cfg.workspaces}

    # ---------------- 全链路留痕 / 过程评估 ----------------
    @app.get("/api/traces")
    async def api_traces(workspace: Optional[str] = None, limit: int = 100,
                         x_admin_token: str = Header(default="")) -> Dict[str, Any]:
        require_admin(x_admin_token)
        wid = None
        if workspace:
            try:
                wid = cfg.workspace(workspace).id
            except Exception:
                wid = workspace
        return {"traces": store.list_traces(wid, limit=limit)}

    @app.get("/api/traces/{trace_id}")
    async def api_trace(trace_id: str, x_admin_token: str = Header(default="")) -> Dict[str, Any]:
        require_admin(x_admin_token)
        t = store.get_trace(trace_id)
        if not t:
            raise HTTPException(status_code=404, detail="trace not found")
        return t

    @app.post("/api/traces/{trace_id}/evaluate")
    async def api_evaluate(trace_id: str, learn: bool = True,
                           x_admin_token: str = Header(default="")) -> Dict[str, Any]:
        require_admin(x_admin_token)
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
                         x_admin_token: str = Header(default="")) -> Dict[str, Any]:
        require_admin(x_admin_token)
        from .knowledge import KnowledgeStore
        from .knowledge.build import ws_kb_id
        kb = KnowledgeStore(cfg.knowledge_db_path)
        w = cfg.workspace(workspace or None)
        return {"workspace": w.id, "boosts": kb.list_boosts(ws_kb_id(w.id, w.name))}

    @app.delete("/api/boosts")
    async def api_boost_delete(kind: str, pattern: str, workspace: Optional[str] = None,
                               x_admin_token: str = Header(default="")) -> Dict[str, Any]:
        require_admin(x_admin_token)
        from .knowledge import KnowledgeStore
        from .knowledge.build import ws_kb_id
        kb = KnowledgeStore(cfg.knowledge_db_path)
        w = cfg.workspace(workspace or None)
        kb.delete_boost(ws_kb_id(w.id, w.name), kind, pattern)
        return {"ok": True}

    @app.get("/api/traces/{trace_id}/audits")
    async def api_trace_audits(trace_id: str, x_admin_token: str = Header(default="")) -> Dict[str, Any]:
        require_admin(x_admin_token)
        t = store.get_trace(trace_id)
        if not t:
            raise HTTPException(status_code=404, detail="trace not found")
        return {"audits": t.get("audits", [])}

    @app.post("/api/conversations")
    async def create_conversation(req: NewConversationRequest,
                                  x_admin_token: str = Header(default="")) -> Dict[str, Any]:
        require_admin(x_admin_token)
        ws = cfg.workspace(req.workspace or None)
        # 规则：同一库最多只有一个“空的新对话”。
        #   - 已有空对话 -> 直接复用，不再新建
        #   - 没有空对话 -> 建一个
        existing = store.find_empty_session(ws.id)
        reused = existing is not None
        if existing:
            conv = existing
        else:
            sid = uuid.uuid4().hex
            conv = store.create_session(sid, ws.id, req.title or "新对话")
        store.cleanup_empty_sessions(None)          # 兜底：全局只留一个空对话
        return {"conversation": conv, "reused": reused,
                "workspace": {"id": ws.id, "name": ws.name}}

    @app.get("/api/conversations")
    async def list_conversations(workspace: Optional[str] = None,
                                 x_admin_token: str = Header(default="")) -> Dict[str, Any]:
        require_admin(x_admin_token)
        store.cleanup_empty_sessions(None)          # 列表前清理多余空对话
        wid = None
        if workspace:
            try:
                wid = cfg.workspace(workspace).id
            except Exception:
                wid = workspace
        items = store.list_sessions(wid)
        # 标记哪些是空对话，前端据此置灰/复用
        for it in items:
            it["empty"] = store.is_empty_session(it["id"])
        return {"conversations": items}

    @app.get("/api/conversations/{sid}")
    async def get_conversation(sid: str,
                               x_admin_token: str = Header(default="")) -> Dict[str, Any]:
        require_admin(x_admin_token)
        sess = store.get_session(sid)
        if not sess:
            raise HTTPException(status_code=404, detail="conversation not found")
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
                           x_admin_token: str = Header(default="")) -> Dict[str, Any]:
        require_admin(x_admin_token)
        sess = store.get_session(sid)
        if not sess:
            raise HTTPException(status_code=404, detail="conversation not found")
        if not (req.content or "").strip():
            raise HTTPException(status_code=400, detail="content 不能为空")
        ws = cfg.workspace(sess["workspace_id"])
        history = store.get_all_messages(sid)[-8:]          # 先取历史（不含本次）
        ans = await bot.handle(ws.id, req.content, sid, req.deep, history)
        new_title = None
        if (sess.get("title") or "新对话") in ("新对话", ""):
            new_title = (req.content or "").strip().replace("\n", " ")[:30] or "新对话"
        store.touch_session(sid, new_title)
        return {"answer": ans.text, "sources": _source_labels(ans.citations),
                "trace_id": ans.trace_id,
                "conversation": store.get_session(sid),
                "messages": store.get_all_messages(sid)}

    @app.delete("/api/conversations/{sid}")
    async def delete_conversation(sid: str,
                                  x_admin_token: str = Header(default="")) -> Dict[str, Any]:
        require_admin(x_admin_token)
        store.delete_session(sid)
        store.cleanup_empty_sessions(None)     # 删完后仍保证最多一个空对话
        return {"ok": True}

    @app.post("/ask")
    async def ask(req: AskRequest, x_admin_token: str = Header(default="")) -> Dict[str, Any]:
        require_admin(x_admin_token)
        ans = await bot.handle(req.workspace or "", req.question,
                               req.session_id or "api", req.deep)
        # 默认只返回干净结果：答案 + 精简来源
        payload: Dict[str, Any] = {
            "answer": ans.text,
            "sources": _source_labels(ans.citations),
        }
        if req.debug:
            payload["debug"] = ans.to_dict()
        return payload

    # ---------------- 企业微信 ----------------
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
        ans = await bot.handle(msg.workspace_id, msg.text, msg.session_id)
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
                ans = await bot.handle(msg.workspace_id, msg.text, msg.session_id)
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
