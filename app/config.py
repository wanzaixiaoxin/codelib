"""配置加载：YAML + 环境变量占位符 ${VAR}。

保持极简：
- 一个代码目录（workspace）
- 一个大模型服务（llm：type/base_url/api_key/model）
- 渠道按需开启
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULT_INCLUDE = [
    "*.py", "*.pyi", "*.java", "*.go", "*.ts", "*.tsx", "*.js", "*.jsx",
    "*.mjs", "*.cjs", "*.kt", "*.kts", "*.rs", "*.c", "*.h", "*.cc", "*.cpp",
    "*.hpp", "*.cs", "*.php", "*.rb", "*.scala", "*.lua", "*.sql", "*.proto",
    "*.md", "*.markdown", "*.rst", "*.txt", "*.yaml", "*.yml", "*.json",
    "*.toml", "*.ini", "*.cfg", "*.conf",
]

DEFAULT_EXCLUDE_DIRS = [
    ".git", ".svn", ".hg", ".idea", ".vscode", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".venv", "venv", "env", "node_modules", "bower_components",
    "dist", "build", "out", "target", "bin", "obj", "vendor", "third_party",
    "logs", "log", "data", "cache", ".cache", "coverage", ".next", ".nuxt",
    "chrome", "BDPlugins", "ZDPlugins", "SEPlugins", "SEPlugins64", "excel",
]

DEFAULT_EXCLUDE_GLOBS = [
    "*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json",
    "pnpm-lock.yaml", "yarn.lock", "*.log", "*.rar", "*.zip", "*.7z", "*.tar",
    "*.gz", "*.exe", "*.dll", "*.so", "*.dylib", "*.bin", "*.dat", "*.db",
    "*.sqlite", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.ico", "*.bmp",
    "*.pdf", "*.doc", "*.docx", "*.xls", "*.xlsx", "*.ppt", "*.pptx", "*.mp4",
    "*.mp3", "*.woff", "*.woff2", "*.ttf", "*.eot", "*.class", "*.jar", "*.war",
]

_INDEX_KEYS = ("include", "exclude_dirs", "exclude_globs", "max_file_bytes",
               "snippet_lines", "max_signatures_per_file", "max_chunks_per_file",
               "auto_reindex_on_query", "watch_interval_seconds", "deep_read_top_k")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            name = m.group(1)
            default = m.group(2) if m.group(2) is not None else ""
            return os.environ.get(name, default)
        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def _as_list(value: Any, default: List[str]) -> List[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        return [value]
    return list(value)


@dataclass
class IndexConfig:
    include: List[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE))
    exclude_dirs: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_DIRS))
    exclude_globs: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS))
    max_file_bytes: int = 512 * 1024
    snippet_lines: int = 12
    max_signatures_per_file: int = 80
    max_chunks_per_file: int = 120
    auto_reindex_on_query: bool = True
    watch_interval_seconds: int = 0
    deep_read_top_k: int = 2

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "IndexConfig":
        d = d or {}
        return cls(
            include=_as_list(d.get("include"), DEFAULT_INCLUDE),
            exclude_dirs=_as_list(d.get("exclude_dirs"), DEFAULT_EXCLUDE_DIRS),
            exclude_globs=_as_list(d.get("exclude_globs"), DEFAULT_EXCLUDE_GLOBS),
            max_file_bytes=int(d.get("max_file_bytes", 512 * 1024)),
            snippet_lines=int(d.get("snippet_lines", 12)),
            max_signatures_per_file=int(d.get("max_signatures_per_file", 80)),
            max_chunks_per_file=int(d.get("max_chunks_per_file", 120)),
            auto_reindex_on_query=bool(d.get("auto_reindex_on_query", True)),
            watch_interval_seconds=int(d.get("watch_interval_seconds", 0)),
            deep_read_top_k=int(d.get("deep_read_top_k", 2)),
        )


@dataclass
class WorkspaceConfig:
    id: str
    path: str
    name: str = ""
    description: str = ""
    index: IndexConfig = field(default_factory=IndexConfig)

    @classmethod
    def from_dict(cls, d: Dict[str, Any], base_dir: Path) -> "WorkspaceConfig":
        raw_path = str(d.get("path") or "").strip()
        if not raw_path:
            raise ValueError("workspace.path 不能为空")
        p = Path(os.path.expanduser(raw_path))
        if not p.is_absolute():
            p = (base_dir / p).resolve()
        # id 可选：缺省用目录名，便于客户端直接用目录名作为 workspace
        wid = str(d.get("id") or p.name or "default").strip() or "default"
        # 允许把索引选项直接平铺在 workspace 下（更简洁）；上下文预算统一归 knowledge.retrieve 管理。
        idx_raw = d.get("index")
        if idx_raw is None:
            idx_raw = {k: d[k] for k in _INDEX_KEYS if k in d}
        return cls(
            id=wid,
            path=str(p),
            name=str(d.get("name") or wid),
            description=str(d.get("description") or ""),
            index=IndexConfig.from_dict(idx_raw),
        )


@dataclass
class LLMConfig:
    """单个大模型服务配置。为空（无 base_url/model）即视为未启用。"""
    type: str = "openai_compatible"   # openai_compatible | azure_openai | anthropic | gemini
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    api_version: str = ""
    deployment: str = ""
    temperature: float = 0.2
    max_tokens: int = 900
    timeout: int = 60

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "LLMConfig":
        d = d or {}
        # 兼容此前「多提供商」写法：取 active 指向的那一个
        if d.get("providers"):
            provs = [p for p in (d["providers"] or []) if isinstance(p, dict)]
            chosen = next((p for p in provs if p.get("name") == d.get("active")),
                          provs[0] if provs else {})
            merged = dict(chosen)
            for k in ("temperature", "max_tokens", "timeout"):
                merged.setdefault(k, d.get(k))
            d = merged
        return cls(
            type=str(d.get("type") or d.get("provider") or "openai_compatible"),
            base_url=str(d.get("base_url", "")).rstrip("/"),
            api_key=str(d.get("api_key", "")),
            model=str(d.get("model", "")),
            api_version=str(d.get("api_version", "")),
            deployment=str(d.get("deployment", "")),
            temperature=float(d.get("temperature", 0.2)),
            max_tokens=int(d.get("max_tokens", 900)),
            timeout=int(d.get("timeout", 60)),
        )

    @property
    def requires_key(self) -> bool:
        host = (self.base_url or "").lower()
        if self.type == "ollama":
            return False
        if "127.0.0.1" in host or "localhost" in host:
            return False
        return self.type in ("openai_compatible", "openai", "azure_openai",
                             "anthropic", "gemini")

    @property
    def available(self) -> bool:
        if not self.model and not self.base_url:
            return False
        return (not self.requires_key) or bool(self.api_key)

    @property
    def enabled(self) -> bool:
        return self.available

    def public_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "base_url": self.base_url, "model": self.model,
                "available": self.available, "requires_key": self.requires_key}


@dataclass
class AppConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    data_dir: str = "data"
    read_only: bool = True          # 强制只读：不提供任何写工程文件的接口
    default_workspace: str = ""
    workspaces: List[WorkspaceConfig] = field(default_factory=list)
    llm: LLMConfig = field(default_factory=LLMConfig)
    channels: Dict[str, Any] = field(default_factory=dict)
    memory: Dict[str, Any] = field(default_factory=dict)
    knowledge: Dict[str, Any] = field(default_factory=dict)
    auth: Dict[str, Any] = field(default_factory=dict)
    config_path: str = ""

    @property
    def knowledge_db_path(self) -> Path:
        raw = (self.knowledge or {}).get("db")
        if raw:
            p = Path(os.path.expanduser(str(raw)))
            if not p.is_absolute():
                p = (Path(self.data_dir).parent / p).resolve()
            return p
        return Path(self.data_dir) / "knowledge.db"

    @property
    def knowledge_enabled(self) -> bool:
        kb = self.knowledge or {}
        if "enabled" in kb:
            return bool(kb["enabled"])
        return True  # 默认尝试；库为空时 bot 会自动回退到旧检索

    def workspace(self, wid: Optional[str] = None) -> WorkspaceConfig:
        if not self.workspaces:
            raise ValueError("配置中没有定义 workspace（请设置 workspace.path）")
        if not wid:
            if self.default_workspace:
                for w in self.workspaces:
                    if w.id == self.default_workspace:
                        return w
            return self.workspaces[0]
        # 依次按 id / name / 目录名匹配
        for w in self.workspaces:
            if wid in (w.id, w.name) or Path(w.path).name == wid:
                return w
        # 只有一个工作区时容忍多余的名字，避免接口报错
        if len(self.workspaces) == 1:
            return self.workspaces[0]
        raise KeyError(f"未知 workspace: {wid}，可选：{[w.id for w in self.workspaces]}")

    def channel(self, name: str) -> Dict[str, Any]:
        return self.channels.get(name) or {}

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "codelib.db"

    def public_dict(self) -> Dict[str, Any]:
        return {
            "workspace": [{"id": w.id, "name": w.name, "path": w.path} for w in self.workspaces],
            "llm": self.llm.public_dict(),
        }

    @classmethod
    def load(cls, path: Optional[str] = None) -> "AppConfig":
        raw_path = path or os.environ.get("CODELIB_CONFIG") or "config/config.yaml"
        cfg_file = Path(raw_path).expanduser()
        if not cfg_file.is_absolute():
            cfg_file = (Path.cwd() / cfg_file).resolve()
        if not cfg_file.exists():
            raise FileNotFoundError(f"配置文件不存在: {cfg_file}")
        with cfg_file.open("r", encoding="utf-8") as fh:
            data = _expand_env(yaml.safe_load(fh) or {})

        project_root = cfg_file.parent.parent
        app = data.get("app") or {}
        data_dir_raw = str(app.get("data_dir", "data"))
        data_dir = Path(os.path.expanduser(data_dir_raw))
        if not data_dir.is_absolute():
            data_dir = (project_root / data_dir).resolve()

        ws_list: List[WorkspaceConfig] = []
        if data.get("workspaces"):
            ws_list = [WorkspaceConfig.from_dict(w, project_root) for w in data["workspaces"]]
        elif data.get("workspace"):
            ws_list = [WorkspaceConfig.from_dict(data["workspace"], project_root)]

        return cls(
            host=str(app.get("host", "0.0.0.0")),
            port=int(app.get("port", 8000)),
            log_level=str(app.get("log_level", "INFO")),
            data_dir=str(data_dir),
            read_only=bool(app.get("read_only", True)),
            default_workspace=str(data.get("default_workspace") or
                                  (ws_list[0].id if ws_list else "")),
            workspaces=ws_list,
            llm=LLMConfig.from_dict(data.get("llm")),
            channels=data.get("channels") or {},
            memory=data.get("memory") or {},
            knowledge=data.get("knowledge") or {},
            auth=data.get("auth") or {},
            config_path=str(cfg_file),
        )
