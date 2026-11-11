"""问答门槛和过程评估共用的词面证据检查；用于排除明显无关的召回。"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{2,}")
_SCOPE_PREFIX = re.compile(
    r"([A-Za-z_][A-Za-z0-9_.-]{2,})\s*(?:工程|项目|代码库|知识库)(?:中|内|里|的)?")
_ZH_SPLIT = re.compile(
    r"如何|怎么|怎样|什么|为什么|哪些|请问|的|了|吗|呢|吧|啊|是|在|把|被|和|与|及|或|对|"
    r"从|向|为|这|那|个|请|问|如|何|怎|样|什|么|哪|些|能|可|要|会|就|都|也|还|又|很|再|"
    r"所|以|而|但|并|等|中|上|下|里|后|前|先")
_ZH_GENERIC = re.compile(r"代码库|知识库|工程|项目|场景|机制|实现|逻辑")
_ZH_STOP = {"函数", "方法", "参数", "时候", "问题", "一下", "东西", "情况",
            "信息", "处理", "管理", "普通", "具体"}


def identifier_hit(term: str, text: str) -> bool:
    """避免 Service2 被当成 Service、UnknownService 被当成 Service。"""
    return bool(re.search(r"(?<![a-z0-9_])" + re.escape(term.lower()) + r"(?![a-z0-9_])",
                          text.lower()))


def evidence_coverage(question: str, context: str, citations: Iterable[Any], *,
                      original_question: str | None = None,
                      scope_terms: Iterable[str] = ()) -> Dict[str, Any]:
    """同时检查中文主题与代码标识符，工程名称只作为检索范围。

    多轮改写补入的工程名不应挤掉用户的中文主题；用户明确指定的函数等标识符
    仍需命中，不能仅凭“补全”“处理”等通用内容放行。词面命中不代表证据完整。
    """
    q = (question or "").strip()
    scopes = {str(term).lower() for term in scope_terms if term}
    scopes.update(match.group(1).lower() for match in _SCOPE_PREFIX.finditer(q))
    ignored = list(dict.fromkeys(t for t in _IDENTIFIER.findall(q) if t.lower() in scopes))
    for scope in sorted(scopes, key=len, reverse=True):
        q = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(scope) + r"(?![A-Za-z0-9_])",
                   " ", q, flags=re.I)

    ascii_terms = list(dict.fromkeys(_IDENTIFIER.findall(q)))[:10]
    zh_terms: List[str] = []
    for segment in re.findall(r"[\u4e00-\u9fff]+", _ZH_GENERIC.sub(" ", q)):
        for part in _ZH_SPLIT.split(segment):
            if len(part) >= 2 and part not in _ZH_STOP and part not in zh_terms:
                zh_terms.append(part)
    terms = ascii_terms + zh_terms[:8]

    labels = []
    for citation in citations:
        if isinstance(citation, dict):
            labels.append(f"{citation.get('path', '')} {citation.get('symbol', '')}")
        else:
            labels.append(f"{citation.path} {citation.symbol}")
    hay = ((context or "") + " " + " ".join(labels)).lower()

    def hit(term: str) -> bool:
        if term in ascii_terms:
            return identifier_hit(term, hay)
        if term.lower() in hay:
            return True
        # 中文短语变体如“券补全”与“券信息补全”共享核心子词。
        return any(term[i:i + size] not in _ZH_STOP and term[i:i + size] in hay
                   for size in (3, 2) for i in range(len(term) - size + 1))

    covered = [term for term in terms if hit(term)]
    coverage = len(covered) / len(terms) if terms else 1.0
    explicit = question if original_question is None else original_question
    required = list(dict.fromkeys(t for t in _IDENTIFIER.findall(explicit or "")
                                  if t.lower() not in scopes))
    if required:
        coverage = min(coverage, sum(identifier_hit(t, hay) for t in required) / len(required))
    return {"coverage": coverage, "terms": terms, "covered": covered,
            "missing": [term for term in terms if term not in covered],
            "required_identifiers": required, "ignored_scope": ignored}
