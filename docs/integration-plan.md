# codelib 整合方案：从「检索问答」到「代码理解 Agent」

> 目标：解决「每次回答都很表面」。做法不是继续调检索参数，而是补一层**只读研究循环（Research Loop）**，
> 把开源 harness 已验证的共同范式吸收进来，复用我们已有的索引、留痕与 A/B 验收体系。

---

## 0. 结论先行

我们缺的不是更好的检索排序，是**没有循环**：现在一次 FTS → 一次生成 → 结束。
开源 harness 的共同精华是「模型拿着工具，自己决定下一步读什么，读够了再答」。
整合后形态：`plan（要验证哪些断言）→ 工具读代码 → 观察 → 更新证据表 → 直到断言都有证据或预算耗尽 → 生成 → 复核`。

**同时要说清楚：用户的判断「他们都实现过了」一半对一半不对。**
对的是 agent loop、工具集、按需上下文、compaction、子任务、repo wiki、权限这些机制确实成熟；
不对的是它们都是**编码 agent**（有 Edit/Write/Shell、能跑测试、能开 worktree），
我们 `read_only: true` 且目标是解释而非修改——写与执行部分一个都不能抄，
「验证闭环」也必须从"跑测试"换成"证据核对"。

---

## 1. 调研核实（证据强度如实标注）

> 注：本节为初版粗略核实，**更完整的一手调研与方案修订见 §15**。

| 参考 | 核实到的事实 | 证据强度 |
|---|---|---|
| **ZCode** | `zcode.z.ai` 确认是 **GLM-5.3 官方 Harness**（我们项目配的正是 glm-5.3）。定位 Agentic Development Environment；欢迎页写明「依托 1M 上下文与长程任务能力，把目标、文件、终端结果、执行模式和 Git 状态保持在同一个任务里，从规划推进到实现与验证收口」。文档目录含：ZCode Agent、目标模式、任务与文件管理、**Wiki（repo-wiki）**、Memory、子智能体、Skill、MCP、Hooks、安全操作确认、ADE-tools | 官网 + 文档目录 + 欢迎页正文，**强** |
| **opencode** | 官网 `opencode.ai/docs`（V2，仓库 `github.com/anomalyco/opencode`）。Tools 页目录含 Read / Glob / Grep / Edit / Write / Patch / Commands / Shell / Web / Webfetch / Websearch / Question / Skill / Automation / Subagent / Execute / Sessions / Worktrees；配置侧含 **Compaction**（把旧对话压成摘要让长会话继续）、Snapshots、References、Permissions、Policies、Agents、Skills、MCP servers | 官方 Docs 目录 + Compaction 正文首段，**强**（README raw 抓取失败，不引用） |
| **DeepSeek harness** | **没查到明确的同名开源项目**：搜索只返回 DeepSeek 公司、API Platform、App、GitHub 组织页，没有名为 "DeepSeek Harness" 的仓库或框架 | 未核实，**不编造** |
| **Alma** | 本机宿主应用。产品层面可观察到：任务委派与多角色并行、工作区文件树/变更/预览、技能扩展、长任务编排 | 产品可见形态，**中等**（不涉内部实现） |

ZCode 的子页面（Wiki/Agent/目标模式）正文由 JS 渲染，抓取只拿到目录，
所以下表里的「能力」按**目录名 + 欢迎页表述**归类，不假装读过实现细节。

---

## 2. 共同精华：真正要抄的 5 个机制

1. **Agent loop，不是单次生成**：`观察 → 决定下一步 → 调工具 → 观察` 循环，显式终止条件（证据够了 / 步数 / 预算 / kill switch）。
2. **工具化的代码导航**：模型能**主动点名** read / grep / 找引用 / 找调用者，而不是被动等 top_n 片段。
3. **按需上下文 + 压缩**：不把全库一次塞进去；按当前假设读相关片段，维护「已读清单 + 结论草稿」，旧观察滚动摘要（opencode Compaction 的只读版）。
4. **验证闭环**：结论拆成可检验的断言，逐条去代码里找证据；找不到就改假设、继续读，**不许用「候选未覆盖」收尾**。
5. **任务级规划与子任务**：目标模式 / 子智能体把长任务拆开（ZCode），问答场景**先不上多模型**，单 loop 内换 prompt 即可。

**不抄**：Edit / Write / Patch / Shell / Execute / Worktrees（写与执行）、浏览器自动化、闲时任务、远程控制、图像生成。
**要换**：它们的验证靠跑测试/编译（有副作用、有明确成败），我们靠 `symbol.body` + `edge` 做证据核对。

---

## 3. 我们已有的资产（别重写）

| 资产 | 现状 | 整合中的角色 |
|---|---|---|
| `knowledge.db` | `symbol` 1130 个、**body 全量非空**（平均 571 字符）；`edge` 3265 条 `calls`；`chunk` / `doc_chunk` / `kb_file` | 现成的静态调用图 + 函数体库，等价于 LSP 的 go-to-definition / find-references |
| `app/chain.py` | 本轮原型：沿链展开、结构锚点、事实抽取（**首轮验收 3/8，未接线**） | P0 起点，见 §6 |
| `app/lexicon.py` | 运行时热读词表、A/B 可回滚、空表零影响 | 中文业务词 → 代码符号的桥 |
| `app/trace.py` / `trace_event` | 全链路留痕（已有 9~11 stage） | 直接扩成 agent 轨迹（新 stage `research_tool`） |
| 三层归因 / baseline / A-B | 24 题题集、hash 锁定基线、自动接受/回滚 | **验收层**。开源 harness 普遍没有「固定题集量化改动是否有效」，这是我们的独特优势，必须保留 |
| `readonly_guard` | 三重只读 | 工具层白名单直接复用 |

---

## 4. 差距对照

| 维度 | 现在 | 整合后 |
|---|---|---|
| 生成形态 | 一次检索 → 一次生成 | 多步 loop，模型决定读什么 |
| 上下文 | `top_n=10` / 4000 token 一次装 | 按需读，独立预算，滚动摘要 |
| 跨文件链 | `graph_hops=1` | 双向沿 `calls` 边走到 4~5 跳 |
| 锚点 | FTS 词面，中文问法 0 条 .go | 名称 + 结构特征 + lexicon + 模型点名 |
| 证据不足 | 「候选未覆盖」免责 | 强制继续读 / 显式列出「没读到 X 文件」 |
| 验证 | 无 | 断言 → 逐条回指函数体 |
| 评估口径 | 奖励谨慎、有引用 | 奖励证据链完整、敢下结论且对、被纠正后能改 |

---

## 5. 目标架构（新增，全只读，不改动现有主链路行为）

```
app/research/                      # 新增
  tools.py        # 只读工具集（全部基于 knowledge.db / 白名单文件读）
  loop.py         # ResearchLoop：状态机、终止条件、预算、kill switch
  evidence.py     # 断言表：已证实 / 已证伪 / 仍缺
  prompts/        # planner.md / reader.md / verifier.md（锁在 loop/frozen/prompts/）
app/chain.py      # 已有原型，P0 修好后作为 tools 里的一个能力
config/config.yaml 新增 research: { enabled:false, max_steps:12, max_chars:24000, ... }
```

**并行、灰度**：Research Loop 与现有 FTS 通道并行取证据，用 `research.enabled` 开关；
默认关，行为与现在完全一致；灰度通过后才在 `BotService.handle` 里合并证据。
不改索引格式、不改 `read_only`、不给任何写路径。

## 6. 工具集（只读版，逐项映射到已有数据）

| 工具 | 实现 | 数据源 |
|---|---|---|
| `search_symbols(name)` | qualname / signature 精确+模糊 | `symbol` |
| `read_body(qualname)` | 取整段函数体（含行号） | `symbol.body` |
| `callers(sym)` / `callees(sym)` | **双向**沿 `calls` 边 | `edge` + `symbol` |
| `grep(pattern, path?)` | 全文 / 正则 | `chunk` / `doc_chunk`（FTS5 + LIKE） |
| `read_lines(path, a, b)` | 只读工程文件指定行 | 白名单目录 + `read_text_safe` |
| `related_sections(path)` | 文档章节标题 | `doc_chunk.section` |
| `expand_chain(anchor, hops)` | 沿链一次性展开 | 修好后的 `app/chain.py` |
| `check_fact(claim)` | 在已读证据里找，缺则触发补读 | `evidence.py` |

**权限**：工具层硬白名单，只允许读 `workspaces[].path` 与 `knowledge.db`；
不提供写、不提供 shell、不提供网络。每步调用写 `trace_event(stage="research_tool")`，
记录工具名、参数、返回字符数，可完整回放「它到底读了哪些代码」。

## 7. Research Loop 状态机

```
PLAN      把问题拆成 N 条可检验断言 + 候选起点符号（planner）
  ↓
LOOP      while 未终止:
            reader 选一个工具调用 → 观察结果
            → 更新 evidence: 证实 / 证伪 / 仍缺
            → 若上下文超阈值，把旧观察压成「已读清单 + 结论草稿」（compaction 只读版）
  ↓
STOP      断言全部有证据 | max_steps | max_chars | max_llm_calls | loop/STOP 文件
  ↓
ANSWER    生成答案，每条因果必须回指某个 read_body 结果；
          缺证据的断言必须写「本次未读到 <文件/符号>」，禁止「候选未覆盖」
  ↓
VERIFY    verifier 复核：答案里的跨文件因果是否都能指回证据；不通过则回到 LOOP 补读
```

终止条件写在 `research` 配置里，默认 `max_steps=12`、`max_chars=24000`、
`max_llm_calls=8`、`max_wall_ms=120000`；`loop/STOP` 文件立即停机。
成本可控，且每一步都可回放——避免 harness 常见的「无限循环烧钱」。

## 8. 为什么必须上 loop，而不是把自动展开做复杂

本轮 `app/chain.py` 原型给了反证：沿调用链自动展开，首轮验收只拿到 **3/8** 个关键事实，
覆盖的还全是 `mock_nlp_ctl.go` / `cron_tab` 这类噪声文件。诊断出的四个原因：

1. 名称锚点被中文单字/二元组污染（`tokenize` 把「顺序」拆成单字，撞到一堆无关符号）；
2. 结构锚点按「短函数优先」取前 3 个，偏偏选中 `Final` / `RunMetric`，漏掉 `OnQDRawNlpMsgNtf` 里的 `go`；
3. 只走 `calls` **出边**，从 `sendDataToKafka` 走不到上游 `ConsumeClaim`；
4. 问题里没有「位点」字样就没触发 offset 关注点；`max_nodes` 被 logging/utils 吃满。

这四条都是**启发式选锚点的脆弱性**：静态规则永远会有漏。
harness 的答案更朴素——**让模型看着观察结果自己决定下一步读哪个符号**。
所以 `chain.py` 不是要修到 8/8 才用，而是降级成工具集里的一个能力；
真正的锚点决策交给 loop。P0 仍会修这四点（双向边、目录优先、锚点收紧、关注点联动），
因为修完能显著降低 loop 的起步成本，但不再指望它单独解决理解问题。

---

## 9. 分阶段路线

| 期 | 做什么 | 验收 | 估计 |
|---|---|---|---|
| **P0 工具层** | 修 `app/chain.py` 四点（双向边、结构锚点按业务目录优先、名称锚点收紧、顺序类问题默认带 offset/entry 关注点）；新建 `app/research/tools.py`，实现 §6 全部只读工具；`probe_chain.py` 的事实覆盖从 3/8 提到 **8/8** | 顺序题 8 个关键事实全命中，且覆盖文件集中在 `service/` `kafka_client/` `bond_chat/` | 1~2 天 |
| **P1 循环层** | `loop.py` + `evidence.py` + planner/reader/verifier 三套 prompt；接 `trace_event`；`loop/STOP` 与预算熔断。用 24 题跑 `research off / on` A/B（复用 `loop/eval_lexicon.py` 的框架，只换自变量） | A/B 有显著提升、两轮确定性 PASS、至少一次自动回滚后再接受；证据链可完整回放 | 2~3 天 |
| **P2 接入与口径** | `BotService.handle` 灰度开关合并证据；`JUDGE_SYSTEM` 改口径（奖励证据链、惩罚免责）；`expected_*` 验收从「路径命中」扩到「断言有证据」 | 24 题三层指标提升；「候选未覆盖」出现次数归零或显著下降；5 个现有回归测试全过 | 2~3 天 |
| **P3 子任务（可选）** | 若单 loop 在长问题上撞预算，再引入 ZCode 式目标模式/子智能体拆分 | 仅在 P1 数据证明单 loop 不够时启动，不预先上多模型 | 待定 |

总工期约 **1~2 周**，全部增量、可灰度、可回滚。

## 10. 验收口径（复用现成体系，别另起炉灶）

- **可证伪主验收**：那道顺序题的 8 个代码事实（`go asyncQDNlpMsgProcess`、`sess.MarkMessage`、空 `OnKafkaWriteOK`、`nlp_%s_%s` key、`NewHashPartitioner`、`GenBoundKey`、round 上下文、回调空实现）必须全部由已读证据支撑，并给出正确结论。
- **现有指标**：24 题三层归因、`baseline.json` 对照、`report_sha256` 可复现、hash 自洽。
- **新增指标**：
  - `evidence_chain_depth`：答案里跨文件因果链的最大跳数（目标 ≥3，现在 ≈0~1）；
  - `go_source_ratio`：已读文件中 `.go` 源码占比（现在中文问法下为 0）；
  - `disclaimer_rate`：出现「候选未覆盖 / 无法证实也无法证伪」的题数（要下降）；
  - `assertion_coverage`：答案断言中能回指 `read_body` 结果的比例。
- **A/B 规矩不变**：改动先跑基线、多次采样取中位数、设最小提升阈值、失败自动回滚；
  第一轮若不提升（像词表那次），先 leave-one-out 诊断再复测，不许直接落地。

## 11. 明确不做

- 不引入任何写权限或 shell/执行工具（编码 harness 的 Edit/Write/Patch/Shell/Worktrees 一律不抄）。
- 不重写索引格式、不改 `knowledge.db` schema、不动主库 `codelib.db`。
- 不上多模型 / 多智能体，直到 P1 数据证明单 loop 不够。
- 不把「我这次没读到」写成「代码里不存在」；不把「没读到」当成可以通过验收的状态。
- 不照搬浏览器自动化、远程控制、闲时任务、图像生成等与「理解代码」无关的产品特性。
- 不擅自重启跑在 `:8000` 的现有服务（接入新代码需重启才生效，届时单独处理）。

## 12. 已知缺口（不编造）

- **DeepSeek harness**：未查到同名开源项目，方案里没有为它编造能力。若指某个具体仓库（或 SWE-bench 类评测 harness），补链接后单独评估。
- ~~kimi-code-cli 未核实~~ → **已解决**：仓库名纠正为 `MoonshotAI/kimi-code`，能力见 §16.5。
- ZCode 的 Wiki / Agent / 目标模式子页正文由 JS 渲染未抓到，能力归类依据文档目录与欢迎页表述。
- opencode 的 GitHub README（raw）抓取失败，引用的是官方 Docs 的工具目录与 Compaction 正文。
- 本机 Alma 只按产品可见形态归类，不涉及任何内部实现。

## 13. 与既有方案的关系

- `docs/self-loop-plan.md`：通用自循环架构，保留作参考。
- `docs/answer-quality-loop.md`：单目标调参闭环（题集 / 归因 / 词表 / A-B），**已跑通并继续作为验收层**。
- 本文：补的是它上面缺的「理解层」——前者优化「找对片段」，本文解决「读懂系统」。
  两套不冲突：调参闭环负责量化改动有没有变好，Research Loop 负责让改动本身有意义。

## 14. 第一步

修 `app/chain.py` 的四个锚点问题、把 `probe_chain.py` 的 3/8 跑到 8/8，
同时把 §6 的只读工具集落进 `app/research/tools.py`。
这一步不碰主链路、不需要 LLM、秒级可验；跑通了再开始写 loop。

---

## 15. 开源 harness 一手调研与方案修订（补充）

> 本节取代 §1 的粗略核实。GitHub 直连在这台机器上不通（`git ls-remote` / curl / WebFetch 全 000），
> 最终经 **gh-proxy 代理取 raw README**、**npm registry / unpkg 取 CLI 包**才拿到一手资料。
> 只写核实到的，标注每条的证据强度。

### 15.1 六个项目（含仓库名纠正）

| 项目 | 核实到的一手事实 | 证据 |
|---|---|---|
| **ZCode** `zai-org/ZCode` | "AI 编程工作台，桌面 / 浏览器 / 终端三端"；仓库含客户端、后端服务、共享 UI 与 **Agent CLI + 运行时源码 `apps/zcode-cli/`**。CLI 使用指南 13 章：04 工具与评测配置（工具可见性、评测边界）、05 权限（权限模式、拒绝行为、**执行证据**）、06 会话（目标、用量、持久化）、08 Skills、09 Hooks、11 指令与记忆（`AGENTS.md`、记忆隔离与提取）、**13 批量任务与运行记录（可复现运行、结果判定、轨迹导出）**。「每次运行至少保存 `zcode --version`、`zcode doctor --json`、构建提交号或 SHA-256，记录插件版本、模型 ID、模型配置与任务工作区；相同 CLI 版本不等于相同运行条件」。官网另有 Repo Wiki / Memory / 子智能体 / 目标模式 | README + CLI 使用指南正文，**强** |
| **Qwen Code** `QwenLM/qwen-code` | 用户给的 `QwenLM/QwenCode` 不存在（`git ls-remote` 报 not found），实际仓库是 `qwen-code`。README："open-source AI coding agent for terminal, editor, desktop, browser, and chat"；「**Agentic out of the box — Auto-Memory, Auto-Skills, SubAgents, Agent Teams, and MCP**」；「1.0 Preview 合并了 Qoder 与 Qwen Code 的 agent harness 能力」；官方文档称「能够感知整个项目结构」；支持长生命周期服务让多客户端**共享 agent sessions**。npm `@qwen-code/qwen-code@0.27.2`，dist-tags 里有 `notes-compaction-test` / `simplify-system-prompt` 等实验分支 | README + npm registry + 官方文档搜索摘要，**强** |
| **MiniMax Code** `MiniMax-AI/minimax-code` | "Build, test, and keep iterating from your terminal"；`mcode init .` 生成 `AGENTS.md`，要求写清「预期结果、允许的改动、**如何验证**」；**Lightweight 模式**明确列出被省略的上下文块：workspace instructions / Skills / memory blocks / MCP schemas / full environment block；`/compact` 用标准压缩、子 agent 会话不继承 lightweight；支持 ACP 接编辑器 | README 正文，**强** |
| **MiMo-Code** `XiaomiMiMo/MiMo-Code` | "Where Models and Agents Co-Evolve"；terminal-native，**「persistent memory system to keep a deep understanding of your project across sessions」**（跨会话保持对项目的深度理解）；**Smart orchestration**：按任务类型与成本动态选模型/框架/工具，复杂任务多 agent 并行；内置 `websearch` / `codesearch`，MCP 可配；Desktop 以 MiMo Code 为核心引擎，多会话分工 | README 正文，**强** |
| **opencode** `anomalyco/opencode` | Tools 页：Read / Glob / Grep / Edit / Write / Patch / Shell / Question / Skill / Subagent / Sessions / Worktrees；配置侧 Compaction（旧对话压成摘要）、Snapshots、References、Permissions、Policies | 官方 Docs 目录，**中**（GitHub README raw 抓取失败） |
| **Kimi Code CLI** `MoonshotAI/kimi-code` | 用户给的 `kimi-code-cli` **不存在**（直连 404、`git ls-remote` 报 Repository not found）；`MoonshotAI/kimi-cli` 是**已归档的 Python 旧版**，README 明确指向继任者 `MoonshotAI/kimi-code`。后者：terminal agent「read/edit code、run shell、search files、fetch web、**choose the next step based on the feedback it receives**」；Key Features 含对话式 MCP 配置 `/mcp-config`、插件 trust level、**内置 `coder`/`explore`/`plan` 子智能体在隔离上下文并行**、Lifecycle hooks（gate 危险工具调用/审计）、ACP 编辑器集成。示例任务「Take a look at this project and explain its main directories」正是我们的场景 | README 全文，**强**（详见 §16.5） |

### 15.2 共同范式：8 条，每条都有出处

1. **Agent Runtime 是独立、可复用的引擎**：ZCode 把 CLI + runtime 单独放 `apps/zcode-cli/`，三端共用；Qwen 支持长生命周期服务共享 sessions。→ 我们的 `BotService` 把检索与生成焊死，没有独立 runtime。
2. **上下文是分层装配，不是一个 prompt**：MiniMax 的 lightweight 段是铁证——workspace instructions / Skills / memory / MCP schemas / environment 各自成块、可整块省略。→ 我们靠 `budget_tokens` 一刀切，所有东西挤一个 prompt。
3. **项目理解是持久化状态，不是每次现查**：MiMo persistent memory「跨会话深度理解项目」、ZCode Repo Wiki + Memory + `AGENTS.md`、Qwen Auto-Memory +「感知整个项目结构」。→ 我们每问一次从零 FTS。
4. **工具可见性与权限是一等公民**：ZCode 04/05 把「工具可见性、评测边界、拒绝行为、执行证据」单列成章；opencode 有 Permissions/Policies。→ 我们 `readonly_guard` 只是路径黑名单。
5. **会话 / 目标 / 轨迹可持久化、可回放、可批量评测**：ZCode 06「目标、用量、持久化」+ 13「可复现运行、结果判定、轨迹导出」。→ 我们有 `trace_event`，缺「目标」与「运行记录」层。
6. **验证是显式环节**：MiniMax `mcode init` 强制写「如何验证」；Qwen 用自家 agent 提 issue / review / 跑测试。→ 我们没有验证，只有生成。
7. **可复现性是工程纪律**：ZCode 要求记录 version / doctor / build SHA / 模型 ID / 插件版本，明说「相同 CLI 版本不等于相同运行条件」。→ **这条我们没落后**：`report_sha256`、`baseline.json`、三个工件 hash 自洽已经是同一套思路，继续保留。
8. **模型与 harness 协同**：MiMo 标题即「Models and Agents Co-Evolve」，ZCode 是 GLM-5.3 官方 Harness 并做深度联调，Qwen 合并 Qoder harness。→ 解释了为什么「换个模型」救不了我们：裸 `LLMClient` 缺的是把模型能力接出来的那层工程。

### 15.3 对「综合一下就行」的修正

这批项目**确实实现了机制**，但三点不能直接搬：

- 它们都是**模型厂商自家 harness**，围绕自家模型的 tool-calling / 长上下文做深度联调（GLM-5.3 / MiniMax M / MiMo / Qwen）。我们本地网关跑的正是 `glm-5.3`，所以 **ZCode 是最贴近的参照**，但它的 runtime 依赖自家模型行为，源码搬过来不可用。
- 它们全是**编码 agent**（读、改、跑、提交）；我们 `read_only: true`，目标是解释。写工具、shell、worktree 一律不抄。
- 「理解整个项目」在这些产品里靠的是**持久化记忆 + 预建 Wiki + 分层上下文**三件套，不是一次更聪明的检索。这恰好证明 §0 的判断：该补的是 runtime 层，不是排序参数。

### 15.4 对方案的 5 处具体修订

1. **拆分层上下文**（对应第 2 条）：在 `app/research/` 之外，把 `BotService` 的 prompt 拆成独立可开关的块——`workspace_map`（项目结构摘要）/ `skills`（检索策略）/ `memory`（跨会话事实）/ `evidence`（本轮已读证据）/ `environment`。每块独立预算，可按问题类型省略。这是 MiniMax lightweight 的只读版。
2. **加持久化项目记忆**（对应第 1、3 条）：`knowledge.db` 里有一张**空的预留表 `card`**（`id, workspace, type, title, body, links, source_refs, hash`），代码里除建表/清理外从未写入——正好用来存「项目级理解卡」：入口链路、并发模型、状态归属、消息流向。由 Research Loop 在读完代码后生成/更新，跨会话复用，不再每次从零推。这比 ZCode 的 Wiki 更结构化（我们已有 `symbol` / `edge` 可挂 `source_refs`）。
3. **工具可见性与执行证据**（第 4 条）：每个 `research_tool` 调用写 `trace_event`，记录工具名、参数、返回字符数、拒绝原因；配置里给工具白名单，默认全只读。
4. **运行记录层**（第 5、7 条）：把 ZCode 13 章的「可复现运行 / 结果判定 / 轨迹导出」映射到我们已有的 `loop/reports/` + `baseline.json`，补一个 `run_manifest.json`（模型 ID、词表版本、题集 hash、代码提交号、工具集版本），让「同一份报告」在换模型/换插件后可辨。这条我们已经有 80%，是补齐不是新建。
5. **Runtime 独立**（第 1 条）：`app/research/loop.py` 不依赖 FastAPI / BotService，单独可被 CLI 调用，方便未来接 `scripts/eval_real.py` 式批量评测与轨迹导出。

### 15.5 仍然不做的

不引入写/执行工具，不照搬桌面端/浏览器/远程控制/Computer Use，不上多模型编排直到单 loop 被数据证明不够；
kimi-code-cli 在资料补齐前不纳入能力对照；ZCode 子页正文（JS 渲染）只按文档目录归类，不假装读过实现。

---

## 16. 评测纪律（ZCode CLI 04/05/06/11/13 章，一手全文）

网络恢复后直连取到全文。这几章讲的不是「怎么让 agent 更聪明」，是**怎么让评测结果可信**——恰好是我们自循环最该补的一层，而且几条正好打在我们现在的漏洞上。

### 16.1 九条原文纪律

1. 「CLI 可以作为任务执行器；数据准备、环境隔离、资源预算和评分由**外部启动器**负责」→ 我们 `loop/` 作为外部 driver，分层是对的。
2. 「不能把一次 prompt 成功当作 benchmark 集成完成」→ 不能把「跑通一次问答」当成能力达标。
3. 每个样本固定：构建提交/二进制 SHA-256、CLI 版本与 doctor 输出、平台、模型与 provider 协议、非敏感模型选项、任务输入、工作区提交、扩展清单、工具过滤、权限模式、运行时限；「**API Key 与登录凭据不要归档**」。
4. 「每个独立样本使用**独立的工作目录、用户目录和数据基目录**。只换 sessionId 或只设置 `ZCODE_STORAGE_DIR` 不足以隔离个人配置、插件、记忆和用户指令」。
5. 判定流程：「启动失败 / 被取消 / 超时 → 保存失败证据，**不判成功**；正常结束 → 解析输出 → 检查工具错误与任务产物 → **独立评分**」；保留 `sessionId` / `traceId` / `turnId` / 原始退出码 / signal / 超时状态。
6. 「**评测评分不能依赖模型最后一句"已完成"**」；「训练数据应保留真实工具调用与结果，包括拒绝、超时和失败；不要把模型对工具的转述补成一次成功调用」。
7. 「**工具拒绝后模型仍可能继续回答，进程也可能返回 0**。同时检查流式事件、工具结果和文件产物」。
8. 「验证指令是否进入上下文时，使用受控的无敏感标记或检查实际请求，**避免依靠模型声称"已阅读"**」；「并发评测应使用隔离用户目录和固定工作区，**记录这两处指令文件的内容哈希**」。
9. 「**单轮结果、跨轮学习和跨任务复用应分别设计实验**，避免把上一个样本的记忆带入下一个独立样本」；「不要把内部 `request.messages` 强行填回 provider 请求体来伪造完整记录」。

### 16.2 对照自查：我们现在的缺口

| 纪律 | 现状 | 要补 |
|---|---|---|
| 固定运行条件 | `baseline.json` 记了题集/runner/frozen hash，`run.json` 记了 `llm_model` | 补模型 ID + provider 协议、平台、工具面、权限模式、运行时限、工作区提交号、**`AGENTS.md` 内容 hash**（它是我们检索最常召回的文档，内容一变结果就变，现在完全没记） |
| 每样本独立环境 | A/B 复用同一个临时 `knowledge.db` 副本 | 每样本独立工作目录 + 数据基目录；跨轮复用必须显式记录 |
| 独立评分 | `JUDGE_SYSTEM` 是 LLM 裁判，代理归因来自同链路 | 改由**外部脚本**按答案产物/工具结果判定，不采信模型自述 |
| 退出状态 | `run.json` 只有 error 字符串 | 记 exit code / signal / timeout / traceId / turnId |
| 跨轮记忆隔离 | 自循环累积 boost / lexicon / card | 拆「单轮结果 / 跨轮学习 / 跨任务复用」三类实验，禁止上一样本状态泄漏 |
| 凭据 | 无归档 | `run_manifest.json` 明确不写任何 key/token |

### 16.3 `run_manifest.json` 字段（可直接落）

```json
{
  "run_id": "", "created_at": "", "code_commit": "",
  "runner_sha256": "", "runner_version": "", "platform": "", "python_version": "",
  "model_id": "", "provider_protocol": "", "model_options_nonsecret": {},
  "cases_sha256": "", "workspace_commit": "", "knowledge_db_sha256": "",
  "agents_md_sha256": "", "lexicon_sha256": "",
  "toolset": [], "permission_mode": "read_only",
  "runtime_limits": {"max_steps":0,"max_chars":0,"max_wall_ms":0,"max_llm_calls":0},
  "sample": {"workdir":"","data_basedir":"","session_id":"","trace_id":"",
             "turn_id":"","exit_code":0,"signal":"","timeout":false,"status":""},
  "artifacts": ["answer","citations","tool_events","evidence"],
  "scoring": {"by": "external_script", "self_report_used": false}
}
```

### 16.4 对方案的修订

- §9 P1 增加前置步骤：**A/B 之前先落 `run_manifest.json`，否则结果不计入验收**。
- §10 验收口径新增三个指标：`independent_scoring`（是否外部脚本判定）、`sample_isolation`（每样本独立目录）、`manifest_complete`（字段齐全）。
- §15.4 第 4 条「运行记录层」细化为上面这份字段表；第 5 条 runtime 独立正好对应纪律 1。

### 16.5 Kimi Code CLI 补遗 → 一条架构修订

`explore` 子智能体「在**隔离上下文**中做专注工作，主对话保持干净」比我们 §7 的「滚动摘要」更干净：
**把「研究」作为隔离子任务**——子任务读完全部函数体，只把「结论 + 证据引用 + 仍缺的断言」交回主上下文，
不把 16000 字符的函数体塞进主对话。P1 采用这个形态，主 loop 只负责 plan / 分派 / 复核。
这也解释了为什么 ZCode、Qwen、Kimi 三家都做了子智能体：**长上下文不等于把东西全塞进去，而是分而治之**。

---

## 17. 源码级深读结论（2026-11-13，clone 一手源码）

> 网络恢复后浅克隆了 4 个仓库到系统临时目录（共 ~475M：kimi-code 100M / minimax-code 111M / MiMo-Code 38M / ZCode sparse 125M，3124 个 .ts）。
> qwen-code 1.5G 浅克隆失败，未读源码。Kimi 由子任务深读（其 1.1 万文件中相关子系统全覆盖）；
> MiniMax / MiMo 的两个深读子任务因模型配额超限失败，由我自查关键模块——**覆盖不完整，已标注**。
> 本节所有结论都有 `文件路径 + 函数名`，不是 README 转述。

### 17.1 血统发现（解释了为什么这几家长得很像）

- MiMo 的记忆服务注册名是 **`@opencode/Memory`**（`memory/service.ts:35`）——MiMo Code 从 **opencode** 派生。
- ZCode 仓库里有 `packages/adapters/src/model/opencode-session.ts` 适配器——同样源自 opencode。
- MiniMax 的核心是 `pi-turn-runner`（`PiUserMessage` 类型、pi 命名空间）——基于 pi coding agent 框架。
- 结论：**这批国产 harness 大多是 opencode / pi 系架构的衍生**，共同范式不是巧合，是同一套血统。
  对我们的意义：照抄它们的**架构分层**比照抄实现更靠谱；同时别指望从它们那里拿到"模型厂商独家魔法"——
  MiMo 宣传的 "deep understanding of your project" 源码里就是 **markdown 文件 + SQLite FTS5 索引**，没有向量库、没有 LLM 摘要。

### 17.2 源码级机制清单（按参考价值排序）

**① Kimi：同一 loop + 不同 profile = 子智能体（最值得照搬的形态）**
`packages/agent-core-v2/src/session/agentLifecycle/profile/profiles.ts`：`agent` / `coder` / `explore` 是**同一个循环引擎 + 不同 tool 白名单 + 不同 role**，不是三套引擎。`explore` 的白名单 `EXPLORE_TOOLS = [NotifyUser, Bash, Read, ReadMediaFile, Glob, Grep, WebSearch, FetchURL]`，且 `readOnly: true` 会把 `coding_section` 整块置空。子任务**冷启动**：`subagentService.spawn()` 用 `agentLifecycle.create()` 建全新 scope（独立 systemPrompt / history / wire.jsonl），只喂一条 prompt；返回值 = `latestAssistantText(history())` 即**子任务最后一条消息全文**，无二次摘要。三个例外要警惕：`fork:true` 会复制主对话历史快照（我们要禁用）；`resume` 可跨会话重建子 agent；`promptPrefix` 会注入 git 上下文。
→ 落地：我们的 Research Loop 采用**同一 loop + reader profile（纯只读白名单，连 Bash 都不要）**；子任务冷启动、禁 fork、返回值加 JSON 契约（kimi 是自由文本，太弱）。

**② Kimi：四级重复熔断（数值可直接抄）**
`agent/toolDedupe/toolDedupeService.ts:65-68`：`REPEAT_REMINDER_1_START=3 / 2_START=5 / 3_START=8 / FORCE_STOP=12`——按 `(toolName, 归一化args)` 的连续 streak 分级：3 次轻提醒、5 次中等、8 次强提醒、>12 次强制 `stopTurn` 且 `stopReason='repeat_breaker'`；重复调用直接复用先前结果（synthetic call id），不是只告警。`stopReason` 枚举一路传回调用方，让它知道该"改检索词"还是"自己接手"。
→ 落地：我们的 loop 直接抄数值与分级；`partialOutput` 上限 32KB 并标注 `isError:true` 的做法一并抄。

**③ Kimi：compaction 逐字保问题、丢工具轨迹**
`agent/contextMemory/compactionHandoff.ts`：触发 `usedSize >= max_input_tokens * 0.85`；压缩后保留**用户原始消息逐字**（头 2k + 尾 18k tokens，中间省略插 `<system-reminder>`）+ 一条第一人称自足摘要 + continuation 提醒；**丢弃全部 assistant 消息、全部 tool call/result**（`compactionUserMessageDisposition()` 对 injection/shell/task/hook_result 一律 'drop'）；摘要前要求写"已执行命令与具体返回值、已决策 vs 未决、未知项"。
→ 落地：我们的 compaction 在此基础上**多保留一项**——「已读证据路径清单」（只读问答特有，别的 harness 不需要）。

**④ Kimi：goal mode 的状态外置（不靠对话历史）**
`features/goal/goalService.ts`：turn 结束后自动 `loop.submit` 一条 continuation prompt；`objective / turnsUsed / budgets / blocked 次数` 存 state，每个新 turn 由 `GoalInjection` 重新注入完整或精简提醒；终止三态 `complete/blocked/paused` + 退出码 `0/3/6`；blocked 需同一阻塞连续 ≥3 个 goal turn 才成立；预算（turn/token/wall-clock）耗尽注入 `GOAL_BUDGET_STOP_REMINDER`。
→ 落地：我们 Research Loop 的「断言表 + 仍缺断言」就该这样外置到 state，每轮注入，而不是指望模型记得。

**⑤ MiMo：持久记忆 = md 文件 + FTS5 + fingerprint 增量（朴素但工程化到位）**
`memory/` 模块 655 行：磁盘 `<base>/<slug>/memory/**/*.md` 按 scope/type 组织；`memory_fts` 表带 `fingerprint + last_indexed_at`（`indexFromDisk` 返回 hit/updated/skipped 增量）；`reconcile.ts` 从磁盘对账重建；检索 `bm25()` 排序 + `snippet()` 高亮 + **相对分数地板**（top hit 总保留，其余须 ≥ top×0.15——注释明说防小语料 BM25 塌缩，3 倍超取再裁）；`write-gate.ts` 单点写开关（「Reading is never affected」）；**注入给路径不给内容**：prompt 里只有 `Project memory: <file>` 和 `Current session memory directory: <dir>` 两行，agent 用 memory 搜索工具或读文件自己决定看什么。
→ 落地：映射到我们的 `card` 表 + `chunk_fts`，**补两个我们没有的机制：fingerprint 增量索引、相对分数地板**（我们的 `_apply_boosts` 无此保护，小语料下 BM25 分数会塌）；「注入路径不注入内容」正是子任务隔离的另一半。

**⑥ MiniMax：装配指纹 + 防御性模块**
`pi-turn-runner/assembly-fingerprint.ts`：只对 `systemPrompt + tools` 做 SHA-256 指纹，**故意排除消息历史**（注释：「Message history is intentionally excluded so normal loop growth does not look like system/tool assembly churn」）——这是给 provider prompt cache 用的，区分「装配变了」和「对话变长了」。`turn.ts` 的 LLM hooks 全套（beforeLLM / onLLMPrepared / afterLLM / beforeTool / onToolExecutionStart / afterTool / onHistory / stepEnd），llm-stream-timeout / llm-retry / tool-context-size / request-image-limit 等防御模块齐全。
→ 落地：`run_manifest.json` 加 `assembly_fingerprint`（systemPrompt + tools），把「改了 prompt」从「多轮对话」里区分出来；我们的工具调用 hooks 粒度照 §16 的 trace_event 扩。

**⑦ ZCode：session-store 的迁移纪律**
`packages/adapters/src/storage/session-store/migrations/0020/0021/0022...` 编号迁移序列 + `migration-runner.ts`；`strict-tool-schema.ts` / `tool-call-validation.ts` / `tool-input-normalization.ts` / `streaming-tool-call-assembler.ts`——工具入参在适配层统一校验、规范化、流式重组。
→ 落地：我们的 `card` 表写入也要走编号迁移；工具入参在 driver 层校验，不信任模型给的参数。

### 17.3 源码对 §15（README 级结论）的修正

| §15 的说法 | 源码验证结果 |
|---|---|
| MiMo「persistent memory 深度理解项目」 | 证实但**去魅**：就是 md 文件 + SQLite FTS5 + fingerprint 增量，无向量库、无 LLM 摘要、无知识图谱。朴素，但增量/地板/门控三个工程细节做得极好 |
| Kimi「Subagents for focused, parallel work」 | 证实，且拿到完整机制：同 loop 多 profile、冷启动隔离、只回 final message；**新增三个 README 没说的例外**：fork 会继承主对话快照、resume 可跨会话重建、promptPrefix 注入 git 上下文 |
| ZCode「Wiki / Memory」 | CLI 侧只确认 `sessionInit` 派生 coder 写 `AGENTS.md`（与 Kimi 同款）；**Repo Wiki 的实现在 desktop 侧，本次 sparse 未拉取，未验证**——不下结论 |
| opencode 是共同祖先 | 证实（MiMo `@opencode/Memory`、ZCode `opencode-session.ts`）；opencode 自身 README raw 仍抓不到，但它的架构已通过两个派生品间接核实 |
| MiniMax「Lightweight 模式省略上下文块」 | 见 `pi-turn-runner` 的 hooks 与 turn 装配，机制存在；**分块注入的完整实现未逐文件核实**（子任务配额失败，覆盖不完整），§15.4 修订 1 的可行性由 kimi 的 `systemPromptVars()` 条件注入旁证 |

### 17.4 最终落地形态（吸收全部源码证据后定稿）

**Research Loop = 单引擎 + 多 profile（Kimi 形态），不是多套引擎：**

```
app/research/
  loop.py          # 唯一循环：LLM → tool calls → 结果回灌 → 重复熔断 → 终止判定
  profiles.py      # reader / verifier 两个 profile：tool 白名单 + role + 只读开关
  tools.py         # search_symbols / read_body / callers / callees / grep /
                   # read_lines / related_sections / expand_chain / check_fact
  evidence.py      # 断言表外置 state（Kimi goal 形态）：每轮注入，不靠对话历史
  dedupe.py        # 四级重复熔断：3 提醒 / 5 中等 / 8 强 / 12 强停，partialOutput≤32KB
  compact.py       # 逐字保问题 + 自足摘要 + 证据路径清单
  manifest.py      # run_manifest.json + assembly_fingerprint
  memory.py        # card 表写入：fingerprint 增量 + 相对分数地板 + 写入门控
```

关键决策（全部有源码依据）：
1. 子任务**冷启动**、禁 `fork`、返回值用 JSON 契约：`{conclusions, evidence:[{path,lines,quote}], open_assertions}`（kimi 是自由文本，我们必须强约束）。
2. reader profile 工具白名单**不含 Bash**（kimi 的 explore 留了 Bash，对我们是多余风险面）；profile 层过滤 + executor 层二次校验双保险。
3. 断言表外置到 state 并每轮注入（Kimi goal 形态），循环终止 = 断言全有证据 / 熔断 / 预算耗尽；`complete / blocked / paused` 三态 + 退出码。
4. 项目记忆写入走编号迁移 + fingerprint 增量 + 相对分数地板 + 写入门控（MiMo 三件套）；注入给 `card` 引用与路径，不给全文。
5. `AGENTS.md` 等知识库文档进上下文时带 `<!-- From: path -->` 来源标注 + 非特权声明（Kimi `project-information.md`），内容 hash 进 manifest。

**P0 工作量重估**：修 `chain.py` 四点 + `tools.py` + `probe 8/8` 仍为第一步；
P1 增加 `dedupe.py` 与 `evidence.py` 外置 state（比原计划多约 1 天，总工期 1.5~2 周）。

**验证题不变**：那道 Kafka 顺序题的 8 个代码事实 + 正确结论；
外加一个新验收：**跑两次同题，第二次必须命中 `card` 表里第一次沉淀的项目理解卡**（跨会话记忆生效的直接证据）。
