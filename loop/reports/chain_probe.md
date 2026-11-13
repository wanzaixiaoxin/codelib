# 调用链展开原型验证

问题：分析当两个人的对话数据进入kafka后被我们消费，到最后写入kafka，对话的顺序能保证吗

- 关注点：['concurrency', 'offset', 'writeback', 'entry', 'partition']
- 锚点 20 / 节点 48 / 文件 19 / 上下文 16028 字符
- 事实覆盖 **8/8**

| 事实 | 命中 | 位置 |
|---|---|---|
| 消费端起协程 | 是 | OnIDealGroupMsgNtf d0 |
| 位点提前提交 | 是 | ConsumeClaim d0 |
| 写回成功回调 | 是 | OnKafkaWriteOK d0 |
| 输出 key 构造 | 是 | sendGatwayNlpRawMsgNtfToKafka d0 |
| hash 分区器 | 是 | newKafkaProducerClient d0 |
| 无方向会话键 | 是 | asyncQDNlpMsgProcess d1 |
| round 上下文依赖 | 是 | AsyncIdealGroupMsgProcess d1 |
| 回调为空实现 | 是 | OnKafkaWriteOK |

对照：上一轮同题 FTS 召回 10 条、0 条 .go 源码。
