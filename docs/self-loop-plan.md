# codelib 自循环迭代方案（v1）

> 目标：让「模拟使用者 → 反馈 → 产品评估 → 规划 → 开发 → 测试 → 再交给使用者」无人值守跑起来。
> 迭代对象：**codelib 自身源码与配置**。
> 绝不触碰：`workspaces[].path` 指向的知识库工程（只读被测数据）与评测基准区。

---

## 0. 先把边界钉死

| 区 | 具体路径 | 权限 | 保护手段 |
|---|---|---|---|
| A 数据只读区 | `workspaces[].path`（默认 `./sample_workspace`，可指向真实工程） | 永不写 | `readonly_guard`（`app/main.py:234`）拦 HTTP 写路径；`indexer.py` / `knowledge/build.py` / `watcher.py` 实测只读 mtime+sha256；每轮 manifest hash 校验 |
| B 基准只读区 | `loop/frozen/` | driver 锁 hash，开发角色禁写 | 金标题集、角色 prompt、评分脚本、测试基线、baseline.json |
| C 可写产品区 | `app/` 白名单 + 可改的检索/prompt 参数 | 开发角色**只产出 diff**，由 driver 应用 | 路径白名单 + 每轮 `git commit` |
| D 编排区 | `loop/`（driver 自身） | 只有人能改 | 独立进程，不在 uvicorn 内 |

**关键纠正**：`app.read_only: true` 保护的是知识库，不是禁止我们改 codelib 源码。它是现成的隔离边界，不需要另造沙箱去保护第三方仓库。反过来要防的是：开发角色通过文件系统误改 `config/config.yaml` 里的 `workspaces[].path`，或往知识库目录落文件——HTTP 守卫看不见这个。

---

## 1. 架构：司机必须坐在车外面

改的是**正在跑的那个进程自己的代码**，uvicorn 不热加载，每轮必须重启。而 `watch_loop` 挂在 `main.py:lifespan`（`app/main.py:161`）里，跟服务同生共死，服务一重启循环就断。所以：

- **`loop/driver.py` 是独立 OS 进程**，不 import `app.main`，只用 HTTP 跟服务说话。
- driver 用 `subprocess` 起 `python -m app --config config/config.yaml serve`（可指定端口），改完代码 → `terminate()` → 重启 → 轮询 `/healthz` 直到就绪 → 继续下一轮。
- 循环状态放**独立库 `data/loop.db`**，不写 `codelib.db` / `knowledge.db`。理由：driver 是外部进程；回滚/清库不污染主库；现有两个库都是 SQLite + 每次新建连接（`Store._connect` / `KnowledgeStore._connect`），不适合多角色长期并发写。
- 主服务**保持 `read_only: true` 不变**。

```
loop/driver.py (独立进程, 不被重启)
   │
   ├─ PRECHECK   frozen hash / workspace hash / STOP 文件 / 预算
   ├─ USE        admin 会话调 POST /ask (debug=true) 跑固定题集
   ├─ JUDGE      独立裁判客户端打分 + 客观成败判定
   ├─ FEEDBACK   结构化反馈入库
   ├─ PLAN       产品角色产出需求 JSON（校验绑定 feedback_id + 可测指标）
   ├─ PATCH      开发角色产出 unified diff → 白名单校验 → driver 应用
   ├─ RESTART    重启服务 → /healthz 就绪
   ├─ VERIFY     四门禁
   ├─ DECIDE     ACCEPT(git commit) / REVERT(git revert + 记原因)
   └─ STOP?      终止条件 → 下一轮 or 收尾报告
```

**接口契约（已核实）**
- `POST /ask`：`AskRequest{question, workspace, session_id, deep, debug}`，需 `current_user` 鉴权（cookie 或 `Authorization: Bearer <session-token>`）；`debug=true` 才返回 `citations/memory/usage`。driver 用 admin 账号登录一次拿会话。
- `BotService.handle(workspace_id, text, session_id, deep) -> Answer`（`app/bot.py:86`），每次调用自动生成 `trace` + `trace_event`（`app/trace.py` / `Store.create_trace:469`）。
- `judge_trace(trace, llm, provider_note="")`（`app/evaluate.py:227`）：`llm` 是鸭子类型，只要 `.chat()` + `.available`。**换独立裁判非常容易**——传一个不同 `base_url`/`model` 的 `LLMClient` 即可，不用改评估代码。
- `Store.list_traces / get_trace / add_audit` 可直接复用取每轮留痕。
- `files.sha256`（`codelib.db`，653 行）与 `kb_file.hash`（`knowledge.db`，646 行）已有现成基线，用作知识库污染检测。
- `tests/*.py` 13 个文件自包含、`python tests/xxx_test.py` 直跑、无需 pytest、LLM 关闭时降级到检索摘要 → 直接当 QA 回归基线。
- git 有完整提交历史（`31eaf22` 等），`git commit` / `git revert` / `git worktree` 现成。

---

## 2. 五个角色（prompt 全部锁在 `loop/frozen/prompts/`）

| 角色 | 输入 | 输出 | 硬约束 |
|---|---|---|---|
| **UserSimulator** | persona + `golden_task` + 当前版本服务 | 客观成败（任务完成/答案正确/citation 指向期望文件/是否被 gate 拦）+ 主观 complaint | 主观反馈必须绑定 `trace_id` + `task_id`；不生成空泛"不好用"当主信号 |
| **ProductManager** | 反馈 + 本轮指标 + 上轮 diff | 需求 JSON：`{goal, target_files, acceptance_criteria, priority, linked_feedback_ids}` | 校验：至少引用 1 条真实 feedback、至少 1 个可测指标；无直接改码权 |
| **Developer** | 需求 + 白名单内文件当前内容 | unified diff 文本 | 只产出 diff，**不执行写、不跑 git、不碰 A/B/D 区** |
| **QA** | 当前版本 | 四门禁结果 + 跨轮指标 | 跑的是锁定版测试与题集 |
| **Judge** | trace + 答案 + 检索证据 | `verdict/score/improvements` | **独立 endpoint + 独立 model + 固定 prompt**；不参与任何生成 |

**使用者语料从哪来**：`conversations`（126 行）、`trace`（15 条）、`qa_memory`（10 条）里是真人问过的真实问题（券补全机制、万得简称补全等）。把它们清洗成 `golden_tasks.yaml` 的种子，比凭空编 persona 靠谱。题集拆 `train` / `holdout` 两组，**开发角色只能看到 train**。

**persona 保持轻量**：`{name, 目标, 术语习惯, 耐心, 任务脚本}`，别搞复杂人格系统。产品形态是一问一答，没有流程/状态变更，模拟使用者的动作天然有限，主信号必须是客观成败而不是模型编的感想。

---

## 3. 数据模型（`data/loop.db`，SQLite）

```sql
CREATE TABLE loop_run (
  run_id TEXT PRIMARY KEY, started_at REAL, finished_at REAL,
  status TEXT,                       -- running|done|aborted
  baseline_hash TEXT,                -- frozen/ 全量 hash
  workspace_manifest_hash TEXT,      -- 知识库目录基线
  config_json TEXT, stop_reason TEXT,
  total_llm_calls INTEGER DEFAULT 0, total_wall_ms INTEGER DEFAULT 0
);

CREATE TABLE loop_round (
  round_id TEXT PRIMARY KEY, run_id TEXT, idx INTEGER,
  phase TEXT, status TEXT, parent_round_id TEXT,
  commit_hash TEXT, artifact_hash TEXT,
  metrics_json TEXT, patch_id TEXT,
  started_at REAL, finished_at REAL, note TEXT
);

CREATE TABLE persona (
  persona_id TEXT PRIMARY KEY, name TEXT, profile_json TEXT
);

CREATE TABLE golden_task (
  task_id TEXT PRIMARY KEY, question TEXT, workspace TEXT,
  split TEXT,                        -- train|holdout
  expected_paths TEXT,               -- JSON array
  expected_symbols TEXT,             -- JSON array
  success_criteria_json TEXT, enabled INTEGER DEFAULT 1
);

CREATE TABLE feedback (
  feedback_id TEXT PRIMARY KEY, round_id TEXT, trace_id TEXT,
  persona_id TEXT, task_id TEXT,
  success INTEGER, rating INTEGER, complaint TEXT,
  expected_source TEXT, raw_json TEXT, created_at REAL
);

CREATE TABLE requirement (
  req_id TEXT PRIMARY KEY, round_id TEXT, goal TEXT,
  target_files_json TEXT, acceptance_json TEXT, priority INTEGER,
  linked_feedback_ids_json TEXT, status TEXT
);

CREATE TABLE patch_record (
  patch_id TEXT PRIMARY KEY, round_id TEXT, diff_text TEXT,
  files_json TEXT, applied INTEGER, rejected_reason TEXT, created_at REAL
);

CREATE TABLE round_metric (
  round_id TEXT, task_id TEXT, metric TEXT, value REAL,
  PRIMARY KEY (round_id, task_id, metric)
);

CREATE TABLE gate_result (
  round_id TEXT, gate TEXT, passed INTEGER, detail_json TEXT,
  PRIMARY KEY (round_id, gate)
);
```

`loop_round` 补上了现有库最缺的东西：**轮次、父子轮、产物 hash、指标、可回滚锚点**。现在 `trace` = 一次问答、`audit` = 一条评审、`boost` = 全局累积量，根本表达不了"第 3 轮比第 2 轮好在哪"。

---

## 4. 每轮状态机细则

**PRECHECK**（任一失败即 abort）
1. `loop/frozen/` 全文件 sha256 == `loop_run.baseline_hash`
2. 知识库目录 manifest hash == `loop_run.workspace_manifest_hash`（或逐文件比对 `files.sha256`）
3. `loop/STOP` 不存在
4. 累计 `total_llm_calls` / wall time 未超预算

**USE**：driver 以 admin 会话批量调 `/ask?debug=true`，每个 task 独立 `session_id`，落 `trace_id` 与 `debug` 里的 `citations/usage`。

**JUDGE**：复用 `judge_trace(trace, independent_llm)` 的规则部分（`_check_rules`，11 项确定性检查）＋独立 LLM 裁判。**`api_evaluate` 里 `judge_trace(t, bot.llm)`（`app/main.py:504`）的自评结果只能写进参考字段，不作验收依据**——同一个模型评自己，是上一轮确认过的同源偏差。

**FEEDBACK**：客观信号优先（任务成功率、citation 命中期望路径、被 gate 拦次数、answer 非空、耗时/token）；主观 complaint 只作产品角色输入，且必须能追到 `trace_id`。

**PLAN**：需求校验通过才进下一阶段；校验不通过记 `note` 并跳过本轮改动（不算失败轮）。

**PATCH**：路径白名单校验。
- 允许（P2）：`app/bot.py`、`app/knowledge/query.py`、`app/presentation.py`、`app/retrieval.py`、`app/memory.py` 等产品行为文件
- 禁止：`loop/**`、`tests/**`、`app/evaluate.py`、`app/evidence.py`、`app/trace.py`、`app/main.py` 的鉴权/守卫段、`config/config.yaml` 中的 `workspaces[].path` 与 `read_only`、`data/**`
- 任何 diff 含禁改路径 → 直接 reject，记 `patch_record.rejected_reason`

**RESTART**：`terminate()` 旧进程 → 起新进程 → 轮询 `/healthz`（超时 30s 判失败）→ 跑一轮冒烟问答确认服务可用。失败直接 REVERT。

**VERIFY**：四门禁，见下节。

**DECIDE**：全过 → `git add -A && git commit -m "loop(round N): <req goal>"`，写 `commit_hash`；任一不过 → `git revert` 到 `parent_round_id.commit_hash`，`status=reverted`，保留 patch 与 metrics 供复盘。

---

## 5. 验收契约：四门禁（缺一不可）

| # | 门禁 | 实现 | 依据 |
|---|---|---|---|
| 1 | 全量回归绿 | 逐个 `python tests/*_test.py`（13 个），非零退出即失败 | 现成 200+ 断言，无需 pytest |
| 2 | 固定题集不降 | `judge_score` / `task_success_rate` / `citation_hit_rate` 对 `baseline.json` 无显著下降；holdout 盲评 | 独立裁判 + 留出集；防 Goodhart |
| 3 | 知识库未污染 | 目录 manifest hash 与 PRECHECK 一致 | `files.sha256` / `kb_file.hash` |
| 4 | 基准区未篡改 | `loop/frozen/` hash 与 `baseline_hash` 一致 | 防开发角色改评分器/删断言 |

附加：单轮 `llm_calls` / wall time 不超预算。任一失败 → REVERT + 记 `stop_reason`；连续 2 轮门禁失败 → abort。

**门禁 4 是整套方案的生命线。** `JUDGE_SYSTEM`、`score_rule`、`_check_rules`、`evidence_coverage`、`tests/*` 全在 codelib 仓库里，本来都落在开发角色的可写范围内。把评分改松、把断言删一条、把 coverage 阈值调低，指标当场就涨而产品没动。所以评测体系必须物理挪出可写区，每轮校验 hash。

---

## 6. 防刷分 / 防漂移 / 防失控

- **裁判独立**：不同 endpoint + 不同 model + 固定 prompt；生成链路上 `bot.llm` 的自评只作参考。
- **评分器不可写**：见 PATCH 禁改清单。
- **boost 修正**（P1 必做）：现在 `KnowledgeStore.add_boost`（`app/knowledge/schema.py:185`）是 `weight=min(weight+0.5, 5.0)` 只增不减，`hits` 字段 12 行全是 0（代码里只有 SELECT 没有 UPDATE），`_apply_boosts` 让被加权文件更容易进上下文 → 下轮更不容易报缺失 → 权重继续涨，天然确认偏差闭环。补：`hits` 回写、时间衰减、上限、A/B 对照、漂移报警。
- **留出集**：holdout 题集对开发角色不可见，防止针对题集过拟合。
- **趋势判定**：连续 3 轮指标提升 < 阈值（如 `judge_score` +2 或任务成功率不涨）→ 判定无显著提升，停。防止"看起来一直在涨"。
- **成本熔断**：全局 `asyncio.Semaphore`、每轮 LLM 调用上限、wall time 上限、`loop/STOP` kill-switch 文件、异常 Webhook 告警（复用 `WeComChannel.send_webhook` / `FeishuChannel.send_webhook`）。一次 `!deep` 问答可能 8~11 次串行调用，五个角色一轮保守估计 30~50 次，没有预算就是半夜烧钱。
- **人工闸门开关**：`require_human_gate: true` 时每轮 DECIDE 前暂停等 admin 确认；默认关（无人值守）。

---

## 7. 终止条件与回滚

硬上限（写在 `loop/config.yaml`）：`max_rounds: 10`、`max_wall_time: 2h`、`max_llm_calls_per_run: 2000`、`no_gain_rounds: 3`、`gate_fail_rounds: 2`。任一命中即 `status=done/aborted` 并写 `stop_reason`。

`loop/STOP` 文件 = 立即停机（每轮 PRECHECK 检查；紧急时主循环每步也检查）。

回滚：`git revert` 到 `parent_round_id.commit_hash`，重新起服务，`round.status=reverted`。patch、metrics、feedback 全部保留，便于复盘"为什么这轮变差了"。

收尾产出：`loop/reports/<run_id>.md`（每轮指标曲线、接受了什么、回滚了什么、停在哪、成本）。

---

## 8. 分期落地

| 期 | 目标 | 开发角色能改什么 | 验收 | 估计 |
|---|---|---|---|---|
| **P0 评测闭环** | 拿到可复现的跨轮指标曲线，**完全不改产品代码** | 无（只观察） | 同一批任务在版本 A/B 上跑分可复现；四门禁跑通；`loop.db` 有完整轮次记录 | 3~5 天 |
| **P1 策略闭环** | 评估 → 改动 → 复测 → 接受/回滚 跑通 | 仅 `boost` 权重、检索参数、`top_k/top_n/graph_hops`、可版本化的 prompt 参数 | A/B 能判出"改动是否真的变好"；boost 有衰减/hits/漂移报警；至少一轮成功接受 + 一轮成功回滚 | ~1 周 |
| **P2 代码闭环** | 开发角色产出 patch → 重启 → 全量回归 | 白名单内 `app/*.py` | 连续 ≥5 轮无人值守，四门禁全过，holdout 指标不降，知识库 hash 恒定 | 1~2 周 |

合计 **3~4 周**。P0 不做完不要进 P1，P1 不做完不要放开发角色写代码。

---

## 9. 新增文件清单

```
loop/
  driver.py            # 外部编排主进程、状态机、终止判定
  state.py             # loop.db DDL + CRUD
  roles.py             # 四角色 prompt 调用封装（隔离 llm 客户端）
  simulator.py         # persona + golden_task 执行
  judge.py             # 独立裁判 + 四门禁 + 指标计算
  guard.py             # 白名单 / frozen hash / workspace hash / 预算 / STOP
  ports.py             # subprocess 起停服务、/healthz 等待、A/B 端口
  config.yaml          # 循环自身配置（预算、阈值、白名单、裁判 endpoint）
  selftest/            # driver 自身的测试（不能放 tests/，tests/ 已锁定）
  frozen/
    golden_tasks.yaml  # 固定题集（train / holdout）
    prompts/*.md       # 四角色 prompt（锁死）
    baseline.json      # 基线指标 + 全量 hash
  STOP                 # kill-switch（存在即停）
  reports/             # 每个 run 的收尾报告
docs/self-loop-plan.md
```

**对现有代码的最小改动**
- `app/knowledge/schema.py`：`add_boost` 加衰减、`hits` 回写（P1）
- `app/main.py`：可选新增 `/api/loop/status` 只读观测接口，供网页看循环状态
- `app/evaluate.py` / `app/evidence.py` / `tests/`：一律不动，锁定
- `config/config.yaml`：不动（`workspaces[].path` 是红线）；循环参数放 `loop/config.yaml`

---

## 10. 明确不做的事

- 不改知识库工程代码，不往 `workspaces[].path` 写任何东西。
- 不放开主服务 `read_only`，不给 codelib 自己加写知识库的接口。
- 不用同一个模型既生成又裁判；自评结果不作验收依据。
- 不在 P0/P1 引入代码生成。
- 不追求"无限跑"——默认硬上限，随时能停、能回滚、能人工接管。

---

## 11. 第一步具体动作（P0 起步）

1. 从 `conversations` / `trace` / `qa_memory` 抽出真实问题，人工标 20~30 条成 `loop/frozen/golden_tasks.yaml`，分 train/holdout，写清 `expected_paths` 与 `success_criteria`。
2. 用现有 `scripts/eval_real.py` 的临时库模式跑一遍，固化 `baseline.json`（指标 + `frozen/` 全量 hash + 知识库 manifest hash）。
3. 写 `loop/state.py` 的 DDL 和 `loop/driver.py` 的 PRECHECK / USE / JUDGE / VERIFY 四段，**先不接 PLAN/PATCH**，让循环只观察、只记录。
4. 配一个独立裁判 endpoint（与 `llm.base_url` 不同的 model），验证 `judge_trace(trace, independent_llm)` 能出分。
5. 空跑 3 轮，确认指标曲线可复现、四门禁全过、`loop/STOP` 生效——再决定要不要放行 P1。

**顺序上第一步不是写循环，是先把评测区锁死。** 否则跑起来第一天它就会给自己打满分。
