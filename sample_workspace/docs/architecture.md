# 消息网关架构

## 1. 总览

系统由三层组成：

1. **接入层**：企业微信、飞书、本地通道，把各平台消息转换为统一的 `IncomingMessage`。
2. **网关层**：`MessageGateway.route` 按命令前缀路由到 `CommandHandler` 或 `QueryHandler`。
3. **存储层**：`MemoryStore` 保存用户事实，供后续问答召回。

## 2. 消息流

```
渠道回调 -> IncomingMessage -> MessageGateway.route
        -> CommandHandler / QueryHandler
        -> MemoryStore
        -> 回复渠道
```

## 3. 扩展点

- 新增渠道：实现 responder 并 `register_responder`。
- 新增命令：在 `CommandHandler.handle` 中增加分支。
- 更换存储：替换 `MemoryStore` 为数据库实现。
