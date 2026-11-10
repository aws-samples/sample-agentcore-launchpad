# sample-agentcore-launchpad → 中国区（cn-north-1 / cn-northwest-1）迁移分析

- 分析日期：2026-10-08
- 分析对象：https://github.com/aws-samples/sample-agentcore-launchpad @ `4e27b1f`（本地副本：`upstream/sample-agentcore-launchpad/`）
- 代码量：backend ≈ 80k 行 Python，frontend ≈ 111k 行 TS/TSX，另有 CDK（infra/）、vendored Strands Studio（apps/studio/）、SkillOpt（vendor/skillopt/）

---

## 0. 结论（TL;DR）

**这不是“改配置 + 换 region”就能迁的项目，而是一次“按中国区能力子集重做”的改造。** 原因有三个硬事实（均来自官方文档/服务目录，2026-10-08 核实）：

| # | 事实 | 对本 repo 的影响 |
|---|---|---|
| 1 | 中国区 AgentCore **只有 6 个组件**：Runtime、Gateway、Identity、Code Interpreter、Browser、Observability。**不可用**：Memory、Policy、Harness、Payments、Knowledge Bases、Evaluations、Optimizations、Registry。 | 本 repo 10 个 AgentCore 服务里有 **6 个在中国区不存在**（Memory/Harness/Registry/Policy/Evaluation/Optimization）。对应的后端模块、控制台页面、bootstrap 阶段都要砍掉或功能开关化。 |
| 2 | **Amazon Bedrock（模型服务）在中国区尚未上线**（服务目录状态 `isBeingPlannedIn`）。Bedrock Knowledge Bases 同样未上线。**既定方案：中国区 Runtime 跨分区远程调用 Global Bedrock 上的 Kimi K3（`global.moonshotai.kimi-k3`）与 GLM 5.3（`global.zai.glm-5.3`）。** | 模型 ID 目录、默认模型要换成这两个；`model_source="bedrock"` 的 Converse 路径可保留，但凭证必须换成 **Global 账号的 Bedrock API key**（中国区 IAM 角色无法签 Global 分区请求），由中国区 AgentCore Identity 托管；Mantle、Claude、GPT、Knowledge Bases 路径仍失效（见 §3.A）。 |
| 3 | **Amazon Cognito User Pools 在中国区不可用**（仅北京区有 Cognito Identity Pools）；中国区 AgentCore 文档明确“Gateway CUSTOM_JWT 不能用 Cognito”、“Runtime 入站授权不支持 Cognito user pool”。 | repo 的用户/组、Gateway JWT 授权、M2M client_credentials、Cedar `OAuthUser` 身份、demo 用户、Chat “以我身份调用”等整条身份链路要替换（见 §3.B）。 |

此外，中国区 AgentCore 还有若干功能级限制与本 repo 直接相关：**Gateway rules 不可用**（A/B 路由依赖）、**ConfigBundle A/B 不可用**、Gateway 入站**不允许 “No authorization”**（必须 CUSTOM_JWT 或 AWS_IAM）、Runtime **不支持 S3 Files 自带文件系统**、Identity **不支持 Private IdP** 及 GitHub/Google 等内置 OAuth 厂商、Gateway **语义搜索/Inference targets/Connectors 目录不可用**。

**可行的中国区落地范围（建议 MVP）**：方式C（Strands Studio → zip runtime）+ BYOC（zip / Dockerfile / 私有 ECR 镜像）→ AgentCore Runtime；Gateway（Lambda / OpenAPI 目标，**AWS_IAM 入站**）；Identity（API-key / 自定义 OAuth2 凭证提供方）；Observability；Chat 演练场 + `/v1` 公共 API；Code Interpreter / Browser 内置工具演示；**方式B 以自建 Harness 保留**（§3.L），**Memory 与 Knowledge Base 以中国区自建后端保留**（§3.H），**Registry 以本地账本自建保留**（§3.I）；**评估 / 优化 / A/B 可在后续阶段以自建评估引擎 + 应用层分流恢复**（§3.J）。模型层为 **跨分区调用 Global Bedrock 的 Kimi K3 / GLM 5.3**（Bedrock API key 经 AgentCore Identity 托管），备选为 OpenAI 兼容自托管端点。

---

## 1. 中国区服务可用性核实结果

> 目标区域已定为 **cn-northwest-1（宁夏）**；下表两区结论相同，§7 为宁夏区账号实测记录。

| 服务 / 能力 | cn-north-1 | cn-northwest-1 | 来源 |
|---|---|---|---|
| Amazon Bedrock AgentCore（整体） | ✅ | ✅ | AWS 服务目录；docs.amazonaws.cn 《Amazon Bedrock AgentCore in AWS China》 |
| AgentCore Runtime / Gateway / Identity / Code Interpreter / Browser / Observability | ✅ | ✅ | 同上 |
| AgentCore Memory / Policy / Harness / Payments / Knowledge Bases / Evaluations / Optimizations / Registry | ❌ | ❌ | 同上（“Capabilities not available in the China Regions”） |
| Runtime：Managed EC2 capacity provider、S3 Files BYO 文件系统、Cognito 入站授权 | ❌ | ❌ | 同上 |
| Gateway：Cognito CUSTOM_JWT、No-auth 入站、Gateway rules、ConfigBundle A/B、语义搜索、Inference targets、连接器目录、WAF | ❌ | ❌ | 同上 |
| Identity：Private IdP；GitHub/Google/Facebook/X/Reddit/Twitch/Dropbox/CyberArk 内置 OAuth | ❌ | ❌ | 同上 |
| **Amazon Bedrock（模型推理）** | ❌ 规划中 | ❌ 规划中 | AWS 服务目录 `isBeingPlannedIn`；docs.amazonaws.cn 无 Bedrock 用户指南 |
| Amazon Bedrock Knowledge Bases (RAG) | ❌ 规划中 | ❌ 规划中 | 同上 |
| Amazon Cognito **User Pools** | ❌ | ❌（Cognito 整体不在宁夏） | docs.amazonaws.cn 《Amazon Cognito in AWS China》：“User Pools are not currently available” |
| AWS CodeBuild | ✅ | ✅ | docs.amazonaws.cn（仅 CodePipeline 集成差异） |
| AWS X-Ray | ✅ | ✅ | docs.amazonaws.cn（“no differences”）；**CloudWatch Transaction Search（`aws/spans`）在中国区是否可用需实测** |
| Amazon ECR / Lambda / API Gateway / Secrets Manager / S3 / IAM | ✅ | ✅ | 服务目录 / botocore `endpoints.json` aws-cn 分区 |
| Lambda arm64（Graviton） | 需实测 | 需实测 | 中国区 Lambda 差异页未提及 arm64 限制 |
| Amazon CloudFront（repo 的 `video_stack` 与控制台入口） | 需 ICP 备案 + 自有域名 | 同 | docs.amazonaws.cn：不能用 `*.cloudfront.cn` 默认域名；**决定：第一版去掉**（§3.K） |

SDK 层面：botocore（最新 develop 分支）的 `endpoints.json` aws-cn 分区里**没有** `bedrock-agentcore` / `bedrock-agentcore-control` / `cognito-idp` / `bedrock*` / `agent-registry*` 条目，但 `bedrock-agentcore` 的 endpoint-rule-set 使用 `{PartitionResult#dnsSuffix}` 模板，因此 `boto3.client("bedrock-agentcore", region_name="cn-north-1")` 会自动解析到 `bedrock-agentcore.cn-north-1.amazonaws.com.cn`，**无需手工 `endpoint_url`**（建议在中国区账号实测一次 `ListAgentRuntimes` 确认）。

---

## 2. 功能模块 × 中国区状态（影响矩阵）

| repo 模块（控制台页 / 后端） | 依赖的 AWS 能力 | 中国区 | 处理方式 |
|---|---|---|---|
| 方式B Managed Harness（`deployer/harness.py`，49 个文件引用 harness） | AgentCore **Harness**、Bedrock/Mantle 模型 | ❌ | **自建 Harness（D1）**：方式B 体验保留，以 Global 区 Harness 导出代码为模板渲染 Strands 运行时并部署到中国区 Runtime（§3.L）；架构师助手保留并改为产出该规格（D7） |
| 方式A Claude Agent SDK 容器（`deployer/container.py`、`templates/claude_sdk_agent/`） | Bedrock 上的 Anthropic Claude（`CLAUDE_CODE_USE_BEDROCK=1`）、npm `@anthropic-ai/claude-code`、CodeBuild | ❌（无 Bedrock；外网 npm） | **删除（D2）**，容器场景由 BYOC 覆盖 |
| 方式C Strands Studio → zip runtime（`deployer/zip_runtime.py`、`templates/strands_agent/`、`apps/studio/`） | AgentCore Runtime、Bedrock Converse 模型 | ✅ Runtime / ❌ 模型 | **保留**，模型层改为 OpenAI 兼容（§3.A） |
| BYOC（`deployer/byoc.py`） | Runtime、ECR、CodeBuild、S3 | ✅ | 保留；修分区/域名解析（§3.D）、镜像源（§3.F） |
| Chat 演练场 + `/v1` API（`services/invoke.py`、`services/chat.py`、`routers/public_api.py`） | Runtime 数据面（SigV4） | ✅ | 保留；去掉 Memory actor 封装、Cognito JWT “以我身份调用”、Harness 分支 |
| Memory 控制台 + 运行时记忆（`routers/memory*.py`、`services/memory*.py`，27 个文件引用） | AgentCore **Memory** | ❌ 服务不存在 | **保留功能，换后端**：中国区自建“Launchpad Memory”后端（S3 会话快照 + OpenSearch k-NN 长期记忆 + SQS/Lambda 抽取），控制台与聊天记忆栏的 API 契约不变，见 §3.H |
| Registry（`services/agentcore/registry.py`、`registry_console.py`、deploy 第 5 阶段 register） | **Agent Registry** | ❌ 服务不存在 | **保留功能，换后端**：用 Launchpad 自己的 SQLite 账本 + S3 技能包 + FTS/OpenSearch 搜索实现本地 Registry，控制台与 deploy 流程契约不变；是 6 个缺失服务中最容易自建的一个，见 §3.I |
| Governance / Cedar Policy（`services/governance.py`、`policy_bootstrap.py`、`policy_identity.py`） | AgentCore **Policy**、Cognito `OAuthUser` | ❌ | **不自建（D3）**：Policy Engine / Cedar / 决策日志下线；保留 Gateway 目标管理、每 agent 工具 allowlist、限流（限流中国区可用性需实测） |
| Evaluation / Online eval / Insights / Recommendations / 实验 A/B / 金丝雀（`evaluation/`，`optimization/`，44 个文件，约 15k 行） | AgentCore **Evaluations**、**Optimizations**、Bedrock judge 模型、**Gateway rules**、**ConfigBundle A/B** | ❌ 服务不存在（judge 模型可跨境） | **可自建，但是整组里最重的一项**：判题器/批量/在线评估以 **Strands Evals**（`strands-agents-evals`，自带对应 `Builtin.*` 的评估器、CloudWatch spans 读取、detectors、模拟器）为引擎，Kimi/GLM 做 judge，结果落本地表；推荐可接 **Strands Harness Optimizer**；推荐沿用 repo 已有的本地 `gepa_lite`；ConfigBundle 改为本地版本化 JSON；A/B 分流从 Gateway rules 下沉到 Launchpad 调用链。见 §3.J |
| Knowledge Bases（`services/knowledge.py`、`kb_gateway.py`、`templates/direct_kb_tools.py.tmpl`） | Bedrock **Managed KB** | ❌ | **自建 RAG 知识库（D4）**：S3 文档 → 切块 → Titan 向量化 → OpenSearch（与 Memory 共用域），恢复 KB 控制台与 `kb_search` 工具（§3.H 末尾） |
| Skill Lab（`skill_lab/`，vendor/skillopt） | Runtime + Claude/Codex CLI + Bedrock judge | ❌ CLI / ✅ Runtime | ~~删除（D5）~~ → **恢复，OpenCode 替代 Claude Code / Codex，模型走 Global Bedrock OpenAI 兼容端点（D16，N13）** |
| Identity / Connections（`routers/identity.py`、`services/identity_providers.py`） | AgentCore **Identity** | ✅（API key / CustomOauth2 可用；Google/GitHub 等厂商不可用） | 保留；厂商下拉去掉中国区不可用项（GitHub/Google）；Cognito 相关建议文本删除 |
| Gateway 共享网关 `launchpad-gw`（`gateway_bootstrap.py`、`templates/gateway_support.py`） | Gateway + **Cognito CUSTOM_JWT** + M2M token | ⚠️ | 改为 **AWS_IAM 入站**（repo 已有 `templates/sigv4_gateway_transport.py.tmpl` 和 `harness_convert.GW_SIGV4_TRANSPORT`，在 zip-runtime 生成路径复用）；或接入自建/第三方 OIDC（Keycloak、Authing 等） |
| Observability（`services/observability.py`、`routers/observability.py`） | CloudWatch Logs Insights over `aws/spans` + `/aws/bedrock-agentcore/runtimes/*`、X-Ray Transaction Search | ✅ Observability / ⚠️ Transaction Search 需实测 | 保留；若 Transaction Search 不可用，只读 runtime 统一日志组，去掉 `aws/spans` 与 `update_trace_segment_destination` |
| Builtin Tools 演示（Code Interpreter / Browser） | AgentCore CI / Browser | ✅（Web Bot Auth Signer 不可用） | 保留；去掉 `browserSigning` 选项。**N17-obs258（用户裁定 2026-10-22「隐藏 web bot auth」，cn/r17-an）**：实测在 cn-northwest-1 与 cn-north-1 以 `browserSigning.enabled=true` 调 `CreateBrowser` 均返回 ValidationException "Browser signing configuration is not enabled in this environment"（未创建资源）——中国分区不提供 Web Bot Auth 签名；`GET /api/capabilities.tools.browser_signing` 按分区为 false，Browser 演示隐藏「启用 Web Bot Auth」开关与签名浏览器选择器，后端对 `web_bot_auth: true` 回 `409 browser.signing_unsupported_in_partition`。**N15-obs16（2026-10-19，cn/r15-h）**：自建 Harness（方式B）现同时渲染两个内置工具——`code-interpreter`（strands-agents-tools）与 `browser`（`harness_builtin_browser.py.tmpl`：`BrowserClient` 开 `aws.browser.v1` 会话 + CDP 读页面，始终 stop）；SDK 1.17.0 的 `generate_ws_headers()` 把流地址写死为 Global 后缀，模板改为按 boto3 数据面端点自签 SigV4（aws-cn 可用）；不渲染的内置工具在 `POST /api/agents` / 审批时即 `422 agent.builtin_unsupported`（harness-template.md §Builtin tools） |
| Workspaces 多账号/多区域（`services/workspace_bootstrap.py`、`infra/spoke/`） | STS AssumeRole、各服务探针 | ✅ | 保留；探针列表去掉 `agent-registry-control`、`cognito-idp`（§3.E） |
| Users / 控制台登录（`routers/auth.py`、`services/users.py`） | 本地密码 + SQLite | ✅ | 保留（与 Cognito 无关） |
| Videos（`infra/stacks/video_stack.py`，CloudFront+S3） | CloudFront | ⚠️ ICP | 可选；中国区需备案域名，建议移除 |

---

## 3. 必须改动的代码点（按类别）

### A. 模型层（既定方案：中国区 Runtime 跨分区调用 Global Bedrock 的 Kimi K3 / GLM 5.3）

**核实到的模型事实（2026-10-08）**

| 模型 | Bedrock 模型 ID | 说明 |
|---|---|---|
| Kimi K3（Moonshot AI） | `global.moonshotai.kimi-k3` | 2026-09-18 GA，所有 Bedrock 区域可用；支持 Converse / Invoke / Chat Completions / Responses，1M 上下文，显式 prompt caching。官方建议新应用用 Chat Completions，但 Converse 可用（Strands `BedrockModel` 走 Converse）。repo 的 `frontend/src/lib/models.ts:67` 已有此条目。 |
| GLM 5.3（Z.AI） | `global.zai.glm-5.3`（或 `us.zai.glm-5.3`） | 2026-10-05 GA，**仅跨区推理档，不支持 in-Region on-demand**；支持 Converse、function calling、流式、显式 prompt caching；1M 上下文 / 128K 输出；**“Access limited to eligible customers”，需联系 AWS 账号团队开通**。 |

**跨分区凭证：这是整个方案的关键点。** 中国区 AgentCore Runtime 的执行角色属于 `aws-cn` 分区，不能对 `bedrock-runtime.us-west-2.amazonaws.com` 做 SigV4 签名，也不能 AssumeRole 到 Global 账号。可行做法：

1. 在 **Global 账号**生成 Bedrock **长期 API key**（Bedrock 控制台 → API keys；本质是绑定 IAM user 的 service-specific credential，仅能调 Bedrock，可设过期），该 IAM user 只授予 `bedrock:InvokeModel*` 于 `global.moonshotai.kimi-k3` / `global.zai.glm-5.3` 的 foundation-model 与 inference-profile ARN。
2. 在 **中国区** AgentCore Identity 创建 **API-key credential provider**（`CreateApiKeyCredentialProvider`，中国区可用）保存该 key；执行角色授予 `bedrock-agentcore:GetResourceApiKey` + 对应 `secretsmanager:GetSecretValue`（repo 已有 `IdentityVaultSecrets` 语句模式）。
3. 生成代码启动时取回 key，构造 `BedrockModel(model_id=MODEL_ID, region_name=MODEL_REGION, api_key=<key>)`（Strands 原生支持 `api_key` bearer 认证，等价于 `AWS_BEARER_TOKEN_BEDROCK` 环境变量 + boto3 `bedrock-runtime`）。`MODEL_REGION` 用 `us-west-2` 或 `us-east-1`（global.* 推理档两处都可入口）。
4. Runtime 保持 `networkMode: PUBLIC`（`services/agentcore/runtime.py:101`），出公网到 `bedrock-runtime.<region>.amazonaws.com`。

需要正视的约束：① **数据跨境**：对话内容从中国区出境到美国区推理，需合规评估；② **时延**：每次模型调用跨太平洋往返，流式输出可缓解体感；③ **GLM 5.3 受限开通**；④ API key 为长期凭证，需轮换流程（Identity provider 可 `UpdateApiKeyCredentialProvider` 原地换 key，无需重新部署 agent）。

**代码改动点**

| 位置 | 现状 | 改法 |
|---|---|---|
| `backend/app/schemas/agent.py:15` `DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-5"`；`:30` `ModelSource = Literal["bedrock","mantle"]` | 默认 Claude；Bedrock/Mantle 两源 | 默认改 `global.zai.glm-5.3`（或 Kimi K3）；去掉 `mantle`；`AgentSpec` 新增 `model_region`（默认 `us-west-2`）与 `model_credential`（Identity API-key provider 名称，默认 workspace 级配置） |
| `frontend/src/lib/models.ts:35-80,101`、`frontend/src/studio/lib/models.ts` | 目录含 Claude/GPT/Nova/Mantle | 目录缩为 Kimi K3 + GLM 5.3（保留 `isCustomModelId` 自定义入口）；`SPEC_DEFAULT_MODEL_ID` 同步 |
| `backend/app/templates/strands_agent/main.py.tmpl:90-130,205-230`、`strands_a2a_agent/main.py.tmpl` | `MODEL_SOURCE in {bedrock,mantle}`；bedrock 分支直接返回字符串 model id（SDK 用执行角色 SigV4 调本区 Bedrock） | bedrock 分支改为显式 `BedrockModel(model_id, region_name=MODEL_REGION, api_key=_resolve_bedrock_api_key())`，key 通过 `bedrock_agentcore.identity` / `GetResourceApiKey` 取回并进程内缓存；删除 Mantle 分支与 `MANTLE_REGION` |
| `backend/app/deployer/zip_runtime.py:426 MANTLE_EXTRA_REQUIREMENTS` | Mantle 额外依赖 | 删除 |
| `backend/app/services/agent_iam.py:190-199,255-270`（按模型生成 `bedrock:InvokeModel` ARN） | 对中国区执行角色授予 Bedrock 权限 | **删除**（Global Bedrock 鉴权看的是 API key 背后的 Global IAM user，中国区角色上的 Bedrock 语句无意义）；改为授予 `bedrock-agentcore:GetResourceApiKey` 于该 provider 的 workload identity，并保留 `IdentityVaultSecrets` |
| `infra/stacks/base_stack.py:206-250`、`services/workspace_iam.py:100-120,320-335`（`BedrockModels`/`BedrockMantle*`/Marketplace 语句） | 同上 | 删除 |
| `backend/app/services/bootstrap.py` / `workspace_bootstrap.py` | 无模型凭证阶段 | 新增 `model-credential` 阶段：读取运维提供的 Global Bedrock API key（环境变量 `LAUNCHPAD_GLOBAL_BEDROCK_API_KEY`，不落盘）→ `CreateApiKeyCredentialProvider("launchpad-global-bedrock")` → 记录 provider ARN 到 `launchpad.yaml` / workspace 资源 |
| `backend/app/core/config.py` | `prompt_opt_*`、`codegen_model`、`skill_lab_*` 全是 Bedrock 模型 ID | Evaluation/Skill Lab 随模块移除；`codegen_backend` 新增 `kimi`（OpenAI 兼容 / Converse 走 Global Bedrock，D6），`codegen_model=global.moonshotai.kimi-k3`，`scripts/setup_exec_env.sh` 注入 `AWS_BEARER_TOKEN_BEDROCK` |
| `backend/app/core/config.py:280-300 model_prices` | 无 Kimi/GLM 价格 | 手工加 `kimi-k3`、`glm-5.3` 单价（观测页成本估算用）；关闭 GitHub 价格自动刷新 |
| `apps/studio/backend/deployment/agentcore/agent_runtime_template.py:145,192`、`debug_streaming.py:17` | `us.anthropic.claude-3-haiku-*` | 改 Kimi K3 / GLM 5.3 + API key |
| `apps/studio/backend/app/models/deployment.py:34,53,89,196,211,228` | 默认 `region="us-east-1"` | 部署区域 `cn-north-1`，模型区域单独字段 |
| `backend/app/services/traces.py`、`optimization/providers/bedrock_lm.py`、`skill_lab/runner.py` 等**控制平面自身**调用 `bedrock-runtime` 的地方 | 用 hub 凭证本区调用 | 随 Evaluation/Skill Lab 移除；若保留任何后端侧模型调用，统一走 `aws_clients.py` 新增的 `global_bedrock_runtime_client()`（`region_name=us-west-2` + 环境变量 `AWS_BEARER_TOKEN_BEDROCK`） |
| `backend/app/system_agents/presets.py:40,59`、`assistant/proposal.py:51`、`evaluation/agentcore_eval.py:31-32` | GPT-6 / Claude judge、境外 Knowledge MCP | 助手与预置 agent **保留（D7）**：模型改 Kimi/GLM，提案目标改为自建 Harness 规格（§3.L）；Knowledge MCP 可达性需实测 |

**方式A（Claude Agent SDK 容器）已决定删除（D2）**：它依赖 Bedrock 上的 Anthropic Claude（`CLAUDE_CODE_USE_BEDROCK=1`），Kimi/GLM 不在其支持范围；Knowledge Bases 也不随此方案恢复（Bedrock KB 在 Global 区可用，但跨境把企业文档送出去做 RAG 不建议，且 Harness 侧 KB 网关挂载依赖 Harness）。

备选方案（如跨境或 GLM 开通受阻）：`model_source="openai_compatible"`，Strands `OpenAIModel` 对接自托管 vLLM/SageMaker 上的 Qwen/DeepSeek 或国内模型 API，凭证同样走 Identity API-key provider。

**2026-10-14 更新（D21 / N15，已实现并合入 `cn/integration`）：** 上表描述的"模型目录写在代码里"（`schemas/agent.py` 的 `PROVIDERS` / `MODEL_CATALOG` 模块字典，以及 N-R6 的 `LAUNCHPAD_SILICONFLOW_MODELS` 环境变量目录）已被**管理员维护的模型注册表**取代：提供方、模型行、每模型能力（tools / streaming / vision / 推理参数 / 输出上限 / judge 预算 / 嵌入维度，`source: builtin|probed|manual`）、变更日志（id 即目录版本）与按工作区的启用开关全部存于账本（`model_providers` / `models` / `model_catalog_changes` / `workspace_model_settings`），由 `services/model_registry.py` 发布为进程内不可变快照，`schemas/agent.py` 只保留函数视图（`allowed_model_ids()`、`providers()`、`model_catalog_table()`、`model_capabilities()` 等）；所有取模型列表的模块（向导 / 经典创建 / BYOC、Studio 节点、架构师预设、评测 judge / actor、推荐与优化器、Skill Lab、Memory / KB 的嵌入模型）均只读该快照。凭证仍只经 Identity：管理员填写 SSM 参数名 `launchpad-<provider>-api-key`，hub 读取一次并创建 / 轮换 Identity API-key provider `launchpad-<provider>`，密钥不进账本、API、日志与 UI；`base_url` 仅 https 且须命中运维设置的主机允许列表并经 SSRF 护栏——这是静态 lint 看不见的数据层面的区域边界。空注册表在首次启动时由内置表播种，行为与 D21 前逐字节一致（parity 测试）；旧的模块字典与 `Settings.siliconflow_models` 已按 C5 删除（M1-e）。本节的 Kimi K3 / GLM 5.3 两行即内置 `bedrock` 提供方的种子行；SiliconFlow 行在用户创建 `launchpad-siliconflow-api-key` 并在页面上注册凭证后可用。详见 `docs/cn/interfaces/model-registry.md` 与 §6.2 D21。

### B. 身份认证层（Cognito 全面替换）

Cognito User Pools 在中国区不存在，涉及 10 个后端文件 + CDK：

| 位置 | 现状 | 改法 |
|---|---|---|
| `infra/stacks/base_stack.py:107-180`（UserPool、Domain、ResourceServer、M2M client、Groups、demo users） | CDK 创建 Cognito | **整段删除**；CfnOutput `UserPoolId/UserPoolClientId/M2MClientId` 删除 |
| `backend/app/services/gateway_bootstrap.py:34-70,115-170` | `CreateGateway(authorizerType="CUSTOM_JWT", discoveryUrl=cognito-idp...)`，M2M allowedClients | 改为 `authorizerType="AWS_IAM"`（中国区禁止 No-auth；AWS_IAM 可用）。如需 JWT，接自建/第三方 OIDC（Keycloak / Authing / 企业 IdP），discoveryUrl 由配置提供 |
| `backend/app/templates/gateway_support.py`、`gateway_tools.py.tmpl`、`identity_tools.py.tmpl` | 生成代码经 Identity 做 Cognito M2M token exchange 再调 Gateway | 对 AWS_IAM 网关改用 `templates/sigv4_gateway_transport.py.tmpl`（repo 已有，目前只在 `harness_convert.py:130-136,912-935` 的 Harness→Runtime 转换中使用），用执行角色 SigV4 直签 `InvokeGateway` |
| `backend/app/services/inbound_auth.py:214-300,306-390` | workspace Cognito issuer、M2M bearer、hosted UI 域名 `*.auth.<region>.amazoncognito.com` | 删除 Cognito 默认值与 M2M；Runtime 入站只保留 IAM（SigV4）或外部 OIDC JWT |
| `backend/app/services/mcp_client.py:35-60` `get_cognito_token` | 后端用 Cognito 用户名密码换 JWT 调 Gateway | 改为 SigV4（boto3 凭证）调用 |
| `backend/app/services/kb_gateway.py:73`、`policy_identity.py`、`routers/identity.py:389-426` | Cognito issuer / `cognito:groups` | 随 KB/Policy 移除；identity 路由去掉 cognito 建议项 |
| `backend/app/services/bootstrap.py:215-300,343,401` `ensure_demo_user_passwords` 等 | 创建 demo Cognito 用户 | 删除 |
| `backend/app/services/workspace_bootstrap.py:328`（探针 `cognito-idp`）、`:860-960`（cognito 阶段） | 探针失败即判定 “region not supported” | 删除探针与 `cognito` 阶段 |
| `backend/app/services/identity_providers.py:74-81` 厂商表 | 含 GoogleOauth2/GithubOauth2 | 中国区不可用项移出下拉（Google、GitHub） |
| `frontend/src/lib/inbound-auth.ts`、`api.ts:2807`、Chat “invoke as me” | 依赖 Cognito JWT | 对应删减 |
| `samples/inbound-jwt/`、`docs/identity*.md` | Cognito 示例 | 替换为通用 OIDC 示例 |

控制台自身登录（`LAUNCHPAD_AUTH_*`、本地 users 表）与 Cognito 无关，可保留。

### C. 中国区不存在的 AgentCore 服务 → 移除 / 功能开关

决策后的归类：**自建保留** — Harness（§3.L）、Memory（§3.H）、Registry（§3.I）、Knowledge Bases（§3.H 末尾）、Evaluations/Optimizations/A/B（§3.J，**D14 纳入为 N12，异步 Runtime worker**）、Skill Lab（**D16 纳入为 N13，OpenCode 执行器**）；**删除** — 方式A、Policy/Governance 的 Cedar 部分、Skill Lab、Gateway rules / ConfigBundle 的 AWS 实现。因已决定 fork 独立分支（D11），删除项直接删代码即可，不必做 feature flag；下表保留为删除/改造清单：

| 功能 | 后端入口（`backend/app/main.py`） | 其它要动的地方 |
|---|---|---|
| Harness（方式B，**改为自建 §3.L**，下列为 AWS 实现的替换点） | `main.py:11` `import app.deployer.harness`；`assistant_router`、`system_agents_router` | `deployer/pipeline.py` 方法注册；`services/harness_convert.py`；前端 Create 向导方式B 卡片；`optimization/canary_harness.py` |
| Memory（**改为自建后端保留**，见 §3.H；下列为若放弃时的移除清单） | `main.py:185-186` `memory_router`、`memory_resources_router` | `services/memory.py`（`scoped_actor` 被 chat/invoke/observability 引用）、`memory_ownership.py`、`memory_strategies.py`、bootstrap `ensure_memory`（`services/bootstrap.py:95-165`）、`workspace_bootstrap` `memory` 阶段、生成模板里的 `AgentCoreMemorySessionManager`（`templates/strands_agent/main.py.tmpl:89`、`strands_a2a_agent/main.py.tmpl:119`）、Claude SDK 模板 hook、前端 `/memory` 页、Chat 右侧记忆栏 |
| Registry（**改为自建后端保留**，见 §3.I；下列为若放弃时的移除清单） | `main.py:180` `registry_router` | `deployer/registration.py`（第 5 阶段）、`services/agentcore/registry.py`、`registry_console.py`、bootstrap `ensure_registry`（`services/bootstrap.py:64-94`）、`workspace_bootstrap:327` 探针、前端 `/registry` 页；`agent_skills_router`（技能附件来自 Registry 记录） |
| Policy / Governance（**Cedar 部分删除，D3**） | `main.py:190` `governance_router` | `services/policy_bootstrap.py`（Policy Engine 部分）、`policy_identity.py`、`policy_denials.py`、`samples/policies/`、前端 `/governance` 页（保留 Gateway 目标/限流子页） |
| Evaluation / Online eval / Pipelines / Experiments / Canaries（**可按 §3.J 自建恢复**，下列为先行移除清单） | `main.py:192-194,196-197` | `backend/app/evaluation/`、`backend/app/optimization/` 整目录；`config.py` 的 `eval_*`、`prompt_opt_*`、`traffic_concurrency`；前端 `/evaluation`、`/v2/eval/*` |
| Knowledge Bases（**改为自建，见 §3.H 末尾**） | `main.py:183` `knowledge_router` | `services/knowledge.py`、`kb_gateway.py`、`templates/direct_kb_tools.py.tmpl`、`kb_support.py`、执行角色 `ManagedKb*` 语句、前端 `/knowledge-bases` |
| Skill Lab | `main.py:195` `skill_lab_router` | `backend/app/skill_lab/`、`vendor/skillopt/`、`config.py` `skill_lab_*`、bootstrap `ensure_skill_lab`（`services/bootstrap.py:315-336`） |
| Gateway rules / ConfigBundle A/B | — | 执行角色 `ABTestOrchestration` 语句（`base_stack.py` 及 `workspace_iam.py`）、`optimization/` 已随 Evaluation 移除 |

### D. 分区与域名硬编码（`arn:aws:` → `arn:aws-cn:`，`amazonaws.com` → `amazonaws.com.cn`）

统计（排除测试）：`arn:aws:` 硬编码 **87 处**（agent_iam.py 24、workspace_iam.py 16、base_stack.py 10、workspace_bootstrap.py 6 ……）；`amazonaws.com` 非 `.cn` 硬编码 **约 45 处**。建议：

| 做法 | 位置 |
|---|---|
| 后端统一取分区：`WorkspaceContext.partition`（用 `boto3.session.Session().get_partition_for_region(region)`），所有 f-string 改 `arn:{ctx.partition}:...` | `services/agent_iam.py`（24 处）、`workspace_iam.py`（16）、`workspace_bootstrap.py:525,661,1043,1049`、`skill_lab/infra.py:63`、`routers/workspaces.py`、`deployer/harness.py`、`services/memory.py`、`knowledge.py`（后几者随功能移除） |
| CDK 改用 `self.partition` / `Stack.format_arn` | `infra/stacks/base_stack.py:230,375-480` 等 10 处 |
| ECR 镜像 URI 解析不要 `split(".amazonaws.com/")` | `backend/app/deployer/byoc.py:156`、`services/agent_iam.py:652`（中国区是 `<acct>.dkr.ecr.cn-north-1.amazonaws.com.cn/<repo>`） |
| 数据面/网关 URL 拼接加 dnsSuffix | `services/agentcore/registry.py:60`、`harness_convert.py:931`、`registry_console.py:676`、`optimization/canary_harness.py`、`frontend/src/lib/inbound-auth.ts:264`（`https://bedrock-agentcore.${region}.amazonaws.com/runtimes/...`） |
| 服务主体 `bedrock-agentcore.amazonaws.com` / `codebuild.amazonaws.com` 等 | 中国分区服务主体仍为 `*.amazonaws.com`，**不用改**（`infra/spoke/launchpad-workspace-role.yaml` 已正确使用 `${AWS::Partition}` 与 `.amazonaws.com` 主体，可作范本） |
| 生成代码里的 `AWS_REGION` 回退 `us-west-2` | `templates/gateway_tools.py.tmpl:49`、`identity_tools.py.tmpl:79`、`strands_agent/main.py.tmpl:49`、`strands_a2a_agent/main.py.tmpl:42`、`claude_sdk_agent/main.py.tmpl:222`、`direct_kb_tools.py.tmpl:22` → 改为无默认或 `cn-north-1` |
| 前端占位文本 | `frontend/src/v2/pages/agents/MethodSections.tsx:528`、`pages/CreateAgent.tsx:2199`（ECR 示例）、workspaces 页面 `placeholder="eu-central-1"` |
| S3 `LocationConstraint`（`workspace_bootstrap.py:609`）对 cn 区已正确 | 无需改 |

### E. Bootstrap / CDK / Workspace 探针

| 位置 | 改法 |
|---|---|
| `infra/app.py:12` 默认 region `us-west-2`；`base_stack.py:47 LEGACY_REGION` | 默认 `cn-north-1`；`regional_role_name` 逻辑可保留 |
| `backend/app/core/config.py:46` `region: str = "us-west-2"`、`config/launchpad.example.yaml` | `cn-north-1` |
| `scripts/bootstrap.py` → `services/bootstrap.py:337-434` `run_bootstrap` | 顺序现为 stack outputs → registry → memory → gateway(Cognito) → policy/xray → skill-lab → demo users。中国区保留：stack outputs → gateway(AWS_IAM) → observability（Transaction Search 实测后决定）→ 写 `launchpad.yaml` |
| `services/workspace_bootstrap.py:68-80 STAGE_ORDER`、`:325-332 _PROBES` | 去掉 `cognito`、`memory`、`registry`、`skill-lab` 阶段；探针去掉 `agent-registry-control`、`cognito-idp`。否则中国区 workspace 会被判定 “region not supported” |
| `infra/stacks/base_stack.py` 中 `AgentExecutionRole` / `GatewayRole` 的策略 | 删除 Bedrock、Mantle、Marketplace、Memory 数据面、Harness、ConfigBundle、PolicyEngine、ManagedKb 语句；保留 ECR pull、Identity vault、Secrets Manager、Logs/X-Ray、Gateway/Runtime invoke、Code Interpreter/Browser |
| `infra/stacks/video_stack.py` | CloudFront 需 ICP；建议移除 |
| CDK bootstrap | `cdk bootstrap aws://<acct>/cn-north-1` 可用；CDK 的 asset 发布依赖 S3，正常 |

### F. 构建链路与境外网络依赖（中国区 CodeBuild / Runtime 访问 Docker Hub、GitHub、npm 不稳定）

| 位置 | 现状 | 改法 |
|---|---|---|
| `backend/app/templates/claude_sdk_agent/Dockerfile:3,10,12` | `FROM python:3.12-slim-bookworm`（Docker Hub）、`deb.nodesource.com`、`npm i -g @anthropic-ai/claude-code` | 方式A 下线；如保留容器模板，基础镜像先镜像到中国区 ECR（`<acct>.dkr.ecr.cn-north-1.amazonaws.com.cn/base/python:3.12-slim`），pip 用 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
| `apps/studio/backend/deployment/agentcore/dockerfile_template:4,14` | `ghcr.io/astral-sh/uv:...`、nodesource | 同上 |
| `scripts/exec-image/Dockerfile:12`、`vendor/skillopt/deploy/agentcore/Dockerfile:10,25` | `public.ecr.aws/...`、`claude.ai/install.sh` | Skill Lab 下线；exec-image 改国内镜像 |
| `backend/app/deployer/zip_runtime.py`（`uv pip compile` / `pip install --platform manylinux_2_28_aarch64`） | 从 PyPI 拉 aarch64 wheel | 后端主机配置 `UV_INDEX_URL` / `PIP_INDEX_URL` 国内镜像；CodeBuild 项目加环境变量 |
| `backend/app/core/config.py:308` `model_prices_source_url`（raw.githubusercontent.com）、`model_prices_refresh_hours=24` | 定时拉 GitHub | 设 `model_prices_refresh_hours: 0`，价格表改手工配置（国内模型价格） |
| `backend/app/system_agents/presets.py:40` `https://knowledge-mcp.global.api.aws` | 境外 MCP | 预置 agent 下线 |
| 前端 `npm install`、后端 `uv sync`（`uv.lock` 指向 pypi.org） | 开发机访问 | 用 npmmirror / 清华源；lock 文件不必改，配置 index 镜像即可 |

### G. 文档与默认值

- `README.md` / `README.zh-CN.md` / `docs/setup*.md` / `CLAUDE.md` / `AGENTS.md`：前置条件（“AgentCore previews in us-west-2”、Registry GA、Cognito、Bedrock 模型）全部重写为中国区版本；`cdk bootstrap aws://<account>/cn-north-1`。
- `docs/architecture.md` 的服务映射表要按中国区 6 组件重写。
- 测试：`backend/tests/` 里大量 `us-west-2`、`arn:aws:`、Cognito stub（test_inbound_auth、test_system_agents、test_workspaces、test_bootstrap 等）要随功能同步删改；`make verify` 的 i18n 校验在删页面后要同步清 `frontend/src/locales/{en,zh-CN}/common.json` 中的键。

---

### H. Memory：用中国区自建后端保留（可行，建议做）

**结论：可以保留。** 但要澄清一点：单靠一个“知识库”（向量库）只覆盖 Memory 的长期记忆存储与检索；AgentCore Memory 在本 repo 里还承担短期会话事件存储、LLM 抽取（facts / preferences / summaries / episodes）、以及控制台的 actors / sessions / events / extraction-jobs 查询。要保留现有体验，需要把这四块都补齐，但每一块都有现成的中国区可用服务，总体是中等工作量，且是 6 个缺失服务中**唯一值得重建**的（API 面小、对 demo 价值高、数据可以全部留在境内）。

**repo 对 AgentCore Memory 的实际依赖面（盘点）**

| 用途 | 调用的 API | 代码位置 |
|---|---|---|
| 运行时短期记忆：恢复/追加会话消息 | `CreateEvent`、`ListEvents`（经 `AgentCoreMemorySessionManager`） | `templates/strands_agent/main.py.tmpl:31-33,75-89`、`strands_a2a_agent/main.py.tmpl:25-27,112-119` |
| 运行时长期记忆召回：每轮把 `/preferences/{actor}`、`/facts/{actor}` 的 top-3 记录注入提示 | `RetrieveMemoryRecords` | `templates/strands_agent/main.py.tmpl:52-72`（`recall_context`） |
| 控制台写回：Chat 每轮 USER/ASSISTANT 事件 | `CreateEvent` | `services/memory.py:105-118`（`create_turn_event`） |
| Chat 右侧记忆栏 | `ListEvents` + 4 个 namespace 的 `ListMemoryRecords` | `services/memory.py:191-250`（`session_memory_summary`）、`routers/chat.py:426-465` |
| Memory 控制台 8 个只读接口（overview/actors/sessions/events/namespaces/records/search/extraction-jobs） | `GetMemory`、`ListActors`、`ListSessions`、`ListEvents`、`ListMemoryRecords`、`RetrieveMemoryRecords`、`ListMemoryExtractionJobs` | `services/memory_console.py`（549 行）、`routers/memory.py` |
| Memory 资源管理 6 个接口（create/adopt/get/update/delete） | `CreateMemory`、`UpdateMemory`、`DeleteMemory`、`ListMemories` | `services/memory_admin.py`、`memory_ownership.py`、`routers/memory_resources.py` |
| 可观测性会话转录回退 | `ListEvents` | `services/observability.py:600-625,1330-1360` |
| 每 agent 记忆分区 `scoped_actor(agent, human)` | 纯字符串逻辑 | `services/memory.py:88`，被 chat / public_api / agents / assistant / observability 引用 |
| 执行角色权限与环境变量 | — | `services/agent_iam.py:520-545`、`deployer/environment.py:47-50`（`LAUNCHPAD_MEMORY_ID`） |

**中国区自建方案（推荐形态）**

```
Runtime (cn)                    控制平面 backend (cn)              抽取流水线 (cn)
┌──────────────────────┐        ┌──────────────────────┐           ┌─────────────────────┐
│ Strands Agent        │        │ MemoryBackend 协议    │           │ SQS  ──► Lambda      │
│  SnapshotSessionMgr ─┼─► S3 ◄─┼─ selfhosted 实现      │           │  extractor           │
│  (会话快照=短期记忆)  │        │  · actors/sessions   │           │  · 读 S3 快照         │
│  MemoryManager ──────┼─► OpenSearch k-NN ◄───────────┼───────────┤  · Kimi/GLM 抽取      │
│  (LTM 检索/注入)      │        │  · records/search    │           │  · Titan/bge 向量化    │
│  AfterInvocation 钩子─┼─► SQS  │  · extraction jobs   │◄──────────┤  · 写 OpenSearch      │
└──────────────────────┘        └──────────────────────┘           └─────────────────────┘
```

| 组件 | 中国区服务 | 可用性（已核实） | 说明 |
|---|---|---|---|
| 短期记忆（会话消息） | S3（现有 `launchpad-artifacts-*` 桶下 `memory/<store>/<actor>/<session>/`） | ✅ | 直接用 Strands `SnapshotSessionManager(storage=S3Storage(...))`，**零自定义代码**即可恢复多轮对话；控制台的 events/sessions/actors 从快照与前缀列举读取（规模大时加一张 DynamoDB 索引表） |
| 长期记忆存储 + 语义检索 | **Amazon OpenSearch Service**（k-NN 插件，IAM/SigV4 鉴权） | ✅ 两区都有 | 索引字段：`namespace`（保持 `/facts/{actor}`、`/preferences/{actor}`、`/summaries/{actor}/{session}`、`/episodes/{actor}/{session}` 原样）、`text`、`embedding`、`strategy`、`created_at`。OpenSearch **Serverless 要到 2027 Q1**，用托管域；备选 Aurora PostgreSQL + pgvector（✅，但 Runtime 侧需密码或 Data API，不如 SigV4 干净） |
| 向量化 | ① Global Bedrock `amazon.titan-embed-text-v2:0`（同一把 API key，跨境）；② 中国区 SageMaker 自托管 bge-m3（数据不出境，多一套运维） | ① ✅ ② ✅ | 对话正文本来就跨境送去 Kimi/GLM，①不增加新的出境面；若要求“记忆内容不出境”，选② |
| 抽取 LLM | Global Bedrock Kimi K3 / GLM 5.3（§3.A 同一凭证链） | ✅ | 每个策略一套提示词：facts（原子事实、去重）、preferences（结构化偏好）、summary（每会话覆盖写）、episode（场景/意图/动作/结果）；reflection 做成按 actor 定时聚合 |
| 异步触发 | SQS + Lambda（或复用 backend 的后台 job 线程） | ✅ | Runtime 侧 `AfterInvocationEvent` 钩子投递 `{store, actor, session}`；Lambda 读快照增量、调用 LLM、写 OpenSearch、记 `extraction_jobs` 行供控制台展示 |

**代码改动点**

| 位置 | 改法 |
|---|---|
| `backend/app/services/memory.py`、`memory_console.py`、`memory_admin.py`、`memory_ownership.py` | 抽出 `MemoryBackend` 协议（`create_event / list_events / list_sessions / list_actors / list_records / search_records / list_extraction_jobs / create_store / update_store / delete_store`），现有 AgentCore 实现保留为 `agentcore` 后端，新增 `selfhosted` 后端（S3 + OpenSearch + SQLite `extraction_jobs` 表）；`routers/memory*.py` 与前端 `api.ts:5066-5121,5458` **契约不变** |
| “Memory 资源 / 策略” 概念 | 一个 memory resource = 一行 `memory_stores`（名称、S3 前缀、OpenSearch 索引、启用的策略、过期天数）；`memory_strategies.py` 的 4 个策略改为抽取提示词配置；`eventExpiryDuration` 用 S3 生命周期 + 索引 TTL 字段模拟 |
| `templates/strands_agent/main.py.tmpl:31-33,52-89,241-274,374-395`、`strands_a2a_agent/main.py.tmpl` | 替换 `AgentCoreMemorySessionManager` → `SnapshotSessionManager(session_id, storage=S3Storage(bucket, prefix))`；`recall_context` → Strands `MemoryManager(stores=[LaunchpadOpenSearchMemoryStore(...)])`（实现 `MemoryStore` 接口：`search` 走 k-NN，`add` 可关闭）或保留现有“每轮手工检索注入”的写法改调 OpenSearch；新增 `AfterInvocationEvent` 钩子投 SQS。需确认锁定的 `strands-agents==1.47.0` 含 `strands.memory.MemoryManager` 与 `SnapshotSessionManager`，否则升级或用自定义 hook |
| `backend/app/deployer/environment.py:47-50` | `LAUNCHPAD_MEMORY_ID` → `LAUNCHPAD_MEMORY_BACKEND=selfhosted`、`LAUNCHPAD_MEMORY_BUCKET/PREFIX`、`LAUNCHPAD_MEMORY_OS_ENDPOINT/INDEX`、`LAUNCHPAD_MEMORY_QUEUE_URL`、向量化方式 |
| `backend/app/services/agent_iam.py:520-545`、`workspace_iam.py`、`infra/stacks/base_stack.py` 执行角色 | `AgentCoreMemory` 语句 → `s3:GetObject/PutObject/ListBucket` 限定 `memory/<store>/` 前缀、`es:ESHttpGet/Post/Put` 限定域+索引、`sqs:SendMessage`、（跨境向量化时）`bedrock-agentcore:GetResourceApiKey` |
| `infra/stacks/base_stack.py`、`services/workspace_bootstrap.py` `memory` 阶段 | 新增：OpenSearch 域（最小规格，IAM 细粒度访问控制）+ k-NN 索引模板、SQS 队列、extractor Lambda（Python，读取 Identity 中的 Bedrock API key）、S3 生命周期规则；bootstrap `ensure_memory` 改为建索引/队列并写回 `launchpad.yaml` |
| `backend/app/services/observability.py:600-625,1330-1360` 转录回退 | 改读 S3 快照（经 `MemoryBackend.list_events`） |
| `deployer/zip_runtime.py` 打包 | 把 `launchpad_memory` 运行时小库（OpenSearch `MemoryStore` + SQS 钩子，依赖 `opensearch-py`、`requests-aws4auth`/`botocore` 签名）随 zip 下发，与现有 `gateway_support` / `kb_support` 模板同一机制 |
| 前端 `frontend/src/pages/Memory.tsx`、`v2/pages/Memory.tsx` | 基本不动；“extraction jobs” 列展示自建任务状态；资源创建表单字段改为策略开关 + 过期天数 |

**保留与不保留的能力**

- 保留：每 agent / 每用户记忆分区（`scoped_actor` 不变）、多轮会话恢复、facts / preferences 跨会话召回并注入提示、每会话摘要、Chat 记忆栏、Memory 控制台全部视图、按 agent 固定（pin）某个记忆库、A2A 每 context 独立会话。
- 降级：抽取与去重质量取决于自写提示词（AgentCore 的合并/冲突消解逻辑需自己实现，v1 用“向量相似度 > 阈值视为重复”）；episodic reflection 建议排到 v2；`memoryStrategyId` 过滤改为 `strategy` 字段过滤。
- 顺带收益：同一个 OpenSearch 域 + 向量化链路可以同时承载 **自建 RAG 知识库**，把 §2 里“移除”的 Knowledge Bases 模块（`kb_search` 工具、KB 控制台）也以自建形式恢复——两者共用一套基础设施，边际成本低。

**工作量与风险**：新增约 1.5–2.5k 行（backend selfhosted 后端 ≈ 800，extractor Lambda ≈ 400，运行时小库 + 模板 ≈ 300，CDK ≈ 200，测试），修改约 500 行。主要风险在抽取质量与成本（每轮对话多一次跨境 LLM 调用），以及 OpenSearch 域的常驻费用（demo 可用最小 1 节点 `t3.small.search`）。建议排在 §4 的 Phase 3 之后作为 **Phase 3.5**：先做 S3 会话快照 + facts/preferences，再加 summaries / episodes。


**自建 Knowledge Base（D4，与 Memory 共用基础设施）**

| 环节 | 实现 |
|---|---|
| 文档源 | 现有 S3 `kb/` 前缀（控制台上传或指定既有 S3 位置，`services/knowledge.py` 的数据源管理保留） |
| 摄取 | 复用 Memory 的 SQS + Lambda：S3 事件 → 解析（txt/md/pdf/docx，`unstructured` 或 `pypdf`）→ 切块（固定 token 或层级）→ Titan Embeddings V2 向量化 → 写 OpenSearch `kb-<kb_id>` 索引；每文档状态写回 SQLite（替代 `GetKnowledgeBaseDocuments` 的索引状态） |
| 检索 | k-NN + BM25 混合查询，可选 LLM 重排（Kimi）；`kb_search` 工具改调 OpenSearch（SigV4）而不是 `bedrock-agent-runtime.Retrieve`；`kb_deep_search`（AgenticRetrieveStream）改为 Strands 子代理多步检索或先降级 |
| 控制台 | KB 列表/详情/摄取状态/检索演练场 API 契约不变；`type: MANAGED` 概念改为本地 `knowledge_bases` 表 |
| 挂载 | 创建向导里 KB 挂载不变；生成代码用 `templates/direct_kb_tools.py.tmpl` 路径（Harness 的 `launchpad-kb-gw` 网关路径随 Harness 消失，自建 Harness 也走直连工具） |
| IAM | 执行角色加 `es:ESHttpGet/Post` 于 `kb-*` 索引 + `GetResourceApiKey`（Titan 查询向量化） |

工作量：新增约 800–1200 行（摄取 Lambda + 检索服务 + 表 + 控制台适配），与 Memory 后端同期做（Phase 3.5）。

### I. Agent Registry：用本地账本自建保留（可行，难度低于 Memory）

**结论：可以自建，而且是 6 个缺失服务里最容易重建的。** 原因：Registry 对本 repo 而言是**纯控制平面**能力——已部署的 agent 从不调用 Registry（技能包在打包时由 backend 从 S3 下载，MCP/Gateway 选择由 backend 解析），没有运行时 SDK、没有异步抽取、没有 LLM；本质是“带审批工作流和搜索的目录 CRUD”。repo 已经把 AWS 的 GA 数据模型隔离在一层适配器里，并且已经实现了 “Registry 不可用则降级” 的路径，自建后端正好插在这个缝上。

**repo 对 Registry 的实际依赖面（盘点）**

| 用途 | AWS API | 代码位置 |
|---|---|---|
| 单例注册表 `launchpad-registry` | `CreateRegistry`、`ListRegistries` | `services/bootstrap.py:64-94`（`ensure_registry`）、`workspace_bootstrap` `registry` 阶段 |
| 记录 CRUD 与版本（`(name, recordVersion)` 唯一，`1.0.0-a2a / -mcp / -skill`） | `CreateRegistryRecord`、`GetRegistryRecord`、`UpdateRegistryRecord`、`DeleteRegistryRecord`、`ListRegistryRecords` | `services/agentcore/registry.py:385-550`（`find_record / upsert_record / get_record / list_records / wait_record_settled`） |
| 审批生命周期 DRAFT → PENDING_APPROVAL → APPROVED / REJECTED，另有 DEPRECATED | `SubmitRegistryRecordForApproval`、`UpdateRegistryRecordStatus` | `services/agentcore/registry.py:577-612`、`routers/registry.py:267`（action） |
| 消费者视角：只列 APPROVED、混合（语义+关键词）搜索 | `ListDiscoverableRegistryRecords`、`SearchDiscoverableRegistryRecords` | `services/agentcore/registry.py:551-622`、`routers/registry.py:158,173` |
| 三种资产的描述符互转（A2A card / MCP url+tools / skillDefinition+SKILL.md ↔ GA `data/dataSchemaVersion`） | — | `services/agentcore/registry.py:229-495` |
| 每次部署自动注册并提交 A2A 记录（第 5 阶段） | `CreateRegistryRecord` + `Submit…` | `deployer/registration.py`、`registry_console.register_agent_record` |
| 技能包导入：zip / git / url / inline → S3 `skills/{name}/` + SKILL 记录；重导入、改版本 | 上述 CRUD | `services/skill_ingest.py`、`registry_console.py:1043-1480`、`routers/registry.py:360-574`、`routers/agent_skills.py` |
| 部署时按 APPROVED SKILL 记录下载技能包；创建向导/助手的“可挂载资产”目录 | `ListRegistryRecords` | `deployer/zip_runtime.py:204-333`、`registry_console.attachable_records:877`、`assistant/service.py:612` |
| Governance：把一个 Gateway 导入为一条 MCP 记录、退役旧记录 | CRUD | `registry_console.py:305-665`、`services/governance.py:299-320,1088` |
| 系统预置技能包的注册与保护 | CRUD + 幂等 client token | `models/ledger.py` `SystemSkillRecord`、`system_agents/skill_registry.py` |
| LIVE CARD 对比（读运行时实际 A2A 卡片） | `GetAgentCard`（**Runtime 数据面**，与 Registry 无关） | `routers/registry.py:198`、`registry_console.console_live_agent_card` |
| 总览页资产计数 | `ListRegistryRecords` | `routers/overview.py:42` |

共 12 个 AWS 操作，全部可被一个本地实现覆盖。前端 `api.ts:4136-4196,4504` 的 15 个客户端方法与 `routers/registry.py` 的 18 条路由**契约不变**。

**自建方案**

| 组件 | 实现 | 说明 |
|---|---|---|
| 记录存储 | SQLite 账本新增 `registry_records` 表（`id, workspace_id, type[AGENT/MCP/SKILL], name, record_version, status, descriptors(JSON), tags, created_at, updated_at, submitted_at, decided_by, decided_at`），`UNIQUE(workspace_id, name, record_version)` | repo 的 “AWS 是真相源、账本只存标识符” 原则在这一项上**反转**：Registry 记录本身就是账本数据。这是可接受的，因为 Registry 没有运行时状态 |
| 技能包存储 | 不变：S3 `skills/{name}/`、`system-skills/{name}/{version}/`（中国区桶） | `skill_ingest.py` 与下载逻辑零改动 |
| 关键词搜索 | SQLite **FTS5** 虚拟表（name、description、tool 名、SKILL.md 正文） | 零新增基础设施，覆盖 `SearchDiscoverableRegistryRecords` 的关键词部分 |
| 语义搜索（可选） | 复用 §3.H 的 OpenSearch 域 + 向量化链路，给每条 APPROVED 记录建一条向量 | v1 可只做 FTS；有 Memory 的域后再加混合检索 |
| 审批工作流 | 状态机在 `LocalRegistryBackend` 内实现；动作权限沿用现有 `registry.manage` 路由策略（`core/route_policy.py`） | AWS 侧的审批也只是状态字段，没有外部审批人集成 |
| 多账号 / 多区域 workspace | 记录按 `workspace_id` 分区；如需“全组织一个目录”，加一个 `shared` 标记跨 workspace 可见 | 比 AWS 原生更灵活（hub 一个库），但失去 AWS 跨账号资源策略那种“别的团队用 SDK 直接发现”的能力，见下文 |
| 对外发现（可选） | 在 `/v1` 公共 API 加 `GET /v1/registry/records`（X-Api-Key），或把目录做成 Gateway 上的一个 MCP 工具 `registry_search` | 覆盖“其他 agent 发现本目录”的场景 |

**代码改动点**

| 位置 | 改法 |
|---|---|
| `backend/app/services/agentcore/registry.py` | 以现有 15 个函数（`find_record / upsert_record / get_record / wait_record_settled / list_records / list_discoverable_records / submit_record / set_record_status / approve / disable / reject / delete_record / search_records` 等）为 `RegistryBackend` 协议；现实现保留为 `agentcore` 后端，新增 `LocalRegistryBackend`（SQLAlchemy + FTS5，约 400–600 行）。`normalize_record / to_ga_descriptors / from_ga_descriptors` 在本地后端里退化为直存平台描述符，`wait_record_settled` 直接返回 |
| `backend/app/services/agentcore/client.py:38-44` `registry_control_client / registry_data_client` | 工厂按 `features.registry_backend` 返回本地后端对象（鸭子类型满足 `registry_console` 的 `client.xxx(...)` 调用）或 boto3 客户端 |
| `backend/app/services/registry_console.py` | 大部分不动（它拼的是平台描述符，再交给适配层）；`_require_new_name`、`gateway_registry_states`、`_list_mcp_record_details` 走后端协议；`RegistryUnavailableError` 路径保留 |
| `backend/app/models/ledger.py` | 新增 `RegistryRecord` 模型 + FTS5 表（Alembic/建表脚本）；`SystemSkillRecord` 的 `client_token` 幂等逻辑可简化为同事务写入 |
| `backend/app/services/bootstrap.py:64-94`、`workspace_bootstrap.py:327`（探针）、`registry` 阶段 | `ensure_registry` → 生成本地 `registry_id = f"local-{workspace_id}"` 写回 `launchpad.yaml`；探针移除 `agent-registry-control`；`registry_unavailable_reason` 置空 |
| `backend/app/deployer/registration.py`、`zip_runtime.py:204-333`、`assistant/service.py:612`、`governance.py`、`overview.py` | 无改动（经 `registry_console` 间接调用） |
| `infra/spoke/launchpad-workspace-role.yaml:186,290-293`、`workspace_iam.py` | 删除 `agent-registry:*` 与 `agent-registry.amazonaws.com` 主体 |
| `routers/registry.py:198` LIVE CARD | 保留，但依赖 Runtime 数据面 `GetAgentCard`（A2A）在中国区可用性，见 §5 第 4 项；不可用则该按钮按现有 “资源不可用” 失败态处理 |
| 前端 `frontend/src/pages/Registry.tsx`（1175 行）、`v2/pages/Registry.tsx` | 不动；“NOT DISCOVERABLE” 芯片逻辑（控制面 vs 数据面差集）在本地后端下仍成立（discoverable = APPROVED） |
| 文档 `docs/architecture.md` Registry 行、`docs/registry-ga-migration.md` | 重写为本地 Registry 说明 |

**保留与不保留的能力**

- 保留：A2A / MCP / SKILL 三类资产目录；提交 → 审批 → 退役生命周期；每次部署自动注册 A2A 记录；技能包四种导入源与重导入；创建向导与架构师助手的“可挂载资产”清单；Governance 的 Gateway → MCP 记录导入；总览资产计数；全部控制台视图。
- 降级：**失去 AWS 原生跨账号共享与 IAM 治理的发现面**（其他团队无法用 AWS SDK `SearchDiscoverableRegistryRecords` 发现本目录），需用 `/v1` 或 MCP 工具暴露；语义搜索 v1 退化为 FTS 关键词（接 OpenSearch 后恢复混合检索）；没有 AWS 控制台视图与 CloudTrail 级审计（用现有 `policy_changes` 风格的本地审计表补）。
- 与 Harness 的耦合消失：原来 Registry 记录还要给 Harness 挂载技能 / MCP（`attachable` 判定），中国区无 Harness，`attachable_records` 只需服务 zip-runtime / BYOC 路径，反而更简单。

**工作量与风险**：新增约 600–900 行（本地后端 + 模型 + FTS + 测试），修改约 200 行（client 工厂、bootstrap、IAM、文档）；前端零改动。风险很低：无外部服务、无异步、无 LLM。建议排在 §4 的 **Phase 1**（骨架可跑）里一并完成，而不是像 Memory 那样单列阶段——因为 deploy 流水线第 5 阶段和创建向导的技能挂载都依赖它，早做能让 Phase 2 的联调覆盖“带技能的 agent”。

### J. Evaluations / Optimizations / judge 模型 / Gateway rules / ConfigBundle A/B：能否自建

**结论：可以，但要分开看。** 这五项在 repo 里是一条闭环：评估产出分数 → 推荐产出新提示词 → 打成配置包 → 网关规则按权重分流做 A/B → 再评估出判决 → 推广。其中 **judge 模型、推荐、配置包** 三项自建成本很低（repo 已有本地实现或只是存 JSON）；**评估引擎** 是重头（要自己写判题流水线）；**Gateway rules / A/B 分流** 在中国区 Gateway 上没有替代品，必须把分流从网关层下沉到 Launchpad 自己的调用链。整体是 6 个缺失能力里**工作量最大**的一组，建议放在 MVP 之后单独一个阶段。

**repo 对这组 AWS 能力的依赖面（盘点）**

| 环节 | 用到的 AWS API（次数） | 代码位置 | 中国区 |
|---|---|---|---|
| 判题器定义：14 个 `Builtin.*` 提示词评估器、自定义 LLM-as-a-judge / derived / code-based（Lambda） | `CreateEvaluator / UpdateEvaluator / GetEvaluator(14) / ListEvaluators / DeleteEvaluator` | `evaluation/agentcore_eval.py:76-110,580-720`、`online_evaluators.py` | ❌ |
| 批量评估：数据集回放 → 等 spans 落 CloudWatch → `StartBatchEvaluation(sessionIds)` → 轮询 → 从结果日志流读 `gen_ai.evaluation.result` | `StartBatchEvaluation(4) / GetBatchEvaluation(9) / StopBatchEvaluation / ListBatchEvaluations` | `evaluation/service.py`（876 行）、`agentcore_eval.py:287-430`、`queue.py`、`telemetry.py` | ❌ |
| 数据集：SQLite 场景 → 单向同步到 AWS Dataset（draft/version） | `CreateDataset / AddDatasetExamples / DeleteDatasetExamples / CreateDatasetVersion / ListDatasetVersions / GetDataset(5) / DeleteDataset` | `evaluation/scenarios.py`、`models.py:21-40`（`cloud` 字段） | ❌（本地侧已是 SQLite） |
| 在线评估：按采样率 + 会话空闲超时持续判题，结果在 `/aws/bedrock-agentcore/evaluations/results/<configId>` | `CreateOnlineEvaluationConfig / Update / Delete / List(4) / Get` | `evaluation/online.py`（1075 行）、`online_routers.py` | ❌ |
| 按需评估：对单会话同步判题 | 数据面 `Evaluate` | `evaluation/service.py`、Observability 会话页 SCORE NOW | ❌ |
| Insights：失败分析 / 用户意图 / 执行摘要聚类 | `StartBatchEvaluation(analysisTypes)` | `evaluation/service.py`、`insights` 视图 | ❌ |
| 模拟角色场景：SDK `SimulatedScenarioExecutor`（本地代码，LLM 扮演用户） | SDK 内部用 `bedrock-runtime` | `evaluation/simulation.py` | ⚠️ SDK 本地执行，模型调用需走 Global Bedrock API key |
| 推荐（系统提示词 / 工具描述）：AWS 作业 **或** 本地 `gepa_lite` 反思一轮 | `StartRecommendation(2) / GetRecommendation(4)`；`gepa_lite` 只用 `ConverseStream` | `evaluation/recommendations.py`（963 行）、`optimization/providers/{agentcore,gepa_lite,bedrock_lm}.py` | ❌ AWS 作业；✅ `gepa_lite` |
| 配置包：版本化 `{system_prompt, tool_descriptions, tools}`；运行时经请求 `baggage` 头拿到 bundle 引用再取内容 | `CreateConfigurationBundle(2) / Update / Get(2) / GetVersion / Delete` | `optimization/service.py:920-990`、模板 `strands_agent/main.py.tmpl:111-135`（`_config_bundle()` 唯一读取点）、SDK `runtime/app.py:436-440` | ❌ |
| A/B 分流：实验网关（AWS_IAM）+ `http.agentcoreRuntime` 目标 + `CreateABTest` 变体权重（配置包变体或目标变体）+ Gateway rules | `CreateABTest(3) / GetABTest(8) / UpdateABTest(5) / DeleteABTest`、`CreateGateway/Target` | `optimization/service.py:995-1250,1685-1701`、`canary_service.py:454-760`、`canary_infra.py` | ❌ rules / ABTest / ConfigBundle A/B 均不可用；Gateway 本身 ✅ |
| 金丝雀 Model 1：一个 Runtime 两个不可变版本 + `stable`/`treatment` 命名端点 | `UpdateAgentRuntime`、`CreateAgentRuntimeEndpoint`（**Runtime API，中国区可用**）+ 上述 A/B | `optimization/canary_infra.py`、`canary_service.py` | ✅ 版本/端点；❌ 分流 |
| Harness 金丝雀 | passthrough 目标 → `InvokeHarness` | `optimization/canary_harness.py`（411 行） | ❌ 随 Harness 消失 |
| judge 模型 | Bedrock Converse 推理档 | `agentcore_eval.py:31-32`、`bedrock_lm.py`、`config.py:183-187` | ✅ 跨境 Global Bedrock Kimi/GLM |

控制平面代码约 15k 行（`evaluation/` + `optimization/`），前端评估/实验页约 11k 行；AWS 调用全部收口在 `evaluation/agentcore_eval.py`（1568 行，显式注入 client 的薄包装），这正是可替换的边界。

**逐项评估**

| 能力 | 可否自建 | 做法 | 工作量 | 失去什么 |
|---|---|---|---|---|
| **① Bedrock judge 模型** | ✅ 直接可用 | §3.A 的同一把 Global Bedrock API key；`bedrock_lm.converse_for` 改从 `aws_clients.global_bedrock_runtime_client()` 构造；默认 judge 改 `global.zai.glm-5.3`（或 Kimi K3） | 极小 | AWS 托管判题器内部用的是 AWS 自己的 judge，自建后一致性由我们的提示词保证；判题把会话正文送出境（与聊天一致） |
| **② 推荐（Optimizations）** | ✅ 低 | 两条路：(a) repo 已有绕开 `StartRecommendation` 的本地提供方 `gepa_lite`（系统提示词 + 工具描述，496 行），设为默认；(b) 接 **Strands Harness Optimizer**（`strands-harness-optimizer`）作为第二个提供方：`SystemPromptFormula` + `ContrastiveReflectionOptimizer`（对比反思）+ `AgentCoreRolloutEngine`（直接对中国区 Runtime 做 rollout，`InvokeAgentRuntime`）+ 以 Strands Evals 评估器包装成 `RewardFunction`，可多轮迭代而不是 `gepa_lite` 的单轮反思。删除 `providers/agentcore.py` 与 `recommendations.py` 的 AWS 作业分支 | 小（(a) 主要是删；(b) 一个新 provider 约 200–300 行） | AWS 推荐作业的内容过滤与 `agentTraces.batchEvaluation` 数据源 |
| **③ ConfigBundle** | ✅ 低 | 本地 `config_bundles` 表（bundle_id, version, JSON）+ S3 不可变副本；运行时模板 `_config_bundle()` 改为：从请求头 `X-Amzn-Bedrock-AgentCore-Runtime-Custom-Launchpad-Bundle: <id>:<ver>`（Runtime 透传 `…-Custom-*` 头）读指针 → 从 S3 取 JSON（进程内缓存）；`system_prompt / tool_descriptions / tools` 解析逻辑不变 | 小 | Gateway 代为解析配置包的能力（`gateway_role` 的 `GetConfigurationBundleVersion`）——自建后不需要 |
| **④ Gateway rules / ABTest 分流** | ⚠️ 可以，但换层 | 中国区 Gateway 没有 rules、ABTest、ConfigBundle A/B、Inference targets，网关层无法分流。把分流下沉到 Launchpad **唯一的调用入口** `services/invoke.invoke_agent_text` / `chat_stream`：按 `hash(session_id)` 与实验行上的权重确定臂；配置包 A/B → 设置 ③ 的请求头；目标金丝雀 → 选 `qualifier=stable|treatment` 命名端点（`CreateAgentRuntimeEndpoint` 中国区可用，`canary_infra` 的版本铸造逻辑保留）。权重修改/暂停改为写实验行。按臂统计：spans 上打 `launchpad.experiment / launchpad.variant` 属性（优先用 `baggage` 头透传给 SDK 的 baggage→span 属性处理器，需实测透传；否则模板从自定义头写 span 属性） | 中（`service.py` 的 gateway/abtest/traffic 三个阶段与 `canary_service` 重写约 800–1200 行；实验网关 `launchpad-exp-gw`、`canary_infra` 的网关部分删除） | 只有经 Launchpad（Chat、`/v1`、数据集回放）进入的流量参与分流，直连 Runtime ARN 的外部调用不参与——**AWS 版本同样只分流经实验网关的流量**，等价；Harness 金丝雀随 Harness 消失 |
| **⑤ Evaluations（批量 / 在线 / 按需 / insights / 数据集 / 模拟）** | ✅ 可以，**用 Strands Evals 后从“重”降为“中”** | 不自写判题流水线，直接以 `strands-agents-evals`（1.4.0）为评估引擎：它自带与 AgentCore `Builtin.*` 一一对应的 LLM-as-a-judge 评估器、`CloudWatchProvider` + `CloudWatchSessionMapper`（直接读 AgentCore 落在 CloudWatch 的 ADOT spans，aws/spans 或 runtime 日志组）、detectors（失败检测 + 根因分析 = Insights 替代）、`ActorSimulator`/`ToolSimulator`（= 模拟角色场景替代）、deterministic 评估器（= `Builtin.Trajectory*Match` 替代）；judge 用 `BedrockModel(api_key=…, region_name="us-west-2", model_id="global.zai.glm-5.3")` 实例传入。Launchpad 侧只需：结果表、在线采样调度、把 `agentcore_eval.py` 的包装改为调用 Strands Evals `Experiment`。详见下文“采用 Strands Evals / Harness Optimizer” | 中（新增约 1–1.5k 行：结果表 + 评估器注册/配置映射 + 在线采样调度 + 编排改造；判题提示词、trace 解析、模拟器、诊断全部复用包内实现） | AWS 托管评估器的校准质量换成 Strands Evals 的提示词（同一团队维护、对应关系明确）；CloudWatch Evaluations 控制台；账户级批量配额约束消失 |

**采用 Strands Evals / Harness Optimizer 作为评估与优化引擎（已核实，2026-10-08）**

两个包都是 Strands 官方生态、纯 Python、本地执行，只依赖 `boto3`/`strands-agents`，没有任何 AgentCore Evaluations / Optimizations 服务调用，因此在中国区可直接用；judge / 反思模型通过 Strands `Model` 实例注入，即 §3.A 的 `BedrockModel(api_key=…)`。

| 需要替代的 AWS 能力 | Strands 包内对应物 | 备注 |
|---|---|---|
| 14 个 `Builtin.*` 提示词评估器 | `strands_evals.evaluators`：`GoalSuccessRateEvaluator`（SESSION）、`HelpfulnessEvaluator`、`CorrectnessEvaluator`、`FaithfulnessEvaluator`、`ResponseRelevanceEvaluator`、`ConcisenessEvaluator`、`CoherenceEvaluator`、`InstructionFollowingEvaluator`、`RefusalEvaluator`、`HarmfulnessEvaluator`、`StereotypingEvaluator`（TRACE）、`ToolSelectionAccuracyEvaluator`、`ToolParameterAccuracyEvaluator`（TOOL）、`SkillSelectionAccuracyEvaluator`、`SkillInstructionFollowingEvaluator` | 15/16 名称级一一对应；缺 `ContextRelevance`，用 `OutputEvaluator(rubric=…)` 写一条 rubric 补齐。另有 AWS 没有的 `FailureCommunication` / `PartialCompletion` / `RecoveryStrategy` / 多模态评估器可顺带暴露 |
| 自定义 LLM-as-a-judge（指令 + 数值刻度 + 模型） | `OutputEvaluator(rubric=…, model=…)` / 继承 `Evaluator` | repo 的 `llmAsAJudge` 表单字段直接映射 |
| derived 评估器（内置提示词换模型） | 任一内置评估器 + `model=` 参数 | — |
| code-based 评估器（Lambda） | 自定义 `Evaluator` 子类内 `lambda:Invoke`，入参沿用 `{schemaVersion, evaluationInput…}` 协议 | Lambda 中国区 ✅ |
| `Builtin.Trajectory*Match`（精确 / 有序 / 任意序） | `TrajectoryEvaluator` + deterministic `ToolCalled` 等，包内自带 exact / in-order / any-order 匹配工具函数 | — |
| 从 CloudWatch 读会话 spans 判题（批量 `sessionIds` / 时间窗 / 在线采样） | `providers.CloudWatchProvider(log_group=…)` + `mappers.CloudWatchSessionMapper` → `Session` 对象；也有 `OpenSearchProvider` | 与 Observability 读的是同一批 ADOT spans；若 §3.H 把 spans 也写入 OpenSearch，可走 `OpenSearchProvider` |
| Insights：失败分析 / 根因 / 修复建议 | `detectors.detect_failures` / `analyze_root_cause` / `diagnose_session`，约 20 类失败分类，`Experiment(diagnosis_config=DiagnosisConfig(ON_FAILURE))` 自动对失败用例诊断 | 比 AWS Insights 多了“修复建议”，可直接作为推荐阶段的证据 |
| 模拟角色场景（`SimulatedScenarioExecutor`） | `ActorSimulator`（用户 / 对抗者 / 领域专家画像，goal 驱动多轮）+ `ToolSimulator`（无需真实后端的工具模拟） | 替代 SDK 的 preview 执行器 |
| 数据集 / 实验定义与版本 | `Case` / `Experiment` JSON 序列化，`ExperimentGenerator` 从工具描述自动生成用例 | Launchpad 的 SQLite 场景 → `Case` 一一映射；AWS Dataset 同步删除 |
| 红队 | `experimental.redteam` | 可选新增 |
| 推荐 / 优化（系统提示词） | `strands-harness-optimizer`：`SystemPromptFormula` → `ContrastiveReflectionOptimizer` / `MultiAgentOptimizer`，`Trainer.fit()` 循环；`AgentCoreRolloutEngine` 对已部署 Runtime 做 rollout；`RewardFunction` 用 Strands Evals 评估器包装 | PyPI 目前 **0.0.1**（仅 SystemPrompt / ContextExpansion 两种 Formula）；README 所述 `ToolDescriptionFormula` / `SkillFormula` / `MultiSurfaceOptimizer` / S3 参数存储在 main 分支，需装 git 版；优化器内部 `BedrockModel(**model_config)` 需确认能透传 `api_key`/`region_name`（否则一行小补丁） |

对 repo 的意义：`evaluation/agentcore_eval.py`（1568 行）里对 `StartBatchEvaluation / Evaluate / CreateEvaluator / Dataset*` 的包装，整体替换为“构造 `Experiment(cases, evaluators)` → `run_evaluations_async(task)`（回放模式，task = 经 `invoke_agent_text` 调用 Runtime）或对既有会话 `CloudWatchProvider.get_session()` → 评估器 `.evaluate()`（被动 / 在线 / 按需模式）→ 写 `eval_results`”。`service.py` 的 `invoking → waiting → evaluating` 三阶段编排、运行队列、停止语义、51 条路由和前端全部保留；`online.py` 的采样规则改为 backend 定时任务驱动同一条路径。模拟角色场景（`simulation.py`）改为 `ActorSimulator` 驱动 `invoke_agent_text`。推荐阶段在 `providers/` 下新增 `harness_optimizer` 提供方与 `gepa_lite` 并列。

运行位置与数据流向：评估与优化都在 **Launchpad backend 进程（中国区）** 内执行，输入 spans 来自中国区 CloudWatch，结果落中国区 SQLite / CloudWatch EMF；只有 judge / 反思模型调用经 Global Bedrock 出境（与聊天一致）。

**自建后的闭环形态（中国区）**

```
数据集(SQLite) ─回放─► Runtime(cn) ─spans─► CloudWatch Logs(cn)
                                                 │ Logs Insights(sessionIds)
                                                 ▼
                    ┌── 判题流水线(backend) ── Kimi/GLM@Global Bedrock ──► eval_results(SQLite) + EMF 指标
                    │
推荐 gepa_lite ◄────┘ 证据
   │ 新提示词
   ▼
config_bundles(SQLite+S3) ──► invoke 调用链按 hash(session)·权重选臂 ──► 请求头 bundle 指针 / 命名端点 qualifier
                                                                         └─► 运行时 _config_bundle() 从 S3 取 JSON
```

**代码改动点**

| 位置 | 改法 |
|---|---|
| `backend/app/evaluation/agentcore_eval.py` | 以其现有函数签名为 `EvaluationBackend` 协议；AWS 实现保留，新增 `LocalEvaluationBackend`（判题流水线、结果表读写、在线采样调度） |
| `backend/app/evaluation/{service,online,recommendations,scenarios,simulation}.py` | `service.py` 的 `waiting → evaluating` 两阶段改调本地后端；`online.py` 的 owner 分类（agent/experiment/external）保留但数据源换本地表；`recommendations.py` 删 AWS 作业分支；`scenarios.py` 删 AWS Dataset 同步；`simulation.py` 注入 Global API key 客户端 |
| `backend/app/evaluation/models.py` | 新增 `Evaluator`（定义 + 提示词 + 刻度）、`EvalResult`、`OnlineSample` 表；`EvalDataset.cloud` 弃用 |
| `backend/app/optimization/service.py:920-1250,1685-1701`、`canary_service.py`、`canary_infra.py`、`canary_harness.py`、`providers/agentcore.py` | 配置包改本地表 + S3；`stage_gateway` 删除；`stage_abtest`/`send_gateway_traffic`/权重更新改为写实验行 + 经 `invoke_agent_text` 回放；`canary_service` 保留版本铸造与命名端点，分流改走调用链；`canary_harness.py`、`providers/agentcore.py` 删除 |
| `backend/app/services/invoke.py`（`invoke_agent_text` / `chat_stream` 唯一入口） | 新增“实验路由”步骤：查该 agent 的进行中实验 → 选臂 → 设置 bundle 请求头或端点 qualifier → 在 ledger `ChatSession` / 调用记录上记臂 |
| `backend/app/templates/strands_agent/main.py.tmpl:111-135` | `_config_bundle()` 改读自定义头指针 + S3 JSON（带缓存）；可选从头部写 `launchpad.variant` span 属性 |
| `backend/app/services/agent_iam.py`、`workspace_iam.py`、`base_stack.py` | 执行角色：删 `ABTestOrchestration`/`ConfigurationBundle*`/`StartBatchEvaluation` 等语句；加 `s3:GetObject` 于 `bundles/*` 前缀；评估执行角色加 `lambda:InvokeFunction`（code-based 评估器） |
| `backend/app/core/config.py` | `eval_max_concurrent_runs` 上限 5 的配额约束解除；`prompt_opt_*` 默认模型改 Kimi/GLM；新增 `eval_judge_model_id`、在线评估调度间隔 |
| 前端 `frontend/src/pages/Evaluation*.tsx`、`v2/pages/eval/*`、`Experiments*` | 基本不动；去掉“发布数据集版本到 AWS”“AWS 推荐作业来源”等文案与按钮；实验页 gateway 阶段卡片删除 |

**建议排期与优先级**：放在 §4 的 MVP 之后作为 **Phase 5**，按价值顺序：① judge 模型接通 + 自定义 LLM-as-a-judge + 批量评估（数据集回放 → 判题 → 分数）→ ③ ConfigBundle + ④ 应用层 A/B（配置包臂）→ ② `gepa_lite` 推荐闭环 → ⑤ 在线评估与按需评估 → 目标金丝雀（命名端点臂）→ insights / 模拟角色。采用 Strands Evals / Harness Optimizer 后，总新增约 **1.5–2.5k 行**（原估 3–5k），修改约 1.5k 行；前端近乎零改动。

### K. 控制平面本身的部署形态（EC2 单机）与中国区差异

repo 的控制平面（FastAPI backend + Vite 前端 + SQLite 账本 + Studio 本地执行沙箱）是**单机进程**，参考部署就是一台 EC2：

| 项 | repo 现状 | 来源 |
|---|---|---|
| 形态 | 两个 systemd 单元 `launchpad-backend`（`uv run uvicorn … 127.0.0.1:8000`）+ `launchpad-frontend`（`vite preview … 127.0.0.1:5173`），nginx :80 反代；SQLite 在本地磁盘 `data/launchpad.db` | `docs/agent-runbook-prod.md` §3、`docs/setup.md` “Production deployment” |
| 公网入口 | **CloudFront**（TLS 终止，注入 `X-Launchpad-Origin-Key`）→ nginx 校验该头，拒绝直连实例 IP | `docs/setup.md:563-610` |
| 一键环境 | `docs/workshop3/static/infrastructure/workshop-ec2.yaml`：VPC + 公有子网 + SG(80) + **CloudFront 分发** + IAM 实例配置文件 + EC2（Ubuntu 24.04 **arm64** AMI via SSM 参数）+ UserData 里 `git clone` → `cdk bootstrap` → `make bootstrap` → 写 systemd/nginx | 该模板 |
| 凭证 | 实例配置文件（hub 凭证），`aws_clients.py` 用 ambient 凭证；跨账号 workspace 用 AssumeRole | `services/aws_clients.py` |
| 本地依赖 | Docker（Studio 沙箱 `studio_exec_backend: docker`、本地镜像冒烟）、`uv`、Node 20、`data/exec-venv`、`data/skill-lab-venv` | `scripts/setup_exec_*.sh`、`config.py` |
| 并发/高可用 | 单实例；SQLite 不支持多写者；更新流程先备份 `launchpad.db` | `docs/setup.md` “Update an existing host” |

只有 **agent 本身**跑在 AgentCore Runtime 上，共享基础设施由 CDK `launchpad-base` 建；控制平面不在 AgentCore 上。

**中国区差异**

| 项 | 中国区 | 处理 |
|---|---|---|
| EC2 / VPC / SSM / IAM 实例配置文件 | ✅ | 不变；AMI 的 SSM 公共参数路径在中国区同样存在（Canonical Ubuntu arm64），需实测 `cn-north-1` 下该参数可解析 |
| **CloudFront 入口** | ⚠️ 中国区 CloudFront 需自有域名 + ICP 备案 + 证书 | **决定（2026-10-08）：第一版去掉 CloudFront。** 入口形态改为：PoC/内部 → 实例安全组只放行办公网/VPN 网段的 80/443，或私网 ALB；需要公网 → 备案后再加 ALB + ACM 证书或 CloudFront。随之调整：① nginx 删除 `X-Launchpad-Origin-Key` 校验（该头由 CloudFront 注入）；② `LAUNCHPAD_AUTH_COOKIE_SECURE` 随 TLS 终止位置设置——nginx 自签/ACM 证书终止 TLS 则保持 `true`，纯 HTTP 内网访问必须设 `false`，否则浏览器丢弃会话 cookie；③ `public_base_url` / `cors_origins` 改为实际访问地址；④ `infra/stacks/video_stack.py`（教学视频 CloudFront+S3）一并移除 |
| `workshop-ec2.yaml` 模板 | 含 `AWS::CloudFront::Distribution`、`arn:aws:` 等 | 删除 CloudFront 分发与 Origin-Key 相关资源/UserData 片段，安全组入站改为参数化 CIDR；分区用 `${AWS::Partition}`；UserData 里 `git clone`（GitHub）、`npm ci`、`uv sync`、`pip` 改国内镜像或预打包（见 §3.F） |
| 实例上的 Docker 镜像拉取 | Docker Hub / ghcr.io 不稳定 | 同 §3.F，基础镜像先镜像到中国区 ECR |
| IMDS / 凭证 | 实例配置文件为 `aws-cn` 分区角色 | §3.A 的 Global Bedrock API key 通过环境变量注入 systemd 单元（`EnvironmentFile=` 权限 600），不写入 `launchpad.yaml` |
| 域名 / 备案 | 控制台本身是 Web 应用，对外提供服务需 ICP 备案 | 若仅内部 PoC，走私网即可绕过 |

### L. 自建 Harness（方式B）：以 Global 区 Harness 导出代码为模板（D1）

**思路**：方式B 的价值是“模型 + 提示词 + 工具 + 技能 + 记忆，声明即部署，不写代码”。中国区没有 Harness 服务，但 repo 已经有一条把 Harness 变成可部署 Strands 代码的路径——`services/harness_convert.py`：用 `agentcore` CLI 导出 Harness 的 `main.py`，再嫁接 Launchpad 的配置包契约、Gateway 软失败、KB 直连工具。把这条“转换路径”改成“创建路径”即可。

| 步骤 | 做法 |
|---|---|
| 1. 取模板 | 在 Global 区（us-west-2）用现有代码建一次参考 Harness（含 Gateway 工具、技能、内置 code-interpreter），`agentcore` CLI 导出其 `main.py` 等文件，**vendor 进仓库** `backend/app/templates/harness_runtime/`；之后创建 agent 不再依赖任何 Global Harness。导出代码按版本固化，升级时重新导出 |
| 2. 渲染 | 新建 `deployer/harness_local.py` 注册为 `harness` 方法：`generate` 阶段把 `AgentSpec`（model_id / system_prompt / tools / skills / memory / toolkits）渲染进模板——复用 `harness_convert.py` 现有的 graft 函数（配置包、Gateway soft-fail、KB 直连、`runtimeUserId`）；`package / provision / deploy` 阶段直接复用 `zip_runtime.py`（打包、每 agent 执行角色、CreateAgentRuntime） |
| 3. 模型 | 模板里的模型构造改为 §3.A 的 `BedrockModel(api_key=…, region_name=…)`；Harness 原生的 `bedrockModelConfig.apiFormat`（converse_stream / responses）只保留 converse |
| 4. 工具 | Gateway 工具：AWS_IAM 网关 + `sigv4_gateway_transport.py.tmpl`；内置 code-interpreter / browser：中国区 AgentCore 内置工具可用，模板里用 `bedrock_agentcore.tools`；Harness 原生 `shell` / `file_operations`：Runtime microVM 同样有文件系统与 shell，由 Strands 工具实现；技能：`AgentSkills` 加载 S3 技能包（zip_runtime 已有） |
| 5. 记忆 | 接 §3.H 自建后端（`SnapshotSessionManager` + OpenSearch `MemoryStore`） |
| 6. 控制台 | 创建向导方式B 表单、Agent 详情、VERSIONS & ENDPOINTS（读 Runtime 版本而非 Harness 版本）、“转换为 Runtime”按钮删除（已经是 Runtime） |

**改动点**：`deployer/harness.py`（728 行，AWS Harness API 调用）→ 替换为 `harness_local.py`（约 300–500 行，大部分复用 `harness_convert.py` 与 `zip_runtime.py`）；`services/agentcore/harness.py` 包装删除；`invoke.py` 的 Harness 分支（`InvokeHarness`、事件流解析）删除——自建 Harness 就是普通 Runtime；`optimization/canary_harness.py` 删除；IAM 中 `InvokeHarness`、`harness/*` 语句删除。

**与原版的差异**：没有 Harness 服务端的托管 agent loop（迭代/令牌预算、`harness.execution_limit` 等停止原因）——由 Strands 的 agent loop 与模板内的预算参数实现；部署时间从“约 30 秒无需构建”变为 zip-runtime 的打包 + CreateAgentRuntime（分钟级）；30 秒体验可通过预热依赖层/缓存 wheel 缩短。

## 4. 建议的改造路线

1. **Phase 0 · 中国区账号实测（1–2 天）**：用 boto3 在 cn-north-1 验证 `bedrock-agentcore-control.ListAgentRuntimes`、`CreateGateway(authorizerType=AWS_IAM)`、`CreateApiKeyCredentialProvider`、CodeBuild ARM64 镜像、Lambda arm64、X-Ray `GetTraceSegmentDestination`（Transaction Search）。这些决定 §3 中“需实测”项的取舍。
2. **Phase 1 · 骨架可跑**：分区/域名修复（§D）；bootstrap/workspace 探针与阶段裁剪（§E）；删除方式A、Skill Lab、Cedar Policy、AWS Harness/Eval/KB 实现（§C），Registry 切到本地账本后端（§3.I）；CDK 去 Cognito/Bedrock（§B/§E）。目标：`make bootstrap` 在 cn-north-1 成功，控制台显示 Agents / Chat / Registry（本地） / Identity / Gateway / Observability / Workspaces / Users。
3. **Phase 2 · 模型层**：Global 账号生成 Bedrock API key（并申请 GLM 5.3 开通）→ 中国区 Identity API-key provider → `AgentSpec` 加 `model_region`/`model_credential` → Strands 模板 `BedrockModel(api_key=...)`（§A）；先用 Kimi K3 联调，再切 GLM 5.3。目标：方式C 与 BYOC 的 agent 能在中国区 Runtime 上通过 Global Bedrock 回答问题并流式输出；Studio AI 修复切到 Kimi 后端（D6）。
4. **Phase 3 · 工具与认证**：Gateway 改 AWS_IAM + SigV4 transport（§B），hr-database Lambda / office-facts OpenAPI 两个示例目标跑通；Identity Connections 保留 API-key / CustomOauth2。
4.5 **Phase 3.5 · Memory + Knowledge Base 自建后端**（§3.H）：OpenSearch 域 + SQS + Lambda 上线，Titan 向量化；KB 摄取/检索与 `kb_search` 工具；模板切到 `SnapshotSessionManager(S3Storage)` + OpenSearch `MemoryStore`；控制台记忆页与 Chat 记忆栏回归验证。可与自建 RAG 知识库共用同一域。
4.7 **Phase 3.7 · 自建 Harness（方式B）**（§3.L）：vendor Global Harness 导出模板 → `harness_local` 方法 → 创建向导与架构师助手改为产出该规格（D7）。
5. **Phase 4 · 可观测与收尾**：控制平面 EC2 落地（§3.K：第一版不用 CloudFront，私网/受限 CIDR 入口，模板去 CloudFront/改分区，cookie secure 标志随 TLS 位置设置）；Observability 适配（有/无 Transaction Search 两条路径）；镜像源与国内 PyPI/npm 镜像（§F）；文档、测试、i18n 清理。
6. **Phase 5 · 评估 / 优化 / A/B 自建**（§3.J，**2026-10-10 纳入 = N12**，引擎跑在 AgentCore Runtime 异步 worker 上；随后 **N13 Skill Lab on OpenCode**）：judge 模型接通 → 以 Strands Evals 为引擎的批量评估（CloudWatchProvider + 内置评估器） → 本地 ConfigBundle + 调用链 A/B → `gepa_lite` 推荐闭环 → 在线评估 → 目标金丝雀 → insights / 模拟角色。

**粗略体量**：删减多于新增。需要新写的核心是“跨分区 Bedrock 凭证链”（Identity API-key provider 的 bootstrap 阶段 + `AgentSpec` 两个字段 + 模板里 `BedrockModel(api_key=...)` + IAM 调整 + 前端目录，约数百行）和“AWS_IAM 网关接入 zip-runtime 生成代码”（复用已有 SigV4 模板）；加上本地 Registry 后端（约 600–900 行，§3.I）与 Memory 自建后端（§3.H）；评估 / 优化 / A/B 若按 §3.J 以 Strands Evals + Harness Optimizer 自建再加约 1.5–2.5k 行（可选的 Phase 5）；再加自建 Harness 方法（约 300–500 行，§3.L）与自建 KB（约 800–1200 行）；其余是删除方式A / Cedar Policy / Skill Lab、修 ~130 处分区/域名/区域字符串、重写文档与测试。

---

## 5. 待验证事项（需中国区账号实测）

1. CloudWatch **Transaction Search**（X-Ray → `aws/spans`）在 cn-north-1 是否可开启；若否，Observability 只读 `/aws/bedrock-agentcore/runtimes/*` 统一日志组。
2. AgentCore **Gateway rate limits**（`ListGatewayRateLimits` 等）在中国区是否存在（文档未列入不可用，但也未明确）。
3. ~~CodeBuild ARM64~~（已实测可用，§7）；Lambda `ARM_64` 待 bootstrap 时验证。
4. AgentCore Runtime **A2A 协议**在中国区是否可用（文档未列入不可用）；若可用，`strands_a2a_agent` 模板可保留。
5. ~~botocore 端点解析~~（已实测自动解析，§7）。
6. 中国区 Runtime（`networkMode: PUBLIC`）到 `bedrock-runtime.us-west-2.amazonaws.com` 的出境连通性、时延与流式稳定性（Global 侧 GLM 5.3 权限、Converse、tool use 已实测通过，§7）。
7. `strands-agents==1.47.0` 是否已含 `strands.memory.MemoryManager` / `strands.session.SnapshotSessionManager` / `strands.storage.S3Storage`（Memory 自建方案依赖）；中国区 OpenSearch Service k-NN 索引 + 执行角色 SigV4 访问实测；Runtime `PUBLIC` 网络到 OpenSearch 公网端点或改 VPC 模式。
8. InvokeAgentRuntime 是否透传客户端 `baggage` 头到容器（决定 A/B 臂标记走 baggage 还是 `X-Amzn-Bedrock-AgentCore-Runtime-Custom-*` 头）；`CreateAgentRuntimeEndpoint` 命名端点在中国区可用性（目标金丝雀依赖）。
9. `strands-agents-evals` 1.4.0 的 `CloudWatchProvider` 对中国区 CloudWatch Logs（`amazonaws.com.cn` 端点）与 AgentCore runtime 日志组格式的兼容性实测；`strands-harness-optimizer` 优化器内部 `BedrockModel(**model_config)` 是否透传 `api_key`/`region_name`。
10. 中国区 AgentCore 当前的 **Runtime platformVersion**（V2 新 Runtime 仅在 us-east-1/us-east-2/us-west-2/eu-west-1/ap-northeast-1 发布）。

---

## 附：核实来源

- docs.amazonaws.cn《Amazon Bedrock AgentCore in Amazon Web Services China》：https://docs.amazonaws.cn/en_us/aws/latest/userguide/bedrock-agentcore.html
- docs.amazonaws.cn《Amazon Cognito in Amazon Web Services China》：https://docs.amazonaws.cn/en_us/aws/latest/userguide/cognito.html
- docs.amazonaws.cn《Amazon Lambda / CodeBuild / X-Ray / CloudWatch / ECR / CloudFront in AWS China》差异页
- AWS 服务目录区域可用性（Amazon Bedrock、Bedrock Knowledge Bases：cn-north-1 / cn-northwest-1 = isBeingPlannedIn；AgentCore / CodeBuild / X-Ray / API Gateway = isAvailableIn；Cognito：cn-north-1 isAvailableIn（仅 Identity Pools）、cn-northwest-1 isNotExpandingIn）
- botocore develop 分支 `endpoints.json` / `partitions.json` / `bedrock-agentcore/2024-02-28/endpoint-rule-set-1.json`
- AWS 全球文档《Amazon Bedrock AgentCore endpoints and quotas》（全球区端点列表，不含中国区）
- Bedrock 模型卡：Kimi K3（https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-moonshot-ai-kimi-k3.html）、GLM 5.3（https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-zai-glm-5-3.html）；What's New 2026-09 Kimi K3 GA、2026-10 GLM 5.3 GA
- Strands Evals 文档（evaluators / detectors / simulators，strandsagents.com/docs/user-guide/evals-sdk/）与 PyPI `strands-agents-evals` 1.4.0 轮包内容；Strands Harness Optimizer（github.com/strands-labs/harness-optimizer README，PyPI 0.0.1）
- Bedrock API keys（https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html）；Strands `BedrockModel(api_key=...)` 参数（strandsagents.com API 文档）

---

## 6. 待沟通清单（2026-10-08 复查）

### 6.1 已定（本文依据）

| 事项 | 决定 |
|---|---|
| 模型 | 中国区 Runtime 跨分区调 Global Bedrock 的 Kimi K3 / GLM 5.3，Bedrock API key 经中国区 AgentCore Identity 托管（§3.A） |
| Memory | 自建后端保留：S3 会话快照 + OpenSearch k-NN + SQS/Lambda 抽取（§3.H） |
| Registry | 本地账本自建保留（§3.I） |
| Evaluations / Optimizations / A/B | Strands Evals + Harness Optimizer 为引擎，运行在 AgentCore Runtime 异步 worker 上，A/B 分流下沉到 Launchpad 调用链；**2026-10-10 纳入（D14，N12）** |
| CloudFront | 第一版去掉；控制台入口为受限 CIDR / 私网（§3.K） |

### 6.2 决策记录（2026-10-08 已全部确认；D12 于 2026-10-09 新增；D13 added 2026-10-10）

| # | 问题 | 选项与建议 |
|---|---|---|
| D1 | 方式B（Managed Harness） | **已定：自建 Harness** —— 以 Global 区 Harness 导出的代码为模板（repo `harness_convert.py` 的 agentcore CLI 导出 + 配置包嫁接路径），方式B 的声明式体验保留，底层部署为中国区 zip-runtime。见 §3.L |
| D2 | 方式A（Claude Agent SDK 容器） | **已定：删除**，容器场景由 BYOC 覆盖 |
| D3 | Governance / Cedar Policy | ~~已定：不自建。保留 Gateway 目标管理与每 agent 工具 allowlist；Policy Engine、Cedar 策略、决策日志下线~~ **2026-10-14 被 D20 撤销：Policy 自建**（见 D20） |
| D4 | Knowledge Bases | **已定：自建 RAG 知识库**，与 Memory 共用 OpenSearch 域，向量化用 Titan（D8）。见 §3.H 末尾“自建 KB” |
| D5 | Skill Lab | ~~已定：删除~~ **2026-10-10 被 D16 撤销：以 OpenCode 为执行 CLI 恢复** |
| D6 | Studio “AI 修复”（codegen） | **已定：改为 Kimi 后端** —— 新增 codegen 后端走 Global Bedrock Kimi K3（OpenAI 兼容 / Converse），替换默认的 Claude Agent SDK 后端 |
| D7 | 架构师助手 + 系统预置 agent | **已定：保留**。助手产出的提案改为自建 Harness（D1）规格；预置 agent 的模型改 Kimi/GLM；其调用的 AWS Knowledge MCP（`knowledge-mcp.global.api.aws`）需实测中国区可达，不可达则该预置降级为无 MCP |
| D8 | 向量化 | **已定：Global Bedrock Titan Text Embeddings V2**（`amazon.titan-embed-text-v2:0`，同一把 API key，跨境） |
| D9 | Gateway / Runtime 入站认证 | **已定：v1 全部 AWS_IAM**，JWT 入站与 Chat “以我身份调用” 分支先下线（代码保留开关） |
| D10 | 默认模型与区域 | **已定**：GLM 5.3 开通后为默认，未开通前 Kimi K3；Bedrock 入口区 us-west-2 |
| D11 | 代码策略 | **已定：fork 出独立分支**（如 `cn-region`），不以 feature flag 回合上游；仍建议把改动集中在后端协议层，便于日后 cherry-pick 上游修复 |
| D12 | `apps/` 目录（vendored 的独立 Studio 画布应用 `apps/studio`） | **已定（2026-10-09）：删除**。评估确认无任何运行时/构建依赖（控制台自带的 `frontend/src/studio` 与 `backend/app/templates/studio_agent` 自包含），已在 `cn/integration` 以 e6f8d76 删除并同步 10 处文档；N2 对 `apps/studio` 的默认模型修改随之作废（见 `docs/cn/reports/n2-w1-model.md` 撤回注记）。评估报告：`APPS-DELETION-EVALUATION.md`（仓库外） |
| D13 | Parallel environments in one account+region (`EnvName` suffix) | **Decided (2026-10-10): add one optional knob, `EnvName`**, threaded through all three layers — EC2 template parameter `EnvName`, CDK context `-c env_name=<x>`, backend `Settings.env_name` (`LAUNCHPAD_ENV_NAME`) — so a failed round-1 attempt no longer has to be fully deleted before the next one is created. Empty (default) keeps every stack / physical resource / export name byte-identical (pinned by a pre-change snapshot test); `t1` appends `-t1` at the END of every fixed `launchpad-*` name, keeping the ops role's `launchpad-*` IAM wildcards valid. Pattern `^[a-z0-9]{0,3}$` (OpenSearch's 28-char domain limit is the binding constraint). Shared on purpose: `launchpad-global-bedrock`, CDKToolkit, Transaction Search, SSM secret parameters (operator chooses). Report: `docs/cn/reports/n11-env-name.md`; usage: `docs/cn/interfaces/deploy.md` §3.1 |
| D14 | Evaluations / Optimizations / A/B（Phase 5） | **已定（2026-10-10）：纳入本轮，作为节点 N12。前端沿用 Global 版评估/实验/金丝雀/数据中心页面不改（从 fork 基线 `4e27b1f4` 原样恢复），后端保持同一 HTTP API 契约，把 `evaluation/agentcore_eval.py` 边界内的 AgentCore Evaluations / Optimizations / Dataset / ConfigBundle / ABTest / Gateway rules 调用整体替换为 §3.J 的自建实现：`strands-agents-evals` 为评估引擎（评估器、CloudWatchProvider、detectors、ActorSimulator），`strands-harness-optimizer` + 既有 `gepa_lite` 为推荐引擎，配置包与 A/B 分流下沉到 Launchpad 调用链（`invoke_agent_text` / `chat_stream`），金丝雀复用 Runtime 版本/命名端点；judge 模型走 Global Bedrock 同一把 API key。** |
| D15 | 部署运维由谁执行 | **已定（2026-10-10，用户建议）：新增专职 deploy-ops 子会话。** 约束 C8 修订为“实现/审查类子会话不得创建或修改 AWS 资源；deploy-ops 子会话可在书面信封内操作中国区账号 123456789012 的测试环境（栈 `launchpad-cn-ec2`、`launchpad-cn-ec2-t1` 及其 CDK 栈、两台实例的 SSM）”。信封：只做 git pull / 依赖同步 / 前端重建 / `make bootstrap` / 显式 `cdk deploy` / 服务重启 / 只读巡检 / e2e 重跑；**禁止**删除栈或实例、修改 IAM（模板之外）、触碰 Global 账号、把任何密钥写入文件或日志；破坏性操作须回到主 agent 请用户确认。 |
| D16 | Skill Lab（撤销 D5） | **已定（2026-10-10）：恢复 Skill Lab，与 Global 版一致，只把执行 CLI 换成 OpenCode。** 前端 Skill Lab 页面、`skill_lab/` 后端路由与 SkillOpt 三条流水线（taskgen / train / eval）、Registry 联动从基线 `4e27b1f4` 原样恢复；SkillOpt 的 `claude_code` / `codex` 模型后端替换为 `opencode`（无头 `opencode run --format json`，自定义 OpenAI 兼容 provider 指向 Global Bedrock `/openai/v1`，API key 经 Identity 取回后只以环境变量注入）；worker 为 BYOC 容器镜像（OpenCode 需 node/二进制，zip-runtime 不适用），基础镜像与 npm 走国内镜像，异步运行在 AgentCore Runtime 上，复用 N12 作业协议预留的 `skill_taskgen / skill_train / skill_eval`。方式A（D2）保持删除。节点 N13，先做 OpenCode 可行性 spike。 |
| D17 | 多环境共享 OpenSearch 域 | **已定（2026-10-10）：同一账号+区域内所有 EnvName 环境共享一个 OpenSearch 域**（冷启动最长的 15–16 分钟只付一次）。`launchpad-memory` 拆为共享域栈 `launchpad-memory-domain`（无 EnvName 后缀，首个环境创建，teardown 时最后删除且仅当无环境引用）与每环境栈 `launchpad-memory[-env]`（SQS、Lambda 抽取器、FGAC 角色映射，引用共享域端点）；索引名加环境前缀（`launchpad-<env>-memory`、`-jobs`、`-kb-docs`、`-kb-<id>`；默认环境保持现名），FGAC 角色 `launchpad_memory_rw[_<env>]` 的索引权限限定在本环境前缀；运行时模板、抽取 Lambda、KB/Memory 后端统一经 `app/core/naming.py` 取索引名。**实施补充（2026-10-10，`docs/cn/reports/n11-r3-shared-os.md`）**：共享域的 FGAC master 改为域栈自有角色 `launchpad-memory-admin-<region>`（不再用 `memory_master_arn=<ops role>` 上下文——绑定到某一环境的 ops role 会在该环境销毁后让共享域失去可管理的 master），每环境后端 `sts:AssumeRole` 该角色（EC2 ops role 新增 `MemoryAdminAssume`）；每环境栈只从 SSM `/launchpad/memory-domain/<region>/endpoint` 取端点，域/角色 ARN 由固定名计算，无跨栈 Export 耦合；EnvName `kb` 保留（其前缀与默认环境 `launchpad-kb-*` 重叠）；teardown 默认保留域栈，`--delete-shared-domain` 且无其它 `launchpad-memory[-env]` 栈时才删。节点：N11 第 3 轮 infra+memory 组。 |
| D20 | Policy 自建（撤销 D3） | **Decided (2026-10-14): self-hosted Cedar Policy module (N14, plan `docs/superpowers/plans/2026-10-14-d20-policy.md`).** AgentCore Policy / Policy Engines do not exist in `aws-cn`, so Launchpad evaluates Cedar in-process (`cedarpy` 4.12.1 behind the `PolicyEngine` Protocol), stores policies in the ledger + immutable S3 copies under `policy/`, enforces at the hub invoke gate and by deploy-time narrowing of the Gateway tool surface (Phase 1), adds a runtime guard in Phase 2, and audits every decision to `policy_decisions` plus CloudWatch `/launchpad/policy/decisions[-env]` (batched, fail-soft sink; the `policy` bootstrap stage creates the group). Nothing AgentCore-Policy-shaped is reintroduced (C5). Contract: `docs/cn/interfaces/policy-backend.md`. |

| D21 | 统一模型接入（取代 N-R6 的环境变量目录） | **Decided (2026-10-14, ruling ⑮): one admin-only Settings → Model Providers registry (N15, plan `docs/superpowers/plans/2026-10-14-d21-model-providers.md`).** Providers (kinds `bedrock` / `openai_compatible` stay code-defined), models and per-model capabilities live in the ledger (`model_providers`, `models`, journal = catalog version, per-workspace enable flags) and are published as an in-process snapshot every module reads — wizard, Studio, architect, judges, optimizer, Skill Lab, Memory/KB embeddings. Credentials are SSM parameter names resolved by the hub into AgentCore Identity API-key providers (`launchpad-<provider>`), never stored. Capabilities are probed at registration and admin-editable (`source: builtin|probed|manual`). Empty registry = today's tables (seed + parity test); `PROVIDERS`/`MODEL_CATALOG`/`LAUNCHPAD_SILICONFLOW_MODELS` removed after the seed (C5). Embedding model change refused while vector indices hold data (pilot). |

### 6.3 对方提供的信息（I1–I4 已确认，I5–I7 待补）

| # | 信息 | 用途 |
|---|---|---|
| I1 | 目标区域与账号 | **已定：cn-northwest-1（宁夏）**，账号 123456789012（本机 profile `cn-northwest-1`；`cn-north-1` profile 的密钥已失效）。当前凭证为 IAM 用户 `example-operator`，属 Administrators 组（测试账号） |
| I2 | Global 账号 | 账号 210987654321（本机 `default` profile，us-west-2）。**已实测**：`global.zai.glm-5.3` 与 `global.moonshotai.kimi-k3` 推理档均可用，Converse / ConverseStream / tool use 正常（见 §7） |
| I3 | 数据跨境合规口径 | **已定：全部为测试数据**，跨境调用可接受；向量化用 Titan（D8） |
| I4 | 控制台访问方式 | **已定：不开放外网访问**。EC2 安全组仅放行办公网/VPN 网段或私网访问，不配 CloudFront/ALB 公网入口（§3.K） |
| I5 | 中国区网络：PyPI / npm / Docker 镜像源或代理地址，CodeBuild 与 EC2 的出网策略 | §3.F 构建链路 |
| I6 | 预期用户与规模：内部 PoC 还是对外演示；并发；是否多账号 workspace | 决定 OpenSearch 规格、是否保留 Workspaces 跨账号功能、SQLite 单机是否够用 |
| I7 | 现有 OIDC IdP（若有） | D9 |

### 6.4 需在中国区账号实测后才能定稿（与 §5 对应）

Transaction Search（决定 Observability 读哪条日志路径）；CodeBuild ARM64 与 Lambda arm64；A2A 协议与 `GetAgentCard`（决定 A2A 模板与 Registry LIVE CARD 是否保留）；`CreateAgentRuntimeEndpoint` 命名端点（目标金丝雀）；InvokeAgentRuntime 是否透传 `baggage` 头；Runtime 出境到 `bedrock-runtime.us-west-2.amazonaws.com` 的连通性与时延；Strands 1.47 / Strands Evals 1.4.0 / Harness Optimizer 在中国区端点下的兼容性；Ubuntu arm64 AMI 的 SSM 公共参数在 cn 区可解析。

### 6.5 两个需要提前说清的预期

- **Runtime 只有 V2（已实测，§7）**：宁夏区 `CreateAgentRuntime` 只接受 `platformVersion=V2`，即新一代运行时（P75 冷启动约 2 s）。代码里必须显式传 `platformVersion="V2"`。
- **功能面缩减是服务层面的，不是实现缺陷**：Harness、Policy、Payments 在中国区没有且本文未规划替代；Memory / Registry / Evaluations / KB 的自建版本在功能上对齐控制台契约，但抽取、判题、检索质量由自写提示词与开源包决定，与 AWS 托管版不完全一致。

---

## 7. 宁夏区（cn-northwest-1）与 Global 账号实测记录（2026-10-08）

| 项 | 结果 | 对方案的影响 |
|---|---|---|
| 凭证 | `cn-northwest-1` profile 有效：账号 123456789012，IAM 用户 `example-operator`（Administrators 组）；`cn-north-1` profile 密钥无效 | 目标区域定为 cn-northwest-1；控制平面建议改用实例角色而非该管理员密钥 |
| AgentCore 端点解析 | boto3 1.43.109 自动解析到 `bedrock-agentcore-control.cn-northwest-1.amazonaws.com.cn`，无需 `endpoint_url` | §1 推断成立；repo 需升级 boto3 ≥ 1.43.109（本机 1.43.90 的模型无 `platformVersion`） |
| **Runtime platformVersion** | `CreateAgentRuntime(platformVersion="V3")` → `ValidationException: platformVersion must be one of: V2`；`V2` 创建成功（随后删除） | **中国区 Runtime 只有 V2**（新一代运行时，V1 不接受）。§6.5 原“只有 V1”的预期**作废**；repo 所有 `CreateAgentRuntime` 调用必须显式传 `platformVersion="V2"`（`services/agentcore/runtime.py` 两处 params） |
| Code Interpreter / Browser | `ListCodeInterpreters(type=SYSTEM)` → "AgentCore Code Interpreter"；`ListBrowsers(type=SYSTEM)` → "AgentCore Browser Tool" | 内置工具演示可保留 |
| Memory / Evaluators / Harness 操作 | `AccessDeniedException: Unable to determine service/operation name to be authorized` | 端点不认识这些操作，与文档一致 |
| Gateway / Identity API-key provider / Runtime 列表 | 均可调用，账号内尚无资源 | 干净环境 |
| X-Ray Transaction Search | `GetTraceSegmentDestination` → `Destination=XRay, Status=ACTIVE`（未开启 CloudWatch Logs 目的地） | API 在宁夏区可用；bootstrap 时执行 `UpdateTraceSegmentDestination(CloudWatchLogs)` 即可，`aws/spans` 路径有望保留（开启后需复核） |
| CodeBuild ARM64 | 24 个 `aarch64` 托管镜像可用 | 容器构建链路可用（基础镜像仍需国内源） |
| Ubuntu 24.04 arm64 AMI SSM 参数 | 可解析（`ami-0abcdef1234567890`） | `workshop-ec2.yaml` 的 AMI 查找方式可沿用 |
| OpenSearch / ECR | 宁夏区均无既有域/仓库（北京区有一个 `knowledge-base-simple` 域） | 需新建 |
| Global：推理档 | `global.zai.glm-5.3`、`us.zai.glm-5.3`、`global.moonshotai.kimi-k3`、`us.moonshotai.kimi-k3` 均在列表中 | GLM 5.3 已有权限，无需额外申请 |
| Global：Converse | GLM 5.3 单轮 1.3 s；Kimi K3 1.2 s；GLM 5.3 流式首 token 1.8 s（从 us-west-2 本机测，非中国区出境链路） | 可用；中国区 Runtime 出境时延待部署后实测 |
| GLM 5.3 行为 | reasoning 常开：`maxTokens=60` 时输出为空（预算被推理占满），`maxTokens=600` 正常，返回 `text` + `reasoningContent` 两个块 | 模板与评估调用需 `maxTokens ≥ 200` 并忽略/单独展示 `reasoningContent`；Strands `BedrockModel` 默认处理 |
| GLM 5.3 tool use | `stopReason=tool_use`，正确选择工具 | 满足 agent 场景 |

探测遗留资源（可复用于正式部署，或删除）：IAM 角色 `launchpad-cn-probe-runtime-role`（仅信任策略 + 对探测桶的只读）、S3 桶 `launchpad-cn-probe-123456789012`（私有，含 `probe/main.zip`）。探测用 Runtime 已删除。
