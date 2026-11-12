"""IM（企业微信/飞书）选库交互测试：不联网，验证多库选择/切换/隔离。"""
from __future__ import annotations

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

from app.config import AppConfig, IndexConfig, WorkspaceConfig  # noqa: E402
from app.im import IMConversation  # noqa: E402
from app.store import Store  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="codelib_im_"))
    cfg = AppConfig(
        data_dir=str(tmp / "data"),
        workspaces=[
            WorkspaceConfig(id="gateway", name="消息网关", path=str(ROOT / "sample_workspace"),
                            index=IndexConfig()),
            WorkspaceConfig(id="other", name="另一个库", path=str(ROOT / "sample_workspace"),
                            index=IndexConfig()),
        ],
    )
    store = Store(cfg.db_path)
    im = IMConversation(cfg, store)
    u = "userA"

    print("== 1. 未选库直接提问 -> 要求先选库 ==")
    d = im.handle("wecom", u, "有多少公共模块")
    print(d["action"])
    assert d["action"] == "need_select"
    assert "消息网关" in d["reply"] and "另一个库" in d["reply"]

    print("\n== 2. 回复序号选库 ==")
    d = im.handle("wecom", u, "2")
    print(d["action"], d["workspace_id"])
    assert d["action"] == "selected" and d["workspace_id"] == "other"
    assert store.get_im_binding("wecom", u) == "other"

    print("\n== 3. 选库后提问 -> 用绑定库 ==")
    d = im.handle("wecom", u, "这个工程是做什么的")
    print(d["action"], d["workspace_id"])
    assert d["action"] == "ask" and d["workspace_id"] == "other"

    print("\n== 4. 账号隔离：另一个用户未选库 ==")
    d2 = im.handle("wecom", "userB", "hi")
    assert d2["action"] == "need_select"
    assert store.get_im_binding("wecom", "userB") is None

    print("\n== 5. 切库（再发 1）==")
    d = im.handle("wecom", u, "1")
    assert d["action"] == "selected" and d["workspace_id"] == "gateway"
    assert store.get_im_binding("wecom", u) == "gateway"

    print("\n== 6. 帮助/菜单显示当前库 ==")
    d = im.handle("wecom", u, "帮助")
    assert d["action"] == "menu" and "← 当前" in d["reply"]

    print("\n== 7. 单据库时自动选中（无需选择）==")
    single = AppConfig(data_dir=str(tmp / "data2"),
                       workspaces=[WorkspaceConfig(id="only", name="唯一库",
                                                   path=str(ROOT / "sample_workspace"),
                                                   index=IndexConfig())])
    st2 = Store(single.db_path)
    im2 = IMConversation(single, st2)
    d = im2.handle("feishu", "u2", "问题")
    print(d["action"], d.get("workspace_id"))
    assert d["action"] == "ask" and d["workspace_id"] == "only"

    print("\nIM 选库交互测试通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
