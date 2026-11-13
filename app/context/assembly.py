"""分层上下文装配。

依据 docs/refactor-plan.md §3：
  - 六块独立预算、按 profile 整块省略（Kimi systemPromptVars + MiniMax lightweight 形态）；
  - AGENTS.md 逐层合并（用户级 → 工程根 → 子目录）、来源标注、>32KB 告警（Kimi context.ts 数值）；
  - 装配指纹只算 systemPrompt + tools，排除消息历史。
R0 只提供装配能力，不改变任何现有行为；research.enabled 接线在 R3。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

from app.research.manifest import fingerprint_assembly

BLOCKS = ("identity", "workspace", "skills", "memory", "evidence", "environment")
AGENTS_MD_WARN_BYTES = 32 * 1024
AGENTS_MD_NAMES = ("AGENTS.md", "agents.md")


@dataclass
class Assembled:
    text: str
    fingerprint: str
    profile: str
    included: list[str] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def assemble(profile: str, blocks: dict[str, str],
             total_budget: int | None = None,
             tools: list[dict] | None = None,
             keep_first: list[str] | None = None) -> Assembled:
    """分层装配：keep_first 中的块优先占用预算（如 evidence/identity），
    其余按 BLOCKS 顺序纳入；超预算整块省略（不截半截文本），omitted 可追溯。
    输出顺序恒按 BLOCKS，与省略顺序解耦。"""
    included: set[str] = set()
    omitted: list[str] = []
    warnings: list[str] = []
    sizes = {b: len((blocks.get(b) or "").strip()) for b in BLOCKS
             if (blocks.get(b) or "").strip()}
    keep = list(dict.fromkeys(keep_first or []))
    order = keep + [b for b in BLOCKS if b not in keep]
    used = 0
    for name in order:
        if name not in sizes:
            continue
        if total_budget and total_budget > 0 and used + sizes[name] > total_budget:
            omitted.append(name)
            if name in keep:
                warnings.append(f"keep block over budget: {name}")
            continue
        used += sizes[name]
        included.add(name)
    parts = []
    for name in BLOCKS:
        if name in included:
            parts.append(f"## {name}" + chr(10) + (blocks.get(name) or "").strip())
    text = (chr(10) * 2).join(parts)
    return Assembled(text=text, fingerprint=fingerprint_assembly(text, tools or []),
                     profile=profile, included=sorted(included), omitted=omitted,
                     warnings=warnings)


def collect_agents_md(roots: list[Path]) -> tuple[str, list[str], list[str]]:
    """逐层收集 AGENTS.md：每个 root 内按给定顺序取第一个命中的文件名，
    跨 root 去重（规范化路径）；每段前加 `<!-- From: path -->` 来源标注。

    返回 (合并文本, 来源路径列表, 告警列表)。超大（>32KB）只告警不截断——
    截断会静默丢指令，Kimi 的做法是告警由调用方决定。
    """
    seen: set[str] = set()
    files: list[Path] = []
    warnings: list[str] = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        picked = False
        for name in AGENTS_MD_NAMES:
            f = root / name
            try:
                rp = f.resolve()
            except OSError:
                continue
            key = str(rp).lower()
            if rp.is_file() and key not in seen:
                seen.add(key)
                files.append(f)
                picked = True
            if picked:
                break
        if not picked:
            warnings.append(f"no AGENTS.md under {root}")
    chunks, sources = [], []
    for f in files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            warnings.append(f"read failed {f}: {e}")
            continue
        sources.append(str(f))
        if len(content.encode("utf-8")) > AGENTS_MD_WARN_BYTES:
            warnings.append(f"AGENTS.md over {AGENTS_MD_WARN_BYTES}B: {f}")
        chunks.append(f"<!-- From: {f} -->\n{content.strip()}")
    return "\n\n".join(chunks), sources, warnings


def agents_md_sha256(roots: list[Path]) -> str:
    """合并内容 hash——进 run_manifest（AGENTS.md 是我们检索最常召回的文档，
    内容一变结果就变，必须纳入固定运行条件）。"""
    from app.research.manifest import sha256_text
    text, _sources, _warnings = collect_agents_md(roots)
    return sha256_text(text) if text else ""
