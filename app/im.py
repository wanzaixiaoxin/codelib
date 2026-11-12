"""IM（企业微信/飞书）会话交互：选库、切库、帮助、提问。

企业微信/飞书里没有按钮和下拉框，所以用文本协议：

  发 "帮助" / "menu" / "?"  -> 返回知识库列表与用法
  发数字 1/2/3 ...          -> 选中第 N 个知识库（对该用户持久化）
  发 "库" / "list"          -> 只看库列表
  其它文本                  -> 在当前库中提问

每个 IM 用户各自绑定一个当前库（im_binding 表），跨消息保持。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config import AppConfig, WorkspaceConfig
from .store import Store

HELP_WORDS = {"帮助", "help", "?", "？", "菜单", "menu"}
LIST_WORDS = {"库", "列表", "list", "知识库", "切换", "选库"}


class IMConversation:
    def __init__(self, cfg: AppConfig, store: Store):
        self.cfg = cfg
        self.store = store

    # ---------- 可用库 ----------
    def _libs(self) -> List[WorkspaceConfig]:
        return list(self.cfg.workspaces)

    def _lib_label(self, i: int, w: WorkspaceConfig) -> str:
        return f"{i}. {w.name or w.id}"

    def _menu_text(self, current: Optional[str] = None) -> str:
        libs = self._libs()
        if not libs:
            return "当前还没有配置知识库，请联系管理员。"
        lines = ["请选择要提问的知识库（回复序号）："]
        for i, w in enumerate(libs, 1):
            mark = " ← 当前" if current and w.id == current else ""
            lines.append(self._lib_label(i, w) + mark)
        lines.append("")
        lines.append("选好后直接提问即可；回复“帮助”随时查看菜单。")
        return "\n".join(lines)

    def _resolve_lib(self, token: str) -> Optional[WorkspaceConfig]:
        libs = self._libs()
        t = (token or "").strip()
        if t.isdigit():
            idx = int(t)
            if 1 <= idx <= len(libs):
                return libs[idx - 1]
            return None
        # 也支持直接发库名/库 id
        for w in libs:
            if t in (w.id, w.name):
                return w
        return None

    # ---------- 主入口 ----------
    def handle(self, channel: str, user_id: str, text: str) -> Dict[str, Any]:
        """返回 {"action": "menu"|"ask"|"need_select"|"selected"|"unknown",
                 "reply": 若 action 为 menu/need_select/selected 时的文本,
                 "workspace_id": 提问时使用的库}"""
        raw = (text or "").strip()
        low = raw.lower()
        current = self.store.get_im_binding(channel, user_id)

        # 帮助/菜单/列表
        if low in HELP_WORDS or low in LIST_WORDS:
            return {"action": "menu", "reply": self._menu_text(current)}

        # 纯数字或库名 -> 选库
        chosen = self._resolve_lib(raw)
        if chosen is not None and (raw.isdigit() or low in
                                   {w.id.lower() for w in self._libs()} |
                                   {(w.name or "").lower() for w in self._libs()}):
            self.store.set_im_binding(channel, user_id, chosen.id)
            return {"action": "selected", "workspace_id": chosen.id,
                    "reply": f"已切换到知识库：{chosen.name or chosen.id}。\n现在可以直接提问。"}

        # 还没选库 -> 先让选
        if not current:
            if len(self._libs()) == 1:
                # 只有一个库，自动选中
                w = self._libs()[0]
                self.store.set_im_binding(channel, user_id, w.id)
                current = w.id
            else:
                return {"action": "need_select", "reply": self._menu_text(None)}

        return {"action": "ask", "workspace_id": current or ""}
