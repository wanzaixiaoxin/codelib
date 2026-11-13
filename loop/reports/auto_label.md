# 自动预标注报告

生成时间：2026-11-12 17:00:19

- 题目：24
- `expected_paths` 有 audit 点名证据：3 题
- 仅静态符号匹配候选：16 题（**需人工核对**）
- 无客观证据、留空待填：5 题

## 为什么两个字段不自动填

- `failure_layer`：拿系统自己的 `attributed_layer` 回填，再算「归因一致率」就是自评，必然 100%，毫无意义。这是 L0 验收的唯一主观输入。
- `expected_answer_points`：什么叫「满意答案」是产品判断，用模型生成或抄当前答案都会造成确认偏差。

## 数据边界

- 期望路径**从不**取自当前问答实际召回的 citations。
- audit 路径来自历史评审的 `harness_improvements`（评审当时点名「应补充召回 X」）。
- 静态匹配直接查 `knowledge.db!symbol`，不经过 BotService 检索链路。

## 下一步

1. 核对 `expected_paths`（尤其标注「静态符号匹配(候选)」的）。
2. 填 `expected_answer_points` 与 `failure_layer`。
3. `python -B loop/quality_runner.py --cases loop/frozen/failure_cases.yaml` 切到真判据。
4. 用 `failure_layer` vs `attributed_layer` 算归因一致率（≥70% 才算 L0 通过）。
