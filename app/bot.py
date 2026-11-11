"""问答编排：命令路由 + 增量索引 + 检索 + 记忆 + LLM/降级回答。"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import indexer, retrieval
from .config import AppConfig, WorkspaceConfig
from .evidence import evidence_coverage
from .llm import LLMClient
from .memory import MemoryManager
from .models import Answer, Citation
from .store import Store
from .trace import Tracer
from .utils import clip, estimate_tokens

SYSTEM_PROMPT = (
    "你是一名资深软件工程师，负责向同事讲清楚这个工程。"
    "关于当前工程的事实必须由提供的候选片段支持，不得编造不存在的文件、函数或行为。"
    "历史对话与记忆只用于消解指代或理解用户偏好，不能代替当前代码证据；候选没有覆盖的部分要明确说明。"
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

        retrieve_cfg = (self.cfg.knowledge or {}).get("retrieve", {}) or {}
        budget = max(1, int(retrieve_cfg.get("budget_tokens", 4000))) * (2 if deep else 1)
        top_k = max(1, int(retrieve_cfg.get("top_k", 40))) * (2 if deep else 1)
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
                budget_tokens=budget, top_k=top_k,
                deep_top_k=deep_k, llm=self.llm, deep=deep,
            )
        else:
            context, citations, used = retrieval.build_context(
                self.store, ws.id, ws.path, retrieval_query,
                budget_tokens=budget, top_k=top_k, deep_top_k=deep_k,
            )
        if tracer:
            tracer.retrieval_info("knowledge" if use_kb else "legacy",
                                  retrieval_query, context, citations, budget)

        mem_block, mem_hits = ("", [])
        if self.memory_enabled:
            mem_block, mem_hits = self.memory.relevant(ws.id, text)

        if not context:
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
        evidence = evidence_coverage(
            effective_question, context, citations, original_question=text,
            scope_terms=(ws.id, ws.name, Path(ws.path).name))
        coverage = evidence["coverage"]
        min_cov = float((self.cfg.knowledge or {}).get("retrieve", {}).get("min_coverage",
                         self.cfg.knowledge.get("min_coverage", 0.2) if isinstance(self.cfg.knowledge, dict) else 0.2))
        if tracer:
            tracer.event("evidence", {**evidence, "min_coverage": min_cov})
        # 记忆不能绕过门槛；短问题也检查，工程名不作为业务证据。
        if coverage < min_cov:
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

        # 只在当前检索通过证据门槛后复用缓存；分析问题、索引变化或证据来源变化都重新生成。
        mem_answer = None
        if not index_result and not self._is_analytical_question(effective_question):
            mem_answer = self._find_same_question_answer(ws.id, text, citations)
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

        if self.llm.available:
            user_prompt = self._build_prompt(ws, effective_question, context, mem_block, history)
            if tracer:
                tracer.prompt(SYSTEM_PROMPT, user_prompt)
            _llm_t0 = time.time()
            llm_out = await self.llm.chat(SYSTEM_PROMPT, user_prompt)
            _llm_ms = int((time.time() - _llm_t0) * 1000)
            if llm_out and not llm_out.startswith("__LLM_ERROR__"):
                answer_text = llm_out
                answer_finish = self.llm.last_raw_finish
                answer_usage = dict(self.llm.last_raw_usage or {})
                review_report = None
                if self._is_analytical_question(effective_question) and bool(
                        (self.cfg.knowledge or {}).get("answer_review", True)):
                    review_t0 = time.time()
                    answer_text, review_report, more_citations, more_used = await self._review_and_revise(
                        ws, effective_question, retrieval_query, context, citations,
                        mem_block, history, answer_text, use_kb,
                        max(0, budget - used), top_k, deep_k, deep)
                    used += more_used
                    citations.extend(more_citations)
                    _llm_ms += int((time.time() - review_t0) * 1000)
                llm_used = True
                llm_provider = self.llm.name
                if tracer:
                    tracer.llm_result(True, llm_out, answer_finish,
                                      answer_usage, _llm_ms)
                    if review_report is not None:
                        tracer.event("quality_review", review_report)
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

        cited_in_answer = any(c.path and c.path in answer_text for c in citations)
        self._record(ws, session_id, text, answer_text,
                     [c.to_dict() for c in citations],
                     cacheable=(llm_used and len(answer_text) >= 120
                                and bool(citations) and cited_in_answer))
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
    def _find_same_question_answer(self, workspace_id: str, question: str,
                                   current_citations: List[Citation]
                                   ) -> Optional[Dict[str, Any]]:
        """仅复用有实质内容、且来源仍命中当前检索结果的旧答案。"""
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
            if len(ans) < 120:
                continue
            # 只复用“正经答案”：不复用“没找到/证据不足/降级摘要”这类负结果，避免错误固化
            if any(x in ans for x in ("没有找到", "现有知识库中没有", "未找到",
                                      "已降级为检索摘要", "没有在当前工作区索引中",
                                      "未配置大模型", "以上为检索摘要")):
                continue
            old = qa.get("citations") or []
            if not old or not current_citations or not any(
                    str(c.get("path", "")) in ans for c in old if isinstance(c, dict)):
                continue
            still_supported = False
            for c in old:
                if not isinstance(c, dict):
                    continue
                try:
                    start = int(c.get("start_line", 1))
                    end = int(c.get("end_line", 1))
                except (TypeError, ValueError):
                    continue
                if any(str(c.get("path", "")) == current.path
                       and max(start, current.start_line) <= min(end, current.end_line)
                       for current in current_citations):
                    still_supported = True
                    break
            if not still_supported:
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
    @staticmethod
    def _is_analytical_question(question: str) -> bool:
        markers = ("分析", "为什么", "原因", "根因", "原理", "机制", "流程", "链路",
                   "架构", "设计", "对比", "差异", "影响", "排查", "优化", "如何实现", "怎么实现")
        return any(marker in (question or "") for marker in markers)

    async def _review_and_revise(self, ws: WorkspaceConfig, question: str,
                                 retrieval_query: str, context: str,
                                 current_citations: List[Citation], mem_block: str,
                                 history: Optional[List[Dict[str, Any]]], draft: str,
                                 use_kb: bool, remaining_budget: int, top_k: int,
                                 deep_top_k: int, deep: bool
                                 ) -> tuple[str, Dict[str, Any], List[Citation], int]:
        """分析类问题：核对覆盖度，按遗漏项补检索，再用证据修订。"""
        review_system = (
            "你是代码问答质量检查器。只判断回答是否切题、是否覆盖问题要求、是否有结论缺少候选证据。"
            '只输出 JSON：{"relevant":true,"complete":true,"missing":[],"unsupported":[]}。'
            "missing 只写用户明确要求但回答遗漏的要点；unsupported 只写无法由给定片段支持的关键断言。"
        )
        review_user = (
            f"问题：{question}\n\n当前代码证据：\n{context[:16000]}\n\n"
            f"草稿：\n{draft[:6000]}\n\n请检查相关性、覆盖度和证据支持。"
        )
        raw = await self.llm.chat(review_system, review_user)
        report: Dict[str, Any] = {"checked": False, "revised": False}
        if not raw or str(raw).startswith("__LLM_ERROR__"):
            return draft, report, [], 0
        try:
            match = re.search(r"\{.*\}", str(raw), re.S)
            review = json.loads(match.group(0) if match else str(raw))
            if not isinstance(review, dict):
                return draft, report, [], 0
        except (ValueError, TypeError):
            return draft, report, [], 0

        missing = [str(x) for x in (review.get("missing") or []) if str(x).strip()]
        unsupported = [str(x) for x in (review.get("unsupported") or []) if str(x).strip()]
        report = {"checked": True, "revised": False,
                  "relevant": bool(review.get("relevant", True)),
                  "complete": bool(review.get("complete", True)),
                  "missing": missing[:5], "unsupported": unsupported[:5]}
        needs_revision = (not report["relevant"] or not report["complete"]
                          or bool(missing) or bool(unsupported))
        if not needs_revision:
            return draft, report, [], 0

        added_context = ""
        added_citations: List[Citation] = []
        added_tokens = 0
        if (missing or unsupported) and remaining_budget > 0:
            targeted_query = " ".join([retrieval_query, *missing, *unsupported]).strip()
            try:
                if use_kb and self.kb is not None:
                    from .knowledge import query as kb_query
                    added_context, added_citations, added_tokens = await kb_query.build_context(
                        self.kb, self.cfg, ws, targeted_query,
                        budget_tokens=remaining_budget, top_k=top_k,
                        deep_top_k=deep_top_k, llm=self.llm, deep=deep)
                else:
                    added_context, added_citations, added_tokens = retrieval.build_context(
                        self.store, ws.id, ws.path, targeted_query,
                        budget_tokens=remaining_budget, top_k=top_k,
                        deep_top_k=deep_top_k)
            except Exception:
                added_context, added_citations, added_tokens = "", [], 0
        if added_context:
            have = {(c.path, c.start_line, c.end_line) for c in current_citations}
            added_citations = [c for c in added_citations
                               if (c.path, c.start_line, c.end_line) not in have]
            report["supplemental_retrieval"] = bool(added_citations)
            if added_citations:
                context = context + "\n\n补充检索证据：\n" + added_context
                report["supplemental_sources"] = [c.to_dict() for c in added_citations]
                report["supplemental_tokens"] = added_tokens
            else:
                added_context = ""
                added_tokens = 0
        else:
            report["supplemental_retrieval"] = False

        prompt = self._build_prompt(ws, question, context, mem_block, history)
        prompt += (
            "\n\n上一版回答的检查结果如下。请在现有证据范围内补齐遗漏、删除无依据断言，"
            "重新完整回答用户问题；证据没有覆盖的部分要明确标为未知，不要猜测。\n"
            f"遗漏：{'; '.join(missing) or '检查器未列出'}\n"
            f"无证据断言：{'; '.join(unsupported) or '检查器未列出'}\n"
            f"上一版回答：\n{draft[:6000]}"
        )
        revised = await self.llm.chat(SYSTEM_PROMPT, prompt)
        if revised and not str(revised).startswith("__LLM_ERROR__"):
            report["revised"] = True
            return str(revised).strip(), report, added_citations, added_tokens
        return draft, report, [], 0

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
            hist_lines = ["最近对话（只用于理解指代和追问关系；历史回答不是当前事实证据）："]
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
            parts += ["", "历史记忆（仅用于补充背景，不可作为当前工程行为的证据）：", mem_block]
        parts += ["", "回答要求："]
        if self._is_analytical_question(question):
            parts += [
                "这是分析类问题。先直接回答核心结论，再按问题需要说明涉及组件及职责、调用/数据流、"
                "关键原因与因果关系、边界或失败路径；逐项回应用户明确提出的子问题。",
                "把代码中可直接确认的事实与推断分开；每个关键事实就近引用来源（path:line-line）。",
                "使用清晰的小标题或分点组织内容，充分解释但避免重复，不受固定字数上限限制。",
                "若证据只覆盖部分问题，先回答有据可答的部分，再明确列出缺失证据；不得靠历史记忆或猜测补齐。",
            ]
        else:
            parts += [
                "先给直接结论，再用简洁说明交代原因或操作路径；回答应覆盖用户问题，不要只复述检索片段。",
                "关键事实就近引用来源（path:line-line）；证据不足时说明具体缺口，不要硬凑。",
            ]
        parts += [
            "候选片段是证据而非答案清单；不要写“依据1/2/3”，不要复制大段代码，也不要输出片段编号。",
            "只引用候选中真实出现的文件与行号；没有证据支持的推断要明确标为推断。",
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
