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
 │  Observability: CloudWatch Logs（旧版 + 按 Agent 统一）           │
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
| **Runtime** | 托管 zip 与 container Agent(`CreateAgentRuntime`);调用链访问 runtime 数据面。Agent 详情中的只读「版本与端点」面板通过 `GET /api/agents/{id}/versions` 读回 `ListAgentRuntimeVersions` + `ListAgentRuntimeEndpoints` 的全部分页,让操作者看到不可变版本列表、`DEFAULT` 端点以及任何固定在某版本上的命名端点。 |
| **Harness** | 托管方式B Agent(`CreateHarness`)——托管入口,无构建产物。同一「版本与端点」面板对 harness 类 Agent 读取 `ListHarnessVersions` + `ListHarnessEndpoints`。 |
| **Memory** | 一个共享的 `launchpad_memory` 单例:短期 session 事件 + 长期语义与用户偏好策略。命名空间只按 `{actorId}` 分区(没有 `{agentId}` 模板变量),因此平台把 Agent id 折进 actor——`scoped_actor(agent_id, human)` → `<agent>__<human>`——从而让**短期事件与长期记录**(`/facts/<agent>__<human>`)都按 Agent 分区。生成的 Strands Runtime 通过 `AgentCoreMemorySessionManager` 恢复短期对话。Claude Agent SDK 容器为每次调用创建独立的 `MemorySessionManager`,通过 `UserPromptSubmit` Hook 注入有界的短期对话及 `/facts/<actor>`、`/preferences/<actor>` 记录,并在调用成功后把 USER/ASSISTANT 对作为一个事件持久化。A2A Runtime 使用 `<agent>__a2a__<contextId>`,因为直接 A2A 调用目前没有经过身份认证的 human actor envelope。一个 Agent 学到的偏好不会串到同一个人的另一个 Agent 或 A2A context;台账仍存裸的 human actor 用于展示。 |
| **知识库（Knowledge Bases）** *（Bedrock，不属于 AgentCore）* | 托管 Bedrock 知识库（`type: MANAGED`——向量库、嵌入与重排都由服务负责）是接地层：`MANAGED_KNOWLEDGE_BASE_CONNECTOR` 类型的 S3 数据源、ingestion 作业，以及 `Retrieve` / `AgenticRetrieveStream` 检索。Agent 既可以经专用 MCP 网关 `launchpad-kb-gw` 挂载（托管 Harness），也可以通过烤进生成的 zip/container 代码里的 `kb_search` / `kb_deep_search` 工具挂载——详见「托管知识库」一节。 |
| **Gateway** | `launchpad-gw` 把一个 REST API(office-facts)和一个 Lambda(hr-database)转成带 Cognito-JWT 鉴权的 MCP 工具;Agent 的工具调用经由它流转。治理页为已纳管的 Gateway 管理 **Gateway 限流**（2026 年 8 月 GA）：`ListGatewayRateLimits` / `CreateGatewayRateLimit` / `UpdateGatewayRateLimit` / `DeleteGatewayRateLimit` 位于网关详情的「限流」面板之后，服务端校验并记入 `policy_changes`。 |
| **Identity** | 支撑网关的 token vault——一个 OAuth2 provider(Agent 出站鉴权)和一个 API-key provider。 |
| **Registry** | GA 的 `agent-registry` 服务托管 `launchpad-registry`，编目 A2A Agent、MCP 服务器与 AGENT_SKILLS。`services/agentcore/registry.py` 把 GA 的 `AGENT/MCP/SKILL` 与 `data/dataSchemaVersion` 模型翻译成稳定的 Launchpad descriptor 契约；其他 AgentCore 服务仍在 `bedrock-agentcore` 之下。GA 的唯一性约束是 `(name, recordVersion)`，因此新建记录使用带类型后缀的初始版本（`1.0.0-a2a`、`1.0.0-mcp`、`1.0.0-skill`），内容编辑会保留该后缀。Registry 可用时，每次部署都会自动创建并提交一条 A2A 记录。在 SCP/IAM 策略拒绝 Registry 初始化的账号中，bootstrap 会把该能力记为不可用，仅属于 Registry 的 API 返回 503，部署管道只跳过 register 阶段；Runtime/Harness 部署仍然可用。控制台也支持手动注册——外部远程 MCP 服务器(streamable-http URL)与技能(SKILL.md → 制品桶)——并驱动完整生命周期:提交 → 批准/驳回(REJECTED 仍可改判批准)、下架(终态——已实测,之后只能删除)、删除。注册中心同时是**挂载目录**:`GET /api/registry/attachables` 只向创建向导提供 APPROVED 的 MCP/技能记录,MCP 记录按 URL 分流——共享网关 URL 挂为 `agentcore_gateway`(OAuth),其他 URL 挂为 `remote_mcp`(暂不带鉴权)——技能按其 s3 路径经 `skills[{path}]` 挂载。治理页可以把一个既有的 AgentCore Gateway 导入为**一条** MCP 记录，其中包含 Gateway 端点与它完整的已发现工具目录；旧的按 target 逐条的记录会一直保留，直到该 Gateway 记录 APPROVED 之后被显式下架。Registry 的批准控制的是目录可见性，而不是 Gateway 授权。`GET /api/registry/attachables` 会把目录状态与 Harness 可挂载性分开报告，并在服务端解析 Gateway 鉴权方式。对于已部署 Launchpad A2A Agent 所拥有的 A2A 记录，Registry 抽屉的「实时名片」会读取运行时此刻实际提供的名片（`GET /api/registry/records/{id}/live-agent-card` → 以账本中的 `Agent.arn` 调用数据面 `GetAgentCard`，AWS 为此打开的会话随即结束），并与记录中存储的名片做对比；实时名片不落账本——AWS 始终是事实来源。两份名片的 `version` 都来自同一个平台常量——`services/agentcore/registry.py` 中的 `A2A_CARD_VERSION`：A2A 运行时模板在打包阶段把它传给 Strands `A2AServer(version=...)`，`build_a2a_card` 在注册阶段把它写进记录，因此两边从构造上就是一致的。它**不是** AgentCore 运行时版本（`Agent.version`，显示在「版本与端点」面板）——运行时版本由 Create/UpdateAgentRuntime 在模板渲染完成之后才分配，名片无法携带。在该常量出现之前发布的 A2A Agent 仍会提供 Strands 默认的 `0.0.1`，而记录里是 `1`；对比会持续标出差异，直到下一次重新发布（重新渲染 + 重新注册）让两边收敛。Registry 页面对同一个注册中心提供两种视图：**发布者列表**（`GET /api/registry/records` → 控制面 `ListRegistryRecords`，包含所有状态的全部记录）与**消费者视图**（`?view=discoverable`，`GET /api/registry/records/discoverable` → 数据面 `ListDiscoverableRegistryRecords`，按 `nextToken` 翻页到底）——即拥有数据面访问权限的消费者或 Agent 实际能发现的记录。发现摘要不含 `descriptors`，点开某一行才读取完整记录。在同一页面会话中拉取过消费者视图后，凡不在其中的控制面记录都会打上「不可发现」标签（DRAFT / PENDING_APPROVAL / REJECTED / DEPRECATED 是预期情形）——两份列表的差异才是这项功能的意义。 |
| **Policy** | 挂接到网关的 Cedar 策略引擎,初始挂载模式由操作员选择(默认 `ENFORCE`,可选 `LOG_ONLY`);deny 决策会带上作出判定的 policy id。支持 NL → Cedar 策略生成。引用已被删除的引擎时,治理页面显式展示失效引用而不是报错,策略变更返回 409,创建并挂载会替换该引用。 |
| **Evaluation** | 基于 CloudWatch trace 的真实 `StartBatchEvaluation` / insights。运行范围三选一:**数据集**(回放条目——多轮 scenario 在同一 session 内顺序回放)、显式 **session id 列表**、或**时间窗口**(`lookback_hours` 1–336——被动模式:不产生新调用,用 `filterConfig.timeRange` 圈定既有流量)。14 个通用提示词模板评估器(12 个 trace/session 级和 2 个普通工具调用级)、2 个技能 `TOOL_CALL` 提示词模板评估器,外加 3 个仅限真值的程序化 `Builtin.Trajectory*Match` session 级匹配器(仅当数据集 scenario 定义了 `expected_trajectory` 时可选),以及在 `?view=evaluators` 子页支持完整 CRUD 的**自定义评估器**，共三种定义——**LLM 评审**（`llmAsAJudge`：带占位符的指令、数值量表、Bedrock 评审模型）、**派生**（`derived`：在所选模型上运行某个 Builtin/ThirdParty 基础评估器的提示词）与**代码评估器**（`codeBased.lambdaConfig`：位于 workspace 同区域的 Lambda 函数 ARN 加 1–300 秒超时，默认 60；没有指令、量表与模型）。任何定义在 `CreateEvaluator` 上都必须带 `level`。控制台详情投影带 `definition: judge|derived|code`；`UpdateEvaluator` 为全量配置替换，因此更新载荷必须与评估器自身的类型一致——用评审载荷更新代码评估器（或任何跨类型组合）会被 `evaluator.definition_mismatch` 拒绝，而不是被静默转换。代码评估器的 Lambda 接收 `{schemaVersion, evaluatorId, evaluatorName, evaluationLevel, evaluationInput.sessionSpans, evaluationReferenceInputs, evaluationTarget}`，返回 `{label, value?, explanation?}` 或 `{errorCode, errorMessage}`（限制 300 秒 / 6 MB）；控制台**不管理其 IAM**——批量/在线运行作为 `evaluationExecutionRoleArn` 传入的评估执行角色需要对该函数拥有 `lambda:InvokeFunction` + `lambda:GetFunction` 权限，且函数的资源策略须允许 `bedrock-agentcore.amazonaws.com`，两点都以提示形式写在 ARN 字段下方。代码评估器在所有可选自定义评估器的地方（批量运行、实验、在线配置）均可选用。洞察运行可在三种分析类型(失败归因/用户意图/执行摘要)中任选子集。数据集以 devguide scenario 形式存于 SQLite(`?view=datasets` 子页:scenario 编辑器、JSON/JSONL 导入),一键单向同步为 AWS Dataset 资源(`AGENTCORE_EVALUATION_PREDEFINED_V1`):首次同步创建数据集(`CreateDataset`),之后每次同步都原地编辑该数据集的**草稿(DRAFT)**(`ListDatasetExamples` → `DeleteDatasetExamples` → `AddDatasetExamples`,每步经 `UPDATING` 轮询到 `ACTIVE`),因此数据集 id 与已发布版本得以保留;**发布版本**(`CreateDatasetVersion`)把草稿快照为不可变的编号版本,并把 `draftStatus` 从 `MODIFIED` 翻为 `UNMODIFIED`。行上的 `cloud` blob 缓存 id/ARN/状态以及 `draft_status`、`example_count` 与版本列表(`ListDatasetVersions`);仅云端的数据集以只读方式展示同样信息并同样可发布,单个已发布版本可删除(带 `datasetVersion` 的 `DeleteDataset`)。云端数据集运行可**固定到某个已发布版本**(`dataset_version`,在创建运行行之前先对照 `ListDatasetVersions` 校验;随后 `GetDataset` 与 `ListDatasetExamples` 读取该快照,回放的 scenario 与真值即该版本的内容);默认使用草稿,固定的版本记录在运行上并在运行列表显示为 `· v<N>`。已记录的副本若 AWS 已不认识(`ResourceNotFoundException`)或已经控制台删除,下次同步会重新创建;scenario 真值(断言/期望回复/期望轨迹)经 `evaluationMetadata.sessionMetadata` 注入批量评估。账户单批次锁与队列语义不变。操作员可在运行页**停止**任意活跃运行(`POST /api/eval/runs/{id}/stop`):批次已在 AWS 上存在的运行用 `StopBatchEvaluation` 停止(STOPPING → STOPPED——已评判的会话保留结果,轮询器记为部分分数),仍在排队的运行在到达 AWS 之前于本地取消,正在回放数据集的运行在提示词之间停止且不会调用 `StartBatchEvaluation`。三种情形都以终态 `stopped` 结束(绝不记为 `failed`),原因为「stopped by operator」;不暴露 `DeleteBatchEvaluation`。运行行只保存每个评估器的**平均分**(`evaluatorSummaries.statistics.averageScore`);**每个分数背后评审模型给出的理由**只存在于批次自己的结果日志流中(`GetBatchEvaluation.outputConfig.cloudWatchConfig` → `/aws/bedrock-agentcore/evaluations/batch-evaluations/results/default` 下的 `run-<batchId>`,`gen_ai.evaluation.result` 记录),Runs 页面在选中终态运行时按需读取(`GET /api/eval/runs/{id}/results`,绝不持久化),渲染为「会话结果」面板 —— 按会话分组,每条评审一行(评估器、层级、得分、标签、可展开的理由;span 级评估器每次工具调用一行),并链接到可观测性的会话详情。 **在线评估**(`?view=online`):每个 agent + evaluator 集合对应一个 AgentCore `OnlineEvaluationConfig`,按采样比例(0.01–100 %)在会话空闲超时后对真实会话打分,不产生新的调用;结果写入 `/aws/bedrock-agentcore/evaluations/results/<configId>`(同时以 EMF 指标落到 `Bedrock-AgentCore/Evaluations`),控制台用 Logs Insights 聚合(每个 evaluator 的均值 / 标签分布 / 趋势 / 带 judge 解释的最近记录)。页面列出 workspace 账号内**全部**配置并按归属分类:`agent`(本控制台创建,可全操作)、`experiment`(`exp_*`/`can_*` 实验 arm,只读)、`external`(仅暂停/恢复/删除)。Update 始终发送完整 `rule`(AWS 整体替换),从未被调用过的 agent 创建时会被拒绝(AWS 校验日志组存在)。 配置有两种**模式**:`scores`(evaluators)或 `insights`(1–3 种洞察类型 + 可选的 DAILY/WEEKLY/MONTHLY 聚类——AWS 不允许同一配置两者兼有);insights 配置产出**报告**(以配置为数据源的批量评估:AWS 按聚类周期定期生成,或从控制台「立即出报告」经运行队列发起),通过 `GetBatchEvaluation.dataSourceConfig.onlineEvaluationConfigSource` 归属,并复用运行页的洞察聚类树渲染;报告只覆盖该配置采样过的会话。 在线评分同时出现在查看会话的地方:可观测性的会话详情带一个「在线评估」区块(该会话在所有配置下的结果记录,按归属分类,失败降级——结果查询失败不会影响追踪),概览页新增 **在线质量 · 24h** tile(对 workspace 内 agent 持有配置做极性归一、按计数加权的均值,120 秒缓存,没有配置时不调用 AWS)。二者都通过 `SOURCE logGroups(namePrefix: ['/aws/bedrock-agentcore/evaluations/results/'])` 一次读取全部结果日志组。第三种**即时**模式从可观测会话详情同步调用数据面 `Evaluate` API 对单个会话打分(SCORE NOW:≤5 个 evaluator、每次调用 ≤10 条结果、不持久化)——用于试跑自定义 evaluator 或排查某个可疑会话;需要留档的分数仍由批量运行给出。 |
| **Optimization** | 推荐 → 配置捆绑(configuration bundles)→ 网关 A/B(config-bundle 50/50)→ target-based canary → verdict → promote → cleanup。系统提示词推荐**可插拔**:默认走 AgentCore 推荐任务,也可选第三方 provider(`gepa_lite`——对所固定评估运行的逐会话 judge 分数、解释与对话记录做一轮 GEPA 式反思,模型为操作员选择的 Bedrock Converse 模型;同一轮反思也可改写 Agent 自带工具的描述),绕开 `StartRecommendation` 及其内容过滤;产出的提示词与工具描述仍写入 treatment 配置捆绑,由后续 A/B 测试衡量。发送流量阶段的数据集回放为并发发送(在途请求上限 `TRAFFIC_MAX_CONCURRENCY` = 10,可用 `LAUNCHPAD_TRAFFIC_CONCURRENCY` 下调);一条 prompt 即一个 session 即一个分组,因此不影响分流。 |
| **Observability** | 通过 CloudWatch Logs Insights 同时读取两种遥测布局：旧版 trace 位于 `aws/spans`，统一后的 trace、日志和 prompt 位于 `/aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint>`。Span 记录按 session 渲染为链路面板。 |
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
| **package** | *跳过*(无产物) | 解析 → 带 hash 的 lock → `--require-hashes` 安装 ARM64 wheels → zip → S3 | zip 上下文 → S3 → CodeBuild(docker build+push)→ ECR → 解析 digest → 扫描闸门 |
| **provision** | 复用共享执行角色 | 复用共享执行角色 | 复用共享执行角色 |
| **deploy** | `CreateHarness` + 轮询 READY | `CreateAgentRuntime` + 轮询 READY | `CreateAgentRuntime(containerConfiguration)` + 轮询 READY |
| **register** | A2A 注册记录,自动提交 | A2A 注册记录,自动提交 | A2A 注册记录,自动提交 |

典型耗时:harness ≈ 30 秒,zip ≈ 1–3 分钟(含 pip),container ≈ 2–4 分钟(实测:CodeBuild 1.7 分钟 + 数秒即 READY)
(经 CodeBuild)。见 [troubleshooting.zh-CN.md](troubleshooting.zh-CN.md)。

### 按 Agent 的执行角色

过去所有 agent 共用一个 `launchpad-agent-execution-role`,其上有 14 条语句、多数是账号级
的。真正的暴露面不在于抽象意义上的通配符,而在于**任何一个 agent 都拥有其他所有 agent 的
触达范围**:挂载其他 agent 的文件系统、读取所有 agent 的 skill 包、检索账号内任意知识库、
改写 gateway 路由。

`app/services/agent_iam.py` 按 spec 为每个 agent 派生角色。Sid 与 CDK 角色保持一致,以便
逐条对比。

| 授权 | 何时产生 | 范围 |
|---|---|---|
| `BedrockModels` | 总是 | 配置的 `model_id` |
| `BedrockMantle*`、Marketplace | `model_source == "mantle"` | project/`*`;Marketplace 由 `CalledViaLast` 约束 |
| `AgentCoreMemory` | 启用记忆 | 记忆单例 |
| `AgentCoreWorkloadIdentity`、`IdentityVaultSecrets` | 有 gateway/MCP 工具或知识库 | — |
| `AgentCoreCodeInterpreter` / `AgentCoreBrowser` | 挂载了对应内置工具 | — |
| `EcrPull` / `EcrAuth` | `method == "container"` | 该仓库 |
| `SkillBundle*` | 挂载了 skill | **本 agent 的**前缀 |
| `ManagedKbRetrieval` | 挂载了知识库 | **已挂载的** KB ARN |
| `A2AInvokePeerRuntimes` | `protocol == "a2a"` | 账号内 runtime |
| `Telemetry` | 总是 | runtime 日志组 |
| BYO 挂载策略 | 配置了挂载 | **本 agent 的**接入点 |

**刻意保留 `*` 的部分及原因**:`bedrock:AgenticRetrieveStream`、
`bedrock-mantle:CallWithBearerToken`、`ecr:GetAuthorizationToken` 都不支持资源级收窄,
X-Ray 上报与 `cloudwatch:PutMetricData` 同理。这些在语句处就地注明,而不是悄悄收窄。

**移除了两项授权**——值得知道,因为"移除"才是会以运行时失败形式暴露出来的那一类:
`ABTestOrchestration`(19 个动作,含 `CreateGatewayRule`、`UpdateGateway`、
`InvokeAgentRuntime`)本是**平台**用自己凭证做的事;CloudWatch Logs 的**读**动作是控制台
路径,泄漏到了工作负载角色上。`InvokeAgentRuntime` 对 A2A agent 保留,它确实要调用同伴。

**按 agent 的角色并不带来按 agent 的记忆隔离。** 记忆只有一个共享实例,靠把 agent id 折进
actor id 来分区(`services/memory.py::scoped_actor`),不是靠 IAM。按 agent 建记忆是另一
件事。

生命周期:在 `provision` 创建,重新发布时对齐(被去掉的能力会让策略收缩),随 agent 删除
——且必须在 runtime **之后**,因为先删角色可能卡住 runtime 自身的删除。删除失败绝不阻塞
agent 的删除;角色带 `launchpad:agent-id` 标签,便于找到孤儿。`ensure_role` 会接管同名的
已有角色,因此一次半失败的删除不会卡住用同名重建 agent。

Canary 与 A/B 候选版本沿用**生产当前所在的角色**,取自 `GetAgentRuntime.roleArn`。候选版本
是替生产站位的,给它共享角色会让它以生产并不具备的权限被评测;而读取实时值(而非按名字
推导)也让早于本改动部署的 agent 继续可用。

共享角色仍然存在、也仍带宽泛授权:它支撑尚未重新发布的 agent。在所有 agent 迁移完成前
缩减它会抽掉仍在使用它的 agent 的授权,因此该缩减**尚未**执行。

### 构建的供应链

一个已部署产物必须能回答两个问题:里面装了什么,以及正在运行的是否仍是当初构建出来的。
两者都落在 `package` 阶段。

**依赖先解析、再锁定、再校验安装。** 过去这里只有一次针对声明列表的 `pip install`,它
装的是那一刻索引提供的任何版本(平台自带的范围写法也一样),而且不留任何记录。现在该
阶段先用 `uv pip compile --generate-hashes` 针对部署目标解析(aarch64、Python 3.13,在
`zip_runtime.py` 里只写一次,以保证解析与安装不会各说各话),再用 `--require-hashes`
安装。被替换或重新上传过的发行包会让构建失败。lock 以 `requirements.lock` 随 zip 下发,
产物自带物料清单。这里刻意没有回退路径:解析失败就是阶段失败。

调用方提供的 `spec.requirements` 还会在 **schema** 校验阶段被要求固定版本
(`app/schemas/requirements.py`),因此控制台会在构建启动前就拒掉范围写法。平台自带的
清单保留范围——`MANTLE_EXTRA_REQUIREMENTS` 的注释解释了 pip 本就应当对同一个项目的两条
规格求交集——可复现性由 lock 提供。Harness 转换是平台唯一一处从别处派生依赖的地方(源
Harness 的 `pyproject.toml`),所以它把那些范围解析成固定版本,而不是被豁免于该规则。

**容器镜像会被扫描,并按 digest 部署。** ECR 在推送时扫描。构建完成后
`_stage_package` 把推送出的标签解析为不可变 digest、记录到 `Deployment` 行上,并在镜像
能够支撑 runtime 之前运行闸门;`_stage_deploy` 以 `repo@sha256:…` 作为 `containerUri`
下发。若按 `{agent}-v{version}` 标签部署,runtime 执行的内容就可能在无任何记录的情况下
发生变化。

闸门的阈值和开关都可配置,因为一个无法绕过的闸门会在基础镜像第一次出现 CVE 时把所有
agent 全部卡死。而读不到的扫描——未启用扫描、API 报错、超时——会被如实记录并让部署以
"未扫描"状态继续;它绝不会被并入"干净",因为缺失的闸门不能被读成通过的闸门。

镜像标签保持**可变**:打包发生在 `_stage_deploy` 递增版本号之前,因此重新发布会把同一
标签推送两次,不可变标签策略会让第二次推送失败。digest 固定才是真正的控制点,并且有一
条 infra 测试断言该标签策略,以防它悄悄漂移成一个坏掉的重新发布。

未覆盖:SBOM 生成、provenance/attestation、签名、受信镜像源强制,以及 skill **内容**
审查。不可变不等于可信。

### 创建入口

`/create` 的入口卡片共四张,顺序如下:

| # | 卡片 | `AgentSpec.method` | 说明 |
|---|---|---|---|
| 1 | **托管 Harness** | `harness` | 方式B —— 声明式,无构建产物 |
| 2 | **Strands Studio** | `zip_runtime` | 方式C —— Strands 模板走 zip 快速通道;卡片内嵌链接进入 `/create/studio` 画布,画布以 `studio` 方式部署 |
| 3 | **其他 Agent SDK** | `container` | 方式A —— 自带 Agent SDK,经 CodeBuild 打包为 ARM64 容器 |
| 4 | **发现现有 Runtime 与 Harness** | — | 不是部署方式(见下文) |

第三张卡片是一个**类别**,而不是某一个 SDK。`AgentSpec.agent_sdk` 记录容器
Agent 打包的是哪个 SDK,向导把它作为配置步骤上的二级选项。它是只有一个成员的
`Literal`(`claude_agent_sdk`)且默认取该成员,因此在该字段出现之前写入的容器
spec 也能被无歧义地读回,将来新增第二个 SDK 无需迁移已存 spec。目前**故意不对
该字段做分派**:在类别出现第二个成员之前,`app/deployer/container.py` 与
`app/templates/claude_sdk_agent/` 保持无条件实现。

### 推荐的 trace 来源

`RECOMMEND` 读取两者之一:默认是滚动的 `RECOMMEND_LOOKBACK_DAYS`（7）天 CloudWatch 窗口，
或者由 `agentTraces.batchEvaluation` 固定的某一次已完成的批量评估。固定它有两重意义:

- **血缘。** 一个洞察任务与一次基于同一窗口的推荐只是彼此**重叠**;固定之后，推荐才是
  可证明地*从*那次分析生成出来的。
- **可复现。** 7 天窗口比任何单次分析都**更宽**，所以默认路径可能摄入没有人看过的流量
  ——包括上一次实验的 treatment 分支——而明天重跑同一个实验读到的又是另一批 trace。

控制台提供该实验 Agent 自己已完成的运行（`GET /api/eval/runs?agent_id=…`）;后端用
`GetBatchEvaluation` 解析所选运行，这同时也是校验手段（存在、已完成、属于同一个 Agent）。
同一次 RECOMMEND 中的两个生成器共享被固定的来源，而解析出的来源——ARN、run id、batch id、
模式——会为两条路径都记录在 `recommend` 产物上，因此一个已完成的实验始终可解释。

### 推荐 provider

RECOMMEND 背后的系统提示词生成器是一个 **provider**
（`backend/app/optimization/providers/`）;工具描述生成器则始终是 AgentCore 自己的。
`recommend_provider` 缺省意味着 `StartRecommendation` 任务与以前完全一样地运行。
`gepa_lite` 则改为读取被固定运行的批量评估结果流（逐会话的评估器分数、标签与解释——与在线
评估发出的 `gen_ai.evaluation.result` 记录同源），与每个会话的对话记录做联接，按最差优先
采样最多 30 个会话（做极性归一，并带一组得分最高的对照样本），然后请一个 Bedrock 模型
——默认 Claude Opus 5，可选 Sonnet 5 / GPT-5.6 Sol，也允许自定义 id——做一轮反思式重写:
诊断、具体改动、修订后的提示词;并在同一次调用中，依据每个会话的工具调用、结果与工具调用级
评审判定，为该 Agent **自带的**工具（treatment 捆绑可以覆盖的那一组已发现工具）给出修订后
的描述;gateway / MCP 工具只作为上下文展示、绝不被改写，而一次没有任何工具调用的运行会把
工具侧收口为 `no-tool-calls`，提示词侧照常进行。这是去掉了 GEPA 搜索循环、只留下其反思步骤
的做法:随后的配置 A/B 才是评估该候选者的环节。无法产出可用提示词的 provider（没有已打分
的会话、模型访问被拒、输出无法解析、经过一次压缩后仍超出 8 000 字符预算）会写入 `FAILED`
状态与原因并且**不写提示词**——与失败的 AWS 任务遵循同一条 ISSUE-007 规则——因此 `accept`
仍然被闸门挡住。产物记录 `provider`、`provider_model_id` 与证据数量，treatment 捆绑的提交
信息也会点出它们，因此一个已完成的实验始终可解释。Bedrock 调用走 workspace 客户端漏斗;
`gepa` 包（以及它构造的 litellm 客户端）刻意不作为依赖引入。provider 自身位于
`backend/app/optimization/providers/`:`base.py` 是每个 provider 都要实现的契约，
`registry.py` 是靠 import 副作用注册的注册表，`evidence.py` 负责已打分会话与对话内容
的关联，`bedrock_lm.py` 是 ConverseStream 文本调用体，`gepa_lite.py` 是上文那一轮反思，
`agentcore.py` 则是仅为被发现而列出的内置 AgentCore 任务。

### 平台工具包（`AgentSpec.toolkits`）

**工具包（toolkit）**是一组有名字、由平台自己拥有的本地 `@tool` 函数，覆盖在内嵌的种子
数据之上，由 Strands ZIP 模板内联进生成的 `main.py`。仅适用于 `zip_runtime` +
`protocol=http`;今天只有一个成员 `hr_assistant`（五个 HR 工具:PTO 余额/申请、政策查询、
福利摘要、工资单）。

它刻意**不是** `ToolRef.type` 的一个成员:现有每个成员都指向一个外部资源，会驱动 IAM 与
部署器行为，而工具包两者都不驱动——没有 ARN、没有授权、没有 gateway、没有网络调用、也没有
额外的 pip 依赖。

有两个性质让它值得拥有自己的字段:

- **它在 generate 阶段渲染，因此 `spec.code` / `spec.code_bundle` 保持 `None`**，Agent 也
  就保留了配置捆绑实验的资格。把生成的源码写进这两个字段中的任何一个，都会让
  `experiment_capability` 返回 `custom-source-unverified`——这正是它是一个 spec *选择项*
  而不是被物化的代码的原因。
- **工具包是把模板自带的 `calculator` / `current_utc_time` 替换掉，而不是在其之上追加**，
  因此部署出的工具面就恰好是工具包本身。这一点对 trace 就绪度很重要:`missing_tools` 非空
  会强制 `state="sparse"`，于是一个被期望却从未被调用过的工具会把 Agent 永久压在 `ready`
  之下。

工具名与描述用 `ast` 从工具包源码派生，遵循 Strands 自己的 docstring 规则（docstring 去掉
`Args:` 段），因此 `discover_agent_tools`——以及由它决定的 `expected_tools`、就绪度和推荐
界面里的「当前描述」——报告的正是模型看到的内容。目录本身是
`backend/app/templates/toolkits/__init__.py`（每个成员的工具源码是它旁边的
`*.py.tmpl` 模板）;spec 字段是 `backend/app/schemas/agent.py` 里的
`AgentSpec.toolkits`。

### Registry 技能与部署快照

创建 Agent 向导只从 `GET /api/registry/attachables` 读取 APPROVED 的 `AGENT_SKILLS` 记录。
选中之后，`AgentSpec.skills` 中存的是该 bundle 的 S3 前缀;调用时绝不会去检索 Registry。
被选中的前缀同时驱动所属 Agent 的 `SkillBundle*` IAM 语句。

每种方式按自己的产物模型消费这同一个字段:

| Agent 形态 | 技能物化方式 | 运行时激活 |
|---|---|---|
| Harness | 原生 Harness S3 Skill 源 | Harness 渐进式披露 |
| 生成的 zip，HTTP 或 A2A | 打包时快照到 `skills/<name>/` | Strands `AgentSkills` 插件，仅当至少打进一个 `SKILL.md` 时启用 |
| Container | 镜像构建时快照到 `.claude/skills/<name>/` | Claude Agent SDK 项目的 `Skill` 工具 |
| Studio | 生成代码中的引用把 APPROVED 的 bundle 解析进 `skills/<name>/` | Studio 生成的 `AgentSkills` 插件 |
| 由 Harness 转换出的 `code_bundle` | 没有平台快照;导出的 fetcher 仍然是权威 | 导出的运行时 fetcher |

Registry 上的编辑与重新导入不会热更新 zip、container 或 Studio 产物——要重新发布 Agent 才会
抓取新的快照。A2A 有两个彼此独立的 Skill 概念:`AgentSpec.skills` 挂载指令/资源 bundle，而
`AgentSpec.a2a_skills` 发布 AgentCard 的路由元数据。

### 系统托管预置（`aws-agent-solution-architect`）

**系统托管预置**是身份与配置归平台、而非成员所有的 Agent。第一个预置是
`aws-agent-solution-architect`：一个托管 Harness（方式B），把 AI Agent 业务需求转化为
评估优先的 AWS 方案设计。它把一份外部方法论包（三轮需求采集、痛点 → 指标 → 黄金测试 →
evaluator 映射、AgentCore 优先的取舍、证据分级、不自主执行）改写为平台自有的**英文**资产：
`backend/app/system_agents/skills/aws-agent-solution-architect/` 下的 `SKILL.md` 与
`references/`，以及 `backend/app/system_agents/presets.py` 中的系统提示词。原始包不入库，
其 PDF、DOCX、安装器与桌面脚本一律不发布。Agent 用用户最新一条消息的语言回复，不写死语言。

**服务端持有身份。** 新增可空、带索引的 `Agent.system_key` 标记预置行。它从不从请求读取：
`AgentSpec` 没有该字段，客户端在 spec 中传 `system_key`/`system` 会被 Pydantic 丢弃，行仍是
普通 Agent。保留名称对普通 Agent 拒绝（`409 agent.name_reserved`），发现导入也会绕开它；
`(workspace_id, system_key) WHERE system_key IS NOT NULL AND status != 'deleted'` 上的部分
唯一索引保证每个 Workspace 只有一个在用的预置。API 投影新增 `system` 成员
（`{managed, key, label, skill_version, protected_actions}` 或 `null`），控制台据此渲染
“系统”标签。

**受保护的变更路径。** 对预置，`POST …/redeploy`、`DELETE /api/agents/{id}` 与
`POST …/convert` 在**构建任何 AWS 客户端之前**返回 `403 agent.system_managed`，无论调用者持有
哪些 `perm:agents.*`——管理员也一样，只能通过 `/api/system-agents` 维护。间接写入路径受同一拒绝
保护：`POST /api/experiments/{id}/action` 与 `POST /api/runtime-canaries/{id}/action` 在写入
`running_action` 之前就拒绝任何引用预置的（可能陈旧的）记录；后台线程会执行的服务入口
（`act_promote`、金丝雀 `act_setup`/`act_complete`/`act_rollback`、两个 `run_action`
调度器）在第一次 AWS 调用前拒绝；能力投影另外返回 `reason_code: system-managed`。
`DELETE /api/knowledge-bases/{kb_id}`（无论是否 `force`）在知识库挂载于预置时，以仅读台账的
预检返回 `409 kb.attached_to_system_agent`，成员永远无法强制解除预置的知识库或触碰其网关目标；
管理员先用省略该知识库的 `knowledge_bases` 请求体修复预置来解除挂载。普通 Agent 保持
2026-08-07 的成员生命周期权限与普通知识库强制删除语义不变（`tests/test_system_agents.py`
断言了这一对等性）。

**显式、幂等安装——绝不在启动或读取时发生。** `GET /api/system-agents`（成员）只读台账，
状态为 `configuration_required`（Workspace 未 `ready`、缺 `artifacts_bucket` /
`execution_role_arn`、或按 Agent 角色被禁用）、`not_installed`、`deploying`、`active`、
`uninstalling`（拆除任务持有该行；`operation` 携带其任务 ID、状态、尝试次数、错误与
`retryable`）、`failed` 之一，附带 `{code, message}` 形式的 `requirements`（已安装但 Workspace 后来失去前置
条件时同样给出），在已有普通 Agent 占用保留名称时给出 `name_collision`（预置**绝不接管**，安装
返回 `409 system_agent.name_collision`），以及按操作区分的裁决 `can_install` / `can_repair` /
`can_uninstall`（管理员 + 该操作的就绪条件）。控制台按 code 本地化描述与条件，显示加载、错误与
重试状态；直接消费安装/卸载响应，并在轮询到终态时刷新 Agent 列表。
`POST /api/system-agents/{key}/install`（管理员）是唯一触达 AWS 的路径：

| 预置状态 | 结果 |
|---|---|
| 未安装 | 新建行 + 创建任务（`202`，`created: true`） |
| 部署中 | 返回进行中的任务（`202`，`changed: false`）——重复点击不会堆叠任务 |
| 运行中，版本与选项相同 | 无操作（`200`，`job_id` = 产出当前运行中预置的那个任务） |
| 失败 / 选项变更 / 技能包更新 / `force: true` | 更新任务 = 就地重新发布（`202`） |

请求体是必需的 JSON 对象；`{}` 在首次安装时表示“平台默认值”，在修复时表示“已存选择”。维护
声明是持久且原子的：新安装在部分唯一索引上竞争，落败方重读胜出方**并返回其任务 ID**；修复在与
所建任务行相同的事务里执行一次条件更新 `UPDATE … WHERE status IN (active, failed)`，两个都
加载了运行中行的会话收敛到同一个任务。**卸载是持久任务，不是终态标记**：
`DELETE /api/system-agents/{key}` 把行置为非终态 `uninstalling`，并**同时**创建
`uninstall_system_agent` 任务（`202 {job_id, attempt, started, preset}`）。在 worker 拆除成功
之前，该行保留系统身份——部分唯一索引也持续占用该 key——因此 AWS 资源尚在删除时，任何安装或
修复都无法夺取该 key（均返回 `409 system_agent.uninstalling`）；重复卸载返回同一个在途任务；
拆除失败时行仍为 `uninstalling`，原因记录在任务与行上（`operation.retryable`），再次显式卸载
启动第 N+1 次尝试；拆除中途崩溃由 `resume_pending_jobs()` 与其他任务一样恢复，worker 只处理
仍处于 `uninstalling` 的行。只有拆除成功才把行标记为 `deleted`。拆除使用普通删除同一个幂等
helper，只处理该行上记录的资源。部署任务走**标准**
`generate → package → provision → deploy → register` 管道，并带三项预置专属加固：

- **原子版本钉住**——写入任何内容之前，安装把仓库技能包**一次性**读入不可变的内存快照，校验该
  快照（与成员技能相同的 `validate_bundle`，加版本/名称不变量）并计算哈希；随后
  `{version, digest, files{rel: sha256}}` 与 Agent、Deployment、Job 行**在同一次提交**中落到任务上
  （`create_deployment(payload_extra=…)`），崩溃永远不会留下没有钉住信息的可运行任务。普通 Harness
  跳过的 `package` 阶段在钉住信息缺失或格式错误时故障关闭——不接受任何“遗留”情况——并在已存
  spec、钉住信息与当前构建快照不一致时拒绝；
- **单一字节快照、冲突安全发布**——被校验和哈希的字节就是被上传并回读的字节。
  `s3://<artifacts_bucket>/system-skills/<name>/<skill_version>/` 下每个对象都以
  `If-None-Match: *` 创建；412 表示另一写入者抢先，已有字节必须与我们的一致（部分上传后的重启，
  或相同内容的并发重试），否则阶段失败且不覆盖任何内容。清单（`.bundle-manifest.json`，含逐文件
  摘要）最后条件写入；竞争的清单只有完全一致时才被接受。已有清单只在与快照完全匹配时才被信任
  （格式错误或不同 → 失败：已发布版本不可变，同时提升 `skill_version` 与 SKILL.md 的
  `version`）。最后**回读并哈希每个对象**与快照比对：缺失对象以 `If-None-Match: *` 恢复，损坏
  对象只以读取时 ETag 的 `If-Match` 替换，仍不一致的前缀则失败——该阶段绝不会对缺失或被改动的
  `SKILL.md` 报告“已核验”，也绝不写入带版本前缀之外；
- **幂等 AWS 请求**——每次 Harness 创建/更新都发送 `clientToken = lp-<deployment id>`
  （持久化，而非 scratch 状态），在 AWS 调用与台账写入之间崩溃后恢复的任务重放同一请求，不会
  创建第二个 Harness。这适用于所有 Harness Agent，不限于预置；
- **AWS 收到的就是所供给的角色**——deploy 阶段对每个 Harness Agent 都用 provision 阶段的结果
  （或在丢失 scratch 的恢复中用确定性的 `launchpad-agent-<name>-<id8>` 角色名）设置
  `executionRoleArn`；generate 阶段的共享角色占位符不再到达 CreateHarness/UpdateHarness。
  预置另外**故障关闭**：`per_agent_execution_roles=false`，或解析出的角色是共享 Workspace 角色时，
  generate/deploy 在任何 AWS 调用前抛错，状态读取报告 `per_agent_roles_disabled` 条件。

该前缀族与成员可写的 `skills/`（Registry）和 `agent-skills/`（向导暂存）互不相交。

**受约束的工具面。** Harness 默认向每个会话暴露 `shell` 与 `file_operations`，除非
`allowedTools` 加以限制，因此新增 harness 专用的 `AgentSpec.allowed_tools`（`None` = 既有
Agent 保持 API 默认；每项 1–64 字符，匹配服务模型的 `*|@?name(/tool)?`），映射到请求的
`allowedTools`；预置发送 `["file_*", "@aws_knowledge"]`：技能所需的文件工具、公共 AWS
Knowledge MCP 服务器（`https://knowledge-mcp.global.api.aws`，`remote_mcp` 工具，无凭证），
没有 shell。挂载知识库时，部署器追加 `@<知识库网关工具名>`（`@launchpad_kb_gw`）——仅在此时，
且绝不使用 `*`——使提示词点名的检索工具可被调用。`allowedTools` 只约束 LLM 的工具选择；真正的
边界是按 Agent 的执行角色：模型调用、仅限该版本技能前缀的 `s3:GetObject`、遥测——仅文档型预置
别无其他。MCP ToolRef 携带 `auth: "none"`，告知角色推导跳过带认证 MCP 引用才有的工作负载身份与
令牌库语句（普通 Agent 的 MCP 引用不变）。挂载知识库**恰好**增加 Harness 开发指南为 OAuth2 凭证
提供者列出的三条语句（“Execution role policy → OAuth2 credential provider”，2026-09-12 阅读），
以 Workspace 的 `oauth_provider_arn` 实例化到知识库网关的真实提供者：`GetResourceOauth2Token`
作用于 `token-vault/default`、`workload-identity-directory/default` 与
`…/workload-identity/harness_<name>-*`；`GetResourceOauth2Token` 作用于提供者 ARN 本身；
`secretsmanager:GetSecretValue` 作用于 `bedrock-agentcore-identity!default/oauth2/<provider>-*`
（提供者范围的密钥**确实**需要并予以保留）。没有 `GetResourceApiKey`、没有
`GetWorkloadAccessToken*`、没有家族级 `bedrock-agentcore-identity!*` 密钥，也没有直接的
`bedrock:Retrieve` / `AgenticRetrieveStream`——Harness 经网关访问知识库，检索由网关连接器角色执行。
Workspace 没有可限定的提供者时，带知识库的安装被拒绝（`409`，条件 `missing_oauth_provider`）。
普通 Agent 保持历史策略形状。向导在编辑/重新发布时原样回传已存的 `allowed_tools`（`AgentSpecInput`
已定型），控制台重新发布永远不会放宽工具面——注意按服务模型，UpdateHarness 省略 `allowedTools`
会保留线上限制，因此此前的风险是后续重建时丢失台账意图，而非立刻放宽。

**记忆。** `short_term`/`long_term` 标志无法对真实 API 表达“仅短期”：共享 Workspace 记忆带有
长期策略，而 CreateHarness *省略* `memory` 成员意味着 Harness 托管默认值，会创建带
SEMANTIC + SUMMARIZATION 策略的记忆（`HarnessManagedMemoryConfiguration`，其策略列表最少一项）。
因此预置发送 `memory: {"disabled": {}}`——完全没有持久记忆，角色上也没有记忆授权——新会话的需求
基线在结构上独立于此前所有会话。单个运行时会话内的对话保存在 Harness 会话中（服务模型把记忆
描述为*跨*会话持久化上下文）；确认会话内连续性属于待完成的实机冒烟。作为一致性修复，所有无标志
的 Harness spec 现在在创建时也发送显式 `disabled` 变体，与更新路径一致。

**可选知识库。** 安装请求体可以指定既有、已授权的知识库
（`knowledge_bases: [{kb_id, name, description}]`），通过普通 Harness 知识库网关路径挂载。
不会自动创建任何东西，技能也如实声明：没有检索工具时按方法论索引工作，并说明未查阅原文。

**管理员的选择**仅限模型（`model_id` + `model_source`，默认平台 `DEFAULT_MODEL_ID`）与可选
知识库，且**仅通过 API**——控制台面板以 `{}` 安装（默认值，修复时为已存选择）并如实说明；面板
没有模型/知识库字段。知识库引用在请求时做形状校验，并在 **provision 阶段核验**（在目标 Workspace
中 `GetKnowledgeBase`：存在、MANAGED、ACTIVE），然后才创建网关目标，否则以可操作的原因使阶段失败。
普通使用绝不会覆盖版本或配置。

**待实机验证。** 以上全部有封闭测试（`tests/test_system_agents.py`）；实机冒烟——在获批的
Workspace 安装、确认 S3 技能在 `allowedTools` 限制下真正加载、AWS Knowledge 工具可用、成员
无法删除/重新发布、重复安装不产生重复——**尚未**执行，预置在此之前不算可运营。若 Harness 的
技能加载工具名不在 `file_*` 之内，请把它加进 `ARCHITECT.allowed_tools`，而不是放宽为 `*`。

### 模型来源(方式B + 方式C)

`AgentSpec.model_source` 决定模型的托管面:`mantle`(Bedrock Mantle)或
`bedrock`(原生 Bedrock)。**两种托管面都不涉及任何 API Key** —— 鉴权全部由
Agent 自身的执行角色完成。但 Mantle 需要自己的 IAM 授权:`bedrock-mantle` 是独立
的 IAM 服务,`bedrock:InvokeModel` **并不覆盖它**,因此
`infra/stacks/base_stack.py` 额外授予 `bedrock-mantle:Get*`/`List*`/
`CreateInference`、`bedrock-mantle:CallWithBearerToken`,以及以
`aws:CalledViaLast = bedrock-mantle.amazonaws.com` 限定的 Marketplace 订阅权限
(对齐 AWS 托管策略 `AmazonBedrockMantleInferenceAccess`)。缺了这些,Mantle
Agent 会部署成功并进入 ACTIVE,但首次调用报 `401 access_denied`;该授权由 harness
与 zip 共用,新增它需要执行一次 CDK 部署。该字段默认为 `bedrock`,以兼容
此字段出现之前写入的 spec;Mantle 是**表单**默认值,按方式在控制台中分别设定
(`frontend/src/pages/CreateAgent.tsx` 中的 `MODEL_SOURCE_BY_METHOD`)。控制台
提供的模型清单位于 `frontend/src/lib/models.ts`。

**Harness(方式B)** —— 两种来源使用 `HarnessModelConfiguration` 联合类型中
**同一个** `bedrockModelConfig` 分支,只有 `apiFormat` 不同:Mantle 用
`responses`,Bedrock 用 `converse_stream`(`app/deployer/harness.py`)。带 Key
的联合分支(`openAiModelConfig` / `geminiModelConfig` / `liteLlmModelConfig`)
有意不使用 —— 它们都需要一个 Launchpad 从未创建的 AgentCore Identity API Key
凭证提供方 ARN。

**Zip / Strands Studio(方式C)** —— 模型是作为参数传给 `Agent(model=...)` 的,
因此来源会改变**生成的代码**。裸字符串 ID 会被解析为 Converse 调用,所以
`mantle` 会改为渲染一个显式的模型对象
(`app/templates/strands_agent/main.py.tmpl::build_model`):

```python
OpenAIResponsesModel(bedrock_mantle_config={"region": MANTLE_REGION}, model_id=MODEL_ID)
```

`bedrock_mantle_config` 让 Strands SDK 在**每次请求**时从环境中的 AWS 凭证链
(即持有上述 `bedrock-mantle` 授权的 Runtime 执行角色)签发一个短期 Bearer
令牌,并自行推导出 Endpoint。这条路径上**不存在 `BEDROCK_API_KEY`**。两个需要
留意的推论:

- Mantle spec 打包出的 `requirements.txt` 会增加 `strands-agents[openai]`
  (`app/deployer/zip_runtime.py` 中的 `_method_requirements`);正是这个 extra
  带来了 `openai` 与 `aws-bedrock-token-generator`。`OpenAIResponsesModel` 的
  import 写在函数内部,因此从不安装该 extra 的 Bedrock 来源 Agent 仍能正常导入。
- Mantle 模型托管在 **`us-east-1`**,而不是 Runtime 所在的 Region。可用
  `LAUNCHPAD_MANTLE_REGION` 覆盖;默认值是 `us-east-1`,绝不使用 `AWS_REGION`。

`/create/studio` 画布对每个节点同样输出这两种形式:节点未填 `apiKey` ⇒
`bedrock_mantle_config`;显式填写 Key ⇒ 沿用今天的
`client_args={"api_key": …, "base_url": …}` 覆盖形式,因此已带 Key 发布的
Flow 生成的代码与之前逐字节一致。SDK 禁止两者同时出现,而三个画布代码生成器
共用同一个输出函数(`frontend/src/studio/lib/models.ts` 中的
`mantleModelArgs`)。

A2A zip Agent 使用另一个没有 Mantle 分支的模板,因此向导会将其固定为
`bedrock` 并隐藏该选择器。其他 Agent SDK(container)入口同样固定为
`bedrock` 且只提供 Claude 模型 —— 该类别目前唯一的成员 Claude Agent SDK 只能
驱动 Claude;向导在此处用 SDK 选项替代模型来源控件。

### 发现既有 Runtime 与 Harness

`/create?view=discover` 是与三种创建方式并列的一条接入路径，而不是一种部署方式。
`GET /api/agents/discovery` 会跟完所配置 Region 中 Runtime 列表的每一页，并对每个资源做一次
详情读取。后端只返回白名单投影:Runtime 标识、名称、描述、协议、制品类型、authorizer 类型、
AWS 状态/版本以及最近更新时间。环境变量值、制品位置、执行角色与 authorizer 配置从不离开
后端。

一次显式的 `POST /api/agents/discovery/import` 会重新读取每个被选中的 Runtime，并创建或刷新
一条 `method=discovered_runtime`、`owner=aws-discovery` 的 `Agent` 行。它不创建 Deployment 或
Job，不运行任何管道阶段，也不做 Registry 注册。幂等标识先看 ARN、再看 Runtime ID;命中某条由
Launchpad 创建的行时会报告「已纳管」并且绝不改写它。移除一条导入行只是本地解除关联，绝不
调用任何 AgentCore 删除或更新操作。

HTTP 与 A2A 资源可以导入;MCP Runtime 资源在扫描中仍然可见，但它们不是 Agent，不能被导入。
导入能力与调用能力刻意分开:导入的 HTTP/A2A 资源只有在 AWS 报告 `READY` 且没有配置自定义
JWT authorizer 时才可调用。带自定义 JWT 的资源可以作为清单保留，但会被排除在 Chat 与 `/v1`
之外。

托管 Harness 服务会把每个 harness 物化为一个由它自己拥有的后端 Runtime（名为
`harness_<harnessName>`，跑该服务自己的 `public.ecr.aws/…/harness-<region>` 镜像），而该
Runtime 拒绝 `InvokeAgentRuntime`。扫描通过联接 `ListHarnesses` 把这些行标记为制品类型
`harness`:它们永不可导入（原因 `harness-managed`）、永不可调用，并且当拥有它的 harness 是
一个 Launchpad Agent 时，该行会链接到那个 Agent 并标为已纳管。若 `ListHarnesses` 失败，镜像
启发式仍会把它们标出来——只是丢掉归属链接。

操作者真正要导入的是**拥有它的那个 Harness**。同一个响应带一个 `harnesses` 数组（标识、
状态、版本、最近更新、归属链接）以及一个失败降级的 `harness_scan_error`——`ListHarnesses`
失败时 Runtime 那一半扫描仍然完好，而不是让整个请求失败。`POST
/api/agents/discovery/import` 在 `runtime_ids` 之外还接受 `harness_ids`，创建的是同一种外部
拥有的行形态，由 `spec.discovery.resource_type = "harness"` 区分（缺省 ⇒ `runtime`，所以在
此之前导入的行行为不变）。这条行存的是 **harness** 的 ARN 与 id，其余一切都由此自然推出:
Chat 与 `/v1` 完全像对待 Launchpad `method=harness` Agent 那样分派到 `InvokeHarness`，该
harness 的后端 runtime 通过既有的 ARN 联接解析出它的归属，重新发布被拒绝，而移除则是一次
绝不调用 `DeleteHarness`、也不触碰 IAM 的台账解除关联。已经由 Launchpad 部署过的 harness 会
被报告为已纳管，绝不重复创建。状态只对首次导入设门禁（`CREATE_FAILED`/`DELETING` 不能
导入）;对已存在的行重新导入总是刷新它，台账正是这样得知一个外部 harness 已经坏掉。导入会读
`GetHarness`，因此被自定义 JWT authorizer 挡在前面的 harness 会作为清单保留并被排除在 Chat
之外——与 Runtime 路径完全一样的切分。评估、实验以及 harness→zip 转换都仍然以
`method=harness` 为键，因此不会提供导入进来的 harness。

### 版本与端点(只读)

每次 `UpdateAgentRuntime` / `UpdateHarness` 都会发布一个不可变的新版本;`DEFAULT` 端点
自动跟随最新版本,而命名端点(目标金丝雀的 `stable`/`treatment`)固定在某一版本。台账只记得
Launchpad 部署时铸造的那个版本(`Agent.version`),所以 `/create` 的 Agent 详情(details 模式)
带有一个由 `GET /api/agents/{agent_id}/versions` 支撑的**版本与端点**面板。该路由把台账行解析到
唯一一个资源族——`zip_runtime`/`studio`/`container` 以及 `spec.discovery.resource_type` 缺省或为
`runtime` 的导入行 → `ListAgentRuntimeVersions` + `ListAgentRuntimeEndpoints`;`harness` 以及
`resource_type == "harness"` 的导入行 → `ListHarnessVersions` + `ListHarnessEndpoints`——跟随每一页
`nextToken`,并返回与发现功能相同风格的白名单投影(版本、状态、描述、时间戳、端点的生效/目标版本、
失败原因;绝不包含环境变量、制品位置、执行角色或鉴权配置)。没有 AWS 资源的行(部署仍在进行、
首次部署失败、已删除的 Agent,或解析不到任一资源族的形态)返回 409 `agent.no_resource`,并附带
面板会原样展示的人类可读原因。

面板标出 `DEFAULT`,把台账版本与 AWS 最新版本并列——带外更新或金丝雀候选版本铸造之后出现的不一致
会以警告呈现而不是当作错误——并标记 `stable`/`treatment` 端点名,让金丝雀残留一眼可见。它是严格只读的:
从不改指 `DEFAULT`,也从不创建、更新或删除端点;这些操作归金丝雀所有。

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

### Gateway（MCP）工具同时可达 Harness 与 zip runtime

Gateway `ToolRef` 过去是 harness 独有的能力，这在实验课上划出了一条没有参与者会预期的分界
线:第 11 章治理的是只有 Harness 才做得出的工具调用，而第 09/10 章做实验的 runtime 一个工具
调用也做不出来。现在两种方式都能触达 `launchpad-gw`;差别只在*由谁完成令牌交换*。

| | 托管 Harness | 生成的 zip runtime |
|---|---|---|
| 工具接线 | 声明式的 `agentcore_gateway` 工具，带一个 `outboundAuth` OAuth 块 | 生成的 `main.py` 中内置的 MCP 客户端 |
| 令牌交换 | 由 Harness 服务完成 | 由 Agent 自己完成:workload identity token → `GetResourceOauth2Token(oauth2Flow="M2M")` |
| 执行角色 | `agent_iam._uses_gateway()` | **同一个**——它只看 `tool.type`，从不看 `spec.method` |
| Cedar | 在 Gateway 处 | 同样在 Gateway 处 |

runtime 这一侧能跑起来靠三件事，三件都是必需的:

1. **必须存在 workload identity 令牌。** 只有当调用方在 `InvokeAgentRuntime` 上带了
   `runtimeUserId` 时，Runtime 才会注入一个（`WorkloadAccessToken`）。调用链**只**为 spec
   中带 gateway ToolRef 的 Agent 发送它，因此其他所有 Agent 的调用毫无变化。已实测:不带它
   时客户端会打出 `NOT injected` 并以无工具状态运行。
2. **来自 `settings.resources` 的环境变量**——`LAUNCHPAD_GATEWAY_URL` / `_PROVIDER` /
   `_SCOPE`，由 `runtime_environment()` 仅为 gateway spec 注入，且仅在三者全部解析成功时
   注入（半套环境变量看上去像是配好了，却会以令人困惑的方式鉴权失败）。
3. **按构造失败降级。** 生成的客户端里每一处有风险的 import 都写在函数内部，每条失败路径
   都记录日志并返回中性值，因此没有任何模块级语句能抛异常。import 期崩溃比缺少工具更糟:
   部署管道的健康信号仍会把 Agent 报成 `active`，而之后每一次调用都会失败。

Harness→runtime 转换出于同样这三条理由保留它的 gateway 工具。
`POST /api/agents/{agent_id}/convert`（`routers/agents.py` 里的 `convert_agent`）只接受
处于 *active* 的 `harness` Agent，返回 `202` 与 `{agent, job_id, deployment_id}`:它绝不
修改源 harness，而是新建一个名为 `<source>-rt` 的 Agent。具体工作在
`services/harness_convert.py` 里完成。`resolve_agentcore_cli` 找到 bootstrap 安装在
`data/agentcore-cli/` 下、由本仓库托管的 `@aws/agentcore` CLI，`export_harness` 在一个
可复用的临时项目里、以一个唯一的目标 Agent 名执行它的
`export harness --build CodeZip`，随后把生成的目录树读进内存并删除——真正的存档产物是
spec 的 `code_bundle`。`build_conversion_spec` 把 Launchpad 的配置捆绑契约嫁接到导出的
`main.py` 上，这一步是必需的而不是修饰:导出代码把 `DEFAULT_SYSTEM_PROMPT` 写成常量，
因此未经嫁接的转换做 A/B 实验时会像 harness 一样空转，所以嫁接锚点缺失会让整次转换失败，
而不是发布一个静默无法 A/B 的 Agent。产出的 `zip_runtime` spec 把源 harness 的 gateway
`ToolRef`、技能前缀、memory 与知识库配置一并带过来，在 `conversion_notes` 里记下哪些被
接通，并写上 `source_harness`，使 `experiment_capability` 判定新 Agent 具备实验资格。
v1 那条「gateway MCP 未接线」的说明是被删掉了，不是被改了措辞。

一个被路由的配置捆绑会让 runtime **和** Gateway 双方各以自己的角色去解析该捆绑，因此按
Agent 的执行角色*和* `launchpad-gateway-role` 上都需要 `GetConfigurationBundleVersion`。
runtime 侧缺它会让调用从内部 500;Gateway 侧缺它会让 MCP 调用返回
`HTTP 400 "Config bundle fetch failed"`，而 Agent 会静默地丢掉所有 Gateway 工具。两处授权
都已到位，这正是配置捆绑 A/B 能够改变一个 *Gateway* 工具描述的前提。

仍然是 harness 独有的部分:zip runtime 上的远端（`type: "mcp"`）服务器，以及 container 方式
上的 Gateway 工具。

公开 `/v1` 接口额外加了 `X-Api-Key` 鉴权(密钥以 sha256 哈希存储);分派之后的
一切与控制台路径完全相同。每个 Agent 的响应都带一份由后端拥有的 `invoke_capability`;
控制台调用、Chat 与 `/v1` 强制的是同一份投影。导入进来的 runtime 走带缓冲的兼容路径，
因为 Launchpad 无法假定一个任意的外部 runtime 会发出生成代码那套 Claude SDK 事件契约。

Harness、Claude Agent SDK container 以及生成的 Strands zip runtime Agent 都会流式输出
模型原生的增量。Claude container 启用 SDK 的 partial message，而 Strands zip 模板从一个
异步生成器入口驱动 `Agent.stream_async`;两者经 AgentCore Runtime 的 SSE 响应产出同一组
`delta`、`tool` 与 `complete` 事件（长时间工具调用期间还有 `heartbeat` 帧）。平台增量地
解析 Runtime 的 `StreamingBody` 并在不等 EOF 的情况下转发这些事件，因此一个 zip Agent 的
token 与工具调用在 Chat 里的呈现与托管 Harness Agent 完全一致。同步调用消费同一个事件
解析器并把增量拼接起来。Studio runtime、A2A runtime 以及处于活跃状态的金丝雀 Gateway
路由保留带缓冲的兼容路径;用旧模板部署出的 zip runtime 仍然只回一个 JSON 结果，同一个
解析器会把它渲染为单条增量。已存在的 runtime 必须重新发布才能采纳被改动过的生成模板。

AgentCore 会把已存在的 runtime session 固定在最初服务它的那个版本上,因此重新发布后的
验证必须开启新的 Chat 会话;旧会话继续跑在原来的镜像上。涉及的版本可以在 Agent 详情的
「版本与端点」面板中看到(`GET /api/agents/{id}/versions`)。

Chat 也可以**显式结束**存活的 runtime
会话：「结束会话」（位于「新会话」旁，历史栏每行也有）经
`POST /api/chat/{id}/sessions/{session_id}/stop` 调用数据面 `StopRuntimeSession`，
然后清掉当前 id，下一条提示词即从新会话开始。仅按「新会话」只在本地忘掉 id，留下的
runtime 会话会自行空闲过期。只有 runtime 支撑的 agent 才能结束；托管 Harness 没有结束
会话的操作（409 `chat.session_stop_unsupported`）。`ChatSession` 行保留并打上
`ended_at`，历史栏据此显示「已结束」，而对话记录仍可回放。

## 既有 Gateway 治理

`/governance` 直接从 AgentCore 读取 MCP Gateway、目标、Policy Engine、策略与 Registry 记录。
打开一个 Gateway 是只读的。选择**纳管**只会加上这两个持久标签:

```text
agentcore-launchpad:managed = true
agentcore-launchpad:managed-by = agentcore-launchpad
```

Registry 导入与 Policy 变更要求带有该标签并且 `updatedAt` 是新鲜的。取消纳管只移除这两个
标签，绝不解除或删除 Gateway、Engine、Policy 或 Registry 资源。

Registry 与 Harness 的边界刻意分开。一条 Gateway MCP 记录包含整个 Gateway 的工具目录。选中
该记录就是把整个 Gateway 挂到一个 Harness 上;具体动作由 Cedar 策略授权。AWS_IAM 与免鉴权的
Gateway 解析为 `awsIam` 与 `none`。Launchpad 自有的 CUSTOM_JWT Gateway 复用它配置好的 OAuth
provider。没有纳管 provider 映射的外部 CUSTOM_JWT Gateway 只能停留在目录层面。

策略决策证据来自 `AWS/Bedrock-AgentCore` 的 CloudWatch 指标（`AllowDecisions`、
`DenyDecisions` 以及 determining/mismatch 这一族），AgentCore 默认就会发布它们——不需要按
Gateway 逐个启用。`app/services/governance_evidence.py` 拥有这次读取，同时供给限定范围的
决策端点与切换闸门背后真实的 `evidence_count`;该闸门只统计 LOG_ONLY 模式下的决策，与文档化
的晋级规则一致。`available=false` 现在只保留给不可读的通道（并报告 AWS 错误码）;通道可读但
窗口内一片安静时是 `available=true` 加 `evidence_count=0`，而零证据晋级仍然要求输入 Gateway
名称并记录一条理由。

这个指标通道的两个性质塑造了整份契约:

- **只有聚合值。** 指标维度无法携带 principal、判定理由或 trace id，所以 `decisions[]` 保持
  为空，也绝不被合成出来。逐条决策行需要 Policy span，而它确实要求在挂接的 Gateway 上启用
  trace 投递。
- **计数基准按操作不同。** `AuthorizeAction` 发布的是 gateway 级别的数据流（每次调用一条
  决策）;`PartiallyAuthorizeActions` 实测只发布 `ToolName` 投影（每个调用/工具对一条决策）。
  因此每个操作各自解析自己的维度投影，并报告它所依据的 `basis`。AWS 会为同一个事件发布若干
  彼此重叠的投影，所以选择时匹配的是精确的维度名集合——跨投影求和会让计数翻上几倍。

逐条决策行来自那个 span 通道，由 `app/services/governance_spans.py` 解析。行的来源是
`AgentCore.Gateway.InvokeTool` 这个 SERVER span，它同时携带 `tool.name` **和**
`aws.agentcore.policy.authorization_decision`;子 span `AgentCore.Policy.*` 补上
determining/mismatched 策略 id 以及 `aws.agentcore.policy.log_only_matched_policies`——一个
未公开的属性，它能从 ENFORCE 模式的 span 里揭示一条 LOG_ONLY *候选*策略本会匹配到什么，而
这是指标通道无法表达的。`session.id` 需要按 `traceId` 联接的第二趟查询。有三个性质是承重的:

- **`principal` 在结构上就取不到。** trace 中没有任何 span 携带 principal，因为 Harness 是用
  OAuth M2M 客户端凭证向 Gateway 认证的——请求没有人类主体。该字段渲染为「已解释的缺失」，
  绝不推断。本地演示台账保留它自己的 principal，两者不会被混为一谈。
- **`PartiallyAuthorizeActions` 的拒绝是列举期的工具可见性判定**，不是被拦下的调用:在
  ENFORCE 下该工具会被从 `tools/list` 中过滤掉，模型根本看不到它。行上带一个 `evaluation`
  类别（`invocation` / `tool_listing`），因此两者不会被当成同一种事件呈现。在 ENFORCE 下，
  列举期拒绝是唯一可能出现的 DENY。
- **span 绝不重新定义 `evidence_count`。** span 是采样的，而指标是精确计数，所以闸门用的
  数字始终来自指标;span 通道故障时降级为仅用指标（`spans_unavailable_reason`），而不是让
  请求失败。

决策响应还会独立报告实时投递配置，即 `span_channel_status`（`ready`、`missing` 或
`unknown`）加 `span_channel_reason`。一次成功但零行的 Logs Insights 查询并不能证明 Gateway
tracing 已经配置好:`ready` 要求存在预期的 TRACES 源、XRAY 目的地以及把两者连起来的
delivery。这次探测是只读的;GET 路由绝不修复 AWS 资源。

span 通道是需要主动开启的那一半，而且是**按 Gateway** 的:只有在挂接的 Gateway 上启用了
trace 投递之后，AgentCore 才会发出 Policy 决策 span。那是一条 CloudWatch vended-log
delivery（源 `logType=TRACES` → `XRAY` 目的地 → delivery），不是一个 Gateway 设置，所以启用
它从不调用 `UpdateGateway`。`make bootstrap` 会启用共享的 Transaction Search 前置条件，但
刻意不创建这条 Policy 专用的 delivery。`policy_bootstrap.ensure_gateway_traces()` 仍然是
供显式运维工具调用的幂等原语;正常 bootstrap 从不调用它。控制台的投递状态探测是只读的，在
操作者主动开启详细 Policy span 之前，通道缺失都是预期状态。

### Gateway 限流

网关详情的「限流」面板通过 `/api/governance/gateways/{id}/rate-limits` 下的四条同步路由
（`GET` 列表、`POST` 创建、`PUT /{rate_limit_id}` 更新、`DELETE /{rate_limit_id}` 删除）管理
AgentCore Gateway 限流（2026 年 8 月 GA）。封装函数（`list_gateway_rate_limits`、
`create_gateway_rate_limit`、`update_gateway_rate_limit`、`delete_gateway_rate_limit`）放在
`app/services/agentcore/policy.py`，与其他 Gateway 控制面调用并列，显式接收 control client；
列表会跟完所有 `nextToken` 分页。读取对任意 Gateway 可用；所有变更都要求 Launchpad 纳管标签
（`409 governance.gateway_not_managed`，与策略变更同一规则）。

一条限流规则 = 一组固定且有序的**维度键** + 最多 1000 个**条目**；每个条目为每个键给一个值
（`*` 表示任意）并为每个指标给一个速率。`validate_rate_limit_spec` 在任何 AWS 调用之前校验
文档规则，失败返回 `422 governance.rate_limit_invalid` 并带稳定的 `detail.reason`：

| 规则 | `detail.reason` |
|---|---|
| 1–10 个键，每个取自 `targetName`、`toolName`、`qualifiedModelId`、`$.context.jwt.<claim>`、`$.context.iam.principal`、`$.context.iam.sourceIdentity`，不得重复 | `dimension_keys_count`、`dimension_key_unknown`、`dimension_key_duplicate` |
| 1–1000 个条目；每个条目的 `dimensions` 必须恰好是父级键集合，值不能为空 | `entries_count`、`entry_dimensions_mismatch`、`entry_dimension_empty` |
| `*` 只能出现在尾部位置（某个值为 `*` 后，其后每个键都必须是 `*`） | `wildcard_not_trailing` |
| `requests` / `tokens` / `connections` 至少一个，每个指标恰好一个速率配置 | `entry_no_metric`、`rate_config_count` |
| `rate` 0–10 000 000；`requests` 按 `second`/`minute`，`tokens` 仅 `minute`，`connections` 仅 `second` | `rate_out_of_range`、`period_not_allowed` |
| 描述 ≤ 512 字符；更新时不得携带 `dimensionKeys` | `description_too_long`、`dimension_keys_immutable` |

AWS `ConflictException`（同一键集合已有限流规则，或 Gateway 正忙）经共享的 `ClientError`
信封映射为 `409 aws.conflict`。与策略变更不同，这里没有 202/operation 跳转：`PolicyChange`
行（`rate_limit.create` / `rate_limit.update` / `rate_limit.delete`；`before` = 变更前的限流规则
或 `{}`，`requested` = 校验后的载荷，`after` = AWS 响应）在调用前以 `running` 写入、调用后
内联收口为 `succeeded`/`failed`，因此审计视图能列出它，调用中途崩溃也会留下可见的行。面板
明示文档语义——生效速率 = min(服务托管上限，配置值)、约 30 秒内生效、故障放行（fail-open）、
速率 0 拦截全部匹配流量、在 Policy **之前**评估——在客户端镜像尾部 `*` 与周期矩阵规则，并通过
共享 `Btn` 的 `disabledReason` 解释被禁用的操作（未纳管 / Gateway 非 READY / 限流规则非 ACTIVE /
表单无效）。无需 IAM 变更：控制台角色已具备 `bedrock-agentcore:*`。

### 目标同步

网关详情「目标」表的每一行都显示 `lastSynchronizedAt`（AWS 从未同步过则显示 `-`）；对**已纳管**
Gateway 上的**动态 MCP 服务器目标**，还提供「同步」操作。`POST
/api/governance/gateways/{id}/targets/{target_id}/synchronize` 调用
`SynchronizeGatewayTargets(gatewayIdentifier, targetIdList=[target_id])`——服务端会对目标端点
重新执行 MCP `initialize` + 分页 `tools/list`（配置了 Identity 凭证时会带上），目标进入
`SYNCHRONIZING`，随后变为 `READY` 或 `SYNCHRONIZE_UNSUCCESSFUL`。封装函数
`synchronize_gateway_target` 位于 `app/services/agentcore/policy.py`；路由返回 `202` 与目标投影
`{id, name, status, status_reasons, description, listing_mode, last_synchronized_at,
synchronizable, not_synchronizable_reason}`——`gateway_detail` 现在对每个目标返回同一形状，
控制台从不自行推导 AWS 规则。

两道门禁在**任何 AWS 调用之前**执行：Gateway 必须带纳管标签（`409 governance.gateway_not_managed`），
且目标必须可同步，否则返回 `409 governance.target_not_synchronizable` 并带稳定的 `detail.reason`：

| 规则（来自 SynchronizeGatewayTargets 参考文档） | `detail.reason` |
|---|---|
| 必须存在 `targetConfiguration.mcp.mcpServer`——Lambda / OpenAPI / Smithy / connector 的 schema 天然是静态的 | `not_mcp_server` |
| 静态 `mcpServer.mcpToolSchema` 会禁用同步 | `static_tool_schema` |
| `CREATE_PENDING_AUTH` / `UPDATE_PENDING_AUTH` / `SYNCHRONIZE_PENDING_AUTH` 在操作者完成授权前会被拒绝 | `pending_auth` |
| 已处于 `SYNCHRONIZING` | `synchronizing` |
| 其他过渡状态（`CREATING`、`UPDATING`、`DELETING`）；同步要求 `READY`、`SYNCHRONIZE_UNSUCCESSFUL`、`UPDATE_UNSUCCESSFUL` 或 `FAILED` | `not_ready` |

该调用与限流变更完全一致地内联记入审计：一行 `PolicyChange`（`target.synchronize`；`before` =
调用前的目标投影，`requested` = `{target_id, target_name}`，`after` = AWS 返回的目标）以 `running`
写入，再收口为 `succeeded`/`failed`。AWS `ConflictException` 经共享 `ClientError` 信封映射为
`409 aws.conflict`，绝不会变成 500。这里没有 operation 行：同步受理后，只要仍有目标处于
`SYNCHRONIZING`，控制台就每隔数秒重新拉取详情（约 2 分钟后放弃），并在
`SYNCHRONIZE_UNSUCCESSFUL` / `FAILED` 徽标下方显示 `statusReasons`。被禁用的「同步」按钮通过共享
`Btn` 的 `disabledReason` 说明原因（未纳管 / 目标类型 / 待授权 / 已在同步 / 操作进行中）。
不在范围内：列举动态目标的工具（控制面不返回）、创建或更新目标、批量同步。无需 IAM 变更——
控制台角色已具备 `bedrock-agentcore:*`。

### 目标类型

Gateway 目标未必是 MCP 工具提供方：所锁定的 `bedrock-agentcore-control` 模型中，
`TargetConfiguration` 是三路联合——`mcp{openApiSchema, smithyModel, lambda, mcpServer,
apiGateway, connector}`、`http{agentcoreRuntime, passthrough, connector}`（HTTP 直通 /
AgentCore Runtime 目标）和 `inference{connector, provider}`（推理目标）。因此目标投影带有
`kind: {protocol, variant}`，由 `app/services/governance.py` 中的纯函数 `target_kind` 依据 AWS
实际设置的联合成员推导，例如 `{"protocol": "mcp", "variant": "lambda"}`、
`{"protocol": "http", "variant": "passthrough"}`、`{"protocol": "inference", "variant":
"provider"}`。投影刻意保持宽容：空的 `targetConfiguration` 为 `{"protocol": "unknown",
"variant": null}`，锁定模型尚不认识的联合成员映射为 `protocol: <key>` / `variant: null`，
绝不抛异常。详情页「目标」表在「类型」列显示该类型（按 `(protocol, variant)` 于
`governance.targetKind.*` 下本地化，未知组合回退为等宽的 `protocol/variant`）；非 `mcp`
目标的「同步」禁用原因会直接点出类型（「……该目标类型为 HTTP 直通」），而原因码仍为
`not_mcp_server`；`mcp` 下除 `mcpServer` 之外的变体保持原有文案。`discover_actions` 不变——
只有 MCP schema 携带工具——因此 `gateway_detail` 还会为每个 `http` / `inference` 目标返回
`actions_uncovered_targets: [names]`，面板以一行提示渲染（「N 个目标不提供工具 schema：……」），
避免把空的「动作」单元格误读为发现失败。

## 控制台路由

控制台只有一张 `react-router-dom` 路由表(`frontend/src/App.tsx`),全部嵌在同一个
`<Shell />` 元素之下,由它持有侧栏、顶栏(面包屑)和页脚。各模块是顶层路由,模块内
的子页面走 `?view=` 查询参数,不用嵌套路由。路由表末尾有一条位于 Shell 组**内部**的
`path="*"` 兜底路由:未匹配的 URL(拼写错误、指向已下线子路由的旧书签)会渲染
`pages/NotFound.tsx` — kicker、标题、等宽字体显示的请求路径,以及返回总览的主按钮 —
并保留整套外壳,而不是只剩背景网格。面包屑在 `layout/Shell.tsx` 中推导:路径若与
`ROUTE_PATHS`(`layout/nav.ts`,与路由表保持一致)中任何一项都不匹配,就使用
`nav.notFound`;否则取最长前缀匹配的导航项。新增路由时,`<Route>` 表和 `ROUTE_PATHS`
都要加。

**页面按需加载。** 除首页(index)路由之外，同一张表里的每个模块都是一个 `React.lazy`
边界:入口 chunk 只带外壳——React、路由、i18n、共享组件与 `lib/api` 层——某个路由的代码
只在真正导航到它时才下载。于是 Studio 画布（`@xyflow/react` 加 monaco loader）、
markdown/highlight 栈以及另外十二个页面都不再出现在首次访问里:入口 chunk 从 1,936 kB
降到约 600 kB（压缩前的 minified 体积），旁边是每个页面各自的 chunk，以及供 Chat 与可观测
会话详情共用的一个 `Markdown` chunk。`Overview` 与 `NotFound` 仍然是静态导入:给首页路由
单独切一个 chunk 只会让第一次绘制多一个往返，而且没有别的页面会用它；兜底页只有几百字节，
却正是一个未匹配 URL 立刻就需要的东西。`layout/RouteChunk.tsx` 就是 Shell 包在 `<Outlet />`
外面的那个边界，位置在 `.view` **内部**，所以侧栏、顶栏与页脚不会发生位移:chunk 还在路上时
它渲染一行已翻译的等宽提示（`routeChunk.loading`），而不是一片空白。若这次 import 失败，
它渲染共享的 `LoadError` 区块，动作按钮为「重新加载」而非「重试」（`routeChunk.failed` /
`routeChunk.reload`，走 `LoadError` 的 `retryLabel` 属性）。这种失败是预期内的、并不罕见:
chunk 文件名带内容哈希，因此在标签页开着的时候重新构建那台机器（生产环境用 `vite preview`
提供已构建的 `dist/`，见 [agent-runbook-prod.md](agent-runbook-prod.md)）就会让已加载的外壳
所请求的那个哈希消失，而重新加载就是全部的修复动作——dev/preview 服务器消失时同理。这个边界
**只**对被拒绝的动态 import 给出该诊断（按各浏览器的措辞匹配错误信息）；页面抛出的其他错误
一律原样重新抛出，因此真正的渲染缺陷仍然像以前那样暴露出来。边界以 `location.pathname` 为
key，所以导航离开即清除上一个页面的失败态，而 `?view=` 的变化不会重新挂载页面。只有内置的
DCV live-view chunk（它本来就是懒加载的，见 `pages/governance/ToolsView.tsx`）超过 Vite 的
500 kB chunk 警告线，`vite.config.ts` 中的 `build.chunkSizeWarningLimit` 只被抬高到刚好覆盖
这一个 chunk（2900 kB），因此入口 chunk 或任何页面 chunk 一旦劣化，这条警告仍会触发。

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

有两道守卫按顺序执行,回答的是不同的问题。

**这个控制台是否允许处于开放状态?** 未认证的控制台只服务 loopback 调用方,其余一律
`403 auth.open_console_refused`。它按**每个请求**检查而不是在启动时检查,因为请求是
唯一能知道调用方地址的地方——`create_app()` 看不到 uvicorn 的 `--host`,所以仅靠启动
检查会被"直接跑 uvicorn"绕过,而 EC2 主机和容器恰恰就是这么启动的。该检查使用传输层
对端地址,绝不读 `X-Forwarded-For`(可伪造)。在真实 socket 上实测:来自非环回对端的
伪造 `X-Forwarded-For`、`X-Real-IP`、`Forwarded`、`Host` 头全部被拒。残留风险比"信任
localhost"更窄:uvicorn 的 proxy-header 中间件(默认 `forwarded_allow_ips=127.0.0.1`)
会在对端**确实是环回**时用 `X-Forwarded-For` 改写对端地址,因此同主机代理只要设置了该
头,被评估的就是真实客户端并会被拒;只有**不设置**转发头、却在转发远端流量的本机代理
才仍显得像本地。无论哪种情况,该分支在真实生产路径上都不会触发,因为那里认证是开启的。`LAUNCHPAD_ALLOW_OPEN_CONSOLE=true` 表示接受该风险;`create_app()` 与
`start.py` 另外会快速失败,让配置错误在启动时就暴露。

**这个调用方是否允许访问这个路由?** 网关启用后,中间件要求所有 `/api/*` 路由都有活跃
会话,仅放行 `/api/health`、`/api/auth/status`、`/api/auth/login`、
`/api/auth/register`;中间件不管 `/v1/*`,其 `X-Api-Key` 契约保持权威。角色授权则来自
**一张声明式表** `backend/app/core/route_policy.py`,由单个 app 级依赖强制执行:

- 用依赖而非中间件,因为 `scope["route"]` 只有在路由匹配后才写入——这样检查读到的是
  准确的 `path_format`,而不必重新实现路径匹配(在 FastAPI 0.139 的 `_IncludedRouter`
  包装下同样成立,这也意味着枚举路由时必须递归);
- **默认拒绝**:没有登记项的 `/api` 路由会抛 `auth.route_unclassified` 而不是放行,
  因此新端点不可能在未授权的状态下上线;
- `tests/test_route_policy.py` 枚举实际路由并在两个方向上检测漂移,这才是让这张表真正
  可信而非流于形式的原因。

分类原则:**admin** 用于会执行代码、改变已部署或云端状态、签发凭证、或改变治理策略的
路由;**member** 用于读取,以及成员与智能体自身的交互。调用智能体
(`/api/agents/{id}/invoke`、`/api/registry/a2a-demo`)刻意保持 member 可达——这与 Chat
已经给每个成员的能力完全相同,Chat 开着却锁 invoke 保护不了任何东西。

实际效果是 `member` 接近只读。在数据**尚未**按用户隔离的前提下这是有意为之:所有已登录
账户看到同一批 agent、知识库与链路,因此一个能部署的成员同时也能修改其他人的资源。
仅管理员可用的模块(`/users`、`/create`、Studio 画布、注册表的注册/编辑)会渲染"需要
管理员权限"面板而不是发出请求;`auth.forbidden` 也映射进了 `apiErrors` i18n 块,因此
任何漏加门禁的界面仍会显示本地化的原因。

这里刻意没有提供关闭这张表的开关——能关掉授权的开关本身就是漏洞。

会话 Cookie 的 `Secure` 与 HSTS 响应头都跟随 `run_mode == "prod"`;
`LAUNCHPAD_AUTH_COOKIE_SECURE=true` 可在开发模式下强制开启 `Secure`。两者都没有硬编码
为开启,因为明文 HTTP 开发源上的 `Secure` Cookie 不会回传,而那里的 HSTS 头会把
`localhost` 粘死到 HTTPS。不设置密码则对 loopback 保持网关关闭(控制台开放、注册返回
`auth.registration_disabled`、`/api/users*` 以隐式本地 admin 身份可达),保持免引导的
本地开发与测试流程。

## 托管知识库（控制台 04）

`/knowledge-bases` 是接地（grounding）层，也是唯一一个背靠 **Bedrock** 而非某个
AgentCore 服务的控制台模块：*托管* 的 Bedrock 知识库就是全托管 RAG——向量库、
嵌入与重排都归服务所有。它由 `backend/app/services/knowledge.py` 拥有（之下是
`bedrock-agent` 控制面与 `bedrock-agent-runtime` 数据面），对外暴露为
`/api/knowledge-bases/*`（`backend/app/routers/knowledge.py`，逐条列表见
[api.zh-CN.md](api.zh-CN.md)「控制台知识库 API」一节）。这里没有知识库台账表：
状态全在 AWS，平台本地只存每个 Agent 上的 `AgentSpec.knowledge_bases` 引用。

**资源模型。** `create_kb` 以
`knowledgeBaseConfiguration.type = "MANAGED"` 与
`managedKnowledgeBaseConfiguration.embeddingModelType = "MANAGED"` 调用
`CreateKnowledgeBase`——索引侧没有任何可配置项——`roleArn` 取工作区资源映射里的
共享引导角色 `kb_role_arn`（该键缺失时 `create_kb` 直接拒绝，提示「run its
bootstrap」）。文档经由 `MANAGED_KNOWLEDGE_BASE_CONNECTOR` 类型的 S3 数据源进入
（`_data_source_configuration`：桶名与 `bucketOwnerAccountId` 放在
`connectorParameters.connectionConfiguration` 下，可选前缀作为
`filterConfiguration.inclusionPrefixes`，解析策略为 `SMART_PARSING`），每个数据源
由 ingestion 作业建立索引（`StartIngestionJob` / `ListIngestionJobs`），检索则是带
`managedSearchConfiguration` 的 `bedrock-agent-runtime.retrieve`。只有
`type == "MANAGED"` 的知识库在范围内：列表摘要不带类型，因此 `list_kbs` 对每个 id
读一次 `GetKnowledgeBase` 并丢弃其余类型；所有按 id 的路径都会走
`_require_managed`，对账号内确实存在的 VECTOR 知识库返回 `kb.not_found`。

**创建返回 `202`，尾巴在请求之外收。** 知识库需要 1.5–3 分钟才离开 `CREATING`，
而它的数据源必须等到 `ACTIVE` 之后才能创建，所以 `POST /api/knowledge-bases` 直接
以 `202` 返回仍处于 `CREATING` 的详情，外加 `source_pending` 描述符；
`_start_source_completion` 在守护线程里轮询 `GetKnowledgeBase`（间隔 10 秒，
截止 15 分钟，自带 client——请求的 client 不能活得比请求更久），一旦知识库转为
`ACTIVE` 就创建数据源。此前的实现是在请求内轮询，被 ~60 秒的代理源超时从中间切断，
浏览器随后的文件上传就静默丢失了。也正因如此，数据源创建有三处可能相互竞争的入口
——创建路径、该后台线程、手动 `POST …/data-sources`——所以 `_create_data_source`
先调 `_find_data_source_at`：它比对每个既有连接器解析出的（桶，前缀），命中就直接
返回，因此同一个 S3 位置不可能产生第二个连接器。客户端轮询
`GET /api/knowledge-bases/{kb_id}`，并在数据源报告 `AVAILABLE` 后自行发起首次
ingestion。

`?view=` 之下挂着两个子页面（`frontend/src/pages/KnowledgeBases.tsx`）；列表是默认
视图，而 `?view=detail&kb=` 指向的 id 若已解析不到，会走共享的失效深链接提示，而不是
永远停在 LOADING。

| `?view=` | 展示内容 |
|---|---|
| `create` | 名称、描述，以及数据源选择器——待上传的文件，或一个既有的桶 + 前缀（`CreateView.tsx`、`SourcePicker.tsx`）。提交时：先创建，再（上传模式下）用 `POST …/files` 推送所选文件，然后直接跳到新知识库的详情页 |
| `detail&kb=<id>` | 概览（id、ARN、更新时间、描述就地编辑、删除）、已挂载的 Agent、数据源——桶/前缀、状态、最近的 ingestion 作业及其统计，以及可折叠的按数据源文档分页（`ListKnowledgeBaseDocuments`，按 token 分页，每行的索引状态还联结了 S3 侧的大小与上传时间）——以及检索 Playground（`POST …/query`，1–100 条结果，带分数与来源 URI）。只要还有动作在进行中，页面每 5 秒轮询一次；对已 `AVAILABLE` 但还没有作业的数据源自动触发首次同步；当一个 `ACTIVE` 知识库压根没有数据源时，给出告警并提供「补建数据源」按钮 |

**数据源。** `_resolve_source` 接受两种模式。`upload` 指向平台 artifacts 桶的
`kb/{kb_id}/` 前缀，也正是 `upload_files` 写入的位置——文件可以先于连接器落地；而
数据源全在别处的知识库会以 `kb.no_upload_target` 拒绝上传。`existing` 取调用方给的
桶与可选前缀，并对两者做校验（`_validate_external_source`）：桶名必须符合 S3 自身
的命名规则，前缀必须是字面路径——因为二者会被直接插进下面的授权 ARN，桶名里的 `*`
或 `/` 会把该授权从一个桶放大到整个账号。

**按知识库的 IAM。** 自带的桶还得让知识库角色读得到，所以 `_create_data_source` 会
调 `_sync_kb_policy`：在 `kb_role_arn` 指向的角色上放一条内联策略
`launchpad-kb-<kb_id>`——`<bucket>/<prefix>*` 上的 `s3:GetObject`，加上桶级的
`s3:ListBucket`，后者在设置了前缀时带 `s3:prefix` 条件（`_kb_policy_document`）。
artifacts 桶会被跳过——引导阶段已经授权过一次。删除时 `_delete_kb_policy` 再把这条
策略摘掉。`roleArn` 本身在创建时并不会被校验，因此错误的 `kb_role_arn` 只会在
ingestion 失败时才暴露。

**删除。** 只要还有 Agent 的 spec 挂载着这个知识库，`delete_kb` 就以
`409 kb.has_attached_agents` 拒绝（阻塞的 Agent 名在 `detail.agents` 里）。
`force=true` 会先跑 `_strip_kb_from_agents`：把该知识库从每个挂载它的 spec 里摘掉，
并且**只**为 harness 类 Agent 重新同步按 Agent 的网关目标——zip/container Agent 本
就没有这种目标，为它们动网关只会*创建*一个永远用不到的目标。随后尽力删除各数据源、
移除按知识库的 `Retrieve` 目标（仅在网关已存在时——删除流程绝不去预置它）、摘掉内联
策略，最后调 `DeleteKnowledgeBase`；仍在 `CREATING` 的知识库会返回
`409 kb.delete_conflict`。已部署的 harness 会保留其过时的提示词段落，直到下一次
重新发布——这无害，因为对应工具已经不再指向那个已死的知识库。

**两条挂载通道，按创建方式选择。** `AgentSpec.knowledge_bases` 最多容纳 10 个
`KnowledgeBaseRef`（`kb_id` 加上反规范化的名称/描述，这样提示词与详情视图都不必再
回访 Bedrock）；spec 校验器允许 `harness`、`zip_runtime` 与 `container`，拒绝 Studio
画布与 `protocol="a2a"`。

- **网关通道——方式B（harness）。** `services/kb_gateway.py` 拥有一个共享的 MCP
  网关 `launchpad-kb-gw`（入站 Cognito-JWT 鉴权，出站 `GATEWAY_IAM_ROLE`，连接器为
  `bedrock-knowledge-bases`），上面挂两类目标：按知识库的 `Retrieve` 目标，命名为
  `<kb-slug>-<kb_id>`（每个知识库一个，全局可见）；以及按 Agent 的
  `AgenticRetrieveStream` 目标 `agentic-<agent>`，其 `retrievers` 恰好是该 Agent 的
  那几个知识库，基础模型与重排模型类型都是 `MANAGED`。每个 `ensure_*` 都是按名字
  「不存在才创建」，并且 retrieve 目标在遇到 `ConflictException` 时会接管并发发布者
  的胜者，而不是让本次发布失败。harness 部署器的 **provision** 阶段负责引导网关
  （`ensure_kb_gateway_persisted`，它把 `kb_gateway_{id,arn,url}` 持久化到工作区）、
  确保按知识库的目标、同步按 Agent 的目标，然后重新渲染 `CreateHarness` 请求——首次
  挂载时 `generate` 跑在网关存在之前——并以 `CLIENT_CREDENTIALS` 出站鉴权把它作为
  `agentcore_gateway` 工具挂上。`harness.py::_kb_prompt` 会追加一段
  `## Knowledge bases` 提示词，点名网关的 MCP 工具（`…___Retrieve`、
  `agentic-…___AgenticRetrieveStream`）。网关是惰性创建的：在第一个挂载知识库的
  harness 部署（或显式的 `POST /api/knowledge-bases/ensure-gateway`）之前，没有任何
  东西会去预置它。
- **直连通道——方式A（container）与 `zip_runtime`。** 不经网关；生成的运行时自带两个
  工具，用 Agent 自己的执行角色直接调 Bedrock 数据面：`kb_search` → `Retrieve`
  （一次相似度检索，不调用基础模型），以及 `kb_deep_search` →
  `AgenticRetrieveStream`（一个规划循环：拆解问题、跨所有已挂载知识库检索——单个知识
  库最多 3 轮、多个最多 5 轮——并返回带引用的答案）。两种方式的一切都取自
  `templates/kb_support.py`，因此不会各自漂移：`mounted_kb_refs` 把知识库字面量烤进
  生成的源码，`kb_prompt_section` 追加在两个工具之间做取舍的提示词段落；容器把它们以
  `mcp__launchpad_kb__<tool>` 的命名空间形式暴露。授权是 `ManagedKbRetrieval`
  （`bedrock:Retrieve` + `GetKnowledgeBase`，由 `services/agent_iam.py` 的按 Agent
  角色收窄到已挂载的知识库 ARN）与 `ManagedKbAgenticRetrieval`
  （`bedrock:AgenticRetrieveStream`，刻意为 `*`——该动作无法按资源收窄），两者都定义在
  `infra/stacks/base_stack.py`；同一对语句也挂在 `launchpad-gateway-role` 上，供网关
  通道使用。

`launchpad-kb-gw` 属于「引导邻接」而非引导创建，因此拆除脚本按名字连同它的目标一起
清扫——见 [teardown.zh-CN.md](teardown.zh-CN.md)。

## 记忆控制台(控制台 05)

`/memory` 是共享 `launchpad_memory` 单例的**只读**视图
(`backend/app/services/memory_console.py`,接口位于 `/api/memory/*`)。它与
`app/services/memory.py` 刻意分离:后者位于聊天调用热路径上、保持精简;控制台
模块负责控制面读取、actor 解码、命名空间解析与分页,并从 `memory.py` 导入
`SCOPE_SEP` / `memory_id_or_none`,使分区契约只有一个来源。

只读是结构性的,而非界面层的拦截:两个文件中都不存在 `CreateEvent`、
`DeleteEvent`、`DeleteMemoryRecord`、`Batch*MemoryRecords`、
`StartMemoryExtractionJob`、`CreateMemory`、`UpdateMemory`、`DeleteMemory` 的
封装或处理函数,`tests/test_memory_console.py` 会断言这一点。唯一会写的界面——
`resources` 视图——因此放在**独立的一对模块**里(`services/memory_admin.py` +
`routers/memory_resources.py`,由 `tests/test_memory_resources.py` 覆盖):它只管理记忆
*资源*本身(创建/更新/删除),从不触碰事件或记录,控制台模块上的结构性保证保持不变。

| `?view=` | 展示内容 | AgentCore 操作 |
|---|---|---|
| `overview` | 资源配置(id/arn/状态/事件过期/KMS/执行角色)、每条长期策略及其 `namespaces` + `namespaceTemplates`、以及账号内其他记忆资源(标出平台单例) | `GetMemory`、`ListMemories`、`ListActors` |
| `short-term` | actor → session → event 三级下钻;事件以时间轴呈现对话轮次的角色/文本，JSON 载荷（`{json: {content}}`）显示为带标签、可展开的 JSON 块，blob 载荷只显示字节数 | `ListActors`、`ListSessions`、`ListEvents` |
| `long-term` | 解析出的命名空间下的记录,以及带相关度评分的语义检索 | `ListMemoryRecords`、`RetrieveMemoryRecords` |
| `resources` | 账号/区域内的全部记忆(标出工作区默认记忆,以及 spec 绑定了每个记忆的 Agent);创建记忆(名称、描述、事件过期、与引导布局一致的策略选择,以及——目前仅 API 可用——最多 5 个灵活命名空间变量键:CreateMemory `namespaceKeys`,控制台表单通过 `ResourcesTab.tsx` 的 `SHOW_NS_KEYS` 隐藏该编辑器);行内编辑描述与事件过期(7–365 天)——`UpdateMemory` 只发送 `memoryId` 加实际改动的字段,**绝不**发送 `namespaceKeys`(API 文档写明该字段整体替换现有集合,漏掉的键会被删除),随后用 `GetMemory` 读回详情;策略、命名空间变量与执行角色不可编辑,编辑不会被阻止,确认对话框会列出使用该记忆的 Agent(缩短过期窗口会影响它们全部);删除记忆——工作区默认记忆与仍被在线 Agent 引用的记忆受删除保护 | `ListMemories`、`GetMemory`、`CreateMemory`、`UpdateMemory`、`DeleteMemory` |

`ListEvents` 的载荷条目是一个标签联合：`conversational`、`blob`，以及自 2026 年 8 月
Memory 发布起新增的 `json`（`{json: {content: <任意 JSON 值>}}`）。控制台把每条投影为
`{kind, role, text, parts, blob_bytes}`：`json` 条目的 `role` 保持为 null（它是 Agent 存下的
数据，不是对话轮次），值原样序列化到 `text`，因此 `false`、`0`、`null`、`""`、数组与对象都
按其本身显示——投影检查的是 `content` 键是否存在，而不是值的真假。blob 字节仍不会离开服务端，
平台不认识的联合成员仍被省略。这只是**读取**投影：平台不会写入 JSON 事件。

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
| 旧版 `aws/spans` + 统一的 `/aws/bedrock-agentcore/runtimes/*` 日志组 | 追踪/会话列表、仪表盘计数 + p50/p95 + 分时序列、热门工具、Span 树 | Logs Insights `SOURCE logGroups(namePrefix: ...)`,每个视图一组有界查询 |
| 在线评估结果日志组 `/aws/bedrock-agentcore/evaluations/results/<configId>` | 会话详情的「在线评估」区块(每个配置的分数 + judge 解释) | 一条前缀 `SOURCE` 的 Logs Insights 查询按 `attributes.session.id` 过滤,在缓存的会话构建内作为独立调用执行,失败时降级为 `unavailable` |
| `bedrock-agentcore` 指标命名空间 | 各模型 TOKEN 用量卡片与图表 | `ListMetrics`(发现维度)→ `GetMetricData` 对 `gen_ai.client.token.usage` 求和 |
| AgentCore Memory `ListEvents` + ChatMessage 台账 | 会话对话转录 | 通过 ChatSession 联结(`session_id → actor_id`);优先读取 Memory,并用精确渲染消息台账修复延迟、不完整或历史 actor 分区漂移;解码 harness 消息信封并丢弃工具结果轮次 |

每个视图都由 **60 秒 TTL 缓存**(按视图 + 时间范围)提供服务 —— Logs Insights
按扫描量计费 —— `force=true`(⟳ 刷新按钮)可绕过缓存。时间范围为白名单
(`1h/6h/24h/7d`);trace id(`^[0-9a-f]{32}$`)与 session id
(`^[A-Za-z0-9_\-#:.@]{8,256}$`,`#`/`:`/`.`/`@` 用于兼容外部调用方拼接的
`<ulid>#feishu#<chat_id>` 这类复合 id)在路由层校验,并在查询构造器中**再次校验**后才会
插入 Logs Insights 查询字符串。TOKEN 求和按框架只选择一个携带用量的 Span:
Strands 统计终端 LLM 操作(`chat` / `text_completion` /
`generate_content`),Claude Agent SDK 统计原生 OpenInference `AGENT`
根 Span。Strands 的 agent 级 `invoke_agent` Span 与框架 wrapper 会重复
子级/provider 用量,因此仍排除。
统一日志组还包含 prompt、OTel event、结构化日志和标准输出；所有基于 Span
的查询都要求存在 `startTimeUnixNano`，避免带 trace 关联信息的非 Span 记录
抬高 trace、延迟、错误、token 或工具调用统计。

成本为**参考估算**:token 数 × `config/launchpad.yaml` 中的 `model_prices`
(每百万 token 的美元价,按子串匹配 `gen_ai.request.model` 或原生
`llm.model_name`;未知模型只显示 token 数,成本为 `—`)。界面以
`≈ / EST` 标注。价格表通过 litellm 的公开
价格文件保持更新(`app/services/model_prices.py`):每日守护线程 + 仪表盘的
「⟳ 更新价格」按钮(`POST /api/observability/prices/refresh`)会为账户遥测中
出现过的每个模型拉取精确条目(含 Bedrock 区域溢价与缓存读写价),刷新运维
维护的短键,未匹配的键保持不动。来源 URL 与周期可配置
(`model_prices_source_url`、`model_prices_refresh_hours`,设 `0` 关闭守护线程)。

**各创建方式的遥测:** Strands(zip/studio)与 harness Agent 原生发射 gen_ai
span。Claude Agent SDK 容器安装 AgentCore 已支持的
`openinference-instrumentation-claude-agent-sdk`,并继续通过
`opentelemetry-instrument python main.py` 启动 ADOT。插桩会把 SDK 的
`query()` 调用记录为 `ClaudeAgentSDK.query`,原生发射 AGENT/TOOL
OpenInference span,并自动发射同 scope 的结构化 content event 承载输入输出消息;
模型、token、缓存 token、成本与工具数据保留在原生 span 上。
运行时用 `using_session(context.session_id)` 包住每次查询,因此原生 span 的
`session.id` 与 Chat、Evaluation、Observability 使用的平台 session 一致,不会
被 Claude CLI 内部 session id 替换。Evaluation readiness 按 span id 配对完整的
原生 span 与自动 content event;Strands 遥测继续使用相同的 root + content 契约。

页签结构:**仪表盘**(5 个统计卡片 + 流量/延迟/TOKEN/工具图表)·
**会话**(列表 → 含记忆转录与会话内追踪卡片的详情)·
**追踪**(可筛选列表 → 瀑布甘特图 + Span 抽屉:含缓存读写的 token 用量、
预估成本、工具 schema、原始属性)。交叉链接:深链
`/observability?trace=<id>` / `?session=<id>`;Chat 的追踪面板可跳到当前
会话详情(`在可观测中打开 ↗`),会话详情也可跳回(`在对话演练场打开 ↗`);
`service.name` 通过台账映射为平台 Agent 名称(`resource_id` 基名匹配,
回退为原始名称)。

**即时评分(SCORE NOW)。** 会话详情可以立刻用 1–5 个 evaluator(内置、第三方托管
或自定义——与运行向导同一份列表)对该会话打分,走的是 AgentCore **数据面
`Evaluate`** API——这是页面的第四个数据来源,也是该模块唯一的"类写入"调用。
`POST /api/observability/sessions/{id}/evaluate` 先对 `SPANS_SOURCE` 运行一条
Logs Insights 查询(`filter ispresent(scope.name) and attributes.session.id =
"<id>" | fields @message | sort @timestamp asc | limit 2000`),把每条
`@message` 解析为 span 文档(非 JSON 的行——与统一日志组共用的 stdout、结构化日志——
会被跳过),再**按 evaluator 逐个、顺序**调用
`evaluate(evaluatorId, evaluationInput={sessionSpans})`(每次调用都是一次 judge
模型推理;每次最多 10 条结果)。封装在 `services/agentcore/evaluation.py`,客户端就是
调用链使用的那个 `bedrock-agentcore` 数据面客户端。契约:同步、**不缓存也不持久化**
(面板会明确提示;台账永远看不到这些结果)、仅会话级(没有 `evaluationTarget`,
没有 ground truth),部分失败以**错误行**返回(每条结果自带
`error_code`/`error_message`)而不是整次请求失败;span 尚未落地的会话返回
**409 `observability.session_spans_missing`** 并附就绪提示(调用完成后 span 需要
几分钟才会到达 CloudWatch)。适合试跑自定义 evaluator 或排查某个会话;需要留档、
可复现的分数请用下文的批量运行。

## Workspaces —— 多账号/多区域环境

控制台管理的每一个环境都是一个 **workspace**:一对 `(account_id, region)`（带 UNIQUE 约束），
在台账行（`workspaces` 表）上携带它自己的一整套 AgentCore 资源映射。中枢最初的那个环境作为
保留的 `default` workspace 存续，它的行在每次启动时镜像 `config/launchpad.yaml`;其他所有
workspace 都以台账行为权威，并由一个控制台驱动、可恢复的 **bootstrap 作业**
（`POST /api/workspaces/{id}/bootstrap`，十个幂等阶段:validate-access → iam → storage →
codebuild → cognito → gateway → memory → registry → observability → finalize）来开通。
`validate-access` 会拒绝一个已经承载了别人的 Launchpad 部署的区域，而 IAM 角色只有在带
`launchpad:workspace` 标签时才会被接管。位于**另一个账号**的 workspace 会带上 `role_arn` +
`external_id`:中枢扮演该角色（自动续期的一小时会话，按 `(account, region, role)` 缓存），
并且为该 workspace 发出的每一次调用——bootstrap、CodeBuild 构建、invoke、CloudWatch 读取
——都用它签名。spoke 角色以纯 CloudFormation 形式提供
（`infra/spoke/launchpad-workspace-role.yaml`）;开通流程与信任边界上的取舍见
[cross-account-workspaces.md](cross-account-workspaces.md)。`POST /api/workspaces/preflight`
（注册表单上的「测试访问」按钮）会在任何东西被记录之前，用一次 AssumeRole +
`GetCallerIdentity` 探测这一对参数——被拒绝时返回 `ok: false`，并附上与 bootstrap 阶段会打印
的同一条诊断信息，因此一个填错的 ExternalId 会在一秒内被发现，而不是等一次失败的开通运行。

**请求边界。** 控制台请求用 `X-Workspace` 头指明自己的 workspace（前端一个 `window.fetch`
包装器会全局盖上它;管理员回落到 `default`，成员回落到自己唯一的授权）。解析发生在应用级的
route-policy 依赖内部——授权检查（成员需要一条 `user_workspaces` 行;管理员绕过）、对变更类
方法的就绪度门禁，以及给处理函数用的 `request.state.workspace`。路由默认按 workspace 限定
范围;只有中枢全局的前缀（`/api/auth`、`/api/users`、`/api/workspaces`）豁免，并且有漂移
测试双向强制这个分类。所有按环境划分的台账表都带一个 `workspace_id` 列;查询按它过滤，因此
一个外部的资源 id 会返回 404。公开 `/v1` 接口完全忽略该头:一个 API 密钥只授权它自己所属的
那个 workspace。

**后台工作**（部署阶段、评估运行、实验、金丝雀、策略对齐）从它所属的那条持久化的行重新
构造 `WorkspaceContext`，绝不从环境中的设置里取;并且只要有任何一条按范围划分的行缺少
`workspace_id`，启动就会拒绝启动。用户与控制台认证保持中枢全局。授权可以从两侧编辑:在
Users 页面按账号编辑（审批时会分配授权，`PATCH /api/users/{id}` 会替换该账号的整份列表），
或在 Workspaces 详情视图按 workspace 编辑——它的成员表在服务端分页、搜索与过滤
（`GET /api/workspaces/{id}/grants`），并能在一次调用里对所选集合授予或撤销（同一路径上的
`PUT`）。两者都只写 `user_workspaces`;管理员从不作为其中的行存在，因为他们靠角色就能触达
每一个 workspace。

**移除。** `DELETE /api/workspaces/{id}` 是一次解除关联——行与它的授权一起消失，AWS 不受
影响——并且只要还有任何按范围划分的行指向该 workspace，它就会被拒绝。这道门禁同时也会困住
一次*失败的*注册:它的 bootstrap 留下的那一行 job 会挡住解除关联，并占住那个
`(account, region)` 槽位，于是该环境无法被重新注册。`POST /api/workspaces/{id}/purge`
（管理员;`?dry_run=true` 预览各表行数）在一个事务里删除按范围划分的行、授权与那条行本身，
并且只对从未真正可用过的 workspace 允许执行:状态为 `registered` 或 `failed`、没有 Agent、
且不是 `default`。一次失败的运行已经开通出来的东西会留在目标账号里——响应中的
`resource_keys` 会说明那是哪些资源种类。

## Skill Lab —— 技能评估与训练（SkillOpt 集成）

Skill Lab 闭合了一个其他控制台界面都不提供的环路:一条 Registry 技能记录在真实的 AgentCore
Runtime microVM 上，针对一个携带评分量表的任务集接受评估，由内置的
[SkillOpt](https://github.com/xiehust/SkillEvalOpt_Studio) 训练环路（rollout → reflect →
aggregate → select → update → gate）优化，改进后的 SKILL.md 再作为一个次版本号递增的版本
发布回同一条记录——随后即可挂载给 Agent。

**内置引擎，只走子进程。** `vendor/skillopt/` 是 SkillOpt 研究框架的一个裁剪子集（上游 pin
与每一处本地补丁都记录在 `vendor/skillopt/LAUNCHPAD_DEVIATIONS.md` 中;值得一提的补丁:一个
基于 Converse API 的 `bedrock_chat` 评审/优化器后端，让 LLM 评审零密钥地跑在实例角色上;以及
一个 pin 了 claude CLI 的 worker Dockerfile）。后端进程**从不 import** 这棵内置的树——
`evaluate_skill.py` / `train.py` 以子进程形式跑在一个专用 venv（`data/skill-lab-venv/`，由
bootstrap 开通）里，环境变量走白名单。一条守卫测试强制这条边界;任务集校验会 shell 出去调用
CLI 用的同一个 `load_tasks`，因此 API 的接受标准永远不会与 CLI 的接受标准漂移。

**执行拓扑。** 编排子进程留在后端主机上（评审与优化器调用直接打到 Bedrock）;每个任务的
Agent rollout 各自跑在 `launchpad_skill_lab_worker` runtime 上自己的 AgentCore Runtime
microVM 会话中（托管会话存储，5 分钟空闲 / 8 小时生命周期，镜像按内容寻址进共享的
`launchpad-agents` ECR 仓库，由共享的 CodeBuild 项目构建）。在此能力出现之前 bootstrap 过的
workspace 只会把 Skill Lab 显示为未开通——worker 的 resource key 刻意是可选的。

**控制台界面**（`/skill-lab`，`?view=tasksets|eval|train`）:任务集（train/val/test 划分，
行级校验直接用内置校验器自己的定位信息）、评估作业（任意状态的 Registry 技能或临时上传的
zip，日志实时跟随，逐任务的硬/软评审结果，产物浏览器）、训练作业（实时分数曲线与带
ACCEPT/REJECT 闸门判定的步骤时间轴，SEED→BEST 差异对比，为被中断的运行提供从 checkpoint
恢复），以及发布（经记录更新路径做次版本号递增;记录会落回 DRAFT——界面上给出可选的
重新批准）。Registry 抽屉通过「在 Skill Lab 中评估」链接到这里。

**保存前审阅生成的任务。** 生成作业成功后写出不可变的 `out/generated_tasks.json`；控制台把它渲染为
可编辑的审阅列表（每行：`id`、`question`、`rubric`、可选的 `task_type`、排除/恢复开关，以及只读的
「documents」标记），**操作员点击保存之前不会写入任何内容**——保存为新任务集
（`POST …/import-taskset`）或追加到扩展目标（`POST …/apply-expansion`）。保存请求只携带一份*选择*：
`tasks: [{index, id?, question?, rubric?, task_type?}]`，其中 `index` 是该行在 `generated_tasks.json`
中的位置，四个字段是唯一接受的作者编辑（`extra="forbid"`，`index` 为严格整数、不做类型强转，各字段有
长度上限，行数不超过 `MAX_TASKS_PER_SPLIT`）。未出现在选择中的行被排除；省略的字段保留生成值；
`task_type: ""` 清除该字段。服务端从作业自己的产物重建其余所有字段——`files`、`target_skills`、
`attachments` 声明——然后走原有流水线（剥离派生字段 → 绑定快照附件 → 校验器子进程 → 暂存区交换），
因此客户端无法借审阅植入文件描述符、路径或评审契约。错误的选择（为空、索引越界或重复、编辑后 id 重复），
以及扩展时与**任一**现有分割冲突的 id，都会在写入前被拒绝并保持作业未导入状态；不带 `tasks` 的请求
（或旧的无请求体 apply）仍会原样保存全部生成行。草稿只存在于客户端：切换作业会重置，状态轮询或切换语言不会；
保存进行中时编辑器、重置与保存按钮均被锁定；操作员切换作业、离开该界面或回到同一作业之后才返回的结果会被丢弃
（按视图代数计数器判断，作业副作用每次运行与清理时都会递增），不会驱动控制台导航；服务端写入本身保留。
**保存之后作业页展示的是生成器的原始输出，只读并明确标注**——控制台不保存选择的回执；被排除或编辑过的行只存在于
任务集中，页面会链接到它（`imported_taskset_id`，或扩展目标）。被拒绝的保存按错误码加结构化 `detail` 本地化
（重复/冲突为 `{ids}`，错误引用为 `{reason, index, count}`），因此中文界面不会显示英文的服务端句子，也不会丢失其中的 id。

台账:`skill_lab_tasksets` + `skill_lab_jobs`（按 workspace 限定范围）;产物存放在
`data/skill-lab/` 之下（任务文件、作业日志、CLI 的 out/ 目录树——内容的事实来源是这些文件，
不是台账）。

**产物浏览器。** `GET /api/skill-lab/jobs/{job_id}/artifacts?path=`（目录列表，或上限
512 KB 的文本读取）与 `GET /api/skill-lab/jobs/{job_id}/artifacts/raw?path=`（逐字节下载）
对调用方 workspace 所拥有的任意作业开放，不限状态：尚未写出
`out/` 的作业返回空的根目录列表，已消失的子路径则是 404。服务端的路径守卫（`_safe_resolve`：
拒绝绝对路径、`~`、反斜杠与 NUL，两侧都做 resolve，因此被植入的符号链接无法扩大窗口）是唯一
权威；控制台从不请求其之外的任何路径。控制台对排队中、运行中与已结束的作业都显示浏览器，
并且**从不轮询目录树**——列表与打开的文件都带有「已于 … 刷新」的时间戳和手动刷新按钮，刷新
失败时保留上一次成功加载的内容并标注其加载时间，而不是清空。每个响应都会与其发出时的作业、
路径和请求代次核对，因此上一个作业、目录或文件的迟到响应不可能落到当前选择上（包括查看器
已关闭之后）。`.md` 产物提供「预览／源码」切换：预览复用 Chat 的渲染栈（GFM 表格、围栏代码
高亮、不加载 `rehype-raw`——产物中的 HTML 是惰性文本），但在 `skillLab/ArtifactMarkdown.tsx`
中采用更严格的链接策略——相对链接以当前文件所在目录为基准在 `out/` 内解析
（`artifactLinks.ts`）并经同一受 workspace 限定的 API 打开，http/https/mailto 链接在新标签页
打开并带 `rel="noreferrer noopener"`，其他协议、绝对路径、越出根目录、百分号编码格式错误或
暗含编码分隔符（`%2F`、`%5C`、`%00`）的链接一律渲染为惰性文本，图片从不抓取（以占位文字标出
来源；树内图片作为产物打开）。源码模式逐字显示服务端截断后、按 UTF-8 解码的文本（不做渲染）；
只有原始下载才是逐字节精确的。截断提示在两种模式下都会显示。

**评估 token 用量。** CLI 写出的 `results.json` 每一行可能携带内置生产者实际观测到的 token
计数：目标 rollout 的 `usage`（来自 exec transcript——claude transcript 给出 `input` /
`cache_write` / `cache_read` / `output` 以及一个 `total`；codex transcript **只**给出总数，
四个分项计数是字面上的零）与评审方的 `judge_usage`（仅 `input` / `output`——agentic 评审
worker 把缓存读写折算进 `input`，chat 评审根本看不到缓存计数）。后端
（`skill_lab/artifacts.py`）逐行校验后投影为 `token_usage.{target, judge}`，再按侧汇总到
summary 上，**不改动**任何评分语义（score 无效的行仍被排除在通过率分母之外，但它们的用量照常
计入——token 确实消耗了）。这层投影刻意从严：没有任何一行上报的计数为 `null`（未知，绝不是
0）；bool、负数、NaN/inf、小数或非数值的计数会被丢弃并把该行标为 `malformed`，而不是折算成零；
`total` 超出分项之和的部分记为 `unattributed`（codex 的仅总数形态，此时其零占位符按未知上报；
只上报 `total: 0` 也仍是一次上报），低于分项之和的 total 则被忽略。原始的 `usage` /
`judge_usage` 保留在行上，仅在文件带有 `NaN`/`Infinity` 字面量时做可序列化处理。summary 侧
区分**上报覆盖度**（`reports_complete`：每一行都干净上报——`rows` / `reported_rows` /
`missing_rows` / `malformed_rows`）与**拆分完整性**（`complete`：上报完整，且凡有任何一行上报过
的计数都被每一行上报了；逐计数的 `counter_rows` / `counter_complete`）。只有部分行上报的计数是
部分求和，控制台标出 `k/n`——一行 claude 加一行 codex 仅总数，永远不会显示为完整拆分。`scope`
恒为 `reported`：这是对上报了用量的任务的观测统计——不是计费总额，控制台也不给出任何费用估算。
控制台在结果磁贴下方以「TOKEN 用量」表格展示，并在展开的任务行中逐任务展示，未知计数一律显示为
破折号。

## SQLite 台账与 job/event 模型

廉价且本地的状态存放在 `data/launchpad.db` 的 SQLite 台账中
(`backend/app/models/ledger.py` 加评估/优化模型):

| 表 | 内容 |
|---|---|
| `agents` | Agent 记录——name、method、status、ARN、resource id、registry record id、version、spec |
| `deployments` | 每次部署一行——五阶段数组,含各阶段 status/detail/时间戳 |
| `jobs` | 异步工作(type `deploy_agent`)——status + 阶段事件的 JSONL `log` |
| `chat_sessions` | Chat 交互 session——轮次、actor、最近活跃时间、显式结束 runtime 会话后的 `ended_at` |
| `users` | 注册创建的控制台账户——用户名/邮箱、pbkdf2 密码哈希、角色、状态(`pending`/`active`/`disabled`)、`expires_at`(审批前为空)、最近登录与登录次数(内置 admin 仅来自配置,不入表) |
| `api_keys` | 公开 API 密钥——sha256 哈希 + 前缀(从不存明文) |
| `policy_decisions` | 治理决策日志——principal、tool、ALLOW/DENY、原因 |
| `eval_datasets` / `eval_runs` | 评估数据集(legacy prompt 或 devguide scenario + 描述 + 最近一次 AWS 同步信息)与运行状态(分数或 insight 树;窗口运行以 `dataset_name="window:<N>h"` 编码范围) |
| `online_eval_configs` | 控制台为某个 agent 创建的在线评估配置——只存标识(config id/ARN/名称、agent、service name、源日志组);状态、rule 与 evaluators 始终从 `GetOnlineEvaluationConfig` 读回。没有行的配置在读取时按名称归类(`exp_*`/`can_*` → 实验持有,其余为外部) |
| `experiments` | 优化闭环——阶段 + 各阶段产物,可恢复 |

**Job/event 模型。** 创建 Agent 返回 `202` 并带一个 `job_id`。部署 job 在后台线程
运行,每次阶段切换向 `Job.log` 追加一条 JSONL 事件;`GET /api/jobs/{id}` 返回这些
事件,`GET /api/agents/{id}` 返回 `Deployment.stages` 数组。随 job 完成,Agent 从
`deploying → active`(或 `failed`)。在任何阶段之外抛出的失败（job 的 workspace 行已不
存在、方式未注册、台账行缺失）会以同样的方式落到 Agent 上:`Job`、`Deployment` 与
`Agent` 三者都被标记为 `failed` 并带上错误，一条 `error` 事件被追加到 `Job.log`，而管道
的 `launchpad.deploy` logger 会把每一次阶段失败或 job 失败都报告到进程日志。权威的资源
状态(runtime 状态、注册记录状态、评估/trace 数据)始终存放在 AWS;台账只保存标识符与
派生的进度。

## 控制台布局断点

控制台以桌面为主,但在 `frontend/src/theme/app.css` 中有两个刻意设计的响应式
层级。**1180 px** 以下,所有双栏网格(`.grid-2`、`.reg-grid`、`.chat-grid`、
`.eval-grid`、治理/可观测网格以及 `.mem-grid-3`)折叠为单栏,其子元素获得
`min-width:0`,因此过宽的子元素(curl `<pre>` 块、很长的键值行)会在自己的面板内
滚动,而不是把网格轨道撑宽。**720 px** 以下,侧栏变为横向导航条,顶栏隐藏身份文字,
并且页面整体绝不允许横向滚动:过宽的内容要在自身容器内滚动或换行。表格是宽度的
主要来源,因此共享的 `DataTable` 组件以及所有不是 `.panel` 直接子元素的原生
`<table>` 都包在 `.table-scroll` 容器中(`overflow-x:auto;min-width:0`,表格放得下
时不产生任何效果);直接位于面板下的表格由 `.panel:has(> table)` 规则覆盖。
工具栏(`.tabs`、`.tabs-actions`)、筛选选择器(`.filters .fsel`、`.fsearch`)、
创建向导步骤条(`.steps`)和列表行(`.histrow`)在该断点下换行或收敛到面板宽度。
新页面应复用 `DataTable` 或 `.table-scroll` 容器,而不是设置页面级宽度;
两个层级都不影响 ≥ 1180 px 的布局。

## 错误信封与 AWS `ClientError` 映射

所有错误都经 `app/core/errors.register_error_handlers` 注册的处理器以 `{code, message, detail}`
信封离开后端;控制台通过 `apiErrors.*` i18n 块(`lib/api.ts` 的 `localizedMessage`)翻译 `code`,
无对应文案时回退到 `message`。服务层预见到的失败以自有错误码抛出 `AppError`(`kb.not_found`、
`agent.not_found`、`memory.unavailable`),它们永远优先——因为在 `ClientError` 逃逸之前就已抛出。

没人预见的 AWS `ClientError`——URL 里写错的 id、IAM 缺口、限流——会在正在签名请求的任意路由上
爆炸,所以只在一处映射而不是逐路由处理:全局 `ClientError` 处理器把 `ResourceNotFoundException`
→ 404 `aws.not_found`、`ValidationException` → 400 `aws.validation`、`AccessDeniedException` /
`UnauthorizedException` → 403 `aws.access_denied`、`ThrottlingException` /
`TooManyRequestsException` / `ServiceQuotaExceededException` → 429 `aws.throttled`、
`ConflictException` / `ResourceInUseException` / `RetryableConflictException` → 409 `aws.conflict`;`message` 去掉 botocore 的
`An error occurred (…) when calling the … operation:` 前缀,`detail` 携带
`{aws_error_code, operation}`。映射是刻意封闭的列表(`AWS_ERROR_MAP`):其他错误码原样重抛,
仍是带完整堆栈的未处理 500,确保真正意外的 AWS 失败依然醒目。跨账号 `AssumeRole` 失败先行判定,
保留 502 `workspace.assume_role_failed` 诊断。Memory 路由的 `memory.unavailable` 包装会放行
可映射的 `ClientError` 到此处理器,因此未知 actor 的 toast 显示本地化的"未找到"文案而不是 boto
原文。`tests/test_errors_aws.py` 固定了这张表;不要为这些错误码再加逐路由的 `except ClientError`。

## 控制台故障态(后端不可达)

控制台绝不会把"读不到"呈现为"账户为空"。两条规则是关键:

- **顶栏健康芯片绑定 `/api/health`。** `useHealth` 在挂载时、每 30 s、以及 `window`
  的 `online` / `focus` 事件时立即探测,并返回
  `{ health, status: "loading" | "ok" | "down", refresh }`。`Topbar` 只在
  `status === "ok"` 时渲染绿色 LED 与 `topbar.allSystemsGo`;探测失败(无响应、5xx、
  开发代理返回的非 JSON 正文)或尚未返回时,渲染同尺寸的芯片、`crit` LED 与
  `topbar.backendDown`。上一次成功的载荷会在故障期间保留,后端重启时区域 / 账户芯片
  不会变空。
- **列表加载失败渲染共享的错误态,而不是空态文案。**
  `components/LoadError.tsx`(也可通过 `DataTable` 的 `error` / `onRetry` 属性使用)
  是唯一的"加载失败:… · 重试"区块;概览(指标卡、发布动态、健康行)、注册表、知识库、
  对话(智能体选择器)、评估运行与实验列表都使用它,与既有的可观测 / 治理错误区块一致。
  "创建你的第一个 …" / "暂无记录" 文案只在 200 返回空列表后渲染;已加载过的行在之后的
  轮询失败时保留,重试按钮会重新发起请求。按路径 fetch 的页面使用 `lib/api.ts` 中的
  `getJson` / `responseMessage` / `errorMessage`,使消息遵循 `apiErrors.*` 本地化规则
  (未收到 HTTP 响应的请求对应 `apiErrors.network`)。

## 失效的深链接会说明资源已不存在

id 不再能解析的深链接绝不会静默回退。共享的 `components/StaleLink.tsx` 是唯一的提示
区块("`<类型>` `<id>` 在当前工作区已不存在 —— 请从下方表格中选择。",`staleLink.*`,
可关闭),`components/useStaleParam.ts` 是与之配套的 hook:调用方传入参数的当前值与
自己的判定 —— 只有列表已加载而其中没有该 id、或详情请求返回 4xx(上文 `ClientError`
映射中的 `aws.not_found` / `aws.validation` / `aws.access_denied`)时才为真 —— hook 记下
id 供提示使用,并通过 `setSearchParams(..., { replace: true })` 一次性去掉该参数,同一
链接不会再次触发,页面随后就是一次普通访问。列表加载失败*不是*判定:该状态归
`LoadError`,参数保留以便重试。已接入的界面:评估 `?view=datasets&ds=`(本地行在本地
列表加载后判定,`cloud:` 行在云端列表加载后判定)、`?view=evaluators&ev=`、
`?view=online&oe=`、`?view=experiment&exp=`,对话 `?agent=`(连同其伴随的 `?session=`
一并去掉),以及知识库 `?view=detail&kb=` —— 缺少 `kb` 时以同样方式提示
(`staleLink.bodyMissing`),而不是永久 LOADING。对话是唯一不得挑选替代品的界面:选择器
停在 `chatPage.pickAgent` 占位项(`value=""`)直到用户选择,因为自动选中的智能体会静默
接收下一条提示词。有效链接仍像以前一样精确选中对应的行 / 智能体。

## 禁用的主操作说明缺了什么

表单的主操作按钮绝不会只是"变暗"。共享的 `components/Btn.tsx` 接受可选的
`disabledReason`;按钮处于 `disabled` 且给出了原因时,渲染 `title={reason}`,并在旁边
渲染一个同级的 `.btn-hint`(等宽字体、`--ink-3`,与 `.dim` 辅助文字同一视觉权重),按钮
通过 `aria-describedby` 指向它。按钮可用时,或未给出原因时,不渲染任何提示元素。原因由
计算 `disabled` 的*同一组*谓词按顺序推导,第一个未满足的谓词即为提示内容;该属性从不
改变按钮*何时*被禁用,只改变控制台对此说了什么。所有原因都是 i18n 键(en + zh-CN)。
目前接入的表单:注册表登记(`▲ REGISTER` — 名称规则 / MCP URL / SKILL.md)、注册表编辑
(`▲ SAVE` — 无更改 / bundle 无效)、知识库创建(`▲ CREATE` — 名称规则 / 无文件 / 无存储桶)、
Strands Studio(`▲ Publish` — 无节点;发布对话框中的名称规则)、在线评估创建
(`▸ CREATE` — 未选智能体 / 未选评估器 / 未选洞察)以及工作区详情的 `RUN BOOTSTRAP`
(hub 工作区 / 正在运行 / 已为 READY)。忙碌态(`saving`、`busy`)刻意不带原因:按钮文字
本身已经说明正在发生什么。

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
