# 调用链展开原型验证

问题：分析当两个人的对话数据进入kafka后被我们消费，到最后写入kafka，对话的顺序能保证吗

- 关注点：['concurrency', 'writeback']
- 锚点 14 / 节点 34 / 文件 20 / 上下文 16037 字符
- 事实覆盖 **3/8**

| 事实 | 命中 | 位置 |
|---|---|---|
| 消费端起协程 | 否 | — |
| 位点提前提交 | 否 | — |
| 写回成功回调 | 是 | OnKafkaWriteOK d0 |
| 输出 key 构造 | 是 | MockNlpFromFile d0 |
| hash 分区器 | 否 | — |
| 无方向会话键 | 否 | — |
| round 上下文依赖 | 是 | ProcessDoneInquiryResultRoundID d0 |
| 回调为空实现 | 否 | OnKafkaWriteOK |

对照：上一轮同题 FTS 召回 10 条、0 条 .go 源码。
