"""只读工具集：9 个工具，全部映射到 knowledge.db / 白名单内文件。

权限双保险：profile 白名单过滤（profiles.py）+ dispatch 入口再校验；
read_lines 只允许 workspace_root 下的文件（只读），超出即拒绝并记录。
"""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any

from app.chain import ChainReader, build_chain_context
from app.research.dedupe import RepeatBreaker
from app.research.evidence import EvidenceBoard

TOOL_NAMES = ("search_symbols", "read_body", "callers", "callees", "grep_code",
              "read_lines", "related_sections", "expand_chain", "update_evidence")


class ResearchTools:
    def __init__(self, kb_path, workspace: str, workspace_root: str | None = None,
                 board: EvidenceBoard | None = None):
        self.reader = ChainReader(kb_path, workspace)
        self.workspace = workspace
        self.kb = sqlite3.connect(f"file:{Path(kb_path).as_posix()}?mode=ro", uri=True)
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self.board = board or EvidenceBoard()
        self.breaker = RepeatBreaker()
        self.event_log: list[dict] = []       # 执行证据：每次调用的名称/参数/返回字符数

    def close(self) -> None:
        self.reader.close()
        self.kb.close()

    # ---------- schema ----------
    def tool_specs(self, names: list[str] | None = None) -> list[dict]:
        specs = {
            "search_symbols": ("按名称查找函数/类型（精确、前缀、子串三级打分）", {
                "type": "object", "properties": {"query": {"type": "string"},
                                                 "limit": {"type": "integer"}},
                "required": ["query"]}),
            "read_body": ("读取某个函数/类型的完整源码体（含文件与行号）。symbol 用精确限定名。", {
                "type": "object", "properties": {"symbol": {"type": "string"},
                                                 "max_chars": {"type": "integer"}},
                "required": ["symbol"]}),
            "callers": ("谁调用了这个符号（反向调用边）", {
                "type": "object", "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"]}),
            "callees": ("这个符号调用了谁（正向调用边）", {
                "type": "object", "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"]}),
            "grep_code": ("在已索引代码/文档片段里做子串全文检索", {
                "type": "object", "properties": {"pattern": {"type": "string"},
                                                 "limit": {"type": "integer"}},
                "required": ["pattern"]}),
            "read_lines": ("读取工作区内文件的指定行区间（只读，白名单内）", {
                "type": "object", "properties": {"path": {"type": "string"},
                                                 "start": {"type": "integer"},
                                                 "end": {"type": "integer"}},
                "required": ["path", "start", "end"]}),
            "related_sections": ("列出某文档已索引的章节标题（快速看文档结构）", {
                "type": "object", "properties": {"path": {"type": "string"},
                                                 "limit": {"type": "integer"}},
                "required": ["path"]}),
            "expand_chain": ("从某符号沿调用边双向展开 N 跳，一次性返回相关函数体", {
                "type": "object", "properties": {"symbol": {"type": "string"},
                                                 "hops": {"type": "integer"}},
                "required": ["symbol"]}),
            "update_evidence": ("更新断言板：把某条断言标记为 proven/refuted 并挂证据", {
                "type": "object", "properties": {"index": {"type": "integer"},
                                                 "status": {"type": "string",
                                                            "enum": ["proven", "refuted", "open"]},
                                                 "symbol": {"type": "string"},
                                                 "path": {"type": "string"},
                                                 "quote": {"type": "string"}},
                "required": ["index", "status"]}),
        }
        names = names or list(specs)
        return [{"name": n, "description": specs[n][0], "parameters": specs[n][1]}
                for n in names if n in specs]

    # ---------- dispatch ----------
    def dispatch(self, name: str, args_json: str) -> str:
        if name not in TOOL_NAMES:
            return f"未知工具 {name}"
        n, notice = self.breaker.check(name, args_json)
        if notice:
            self._log(name, args_json, 0, rejected=notice)
            return notice
        try:
            args = json.loads(args_json or "{}")
        except Exception:
            args = {}
        try:
            fn = getattr(self, f"_t_{name}")
            out = str(fn(**{k: v for k, v in args.items()}))[:8000]
        except Exception as e:
            out = f"工具执行失败 {type(e).__name__}: {e}"
        self._log(name, args_json, len(out))
        if notice:
            out = notice + "\n" + out
        return out

    def _log(self, name, args, size, rejected: str = "") -> None:
        self.event_log.append({"tool": name, "args": args[:200], "chars": size,
                               "rejected": rejected})

    # ---------- 工具实现 ----------
    def _t_search_symbols(self, query: str, limit: int = 8) -> str:
        q = (query or "").strip().lower()
        if not q:
            return "query 为空"
        scored = []
        for sid, s in self.reader.symbols.items():
            name = s["qualname"].lower()
            score = 3 if name == q else (2 if name.startswith(q) or name.endswith(q)
                                         else (1 if q in name else 0))
            if score:
                scored.append((score, s["path"], s["qualname"]))
        scored.sort(key=lambda x: (-x[0], x[1], x[2]))
        if not scored:
            return f"未找到名称含 {query} 的符号"
        lines = [f"{qn}  ({p})" for _s, p, qn in scored[:limit]]
        return f"命中 {len(scored)} 个符号：\n" + "\n".join(lines[:limit])

    def _t_read_body(self, symbol: str, max_chars: int = 3500) -> str:
        sid = self.reader.by_name.get((symbol or "").lower())
        if not sid:
            near = [k for k in self.reader.by_name if symbol.lower() in k][:5]
            return f"符号 {symbol} 不存在。近似候选：{near}"
        s = self.reader.symbols[sid]
        body = str(s.get("body") or "")[:max_chars]
        return f"{s['qualname']}  ({s['path']})\n{body}"

    def _neighbors(self, symbol: str, reverse: bool) -> str:
        sid = self.reader.by_name.get((symbol or "").lower())
        if not sid:
            return f"符号 {symbol} 不存在"
        nbrs = (self.reader.reverse if reverse else self.reader.edges).get(sid, [])
        names = []
        for n in nbrs[:14]:
            s = self.reader.symbols.get(n)
            if s:
                names.append(f"{s['qualname']}  ({s['path']})")
        rel = "调用方" if reverse else "被调用"
        return f"{symbol} 的{rel}（{len(nbrs)} 个）：\n" + "\n".join(names or ["无"])

    def _t_callers(self, symbol: str) -> str:
        return self._neighbors(symbol, reverse=True)

    def _t_callees(self, symbol: str) -> str:
        return self._neighbors(symbol, reverse=False)

    def _t_grep_code(self, pattern: str, limit: int = 20) -> str:
        pat = (pattern or "").strip()
        if not pat:
            return "pattern 为空"
        safe = pat.replace("%", r"\%").replace("_", r"\_")
        rows = self.kb.execute(
            "SELECT path,symbol,substr(text,1,200) FROM chunk WHERE workspace=? "
            "AND text LIKE ? ESCAPE '\' ORDER BY path LIMIT ?",
            (self.workspace, f"%{safe}%", limit)).fetchall()
        if not rows:
            return f"未检索到含 {pattern} 的片段（注意：只搜已索引片段，不是全库 grep）"
        out = [f"[{p} :: {s}] {t}".replace(chr(10), " ")[:220] for p, s, t in rows]
        return f"命中 {len(rows)} 片段：\n" + "\n".join(out)

    def _t_read_lines(self, path: str, start: int = 1, end: int = 60) -> str:
        p = Path(path)
        if not p.is_absolute():
            base = self.workspace_root
            if not base:
                return "拒绝：未提供 workspace_root 且 path 为相对路径"
            p = base / path
        try:
            rp = p.resolve()
        except OSError:
            return "拒绝：路径无法解析"
        if self.workspace_root and not str(rp).startswith(str(self.workspace_root)):
            return f"拒绝：路径超出工作区白名单（{self.workspace_root}）"
        if not rp.is_file():
            return f"文件不存在：{rp}"
        try:
            lines = rp.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return f"读取失败：{e}"
        a, b = max(1, int(start)), min(int(end), len(lines), int(start) + 120)
        numbered = [f"{i+1:5d} {lines[i][:200]}" for i in range(a - 1, b)]
        return f"{rp} 行 {a}-{b}（共 {len(lines)} 行）：" + chr(10) + chr(10).join(numbered)

    def _t_related_sections(self, path: str, limit: int = 10) -> str:
        rows = self.kb.execute(
            "SELECT section FROM doc_chunk WHERE workspace=? AND path=? AND section!='' LIMIT ?",
            (self.workspace, path, limit)).fetchall()
        if not rows:
            return f"{path} 无已索引章节（可能不是文档或未建库）"
        return f"{path} 章节：" + chr(10) + chr(10).join(f"- {r[0]}" for r in rows)

    def _t_expand_chain(self, symbol: str, hops: int = 2) -> str:
        sid = self.reader.by_name.get((symbol or "").lower())
        if not sid:
            return f"符号 {symbol} 不存在"
        nodes = self.reader.expand([sid], hops=max(1, min(int(hops), 4)), max_nodes=16)
        return ChainReader.render(nodes, budget_chars=7000)

    def _t_update_evidence(self, index: int, status: str, symbol: str = "",
                           path: str = "", quote: str = "") -> str:
        return self.board.update(int(index), status, symbol, path, quote)
