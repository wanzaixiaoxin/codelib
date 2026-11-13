# 单目标自循环：把「听懂 → 找对 → 讲清」跑成闭环

> 唯一目标：准确理解提问意图，依据代码库（代码 + 文档）做准确分析，给出满意且易懂的答案。
> 本文是 `docs/self-loop-plan.md` 的**收窄落地版**，替代那份的 P0~P2 排期；通用架构留作参考。

---

## 0. 目标收窄后，方案小了一大圈

这不是"让产品自演进"，是**问答质量调参闭环**。难度从通用版的 7/10 掉到 **4~5/10**，工期从 3~4 周缩到 **3~7 天能出第一版曲线**。

三个大件可以直接砍掉：

- **不需要开发角色写代码**：可改对象全是数据/配置（boost、术语映射、检索参数、答案模板），没有 patch、没有 git commit/revert、没有代码生成。
- **不需要"司机在车外"的重型编排**：只要旋钮是运行时可读的，改完**不用重启服务**，循环就是个外部批处理脚本顺序调 `/ask` → 归因 → 改配置 → 复测。（通用方案里那套 subprocess 起停是为"改自己的 py 文件"准备的，这里用不上。）
- **不需要模拟使用者人格**：固定失败题集的批量 runner 就够了。前两轮一直担心的"反馈贫瘠"在这里自动消失——判据是客观的任务成败，不是模型编的感想。

主服务保持 `read_only: true`，知识库工程一个字节都不碰。

---

## 1. 最重要的决策：三类失败分开归因，别揉成一个总分

| 你说的失败 | 归因层 | 客观判据 | 该调的旋钮 |
|---|---|---|---|
| 无法理解问题 | **意图层** | 改写后的 `retrieval_query` 是否保留/补全主题词；子问题是否覆盖 `expected_intent`；业务词是否映射成正确代码标识符 | 改写 prompt、`_plan_retrieval` 拆题、同义词/术语映射表 |
| 没有真正找到答案 | **检索层** | citation 是否命中 `expected_paths`；`evidence.coverage` / `missing`；`gate` 是否误拦；`retrieved_paths` 与期望重合度 | `boost` 规则、`top_k / budget_tokens / graph_hops / top_n / rerank`、`min_coverage`、chunking 策略 |
| 答案没编排整理好 | **编排层** | 固定 rubric：结论先行、依据引用可追溯、术语有解释、无答非所问、结构清晰；+ 确定性结构检查 | `SYSTEM_PROMPT`、`presentation` 规则、答案模板 |

一个 0-100 的总分没法告诉产品角色"该调意图、调检索、还是调编排"。`app/evaluate.py:_check_rules` 已经有 11 项 checks 带 `hint`，第 256/263 行还专门对"证据覆盖问题 / 回答有据可依 / 证据门槛"做了归因处理——**骨架现成，把它从三分类（boost/ingest/config）改成按这三层打标签就行**。

---

## 2. 最高杠杆：术语鸿沟，不是 top_k

真实问题用业务词（"券补全机制"、"万得简称如何补全"），代码里是英文标识符。用户说"没找到答案"，很多时候其实是"没听懂"——检索压根没召回对的文件，调 `top_k` 只会多召一堆无关内容。

证据就在 `app/retrieval.py:20-31`：作者已经手写了 30 多条「中文概念 → 英文标识符」映射（`_SYNONYMS`，注释写明"缓解中文提问命中英文代码的问题（零额外 token）"），`app/knowledge/query.py:81` 的 `query_expand: synonym` 调它。这张表**现在靠人肉维护**，正是最该交给循环自动学的东西。

从失败案例里自动补「业务术语 → 代码符号」映射，落进可运行时读取的词表 + `boost`，闭环最短、最可验证、收益最直接。这是 L1 的第一优先级，别一上来调检索参数。

**唯一必要的小开发**：把 `_SYNONYMS` 从 module 级常量升级成 DB 表（或服务启动时读的 `loop/lexicon.yaml` + 热重载），否则改词表要重启进程。这是整个方案里唯一需要动 codelib 代码的地方，量很小。

---

## 3. 三层归因的数据全在 `trace_event` 里，不用新增埋点

`app/trace.py` 的 `Tracer` 已经把每一次问答拆成结构化 stage，逐层对应关系：

**意图层**
- `resolved(history_turns, retrieval_query, ...)` → 改写后的检索词，直接和标注的 `expected_intent_terms` 比对
- `retrieval_info(source, question_used, context, ...)` → 实际拿去检索的问题
- `_plan_retrieval`（`app/bot.py:392`）拆出的 ≤3 个子问题
- `evidence.missing` / `required_identifiers` / `ignored_scope`（`app/evidence.py:evidence_coverage` 已把"哪些词没命中"结构化输出）

**检索层**
- `retrieve` 阶段的 `citations` 与 `context`
- `evidence` 阶段：`coverage` / `terms` / `covered` / `missing`（`app/bot.py:189`）
- `gate` 阶段：`{coverage, min_coverage}`（`app/bot.py:200`）→ 抓误拦
- `quality_review` / `revised` → `_review_and_revise` 补检索的记录
- `debug=true` 返回的 `citations` / `usage`（`POST /ask`，`app/main.py:641`）

**编排层**
- `prompt`（system + user 全文）与 `answer`（text + citations）
- `presentation.response_preferences` 的三个开关（`no_code` / `inline_citations` / `detailed`）
- 独立裁判按固定 rubric 打分，只评答案，不碰生成链路

**一个必须记住的陷阱**：`evidence_coverage` 的 `hit()` 对中文词做了 `size in (3,2)` 的子串匹配，代码注释自己都写着"词面命中不代表证据完整"。术语映射加错、召回一堆无关文件时，`coverage` 反而可能上升。所以**主判据必须是 `expected_paths` 命中率**，coverage 只当辅助信号，别拿它当验收。

---

## 4. 可改旋钮清单（标注是否热生效）

| 层 | 旋钮 | 现状 | 热生效 |
|---|---|---|---|
| 意图 | 术语→符号映射 | `_SYNONYMS` 是 module 常量（`app/retrieval.py`） | ❌ 需改成 DB 表/热重载配置 |
| 意图 | 改写 prompt / 拆题 prompt | 硬编码在 `app/bot.py` | ❌ 需抽成 `loop/tunables.yaml`（L2） |
| 意图 | `query_expand` 词表扩充 | `query.py:_expand_query` | ❌ 同上 |
| 检索 | `boost` 规则（path/dir/symbol/token） | `knowledge.db!boost` + `add_boost` / `_apply_boosts` | ✅ 每次查询读库 |
| 检索 | `top_k / budget_tokens / graph_hops / top_n / rerank.mode` | `config/config.yaml` | ❌ 启动时读 |
| 检索 | `min_coverage`（证据门槛） | `app/bot.py:186` 读配置，默认 0.2 | ❌ 启动时读 |
| 检索 | chunking / `file_summary` / `doc_chunk` | `app/knowledge/chunking.py` | ❌ 需重建索引 |
| 编排 | `SYSTEM_PROMPT` | 硬编码 `app/bot.py` | ❌ 需抽成配置 |
| 编排 | `presentation` 三开关与 strip 规则 | `app/presentation.py` 关键词匹配 | ❌ 需抽成配置 |

**L0/L1 只碰热生效的**：`boost` 表 + 新做的 `lexicon` 表。这样零重启、零 patch、零 git 操作，配置快照存 `loop.db` 就能回滚。想调 prompt 和检索参数，等 L2 把它们抽成 `loop/tunables.yaml` 再说。

---

## 5. 闭环流程（轻版）

```
固定失败题集(train) ──批量 POST /ask(debug=true)──> trace_event
        │
        ├─ 三层归因：intent / retrieval / presentation，各自出指标 + 失败题 id
        ├─ 产品角色：输出「旋钮变更提案」，不是需求文档
        │     {layer, target, change, expected_metric, linked_failure_ids}
        │     校验：绑定 ≥1 个失败题 id、指向白名单旋钮、写清预期涨哪个指标
        ├─ 应用变更：写 boost / lexicon（服务下次查询自动生效）
        ├─ 复测：同批 train + holdout 盲测
        └─ 判定：三层指标均不降 且 holdout 不降 → 接受（存配置快照）
                 否则 → 回滚到上轮快照，记原因
```

**独立裁判**：仍是不同 endpoint/model + 固定 rubric prompt，锁在 `loop/frozen/prompts/`。`judge_trace(trace, llm)` 的 `llm` 是鸭子类型，注入独立客户端零改动；`api_evaluate` 里 `judge_trace(t, bot.llm)` 的自评只作参考，不作验收。

**循环状态**：`data/loop.db`，表砍到 4 张即可 —— `loop_run` / `loop_round` / `failure_case` / `round_metric`（外加 `tunable_snapshot` 存每轮配置）。比通用版的 9 张轻一半。

---

## 6. 燃料：失败集怎么来

循环的验收标准就是这批题，不用凭空编 persona。

从现有库里挖：`conversations`（126 行）、`trace`（15 条）、`audit`（5 条，含 verdict/score/report）、`qa_memory`（10 条）。真人问过的"券补全机制""万得简称如何补全"这类都在里头。每条标注四件事：

```yaml
- task_id: fc_001
  question: "券补全机制是怎么走的？"
  expected_intent_terms: ["券", "补全"]          # 意图层判据
  expected_paths: ["src/handlers.py", "..."]    # 检索层主判据
  expected_symbols: ["fill_coupon", "..."]
  expected_answer_points: ["触发时机", "数据来源", "兜底逻辑"]  # 编排层 rubric 锚点
  failure_layer: retrieval                      # 人工初判，供归因结果对照
  split: train                                  # train | holdout
```

- 先攒 **20~30 条**，其中 1/4 划 `holdout`，**调参过程对 holdout 不可见**。
- 三条失败类型都要覆盖，别只挑检索错的。
- 这份 `failure_cases.yaml` 放 `loop/frozen/`，hash 锁死，开发角色/循环不可改。

---

## 7. 验收、防漂移、终止

**四门禁**（沿用通用版，指标按三层分开看）
1. 全量 `python tests/*_test.py`（13 个）绿
2. 三层指标分别不降：意图命中率、`expected_paths` 命中率、rubric 分；**主判据是路径命中，不是 coverage**
3. `holdout` 不降（防过拟合题集）
4. 知识库 manifest hash 未变 + `loop/frozen/` hash 未变

**头号风险仍是 boost 漂移**：`add_boost`（`app/knowledge/schema.py:185`）是 `weight=min(weight+0.5, 5.0)` 只增不减，`hits` 12 行全 0（只有 SELECT 没有 UPDATE），加权文件更容易进上下文 → coverage 词面上升 → 下轮更不容易报缺失 → 权重继续涨。在这个目标下尤其致命：检索会被慢慢带偏，看起来"证据覆盖变好"，实际答案越来越不对。L1 必补 `hits` 回写、时间衰减、上限、A/B 对照、漂移报警。

**实测补充（2026-11-12，L0 在线补跑 8 题）**：在线复测本身有噪声。同一题连跑两次，答案长度 995→763、引用文件变化、格式正则 `has_structure` 从 True→False，导致编排层代理分 1.0→0.0；而 `coverage`、`gate`、`llm_ok`、`unsupported` 完全稳定。据此收紧验收：

- **不能用单次跑的三层分数做接受/回滚**，否则 LLM 措辞抖动会被当成"变好了/变差了"。
- L1 判定必须多次采样（建议每轮每题 ≥3 次取中位数），并设最小提升阈值（judge_score ≥2 分或任务成功率确有提升）。
- 编排层代理只判"明显坏掉"（空/截断/无引用/违反偏好/无证据支撑）；结构与易懂度交给标注 `expected_answer_points` + 独立裁判 rubric，**不靠格式正则**。
- 优先用检索层的确定性信号（期望路径命中率、coverage、gate）当主判据；格式类信号一律降级为线索。

**终止条件**：`max_rounds: 10`、连续 3 轮 holdout 无显著提升 → 停；**train 涨但 holdout 跌 = 过拟合，立刻回滚**；门禁连续失败 2 轮 → 停；`loop/STOP` 文件 → 立即停；单轮 LLM 调用/耗时超预算 → 停（一次 `!deep` 可能 8~11 次串行调用，批量题集要算总账）。

**接受/回滚**：只存配置快照（`boost` 行 + `lexicon` 行 + tunables），不动代码、不动 git。

---

## 8. 分期与工期

| 期 | 干什么 | 验收 | 估计 |
|---|---|---|---|
| **L0 只观察** | 攒失败集、写批量 runner、读 `trace_event` 出三层归因报告；**不改动任何旋钮** | 归因结果和人工初判一致率 ≥70%；同一批题跑两遍指标可复现 | 1~3 天 |
| **L1 策略闭环** | 把 `_SYNONYMS` 升级成热读词表；循环只调 `boost` + `lexicon`，自动接受/回滚 | 至少一轮真实提升（路径命中率涨且 holdout 不降）+ 一轮成功回滚 | ~3 天 |
| **L2 参数闭环（可选）** | 把 prompt / 检索参数 / 编排规则抽成 `loop/tunables.yaml`，加 A/B | 三层指标都有可调空间，且 holdout 稳定 | ~1 周 |
| **L3 先不做** | 允许改 `app/bot.py` 逻辑 → 才回到通用方案的 P2（patch + 重启） | — | — |

**L0 不做完不要放行 L1**。归因不准的话，后面调参就是在瞎撞。

---

## 9. 明确不做

- 不改知识库工程代码，不往 `workspaces[].path` 写任何东西。
- 不放开主服务 `read_only`。
- 不引入代码生成、不重启服务、不模拟使用者人格、不搞需求文档/角色编排。
- 不用同一个模型既生成又裁判；自评结果不作验收。
- 不拿 `evidence_coverage` 当主判据（中文子串匹配会骗人）。
- 不追求无限跑，默认硬上限，随时能停能回滚。

---

## 10. 第一步具体动作

1. 从 `conversations` / `trace` / `audit` 导出答得不好的问题，人工标 20~30 条 `failure_cases.yaml`（四字段齐全，分 train/holdout）。
2. 写 `loop/quality_runner.py`：批量 `POST /ask(debug=true)` → 读 `trace_event` → 输出三层归因表。**不写任何改动逻辑**。
3. 空跑两遍，确认指标可复现、归因和人工初判对得上。
4. 再把 `_SYNONYMS` 挪进 DB 表，接上"提案 → 改 boost/lexicon → 复测 → 接受/回滚"。

先看清是哪一层在坏，再动手调。这三层的修法完全不一样，混着调只会互相掩盖。

---

## 11. L1 实测（2026-11-12，已跑通一轮完整闭环）

方向自行选定：**术语映射**（中文业务词 → 检索词）。理由是 13 题归因分歧里大半落在这里，且 `_SYNONYMS` 原本靠人肉维护 30 条。

**改造**（只动 codelib 自身；不碰知识库工程，不改 `codelib.db` / `knowledge.db`）：
- 新增 `app/lexicon.py`：独立 `data/lexicon.db`，按 mtime 热读；空表或文件不存在时 `expand()` 返回 `[]`，**线上行为与改造前完全一致**（已验证）。
- `app/retrieval.py:_expand_query` 合并静态 `_SYNONYMS` 与动态词表，词表异常静默降级。
- `loop/build_lexicon.py` 从自动评估标注学映射：只学 `retrieval/mixed` 且期望路径非空的题；扩展词来自期望文档名与符号名，**不取自当前答案或实际召回结果**。

**闭环实测**（只测检索召回：`rerank.mode=none`、不调 LLM、两轮重复确定性 PASS、18 道有期望路径的题）：

| 轮 | 词表 | 平均期望路径命中率 | 判定 |
|---|---|---|---|
| 基线 | 空 | 0.4491 | — |
| 第一轮 | 6 条 | 0.4259（Δ −0.0232） | 不接受，**自动回滚** |
| 第二轮 | 剪枝后 5 条 | **0.4815（Δ +0.0324）** | **接受，已落地** |

第二轮用 leave-one-out 逐条测边际贡献，剔除有害的「现券上下文」（Δ −0.0417），保留 5 条后复测通过。回归测试：`smoke / feedback / core_quality / gate / presentation` 全过。

**边界（必须记住）**：
- 只测召回，不等于答案质量；主判据 `expected_paths` 来自自动评估标注，不是人工 ground truth。
- 已运行的服务进程加载的是旧 `retrieval.py`，**需重启才会用到词表**；mtime 热读只对重启后的进程或新进程生效。未擅自重启。
- 正式 L1 的接受/回滚仍须在线复测并多次采样取中位数（见 §7）。
