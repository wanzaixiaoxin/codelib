"""增量代码/文档索引器。

核心省 token 思想：不把整份文件塞进索引，只抽取「签名 / 文档字符串 / 标题 / 关键注释」
这类高信噪比小块，并记录 file:line 指回源文件。问答时按需再深读少量行。
"""
from __future__ import annotations

import ast
import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import IndexConfig, WorkspaceConfig
from .store import Store
from .utils import (estimate_tokens, read_text_safe, rel_posix, sha256_file,
                    sha256_text, clip)

# ---------------- 语言识别 ----------------
_LANG = {
    ".py": "python", ".pyi": "python", ".java": "java", ".kt": "kotlin",
    ".kts": "kotlin", ".go": "go", ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".rs": "rust", ".c": "c", ".h": "c", ".cc": "cpp",
    ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".php": "php", ".rb": "ruby",
    ".scala": "scala", ".lua": "lua", ".sql": "sql", ".proto": "proto",
    ".md": "markdown", ".markdown": "markdown", ".rst": "rst", ".txt": "text",
    ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".toml": "toml",
    ".ini": "ini", ".cfg": "ini", ".conf": "conf",
}


def detect_lang(path: Path) -> str:
    return _LANG.get(Path(path).suffix.lower(), "text")


# ---------------- 目录遍历 ----------------
def _dir_excluded(rel_dir: str, name: str, patterns: List[str]) -> bool:
    for pat in patterns:
        if "/" in pat or "\\" in pat:
            norm = pat.replace("\\", "/").strip("/")
            if rel_dir == norm or rel_dir.startswith(norm + "/"):
                return True
        else:
            if name == pat:
                return True
    return False


def iter_files(root: str | Path, cfg: IndexConfig) -> Iterable[Path]:
    rootp = Path(root)
    if not rootp.exists():
        return
    for dirpath, dirnames, filenames in os.walk(rootp):
        rel_dir = rel_posix(Path(dirpath), rootp)
        if rel_dir == ".":
            rel_dir = ""
        # 原地剪枝
        keep = []
        for d in dirnames:
            sub_rel = f"{rel_dir}/{d}".strip("/") if rel_dir else d
            if _dir_excluded(sub_rel, d, cfg.exclude_dirs):
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = f"{rel_dir}/{fn}".strip("/") if rel_dir else fn
            if not any(fnmatch.fnmatch(fn, pat) for pat in cfg.include):
                continue
            if any(fnmatch.fnmatch(fn, pat) or fnmatch.fnmatch(rel, pat)
                   for pat in cfg.exclude_globs):
                continue
            try:
                st = full.stat()
            except OSError:
                continue
            if st.st_size == 0 or st.st_size > cfg.max_file_bytes:
                continue
            yield full


# ---------------- 抽取器 ----------------
def _mk(path: str, start: int, end: int, kind: str, symbol: str, text: str) -> Dict[str, Any]:
    text = clip(text, 1400)
    return {"path": path, "start_line": max(1, start), "end_line": max(start, end),
            "kind": kind, "symbol": clip(symbol, 160), "text": text,
            "tokens": estimate_tokens(text)}


def _extract_python(text: str, rel: str, cfg: IndexConfig) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _extract_generic(text, rel, "python", cfg)

    md = ast.get_docstring(tree)
    if md:
        body = "# module docstring\n" + "\n".join(md.splitlines()[: cfg.snippet_lines])
        chunks.append(_mk(rel, 1, 1, "module", "<module>", body))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            if getattr(node, "decorator_list", None):
                starts = [node.lineno] + [d.lineno for d in node.decorator_list]
                start = min(starts)
            sig = lines[node.lineno - 1].strip() if node.lineno - 1 < len(lines) else node.name
            if isinstance(node, ast.ClassDef):
                kind = "class"
                sig = f"class {node.name}"
                bases = []
                for b in node.bases:
                    try:
                        bases.append(ast.unparse(b))
                    except Exception:
                        pass
                if bases:
                    sig += "(" + ", ".join(bases) + ")"
            else:
                kind = "function"
                try:
                    sig = _py_signature(node, lines)
                except Exception:
                    sig = lines[node.lineno - 1].strip()

            doc = ast.get_docstring(node)
            body = sig
            if doc:
                body += "\n" + "\n".join(doc.splitlines()[: cfg.snippet_lines])
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            # 只截止到签名/文档串，不覆盖整个函数体
            if node.body and isinstance(node.body[0], ast.Expr) and doc:
                end = getattr(node.body[0], "end_lineno", node.lineno) or node.lineno
            else:
                end = node.lineno
            qual = node.name
            chunks.append(_mk(rel, start, end, kind, qual, body))
        if len(chunks) >= cfg.max_chunks_per_file:
            break
    return chunks[: cfg.max_chunks_per_file]


def _py_signature(node, lines: List[str]) -> str:
    """取 def 行到冒号结束（可能多行），压缩空白。"""
    start = node.lineno - 1
    if node.body:
        end = max(node.body[0].lineno - 1, start + 1)
    else:
        end = start + 1
    end = min(end, start + 8, len(lines))
    seg = " ".join(line.strip() for line in lines[start:end] if line.strip())
    seg = re.sub(r"\s+", " ", seg)
    return clip(seg, 300)


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def _extract_markdown(text: str, rel: str, cfg: IndexConfig) -> List[Dict[str, Any]]:
    lines = text.splitlines()
    heads = [(i, m.group(1), m.group(2).strip()) for i, line in enumerate(lines)
             if (m := _MD_HEADING.match(line))]
    if not heads:
        return _extract_text(text, rel, cfg)
    chunks: List[Dict[str, Any]] = []
    for k, (i, hashes, title) in enumerate(heads):
        level = len(hashes)
        if level > 4:
            continue
        end = heads[k + 1][0] - 1 if k + 1 < len(heads) else len(lines) - 1
        body_lines = lines[i: min(end + 1, i + 1 + cfg.snippet_lines)]
        body = "\n".join(body_lines).strip()
        chunks.append(_mk(rel, i + 1, min(end + 1, i + 1 + cfg.snippet_lines),
                          "heading", f"{'#'*level} {title}", body))
        if len(chunks) >= cfg.max_chunks_per_file:
            break
    return chunks


def _extract_text(text: str, rel: str, cfg: IndexConfig) -> List[Dict[str, Any]]:
    """纯文本/无标题文档：按空行分段，取前若干段。"""
    chunks: List[Dict[str, Any]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines) and len(chunks) < cfg.max_chunks_per_file:
        if not lines[i].strip():
            i += 1
            continue
        start = i
        buf = []
        while i < len(lines) and lines[i].strip() and len(buf) < cfg.snippet_lines:
            buf.append(lines[i])
            i += 1
        if buf:
            chunks.append(_mk(rel, start + 1, i, "paragraph", "", "\n".join(buf)))
    return chunks


_DECL_RE = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*"
    r"(?:export\s+|default\s+|pub(?:\([^)]*\))?\s+|public\s+|private\s+|protected\s+|"
    r"internal\s+|static\s+|final\s+|abstract\s+|async\s+|override\s+|extern\s+|inline\s+)*"
    r"(?:def|func|fn|function|class|interface|struct|enum|trait|impl|type|module|namespace|"
    r"sub|macro|record|object)\b[^\n]*",
)
_FUNC_LIKE_RE = re.compile(
    r"^\s*(?:export\s+|public\s+|private\s+|protected\s+|static\s+|async\s+|final\s+|"
    r"override\s+|virtual\s+)*(?:[\w<>\[\],.*&?]+\s+)+[\w~]+\s*\([^;{]*\)\s*(?:const)?\s*[{;]?\s*$"
)
_COMMENT_RE = re.compile(r"^\s*(?://|#|/\*|\*|--|;;)")


def _extract_generic(text: str, rel: str, lang: str, cfg: IndexConfig) -> List[Dict[str, Any]]:
    lines = text.splitlines()
    chunks: List[Dict[str, Any]] = []
    for idx, line in enumerate(lines):
        if len(chunks) >= cfg.max_chunks_per_file:
            break
        stripped = line.strip()
        if not stripped or len(stripped) > 400:
            continue
        is_decl = bool(_DECL_RE.match(line))
        is_func = bool(_FUNC_LIKE_RE.match(line)) and "(" in line
        if not (is_decl or is_func):
            continue
        # 收集紧邻上方的注释作为上下文
        ctx: List[str] = []
        j = idx - 1
        while j >= 0 and len(ctx) < 3 and _COMMENT_RE.match(lines[j]):
            ctx.insert(0, lines[j].strip())
            j -= 1
        body_lines = ctx + [stripped]
        # 附上紧随其后的文档注释行
        k = idx + 1
        extra = 0
        while k < len(lines) and extra < cfg.snippet_lines and _COMMENT_RE.match(lines[k]):
            body_lines.append(lines[k].strip())
            k += 1
            extra += 1
        symbol = _guess_symbol(stripped)
        kind = "class" if re.search(r"\b(class|interface|struct|enum|trait|record|object)\b", stripped) else "function"
        chunks.append(_mk(rel, (j + 2) if ctx else idx + 1, k if k > idx + 1 else idx + 1,
                          kind, symbol, "\n".join(body_lines)))
    if not chunks:
        # 兜底：取文件头部注释/内容片段，保证可召回
        head = "\n".join(lines[: cfg.snippet_lines]).strip()
        if head:
            chunks.append(_mk(rel, 1, min(len(lines), cfg.snippet_lines), "head", "", head))
    return chunks


def _guess_symbol(line: str) -> str:
    m = re.search(r"\b(?:def|func|fn|function|class|interface|struct|enum|trait|impl|type|"
                  r"sub|module|namespace|record|object)\s+([A-Za-z_][\w.]*)", line)
    if m:
        return m.group(1)
    m = re.search(r"([A-Za-z_][\w]*)\s*\(", line)
    return m.group(1) if m else ""


def extract_chunks(path: Path, text: str, lang: str, cfg: IndexConfig) -> List[Dict[str, Any]]:
    rel = path.name  # 调用方会用真实 rel 覆盖
    if lang == "python":
        out = _extract_python(text, rel, cfg)
    elif lang in ("markdown", "rst"):
        out = _extract_markdown(text, rel, cfg)
    elif lang in ("text",):
        out = _extract_text(text, rel, cfg)
    else:
        out = _extract_generic(text, rel, lang, cfg)
    # 限制单文件条目数
    return out[: cfg.max_chunks_per_file]


# ---------------- 扫描 / 变更 / 索引 ----------------
def scan_workspace(ws: WorkspaceConfig) -> Dict[str, Tuple[Path, float, int]]:
    current: Dict[str, Tuple[Path, float, int]] = {}
    for p in iter_files(ws.path, ws.index):
        try:
            st = p.stat()
        except OSError:
            continue
        current[rel_posix(p, ws.path)] = (p, st.st_mtime, st.st_size)
    return current


@dataclass
class ChangeSet:
    added: List[str]
    modified: List[str]
    deleted: List[str]

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.modified or self.deleted)

    def to_dict(self) -> Dict[str, Any]:
        return {"added": self.added, "modified": self.modified,
                "deleted": self.deleted,
                "total": len(self.added) + len(self.modified) + len(self.deleted)}


def compute_changes(store: Store, ws: WorkspaceConfig) -> ChangeSet:
    """基于 mtime+size 快速比对；对疑似改动再核 sha256，避免误报。"""
    existing = store.get_file_map(ws.id)
    current = scan_workspace(ws)
    added: List[str] = []
    modified: List[str] = []
    for rel, (p, mtime, size) in current.items():
        old = existing.get(rel)
        if old is None:
            added.append(rel)
            continue
        if abs(float(old["mtime"]) - mtime) > 1e-6 or int(old["size"]) != size:
            try:
                if sha256_file(p) != old["sha256"]:
                    modified.append(rel)
            except OSError:
                modified.append(rel)
    deleted = [rel for rel in existing if rel not in current]
    return ChangeSet(sorted(added), sorted(modified), sorted(deleted))


def index_workspace(store: Store, ws: WorkspaceConfig, full: bool = False) -> Dict[str, Any]:
    cfg = ws.index
    root = Path(ws.path)
    existing = store.get_file_map(ws.id)
    current = scan_workspace(ws)

    added = modified = unchanged = skipped = failed = 0
    total_chunks = 0
    chunk_tokens = 0
    full_tokens_total = 0

    for rel, (p, mtime, size) in current.items():
        old = existing.get(rel)
        need = full or old is None
        if not need and (abs(float(old["mtime"]) - mtime) > 1e-6 or int(old["size"]) != size):
            try:
                need = sha256_file(p) != old["sha256"]
            except OSError:
                need = True
        if not need:
            unchanged += 1
            full_tokens_total += int(old["full_tokens"]) if old is not None else 0
            continue
        try:
            text = read_text_safe(p, cfg.max_file_bytes)
        except Exception:
            failed += 1
            continue
        lang = detect_lang(p)
        ftoks = estimate_tokens(text)
        try:
            raw_chunks = extract_chunks(p, text, lang, cfg)
        except Exception:
            raw_chunks = []
        chunks = []
        for c in raw_chunks:
            c["path"] = rel
            chunks.append(c)
        n = store.replace_chunks(ws.id, rel, chunks)
        store.upsert_file(ws.id, rel, sha256_text(text), size, mtime, lang,
                          full_tokens=ftoks)
        if old is None:
            added += 1
            store.add_change(ws.id, rel, "added")
        else:
            modified += 1
            store.add_change(ws.id, rel, "modified")
        total_chunks += n
        chunk_tokens += sum(c["tokens"] for c in chunks)
        full_tokens_total += ftoks

    deleted = [rel for rel in existing if rel not in current]
    for rel in deleted:
        store.delete_file(ws.id, rel)
        store.add_change(ws.id, rel, "deleted")

    if added or modified or deleted:
        store.bump_generation(ws.id)

    return {
        "workspace": ws.id,
        "path": ws.path,
        "full": full,
        "scanned_files": len(current),
        "indexed": added + modified,
        "added": added,
        "modified": modified,
        "unchanged": unchanged,
        "deleted": len(deleted),
        "failed": failed,
        "chunks_written": total_chunks,
        "chunk_tokens": chunk_tokens,
        "corpus_tokens": full_tokens_total,
        "token_saving_ratio": round(1 - chunk_tokens / full_tokens_total, 4) if full_tokens_total else 0.0,
        "generation": store.get_generation(ws.id),
    }
