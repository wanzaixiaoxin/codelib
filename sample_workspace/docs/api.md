# 内部 API 约定

## MessageGateway.route

- 输入：`IncomingMessage(channel, user_id, text, chat_id)`
- 输出：`str` 回复文本
- 行为：以 `/` 开头走命令处理，否则走问答处理。

## MemoryStore

- `add_fact(user_id, text)`：写入事实。
- `search(query)`：按关键词返回命中的事实列表。
- `count()`：返回事实总数。

## 错误码

| 场景 | 返回 |
|------|------|
| 未知命令 | `未知命令：xxx` |
| 无命中 | `没有找到相关记忆...` |
