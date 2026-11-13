"""运行清单与装配指纹（R0 地基）。

依据 docs/refactor-plan.md §3 / docs/integration-plan.md §16：
  - 固定运行条件清单；「API Key 与登录凭据不要归档」——write 前强制 sanitize；
  - 装配指纹只对 systemPrompt + tools 做 SHA-256，**故意排除消息历史**
    （MiniMax assembly-fingerprint 语义：区分「改了装配」与「对话变长」，
    systemPrompt 保留精确空白——prompt cache 观察得到它）。
"""
from __future__ import annotations
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

SECRET_KEY_RE = re.compile(r"key|token|secret|password|passwd|credential|authorization|api[-_]?key", re.I)
# 「nonsecret / non_secret」是调用方自述的豁免标记（如 model_options_nonsecret），
# 不含它反而说明字段语义未知——宁可错杀也要先过 sanitize。
NONSECRET_RE = re.compile(r"non[-_]?secret", re.I)


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonicalize(value: Any, ancestors: set[int]) -> Any:
    """key 排序、去函数/undefined、防环——tool 定义 canonical 化（MiniMax 同款）。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        if id(value) in ancestors:
            raise TypeError("cannot fingerprint cyclic assembly data")
        ancestors.add(id(value))
        try:
            return [_canonicalize(v, ancestors) for v in value]
        finally:
            ancestors.discard(id(value))
    if isinstance(value, dict):
        if id(value) in ancestors:
            raise TypeError("cannot fingerprint cyclic assembly data")
        ancestors.add(id(value))
        try:
            out = {}
            for k in sorted(value.keys()):
                v = value[k]
                if v is None or callable(v):
                    continue
                out[str(k)] = _canonicalize(v, ancestors)
            return out
        finally:
            ancestors.discard(id(value))
    return str(value)


def fingerprint_assembly(system_prompt: str | None,
                         tools: Iterable[dict] | None) -> str:
    """只指纹 provider 可见的装配面；消息历史故意排除。

    systemPrompt 保留精确空白（prompt cache 观察得到）；
    tools 保序，但每个定义内部的对象 key canonical 化。
    """
    tool_list = [{"name": t.get("name", ""), "description": t.get("description", ""),
                  "parameters": t.get("parameters")} for t in (tools or [])]
    payload = json.dumps({"systemPrompt": system_prompt or "",
                          "tools": _canonicalize(tool_list, set())},
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def sanitize(value: Any) -> Any:
    """递归剔除疑似凭据字段（write 前必过）。字段名匹配 key/token/secret/password/credential/auth 即删。"""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if NONSECRET_RE.search(str(k)):
                out[str(k)] = sanitize(v)
            elif SECRET_KEY_RE.search(str(k)):
                out[str(k)] = "<redacted>"
            else:
                out[str(k)] = sanitize(v)
        return out
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str) and SECRET_KEY_RE.fullmatch(value.strip()):
        return "<redacted>"
    return value


def build_run_manifest(*, code_commit: str = "", runner_sha256: str = "",
                       runner_version: str = "1", platform: str = "",
                       python_version: str = "", model_id: str = "",
                       provider_protocol: str = "openai_compatible",
                       model_options_nonsecret: dict | None = None,
                       cases_sha256: str = "", workspace_commit: str = "",
                       knowledge_db_sha256: str = "", agents_md_sha256: str = "",
                       lexicon_sha256: str = "", toolset: list | None = None,
                       permission_mode: str = "read_only",
                       assembly_fingerprint: str = "",
                       runtime_limits: dict | None = None,
                       sample: dict | None = None,
                       artifacts: list | None = None,
                       scoring: dict | None = None) -> dict:
    m = {
        "run_id": "", "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "code_commit": code_commit, "runner_sha256": runner_sha256,
        "runner_version": runner_version, "platform": platform,
        "python_version": python_version, "model_id": model_id,
        "provider_protocol": provider_protocol,
        "model_options_nonsecret": model_options_nonsecret or {},
        "cases_sha256": cases_sha256, "workspace_commit": workspace_commit,
        "knowledge_db_sha256": knowledge_db_sha256,
        "agents_md_sha256": agents_md_sha256, "lexicon_sha256": lexicon_sha256,
        "toolset": toolset or [], "permission_mode": permission_mode,
        "assembly_fingerprint": assembly_fingerprint,
        "runtime_limits": runtime_limits or {},
        "sample": sample or {}, "artifacts": artifacts or [],
        "scoring": scoring or {"by": "external_script", "self_report_used": False},
    }
    m["run_id"] = "run_" + hashlib.sha256(
        json.dumps(m, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:12]
    return m


def write_manifest(path: Path | str, manifest: dict) -> dict:
    """sanitize 后落盘；返回实际写入内容（供断言凭据已被剔除）。"""
    clean = sanitize(manifest)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(clean, ensure_ascii=False, indent=2, default=str) + "\n",
                 encoding="utf-8")
    return clean
