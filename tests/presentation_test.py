"""回答可读性、引用偏好与禁止代码细节测试。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.bot import BotService
from app.config import AppConfig, WorkspaceConfig
from app.presentation import has_code_details, response_preferences
from app.store import Store


class FakeLLM:
    available = True
    name = "fake"
    last_raw_finish = "stop"
    last_raw_usage = {"prompt_tokens": 10, "completion_tokens": 10}
    last_error = ""

    async def chat(self, system: str, user: str):
        if "自然语言说明编辑" in system:
            return "服务启动时会读取配置，完成必要初始化后进入运行状态。配置缺失或初始化失败时应停止启动并提示检查环境。"
        return (
            "Service.run 用于启动服务，会读取配置并完成初始化。见 src/app.py:1-5。\n"
            "```python\nService.run()\n```\n"
            "启动失败时会使用 error_message 记录原因，并返回检查结果。相关逻辑位于 Service.run 中。"
        )


def main() -> int:
    assert response_preferences("说明启动过程")["inline_citations"] is False
    assert response_preferences("不需要展示代码，也不要函数名")["no_code"] is True
    history = [{"role": "user", "content": "不要代码和字段名"}]
    assert response_preferences("继续说明", history)["no_code"] is True
    assert response_preferences("现在需要代码示例", history)["no_code"] is False

    tmp = Path(tempfile.mkdtemp(prefix="codelib_present_"))
    lib = tmp / "lib"
    lib.mkdir()
    (lib / "app.py").write_text(
        "class Service:\n    \"\"\"服务。\"\"\"\n    def run(self):\n        \"\"\"启动服务并读取配置。\"\"\"\n        return 'ok'\n", encoding="utf-8")
    ws = WorkspaceConfig(id="lib", name="lib", path=str(lib))
    cfg = AppConfig(data_dir=str(tmp / "data"), workspaces=[ws],
                    knowledge={"enabled": True, "rerank": {"mode": "none"}})
    bot = BotService(cfg, Store(cfg.db_path), llm=FakeLLM())

    default = asyncio.run(bot.handle("lib", "Service.run 是做什么的？", "default"))
    assert default.llm_used and default.citations
    assert "src/app.py:1-5" not in default.text
    assert "见 src/app.py" not in default.text

    cited = asyncio.run(bot.handle("lib", "Service.run 做什么？请给出行号来源", "cited"))
    assert "src/app.py:1-5" in cited.text
    assert cited.citations and len(cited.citations) <= 3

    clean = asyncio.run(bot.handle(
        "lib", "不需要展示代码，也不要函数名和字段名，说明服务如何启动", "clean"))
    assert clean.llm_used and clean.citations == []
    assert not has_code_details(clean.text), clean.text
    assert all(x not in clean.text for x in ("Service.run", "error_message", "src/app.py", "```"))
    assert "读取配置" in clean.text

    prompt = bot._build_prompt(ws, "继续说明", "证据", "", history)
    assert "禁止代码块、伪代码、函数名、类名、字段名" in prompt
    print("回答可读性与展示偏好测试通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
