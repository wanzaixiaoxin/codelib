"""通用工具：token 估算、分词、哈希、安全读文本。"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import List

_CJK_CHAR = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_WORD = re.compile(r"[A-Za-z0-9_]+")
_SPACE = re.compile(r"\s+")


def estimate_tokens(text: str) -> int:
    """粗估 token：中文按字计，英文按 4 字符 1 token。用于预算控制，不追求精确。"""
    if not text:
        return 0
    cjk = len(_CJK_CHAR.findall(text))
    other = max(0, len(text) - cjk)
    return int(cjk * 1.0 + other / 4.0) + 1


def count_cjk(text: str) -> int:
    return len(_CJK_CHAR.findall(text))


def tokenize(text: str) -> List[str]:
    """BM25 用分词：英文/数字小写词 + 中文单字 + 中文二元组。"""
    if not text:
        return []
    low = text.lower()
    toks = _WORD.findall(low)
    cjk = _CJK_CHAR.findall(low)
    toks.extend(cjk)
    for i in range(len(cjk) - 1):
        toks.append(cjk[i] + cjk[i + 1])
    return toks


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1 << 16) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def read_text_safe(path: Path, max_bytes: int = 512 * 1024) -> str:
    """尽量安全地读文本：多编码回退，超限截断。"""
    raw = Path(path).read_bytes()[:max_bytes]
    if b"\x00" in raw[:4096]:
        raise ValueError("疑似二进制文件")
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def truncate_tokens(text: str, max_tokens: int) -> str:
    if estimate_tokens(text) <= max_tokens:
        return text
    # 粗略按字符比例裁剪
    ratio = max_tokens / max(1, estimate_tokens(text))
    cut = max(1, int(len(text) * ratio))
    return text[:cut] + "…"


def clip(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def rel_posix(path: Path, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except Exception:
        return Path(path).as_posix()


def ensure_dir(path: Path) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)
