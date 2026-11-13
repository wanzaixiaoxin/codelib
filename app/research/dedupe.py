"""四级重复熔断（Kimi toolDedupeService 数值：3 提醒 / 5 中等 / 8 强 / 12 强停）。"""
from __future__ import annotations
import json


class RepeatBreaker:
    """按 (工具+归一化参数) 的**历史累计**次数分级——模型穿插一次其它调用
    不应重置计数（首轮实测：read_body 与 grep_code 交替，streak 永远到不了阈值）。
    归一化剥离 max_chars/limit 等纯截断参数（改动它们不改变调用语义）。"""

    def __init__(self, warn1: int = 3, warn2: int = 5, warn3: int = 8,
                 force_stop: int = 12):
        self.levels = (warn1, warn2, warn3, force_stop)
        self.counts: dict[str, int] = {}
        self.stop_requested = False

    def check(self, tool_name: str, args_json: str) -> tuple[int, str]:
        """返回 (该调用在历史中的累计次数, 提示或 None)。"""
        key = tool_name + "::" + json.dumps(_canon(args_json), sort_keys=True,
                                            ensure_ascii=False)
        self.counts[key] = self.counts.get(key, 0) + 1
        n, (w1, w2, w3, fs) = self.counts[key], self.levels
        if n >= fs:
            self.stop_requested = True
            return n, (f"同一调用已重复 {n} 次，强制停止本轮。"
                       "请基于已收集的证据直接作答。")
        if n >= w3:
            return n, f"警告（强）：同一调用已重复 {n} 次。必须立即换检索方式，否则本轮将被强制停止。"
        if n >= w2:
            return n, f"警告（中）：同一调用已重复 {n} 次。考虑换关键词/换工具。"
        if n >= w1:
            return n, f"提示：该调用已重复 {n} 次，重复结果不会带来新信息。"
        return n, None


_TRIM_ARGS = ("max_chars", "limit")


def _canon(args_json: str):
    try:
        v = json.loads(args_json or "{}")
    except Exception:
        return {"raw": str(args_json)[:200]}
    if isinstance(v, dict):
        return {k: str(v[k])[:200] for k in sorted(v) if k not in _TRIM_ARGS}
    return {"raw": str(v)[:200]}
