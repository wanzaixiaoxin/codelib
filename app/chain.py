"""调用链上下文读取器（新增模块，不改动现有检索主链路）。

与 FTS 检索互补：FTS 找「词面相关片段」，chain 找「跨文件因果链」。
数据全部来自已有 knowledge.db 的 symbol / edge 表；只读、不改索引格式、
不读知识库工程文件（函数体存在 symbol.body 里）。

设计要点：
  - 锚点不只靠名字匹配，也按「结构特征」自动发现（含 `go ` 的函数 = 并发起点、
    含 MarkMessage = 位点提交、空回调 = 未接线），不硬编码业务函数名。
  - 沿 calls 边做 BFS，把路径上每个函数的整段 body 按跳数顺序拼成上下文。
  - 与 FTS 通道并行、独立预算，避免把散片段挤掉。
"""
from __future__ import annotations
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass
class ChainNode:
    sid: str
    qualname: str
    path: str
    depth: int
    body: str = ""
    signature: str = ""
    via: str = ""


# 关注点 -> 从函数体里识别锚点的判据（结构性，不写死函数名）
STRUCTURAL = {
    "concurrency": re.compile(r"\bgo\s+[A-Za-z_][A-Za-z0-9_.]*\s*\("),
    "offset": re.compile(r"\.MarkMessage\s*\("),
    "writeback": re.compile(r"AsyncSendMessage|ProducerMessage|NewAsyncProducer"),
    "callback": re.compile(r"func\s+On\w+OK\s*\("),
    "partition": re.compile(r"Partitioner|ProducerMessage\s*\{"),
    "shared_state": re.compile(r"GenBoundKey|RoundID|Redis|DeleteRoundKey"),
}
CONCERN_WORDS = {
    "concurrency": ("顺序", "乱序", "保序", "并发", "协程", "先后", "依赖", "race", "order"),
    "offset": ("位点", "重复", "丢消息", "重投", "offset", "提交"),
    "writeback": ("写入", "回写", "生产", "发送", "kafka", "producer"),
    "callback": ("回调", "确认", "成功", "失败", "callback"),
    "partition": ("分区", "key", "hash", "partition"),
    "shared_state": ("上下文", "round", "会话", "缓存", "redis"),
}


def concerns_of(question: str) -> List[str]:
    q = (question or "").lower()
    return [k for k, words in CONCERN_WORDS.items() if any(w in q for w in words)]


class ChainReader:
    def __init__(self, kb_path, workspace: str):
        self.workspace = workspace
        self.con = sqlite3.connect(f"file:{Path(kb_path).as_posix()}?mode=ro", uri=True)
        self.con.row_factory = sqlite3.Row
        self.symbols: Dict[str, dict] = {}
        for r in self.con.execute(
                "SELECT id,qualname,path,signature,doc,body FROM symbol WHERE workspace=?",
                (workspace,)):
            self.symbols[r["id"]] = dict(r)
        self.edges: Dict[str, List[str]] = {}
        for r in self.con.execute("SELECT src,dst FROM edge WHERE workspace=? AND type='calls'",
                                  (workspace,)):
            self.edges.setdefault(r["src"], []).append(r["dst"])
        self.by_name: Dict[str, str] = {}
        for sid, s in self.symbols.items():
            self.by_name.setdefault(s["qualname"].lower(), sid)

    def close(self):
        self.con.close()

    def name_anchors(self, question: str, limit: int = 8) -> List[Tuple[str, str]]:
        """问题词（含动态词表扩展）→ 符号名/签名/注释命中。"""
        try:
            from .utils import tokenize
            toks = [str(t).lower() for t in tokenize(question)]
        except Exception:
            toks = re.findall(r"[a-z_]{3,}|[\u4e00-\u9fff]{2,}", (question or "").lower())
        try:
            from .lexicon import default_store
            toks += [str(t).lower() for t in default_store().expand(toks)]
        except Exception:
            pass
        toks = list(dict.fromkeys(t for t in toks if len(t) >= 2))
        scored = []
        for sid, s in self.symbols.items():
            hay = " ".join(str(s.get(k) or "") for k in ("qualname", "signature", "doc")).lower()
            hit = sum(1 for t in toks if t in hay)
            if hit:
                scored.append((hit, sid))
        scored.sort(key=lambda x: -x[0])
        return [(sid, self.symbols[sid]["qualname"]) for _h, sid in scored[:limit]]

    def structural_anchors(self, question: str, limit_per_concern: int = 3):
        """按关注点从函数体结构特征里找锚点，返回 (sid, qualname, concern)。"""
        out = []
        for concern in concerns_of(question):
            pat = STRUCTURAL[concern]
            hits = []
            for sid, s in self.symbols.items():
                body = str(s.get("body") or "")
                if pat.search(body):
                    hits.append((len(body), sid))
            hits.sort()  # 短函数优先（入口/空回调通常短）
            for _l, sid in hits[:limit_per_concern]:
                out.append((sid, self.symbols[sid]["qualname"], concern))
        return out


    def expand(self, anchor_ids: Sequence[str], hops: int = 4,
               max_nodes: int = 24) -> List[ChainNode]:
        from collections import deque
        seen: Dict[str, int] = {}
        parent: Dict[str, str] = {}
        todo = deque()
        for sid in anchor_ids:
            if sid in self.symbols and sid not in seen:
                seen[sid] = 0
                todo.append((sid, 0))
        while todo:
            sid, depth = todo.popleft()
            if len(seen) >= max_nodes or depth >= hops:
                continue
            for dst in self.edges.get(sid, []):
                if dst in seen or dst not in self.symbols:
                    continue
                seen[dst] = depth + 1
                parent[dst] = self.symbols[sid]["qualname"]
                todo.append((dst, depth + 1))
        nodes = []
        for sid, depth in sorted(seen.items(),
                                 key=lambda kv: (kv[1], self.symbols[kv[0]]["qualname"])):
            s = self.symbols[sid]
            nodes.append(ChainNode(
                sid=sid, qualname=s["qualname"], path=s["path"], depth=depth,
                body=str(s.get("body") or ""), signature=str(s.get("signature") or ""),
                via=parent.get(sid, "")))
        return nodes

    @staticmethod
    def render(nodes: Iterable[ChainNode], budget_chars: int = 12000) -> str:
        parts, used = [], 0
        for n in nodes:
            head = f"### [d{n.depth}] {n.qualname}  ({n.path})"
            head += f"  <- {n.via}" if n.via else "  <- 锚点"
            block = head + "\n" + n.body.strip() + "\n"
            if used + len(block) > budget_chars:
                remain = budget_chars - used
                if remain > 200:
                    parts.append(block[:remain] + "\n…(预算截断)")
                break
            parts.append(block)
            used += len(block)
        return "\n".join(parts)

    @staticmethod
    def find_evidence(nodes: Iterable[ChainNode], patterns: Dict[str, str]):
        """返回 {事实标签: [{symbol,path,line,depth}]}。"""
        out = {}
        for label, pat in patterns.items():
            rx = re.compile(pat)
            hits = []
            for n in nodes:
                for line in n.body.splitlines():
                    if rx.search(line):
                        hits.append({"symbol": n.qualname, "path": n.path,
                                     "depth": n.depth, "line": line.strip()[:150]})
                        break
            out[label] = hits
        return out


def build_chain_context(kb_path, workspace: str, question: str,
                        hops: int = 4, max_nodes: int = 24,
                        budget_chars: int = 12000, name_limit: int = 8):
    """便捷入口：返回 (上下文文本, 节点列表, 元信息)。"""
    reader = ChainReader(kb_path, workspace)
    try:
        named = reader.name_anchors(question, name_limit)
        structural = reader.structural_anchors(question)
        anchors = list(dict.fromkeys([s for s, _ in named]
                                     + [s for s, _q, _c in structural]))
        nodes = reader.expand(anchors, hops=hops, max_nodes=max_nodes)
        text = reader.render(nodes, budget_chars)
        meta = {
            "concerns": concerns_of(question),
            "name_anchors": [q for _s, q in named],
            "structural_anchors": [{"symbol": q, "concern": c} for _s, q, c in structural],
            "anchor_count": len(anchors), "node_count": len(nodes),
            "files": sorted({n.path for n in nodes}),
            "context_chars": len(text),
        }
        return text, nodes, meta
    finally:
        reader.close()
