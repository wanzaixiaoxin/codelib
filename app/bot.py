"""问答编排：命令路由 + 增量索引 + 检索 + 记忆 + LLM/降级回答。"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from . import indexer, retrieval
from .config import AppConfig, WorkspaceConfig
from .llm import LLMClient
from .memory import MemoryManager
from .models import Answer, Citation
from .store import Store
from .trace import Tracer
from .utils import clip, estimate_tokens

SYSTEM_PROMPT = (
    "你是一名资深软件工程师，负责向同事讲清楚这个工程。"
    "你只能依据提供的候选片段与历史记忆作答，不得编造不存在的文件、函数或行为。"
    "候选中若已明确给出某个文件/符号/配置项，就直接依据它作答；没有给出时，不要用记忆中隐约记得的内容填充。"
    "候选片段里可能混有只是关键词相近、实际无关的内容：你要先剔除无关项，"
    "再把真正相关的内容综合成一段连贯、完整、有逻辑的回答，而不是罗列片段或复制代码。"
    "只允许引用候选里真实出现的文件与行号；无法确认时明确说“候选未覆盖”，不要猜测。"
    "本服务是只读的：你只提供解释、结论与方案建议（可按“改哪里、怎么改、如何验证”描述），"
    "但不要产出可执行的补丁或声称已修改任何文件。"
)

HELP_TEXT = """可用命令：
/status              查看当前工作区索引与记忆状态
/index [full]        增量/全量重建索引
/changes             查看自上次索引以来的文件变更
/remember <文本>      把一条事实写入长期记忆
/facts               列出已记忆的事实
/ws                  列出可切换的工作区
/help                显示本帮助
直接发送问题即进行代码问答。
提示：加 !deep 前缀可扩大检索深度（如：!deep 机器人如何解析消息）。"""


class BotService:
    def __init__(self, cfg: AppConfig, store: Store,
                 memory: Optional[MemoryManager] = None,
                 llm: Optional[LLMClient] = None):
        self.cfg = cfg
        self.store = store
        self.memory = memory or MemoryManager(store, cfg.memory)
        self.llm = llm or LLMClient(cfg.llm)
        self.memory_enabled = bool(cfg.memory.get("enabled", True))
        self.kb = None
        try:
            if cfg.knowledge_enabled:
                from .knowledge import KnowledgeStore
                self.kb = KnowledgeStore(cfg.knowledge_db_path)
        except Exception:
            self.kb = None

    # ---------------- 索引保障 ----------------
    def _ensure_index(self, ws: WorkspaceConfig, force: bool = False) -> Optional[Dict[str, Any]]:
        stats = self.store.stats(ws.id)
        if force or stats["chunks"] == 0:
            return indexer.index_workspace(self.store, ws, full=True)
        if ws.index.auto_reindex_on_query:
            ch = indexer.compute_changes(self.store, ws)
            if ch.has_changes:
                return indexer.index_workspace(self.store, ws, full=False)
        return None

    # ---------------- 统一入口 ----------------
    async def handle(self, workspace_id: str, text: str, session_id: str = "",
                     deep: bool = False, history: Optional[List[Dict[str, Any]]] = None) -> Answer:
        raw_text = text or ""
        text = (text or "").strip()
        if not text:
            return Answer(text="请输入问题，或发送 /help 查看命令。")
        if text.startswith("/"):
            return self._handle_command(workspace_id, text)

        deep = deep or text.startswith("!deep")
        if text.startswith("!deep"):
            text = text[len("!deep"):].strip()

        try:
            ws = self.cfg.workspace(workspace_id or None)
        except (KeyError, ValueError) as exc:
            return Answer(text=f"工作区错误：{exc}")

        # 留痕开始
        tracer: Optional[Tracer] = None
        try:
            tracer = Tracer(self.store, ws.id, raw_text, session_id)
            tracer.question(raw_text, text)
        except Exception:
            tracer = None

        # 多轮：取出本会话最近几条消息；先结合上下文把当前话改写成独立问题
        if history is None and session_id:
            try:
                history = self.store.get_messages(session_id, limit=8)
            except Exception:
                history = []
        effective_question = text
        retrieval_query = text
        resolved_by = "none"
        if history:
            heuristic = f"{next((m.get('content','') for m in reversed(history) if m.get('role')=='user'), '')} {text}".strip()
            retrieval_query = await self._resolve_query(text, history)
            effective_question = retrieval_query or text
            resolved_by = "heuristic" if retrieval_query.strip() == heuristic.strip() else "llm"
        if tracer:
            tracer.resolved(len(history or []), retrieval_query, effective_question, resolved_by)

        t0 = time.time()
        index_result = self._ensure_index(ws)
        if tracer:
            tracer.index_info(index_result)

        budget = max(ws.index.token_budget, 3500) * (2 if deep else 1)
        deep_k = max(ws.index.deep_read_top_k, 3) * (2 if deep else 1)
        use_kb = False
        if self.kb is not None:
            try:
                from .knowledge.build import ws_kb_id
                use_kb = self.kb.counts(ws_kb_id(ws.id, ws.name))["chunks"] > 0
            except Exception:
                use_kb = False
        if use_kb:
            from .knowledge import query as kb_query
            context, citations, used = await kb_query.build_context(
                self.kb, self.cfg, ws, retrieval_query,
                budget_tokens=budget, top_k=24 if deep else 16,
                deep_top_k=deep_k, llm=self.llm,
            )
        else:
            context, citations, used = retrieval.build_context(
                self.store, ws.id, ws.path, retrieval_query,
                budget_tokens=budget, top_k=24 if deep else 16, deep_top_k=deep_k,
            )
        if tracer:
            tracer.retrieval_info("knowledge" if use_kb else "legacy",
                                  retrieval_query, context, citations, budget)

        mem_block, mem_hits = ("", [])
        if self.memory_enabled:
            mem_block, mem_hits = self.memory.relevant(ws.id, text)

        # 同一问题答过 → 直接复用历史答案（省一次模型调用；带上原来源）
        mem_answer = self._find_same_question_answer(ws.id, text)
        if mem_answer:
            from .models import Citation as _Cit
            old_cites: List[Citation] = []
            for c in (mem_answer.get("citations") or []):
                try:
                    old_cites.append(_Cit(path=c.get("path", ""),
                                          start_line=int(c.get("start_line", 1)),
                                          end_line=int(c.get("end_line", 1)),
                                          symbol=c.get("symbol", ""),
                                          kind=c.get("kind", ""),
                                          score=float(c.get("score", 0))))
                except Exception:
                    pass
            if tracer:
                tracer.event("cache", {"hit": True, "why": "same_question_in_memory",
                                       "reused_sources": len(old_cites)})
                tracer.answer(mem_answer["answer"], old_cites, False, tracer.elapsed_ms())
            return Answer(text=mem_answer["answer"], workspace=ws.id,
                          trace_id=tracer.trace_id if tracer else "",
                          citations=old_cites, changes=(index_result or {}),
                          memory_hits=mem_hits[:3],
                          used_context_tokens=estimate_tokens(mem_answer["answer"]),
                          full_corpus_tokens=self.store.sum_full_tokens(ws.id),
                          elapsed_ms=int((time.time() - t0) * 1000))

        if not context and not mem_block:
            answer_text = ("没有在当前工作区索引中找到相关内容。可以尝试 /index 重建索引，"
                           "或用更具体的关键词（类名、函数名、模块名）提问。")
            self._record(ws, session_id, text, answer_text, [], cacheable=False)
            if tracer:
                tracer.answer(answer_text, [], False, tracer.elapsed_ms())
            return Answer(text=answer_text, workspace=ws.id, trace_id=tracer.trace_id if tracer else "",
                          changes=(index_result or {}),
                          full_corpus_tokens=self.store.sum_full_tokens(ws.id),
                          elapsed_ms=int((time.time() - t0) * 1000))

        # 证据门槛：候选与问题关键实体几乎不重合时，宁可不硬答（避免靠"词像"或幻觉作答）
        # 有历史记忆命中（同类问题答过）时放行，避免把“已答过的问题”误拦。
        coverage = self._evidence_coverage(effective_question, context, citations)
        min_cov = float((self.cfg.knowledge or {}).get("retrieve", {}).get("min_coverage",
                         self.cfg.knowledge.get("min_coverage", 0.2) if isinstance(self.cfg.knowledge, dict) else 0.2))
        # 证据门槛：候选与问题实体几乎不重合 → 不硬答（记忆不能用来绕过，避免把幻觉当答案）
        if context and coverage < min_cov and len(effective_question) >= 6:
            hint = self._insufficient_answer(effective_question, coverage, citations)
            self._record(ws, session_id, text, hint, [], cacheable=False)
            if tracer:
                tracer.event("gate", {"coverage": round(coverage, 3), "min_coverage": min_cov,
                                      "action": "insufficient_evidence"})
                tracer.answer(hint, citations[:3], False, tracer.elapsed_ms())
            return Answer(text=hint, workspace=ws.id, trace_id=tracer.trace_id if tracer else "",
                          citations=citations[:3], changes=(index_result or {}),
                          used_context_tokens=used,
                          full_corpus_tokens=self.store.sum_full_tokens(ws.id),
                          elapsed_ms=int((time.time() - t0) * 1000))

        if self.llm.available:
            user_prompt = self._build_prompt(ws, effective_question, context, mem_block, history)
            if tracer:
                tracer.prompt(SYSTEM_PROMPT, user_prompt)
            _llm_t0 = time.time()
            llm_out = await self.llm.chat(SYSTEM_PROMPT, user_prompt)
            _llm_ms = int((time.time() - _llm_t0) * 1000)
            if llm_out and not llm_out.startswith("__LLM_ERROR__"):
                answer_text = llm_out
                llm_used = True
                llm_provider = self.llm.name
                if tracer:
                    tracer.llm_result(True, llm_out, self.llm.last_raw_finish,
                                      self.llm.last_raw_usage, _llm_ms)
            else:
                answer_text = self._extractive_answer(text, context, citations)
                if llm_out and llm_out.startswith("__LLM_ERROR__"):
                    answer_text = (f"（大模型调用失败，已降级为检索摘要；原因：{self.llm.last_error}）\n"
                                   + answer_text)
                llm_used = False
                llm_provider = ""
                if tracer:
                    tracer.llm_result(False, answer_text, self.llm.last_raw_finish,
                                      self.llm.last_raw_usage, _llm_ms, self.llm.last_error)
        else:
            answer_text = self._extractive_answer(text, context, citations)
            llm_used = False
            llm_provider = ""
            if tracer:
                tracer.llm_result(False, answer_text, None, {}, 0, "llm 未配置，走检索摘要")

        if llm_used:
            citations = self._filter_used_citations(answer_text, citations)

        self._record(ws, session_id, text, answer_text,
                     [c.to_dict() for c in citations])
        if tracer:
            tracer.answer(answer_text, citations, llm_used, tracer.elapsed_ms())

        return Answer(
            text=answer_text,
            workspace=ws.id,
            trace_id=tracer.trace_id if tracer else "",
            citations=citations,
            memory_hits=mem_hits,
            changes=(index_result or {}),
            used_context_tokens=used + (estimate_tokens(mem_block) if mem_block else 0),
            full_corpus_tokens=self.store.sum_full_tokens(ws.id),
            llm_used=llm_used,
            llm_provider=llm_provider,
            elapsed_ms=int((time.time() - t0) * 1000),
        )

    # ---------------- 证据门槛 ----------------
    def _find_same_question_answer(self, workspace_id: str, question: str) -> Optional[Dict[str, Any]]:
        """若历史里问过“同一个问题”（规范化后完全一致），直接复用那次答案。"""
        import re as _re
        norm = lambda s: _re.sub(r"[\s，。？！,.?!、：:；;]", "", (s or "").lower())
        q = norm(question)
        if len(q) < 6:
            return None
        try:
            items = self.store.all_qa(workspace_id, limit=300)
        except Exception:
            return None
        for qa in items:
            if norm(qa.get("question", "")) != q:
                continue
            ans = (qa.get("answer") or "").strip()
            if not ans:
                continue
            # 只复用“正经答案”：不复用“没找到/证据不足/降级摘要”这类负结果，避免错误固化
            if any(x in ans for x in ("没有找到", "现有知识库中没有", "未找到",
                                      "已降级为检索摘要", "没有在当前工作区索引中")):
                continue
            return {"answer": ans, "question": qa.get("question", ""),
                    "citations": qa.get("citations") or []}
        return None
    def _memory_is_strong(self, question: str, mem_hits: List[str]) -> bool:
        """问题与历史记忆是否“几乎是同一个问题”：去掉空白后高度重合（>=0.75）。"""
        if not mem_hits:
            return False
        import re as _re
        q = _re.sub(r"\s+", "", (question or "").lower())
        if len(q) < 6:
            return False
        for m in mem_hits:
            body = _re.sub(r"\s+", "", str(m).lower())
            if q in body or body.find(q) >= 0:
                return True
        return False
    # 中文虚词/助词，用于把问题切成实词片段
    _ZH_SPLIT = re.compile(
        r"的|了|吗|呢|吧|啊|是|在|把|被|和|与|及|或|对|从|向|为|这|那|个|请|问|如|何|怎|样|什|么|哪|些|能|可|要|会|就|都|也|还|又|很|再|所|以|而|但|并|等|中|上|下|里|后|前|先|如何|怎么|怎样|什么|为什么|哪些|请问")
    _ZH_STOP = {"函数", "方法", "参数", "时候", "问题", "一下", "东西", "情况", "超频"}

    def _evidence_coverage(self, question: str, context: str, citations: List[Citation]) -> float:
        """问题“有区分度的实体”在候选里出现的比例。0~1。

        - 英文标识符逐个精确匹配（强实体）；
        - 中文按虚词切成分片后，对每个分片做“多粒度”匹配（整词 / 其 2~3 字核心子词），
          只要某一粒度在候选里出现即算命中，避免因说法略变就整词不中。
        """
        import re as _re
        q = (question or "").strip()
        ascii_terms = [t for t in _re.findall(r"[A-Za-z_][A-Za-z0-9_.]{2,}", q)]

        zh_units: List[str] = []
        for seg in _re.findall(r"[\u4e00-\u9fff]+", q):
            for part in self._ZH_SPLIT.split(seg):
                part = part.strip()
                if len(part) >= 2 and part not in self._ZH_STOP and part not in zh_units:
                    zh_units.append(part)

        hay = ((context or "") + " " + " ".join(f"{c.path} {c.symbol}" for c in citations)).lower()

        def unit_hit(u: str) -> bool:
            if u.lower() in hay:
                return True
            # 多粒度：整词不中时，退回其 2~3 字子词（只对中文）
            if re.fullmatch(r"[\u4e00-\u9fff]+", u):
                for n in (3, 2):
                    for i in range(len(u) - n + 1):
                        sub = u[i:i + n]
                        if sub not in self._ZH_STOP and sub in hay:
                            return True
            return False

        if ascii_terms:
            terms = list(dict.fromkeys(ascii_terms))[:10]
            hit = sum(1 for t in terms if t.lower() in hay)
            return hit / len(terms)
        if not zh_units:
            return 1.0
        terms = zh_units[:8]
        hit = sum(1 for u in terms if unit_hit(u))
        return hit / len(terms)

    def _insufficient_answer(self, question: str, coverage: float,
                             citations: List[Citation]) -> str:
        near = "；".join(c.path for c in citations[:3]) if citations else "无"
        return (
            f"现有知识库中没有找到能直接回答「{question}」的内容（证据覆盖度约 {coverage*100:.0f}%）。"
            "为避免臆测，这里不强行作答。\n"
            f"检索到的相近内容仅供定位参考：{near}。\n"
            "建议：换个更具体的类名/函数名/表名/配置项提问；或确认该目录已纳入知识库并执行 kb-build。"
        )

    # ---------------- 记忆落库 ----------------
    def _record(self, ws: WorkspaceConfig, session_id: str, question: str,
                answer: str, citations: List[Dict[str, Any]],
                cacheable: bool = True) -> None:
        """落库。cacheable=False（负结果/拦截/无证据）时只写对话记录，不写 qa 记忆，
        避免把“没找到”当作可复用的历史答案。"""
        if self.memory_enabled and cacheable:
            try:
                self.memory.add_qa(ws.id, session_id, question, answer, citations)
            except Exception:
                pass
        if session_id:
            try:
                self.store.append_message(session_id, ws.id, "user", question)
                self.store.append_message(session_id, ws.id, "assistant", answer)
            except Exception:
                pass

    # ---------------- 多轮理解 ----------------
    def _needs_context(self, question: str) -> bool:
        q = (question or "").strip()
        if len(q) <= 14:
            return True
        return any(k in q for k in ("它", "他", "她", "这个", "那个", "上面", "刚才",
                                    "该", "此", "继续", "再说", "还有", "呢", "然后"))

    async def _resolve_query(self, text: str, history: List[Dict[str, Any]]) -> str:
        """把依赖上下文的一句话改写成可独立检索的问题。"""
        recent = history[-6:]
        prev_user = next((m.get("content", "") for m in reversed(recent)
                          if m.get("role") == "user"), "")
        heuristic = f"{prev_user} {text}".strip() if (prev_user and self._needs_context(text)) else text
        if not self.llm.available:
            return heuristic
        convo = []
        for m in recent:
            role = "用户" if m.get("role") == "user" else "助手"
            convo.append(f"{role}: {clip(m.get('content', ''), 300)}")
        system = (
            "你是对话理解助手。先结合最近对话判断用户当前这句话在做什么"
            "（追问上一话题 / 纠正上一条 / 开启新话题 / 普通提问），再把它改写成一句"
            "不依赖上下文、可直接用于检索代码库的独立问题；补全其中的指代（它/这个/那个/刚才/上面）"
            "和省略的主语。若当前这句话本身已完整、或是全新话题，则原样输出。"
            "只输出改写后的问题本身，不要解释、不要引号、不要前缀。"
        )
        user = "最近对话：\n" + "\n".join(convo) + f"\n\n当前这句话：{text}\n\n改写后的独立问题："
        out = await self.llm.chat(system, user)
        if out and not str(out).startswith("__LLM_ERROR__"):
            line = str(out).strip().splitlines()[0].strip().strip("“”\"' ")
            if 0 < len(line) <= 200:
                return line
        return heuristic

    # ---------------- 组装提示词 ----------------
    def _build_prompt(self, ws: WorkspaceConfig, question: str,
                      context: str, mem_block: str,
                      history: Optional[List[Dict[str, Any]]] = None) -> str:
        parts = [f"工程：{ws.name}（{ws.path}）"]
        if history:
            hist_lines = ["最近对话（用于理解当前问题在指什么；如为追问请延续主题，不要照抄）："]
            for m in history[-6:]:
                role = "用户" if m.get("role") == "user" else "助手"
                hist_lines.append(f"{role}: {clip(m.get('content', ''), 220)}")
            parts += ["", "\n".join(hist_lines)]
        parts += [
            f"当前问题（已结合上下文理解为）：{question}",
            "",
            "下面是从工程里检索到的候选片段。它们只是候选，可能只是关键词相近、实际无关。"
            "请先自行判断哪些真正能回答问题，忽略无关片段；再基于真正相关的内容作答。",
            "",
            context or "（没有检索到候选片段）",
        ]
        if mem_block:
            parts += ["", mem_block]
        parts += [
            "",
            "输出要求：",
            "- 先给一句话结论/定位，再用 2-4 段连贯说明（是什么、怎么工作/流程、边界或注意点），"
            "像给同事讲解，而不是检索结果清单；",
            "- 不要逐条罗列片段，不要写“依据1/2/3”，不要复制代码，不要出现“[片段N]”这类编号；",
            "- 引用只在相关句子末尾用括号标注来源，最多 3 个，格式 path:line-line；",
            "- 不要使用 Markdown 符号（**、#、反引号）；",
            "- 总长 300-600 字；若候选不足以回答，用一句话说明还缺什么，不要硬凑。",
        ]
        return "\n".join(parts)

    def _filter_used_citations(self, text: str, citations: List[Citation],
                               limit: int = 5) -> List[Citation]:
        used = [c for c in citations if c.path and c.path in text]
        if used:
            return used[:limit]
        return citations[:min(3, len(citations))]

    # ---------------- 无 LLM 降级 ----------------
    def _extractive_answer(self, question: str, context: str,
                           citations: List[Citation]) -> str:
        lines = [f"针对「{question}」，在当前工程中匹配到 {len(citations)} 处相关内容："]
        for i, c in enumerate(citations[:6], start=1):
            lines.append(f"{i}. {c.path}:{c.start_line}-{c.end_line}")
        if context:
            lines += ["", "最相关片段：", clip(context, 700)]
        lines += ["", "（未配置大模型，以上为检索摘要；配置 llm 后可生成自然语言答案。）"]
        return "\n".join(lines)

    # ---------------- 命令 ----------------
    def _handle_command(self, workspace_id: str, text: str) -> Answer:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower()
        try:
            ws = self.cfg.workspace(workspace_id or None)
        except (KeyError, ValueError) as exc:
            return Answer(text=f"工作区错误：{exc}")

        if cmd in ("/help", "/?"):
            return Answer(text=HELP_TEXT)

        if cmd == "/ws":
            lines = ["可用工作区："]
            for w in self.cfg.workspaces:
                mark = " (默认)" if w.id == self.cfg.default_workspace else ""
                lines.append(f"- {w.id}: {w.name}  ->  {w.path}{mark}")
            return Answer(text="\n".join(lines))

        if cmd == "/status":
            s = self.store.stats(ws.id)
            ch = indexer.compute_changes(self.store, ws).to_dict()
            text_out = (
                f"工作区 {ws.id}（{ws.name}）\n"
                f"路径：{ws.path}\n"
                f"已索引文件：{s['indexed_files']}，代码块：{s['chunks']}，"
                f"索引 token：{s['chunk_tokens']}，全量 token 估算：{self.store.sum_full_tokens(ws.id)}\n"
                f"记忆：问答 {s['qa_memory']} 条，事实 {s['facts']} 条\n"
                f"待处理变更：新增 {len(ch['added'])} / 修改 {len(ch['modified'])} / 删除 {len(ch['deleted'])}"
            )
            return Answer(text=text_out, workspace=ws.id, changes=ch)

        if cmd == "/index":
            full = "full" in arg.lower()
            res = indexer.index_workspace(self.store, ws, full=full)
            retrieval.invalidate(ws.id)
            return Answer(
                text=(f"索引完成（{'全量' if full else '增量'}）：扫描 {res['scanned_files']}，"
                      f"写入 {res['indexed']}，删除 {res['deleted']}，代码块 {res['chunks_written']}，"
                      f"索引 token {res['chunk_tokens']} / 全量 {res['corpus_tokens']}"
                      f"（节省 {res['token_saving_ratio']*100:.1f}%）"),
                workspace=ws.id, changes=res,
                used_context_tokens=res["chunk_tokens"],
                full_corpus_tokens=res["corpus_tokens"],
            )

        if cmd == "/changes":
            ch = indexer.compute_changes(self.store, ws)
            d = ch.to_dict()
            lines = [f"变更统计：新增 {len(d['added'])} / 修改 {len(d['modified'])} / 删除 {len(d['deleted'])}"]
            for tag, items in (("新增", d["added"]), ("修改", d["modified"]), ("删除", d["deleted"])):
                for p in items[:20]:
                    lines.append(f"  [{tag}] {p}")
            if not ch.has_changes:
                lines.append("（无变更）")
            return Answer(text="\n".join(lines), workspace=ws.id, changes=d)

        if cmd == "/remember":
            content = arg.strip()
            if not content:
                return Answer(text="用法：/remember 要记住的内容")
            self.memory.add_fact(ws.id, content, source="user")
            return Answer(text=f"已记住：{content}", workspace=ws.id)

        if cmd == "/facts":
            facts = self.store.all_facts(ws.id, limit=50)
            if not facts:
                return Answer(text="暂无记忆事实。", workspace=ws.id)
            lines = ["已记忆事实："] + [f"- {f['text']}" for f in facts]
            return Answer(text="\n".join(lines), workspace=ws.id)

        return Answer(text=f"未知命令：{cmd}。发送 /help 查看可用命令。")
