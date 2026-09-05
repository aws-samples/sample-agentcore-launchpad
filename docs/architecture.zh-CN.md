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
| **Runtime** | 托管 zip 与 container Agent(`CreateAgentRuntime`);调用链访问 runtime 数据面。 |
| **Harness** | 托管方式B Agent(`CreateHarness`)——托管入口,无构建产物。 |
| **Memory** | 一个共享的 `launchpad_memory` 单例:短期 session 事件 + 长期语义与用户偏好策略。命名空间只按 `{actorId}` 分区(没有 `{agentId}` 模板变量),因此平台把 Agent id 折进 actor——`scoped_actor(agent_id, human)` → `<agent>__<human>`——从而让**短期事件与长期记录**(`/facts/<agent>__<human>`)都按 Agent 分区。生成的 Strands Runtime 通过 `AgentCoreMemorySessionManager` 恢复短期对话。Claude Agent SDK 容器为每次调用创建独立的 `MemorySessionManager`,通过 `UserPromptSubmit` Hook 注入有界的短期对话及 `/facts/<actor>`、`/preferences/<actor>` 记录,并在调用成功后把 USER/ASSISTANT 对作为一个事件持久化。A2A Runtime 使用 `<agent>__a2a__<contextId>`,因为直接 A2A 调用目前没有经过身份认证的 human actor envelope。一个 Agent 学到的偏好不会串到同一个人的另一个 Agent 或 A2A context;台账仍存裸的 human actor 用于展示。 |
| **Gateway** | `launchpad-gw` 把一个 REST API(office-facts)和一个 Lambda(hr-database)转成带 Cognito-JWT 鉴权的 MCP 工具;Agent 的工具调用经由它流转。治理页为已纳管的 Gateway 管理 **Gateway 限流**（2026 年 8 月 GA）：`ListGatewayRateLimits` / `CreateGatewayRateLimit` / `UpdateGatewayRateLimit` / `DeleteGatewayRateLimit` 位于网关详情的「限流」面板之后，服务端校验并记入 `policy_changes`。 |
| **Identity** | 支撑网关的 token vault——一个 OAuth2 provider(Agent 出站鉴权)和一个 API-key provider。 |
| **Registry** | `launchpad-registry` 编目三类 descriptor:A2A(Agent)、MCP(工具)、AGENT_SKILLS(Skill)。每次部署都会自动创建并提交一条 A2A 记录。控制台也支持手动注册——外部远程 MCP 服务器(streamable-http URL)与技能(SKILL.md → 制品桶)——并驱动完整生命周期:提交 → 批准/驳回(REJECTED 仍可改判批准)、下架(终态——已实测,之后只能删除)、删除。注册中心同时是**挂载目录**:`GET /api/registry/attachables` 只向创建向导提供 APPROVED 的 MCP/技能记录,MCP 记录按 URL 分流——共享网关 URL 挂为 `agentcore_gateway`(OAuth),其他 URL 挂为 `remote_mcp`(暂不带鉴权)——技能按其 s3 路径经 `skills[{path}]` 挂载。 |
| **Policy** | 挂接到网关的 Cedar 策略引擎,初始挂载模式由操作员选择(默认 `ENFORCE`,可选 `LOG_ONLY`);deny 决策会带上作出判定的 policy id。支持 NL → Cedar 策略生成。引用已被删除的引擎时,治理页面显式展示失效引用而不是报错,策略变更返回 409,创建并挂载会替换该引用。 |
| **Evaluation** | 基于 CloudWatch trace 的真实 `StartBatchEvaluation` / insights。运行范围三选一:**数据集**(回放条目——多轮 scenario 在同一 session 内顺序回放)、显式 **session id 列表**、或**时间窗口**(`lookback_hours` 1–336——被动模式:不产生新调用,用 `filterConfig.timeRange` 圈定既有流量)。14 个通用提示词模板评估器(12 个 trace/session 级和 2 个普通工具调用级)、2 个技能 `TOOL_CALL` 提示词模板评估器,外加 3 个仅限真值的程序化 `Builtin.Trajectory*Match` session 级匹配器(仅当数据集 scenario 定义了 `expected_trajectory` 时可选),以及支持完整 CRUD 的自定义 LLM-as-a-judge 评估器——在 `?view=evaluators` 子页创建/编辑(UpdateEvaluator 为全量配置替换)。洞察运行可在三种分析类型(失败归因/用户意图/执行摘要)中任选子集。数据集以 devguide scenario 形式存于 SQLite(`?view=datasets` 子页:scenario 编辑器、JSON/JSONL 导入),一键单向同步为不可变的 AWS Dataset 资源(`AGENTCORE_EVALUATION_PREDEFINED_V1`);scenario 真值(断言/期望回复/期望轨迹)经 `evaluationMetadata.sessionMetadata` 注入批量评估。账户单批次锁与队列语义不变。 **在线评估**(`?view=online`):每个 agent + evaluator 集合对应一个 AgentCore `OnlineEvaluationConfig`,按采样比例(0.01–100 %)在会话空闲超时后对真实会话打分,不产生新的调用;结果写入 `/aws/bedrock-agentcore/evaluations/results/<configId>`(同时以 EMF 指标落到 `Bedrock-AgentCore/Evaluations`),控制台用 Logs Insights 聚合(每个 evaluator 的均值 / 标签分布 / 趋势 / 带 judge 解释的最近记录)。页面列出 workspace 账号内**全部**配置并按归属分类:`agent`(本控制台创建,可全操作)、`experiment`(`exp_*`/`can_*` 实验 arm,只读)、`external`(仅暂停/恢复/删除)。Update 始终发送完整 `rule`(AWS 整体替换),从未被调用过的 agent 创建时会被拒绝(AWS 校验日志组存在)。 配置有两种**模式**:`scores`(evaluators)或 `insights`(1–3 种洞察类型 + 可选的 DAILY/WEEKLY/MONTHLY 聚类——AWS 不允许同一配置两者兼有);insights 配置产出**报告**(以配置为数据源的批量评估:AWS 按聚类周期定期生成,或从控制台「立即出报告」经运行队列发起),通过 `GetBatchEvaluation.dataSourceConfig.onlineEvaluationConfigSource` 归属,并复用运行页的洞察聚类树渲染;报告只覆盖该配置采样过的会话。 在线评分同时出现在查看会话的地方:可观测性的会话详情带一个「在线评估」区块(该会话在所有配置下的结果记录,按归属分类,失败降级——结果查询失败不会影响追踪),概览页新增 **在线质量 · 24h** tile(对 workspace 内 agent 持有配置做极性归一、按计数加权的均值,120 秒缓存,没有配置时不调用 AWS)。二者都通过 `SOURCE logGroups(namePrefix: ['/aws/bedrock-agentcore/evaluations/results/'])` 一次读取全部结果日志组。 |
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
`ABTestOrchestration`(18 个动作,含 `CreateGatewayRule`、`UpdateGateway`、
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

## 既有 Gateway 治理：限流

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
| 旧版 `aws/spans` + 统一的 `/aws/bedrock-agentcore/runtimes/*` 日志组 | 追踪/会话列表、仪表盘计数 + p50/p95 + 分时序列、热门工具、Span 树 | Logs Insights `SOURCE logGroups(namePrefix: ...)`,每个视图一组有界查询 |
| 在线评估结果日志组 `/aws/bedrock-agentcore/evaluations/results/<configId>` | 会话详情的「在线评估」区块(每个配置的分数 + judge 解释) | 一条前缀 `SOURCE` 的 Logs Insights 查询按 `attributes.session.id` 过滤,在缓存的会话构建内作为独立调用执行,失败时降级为 `unavailable` |
| `bedrock-agentcore` 指标命名空间 | 各模型 TOKEN 用量卡片与图表 | `ListMetrics`(发现维度)→ `GetMetricData` 对 `gen_ai.client.token.usage` 求和 |
| AgentCore Memory `ListEvents` + ChatMessage 台账 | 会话对话转录 | 通过 ChatSession 联结(`session_id → actor_id`);优先读取 Memory,并用精确渲染消息台账修复延迟、不完整或历史 actor 分区漂移;解码 harness 消息信封并丢弃工具结果轮次 |

每个视图都由 **60 秒 TTL 缓存**(按视图 + 时间范围)提供服务 —— Logs Insights
按扫描量计费 —— `force=true`(⟳ 刷新按钮)可绕过缓存。时间范围为白名单
(`1h/6h/24h/7d`);trace id(`^[0-9a-f]{32}$`)与 session id
(`^[A-Za-z0-9_-]{8,128}$`)在路由层校验,并在查询构造器中**再次校验**后才会
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
| `online_eval_configs` | 控制台为某个 agent 创建的在线评估配置——只存标识(config id/ARN/名称、agent、service name、源日志组);状态、rule 与 evaluators 始终从 `GetOnlineEvaluationConfig` 读回。没有行的配置在读取时按名称归类(`exp_*`/`can_*` → 实验持有,其余为外部) |
| `experiments` | 优化闭环——阶段 + 各阶段产物,可恢复 |

**Job/event 模型。** 创建 Agent 返回 `202` 并带一个 `job_id`。部署 job 在后台线程
运行,每次阶段切换向 `Job.log` 追加一条 JSONL 事件;`GET /api/jobs/{id}` 返回这些
事件,`GET /api/agents/{id}` 返回 `Deployment.stages` 数组。随 job 完成,Agent 从
`deploying → active`(或 `failed`)。权威的资源状态(runtime 状态、注册记录状态、
评估/trace 数据)始终存放在 AWS;台账只保存标识符与派生的进度。

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
`ConflictException` / `ResourceInUseException` → 409 `aws.conflict`;`message` 去掉 botocore 的
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
