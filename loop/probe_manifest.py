"""R0 验收：manifest 字段齐全、指纹两跑稳定、凭据剔除、card 迁移幂等。
跑法: python -B loop/probe_manifest.py
"""
from __future__ import annotations
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.research.manifest import (build_run_manifest, fingerprint_assembly,
                                   sanitize, sha256_file, write_manifest)
from app.context.assembly import assemble, collect_agents_md, agents_md_sha256
from app.knowledge.schema import KnowledgeStore

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILED.append(name)


def main() -> int:
    # 1. 指纹两跑稳定 + 对变化敏感
    tools = [{"name": "read_body", "description": "d", "parameters": {"b": 2, "a": 1}},
             {"name": "grep", "description": "g", "parameters": {}}]
    fp1 = fingerprint_assembly("Hello   world", tools)
    fp2 = fingerprint_assembly("Hello   world", tools)  # 相同输入两跑
    check("装配指纹两跑稳定", fp1 == fp2, fp1)
    # MiniMax 语义：工具保序（prompt cache 按顺序观察）→ 乱序 = 不同装配，是特性
    fp_rev = fingerprint_assembly("Hello   world", list(reversed(tools)))
    check("指纹对工具顺序敏感（保序特性）", fp_rev != fp1)
    fp3 = fingerprint_assembly("Hello  world", tools)  # 少一个空格
    check("指纹对 systemPrompt 空白敏感", fp3 != fp1)
    fp4 = fingerprint_assembly("Hello   world", tools + [{"name": "new_tool"}])
    check("指纹对工具面变化敏感", fp4 != fp1)

    # 2. 分层装配：预算省略 + omitted 可追溯
    blocks = {"identity": "I" * 50, "workspace": "W" * 2000, "evidence": "E" * 3000,
              "skills": "S" * 8000, "memory": "M" * 4000, "environment": "N" * 100}
    keep_first = ["identity", "evidence", "environment"]
    a = assemble("reader", blocks, total_budget=12000, keep_first=keep_first)
    check("装配包含 keep 块（evidence/identity）",
          "evidence" in a.included and "identity" in a.included and "environment" in a.included,
          f"included={a.included}")
    check("装配整块省略低优先块", "skills" in a.omitted, f"omitted={a.omitted}")
    check("装配指纹非空且稳定",
          a.fingerprint == assemble("reader", blocks, 12000, keep_first=keep_first).fingerprint)

    # 3. AGENTS.md 收集 + hash（真实知识库工程）
    from app.config import AppConfig
    cfg = AppConfig.load(str(ROOT / "config" / "config.yaml"))
    roots = [Path(w.path) for w in cfg.workspaces if Path(w.path).is_dir()]
    # 真实知识库工程（只读）不在 config 里（config 只配了示例库），显式加入
    real_ws = ROOT.parent / "code" / "pri_nlp_worker_svr"
    if real_ws.is_dir() and real_ws not in roots:
        roots.append(real_ws)
    text, sources, warns = collect_agents_md(roots)
    h1 = agents_md_sha256(roots)
    h2 = agents_md_sha256(roots)
    check("AGENTS.md 命中", bool(sources), f"{len(sources)} 个来源")
    check("AGENTS.md hash 两跑稳定", h1 == h2 and len(h1) == 64, f"{h1[:12]}…")
    check("来源标注存在", "<!-- From: " in text)

    # 4. manifest 字段齐全 + 凭据剔除 + 落盘
    m = build_run_manifest(
        model_id="workbuddy/glm-5.3", provider_protocol="openai_compatible",
        agents_md_sha256=h1, toolset=["read_body", "grep", "callers"],
        assembly_fingerprint=fp1,
        runtime_limits={"max_steps": 12, "max_chars": 24000},
        sample={"session_id": "s1", "api_key_SHOULD_NOT_APPEAR": "sk-xxx"})
    m["code_commit"] = "local"  # 占位：接入 git 在 R3
    clean = write_manifest(ROOT / "loop" / "reports" / "run_manifest.sample.json", m)
    need = ("run_id", "created_at", "code_commit", "runner_sha256", "platform",
            "model_id", "provider_protocol", "cases_sha256", "agents_md_sha256",
            "lexicon_sha256", "toolset", "permission_mode", "assembly_fingerprint",
            "runtime_limits", "sample", "scoring")
    check("manifest 字段齐全", all(k in clean for k in need),
          f"缺失={[k for k in need if k not in clean]}")
    blob = (ROOT / "loop" / "reports" / "run_manifest.sample.json").read_text(encoding="utf-8")
    check("凭据被剔除（api_key 不落盘）", "sk-xxx" not in blob and "<redacted>" in blob)

    # 5. card 迁移 0023：临时副本幂等生效（不动主库）
    tmp = Path(tempfile.mkdtemp(prefix="codelib_r0_"))
    try:
        shutil.copy2(ROOT / "data" / "knowledge.db", tmp / "knowledge.db")
        kb = KnowledgeStore(tmp / "knowledge.db")
        cols = {r[1] for r in sqlite3.connect(tmp / "knowledge.db").execute("PRAGMA table_info(card)")}
        check("card 迁移补列", {"created_at", "updated_at", "fingerprint"} <= cols, str(sorted(cols)))
        kb._migrate()  # 二次执行：幂等
        cols2 = {r[1] for r in sqlite3.connect(tmp / "knowledge.db").execute("PRAGMA table_info(card)")}
        check("迁移幂等（二次执行不炸不重复）", cols2 == cols)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 6. 主库未被触碰（只读核验）
    mc = sqlite3.connect(f"file:{(ROOT/'data'/'knowledge.db').as_posix()}?mode=ro", uri=True)
    mcols = {r[1] for r in mc.execute("PRAGMA table_info(card)")}
    check("主库 card 未被 R0 触碰", "fingerprint" not in mcols, "迁移只对副本执行")

    print("\nR0 验收: " + ("ALL PASS" if not FAILED else f"FAILED {FAILED}"))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
