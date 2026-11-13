"""项目记忆：研究结果 → card 表（fingerprint 增量 + 写入门控）。"""
from __future__ import annotations
import json
from pathlib import Path

CARD_PROMPT = """你是项目知识沉淀员。把一次代码研究的结论提炼成 0~3 张「项目理解卡」。
卡片类型（type 只能是这三个）：
- entry_chain: 入口/调用链（如「消息从 X 进入，经 Y，最后写入 Z」）
- concurrency_model: 并发与位点模型（哪里起协程、位点何时提交、顺序约束在哪）
- state_ownership: 状态归属（Redis key 结构、DB 表、Kafka topic 与 key 规则）

要求：只沉淀**本次研究中有代码证据支撑**的结论；body 里每条因果带 `path :: symbol`；
不要编造。只输出 JSON：{"cards": [{"type": "...", "title": "...", "body": "...",
"links": "...", "source_refs": "..."}]}"""


async def save_cards(llm, kb, workspace: str, question: str, result: dict,
                     max_cards: int = 3, disable_write: bool = False) -> list[str]:
    """研究结果 → card 表。返回每个 card 的写入状态。"""
    if disable_write:
        return ["skipped: memory.disable_write"]
    payload = json.dumps({"question": question,
                          "conclusions": result.get("conclusions", []),
                          "evidence": result.get("evidence", []),
                          "board": json.loads(result.get("board_dump") or "{}")},
                         ensure_ascii=False, default=str)[:12000]
    from app.research.loop import extract_json
    resp = await llm.chat(CARD_PROMPT, f"问题：{question}\n\n研究结果：\n{payload}")
    data = extract_json(resp or "") or {}
    cards = [c for c in (data.get("cards") or []) if isinstance(c, dict) and c.get("body")][:max_cards]
    if not cards:
        return ["skipped: 无可沉淀结论"]
    out = []
    for c in cards:
        try:
            title_by_type = {"entry_chain": "入口与调用链", "concurrency_model": "并发与位点模型",
                             "state_ownership": "状态归属"}
            ttype = str(c.get("type", "entry_chain"))[:32]
            out.append(upsert_card(kb, workspace, ttype, title_by_type.get(ttype, ttype)[:120],
                                   str(c.get("body", "")), str(c.get("links", "")),
                                   str(c.get("source_refs", ""))))
        except Exception as e:
            out.append(f"write_failed:{e}")
    return out


def render_cards(cards: list) -> str:
    if not cards:
        return ""
    lines = ["以下是此前研究沉淀的项目理解卡（path 引用可用工具进一步核查）："]
    for c in cards:
        lines.append(f"- [{c['type']}] {c['title']}: {str(c['body'])[:300]}")
    return chr(10).join(lines)


# ---------- card 表操作（独立实现，不改 KnowledgeStore——大文件 patch 已证明脆弱） ----------

def upsert_card(kb_path: Path, workspace: str, type_: str, title: str, body: str,
                links: str = "", source_refs: str = "") -> str:
    """fingerprint 增量：内容未变 hit / 变了 updated / 新建 created:<id>。幂等。"""
    import hashlib as _h
    import time as _t
    import sqlite3
    fp = _h.sha256((body or "").encode("utf-8")).hexdigest()[:16]
    conn = sqlite3.connect(Path(kb_path).as_posix())
    try:
        row = conn.execute("SELECT id,fingerprint FROM card WHERE workspace=? AND type=? AND title=?",
                           (workspace, type_, title)).fetchone()
        now = _t.time()
        if row and row[1] == fp:
            conn.execute("UPDATE card SET updated_at=? WHERE id=?", (now, row[0]))
            conn.commit()
            return "hit"
        if row:
            conn.execute("UPDATE card SET body=?,links=?,source_refs=?,hash=?,updated_at=? WHERE id=?",
                         (body, links, source_refs, fp, now, row[0]))
            conn.commit()
            return "updated"
        cid = "card_" + _h.sha256((workspace + type_ + title + str(now)).encode()).hexdigest()[:12]
        conn.execute("INSERT INTO card(id,workspace,type,title,body,links,source_refs,hash,"
                     "created_at,updated_at,fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (cid, workspace, type_, title, body, links, source_refs, fp, now, now, fp))
        conn.commit()
        return "created:" + cid
    finally:
        conn.close()


def search_cards(kb_path: Path, workspace: str, query: str, limit: int = 5) -> list:
    """card 数量小，LIKE 级匹配足够；标题命中权重 3、正文 1，相对分数地板防低分噪声。"""
    import sqlite3
    q = (query or "").strip().lower()
    import re as _re
    cjk = _re.findall("[u4e00-u9fff]{2,}".replace("u", chr(92) + "u"), q)
    bigrams = [c[i2:i2 + 2] for c in cjk for i2 in range(len(c) - 1)]
    words = ([w for w in q.split() if len(w) >= 2] + cjk + bigrams)[:24] or [q]
    if not q:
        return []
    conn = sqlite3.connect("file:" + Path(kb_path).as_posix() + "?mode=ro", uri=True)
    try:
        scored = []
        for r in conn.execute("SELECT id,type,title,body,links,source_refs FROM card WHERE workspace=?",
                              (workspace,)):
            score = sum(3 for w in words if w in str(r[2]).lower()) + sum(
                1 for w in words if w in (str(r[2]) + " " + str(r[3])).lower())
            if score:
                scored.append((score, r))
        if not scored:
            return []
        ranked = sorted(scored, key=lambda x: -x[0])
        floor = ranked[0][0] * 0.15
        return [{"id": r[0], "type": r[1], "title": r[2], "body": str(r[3])[:400],
                 "links": r[4], "source_refs": r[5]}
                for s, r in ranked if s >= floor][:limit]
    finally:
        conn.close()
