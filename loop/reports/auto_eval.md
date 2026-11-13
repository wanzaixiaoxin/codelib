# 自动评估标注报告

生成时间：2026-11-12 17:05:23

- 题目 24｜`expected_paths` 非空 18｜`expected_answer_points` 非空 12
- `failure_layer` 分布：{'mixed': 8, 'retrieval': 5, 'ok': 7, 'presentation': 4}
- 置信度：{'medium': 9, 'low': 12, 'high': 3}

## 与系统代理归因的分歧

两套判定不一致 13/24 题。这不是「谁对谁错」：系统归因看的是 coverage/missing 等代理信号，本评估看的是期望文件与要点是否真被召回/讲清。分歧题就是最该优先看的地方。

| task | 问题 | auto_eval | 系统代理 |
|---|---|---|---|
| fc_001 | 券补全机制 | `mixed` | `retrieval` |
| fc_002 | 资金和现券上下文管理有什么主要的区别 | `mixed` | `retrieval` |
| fc_005 | 消息服务是如何启动的？入口在哪里？ | `retrieval` | `ok` |
| fc_007 | MemoryStore 怎么检索？ | `presentation` | `ok` |
| fc_009 | 总一下当前资金上下文 | `mixed` | `retrieval` |
| fc_012 | 我希望再回答结果中不要包含太多的代码，我不关心代码 | `mixed` | `retrieval` |
| fc_018 | 目前资金和现券业务有哪些公用的部分 | `mixed` | `retrieval` |
| fc_019 | 当前的单聊的聊天顺序是否会顺序到达下游kafka | `mixed` | `retrieval` |
| fc_020 | 当前系统和外部有那些交互 | `mixed` | `retrieval` |
| fc_021 | 详细介绍一下现券的上下文管理 | `presentation` | `ok` |
| fc_022 | 价格补全机制 | `mixed` | `ok` |
| fc_023 | 万得简称如何补全 | `presentation` | `ok` |
| fc_024 | 介绍一下本工程 | `presentation` | `retrieval` |

## 自评风险声明

failure_layer 由独立证据判定，未抄 attributed_layer；属自动评估，非人工 ground truth。
期望路径与要点来自 knowledge.db 事实，从不取自当前召回或答案文本。
用途：L1 改动的相对比较；不可拿本层算归因一致率当验收。
