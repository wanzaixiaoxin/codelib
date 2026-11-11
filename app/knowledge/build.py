"""离线建库：tree-sitter 抽符号 + markitdown 抽文档 → FTS5/邻接表。

P0：结构 + 全文检索；LLM 摘要留接口（默认关闭，P1 开启）。
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import AppConfig, IndexConfig, WorkspaceConfig
from ..indexer import detect_lang, iter_files
from ..utils import read_text_safe, rel_posix, sha256_file, sha256_text, clip
from .chunking import document_chunks, file_summary, symbol_chunks
from .schema import KnowledgeStore

try:  # tree-sitter 核心 + 官方自带编译产物的 grammar
    from tree_sitter import Language, Parser  # type: ignore
except Exception:  # pragma: no cover
    Language = Parser = None  # type: ignore

_GRAMMAR_MODULES = {
    "python": ("tree_sitter_python", "language"),
    "go": ("tree_sitter_go", "language"),
    "java": ("tree_sitter_java", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "c": ("tree_sitter_c", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
    "c_sharp": ("tree_sitter_c_sharp", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
}
_PARSER_CACHE: Dict[str, Any] = {}
INDEX_FORMAT_VERSION = "2"


def _get_parser(lang: str):
    if lang in _PARSER_CACHE:
        return _PARSER_CACHE[lang]
    spec = _GRAMMAR_MODULES.get(lang)
    if not spec or Parser is None:
        _PARSER_CACHE[lang] = None
        return None
    modname, fn = spec
    try:
        mod = importlib.import_module(modname)
        capsule = getattr(mod, fn)()
        parser = Parser(Language(capsule))
        _PARSER_CACHE[lang] = parser
        return parser
    except Exception:
        _PARSER_CACHE[lang] = None
        return None

_LANG_NAME = {
    ".py": "python", ".pyi": "python", ".java": "java", ".go": "go",
    ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cs": "c_sharp",
    ".rs": "rust", ".rb": "ruby", ".php": "php", ".scala": "scala",
    ".lua": "lua", ".kt": "kotlin", ".kts": "kotlin", ".sh": "bash",
    ".sql": "sql",
}

_CLASS_TYPES = {
    "class_declaration", "class_definition", "interface_declaration",
    "struct_declaration", "struct_item", "enum_declaration", "enum_item",
    "type_declaration", "type_alias_declaration", "object_declaration",
    "record_declaration", "trait_declaration", "module_declaration",
    "class_specifier", "struct_specifier",
}
_FUNC_TYPES = {
    "function_declaration", "function_definition", "function_item",
    "method_declaration", "method_definition", "constructor_declaration",
    "function_signature", "method", "subroutine_declaration_statement",
}
_COMMENT_PREFIX = ("//", "#", "/*", "*", "///", "--", ";")


def ws_kb_id(ws_id: str, ws_name: str) -> str:
    """知识库主键：id:name。既保证多库唯一，也兼容按 id 或 name 查询。"""
    return f"{ws_id}:{ws_name}" if ws_name and ws_name != ws_id else str(ws_id)


def _doc_comment(lines: List[str], start_line: int) -> str:
    """取符号上方紧邻的注释行（0 基 start_line）。"""
    out: List[str] = []
    i = start_line - 1
    while i >= 0 and len(out) < 6:
        s = lines[i].strip()
        if not s:
            if out:
                break
            i -= 1
            continue
        if s.startswith(_COMMENT_PREFIX):
            out.insert(0, s)
            i -= 1
            continue
        break
    return "\n".join(out)


def _node_name(node) -> str:
    for field in ("name", "declarator"):
        try:
            n = node.child_by_field_name(field)
            if n is not None:
                return n.text.decode("utf-8", "ignore")
        except Exception:
            pass
    for child in node.children:
        if child.type in ("identifier", "type_identifier", "property_identifier",
                          "field_identifier", "name", "constant"):
            return child.text.decode("utf-8", "ignore")
    return ""


def _extract_tree_sitter(source: bytes, lines: List[str], rel: str, lang: str
                         ) -> List[Dict[str, Any]]:
    parser = _get_parser(lang)
    if parser is None:
        return []
    try:
        tree = parser.parse(source)
    except Exception:
        return []
    symbols: List[Dict[str, Any]] = []

    def walk(node, parents: List[Tuple[str, str]]):
        t = node.type
        kind = None
        if t in _CLASS_TYPES:
            kind = "class"
        elif t in _FUNC_TYPES:
            kind = "function"
        if kind:
            name = _node_name(node)
            if name:
                parent_names = [p[1] for p in parents if p[0] == "class"]
                qual = ".".join(parent_names + [name]) if parent_names else name
                start = node.start_point[0]
                end = node.end_point[0] + 1
                body = node.text.decode("utf-8", "ignore")
                sig = clip(body.splitlines()[0].strip() if body else name, 300)
                doc = _doc_comment(lines, start)
                text = f"{sig}\n{doc}\n{body}".strip()
                sym_id = "sym_" + hashlib.md5(f"{rel}|{qual}|{kind}".encode()).hexdigest()[:16]
                symbols.append({
                    "id": sym_id, "path": rel, "lang": lang, "kind": kind,
                    "qualname": qual, "start_line": start + 1, "end_line": end,
                    "signature": sig, "doc": doc, "body": clip(body, 20000),
                    "text": text, "hash": sha256_text(body),
                })
                parents = parents + [(kind, name)]
        for child in node.children:
            walk(child, parents)

    walk(tree.root_node, [])
    return symbols


def _regex_symbols(text: str, rel: str, lang: str) -> List[Dict[str, Any]]:
    """tree-sitter 不可用时的兜底（复用 indexer 的抽取）。"""
    from ..indexer import extract_chunks
    from ..config import IndexConfig
    idx = IndexConfig()
    chunks = extract_chunks(Path(rel), text, lang if lang in ("python", "markdown") else "generic", idx)
    out = []
    for c in chunks:
        sym_id = "sym_" + hashlib.md5(f"{rel}|{c.get('symbol','')}|{c['start_line']}".encode()).hexdigest()[:16]
        out.append({
            "id": sym_id, "path": rel, "lang": lang, "kind": c.get("kind", "function"),
            "qualname": c.get("symbol", ""), "start_line": c["start_line"], "end_line": c["end_line"],
            "signature": clip(c["text"].splitlines()[0] if c["text"] else "", 300),
            "doc": "", "body": c["text"], "text": c["text"], "hash": sha256_text(c["text"]),
        })
    return out


def _doc_text(path: Path, rel: str) -> Optional[str]:
    suffix = path.suffix.lower()
    if suffix in (".md", ".markdown", ".txt", ".rst", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf"):
        try:
            return read_text_safe(path, 4 * 1024 * 1024)
        except Exception:
            return None
    try:
        from markitdown import MarkItDown  # type: ignore
        result = MarkItDown().convert(str(path))
        return getattr(result, "text_content", None) or None
    except Exception:
        return None


def _rebuild_edges(store: KnowledgeStore, ws_id: str) -> int:
    syms = store.all_symbols(ws_id)
    name_map: Dict[str, List[str]] = {}
    for s in syms:
        qual = s.get("qualname") or ""
        base = qual.split(".")[-1]
        if len(base) < 4:
            continue
        name_map.setdefault(base, []).append(s["id"])
    edges: List[Tuple[str, str, str, float]] = []
    stop = {"self", "this", "string", "return", "class", "function", "object", "value", "error"}
    for s in syms:
        body = s.get("body") or ""
        if not body:
            continue
        identifiers = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body))
        for name, ids in name_map.items():
            if name in stop or name.lower() in stop:
                continue
            if name in identifiers:
                for did in ids:
                    if did != s["id"]:
                        edges.append((s["id"], did, "calls", 1.0))
        if len(edges) > 30000:
            break
    return store.replace_edges(ws_id, edges)


def build_workspace(cfg: AppConfig, ws: WorkspaceConfig, full: bool = False) -> Dict[str, Any]:
    kb = cfg.knowledge or {}
    store = KnowledgeStore(cfg.knowledge_db_path)
    ws_id = ws_kb_id(ws.id, ws.name)   # 知识库按 id:name 存，避免 id↔name 不一致找不到
    version_key = f"index_format:{ws_id}"
    if store.get_meta(version_key) != INDEX_FORMAT_VERSION:
        full = True
    if full:
        store.reset(ws_id)

    parse_exts = kb.get("build", {}).get(
        "parse_exts",
        ["md", "txt", "pdf", "docx", "pptx", "xlsx", "yaml", "yml", "json", "toml", "ini", "conf", "rst"])
    include = list(dict.fromkeys(list(ws.index.include) + [f"*.{e.lstrip('.')}" for e in parse_exts]))
    document_patterns = {f"*.{e.lstrip('.').lower()}" for e in parse_exts}
    exclude_globs = [p for p in ws.index.exclude_globs if p.lower() not in document_patterns]
    scan_cfg = dataclasses.replace(ws.index, include=include, exclude_globs=exclude_globs)

    t0 = time.time()
    scanned = indexed = skipped = failed = deleted = 0
    n_sym = n_doc = 0
    changed = False
    current_paths = set()

    for path in iter_files(ws.path, scan_cfg):
        scanned += 1
        rel = rel_posix(path, ws.path)
        current_paths.add(rel)
        _tf = time.time()
        try:
            fhash = sha256_file(path)
        except OSError:
            failed += 1
            continue
        if not full and store.file_hash(ws_id, rel) == fhash:
            skipped += 1
            continue
        store.delete_file(ws_id, rel)
        suffix = path.suffix.lower()
        lang = detect_lang(path)
        try:
            is_code = suffix in (".py", ".pyi") or (
                suffix in _LANG_NAME and _LANG_NAME[suffix] != "sql")
            if is_code:
                text = read_text_safe(path, 4 * 1024 * 1024)
                code_lang = "python" if suffix in (".py", ".pyi") else _LANG_NAME[suffix]
                syms = _extract_tree_sitter(text.encode("utf-8"), text.splitlines(), rel, code_lang)
                if not syms:
                    syms = _regex_symbols(text, rel, code_lang)
                for s in syms:
                    store.upsert_symbol(ws_id, s)
                    store.insert_chunks(ws_id, "code", rel, symbol_chunks(s))
                summary = file_summary(rel, code_lang, text, syms)
                store.insert_chunk(ws_id, "file", summary["id"], rel, summary["lines"],
                                   Path(rel).name, summary["text"], summary["hash"])
                n_sym += len(syms)
            else:
                text = _doc_text(path, rel)
                if text:
                    chunks = document_chunks(text)
                    for c in chunks:
                        did = "doc_" + hashlib.md5(
                            f"{rel}|{c.get('symbol','')}|{c['start_line']}".encode()).hexdigest()[:16]
                        _insert_doc(store, ws_id, did, rel, c)
                        n_doc += 1
                    titles = list(dict.fromkeys(c["symbol"] for c in chunks if c.get("symbol")))
                    overview = f"文档：{rel}\n\n章节：\n" + "\n".join(f"- {t}" for t in titles[:100])
                    fid = "file_" + hashlib.md5(rel.encode("utf-8")).hexdigest()[:16]
                    store.insert_chunk(ws_id, "file", fid, rel,
                                       f"1-{max(1, len(text.splitlines()))}", Path(rel).name,
                                       overview, sha256_text(text))
        except Exception:
            failed += 1
            continue
        store.set_file(ws_id, rel, fhash, lang, path.stat().st_mtime)
        indexed += 1
        changed = True
        if os.environ.get("KB_DEBUG"):
            print(f"[kb] {time.time() - _tf:6.2f}s {rel}", flush=True)

    if not full:
        for rel in set(store.file_paths(ws_id)) - current_paths:
            store.delete_file(ws_id, rel)
            deleted += 1
            changed = True
    edges = _rebuild_edges(store, ws_id) if changed else store.counts(ws_id)["edges"]
    if failed == 0:
        store.set_meta(version_key, INDEX_FORMAT_VERSION)
    counts = store.counts(ws_id)
    return {
        "workspace": ws_id, "id": ws.id, "name": ws.name, "path": ws.path, "full": full,
        "scanned": scanned, "indexed": indexed, "skipped": skipped, "failed": failed,
        "deleted": deleted,
        "symbols_added": n_sym, "doc_chunks_added": n_doc, "edges": edges,
        "totals": counts, "elapsed_s": round(time.time() - t0, 2),
    }


def _insert_doc(store: KnowledgeStore, ws_id: str, did: str, rel: str, c: Dict[str, Any]) -> None:
    import sqlite3
    conn = store._connect()  # noqa: SLF001 - 同包内部使用
    try:
        conn.execute(
            "INSERT OR REPLACE INTO doc_chunk(id,workspace,path,section,page,text,hash) "
            "VALUES(?,?,?,?,?,?,?)",
            (did, ws_id, rel, c.get("symbol", ""), 0, c.get("text", ""), sha256_text(c.get("text", ""))))
        conn.commit()
    finally:
        conn.close()
    store.insert_chunk(ws_id, "doc", did, rel,
                       f"{c.get('start_line', 1)}-{c.get('end_line', 1)}",
                       c.get("symbol", ""), c.get("text", ""), sha256_text(c.get("text", "")))
