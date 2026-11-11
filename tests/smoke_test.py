"""自包含冒烟测试：建索引 -> 问答 -> 记忆召回 -> 变更感知 -> 增量重建。

运行：python tests/smoke_test.py
无需 pytest、无需外部依赖（LLM 关闭时走检索摘要降级）。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:  # Windows 控制台默认 GBK，统一成 UTF-8 便于中文输出
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from app import indexer, retrieval  # noqa: E402
from app.bot import BotService  # noqa: E402
from app.config import AppConfig, IndexConfig, WorkspaceConfig  # noqa: E402
from app.store import Store  # noqa: E402

FILES = {
    "src/gateway.py": '''"""网关模块。"""
class MessageGateway:
    """负责路由消息。"""

    def route(self, msg):
        """根据命令前缀路由到处理器。"""
        return "ok"


def build_gateway(store):
    """构建一个网关实例。"""
    return MessageGateway()
''',
    "src/storage.py": '''"""存储模块。"""
class MemoryStore:
    """保存事实。"""

    def add_fact(self, text):
        """写入一条事实。"""
        pass

    def search(self, query):
        """按关键词检索事实。"""
        return []
''',
    "docs/architecture.md": "# 架构\n\n## 消息流\n\n渠道 -> 网关 -> 处理器 -> 存储。\n\n## 扩展\n\n新增渠道只需注册 responder。\n",
}


def build_project(root: Path) -> None:
    for rel, content in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="codelib_smoke_"))
    proj = tmp / "proj"
    build_project(proj)

    cfg = AppConfig(
        data_dir=str(tmp / "data"),
        default_workspace="demo",
        workspaces=[WorkspaceConfig(
            id="demo", name="Demo", path=str(proj),
            index=IndexConfig(auto_reindex_on_query=True),
        )],
        memory={"enabled": True},
    )
    store = Store(cfg.db_path)
    ws = cfg.workspace("demo")

    print("== 1. 建索引 ==")
    res = indexer.index_workspace(store, ws, full=True)
    print({k: res[k] for k in ("scanned_files", "indexed", "chunks_written",
                               "chunk_tokens", "corpus_tokens", "token_saving_ratio")})
    assert res["indexed"] >= 3 and res["chunks_written"] > 0, "索引失败"

    print("\n== 2. 问答（无 LLM，走检索摘要）==")
    bot = BotService(cfg, store)
    ans = asyncio.run(bot.handle("demo", "消息网关如何路由消息？", "s1"))
    print(ans.text[:400])
    print("citations:", [c.label() for c in ans.citations])
    assert ans.citations, "应检索到引用"
    assert any("gateway.py" in c.path for c in ans.citations), "应命中 gateway.py"

    print("\n== 3. 降级摘要不进入答案记忆/缓存 ==")
    ans2 = asyncio.run(bot.handle("demo", "消息网关如何路由消息？", "s2"))
    print("memory_hits:", len(ans2.memory_hits), "| llm_used:", ans2.llm_used)
    assert not ans2.memory_hits and not ans2.llm_used, "检索摘要不应被当作可靠答案记忆"

    print("\n== 4. 变更感知 ==")
    gw = proj / "src" / "gateway.py"
    gw.write_text(gw.read_text(encoding="utf-8") + '''

def new_feature():
    """新增的功能函数。"""
    return 42
''', encoding="utf-8")
    ch = indexer.compute_changes(store, ws)
    print("changes:", ch.to_dict())
    assert "src/gateway.py" in ch.modified, "应检测到 gateway.py 被修改"

    print("\n== 5. 增量重建 ==")
    res2 = indexer.index_workspace(store, ws, full=False)
    retrieval.invalidate(ws.id)
    print({k: res2[k] for k in ("modified", "unchanged", "chunks_written")})
    assert res2["modified"] == 1 and res2["unchanged"] >= 2, "增量重建应只处理改动文件"

    print("\n== 6. 答案中应能召回新增函数 ==")
    ans3 = asyncio.run(bot.handle("demo", "new_feature 函数是做什么的？", "s3"))
    assert any("gateway.py" in c.path for c in ans3.citations), "应命中新增函数所在文件"

    print("\n全部冒烟测试通过 [OK]")
    print("临时目录：", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
