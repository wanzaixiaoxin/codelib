# codelib 重构施工图（v1，2026-11-13）

> 依据：`docs/integration-plan.md` §15-17（README + 源码级调研）、§16 评测纪律、
> 三层归因实测（中文问法召回 0 条 .go、调用链 3265 条边闲置、`graph_hops:1`）。
> 本文是**代码级施工图**：改哪些文件、新增哪些文件、接口长什么样、每期怎么验收、怎么回滚。
> 原则：全部增量、主服务 `read_only` 不变、`/ask` 契约不变、默认行为不变、每期可独立回滚。

---

## 0. 重构范围一句话

**保留**数据层与安全层（store / indexer / knowledge.build / readonly_guard / channels），
**瘦身**编排层（bot.py 从 800 行降到 <300 行的薄路由），
**新增**研究层（`app/research/`）与上下文装配层（`app/context/`），
**不动**现有接口与默认行为（`research.enabled: false` 起步）。

## 1. 现状盘点与处置

| 现有文件 | 行数 | 处置 | 理由 |
|---|---|---|---|
| `app/main.py` | 726 | **小改** | 路由/鉴权/守卫保留；`readonly_guard` 白名单追加 `app/research` 工具路径；可选 `/api/research/status` 只读观测接口 |
| `app/bot.py` | 800 | **瘦身** | 拆出：命令处理留原地；检索规划+主答+修订迁入 research loop；presentation 保留 |
| `app/knowledge/query.py` | 250 | **保留** | FTS 通道继续作为 research 的一个工具与兜底通道 |
| `app/knowledge/schema.py` | 375 | **小改** | `card` 表加迁移（索引 + `created_at/updated_at`）；`add_card` / `search_cards`（fingerprint 增量） |
| `app/llm.py` | 142 | **小改** | 加 hooks（before/after）与流式超时、重试退避；装配指纹上报 |
| `app/evaluate.py` | 278 | **锁定** | 评分规则不改；新增外部脚本评分器（§16 纪律）放 `loop/` |
| `app/trace.py` | 99 | **小改** | 新 stage：`research_tool` / `research_step` / `evidence` |
| `app/lexicon.py` | 142 | **保留** | 已验证热读 + A/B 可回滚 |
| `app/chain.py` | 122 | **重构** | 并入 `app/research/tools.py::expand_chain`，修四点（双向边、目录优先、锚点收紧、关注点联动） |
| `app/retrieval.py` | ~200 | **保留** | 旧 BM25 兜底通道不动 |
| `loop/`（题集/归因/A-B） | — | **保留** | 继续作验收层，`research on/off` 作新自变量 |

## 2. 目标架构与数据流

```
                          ┌────────────────────────────────────────┐
 question ──► bot.handle ─┤ plan: 拆断言表 + 候选起点                │
                          │   ├─ FTS 通道(query.py, 兜底)           │
                          │   └─ Research Loop(reader profile)      │
                          │       loop: LLM ⇄ 只读工具 ⇄ 熔断       │
                          │       子任务(explore profile, 隔离scope) │
                          │   merge: 断言逐条对证据                  │
                          │   answer: 因果必须回指 read_body 结果    │
                          │   verify: verifier profile 复核          │
                          │   memory: 写 card(fingerprint 增量)      │
                          └────────────────────────────────────────┘
```

**重构前**：question → FTS top10 → 一次生成（中文问法 0 条 .go，`graph_hops:1` 走不到 `go` 那一行）。
**重构后**：断言表外置 → 模型拿只读工具沿 `calls` 边自导航（双向 4~5 跳）→ 每条结论回指
`symbol.body` → 缺证据显式声明，禁止「候选未覆盖」。

数据流里的三个不变量：
1. 知识库工程与 `knowledge.db` 只读（工具层白名单 + executor 二次校验）；
2. 每步写 `trace_event`（`research_tool` / `research_step` / `evidence`），全程可回放；
3. `loop/STOP` 文件与预算（steps / chars / wall / llm_calls）任一命中即停，partial 结果标 `isError`。

## 3. 关键设计一：分层上下文装配（`app/context/assembly.py`）

来源：Kimi `systemPromptVars()`（`readOnly` 整块清空 `coding_section`）+ MiniMax lightweight（明确列出被省略块）。

```python
BLOCKS = ("identity", "workspace", "skills", "memory", "evidence", "environment")
# identity:    固定 role + 只读声明（research 时替换成 reader role）
# workspace:   项目结构摘要 + AGENTS.md（带 `<!-- From: path -->` 来源标注 + 非特权声明）
# skills:      检索策略卡（从 loop/frozen/prompts/ 读）
# memory:      card 表命中项——只给 [card_id, title, 路径引用]，不给全文（MiMo 模式）
# evidence:    本轮已读证据清单（path:line + 一行结论），滚动更新
# environment: os / cwd / workspace 清单（Windows 补丁独立块）

def assemble(profile: str, blocks: dict[str, str], budget: dict[str, int]) -> Assembled:
    """按 profile 白名单装配，每块独立预算，返回 (text, fingerprint, omitted: list[str])"""
```

- `fingerprint = sha256(systemPrompt || sorted_tools)`，**排除消息历史**（MiniMax `assembly-fingerprint.ts` 语义），
  写进 `run_manifest.json`，用于区分「改了装配」和「对话变长」。
- 每块缺省置空而不是拼接空串；`omitted` 记录被预算挤掉的块，trace 可查。
- `AGENTS.md` 合并：用户级 → 工程根 → 子目录逐层，规范化路径去重，总量 >32KB 告警（Kimi `context.ts` 数值）。

## 4. 关键设计二：Research Loop（`app/research/`）

**单引擎 + 多 profile**（Kimi 形态）：

```python
PROFILES = {
  "reader":   Profile(tools=["search_symbols","read_body","callers","callees","grep",
                             "read_lines","related_sections","expand_chain","check_fact"],
                      role=READER_ROLE, read_only=True),      # 无 Bash / 无写
  "verifier": Profile(tools=[...同上 + "check_fact"], role=VERIFIER_ROLE, read_only=True),
}
```

状态机（断言表外置 state，每轮注入——Kimi goal 形态，不靠对话历史）：

```
PLAN   → 拆 N 条断言 + 候选锚点（名称锚点 + 结构锚点 + FTS citation.symbol + lexicon 扩展）
LOOP   → while True:
           reader 选工具 → 执行（executor 二次校验白名单）
           → 更新 evidence: proven / refuted / open
           → dedupe 检查（3 提醒 / 5 中等 / 8 强 / 12 强停，stopReason='repeat_breaker'）
           → 超预算上下文 → compact（逐字保问题 + 自足摘要 + 证据路径清单）
STOP   → 断言全有证据 | max_steps=12 | max_chars=24000 | max_llm_calls=8 | wall=120s | loop/STOP
ANSWER → 每条因果回指 read_body 结果；open 断言写「本次未读到 <符号/文件>」
VERIFY → verifier 复核跨文件因果；不过 → 回 LOOP（blocked 连续 ≥3 轮才判 blocked，Kimi 阈值）
MEMORY → 生成/更新 card（见 §5）
```

子任务契约（冷启动、禁 fork——Kimi 三例外中我们要关掉的两个）：

```python
spawn_reader(question, hints: list[str], budget) -> {
  "conclusions": [str],                     # 每条一句话
  "evidence":    [{"path": str, "lines": [int,int], "quote": str}],
  "open_assertions": [str],                 # 没查到的，显式列出
}
```

- 隔离：全新 scope、独立 history、**不继承主对话**；主上下文只收到上面这个 JSON（不塞 16000 字符函数体）。
- 返回值用 pydantic 强校验；校验失败视为子任务失败，不静默降级。

## 5. 关键设计三：项目记忆（`card` 表 + MiMo 三件套）

现有 `card(id, workspace, type, title, body, links, source_refs, hash)` 是预留空表，直接启用：

```sql
-- migration 0023-card-table-live（沿用 session-store 式编号迁移）
ALTER TABLE card ADD COLUMN created_at REAL;
ALTER TABLE card ADD COLUMN updated_at REAL;
ALTER TABLE card ADD COLUMN fingerprint TEXT;          -- sha256(body)，增量判定
CREATE INDEX IF NOT EXISTS card_ws_type_idx ON card(workspace, type);
```

```python
def upsert_card(workspace, type_, title, body, source_refs) -> "hit|updated":
    fp = sha256(body)                                    # MiMo fingerprint 增量
    if existing and existing.fingerprint == fp: return "hit"
    ...

def search_cards(workspace, query, limit=5) -> list[CardHit]:
    # chunk_fts MATCH + bm25 + 相对分数地板：top 必留，其余 >= top*0.15（MiMo floorRatio）
```

- 写入门控：`research.memory.disable_write` 单点开关（`!== true` 才禁，MiMo `write-gate.ts` 语义），读不受影响。
- `source_refs` 挂 `symbol.qualname` / `path`，可跳回 `read_body` 原文；**不给模型全文**，注入 `[card_id, title]` 由模型按需取。
- card 类型（P2 先做三类，够用）：`entry_chain`（入口调用链）/ `concurrency_model`（并发与位点）/ `state_ownership`（状态归属：Redis key / DB 表 / Kafka topic）。

## 6. 接口与兼容

- `POST /ask` 请求/响应字段**不变**；`debug=true` 的返回追加 `research` 节（steps / tool_events / evidence 表 / fingerprint），老客户端不受影响。
- 配置新增（`config/config.yaml`）：

```yaml
research:
  enabled: false          # 默认关，行为与现在完全一致
  profiles: {max_steps: 12, max_chars: 24000, max_llm_calls: 8, max_wall_ms: 120000}
  dedupe: {warn1: 3, warn2: 5, warn3: 8, force_stop: 12}
  compact: {trigger_ratio: 0.85, question_head: 2000, question_tail: 18000}
  memory: {disable_write: false, floor_ratio: 0.15, max_cards_per_run: 6}
  subtask: {allow_fork: false, partial_max_chars: 32768}
```

- 灰度：`enabled` 按用户/会话开关；A/B 用现成 `loop/` 框架，`research on/off` 作自变量。
- 回滚：任一期出问题 → 关开关即可，无需 git revert（R3 之前甚至没有行为变化）。

## 7. 文件级施工清单

**新增**（预估行数）：
```
app/context/assembly.py    # ~200  分层装配 + fingerprint + omitted 记录
app/context/compact.py     # ~150  逐字保问题 + 自足摘要 + 证据清单
app/research/__init__.py
app/research/loop.py       # ~350  状态机 + 预算 + STOP
app/research/profiles.py   # ~80   tool 白名单 + role + read_only
app/research/tools.py      # ~300  9 个只读工具（含修好的 expand_chain）
app/research/evidence.py   # ~120  断言表 + 注入渲染
app/research/dedupe.py     # ~90   四级熔断 + synthetic 复用
app/research/subtask.py    # ~140  冷启动 scope + JSON 契约
app/research/memory.py     # ~130  card upsert/search（MiMo 三件套）
app/research/manifest.py   # ~90   run_manifest + assembly_fingerprint
app/research/prompts/      # reader.md / verifier.md / planner.md（锁 loop/frozen 同款 hash 纪律）
```

**修改**：`bot.py`（-500 行编排迁走）、`main.py`（+守卫与观测接口）、`llm.py`（+hooks）、
`trace.py`（+3 stage）、`knowledge/schema.py`（+迁移 0023）、`app/chain.py`（并入 tools 后删除）。
**不动**：`store.py`、`indexer.py`、`knowledge/build.py`、`knowledge/query.py`（除接 hooks）、
`evaluate.py`、`evidence.py`、`channels/*`、`security.py`、`readonly_guard`。
**删除**：无（lexicon/chain 归位，不删文件）。

## 8. 分期（每期独立验收 + 独立回滚）

| 期 | 内容 | 验收（全部复用现成体系） | 回滚 | 估时 |
|---|---|---|---|---|
| **R0 地基** | `manifest.py` + `assembly.py` 骨架 + trace 新 stage + migration 0023 | `run_manifest.json` 字段齐（含 `agents_md_sha256`）；两跑 fingerprint 稳定；无行为变化 | 删文件即回滚 | 1 天 |
| **R1 工具层** | `tools.py`（chain 四点修复并入）；`probe_chain.py` 重跑 | 顺序题事实覆盖 **8/8**；覆盖文件集中于 `service/` `kafka_client/` `bond_chat/` | 纯新增 | 1~2 天 |
| **R2 循环层** | `loop.py` + `profiles.py` + `dedupe.py` + `evidence.py` + `compact.py` + `subtask.py` | 离线跑顺序题：正确结论 + 断言全有证据；一次人为制造的重复检索被熔断；partial 有 isError 标记 | 纯新增 | 2~3 天 |
| **R3 接入层** | `bot.py` 瘦身 + `research.enabled` 灰度 + `/ask` debug 节 | 24 题 `on/off` A/B：三层指标提升、`go_source_ratio` 从 0 → 显著、`disclaimer_rate`→0；两轮确定性 PASS；**先经历一次自动回滚再接受** | 关开关 | 2~3 天 |
| **R4 记忆层** | `memory.py` + card 三类 | 新验收：**同题二跑命中首跑沉淀的 card**；card 写入有 fingerprint 增量与门控记录 | 不写 card 即回滚 | 1~2 天 |
| **R5 评估口径** | 外部脚本评分器 + manifest 前置为硬性验收 | 「评分不依赖模型自述」抽查通过；`independent_scoring/sample_isolation/manifest_complete` 三指标齐 | — | 1 天 |

总计 **8~12 个工作日**。R3 是唯一动现有行为的节点，其余全部增量。

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| Loop 失控烧钱 | 预算四维 + `loop/STOP` + partial 标记；每步 trace 可回放；Webhook 告警（复用 WeComChannel） |
| 子任务返回垃圾 | pydantic 强校验 + 失败即弃 + 主 agent 重试一次后转 self-research |
| card 写入污染 | fingerprint 增量 + 写入门控 + `source_refs` 必须指向真实 symbol/path（否则拒写）+ 人工 `DELETE` 接口已有 |
| 模型不支持 tool-calling | `llm.py` 已支持 4 协议；本地网关 `glm-5.3` 原生支持 function calling（ZCode 即为其官方 harness，联调成熟度最高） |
| 灰度期两通道结果不一致 | merge 规则显式：research 证据优先级高于 FTS 片段，冲突时标注双来源 |
| 现有测试回归 | R3 前每期跑全量 `tests/*_test.py`（13 个文件）+ 我们的 24 题归因 |

## 10. 验收总表（最终形态）

1. 顺序题 8/8 代码事实 + 正确结论（`probe_chain.py`）。
2. 24 题三层指标：意图/检索/编排较 `baseline.json` 提升，`go_source_ratio` 显著非零。
3. 同题二跑命中首跑 card（跨会话记忆）。
4. 「候选未覆盖」在 research 开启的答案中出现次数 = 0；open 断言改为显式列「未读到 X」。
5. `run_manifest.json` 字段齐全且两次运行 fingerprint 仅在装配变化时变化。
6. 全量回归 13 个测试文件全绿；主库 `l0:` 会话数不变；知识库工程零写入。
