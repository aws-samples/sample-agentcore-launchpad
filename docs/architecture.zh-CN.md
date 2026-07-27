# 架构 / Architecture

AgentCore Launchpad 是覆盖在 Amazon Bedrock AgentCore 之上的一层轻量、有明确取舍
的平台。控制台中的每项能力都映射到一个真实的 AgentCore 服务和你账号里的真实资源
——平台的职责是为这些服务提供统一的 create → deploy → invoke → observe 体验,而
不是重新实现它们。

English: [architecture.md](architecture.md)

## 系统图

```
 Browser
 ┌─────────────────────────────┐        ┌──────────────────────────┐
 │ Platform console  :5173     │        │ Strands Studio UI  :5273 │
 │  Overview · Create · Chat   │        │  drag-and-drop canvas    │
 │  Registry · Governance ·    │        │  (方式C, vendored)       │
 │  Evaluation                 │        └────────────┬─────────────┘
 └──────────────┬──────────────┘            /api,/ws │  /launchpad-api
                │ /api  /v1                           │  (→ platform /api)
                ▼                                     ▼
 ┌─────────────────────────────┐        ┌──────────────────────────┐
 │ Platform backend  :8000     │◀───────│ Studio backend    :8100  │
 │  FastAPI                    │ deploy  │  FastAPI (local run,     │
 │  · deploy pipeline          │ via     │  chat, exec history)     │
 │  · invoke chain (/api,/v1)  │ pipeline└──────────────────────────┘
 │  · SQLite ledger (data/)    │
 └──────────────┬──────────────┘
                │ boto3 (bedrock-agentcore control + data planes)
                ▼
 ┌───────────────────────────────────────────────────────────────┐
 │ AWS · us-west-2                                                 │
 │  AgentCore: Runtime · Harness · Memory · Gateway · Identity ·   │
 │             Registry · Policy(Cedar) · Evaluation/Optimization  │
 │  Shared infra (CDK launchpad-base): S3 · ECR · CodeBuild ·      │
 │             Cognito · IAM exec role · HR Lambda · Facts API     │
 │  Observability: CloudWatch Transaction Search (aws/spans)       │
 └───────────────────────────────────────────────────────────────┘
```

## 四层映射(来自 prompt.md)

项目简报把 AgentCore 能力组织为四层;每一层在本仓库中都有真实、可运行的代码支撑。

| 层 | 平台入口 | AgentCore 服务 |
|---|---|---|
| **1. 构建核心(Build Core)** | Create Agent(方式A/B/C)、统一管道、Chat 记忆 | Runtime、Harness、Memory |
| **2. 构建工具(Build Tools)** | 工具目录、内置工具演示 | Gateway(REST + Lambda → MCP)、内置工具(Code Interpreter、Browser) |
| **3. 治理(Governance)** | Governance 页面、Registry 控制台、trace 面板 | Observability(Transaction Search)、Registry、Policy(Cedar) |
| **4. 评估与优化(Evaluation & Optimization)** | Evaluation 页面、Experiments(`?view=experiment` 子页:阶段流水线 + 判定语义化) | Evaluation(batch + online、LLM-judge、insights)、Optimization(config bundles、A/B、canary) |

## 平台 ↔ AgentCore 服务映射

| AgentCore 服务 | 平台如何使用 |
|---|---|
| **Runtime** | 托管 zip 与 container Agent(`CreateAgentRuntime`);调用链访问 runtime 数据面。 |
| **Harness** | 托管方式B Agent(`CreateHarness`)——托管入口,无构建产物。 |
| **Memory** | 一个共享的 `launchpad_memory` 单例:短期 session 事件 + 长期语义与用户偏好策略。命名空间只按 `{actorId}` 分区(没有 `{agentId}` 模板变量),因此平台把 Agent id 折进 actor——`scoped_actor(agent_id, human)` → `<agent>__<human>`——从而让**短期事件与长期记录**(`/facts/<agent>__<human>`)都按 Agent 分区。生成的 Strands Runtime 通过 `AgentCoreMemorySessionManager` 恢复短期对话。Claude Agent SDK 容器为每次调用创建独立的 `MemorySessionManager`,通过 `UserPromptSubmit` Hook 注入有界的短期对话及 `/facts/<actor>`、`/preferences/<actor>` 记录,并在调用成功后把 USER/ASSISTANT 对作为一个事件持久化。A2A Runtime 使用 `<agent>__a2a__<contextId>`,因为直接 A2A 调用目前没有经过身份认证的 human actor envelope。一个 Agent 学到的偏好不会串到同一个人的另一个 Agent 或 A2A context;台账仍存裸的 human actor 用于展示。 |
| **Gateway** | `launchpad-gw` 把一个 REST API(office-facts)和一个 Lambda(hr-database)转成带 Cognito-JWT 鉴权的 MCP 工具;Agent 的工具调用经由它流转。 |
| **Identity** | 支撑网关的 token vault——一个 OAuth2 provider(Agent 出站鉴权)和一个 API-key provider。 |
| **Registry** | `launchpad-registry` 编目三类 descriptor:A2A(Agent)、MCP(工具)、AGENT_SKILLS(Skill)。每次部署都会自动创建并提交一条 A2A 记录。控制台也支持手动注册——外部远程 MCP 服务器(streamable-http URL)与技能(SKILL.md → 制品桶)——并驱动完整生命周期:提交 → 批准/驳回(REJECTED 仍可改判批准)、下架(终态——已实测,之后只能删除)、删除。注册中心同时是**挂载目录**:`GET /api/registry/attachables` 只向创建向导提供 APPROVED 的 MCP/技能记录,MCP 记录按 URL 分流——共享网关 URL 挂为 `agentcore_gateway`(OAuth),其他 URL 挂为 `remote_mcp`(暂不带鉴权)——技能按其 s3 路径经 `skills[{path}]` 挂载。 |
| **Policy** | 以 `ENFORCE` 模式挂接到网关的 Cedar 策略引擎;deny 决策会带上作出判定的 policy id。支持 NL → Cedar 策略生成。 |
| **Evaluation** | 基于 CloudWatch trace 的真实 `StartBatchEvaluation` / insights。运行范围三选一:**数据集**(回放条目——多轮 scenario 在同一 session 内顺序回放)、显式 **session id 列表**、或**时间窗口**(`lookback_hours` 1–336——被动模式:不产生新调用,用 `filterConfig.timeRange` 圈定既有流量)。13 个通用内置评估器,外加 3 个仅限真值的 `Builtin.Trajectory*Match` 匹配器(仅当数据集 scenario 定义了 `expected_trajectory` 时可选),以及支持完整 CRUD 的自定义 LLM-as-a-judge 评估器——在 `?view=evaluators` 子页创建/编辑(UpdateEvaluator 为全量配置替换)。洞察运行可在三种分析类型(失败归因/用户意图/执行摘要)中任选子集。数据集以 devguide scenario 形式存于 SQLite(`?view=datasets` 子页:scenario 编辑器、JSON/JSONL 导入),一键单向同步为不可变的 AWS Dataset 资源(`AGENTCORE_EVALUATION_PREDEFINED_V1`);scenario 真值(断言/期望回复/期望轨迹)经 `evaluationMetadata.sessionMetadata` 注入批量评估。账户单批次锁与队列语义不变。 |
| **Optimization** | 推荐 → 配置捆绑(configuration bundles)→ 网关 A/B(config-bundle 50/50)→ target-based canary → verdict → promote → cleanup。 |
| **Observability** | CloudWatch Transaction Search(X-Ray trace segment destination → CloudWatch Logs)。trace 从 `aws/spans` 日志组读取,并渲染为按 session 的链路面板。 |
| **内置工具(Builtin Tools)** | Code Interpreter(`aws.codeinterpreter.v1`)与 Browser(`aws.browser.v1`)各有一个可运行的演示端点。 |

## 统一的五阶段部署管道

三种创建方式统一收敛到同一组有序阶段,定义在 `backend/app/deployer/pipeline.py`:

```
generate → package → provision → deploy → register
```

每种方式为每个阶段贡献一个可调用函数(或省略以跳过)。阶段进度持久化在
`Deployment` 行上,并作为 JSONL 事件镜像进 `Job` 日志,因此重启后的后端会从第一个
未成功的阶段继续(启动时执行 `resume_pending_jobs()`)。

| 阶段 | 方式B — harness | zip_runtime / 方式C — studio | 方式A — container |
|---|---|---|---|
| **generate** | 从 AgentSpec 构建 `CreateHarness` 请求 | 渲染 Strands 模板(studio:原样适配用户代码) | 组装 ARM64 构建上下文(Dockerfile + `main.py` + `.claude` 脚手架) |
| **package** | *跳过*(无产物) | `pip install` ARM64 wheels → zip → S3 | zip 上下文 → S3 → CodeBuild(docker build+push)→ ECR |
| **provision** | 复用共享执行角色 | 复用共享执行角色 | 复用共享执行角色 |
| **deploy** | `CreateHarness` + 轮询 READY | `CreateAgentRuntime` + 轮询 READY | `CreateAgentRuntime(containerConfiguration)` + 轮询 READY |
| **register** | A2A 注册记录,自动提交 | A2A 注册记录,自动提交 | A2A 注册记录,自动提交 |

典型耗时:harness ≈ 30 秒,zip ≈ 1–3 分钟(含 pip),container ≈ 2–4 分钟(实测:CodeBuild 1.7 分钟 + 数秒即 READY)
(经 CodeBuild)。见 [troubleshooting.zh-CN.md](troubleshooting.zh-CN.md)。

## 调用链

Chat 交互页面(`/api/chat/{id}`)与公开 API(`/v1/agents/{id}/invoke` +
`/invoke-stream`)共享**同一个**入口 `app.services.invoke.invoke_agent_text`
(SSE 走 `app.services.chat.chat_stream`),因此两个入口行为完全一致:

```
console /api  ─┐
               ├─▶ invoke_agent_text / chat_stream
public  /v1  ──┘        │
                        ├─ 方式分派:
                        │    harness            → harness data client
                        │    zip/studio/container → runtime data client
                        ▼
             AgentCore Runtime / Harness
                        │  (session 隔离、流式)
                        ├─ Memory        (session 上下文读写)
                        ├─ Gateway tools (基于 Cognito JWT 的 MCP)
                        ├─ Policy        (网关处的 Cedar ENFORCE)
                        └─ Observability (spans → CloudWatch Transaction Search)
```

公开 `/v1` 接口额外加了 `X-Api-Key` 鉴权(密钥以 sha256 哈希存储);分派之后的
一切与控制台路径完全相同。

## 控制台认证与账户

控制台有一个可选的本地账户网关,与 Gateway/Cedar 演示使用的 Cognito 用户以及
`/v1` 的 API-Key 面完全独立;设置 `LAUNCHPAD_AUTH_PASSWORD` 即启用,不涉及任何
AWS 调用。

一个会话 Cookie 背后有两类凭证来源:

- **内置 admin**:来自配置(`LAUNCHPAD_AUTH_USERNAME`,默认 `admin`),没有台账
  行,因此任何数据问题都无法把控制台锁死;该用户名对注册保留;
- **注册账户**:`users` 表中的行,由自助注册创建(`POST /api/auth/register`:
  用户名 + 公司邮箱 + 密码),`role=member`。默认落到 `status=pending`、没有有效期,
  也无法登录(`401 auth.account_pending`);管理员审批通过(`PATCH /api/users/{id}`
  带 `status=active`)后才开始计算 `LAUNCHPAD_AUTH_REGISTRATION_VALID_DAYS`
  (默认 7 天)的有效期。设 `LAUNCHPAD_AUTH_REGISTRATION_REQUIRE_APPROVAL=false`
  可恢复"注册即生效"。密码以 `pbkdf2_sha256`
  加每用户盐存储——仅用标准库,不引入 passlib/bcrypt。"公司邮箱"通过可配置的
  免费/临时邮箱黑名单强制执行,白名单非空时优先生效。

`POST /api/auth/login` 校验任一来源并签发 HMAC 签名的 HttpOnly Cookie,负载为
`version:subject:expiry`——12 小时,且不超过账户自身的 `expires_at`。**角色不放进
Cookie**:授权在每次请求时解析(配置的 admin → `admin`,其余以 `users` 行为准),
因此禁用、降权或到期在下一个请求即生效,无需等 Cookie 过期。Cookie 其余部分是无
状态的,可跨后端重启;修改内置 admin 凭证会使**所有**会话失效,因为签名密钥由其
派生。

启用后,中间件保护所有 `/api/*` 路由(含 API 文档),仅放行 `/api/health`、
`/api/auth/status`、`/api/auth/login`、`/api/auth/register`;中间件不管 `/v1/*`,
其 `X-Api-Key` 契约保持权威。

admin 另外拥有**用户管理**模块(`/users`),对应 `GET /api/users`、
`GET /api/users/stats`、`PATCH /api/users/{id}`、`DELETE /api/users/{id}`——
审批队列(待审批筛选与计数、通过/拒绝)、列表/搜索/筛选、注册统计、延期、
禁用/启用、修改角色、一次性重置密码、删除。member
会话在这四个路由上得到 `403 auth.forbidden`,页面渲染无权限状态而不是表格。数据本身
**不**按用户隔离:所有已登录账户看到同一批 agent、知识库与链路,只有该模块按角色
限制。

默认 Cookie 适用于本地 HTTP 栈;HTTPS 部署必须设置
`LAUNCHPAD_AUTH_COOKIE_SECURE=true`。不设置密码则整体关闭网关(控制台开放、注册
返回 `auth.registration_disabled`、`/api/users*` 以隐式本地 admin 身份可达),保持
免引导的本地开发与测试流程。

## 记忆控制台(控制台 05)

`/memory` 是共享 `launchpad_memory` 单例的**只读**视图
(`backend/app/services/memory_console.py`,接口位于 `/api/memory/*`)。它与
`app/services/memory.py` 刻意分离:后者位于聊天调用热路径上、保持精简;控制台
模块负责控制面读取、actor 解码、命名空间解析与分页,并从 `memory.py` 导入
`SCOPE_SEP` / `memory_id_or_none`,使分区契约只有一个来源。

只读是结构性的,而非界面层的拦截:两个文件中都不存在 `CreateEvent`、
`DeleteEvent`、`DeleteMemoryRecord`、`Batch*MemoryRecords`、
`StartMemoryExtractionJob`、`CreateMemory`、`UpdateMemory`、`DeleteMemory` 的
封装或处理函数,`tests/test_memory_console.py` 会断言这一点。

| `?view=` | 展示内容 | AgentCore 操作 |
|---|---|---|
| `overview` | 资源配置(id/arn/状态/事件过期/KMS/执行角色)、每条长期策略及其 `namespaces` + `namespaceTemplates`、以及账号内其他记忆资源(标出平台单例) | `GetMemory`、`ListMemories`、`ListActors` |
| `short-term` | actor → session → event 三级下钻;事件以时间轴呈现对话轮次的角色/文本,blob 载荷只显示字节数 | `ListActors`、`ListSessions`、`ListEvents` |
| `long-term` | 解析出的命名空间下的记录,以及带相关度评分的语义检索 | `ListMemoryRecords`、`RetrieveMemoryRecords` |

**抽取不作为控制台视图**。把短期事件变成长期记录是 AgentCore Memory 服务**自己**按资源上
配置的策略异步跑的任务,平台从不触发。`ListMemoryExtractionJobs` 也不是任务历史:它的
`status` 枚举只有一个值(`FAILED`),因此列出的只是 `StartMemoryExtractionJob` 会去重试的
失败积压,健康资源返回空列表。把它做成一个标签页会被读成「什么都没抽取出来」,所以控制台
已移除该视图;`GET /api/memory/extraction-jobs` 仍保留用于排查。

两处投影承担了主要工作。**actor 解码:** AWS 返回的是 `scoped_actor` 构造的复合
`<agent_id>__<human>`,因此 `/actors` 按首个 `__` 拆分,并每页一次批量查询台账
解析 Agent 名称;若 Agent 行已删除,该 actor 仍为 `scoped: true` 但名称为
null —— 因为记忆分区的生命周期长于 Agent。**命名空间解析:**
`ListMemoryRecords`/`RetrieveMemoryRecords` 都要求具体命名空间,所以
`/namespaces` 在服务端把 `{actorId}` 代入每条策略模板,并将仍残留占位符
(如 `{sessionId}`)的模板标记为 `resolvable: false`,而不是把无效命名空间发给
AWS。

记录载荷的形状取决于策略:`SEMANTIC` 在 `content.text` 里存纯文本,而
`USER_PREFERENCE`/`SUMMARIZATION` 存的是 JSON 对象
(`{context, preference, categories}`)。`memory.decode_record_text`(控制台与
Chat 右栏共用)提取可读文本、以 `structured` 暴露解析后的对象、并在 `raw_text`
中保留原始载荷,因此两个界面都不会渲染出一坨序列化对象。

Chat Playground 的「会话记忆」右栏通过 `OPEN IN MEMORY ↗` 深链进入本页
(`/memory?view=short-term&actor=…&session=…`),与它的
`OPEN IN OBSERVABILITY ↗` 对称。`GET /api/chat/{agent_id}/memory` 会回显它实际
读取的复合 `actor_id`,链接直接使用该值:会话记录的 actor 可能与请求 actor 不同,
若在前端自行推导分区,链接会指向一个并不存在的分区。

这里没有 TTL 缓存 —— 与按扫描量计费、耗时数秒的可观测 Logs Insights 查询不同,
`GetMemory` 只是一次快速的控制面读取。所有列表接口都双向传递 `next_token`
(AWS 每页上限 100),概览的 actor 计数只统计一页并显式给出
`actor_count_truncated` 标志,而不是给一个静默错误的总数。在执行
`make bootstrap` 之前,`/overview` 返回 `configured: false`(页面统一渲染的软
状态),其余接口返回 `memory.not_configured`(409);botocore 失败映射为
`memory.unavailable`(502)。

## 可观测模块(控制台 06)

`/observability` 是一个只读的遥测控制台,数据来自三个来源
(`backend/app/services/observability.py`,接口位于 `/api/observability/*`):

| 来源 | 用途 | 方式 |
|---|---|---|
| `aws/spans` 日志组(CloudWatch Transaction Search) | 追踪/会话列表、仪表盘计数 + p50/p95 + 分时序列、热门工具、Span 树 | Logs Insights `start_query`,每个视图一组有界查询 |
| `bedrock-agentcore` 指标命名空间 | 各模型 TOKEN 用量卡片与图表 | `ListMetrics`(发现维度)→ `GetMetricData` 对 `gen_ai.client.token.usage` 求和 |
| AgentCore Memory `ListEvents` + ChatMessage 台账 | 会话对话转录 | 通过 ChatSession 联结(`session_id → actor_id`);优先读取 Memory,并用精确渲染消息台账修复延迟、不完整或历史 actor 分区漂移;解码 harness 消息信封并丢弃工具结果轮次 |

每个视图都由 **60 秒 TTL 缓存**(按视图 + 时间范围)提供服务 —— Logs Insights
按扫描量计费 —— `force=true`(⟳ 刷新按钮)可绕过缓存。时间范围为白名单
(`1h/6h/24h/7d`);trace id(`^[0-9a-f]{32}$`)与 session id
(`^[A-Za-z0-9_-]{8,128}$`)在路由层校验,并在查询构造器中**再次校验**后才会
插入 Logs Insights 查询字符串。TOKEN 求和只统计终端 LLM 操作
(`chat` / `text_completion` / `generate_content`),因为 agent 级
`invoke_agent` Span 会重复其子 Span 的 `gen_ai.usage.*` 值。

成本为**参考估算**:token 数 × `config/launchpad.yaml` 中的 `model_prices`
(每百万 token 的美元价,按子串匹配 `gen_ai.request.model`;未知模型只显示
token 数,成本为 `—`)。界面以 `≈ / EST` 标注。价格表通过 litellm 的公开
价格文件保持更新(`app/services/model_prices.py`):每日守护线程 + 仪表盘的
「⟳ 更新价格」按钮(`POST /api/observability/prices/refresh`)会为账户遥测中
出现过的每个模型拉取精确条目(含 Bedrock 区域溢价与缓存读写价),刷新运维
维护的短键,未匹配的键保持不动。来源 URL 与周期可配置
(`model_prices_source_url`、`model_prices_refresh_hours`,设 `0` 关闭守护线程)。

**各创建方式的遥测:** Strands(zip/studio)与 harness Agent 原生发射 gen_ai
span。Claude Agent SDK 容器把 `claude` CLI 当子进程驱动——ADOT 自动插桩看不见
——因此生成的 Agent 手工发射遥测(`app/templates/claude_sdk_agent/tracing.py`,
移植自 agentxray demo-agent):一个 `invoke_agent` 根 span、每次工具调用一个
`execute_tool` span、一个携带整次查询 token 用量的聚合 `chat` span
(`ResultMessage.usage`;SDK 的 `cache_creation` 映射为 `cache_write`),以及
供 Span 抽屉展示输入/输出消息的 Strands 形状内容 event。scope 名必须保持
`strands.telemetry.tracer` —— AgentCore 只解析受支持插桩库的 span/event。

页签结构:**仪表盘**(5 个统计卡片 + 流量/延迟/TOKEN/工具图表)·
**会话**(列表 → 含记忆转录与会话内追踪卡片的详情)·
**追踪**(可筛选列表 → 瀑布甘特图 + Span 抽屉:含缓存读写的 token 用量、
预估成本、工具 schema、原始属性)。交叉链接:深链
`/observability?trace=<id>` / `?session=<id>`;Chat 的追踪面板可跳到当前
会话详情(`在可观测中打开 ↗`),会话详情也可跳回(`在对话演练场打开 ↗`);
`service.name` 通过台账映射为平台 Agent 名称(`resource_id` 基名匹配,
回退为原始名称)。

## SQLite 台账与 job/event 模型

廉价且本地的状态存放在 `data/launchpad.db` 的 SQLite 台账中
(`backend/app/models/ledger.py` 加评估/优化模型):

| 表 | 内容 |
|---|---|
| `agents` | Agent 记录——name、method、status、ARN、resource id、registry record id、version、spec |
| `deployments` | 每次部署一行——五阶段数组,含各阶段 status/detail/时间戳 |
| `jobs` | 异步工作(type `deploy_agent`)——status + 阶段事件的 JSONL `log` |
| `chat_sessions` | Chat 交互 session——轮次、actor、最近活跃时间 |
| `users` | 注册创建的控制台账户——用户名/邮箱、pbkdf2 密码哈希、角色、状态(`pending`/`active`/`disabled`)、`expires_at`(审批前为空)、最近登录与登录次数(内置 admin 仅来自配置,不入表) |
| `api_keys` | 公开 API 密钥——sha256 哈希 + 前缀(从不存明文) |
| `policy_decisions` | 治理决策日志——principal、tool、ALLOW/DENY、原因 |
| `eval_datasets` / `eval_runs` | 评估数据集(legacy prompt 或 devguide scenario + 描述 + 最近一次 AWS 同步信息)与运行状态(分数或 insight 树;窗口运行以 `dataset_name="window:<N>h"` 编码范围) |
| `experiments` | 优化闭环——阶段 + 各阶段产物,可恢复 |

**Job/event 模型。** 创建 Agent 返回 `202` 并带一个 `job_id`。部署 job 在后台线程
运行,每次阶段切换向 `Job.log` 追加一条 JSONL 事件;`GET /api/jobs/{id}` 返回这些
事件,`GET /api/agents/{id}` 返回 `Deployment.stages` 数组。随 job 完成,Agent 从
`deploying → active`(或 `failed`)。权威的资源状态(runtime 状态、注册记录状态、
评估/trace 数据)始终存放在 AWS;台账只保存标识符与派生的进度。

## 本地进程拓扑

`./start.py` 启动平台的两个后台进程,等待全部 HTTP 健康检查通过,并把进程归属
信息和日志写入 `.run/`。`./stop.sh` 只会优雅停止这些已记录的进程组。默认模式
使用开发服务器;`./start.py --prod` 会构建平台前端,提供生产构建预览,并关闭后端
自动重载。`bash scripts/dev.sh`(`make dev`)仍是绑定当前终端的前台运行方式。

| 服务 | 端口 | 覆盖变量 |
|---|---|---|
| platform backend | 8000 | `PLATFORM_API_PORT` |
| platform frontend | 5173 | `PLATFORM_UI_PORT` |

生命周期脚本会在配置端口已被占用时立即失败。开发模式默认仅绑定 loopback;
生产模式把 UI 与 API 服务都绑定到 `0.0.0.0`。可通过 `LAUNCHPAD_HOST` 和
`LAUNCHPAD_API_HOST` 覆盖绑定地址。

根目录生命周期不再启动 `apps/studio/` 下的独立应用。平台控制台在
`/create/studio` 提供受支持的原生画布。见 [studio-integration.md](studio-integration.md)。
