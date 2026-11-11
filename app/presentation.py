"""回答展示偏好与输出清理，不改变内部证据和评审。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_NO_CODE = (
    "不需要展示代码", "不用展示代码", "不要展示代码", "不展示代码", "不需要代码", "不用代码",
    "不要代码", "无需代码", "别贴代码", "不贴代码", "不要出现代码", "不要函数名", "不用函数名",
    "不要字段名", "不用字段名", "不要变量名", "不要代码细节", "不要实现细节", "不展示实现细节",
    "no code", "without code", "don't show code", "do not show code", "no function names",
)
_NEED_CODE = (
    "需要代码", "展示代码", "给出代码", "显示代码", "代码示例", "需要函数名", "需要字段名",
    "show code", "with code", "code example",
)
_CITATIONS = ("引用", "来源", "出处", "行号", "定位到", "文件路径", "展示依据", "列出依据", "cite", "citation", "source")
_DETAILED = ("详细", "展开", "深入", "完整分析", "逐条", "detailed", "in depth")


def response_preferences(question: str, history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, bool]:
    current = (question or "").lower()

    def no_code(text: str) -> bool:
        return any(x in text for x in _NO_CODE)

    def needs_code(text: str) -> bool:
        return any(x in text for x in _NEED_CODE)

    hide_code = no_code(current)
    if not no_code(current) and not needs_code(current):
        for message in reversed(history or []):
            if message.get("role") != "user":
                continue
            text = str(message.get("content", "")).lower()
            if no_code(text) or needs_code(text):
                hide_code = no_code(text)
                break
    return {
        "no_code": hide_code,
        "inline_citations": any(x in current for x in _CITATIONS),
        "detailed": any(x in current for x in _DETAILED),
    }


def without_presentation_instructions(question: str) -> str:
    value = question or ""
    value = re.sub(
        r"(?:也)?(?:不要|不用|不需要|无需|别)(?:展示|显示|出现|提供|列出|贴)?"
        r"(?:代码|伪代码|函数名|类名|字段名|变量名|实现细节)(?:细节)?",
        "", value, flags=re.I)
    value = re.sub(r"(?:no|without|don't show|do not show)\s+code", "", value, flags=re.I)
    value = re.sub(r"^[，,。；;\s]+|[，,。；;\s]+$", "", value)
    return value.strip() or question


def has_code_details(text: str) -> bool:
    value = text or ""
    patterns = (
        r"```", r"`[^`]+`", r"[\w./\\-]+\.[A-Za-z0-9]+:\d+",
        r"\b[A-Za-z_]\w*\s*\([^\n)]*\)", r"\b[A-Za-z_]\w*\.[A-Za-z_]\w*\b",
        r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b",
    )
    return any(re.search(p, value) for p in patterns)


def strip_code_details(text: str) -> str:
    value = re.sub(r"```[^\n]*\n?.*?```", "", text or "", flags=re.S)
    value = re.sub(r"`[^`]+`", "", value)
    value = re.sub(r"(?:见|参见|依据|来源)?\s*[\w./\\-]+\.[A-Za-z0-9]+:\d+(?:-\d+)?", "", value)
    value = re.sub(r"\b[A-Za-z_]\w*\s*\([^\n)]*\)", "", value)
    value = re.sub(r"\b[A-Za-z_]\w*\.[A-Za-z_]\w*\b", "", value)
    value = re.sub(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def strip_inline_citations(text: str) -> str:
    value = re.sub(
        r"\s*(?:见|参见|依据|来源)[:：]?\s*[\w./\\-]+\.[A-Za-z0-9]+:\d+(?:-\d+)?",
        "", text or "")
    value = re.sub(r"\s*[（(][^（）()\n]*[\w./\\-]+\.[A-Za-z0-9]+:\d+(?:-\d+)?[^（）()\n]*[）)]", "", value)
    value = re.sub(r"\s*[\w./\\-]+\.[A-Za-z0-9]+:\d+(?:-\d+)?", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    return value.strip()
