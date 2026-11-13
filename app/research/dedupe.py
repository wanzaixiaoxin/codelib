"""四级重复熔断（Kimi toolDedupeService 数值：3 提醒 / 5 中等 / 8 强 / 12 强停）。"""
from __future__ import annotations
import json


class RepeatBreaker:
    def __init__(self, warn1: int = 3, warn2: int = 5, warn3: int = 8,
                 force_stop: int = 12):
        self.levels = (warn1, warn2, warn3, force_stop)
        self._last_key: str | None = None
        self._streak = 0
        self.stop_requested = False

    def check(self, tool_name: str, args_json: str) -> tuple[int, str]:
        """返回 (连续重复次数, 提示或 None)。相同 (工具+归一化参数) 连续执行才累计。"""
        key = tool_name + "::" + json.dumps(_canon(args_json), sort_keys=True,
                                            ensure_ascii=False)
        if key == self._last_key:
            self._streak += 1
        else:
            self._last_key = key
            self._streak = 1
        n, (w1, w2, w3, fs) = self._streak, self.levels
        if n >= fs:
            self.stop_requested = True
            return n, (f"同一调用已连续重复 {n} 次，强制停止本轮。"
                       "请改用其它证据来源，或基于已收集证据直接作答。")
        if n >= w3:
            return n, f"警告（强）：同一调用连续 {n} 次。必须立即换检索方式，否则本轮将被强制停止。"
        if n >= w2:
            return n, f"警告（中）：同一调用连续 {n} 次。考虑换关键词/换工具。"
        if n >= w1:
            return n, f"提示：该调用已连续 {n} 次，重复结果不会带来新信息。"
        return n, None


def _canon(args_json: str):
    try:
        v = json.loads(args_json or "{}")
    except Exception:
        return {"raw": str(args_json)[:200]}
    if isinstance(v, dict):
        return {k: str(v[k])[:200] for k in sorted(v)}
    return {"raw": str(v)[:200]}
