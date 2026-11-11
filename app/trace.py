"""全链路留痕：记录一次问答从提问到下发的全部关键环节。

阶段（stage）：
  sanitized  用户原始提问（脱敏前的原话）
  intent     多轮理解/改写结果（retrieval_query / effective_question）
  index      索引/变更检测情况
  retrieve   检索到的片段（候选、选中、用到的 token）
  prompt     实际发给大模型的 system + user（完整协议内容）
  llm        大模型返回（原文、finish_reason、usage、耗时）
  answer     最终答案 + 来源
  error      任何环节异常
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from .store import Store
from .utils import estimate_tokens


class Tracer:
    def __init__(self, store: Store, workspace_id: str, question: str, session_id: str = ""):
        self.store = store
        self.trace_id = "tr_" + uuid.uuid4().hex[:16]
        self.workspace_id = workspace_id
        self.session_id = session_id or ""
        self._seq = 0
        self._t0 = time.time()
        self.store.create_trace(self.trace_id, workspace_id, self.session_id, question)

    def event(self, stage: str, data: Any) -> None:
        self._seq += 1
        try:
            self.store.add_event(self.trace_id, self._seq, stage, data)
        except Exception:
            pass

    def elapsed_ms(self) -> int:
        return int((time.time() - self._t0) * 1000)

    # ---------- 便捷封装 ----------
    def question(self, raw: str, cleaned: str) -> None:
        self.event("sanitized", {"raw": raw, "cleaned": cleaned})

    def resolved(self, history_turns: int, retrieval_query: str,
                 effective_question: str, resolved_by: str) -> None:
        self.event("intent", {"history_turns": history_turns,
                              "retrieval_query": retrieval_query,
                              "effective_question": effective_question,
                              "resolved_by": resolved_by})

    def index_info(self, info: Optional[Dict[str, Any]]) -> None:
        self.event("index", info or {"changed": False})

    def retrieval_info(self, source: str, question_used: str, context: str,
                       citations: List[Any], budget: int) -> None:
        self.event("retrieve", {
            "source": source,                       # knowledge | legacy
            "question_used": question_used,
            "budget_tokens": budget,
            "context_tokens": estimate_tokens(context),
            "context_preview": context[:4000],
            "citations": [c.to_dict() if hasattr(c, "to_dict") else c for c in citations],
        })

    def prompt(self, system: str, user: str) -> None:
        self.event("prompt", {
            "system": system,
            "user": user,
            "system_tokens": estimate_tokens(system),
            "user_tokens": estimate_tokens(user),
        })

    def llm_result(self, ok: bool, text: str, finish: Optional[str],
                   usage: Dict[str, Any], elapsed_ms: int, error: str = "") -> None:
        self.event("llm", {
            "ok": ok,
            "content": text,
            "content_tokens": estimate_tokens(text),
            "finish_reason": finish,
            "usage": usage or {},
            "elapsed_ms": elapsed_ms,
            "error": error,
        })

    def answer(self, text: str, citations: List[Any], llm_used: bool,
               elapsed_ms: int) -> None:
        self.event("answer", {
            "text": text,
            "citations": [c.to_dict() if hasattr(c, "to_dict") else c for c in citations],
            "llm_used": llm_used,
            "elapsed_ms": elapsed_ms,
        })

    def error(self, where: str, err: str) -> None:
        self.event("error", {"where": where, "error": err})
