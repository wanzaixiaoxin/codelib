"""面向检索的代码与文档切片，以及文件级结构摘要。"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List

from ..utils import clip, sha256_text


def _line_parts(text: str, start_line: int, *, max_lines: int = 100,
                max_chars: int = 6000, overlap: int = 6
                ) -> List[Dict[str, Any]]:
    lines = text.splitlines()
    if not lines:
        return []
    parts: List[Dict[str, Any]] = []
    i = 0
    while i < len(lines):
        buf: List[str] = []
        chars = 0
        j = i
        while j < len(lines) and len(buf) < max_lines:
            line = lines[j]
            extra = len(line) + (1 if buf else 0)
            if buf and chars + extra > max_chars:
                break
            buf.append(line)
            chars += extra
            j += 1
        if not buf:
            buf = [lines[i]]
            j = i + 1
        parts.append({
            "text": "\n".join(buf).strip(),
            "start_line": start_line + i,
            "end_line": start_line + j - 1,
        })
        if j >= len(lines):
            break
        i = max(i + 1, j - overlap)
    return parts


def symbol_chunks(symbol: Dict[str, Any]) -> List[Dict[str, Any]]:
    body = symbol.get("body") or ""
    start = int(symbol.get("start_line") or 1)
    prefix = "\n".join(x for x in (symbol.get("signature", ""), symbol.get("doc", "")) if x).strip()
    parts = _line_parts(body, start)
    if len(parts) <= 1:
        return [{
            "ref_id": symbol["id"], "lines": f"{start}-{symbol.get('end_line') or start}",
            "symbol": symbol.get("qualname", ""),
            "text": symbol.get("text") or "\n".join(x for x in (prefix, body) if x),
            "hash": symbol.get("hash") or sha256_text(body),
        }]
    out = []
    for n, part in enumerate(parts, 1):
        marker = f"// {symbol.get('qualname', '')} · 第 {n}/{len(parts)} 段"
        text = "\n".join(x for x in (prefix, marker, part["text"]) if x)
        out.append({
            "ref_id": symbol["id"],
            "lines": f"{part['start_line']}-{part['end_line']}",
            "symbol": symbol.get("qualname", ""),
            "text": text,
            "hash": symbol.get("hash") or sha256_text(body),
        })
    return out


def document_chunks(text: str) -> List[Dict[str, Any]]:
    lines = text.splitlines()
    heads = [(i, re.match(r"^(#{1,6})\s+(.+?)\s*$", line))
             for i, line in enumerate(lines)]
    heads = [(i, m) for i, m in heads if m]
    sections = []
    if heads:
        if heads[0][0] > 0:
            sections.append((0, heads[0][0], "文档导言"))
        for n, (i, match) in enumerate(heads):
            end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
            sections.append((i, end, match.group(2)))
    else:
        sections.append((0, len(lines), "文档内容"))

    out: List[Dict[str, Any]] = []
    for start, end, title in sections:
        body = "\n".join(lines[start:end]).strip()
        if not body:
            continue
        for part in _line_parts(body, start + 1, max_lines=80, max_chars=6000, overlap=4):
            out.append({**part, "symbol": title})
    return out


def file_summary(rel: str, lang: str, text: str,
                 symbols: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    lines = text.splitlines()
    imports = []
    for line in lines:
        if re.match(r"^\s*(?:from\s+\S+\s+import|import\s|package\s|using\s|#include\s|use\s|require\s*\()", line):
            imports.append(line.strip())
    declarations = []
    for symbol in symbols:
        sig = " ".join((symbol.get("signature") or "").split())
        declarations.append(
            f"- {symbol.get('qualname', '')} ({symbol.get('kind', '')}, "
            f"行 {symbol.get('start_line', 1)}): {sig}"
        )
    body = "\n".join([
        f"文件：{rel}",
        f"语言：{lang}",
        "",
        "导入与依赖：",
        "\n".join(imports[:40]) or "（未识别）",
        "",
        "主要类型与函数：",
        "\n".join(declarations[:100]) or "（未识别）",
    ])
    return {
        "id": "file_" + hashlib.md5(rel.encode("utf-8")).hexdigest()[:16],
        "text": clip(body, 12000),
        "lines": f"1-{max(1, len(lines))}",
        "hash": sha256_text(text),
    }
