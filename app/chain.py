"""调用链上下文读取器（R1 重写：修首轮验收暴露的四个锚点问题）。

修复清单（对照 docs/integration-plan.md §8 / refactor-plan.md R1）：
  1. 名称锚点不再被中文单字/二元组污染：只用 >=4 字符 ASCII 标识符 + lexicon 扩展，
     且只匹配 qualname（不再搜 signature/doc）。
  2. 结构锚点不再「短函数优先」：按业务目录优先级排序（service/ > kafka_client/ >
     bond_chat/ > config/ > 其余），并扩大每关注点锚点数。
  3. 只走出边 → **双向 BFS**（出边 + 反向入边），从回写工具能追到上游消费入口。
  4. 顺序类问题自动联动 offset/entry 关注点；入度为 0 的 On*/Consume* 符号识别为入口。
  5. 噪声目录（logging/utils/...）的符号不进节点（可穿越），避免 max_nodes 被吃满。

数据仍全部来自 knowledge.db 的 symbol / edge 表；只读，不读知识库工程文件。
"""
from __future__ import annotations
import re
import sqlite3
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass
class ChainNode:
    sid: str
    qualname: str
    path: str
    depth: int
    body: str = ""
    signature: str = ""
    via: str = ""
    direction: str = ""      # out | in | anchor


# 噪声目录：符号不作为节点（BFS 可穿越），防止 max_nodes 被 logging/utils 吃满
NOISE_DIRS = ("logging/", "utils/", "metric/", "common/", "tcp_server/", "dao/",
              "ctl/", "cron_tab/", "data_manager/", "server_instance/", "test",
              "third_party/", "node_modules/")
# 业务目录优先级：结构锚点命中后按此排序
PRIO_DIRS = ("service/", "kafka_client/", "bond_chat/", "config/", "def/")


def _dir_prio(path: str) -> int:
    p = path.replace("\\", "/").lower()
    for i, d in enumerate(PRIO_DIRS):
        if d in p:
            return i
    return len(PRIO_DIRS)


def is_noise(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    return any(d in p for d in NOISE_DIRS)


CONCERN_WORDS = {
    "concurrency": ("顺序", "乱序", "保序", "并发", "协程", "先后", "依赖", "order", "race"),
    "offset": ("位点", "重复", "丢消息", "重投", "offset", "提交", "顺序", "乱序"),
    "writeback": ("写入", "回写", "生产", "发送", "kafka", "producer", "写入kafka"),
    "callback": ("回调", "确认", "成功", "失败", "callback"),
    "partition": ("分区", "key", "hash", "partition"),
    "shared_state": ("上下文", "round", "会话", "缓存", "redis", "补全"),
    "entry": ("消费", "入口", "kafka", "消息", "入口在哪里"),
}
STRUCTURAL = {
    "concurrency": re.compile(r"\bgo\s+[A-Za-z_][A-Za-z0-9_.]*\s*\("),
    "offset": re.compile(r"\.MarkMessage\s*\("),
    "writeback": re.compile(r"AsyncSendMessage|ProducerMessage|NewAsyncProducer|sendDataToKafka"),
    "callback": re.compile(r"func\s+On\w+OK\s*\("),
    "partition": re.compile(r"Partitioner|ProducerMessage\s*\{"),
    "shared_state": re.compile(r"GenBoundKey|RoundID|DeleteRoundKey|ProcessRepoRoundContext"),
    "entry": re.compile(r"func\s+\(?\w*\s*\*?\w*\)?\s+(On\w+|ConsumeClaim)\s*\("),
}


def concerns_of(question: str) -> List[str]:
    """识别关注点；顺序/并发/kafka 类问题自动联动 offset 与 entry（首轮教训 4）。"""
    q = (question or "").lower()
    out: List[str] = []
    for k, words in CONCERN_WORDS.items():
        if any(w.lower() in q for w in words):
            out.append(k)
    if ("concurrency" in out or "顺序" in (question or "")) and "offset" not in out:
        out.append("offset")          # 谈顺序必然涉及位点提交，不依赖问题字面
    if any(k in out for k in ("concurrency", "writeback")) and "entry" not in out:
        out.append("entry")
    if any(k in out for k in ("concurrency", "writeback", "offset")) and "partition" not in out:
        out.append("partition")   # 谈顺序/回写必然涉及分区键与分区器
    return out


class ChainReader:
    """knowledge.db 的 symbol / edge 只读读取器（R1：双向 + 噪声过滤 + 锚点收紧）。"""

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
        self.reverse: Dict[str, List[str]] = {}
        self.in_degree: Dict[str, int] = {}
        for r in self.con.execute("SELECT src,dst FROM edge WHERE workspace=? AND type='calls'",
                                  (workspace,)):
            self.edges.setdefault(r["src"], []).append(r["dst"])
            self.reverse.setdefault(r["dst"], []).append(r["src"])
            self.in_degree[r["dst"]] = self.in_degree.get(r["dst"], 0) + 1
        self.by_name: Dict[str, str] = {}
        for sid, s in self.symbols.items():
            self.by_name.setdefault(s["qualname"].lower(), sid)

    def close(self) -> None:
        self.con.close()

    # ---------- 锚点 1：名称匹配（收紧：只认 >=4 字符标识符，只碰 qualname） ----------
    def name_anchors(self, question: str, limit: int = 8) -> List[Tuple[str, str]]:
        toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", question or "")
        try:
            from .lexicon import default_store
            extra = [t for t in default_store().expand(re.findall(r"[\u4e00-\u9fff]{2,}", question or ""))
                     if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{2,}", t)]
            toks += extra
        except Exception:
            pass
        toks = list(dict.fromkeys(t.lower() for t in toks))
        if not toks:
            return []
        scored = []
        for sid, s in self.symbols.items():
            name = s["qualname"].lower()
            score = 0
            for t in toks:
                if name == t:
                    score += 3
                elif name.startswith(t) or name.endswith(t):
                    score += 2
                elif t in name:
                    score += 1
            if score:
                scored.append((score, _dir_prio(s["path"]), sid))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [(sid, self.symbols[sid]["qualname"]) for _s, _p, sid in scored[:limit]]

    # ---------- 锚点 2：结构特征（目录优先；entry 用入度识别） ----------
    def structural_anchors(self, question: str,
                           limit_per_concern: int = 5) -> List[Tuple[str, str, str]]:
        out: List[Tuple[str, str, str]] = []
        seen: set[Tuple[str, str]] = set()
        for concern in concerns_of(question):
            if concern == "entry":
                hits = []
                for sid, s in self.symbols.items():
                    if self.in_degree.get(sid, 0) == 0 and re.match(
                            r"^(On|Consume|Handle)[A-Z]", s["qualname"]) and not is_noise(s["path"]):
                        hits.append((_dir_prio(s["path"]), len(s.get("body") or ""), sid))
                hits.sort()
                for _p, _l, sid in hits[:limit_per_concern]:
                    key = (concern, sid)
                    if key not in seen:
                        seen.add(key)
                        out.append((sid, self.symbols[sid]["qualname"], concern))
                continue
            pat = STRUCTURAL[concern]
            hits = []
            for sid, s in self.symbols.items():
                body = str(s.get("body") or "")
                if pat.search(body):
                    hits.append((_dir_prio(s["path"]), len(body), sid))
            hits.sort()          # 业务目录优先，同级内短函数优先（入口/空回调通常短）
            for _p, _l, sid in hits[:limit_per_concern]:
                key = (concern, sid)
                if key not in seen:
                    seen.add(key)
                    out.append((sid, self.symbols[sid]["qualname"], concern))
        return out

    # ---------- 双向 BFS（噪声符号穿越但不占节点名额） ----------
    def expand(self, anchor_ids: Sequence[str], hops: int = 4, max_nodes: int = 28,
               max_visited: int = 800) -> List[ChainNode]:
        visited: set[str] = set()
        parent: Dict[str, Tuple[str, str]] = {}
        depth_of: Dict[str, int] = {}
        node_ids: List[str] = []
        queue: deque = deque()
        for sid in anchor_ids:
            if sid in self.symbols and sid not in visited:
                visited.add(sid)
                depth_of[sid] = 0
                queue.append((sid, 0))
        while queue:
            sid, d = queue.popleft()
            if d >= hops or len(visited) >= max_visited:
                continue
            src = self.symbols[sid]
            src_noise = is_noise(src["path"])
            for direction, nbrs in (("out", self.edges.get(sid, [])),
                                    ("in", self.reverse.get(sid, []))):
                for dst in nbrs:
                    if dst in visited or dst not in self.symbols:
                        continue
                    visited.add(dst)
                    dst_sym = self.symbols[dst]
                    depth_of[dst] = d + 1
                    parent[dst] = (src["qualname"], direction)
                    dst_noise = is_noise(dst_sym["path"])
                    if not src_noise and not dst_noise and len(node_ids) < max_nodes:
                        node_ids.append(dst)
                    queue.append((dst, d + 1))
        keep = set(node_ids) | set(anchor_ids)
        nodes = []
        for sid in sorted(keep, key=lambda s: (depth_of.get(s, 99), _dir_prio(self.symbols[s]["path"]),
                                               self.symbols[s]["qualname"])):
            s = self.symbols[sid]
            via, direction = parent.get(sid, ("", "anchor"))
            nodes.append(ChainNode(sid=sid, qualname=s["qualname"], path=s["path"],
                                   depth=depth_of.get(sid, 0), body=str(s.get("body") or ""),
                                   signature=str(s.get("signature") or ""), via=via,
                                   direction=direction if direction != "anchor" else "anchor"))
        return nodes

    @staticmethod
    def render(nodes: Sequence[ChainNode], budget_chars: int = 16000) -> str:
        parts, used = [], 0
        for n in nodes:
            arrow = f"  <- {n.via} [{n.direction}]" if n.via else "  <- 锚点"
            block = f"### [d{n.depth}] {n.qualname}  ({n.path}){arrow}\n{n.body.strip()}\n"
            if used + len(block) > budget_chars:
                remain = budget_chars - used
                if remain > 200:
                    parts.append(block[:remain] + "\n…(预算截断)")
                break
            parts.append(block)
            used += len(block)
        return "\n".join(parts)

    @staticmethod
    def find_evidence(nodes: Sequence[ChainNode], patterns: Dict[str, str]) -> Dict[str, list]:
        out: Dict[str, list] = {}
        for label, pat in patterns.items():
            rx = re.compile(pat)
            hits = []
            for n in nodes:
                for line in n.body.splitlines():
                    if rx.search(line):
                        hits.append({"symbol": n.qualname, "path": n.path,
                                     "depth": n.depth, "direction": n.direction,
                                     "line": line.strip()[:150]})
                        break
            out[label] = hits
        return out


def build_chain_context(kb_path, workspace: str, question: str,
                        hops: int = 4, max_nodes: int = 28,
                        budget_chars: int = 16000, name_limit: int = 8):
    """便捷入口：返回 (上下文文本, 节点列表, 元信息)。"""
    reader = ChainReader(kb_path, workspace)
    try:
        named = reader.name_anchors(question, name_limit)
        structural = reader.structural_anchors(question)
        anchors = list(dict.fromkeys([s for s, _q in named]
                                     + [sid for sid, _q, _c in structural]))
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
