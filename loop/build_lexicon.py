"""从自动评估标注里学「中文业务术语 -> 检索词」映射，写入独立 lexicon.db。

只学 retrieval/mixed 失败且期望路径非空的题；扩展词来自期望文档文件名、
期望符号，绝不来自当前答案或系统自己召回的内容。
用法: python -B loop/build_lexicon.py [--out data/lexicon.db]
"""
from __future__ import annotations
import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
from app.lexicon import SCHEMA, DEFAULT_PATH  # noqa: E402

STOP = {"业务", "架构", "文档", "说明", "逻辑", "规则", "设计", "数据", "方案", "历史",
        "验收", "场景", "流程", "管理", "实现", "代码", "工程", "系统", "信息", "接口",
        "概览", "介绍", "示例", "模块", "整体", "主要", "问题", "处理", "机制", "当前",
        "支持", "区别", "一致", "入口", "回答", "结果", "包含", "方式", "情况"}


BAD_KEY = re.compile(r"[不没别些中个和与有是在会能要被把对从为这那]")
SUFFIX_SPLIT = re.compile(r"(?=接口文档|业务概览|处理逻辑|设计|说明|规则|逻辑|文档|接口|流程|概览)")

def keywords_from_path(p: str):
    """按语义边界切文件名，不做机械 2-4 字切片（会切出无意义碎片）。"""
    name = re.sub(r"^" + chr(92) + "d+[-_]?", "", Path(p).stem)
    words = []
    for part in re.split(r"[与和及、，, ]+", re.sub(r"[-_]+", " ", name)):
        for seg in SUFFIX_SPLIT.split(part):
            seg = seg.strip()
            if 2 <= len(seg) <= 8 and seg not in STOP:
                words.append(seg)
    words += re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", name)
    return [x for x in dict.fromkeys(words) if len(x) >= 2]


def key_ok(key: str, evidence: str) -> bool:
    """key 须在期望文档名里有 2 字重合，过滤"一致/区别"这类泛词。"""
    if not re.fullmatch(r"[" + chr(92) + "u4e00-" + chr(92) + "u9fff]{2,6}", key) or BAD_KEY.search(key):
        return False
    return any(key[i:i + 2] in evidence for i in range(len(key) - 1))


def main():
    import yaml
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(HERE / "frozen" / "failure_cases.yaml"))
    ap.add_argument("--out", default=str(DEFAULT_PATH))
    args = ap.parse_args()
    cases = yaml.safe_load(Path(args.cases).read_text(encoding="utf-8"))
    mapping = {}
    for c in cases:
        if c.get("failure_layer") not in ("retrieval", "mixed"):
            continue
        paths = [str(x) for x in (c.get("expected_paths") or [])]
        if not paths:
            continue
        vals = []
        for p in paths[:2]:
            vals.extend(keywords_from_path(p))
        vals.extend(s for s in (c.get("expected_symbols") or [])
                    if re.match(r"^[A-Za-z_]", str(s)))
        vals = [v for v in dict.fromkeys(vals) if len(v) >= 2][:5]
        if not vals:
            continue
        evidence = " ".join(Path(x).stem for x in paths)
        keys = [str(t) for t in (c.get("expected_intent_terms") or [])
                if key_ok(str(t), evidence)]
        for k in keys[:3]:
            mapping.setdefault(k, set()).update(vals)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(out)
    con.execute(SCHEMA)
    con.execute("DELETE FROM lexicon")
    for k, vals in sorted(mapping.items()):
        con.execute("INSERT OR REPLACE INTO lexicon(term,expansions,source,updated_at)"
                    " VALUES(?,?,?,?)",
                    (k, json.dumps(sorted(vals)[:5], ensure_ascii=False), "auto_eval", time.time()))
    con.commit()
    con.close()
    print(f"生成 {len(mapping)} 条映射 -> {out}")
    for k, vals in list(sorted(mapping.items()))[:8]:
        print(f"  {k} -> {sorted(vals)[:5]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
