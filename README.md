# codelib — 代码问答机器人（极简版）

把代码目录接成可问答的机器人：内置**网页对话界面**，支持多个知识库、管理员与普通账号；也支持**企业微信 / 飞书**接入与普通 HTTP 调用。内置**增量索引、变更感知、长期记忆、省 token 检索**，对目标工程**只读**。

设计原则：**配置少、接口少**。大模型只配一个；日常管理走对话命令。

- Python 3.10+ / FastAPI
- 没有大模型也能用：未配置时自动降级为「检索摘要」作答

---

## 1. Web 工作台（内网 · 多库 · 只读）

浏览器打开 `http://<内网IP>:8000/`：

- 左侧**知识库列表**：显示每个库是否已建库、块数/符号数；点选即在该库下新建对话；
- **每个对话固定绑定一个库**，提问只在该库内检索，不会串库；
- AI 回答支持 Markdown 标题、列表、表格、引用和代码块；解析器随服务本地提供，工作台不依赖外网加载；
- **历史对话**按时间列出，可回看、继续追问；
- 页面顶部与侧栏标注「🔒 只读模式」：**服务没有任何修改工程文件的接口**。

### 只读是怎么保证的（三重）

1. **代码层**：本服务只有“读”能力（tree-sitter 解析、FTS5 检索、LLM 生成），无写文件/改代码/执行命令的工具；
2. **接口层**：`app.read_only: true` 时，`/api/write`、`/api/edit`、`/api/apply`、`/api/git`、`/fs/*` 等路径被中间件统一 **403**；
3. **提示层**：系统提示要求模型只给解释/结论/方案（可描述改哪里、如何验证），不产出可执行补丁、不声称已改文件。

> 更进一步：可把 `workspace.path` 以**只读方式**挂载/授权（如 NTFS 只给读取权限），双保险。

### 内网部署（Windows 单服务）

```bat
:: 1) 安装依赖（一次）
python -m pip install -r requirements.txt -r requirements-knowledge.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

:: 2) 为每个库建索引（首次/增量）
python -m app kb-build-all
:: 或单独：python -m app kb-build --workspace gateway --full

:: 3) 启动服务（前台）
python -m app serve --port 8000
:: 启动时会打印：本机访问 http://127.0.0.1:8000/  内网访问 http://<内网IP>:8000/
```

内网其它电脑访问前，放行端口（管理员 PowerShell，只需一次）：

```powershell
New-NetFirewallRule -DisplayName "codelib 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

如需开机自启，可用 NSSM 或「任务计划程序」把 `python -m app --config config/config.yaml serve --port 8000` 注册为服务。

Windows 也可以双击仓库根目录的 `start.bat`，或在 PowerShell 运行 `./start.ps1`。首次启动且账号库为空时，脚本默认创建用户名 `admin`，并安全提示输入至少 8 位的管理员密码；密码只写入 SQLite 的哈希，不保存在脚本中。已有账号后会直接启动，不再提示或覆盖密码。需要自定义初始用户名时，启动前设置环境变量 `CODELIB_ADMIN_USERNAME`。

### 知识库与对话管理

管理员登录工作台，打开「账号/知识库管理」即可：

- 查看每个库的路径、来源、索引版本、文件/文档块/符号/图边统计；
- 创建库，编辑名称、目录与说明，执行增量或全量重建；
- 删除库及其授权、对话、记忆、留痕和索引，但不会删除源工程文件；
- 给普通账号分配库权限，并查看、打开、重命名或删除对话。

配置库与动态库都可管理。对配置库的修改和删除会作为覆盖记录持久化到 SQLite，重启后仍然生效；删除后可重新创建同 ID 的库。新建库不必改配置文件。

命令行建库仍可用于配置文件里已定义的库：

```bash
python -m app kb-build --workspace <新id>
```

刷新网页，左侧就会出现新库。**每个库对应一个工程目录，彼此隔离。**

### HTTP 接口

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/` | Web 工作台 |
| GET | `/healthz` | 健康检查（只读状态与知识库数量） |
| POST | `/api/auth/login` | 用户名/密码登录，建立 HttpOnly 会话 |
| POST | `/api/auth/logout` | 注销当前会话 |
| GET | `/api/auth/me` | 当前登录账号 |
| GET | `/api/workspaces` | 当前账号可访问的库列表 |
| GET | `/api/admin/workspaces` | 管理员查看全部知识库 |
| POST | `/api/admin/workspaces` | 管理员创建并索引知识库 `{name,id?,path,description?}` |
| PUT | `/api/admin/workspaces/{id}` | 编辑名称、目录和说明；目录变化时自动全量重建 |
| POST | `/api/admin/workspaces/{id}/rebuild` | 增量/全量重建 `{full}` |
| DELETE | `/api/admin/workspaces/{id}` | 删除库及其授权、对话、记忆、留痕和索引，不删源工程 |
| GET/POST | `/api/admin/users` | 管理员查看/创建账号 |
| PUT | `/api/admin/users/{id}/workspaces` | 管理员设置普通账号可访问的知识库 |
| PUT | `/api/admin/users/{id}/password` | 管理员重置账号密码并注销旧会话 |
| PATCH | `/api/admin/users/{id}/active` | 管理员启用/停用账号 |
| GET | `/api/knowledge/status` | 管理员查看各库知识库统计 |
| POST | `/api/conversations` | 新建对话 `{workspace}` |
| GET | `/api/conversations` | 历史对话列表 |
| GET | `/api/conversations/{id}` | 某对话全部消息 |
| POST | `/api/conversations/{id}/messages` | 在该对话提问 `{content}` |
| PATCH | `/api/conversations/{id}` | 重命名对话 `{title}` |
| DELETE | `/api/conversations/{id}` | 删除对话 |
| POST | `/ask` | 登录后单次问答（供程序调用） |
| GET/POST | `/channels/wecom/callback` | 企业微信 |
| POST | `/channels/feishu/event` | 飞书 |

### 怎么用

1. 在 `config/config.yaml` 配好 `llm`；Windows 首次启动可运行 `start.bat`，其他环境则设置 `CODELIB_ADMIN_USERNAME` 和强密码 `CODELIB_ADMIN_PASSWORD`；
2. 用 bootstrap 账号登录工作台；
3. 管理员创建知识库、创建普通账号并分配知识库，普通账号登录后只能问答已授权库。

```bash
curl -s -c cookies.txt http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"你的管理员密码\"}"

curl -s -b cookies.txt http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d "{\"question\":\"这个工程是做什么的？\"}"
```

返回很干净，只有答案和来源：

```json
{
  "answer": "tdx_msg_bus_svr 是统一消息网关，负责屏蔽 QD/IDeal 渠道差异，向上游提供统一的消息发送、查询与上行事件能力。",
  "sources": ["docs/01-业务与架构/消息网关-业务概览.md:1-4", "docs/01-业务与架构/消息网关-业务概览.md:20-28"]
}
```

- `workspace` 可省略（单目录时）；传 `id` / `name` / 目录名都能匹配。
- 想看引用评分、记忆命中、token 用量等诊断信息，请求体加 `"debug": true`，诊断信息在 `debug` 字段里。
- 微信 / 飞书里直接用自然语言发消息即可，不用关心这些字段。
- 默认正文以自然语言结论为主，不堆砌路径、行号和来源清单；网页中的参考资料默认折叠，渠道不再额外追加来源。
- 需要定位时可说「给出行号来源」，正文最多保留少量关键引用；说「不要代码、函数名和字段名」时会改用纯自然语言，并在当前对话的后续追问中延续该偏好。

> 程序调用可复用登录 cookie，或通过 `Authorization: Bearer <session-token>` 传递 API 会话。密码只保存 PBKDF2 哈希；初始管理员只在数据库首次创建时从环境变量引导。普通账号只能看到和提问管理员已分配的知识库，并且只能查看自己的对话；停用或重置密码会立即注销该账号现有会话。

### 代码知识库（P0，已内置）

在原有“签名块”之外，新增 **代码知识工程层**（进程内、单 SQLite 文件、无容器/无本地模型）：

- 知识组织：文件级结构摘要 + 函数/类/方法精确行号；长函数与长文档自适应分段，避免只保留开头
- 代码：**tree-sitter** 抽符号 + 精确标识符邻接边（调用/引用仍为启发式）
- 文档：**markitdown** + python-docx/pptx/openpyxl + pypdf（PDF/Word/PPT/Excel/MD）
- 检索：**SQLite FTS5（BM25）+ 中英同义词 + 多子句覆盖融合**，可选 LLM 重排与最多三跳邻接扩展
- 生成：仍走你的 LLM API（现有 `3425` 网关）

```bash
python -m pip install -r requirements-knowledge.txt

python -m app kb-build --workspace gateway        # 首次/增量建库（自动跳过未变文件）
python -m app kb-build --workspace gateway --full # 全量重建
python -m app kb-stats                            # 看统计
```

建库后 `bot` 会**自动优先使用知识库**（库为空时自动回退旧检索），无需改配置。默认启用 LLM 重排；复杂问题会生成结构化子查询，分析类问题按缺口补检索、修订并复核关键断言的证据。`!deep` 至少扩展两跳并允许最多三轮修订，因此会增加模型调用和响应时间。

> 建库格式已升级：旧库下次执行 `kb-build` 会自动全量重建，无需手工删库。当前检索仍是词法检索与同义词扩展，尚未实现向量索引；`knowledge.embedding.mode` 目前不会启用混合检索。

---

### 全链路留痕与过程评估（溯源）

**每一次问答都会自动留痕**，完整记录 7 个阶段：

| 阶段 | 记录内容 |
|------|----------|
| `sanitized` | 用户原始提问（原话 + 清洗后） |
| `intent` | 多轮理解/改写结果、历史轮数、改写方式（llm/heuristic） |
| `index` | 索引/变更检测情况 |
| `retrieve` | 检索来源、候选与选中片段（path:line）、上下文 token、预算 |
| `evidence` | 中英文主题匹配的命中词、缺失词及作为范围忽略的工程名 |
| `prompt` | **实际发给大模型的协议内容**（system + user 全文、各自 token 数） |
| `llm` | 模型原始输出、finish_reason、usage（prompt/completion tokens）、耗时 |
| `quality_review` | 分析类问题的覆盖检查结果；有明确缺口时标记是否已修订 |
| `answer` | 最终答案 + 引用来源 |

**过程评估**（对任意一次留痕，一键评估）：判断请求与回答是否合理，并给出 harness 改进建议。

- **规则检查**（确定性、零成本）：检索是否命中、上下文是否接近预算被截断、模型是否失败/降级、回答是否为空/过短、是否带引用、**引用是否可追溯**（回答里提到的文件是否真在检索结果中）、提示词是否完整；
- **模型评审**（1 次调用）：忠实度/相关性/完整性/是否跑题 + 0-100 分；
- **harness 可提升点**：如“某片段被截断应扩大行数”“config 包未纳入检索”“应召回某文件”“建议开启重排/提高 top_k”，可落地逐条给出。

网页上点右上「溯源/评估」即可查看任意一次调用的全链路，并点「评估当前」生成报告。接口：

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/traces?workspace=&limit=` | 留痕列表（含最近评估结论） |
| GET | `/api/traces/{trace_id}` | 单次留痕全貌（含所有阶段与历次评估） |
| POST | `/api/traces/{trace_id}/evaluate` | 运行评估（规则+模型），结果落库 |
| GET | `/api/traces/{trace_id}/audits` | 历次评估记录 |

> 留痕与评估都存本地 SQLite（`trace` / `trace_event` / `audit` 表），不依赖外部服务。

### 评估闭环：结论自动沉淀为“召回提示”

评估里的“应召回某文件/符号/目录”不会说完就丢——点「评估当前」时（接口 `?learn=true` 默认开启），系统会自动：

1. 从评估报告里抽取**缺失类**引用（只认“缺少/应召回/建议补充/未纳入”等语义，无关项不入）；
2. 写入该库的 `boost` 表（`kind` = path / dir / symbol，权重默认 ×1.5，重复累加，上限 ×5）；
3. **下次检索直接生效**：命中 boost 的文件/符号候选被加权置顶。

效果实测（你的 `tdx_msg_bus_svr`）：评估后学到 `config/kafka_config.go` 与 `config/`，
原本这条根本没被召回，再检索时直接进入 **第 2 位、命中 6 条加权**。

管理接口：

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/boosts?workspace=` | 查看当前库的召回提示与权重 |
| DELETE | `/api/boosts?kind=&pattern=&workspace=` | 删除某条（避免误加权） |

> 闭环是**可控可逆**的：规则可见、可删；也可在评估接口传 `learn=false` 只评估不学习。

### 回答质量的三道闸（不靠调权重）

调权重只能改排序；下面三项解决“候选本身不足 / 答案本身不可靠”：

1. **证据门槛（不硬答）**：同时检查中文主题和代码标识符，工程名只作为检索范围；用户明确指定的函数名仍需命中。词面覆盖低于阈值（默认 `0.2`）时，**不调用模型**，
   直接如实说明“现有知识库中没有能直接回答的内容”，并给出相近文件与下一步建议。
   避免用“词像但无关”的片段编出一个看似合理的答案。
2. **同问题缓存（省成本）**：历史问过且**答案有效**的问题，直接复用上次答案，不再调模型；
   **负结果（没找到/证据不足/降级）不缓存**，不会被错误固化。
3. **评估区分缺证据和误拦**：门槛与评估共享匹配算法，旧 trace 重评会检查是否已有相关片段却仍被拒答；拒答模板不计入事实性回答的标识符忠实度。覆盖不足或误拦时分数上限为 55，误拦标记为 poor。词面命中仅用于排除明显无关内容，不表示证据完整。
4. **控制反馈学习**：门槛、提示词和扩库建议不会自动生成加权；不从评审理由中学习权重，不重复推荐已召回文件，也不因推荐某个文件就为其父目录加权。

> 配置：`knowledge.retrieve.min_coverage`（默认 0.2，越大越保守）。

---

## 2. 配置（一个目录 + 一个大模型）

`config/config.yaml`：

```yaml
app:
  host: 0.0.0.0
  port: 8000
  data_dir: ./data

auth:
  bootstrap_admin:
    username: ${CODELIB_ADMIN_USERNAME:-}
    password: ${CODELIB_ADMIN_PASSWORD:-}

workspace:                       # ← 可选的初始知识库（只读）；后续可由管理员添加更多库
  path: ./sample_workspace       #   改成你的目录，如 D:/tdx/zq/tx_msg_svr
  name: 我的工程
  auto_reindex_on_query: true    # 提问时自动感知变更并增量重建
  watch_interval_seconds: 0      # >0 则后台定时巡检（秒）

llm:                             # ← 只配一个大模型；留空 api_key 即降级
  type: openai_compatible        # openai_compatible | azure_openai | anthropic | gemini
  base_url: https://api.deepseek.com/v1
  api_key: ${DEEPSEEK_API_KEY:-}
  model: deepseek-chat
  temperature: 0.2
  max_tokens: 900
  timeout: 60

memory:
  enabled: true

knowledge:
  retrieve:
    top_k: 40                    # 全局候选数
    budget_tokens: 4000          # 全工作区共用的上下文预算
  rerank:
    mode: llm
    top_n: 10
  answer_review: true            # 分析类问题检查覆盖度，必要时修订

channels:
  wecom:  { enabled: false, corp_id: "", agent_id: "", secret: "", token: "", encoding_aes_key: "", webhook_url: "" }
  feishu: { enabled: false, app_id: "", app_secret: "", verification_token: "", encrypt_key: "", webhook_url: "" }
```

大模型 `type` 支持的四种协议（同一个配置，只改 `type`/`base_url`/`model`）：

| type | 适配对象 | 需要的字段 |
|------|----------|-----------|
| `openai_compatible` | OpenAI、DeepSeek、通义、Kimi、智谱、Ollama、vLLM、one-api 等 | `base_url` `api_key` `model` |
| `azure_openai` | Azure OpenAI | `base_url` `deployment` `api_version` `api_key` |
| `anthropic` | Claude | `base_url`(默认官方) `api_key` `model` |
| `gemini` | Google Gemini | `api_key` `model` |

`api_key`、`base_url`、`model` 都为空 → 视为未配置大模型，自动用「检索摘要」作答。密钥建议用环境变量（见 `.env.example`）。

> **推理型模型**（如 hy3、GLM 等）：建议 `max_tokens` 设 ≥ 2048。若首轮响应 `finish_reason=length` 且 `content` 为空（推理过程占满了额度），服务会自动放大到 4096 重试一次，并在进程内记住该额度，后续不再重试。
> `workspace.id` 可选（缺省取目录名）；`/ask` 的 `workspace` 字段可省略（单目录时），传 `id`、`name` 或目录名都能匹配。

---

## 3. 快速开始

```bash
python -m pip install -r requirements.txt          # 国内可加 -i https://pypi.tuna.tsinghua.edu.cn/simple

python -m app --config config/config.yaml index    # 建索引
python -m app --config config/config.yaml ask "消息网关如何路由消息？"   # 命令行提问
python -m app --config config/config.yaml serve --port 8000           # 启动服务
```

启动后打开 `http://127.0.0.1:8000/docs` 调试。

**换成你的工程**：把 `config/config.yaml` 的 `workspace.path` 改成你的目录，重新 `index` 即可。

---

## 4. 接入企业微信（自建应用回调）

1. 管理后台创建自建应用，记录 `CorpID / AgentId / Secret`。
2. 「接收消息」设置 URL：`https://你的公网地址/channels/wecom/callback`，生成 `Token`、`EncodingAESKey`。
3. 把这些值填到 `channels.wecom`（或用 `WECOM_*` 环境变量）。
4. 「企业可信 IP」加入服务器出口 IP；服务需公网可达（云主机或内网穿透）。

配置了 `corp_id+secret+agent_id` 时优先**主动发消息**回复；否则返回**加密被动回复**（AES-256-CBC，遵循官方 `WXBizMsgCrypt`）。

## 5. 接入飞书

1. 开放平台创建企业自建应用，开启「机器人」。
2. 「事件订阅」URL：`https://你的公网地址/channels/feishu/event`，订阅 `im.message.receive_v1`。
3. 填 `app_id / app_secret`（如开启加密再填 `encrypt_key / verification_token`）。
4. 申请权限 `im:message`、`im:message:send_as_bot`，发布版本。

服务对 `url_verification` 直接返回 challenge；收到消息先立即回执 `{"code":0}`，再异步回复。

---

## 6. 对话命令（在微信/飞书/本地都可用）

| 命令 | 作用 |
|------|------|
| `/help` | 帮助 |
| `/status` | 索引与记忆状态、待处理变更 |
| `/index [full]` | 增量/全量重建索引 |
| `/changes` | 查看目录变更 |
| `/remember <文本>` | 写入一条长期记忆 |
| `/facts` | 列出已记忆事实 |

直接发问题即问答；以 `!deep` 开头可加深检索。

CLI 也提供同等能力：`index / ask / changes / status / remember / llm`（详见 `python -m app --help`）。

---

## 7. 省 token 的机制（含实测）

1. **索引不存全文**：只抽函数/类签名、文档字符串、Markdown 标题、关键注释，记录 `file:line` 指回源文件。
2. **按预算检索**：BM25 + 中英同义词排序，只把最相关的少量片段注入上下文；统一由 `knowledge.retrieve.budget_tokens` 控制上限，不再按工作区分别设置；仅对前几条回源读精确行。
3. **答案复用**：仅对足够完整、带来源且当前检索仍命中原来源的简单问题复用答案；复杂问题和降级摘要会重新回答。
4. **增量索引**：只处理变更文件（`mtime+size` 快速跳过）。

真实工程 `D:\tdx\zq\tx_msg_svr`（240 个 Go 文件）实测：

| 指标 | 数值 |
|------|------|
| 全量代码 token | 202,108 |
| 索引块 token | 29,263（约全量 **14.5%**，索引省 **85.5%**） |
| 单次问答上下文 | ~976 token（相对全量省 **约 99.5%**） |

> token 为内置估算，用于预算控制与量级参考。代码库越大，节省越明显。

## 8. 变更感知

- `mtime+size` 快速比对 → 疑似变更再 `sha256` 确认，避免误报；
- 三种触发：提问时自动（`auto_reindex_on_query`）、后台巡检（`watch_interval_seconds`）、手动 `/index` `/changes`；
- 变更记录写入 `change_events`。

## 9. 记忆

- 每次问答自动存 `qa_memory`（问题/答案/引用）；
- `/remember` 存 `facts`（用户确认的事实）；
- 召回时只注入最相关的少量记忆，并标注「与当前代码冲突时以代码为准」。

---

## 10. 目录结构

```
codelib/
├── app/
│   ├── main.py          # FastAPI（极简接口）
│   ├── bot.py           # 问答编排 + 命令
│   ├── indexer.py       # 增量索引 / 变更比对 / 多语言抽取
│   ├── retrieval.py     # BM25 + 同义词 + 预算打包 + 深读
│   ├── memory.py        # 长期记忆
│   ├── store.py         # SQLite 持久化
│   ├── llm.py           # 单个大模型客户端（4 种协议）
│   ├── config.py        # 配置加载
│   ├── watcher.py       # 后台巡检
│   ├── cli.py           # 命令行
│   ├── knowledge/       # 代码知识库胶水层（schema/build/query）
│   ├── web/index.html   # 网页对话界面
│   └── channels/        # wecom.py / feishu.py
├── config/config.yaml
├── sample_workspace/    # 演示工程（可删）
├── tests/               # smoke / api / web / llm / llm_live
├── scripts/eval_real.py # 真实目录只读评估
└── requirements.txt
```

## 11. 测试

```bash
python tests/smoke_test.py     # 索引→问答→记忆→变更→增量
python tests/api_test.py       # 单次问答 + 企业微信/飞书回调
python tests/web_test.py       # 网页会话：新建/连续提问/历史/删除
python tests/workbench_test.py # 工作台：多库隔离 + 只读守卫（写路径 403）
python tests/auth_test.py      # 账号角色、知识库授权、会话隔离、旧数据库迁移
python tests/trace_eval_test.py # 全链路留痕 + 规则/模型评估（假 LLM）
python tests/feedback_test.py  # 评估闭环：结论→boost 规则→检索加权命中
python tests/gate_test.py      # 证据门槛 / 同问题缓存 / 评估分流（不硬答、不固化负结果）
python tests/llm_test.py       # 大模型配置与请求构造（不联网）
python tests/llm_live_test.py  # 本地假大模型端到端 + 失败降级
python tests/core_quality_test.py # 长切片 / 文件摘要 / 子问题覆盖 / 多跳与断言接地
python tests/admin_workspace_test.py # 知识库生命周期 / 对话管理 / 重启持久化 / 越权拦截
python tests/presentation_test.py # 默认简洁回答 / 引用偏好 / 禁止代码细节 / 多轮偏好
```

以上均已通过。

## 12. 安全 / 限制

- 索引只读目标目录，不改源码；默认排除 `.git/node_modules/dist/target/logs` 与二进制大文件；
- 管理员通过环境变量引导创建；密码以 PBKDF2 哈希保存，普通账号按知识库授权；企业微信/飞书均做签名与可选解密；
- 检索为词法为主（内置中英同义词），跨语言概念检索可后续替换为向量检索；
- 单机 SQLite，适合单实例/中小团队；企业微信/飞书需公网回调地址。
