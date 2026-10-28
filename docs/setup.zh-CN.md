# 环境搭建 / Setup

English: [setup.md](setup.md)

## 前置条件

- 已在 `us-west-2` 开启 Bedrock AgentCore 预览的 AWS 账号（Runtime、Harness、
  Gateway、Policy、Evaluation）；Agent Registry 已 GA（自 2026-08-06 起使用
  `agent-registry` 命名空间，见 [registry-ga-migration.md](registry-ga-migration.md)），
  无需开启预览
- 具备管理员级别权限的凭证(`aws sts get-caller-identity` 可用)
- `uv` ≥ 0.8、Node.js ≥ 20(`npm`)、AWS CDK CLI v2(`npm i -g aws-cdk`)、
  Docker(支持 ARM64,第 5 阶段容器路径需要；也可用 [finch](https://github.com/runfinch/finch)，见下文)
- 每个账号/区域执行一次 CDK bootstrap:`cdk bootstrap aws://<account>/us-west-2`

## 引导(Bootstrap)

```bash
# 1. 安装依赖
cd backend  && uv sync && cd ..
cd frontend && npm install && cd ..
cd infra    && uv sync && cd ..

# 2. 部署共享基础设施 + AgentCore 单例,写出 config/launchpad.yaml
make bootstrap          # = cd backend && uv run python ../scripts/bootstrap.py
```

该引导是**幂等**的:CDK 栈(`launchpad-base`)仅在缺失时部署,AgentCore 注册表
(`launchpad-registry`)/ memory(`launchpad_memory`)只创建**一次**,后续每次运行
都复用。再次运行只会打印 `reused`,不会产生重复资源。

bootstrap 还负责安装 Harness 转 Runtime 时使用的 CLI。它会把固定版本
`@aws/agentcore@0.21.1` 安装到
`data/agentcore-cli/node_modules/.bin/agentcore`,不需要全局 npm 安装;安装后会校验
版本,后续运行直接复用。转换过程不会使用 `PATH` 中的 `agentcore`;如果这份托管安装
被删除或不可用,重新运行 `make bootstrap`。该版本同时支持不带 Skill 的 Harness
导出,以及生成代码调用
`get_or_create_agent(session_id, user_id, _skill_plugins)` 的 Skill 导出。

创建内容:

| 资源 | 名称 |
|---|---|
| S3 产物桶 | `launchpad-artifacts-<acct>-<region>` |
| ECR 仓库 | `launchpad-agents` |
| CodeBuild(ARM64) | `launchpad-agent-builder` |
| Cognito 用户池 | `launchpad-users`(+ 组 `platform-admin`、`hr-analyst`,演示用户 `admin`/`demo`) |
| IAM 执行角色 | `launchpad-agent-execution-role` |
| AgentCore Registry | `launchpad-registry` |
| AgentCore Memory | `launchpad_memory`(短期事件 + 语义、用户偏好、会话摘要、情节这四种长期策略;对已存在的 memory 重跑 bootstrap 会补齐缺失的策略) |
| AgentCore Gateway | `launchpad-gw-<suffix>` |
| 托管 AgentCore CLI | `data/agentcore-cli/` (`@aws/agentcore@0.21.1`) |

演示用户密码由 bootstrap 生成并存入 `config/launchpad.yaml`(**已 gitignore**——
视为本地机密;仓库中提交的是脱敏的 `config/launchpad.example.yaml`)。

### Policy 由用户显式管理

bootstrap 会创建共享 Gateway，但不会创建 Policy Engine、创建 Cedar Policy、挂载
Engine，也不会替用户选择 Gateway 的执行模式。重跑 bootstrap 时，已有 Policy 资源与
挂载关系同样保持不变。

需要 Policy 时，请在治理页面中显式纳管目标 Gateway，创建或选择 Engine，以默认
`ENFORCE` 或可选 `LOG_ONLY` 模式挂载。`ENFORCE` 默认拒绝，因此应在依赖 Gateway
流量前创建并审核明确放行的 Policy。

bootstrap 仍会为通用可观测性开启 CloudWatch Transaction Search。切换 X-Ray 目标之前，它会放置
CloudWatch Logs 资源策略 `TransactionSearchXRayAccess`，允许 X-Ray 写入 `aws/spans` 日志组；
已有策略授予了这一权限时则不再放置。bootstrap 不创建逐条 Policy
决策 span 所需的 per-Gateway CloudWatch Logs delivery。Policy 配置完成后，决策计数
仍来自 CloudWatch 指标；逐条决策明细需要另行管理 trace delivery。

## 本地运行

```bash
./start.py          # 后台开发模式
./start.py --prod   # 构建并运行本地生产预览
./stop.sh
```

需要绑定当前终端的前台开发栈时,使用 `make dev`。

本文档中提到的每一个配置项，都既可以写成 `config/launchpad.yaml` 里的 key，也可以
写成同名大写加 `LAUNCHPAD_` 前缀的进程环境变量（`database_url` →
`LAUNCHPAD_DATABASE_URL`）。优先级为：默认值 < `config/launchpad.yaml` < 环境变量 <
init kwargs，因此环境变量总是压过 bootstrap 生成的那份文件。有三个 key 决定后端本身
如何启动：

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `database_url` | `sqlite:///<repo>/data/launchpad.db` | 台账的 SQLAlchemy URL。默认是 `data/` 下的单个 SQLite 文件；它只存标识符与派生进度——AWS 始终是权威状态——所以丢掉台账损失的是控制台历史，而不是资源本身。改这个值即可迁移文件位置。 |
| `cors_origins` | `["http://localhost:5173", "http://127.0.0.1:5173"]` | 允许调用 `/api` 的浏览器 origin。默认值覆盖开发态前端的两种 loopback 写法；当控制台由别的主机或端口提供时，需要把自己的 origin 加进来。`./start.py --prod` 让控制台与 API 同源，无需新增条目。 |
| `agentcore_read_timeout_s` | `1000` | AgentCore 数据面客户端的 boto 读超时（`backend/app/services/agentcore/client.py:30`）。AgentCore 同步调用**最长可运行 15 分钟**，因此该值必须高于这条服务侧上限——botocore 默认的 60 秒会在缓冲式 agent 返回最终响应之前就放弃。 |

### 控制台登录

控制台支持本地账户登录,不依赖 Cognito 或其他 AWS 服务。未配置密码时登录网关关闭,
此时控制台顶栏会显示 `AUTH OFF` 徽标。

**未认证的控制台只响应 loopback 调用方。** `./start.py --prod` 会把两个服务都绑定到
`0.0.0.0`,因此对外可达的部署必须配置密码;未配置时,来自任何非 loopback 地址的
`/api` 请求都会被拒绝(`auth.open_console_refused`),并且 `./start.py` 会在启动前
检查失败而不是继续拉起服务。`/api/health` 与登录端点保持可达,以便被挡在外面的
运维仍能看到登录门禁。

```bash
export LAUNCHPAD_AUTH_USERNAME=admin
export LAUNCHPAD_AUTH_PASSWORD='replace-with-a-strong-password'
./start.py
```

会话使用 12 小时 HttpOnly Cookie。生产模式(`run_mode: prod`,`./start.py --prod`
会设置)下自动带上 `Secure` 并发送 HSTS 响应头;`LAUNCHPAD_AUTH_COOKIE_SECURE=true`
可在开发模式下强制开启。**两者都要求全链路 HTTPS** —— `Secure` Cookie 不会经明文
HTTP 回传,所以若 TLS 在某处终止后再以 HTTP 转发,登录会静默失败。

上述值也可写入 `config/launchpad.yaml` 的 `auth_username`、`auth_password`、
`auth_cookie_secure`,遵循常规配置优先级;密码建议放在进程环境变量中。修改凭证并
重启后端会使已有会话失效。

#### 单点登录（OIDC）

登录门开启后，管理员可以在**配置 → 单点登录**中添加 OpenID Connect 身份提供方（Keycloak、Authentik、Okta、Entra ID、Google 或任意 OIDC IdP）；每个已启用的提供方在登录页上有一个按钮。本地登录表单仍然可用，内置管理员始终可以用本地方式登录，作为应急入口。部署时要注意两项设置：

- **`public_base_url`**（`LAUNCHPAD_PUBLIC_BASE_URL`）必须是浏览器看到的控制台地址（参考部署中为 CloudFront 地址），因为在 IdP 登记的回调地址由它拼出：`<public_base_url>/api/auth/oidc/<provider-id>/callback`。提供方页面会显示要复制的准确值。该值不对时，IdP 会拒绝回跳。
- **客户端密钥的加密密钥。** 客户端密钥以 Fernet 加密保存。生产环境请在后端的环境变量（见下文 systemd 单元）中把 `LAUNCHPAD_SSO_SECRET_KEY` 设为一个 Fernet 密钥（`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`）。未设置时，后端在首次使用时于 SQLite 账本旁创建 `data/sso-secret.key`（权限 0600）。请把该文件与账本一起备份：丢失后已保存的密钥无法读取，每个提供方的密钥都要重新填写。以 Secrets Manager ARN 给出的密钥用 hub 的凭证读取，不在本地加密。

SSO 账户没有本地密码：控制台中不能重置或修改它的密码。它的全局角色与项目成员资格在每次登录时按提供方的组映射更新；管理员手工做的修改优先。

分步配置指南：[IAM Identity Center（SAML 2.0）](sso-identity-center.zh-CN.md)、[Keycloak（OIDC 或 SAML 2.0）](sso-keycloak.zh-CN.md)。

### 角色:成员能做什么

只有两个角色,自 2026-08-11 起两者只差一处:**用户管理**(`/users`)仅限管理员,
**控制台的其余全部功能对成员开放** —— 注册表的注册/编辑/导入、知识库变更、评估
数据集/评估器/运行、AB 实验与金丝雀、Cedar 策略写入、API Key、Studio 画布,以及
浏览器 / 代码解释器 demo。权威清单是 `backend/app/core/route_policy.py` 里的表;
未登记的路由会被拒绝而不是放行。(本地代码执行在生产模式下对所有角色一律禁用,
除非用 `LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED` 显式打开。)

仍有一小组能力**可在用户管理控制台按用户撤销**:智能体生命周期(部署、导入、删除、
转换)以及发起评估/洞察运行。成员默认全部持有;撤销后即可对该账号关闭部署与计费的
评估任务。

注意控制台默认没有按用户的数据隔离:在 `shared` workspace 中每个成员看到并可修改同一套
共享资源,因此成员账号只应发给可以托付整个环境的人。管理员可以把 workspace 切换为 `isolated`
(Workspaces → 详情 → 「成员可见性」卡片),此后每个成员只能看到自己创建的 Agent(连同其可观测性
trace、记忆 actor 与 A2A 注册表记录)、知识库、托管记忆、评估资产、实验、灰度与 Skill Lab 任务/任务集,
以及管理员创建或创建者未知的资源(发现导入的运行时与系统预设的创建者未知，因此保持共享)。成员也只能
列出和启停自己的 `/v1` API 密钥，成员创建的密钥只能触达该成员能看到的 Agent。MCP / 技能注册表记录与
Gateway 在两种模式下都保持共享。

### 系统托管预置

创建页的**系统预置**面板列出平台自有的预置（目前是托管 Harness
`aws-agent-solution-architect`）。引导、启动和任何页面读取都不会安装它：由**管理员**在每个
Workspace 中通过面板的“安装”按钮（或 `POST /api/system-agents/<key>/install`）显式安装，这是
一次走标准管道的计费部署（约 30 秒）。Workspace 必须处于 `ready`（引导完成，具备
`artifacts_bucket` 与 `execution_role_arn`，且启用按 Agent 的执行角色）。首次安装使用预置默认值——架构师
预置为原生 Bedrock 上的 `global.openai.gpt-6-astra`（GPT-6 Astra 全球跨区域推理配置）、单次模型
调用 `max_tokens: 65536`、`reasoning_effort: high` 与最大迭代次数 100。之后管理员可通过面板的**“配置”**按钮——它打开与既有 Agent 的“编辑”相同的配置页（管理员在表格中对预置行点击
“编辑”亦然）——或同一安装 API 请求体修改已存设置：模型来源/ID、单次模型调用最大输出 token（即 Harness 的
`bedrockModelConfig.maxTokens`——单次响应的上限，不是会话或花费上限）、推理强度（仅原生 Bedrock 上的
OpenAI GPT-5.x）、系统提示词、最大迭代次数、超时，以及要挂载的既有知识库（在 provision 阶段于 Workspace 中
核验；不会替你创建任何知识库）。“保存并重新发布”只把改动的字段提交到维护接口（绝不走普通的 redeploy）并运行标准更新任务；成员只能只读
查看同一页面（“查看设置”）。重新发布与技能包更新会保留已存设置——早期构建安装的预置会保留其模型与提示词，
直到你保存更改或点击“使用预置默认值”。没有改动时同一页面提供“重新发布”，按已存设置就地重新发布——用于重试
失败的部署或带外变更之后（没有单独的修复按钮）；“卸载”排入拆除任务——预置显示“卸载中”，并在 Harness 与角色
删除完成前保留身份；拆除失败会显示原因并提供“重试卸载”，期间安装与修复会被拒绝。预置关闭持久记忆（仅关闭 AgentCore 记忆——
对话记录仍保留在台账中，CloudWatch 日志也保留），并运行在自己的受限执行角色上（绝不使用共享角色）。成员可以像普通 Agent 一样与预置对话，但会看到“系统”标签，
编辑/重新发布/转换/删除按钮被禁用，后端也会无视权限拒绝这些调用；挂载在预置上的知识库在管理员
通过预置解除挂载之前无法强制删除。若已有普通 Agent 占用保留名称，安装会被拒绝——请先删除或
重命名该 Agent；预置绝不接管它。
预置的带版本 Skill 也会作为独立记录列入 Registry：用本构建执行的每次安装或重新发布都会在部署的
register 阶段注册它（显式的 `POST /api/system-agents/<key>/skill-registration` 在 API 侧做同样的事）。
这会让一条 `AGENT_SKILLS` 记录指向已发布在 `system-skills/` 下的版本目录（不上传任何内容，也不重新发布
Harness），并提交审核；只有当你**在 Registry 中批准**之后，其他 Agent 才能挂载它——记录在 Registry 中
管理，“系统预置”卡片上没有 Skill 记录控件。技能包更新后记录会前滚到新版本，需要再次批准。该记录带有“系统”
标签：没有人能在控制台编辑、重新导入或删除它，成员可以查看与挂载，管理员负责其审批；卸载预置会
保留该记录。
**实机冒烟尚未完成**（见 architecture.zh-CN.md →“系统托管预置”）。

### 架构助手

> **升级提示：** 本次发布把控制台会话 Cookie 升级为版本 2（绑定账号 ID）。所有已登录用户——
> 成员与管理员——会被登出一次并需要重新登录；其余无变化。

`aws-agent-solution-architect` 预置进入运行中后，Workspace 的每位成员都可以打开**架构助手**
（`/create/assistant`，可从“托管 Harness”入口卡片与“系统预置”面板进入）。粘贴 Workshop 产出，
让助手确认需求基线并只追问缺失且影响架构的问题，然后审阅它对**一个新的托管 Harness 业务 Agent**
的提案。提案只引用 Workspace 中已存在的工具、S3 技能与知识库（APPROVED 的 Registry 记录、ACTIVE
的托管知识库），并展示将要部署的**精确绑定**（Gateway ARN 与认证身份、技能 S3 路径与内容摘要、
Workspace 共享记忆或无记忆）。在你点击**批准并部署**之前它保持惰性——那是一个独立的、计费的
操作（获批的技能从 Workspace artifacts bucket 中一份与审阅字节完全一致的不可变副本部署），需要
`agents.deploy` 权限（在执行时连同你的账号与 Workspace 授权一起重新检查），并通过常规
部署任务在 Workspace 的账号与 Region 中创建 Agent；若任何绑定资源在你审阅后发生变化，批准会被
拒绝，任务本身也会在触碰 AWS 之前失败关闭。编辑提案会生成需要重新批准的新修订；取消则使其不可
执行。记忆只能是 `disabled` 或 Workspace 既有的共享记忆（含其全部策略）——没有中间态。挂载知识库
需要 Workspace 既有的知识库 Gateway；没有 Gateway 的地方属于手动工作。助手从不编辑或删除既有
Agent，也不创建知识库、Gateway 或评估器——这些以及它建议的黄金测试都是供手动实现的指导。会话按
Workspace 归属于打开它的账号（按不可变账号 ID，而非用户名），在可观测页面中对其他成员同样隐藏。**该流程的实机冒烟（真实对话、提案与批准）尚未完成。**

**从提案创建评估资产（SE-047）。** 提案面板下方的“评估资产”让对话所有者从任一形状有效的提案修订（含已批准的）
准备类型化计划，审阅映射表（黄金测试 → 带步骤与参考的 Dataset 场景；建议 → 现有评估器 / 新 LLM 评审 / 新派生 /
新代码规则 / runner 检查 / 人工评审 / 指标基线 / 外部控制），确认或阻止需要评审的场景（旧版黄金测试草拟为单轮，
从不解析为流程——多 session 测试需要类型化步骤），以 JSON 编辑（每次保存都是新修订），并——作为**拥有该对话的
管理员**——在确认披露后“创建资产”。创建只注册本地 Dataset、AgentCore 评估器以及代码规则对应的一个 Lambda（含
独立角色）；不部署、不运行、不同步到 AWS Datasets、不启用在线评估、不调用模型，“已创建”不等于通过。成员可以
准备与编辑；创建按钮会说明为何禁用。重试恢复同一批资源；“删除本操作拥有的云资产”只删除身份仍匹配的自有资源
（本地 Dataset 保留——如不需要请在 评估 → Datasets 中删除）。自有资源命名为 `launchpad-evalfn-<op>`（Lambda、
其角色、`/aws/lambda/launchpad-evalfn-<op>`）与 `launchpad-evalop-<op>`（执行角色上的附加策略）；
`scripts/teardown.py` 不认识它们——拆栈前请通过操作清理。**针对真实账号的实机创建仍待进行；未验证的 AWS
假设见架构文档“评估资产计划”。**

### 应急开关

| 变量 | 效果 |
|---|---|
| `LAUNCHPAD_ALLOW_OPEN_CONSOLE=true` | 在可达网络接口上提供未认证的控制台。恢复硬化前的行为,仅可用于可信网络。 |
| `LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true` | 在生产模式下重新启用本地代码执行(见下)。 |
| `LAUNCHPAD_STUDIO_EXEC_BACKEND=docker` | 让本地调试代码在一次性 Docker 容器里运行而非宿主子进程 —— 同时也使这些端点在生产模式可用(见下)。 |
| `LAUNCHPAD_AUTH_COOKIE_SECURE=false` | 当控制台前面实际未终止 TLS 时,去掉 `Secure`。 |

没有任何开关可以关闭角色授权:能关掉授权的开关本身就是漏洞。要修正误分类的路由,
请直接改 `route_policy.py`。

### 按 Agent 的执行角色

每个已部署的 agent 都获得一个由其 spec 派生的独立 IAM 执行角色,而不是所有 agent 共用
一个 `launchpad-agent-execution-role`。目的在于隔离:在共享角色下,任何 agent 都能挂载
其他任意 agent 的文件系统、读取所有 agent 的 skill 包、检索账号内任意知识库,并改写
gateway 路由。

角色命名为 `launchpad-agent-{name}-{agent-id 前缀}`,并打上 `launchpad:agent-id` 标签。
它们在 `provision` 阶段创建,在重新发布时对齐(被去掉的能力会让策略收缩),并随 agent
一起删除。

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `per_agent_execution_roles` | `true` | 设为 false 可回退到共享角色。 |
| `agent_role_count_warn_threshold` | `800` | 达到该数量后开始告警。 |

**IAM 默认配额是每账号 1000 个角色**,本特性按 agent 线性消耗。demo 规模下不成问题,
但真撞上配额时,表现会是一次莫名其妙的部署失败。

**已有 agent 仍在共享角色上正常运行。** 迁移方式是**重新发布**,而不是手写
`UpdateAgentRuntime`——后者会重置未传的字段,从而静默清掉文件系统挂载、protocol 配置或
环境变量。用下面的命令查看还有哪些未迁移:

```bash
cd backend && uv run python scripts/migrate_agent_roles.py
```

请**先**运行 `scripts/migrate_pin_requirements.py --apply`:重新发布会重新校验 spec,
未固定版本的依赖现在会被拒。

> **为什么共享角色仍然存在、且仍带着宽泛授权。** 它支撑那些尚未重新发布的 agent。在所有
> agent 迁移完成前缩减它,会直接抽掉仍在使用它的 agent 的授权。这项缩减刻意尚未执行;
> 信任策略里的 `aws:SourceArn` 条件同样尚未启用(需先探明 AgentCore 是否会发送该 key)。
> 另外注意 per-agent 角色**不**提供什么:记忆仍是单一共享实例、按 actor id 分区而非按
> IAM 隔离,而账号的 1000 个角色配额现在按每个 agent 一个的速度消耗。

**部署成功并不能证明策略正确。** 这些策略是按 spec 收窄的,而过紧的语句会在**调用**时
失败,而不是部署时。迁移之后,请逐个调用 agent 并检查 CloudTrail 是否出现
`AccessDenied`。

### 依赖与镜像供应链

**依赖必须固定版本。** `spec.requirements` 的每一项都必须指向唯一且不可变的产物——
`name==version`、带 `#sha256=` 的直链 URL,或
`pkg @ git+https://…@<40 位 commit>`。范围写法会在校验阶段被拒,错误信息里会给出
要求的形式。平台自带的依赖清单刻意保留范围;可复现性来自下面的 lockfile,而不是把
它们手工 pin 死。

已有 agent 可能早于该规则。在它们下一次部署之前不会有任何影响,可用下面的命令检查:

```bash
cd backend && uv run python scripts/migrate_pin_requirements.py
cd backend && uv run python scripts/migrate_pin_requirements.py --apply
```

同一脚本还会列出没有记录 commit 的 git skill 记录;这些需要从注册表**重新导入**,
因为拿到 commit SHA 必须发起一次拉取。

**每次 zip 构建都会锁定。** package 阶段用 `uv pip compile --generate-hashes` 针对
部署目标(aarch64、Python 3.13)解析依赖,再以 `--require-hashes` 安装,因此被替换或
重新上传过的发行包会让构建失败而不是被打包进去。lock 文件以 `requirements.lock` 随
部署 zip 一起下发。**因此后端在部署时需要 PATH 上有 `uv` CLI 且能访问包索引**;解析
失败就是部署失败——不会退回到未校验的安装路径。

**容器镜像会被扫描,并按 digest 部署。** ECR 在推送时扫描,package 阶段在镜像存在
达到或超过 `image_scan_block_severities` 的漏洞时拒绝继续:

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `image_scan_enabled` | `true` | 设为 false 可跳过该闸门(任务日志会写明镜像未被扫描)。 |
| `image_scan_block_severities` | `["CRITICAL"]` | 会阻断部署的严重级别。 |
| `image_scan_timeout_s` | `300` | 等待扫描的时长;超时会被记录,不会当作"干净"。 |

部署引用的是不可变的镜像 digest,而不是 `{agent}-v{version}` 标签,并且 digest 会记录
在该次部署上。镜像标签刻意保持**可变**:打包发生在版本号递增之前,因此重新发布会把
同一个标签推送两次,不可变标签策略会让第二次推送失败。

> **两套环境都要应用。** scan-on-push 是 CDK 改动,所以 `make bootstrap` 需要在
> `us-west-2` **和** `us-east-1` 主机上各跑一次。在此之前,那台主机上的闸门会报告
> 无法读取扫描结果。

> **默认阈值第一天就会拦住部署。** 对现役 demo 镜像实测扫描得到 **4 个 CRITICAL**,
> 全部是 Debian 基础镜像里未修补的 OS 包(`glibc`、`perl`),而不是本项目安装的任何东西
> —— 也就是说在 `["CRITICAL"]` 默认值下,一旦开启扫描,容器部署会一直被拦到基础镜像
> 发布修复为止。拦截消息会点名 CVE 与包名,便于判断责任方。请在三者之间明确取舍:换用
> 更新的基础镜像重建、把 `image_scan_block_severities` 放宽为 `[]`(仅报告:每次部署都
> 记录 finding,但不拦截)、或接受被拦截。

未实现:SBOM 生成、构建 provenance/attestation、镜像签名、受信镜像源强制,以及 skill
**内容**审查。固定版本让来源不可变,并不等于可信。

### 本地代码执行

Studio 本地调试端点(`/api/execute`、`/api/execute/stream`,以及
`/api/conversations` 多轮对话面)会在**服务器上运行调用方提供的 Python**。因此它们
在**生产模式下默认禁用**,Studio 本地调试与 AI Fix 在生产环境不可用。设置
`LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true` 表示接受该风险。

开发模式下子进程会拿到一份清洗过的环境(白名单,因此账本 URL、`LAUNCHPAD_*` 配置
以及你 shell 里的密钥都不会进去),外加内存 / CPU / 进程数 / 文件大小上限。

但它默认仍以后端用户身份运行,并且**仍能拿到你的 AWS 凭证**。把
`studio_exec_forward_aws_credentials` 设为 `false` 会让凭证不进入子进程环境,并设置
`AWS_EC2_METADATA_DISABLED=true` —— 这足以让 AWS SDK 与 CLI 不再取用实例角色,但该
变量只是 SDK 约定,不是边界。在 EC2 上凭证走网络,所以**自己去访问
`169.254.169.254` 的代码依然能拿到**。在 EC2 上实测:环境已清洗的情况下,约 20 行
`urllib` 仍取回了有效的实例角色密钥。要封住这一点:

```bash
sudo scripts/setup_exec_env.sh --hardened   # 仅 Linux
```

该命令会创建一个专用的非特权账户,并加一条**按该 uid 限定**的防火墙规则禁止它访问
元数据端点,然后打印需要补上的两个配置项。同一段探测代码在该账户下会超时失败。

**前置条件:**把子进程切换到另一个账户需要特权,因此 `studio_exec_user` 只在**后端
自身以 root 运行**时才生效。`make dev` 和 `start.py` 都以你自己的账户运行后端,此时
降权会失败 —— 所以执行端点会直接返回 `studio.exec.user_unavailable`(503)而不是执行
到一半才报错;你要么让后端以 root 运行,要么把 `studio_exec_user` 留空(即 tier 1:
只有资源上限与环境清洗)。

注意脚本同时说明的权衡:Bedrock Mantle 路径依赖 ambient 凭证来签发 bearer
token,因此一个无凭证的子进程要求每次本地调试请求显式带上
`bedrock_api_key` / `openai_api_key`。

#### Docker 沙箱后端

另一种方式是让生成代码在**一次性 Docker 容器**里运行,而不是宿主子进程:

```bash
scripts/setup_exec_docker.sh          # 构建 launchpad-studio-exec:latest
export LAUNCHPAD_STUDIO_EXEC_BACKEND=docker   # 或在 launchpad.yaml 里写 studio_exec_backend: docker
```

每次运行都是一个全新容器:`--cap-drop ALL`、`no-new-privileges`、只读根文件系统
(tmpfs `/tmp`)、同一套环境白名单,内存 / CPU / 进程数 / 文件大小上限映射为 docker
参数。超时会击杀容器本身,后端启动时还有一个清扫器回收崩溃遗留的
`strands-exec-*` 容器。单次运行开销约 0.3 秒。

这些上限以及容器所用的镜像都是配置项：

| 配置项 | 默认值 | 对应 docker 参数 |
|---|---|---|
| `studio_exec_memory_mb` | `2048` | `--memory` 与 `--memory-swap`，两者取同一个值，使容器无法靠 swap 绕过限制 |
| `studio_exec_cpu_seconds` | `300` | `--ulimit cpu=<n>:<n>`——限制的是消耗的 CPU **秒数**，不是墙上时间；墙上时间由 `execute_timeout_s` 约束 |
| `studio_exec_max_processes` | `64` | `--pids-limit` |
| `studio_exec_max_file_mb` | `256` | `--ulimit fsize=<字节数>`——生成代码可写出的最大单文件 |
| `studio_exec_docker_image` | `launchpad-studio-exec:latest` | `docker run` 启动的镜像；只有当你用别的名字构建沙箱镜像时才需要改 |

同样这四项上限也作用于**子进程**后端，只不过在那里是 fork 后在子进程里下调的
`resource.RLIMIT_*`，而不是 docker 参数（`backend/app/services/local_exec.py`：
`_docker_run_argv` 从第 380 行开始拼装 docker 参数，rlimit 列表在第 557 行附近构造）。
有一处不对称：`studio_exec_max_processes` 映射到 `RLIMIT_NPROC`，而它是**按 uid**
统计进程与线程的，因此子进程后端只在配置了 `studio_exec_user` 时才施加该上限——落在
后端自己的 uid 上，它会把你整个登录会话都算进去，导致子进程每次创建线程都失败。

由于代码不再运行在控制面主机上,**选择 docker 后端本身就是生产环境的 opt-in**:
`run_mode=prod` + `studio_exec_backend=docker` 即可提供本地调试端点,不再需要
`LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true`(显式设为 `false` 仍然是总开关)。
前提:后端用户能访问 docker daemon(`docker` 组),且已用上面的脚本构建镜像。

没有 Docker？所有容器调用（本后端、`setup_exec_docker.sh`、`local_container_smoke.sh`、
`e2e_docker_exec.py`）在 PATH 上有 `docker` 时用它，否则用 `finch`；设置
`LAUNCHPAD_CONTAINER_CLI`（或 launchpad.yaml 里的 `container_cli:`）可固定其一。finch 的
虚拟机只共享 `$HOME`，所以代码检出要放在其下（或把它的 `data/` 加进
`~/.finch/finch.yaml` 的 `additional_directories`）；`--harden-net` 仍仅支持 docker。

有一道边界**不是**白来的:EC2 上 IMDS hop limit ≥ 2 时(这些机器的默认值),默认
bridge 网络里的容器仍能访问实例元数据服务 —— 这也正是依赖 ambient 凭证的 Bedrock 路径无需 API key
就能工作的原因。要得到真正无凭证的沙箱:

```bash
sudo scripts/setup_exec_docker.sh --harden-net   # 仅 Linux
```

它会创建专用的 `launchpad-exec` bridge 网络,并加一条 `DOCKER-USER` iptables 规则
禁止该网段访问 `169.254.169.254`,然后打印需要补上的配置项
(`studio_exec_docker_network`,以及 `studio_exec_forward_aws_credentials: false`,
Mantle 权衡同上)。如果配置了无凭证姿态却没有配置该网络,后端会直接拒绝,而不是
假装隔离。

更深一层的沙箱(迁移到 AgentCore Code Interpreter)尚未实现;docker 后端是推荐的
中间档。

#### 代码生成（AI Fix）

Studio 的 **AI Fix** 会把执行失败的流程的生成代码、traceback 与校验错误交给一个编码
agent，由它在临时工作目录里重写 `generated_agent.py`，再通过 SSE 回传。控制它的有四个
配置项：

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `codegen_backend` | `claude` | 由哪个编码 agent 执行修复。目前只注册了 `claude`（Claude Agent SDK）；填入未注册的名字会让请求直接失败并列出已注册的名字，而不是静默回退。 |
| `codegen_model` | `global.anthropic.claude-sonnet-5` | 该编码 agent 使用的模型。更强的模型单次修得更多，单次也更贵。 |
| `codegen_timeout_s` | `180.0` | 一次 AI Fix 请求的端到端预算，含全部修复轮次。如果修复总在 agent 写完文件之前被打断，就调大它。 |
| `codegen_max_repair_rounds` | `2` | 一次重写最多可以被重新校验并重试几轮。每多一轮就是对首次修复仍无法导入的流程多发一次模型调用。 |

### Skill Lab

Skill Lab 负责评估与训练 Registry 里的技能记录：vendored 的 SkillOpt CLI 以**子进程**
形式跑在后端主机上，每个任务的 agent rollout 跑在 `launchpad_skill_lab_worker` 上各自
独立的 AgentCore Runtime microVM 会话里，LLM 判分器则直接调用 Bedrock。
`make bootstrap` 会把两半都开通好——专用解释器与 worker 镜像——所以下面这些配置项调的是
一个已开通的 Skill Lab，而不是开关。

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `skill_lab_python` | `<repo>/data/skill-lab-venv/bin/python` | 运行 vendored 的 `evaluate_skill.py` / `train.py` / 任务集校验器的解释器。bootstrap 依据 `vendor/skillopt/requirements-launchpad.txt` 构建它，并在该文件变化时重建；后端进程自身永不 import vendored 目录树。该路径缺失时 `GET /api/skill-lab/status` 会返回 `venv_ready: false`——这就是未开通的 workspace 的样子。 |
| `skill_lab_max_concurrent_jobs` | `1`（1–4） | 同时运行的评估/训练任务数。每个任务是一个 CLI 子进程外加它自己的 worker 会话，因此这是在墙上时间与主机 CPU/内存、worker runtime 压力之间取舍；超出的任务排队而不是失败。 |
| `skill_lab_judge_model_id` | `us.openai.gpt-6-sol` | 给 rollout 打分的模型。必须是 Bedrock 的 **Converse inference-profile id**——`bedrock_chat` 判分器会拒绝裸 model id。它同时按模型家族决定 agentic 判分器使用的宿主 CLI：`openai.*` 的 id 会原样路由到宿主的 `codex`（需 codex >= 0.155，且其模型目录要有该 id 的条目），其余路由到宿主的 `claude`（`runner.judge_exec_route`）。因此改这一项也就改变了宿主必须安装哪个 CLI。 |
| `skill_lab_target_model_id` | `global.anthropic.claude-opus-5-5` | `claude_code_exec` 目标后端下被测技能默认运行的模型，同样是 Converse inference-profile id。逐任务参数可以覆盖它；空值永远不会被下发，因为空的 `--model` 会让 vendored CLI 换上它自己的非 Bedrock 默认值。 |
| `skill_lab_codex_target_model_id` | `global.openai.gpt-6-sol` | `codex_exec` 目标后端下的同一个默认值。codex 自行解析模型：配置里内置的 `amazon-bedrock-runtime` provider 直接调用这个 **inference-profile id**（裸 `openai.*` id 会因不支持按需吞吐而被拒绝），并从宿主的模型目录（`skill_lab_codex_catalog_path`）读取它的元数据，因此目录里必须有与该 id 完全一致的条目。单独设一个 key，是为了让每个后端都拿到自己 CLI 能接受的 id。 |
| `skill_lab_codex_catalog_path` | `~/.codex/model-catalogs/bedrock-models.json` | 构建时从后端主机读取、并塞进 worker 镜像 codex-home 的 Bedrock 模型目录。该文件内嵌了专有的模型指令，因此从不入库；缺失时构建会塞一个空的 `{}` 目录并在日志里写明——那个镜像上的 codex 目标也就没有目录可供解析。 |
| `skill_lab_judge_sandbox` | `bwrap` | agentic 判分器的产物解析器所用的沙箱启动 argv（按 shlex 切分），它跑在后端**主机**上——worker microVM 无法运行 bubblewrap。若主机上非特权 `bwrap` 被 AppArmor 拦住，改成 `sudo -n bwrap`。`GET /api/skill-lab/status` 会探测该 argv 的第一个词来给出 `agentic_judge_ready`，vendored 那层 fail-closed 的边界校验仍然叠加生效。 |
| `skill_lab_worker_cli_version` | `2.1.283` | 报告 worker 镜像里内置的 `claude` CLI 版本。 |
| `skill_lab_worker_codex_version` | `0.155.1` | 报告 worker 镜像里内置的 `codex` CLI 版本。 |

最后两个 `*_version` 是**镜像值，不是输入值**。真正生效的是
`vendor/skillopt/deploy/agentcore/Dockerfile` 里 `ARG CLAUDE_CLI_VERSION` /
`ARG CODEX_CLI_VERSION` 的默认值——被镜像内容哈希覆盖的是那个 Dockerfile——这两个配置项
只用于控制台展示。**必须成对修改：**`backend/tests/test_skill_lab_foundation.py` 会断言
两边一致，只改一边会让 `make verify` 失败。版本号变化同时会改变构建上下文的哈希，下一次
bootstrap 正是据此判断要重建并重新推送 worker 镜像。

### 提示词优化

实验的 RECOMMEND 阶段会让第三方 provider（`backend/app/optimization/providers`）依据某次
被固定的评估运行中得分最差与最好的会话，改写 agent 的系统提示词与工具描述。这里的 model
id 都是 Bedrock Converse 的 inference-profile id，并和其他调用一样走同一个 workspace
客户端漏斗。

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `prompt_opt_models` | `["global.anthropic.claude-opus-5", "global.anthropic.claude-sonnet-5", "global.anthropic.claude-sonnet-4-6", "us.openai.gpt-5.6-sol"]` | 控制台提供选择的反思模型清单。想让某个 id 可选，就加到这里。 |
| `prompt_opt_default_model_id` | `global.anthropic.claude-opus-5` | 其中哪一个排在最前、并在请求未指定模型时被使用。 |
| `prompt_opt_max_sessions` | `30`（3–100） | provider 从被固定的那次运行里读取多少个会话（最差优先，另加一组得分最好的对照）。读得少更便宜更快，读得多则给反思更多可归纳的证据。 |
| `prompt_opt_max_tokens` | `8192`（512–16000） | 反思调用的输出预算。含提示词与若干工具描述的双组件反思实测超过了 4096；响应被截断时 provider 会自行把该值翻倍一次。 |
| `prompt_opt_read_timeout_s` | `900` | 反思调用的读超时。该调用是流式的，因此它约束的是两个数据块之间的间隔而不是总时长。botocore 默认的 60 秒在这里远远不够：生产环境里大模型处理 30 个会话时超过了它，botocore 静默重发了整个请求五次才最终失败。 |

### 选择模型

平台自身调用或预选的每一个模型都是一个**模型角色**：一个后端配置项
（`backend/app/services/model_settings.py` 列出了全部角色及其约束）。已经存在的
Agent、评估器和预设保留创建时的模型 ID——角色只决定*新建*的对象用什么。

| 角色（配置项） | 默认值 | 用途 |
|---|---|---|
| `agent_default_model_id` | `global.openai.gpt-6-sol` | 向导里原生 Bedrock 下新建 Agent 的预选模型；通过 API 创建且未指定 `model_id` 的 Agent 也使用它。 |
| `claude_sdk_model_id` | `global.anthropic.claude-sonnet-5` | Claude Agent SDK（`container`）方式的同一默认值。该方式只能驱动 Claude：未指定 `model_id` 的容器 spec 使用它，而不是 Agent 默认模型。仅限 Claude。 |
| `judge_model_id` | `global.openai.gpt-6-sol` | 自定义 / 派生 LLM 评估器的默认评审模型，架构助手起草的评估器也使用它。 |
| `judge_fallback_model_id` | `global.anthropic.claude-sonnet-5-5` | AgentCore Evaluation 拒绝评审模型时改用它重试。留空 = 不回退，直接报出拒绝原因。 |
| `assistant_model_id` | `global.openai.gpt-6-sol` | 架构助手为新 Agent 推荐的模型。 |
| `architect_model_id` | `global.openai.gpt-6-astra` | 管理员安装或修复系统架构师预设时使用的模型。 |
| `actor_model_id` | `global.openai.gpt-6-luna` | 模拟人格评估运行中扮演用户的模型，必须支持 Converse 强制工具调用。 |
| `studio_fallback_model_id` | `global.anthropic.claude-sonnet-5` | Studio 画布中未指定模型的节点所生成的模型。后端按同一个值为该 Agent 的执行角色授权，画布也从服务端读取它，因此两边始终一致。它刻意与 Agent 默认模型分开：改它会改动所有已有流程中未指定模型的节点。 |
| `codegen_model` | `global.anthropic.claude-sonnet-5` | Studio AI 修复所用的编码 Agent。仅限 Claude。 |
| `prompt_opt_default_model_id` | `global.anthropic.claude-opus-5` | 见[提示词优化](#提示词优化)。 |
| `skill_lab_judge_model_id`、`skill_lab_target_model_id`、`skill_lab_codex_target_model_id` | 见 [Skill Lab](#skill-lab) | 评审模型为 OpenAI 或 Claude；Claude CLI 目标仅限 Claude；codex 目标仅限 OpenAI。 |

会运行工具的角色拒绝实测不支持 Converse 工具调用的模型（DeepSeek R1）。每个 ID 都必须是
Bedrock ID（`[A-Za-z0-9._:/-]`，3–121 个字符）。

**优先级（从高到低）：**控制台覆盖 > `LAUNCHPAD_<角色>` 环境变量（例如
`LAUNCHPAD_JUDGE_MODEL_ID`）> `config/launchpad.yaml` 中的同名键 > 上表的内置默认值。

**模型页面**（`/v2/models`，仅管理员可见，位于“配置”分组下）逐个列出角色：生效模型、
默认值（环境变量 / yaml / 内置值解析出的结果）、“已覆盖”标记，以及修改人和修改时间。
管理员可以对每个角色：

- 选择模型或输入自定义 ID 并**保存**——即控制台覆盖，立即生效、无需重启，保存在账本表
  `platform_settings` 中；
- **恢复默认**，即删除该覆盖；
- **探测**所选 ID：在中心的默认工作区发起一次小型真实 Converse 调用（会运行工具的角色另加一次
  工具调用），返回正常及延迟，或 AWS 错误。探测不会在账号中创建任何资源。

控制台的选择器（V2 Agent 向导、评估器编辑器、评估任务向导的 actor、助手方案编辑器、
Studio 代码生成的回退模型）从 `GET /api/model-defaults` 取预选值，因此修改无需重新构建前端；
读取失败时回退到内置默认值。

修改角色前需要了解：

- **覆盖对整个部署生效。**它立即作用于所有工作区和成员。每个部署有自己的账本，因此共享同一
  AWS 账号的两个部署（例如 prod 与其克隆）各自保存覆盖设置。
- **更换评审模型后，评分与之前的评估运行不再可比**：不同的评审模型打分不同。对比期间请保持
  原评审模型不变。
- 探测在中心的默认工作区运行；在那里可用的模型，在分支工作区的账号或区域中仍可能不可用。

### 自助注册与用户管理

登录网关开启后,登录页同时提供**注册**:填写用户名、**公司邮箱**和密码提交申请。
默认情况下新账户处于 **`pending`(待审批)**,必须由管理员审批通过后才能登录,
**7 天有效期从审批时开始计算**。管理员也可以在**用户管理 → 新增用户**直接创建账号
(立即生效,任意角色,包括管理员)。上面配置的内置 admin 没有 `users` 行;如果在控制台
修改了它的密码(顶栏钥匙图标「修改密码」),配置的 `auth_password` 将失效,直到用
`cd backend && uv run python scripts/reset_admin_password.py` 清除覆盖值(应急恢复)。

公共/临时邮箱域名(Gmail、QQ、163、Outlook、mailinator 等)会被拒绝。相关配置:

```bash
export LAUNCHPAD_AUTH_REGISTRATION_ENABLED=true          # false 关闭注册
export LAUNCHPAD_AUTH_REGISTRATION_REQUIRE_APPROVAL=true # false 则注册即生效
export LAUNCHPAD_AUTH_REGISTRATION_VALID_DAYS=7          # 审批通过后授予的有效期
# 白名单非空时优先生效,否则使用内置黑名单
export LAUNCHPAD_AUTH_ALLOWED_EMAIL_DOMAINS='["your-company.com"]'
export LAUNCHPAD_AUTH_BLOCKED_EMAIL_DOMAINS='["gmail.com","qq.com"]'
```

admin 账号会看到**用户管理**模块(`/users`):审批队列(「待审批」统计卡片 +
`PENDING` 筛选 + 每行的**通过**/**拒绝**)、注册统计,以及逐账户操作(延期 +7/+30/
自定义天数或指定到期时间、禁用/启用、修改角色、重置密码(仅显示一次)、删除)。
到期与禁用在每次请求时校验,账户会**立即**失去控制台访问权限,无需等待会话
Cookie 过期。

## Agent-DLC：打开放行门

设计见 [agent-dlc-design.zh-CN.md](agent-dlc-design.zh-CN.md)，这里是操作顺序。以下步骤**不会自动发生**：
在管理员打开之前，工作区保持原有行为（`release_mode: direct`，部署直接进生产）。

1. **把工作区切换为门控**。「判据 → 放行门 → *放行策略*」（管理员），或
   `PUT /api/release-policies/{workspace_id}` 传 `{"release_mode": "gated"}`。同一张卡片还设置裁判校准的
   有效期（`calibration.period_days`、`kappa_floor`）与评估成本闸门（`eval_cost_confirm_usd`、
   `eval_cost_max_usd`）——闸门是**拒绝**超限的运行，不只是提醒。
2. **把每个智能体切换到具名端点**。「判据 → 放行门 → *切换到 live / candidate*」（需要 `release.sign`）。
   在那之前智能体只有一个端点，AWS 每次更新都会自动滚过去，放行门无从拦起。A2A 运行时与系统预置
   无法门控，界面会说明原因。
3. **把三项“标准所有权”权限授予具体的人，而不是角色**。「用户」→ 选中成员 → `criteria.sign`（签署标准）、
   `golden.admit`（准入样本）、`judge.calibrate`（判定裁判一致）。它们刻意不在任何角色的默认权限里：
   这三项决定“什么叫好”。`criteria.manage`、`waiver.approve` 与 `release.sign` 沿用成员/操作员的常规划分。
4. **编写并签署判据表**。「判据 → 判据表」。至少一条红线、每个维度有判据或标注 `n/a:<dimension>`、
   成本与性能用指标、红线不交给大模型裁判——其余情况编辑器会直接拒绝。发布即冻结该版本，再由
   **作者以外的人**签署。未签署的标准会让每份放行报告都是 INVALID，这正是设计意图。
5. **一次性编制黄金集**。「判据 → 黄金集 → *初次编制切分*」（`golden.admit`）。这是唯一写入保留集的路径，
   写完即封存。样本按用例档位分层，使三个切分见到同样的正例 / 反例 / 模糊 / 对抗构成。
6. **第一次跑放行门之前，先在每个端点上调用一次**。每个 AgentCore 端点写自己的日志组，所以全新的
   `candidate` 在有流量之前没有任何遥测，放行门会一直等不存在的记录而超时。在候选端点上聊一轮就够了。
7. **之后循环自己会转**。一次部署落到 `candidate` 并开出放行记录；「判据 → 放行门」对它跑回归集**和**
   保留集，给出带置信区间的判定，再由第二个人签署——只有这时 `live` 才会移动。回滚即把 `live` 指回去，
   不删除任何资源。

**裁判只有校准后才拦人。** 裁判类判据在标注任务记录 `aligned` 之前，显示与处理都是 `observe`。
「判据 → 裁判校准」可以从一次已完成的运行创建标注任务（裁判的判定对标注人隐藏）；在 dev / staging
工作区还可以给外部专家一个无账号链接（`/r/annotate/<token>`）。**prod** 工作区拒绝发放这种链接——
那里的标注会接触真实客户会话，请把这个人邀请为成员。

**端到端验证（真实 AWS，自带清理）：**

```bash
cd backend
# 三个身份走完整循环：部署 → 切换端点 → 由他人签署判据 → 封存黄金集 → 放行门 → 签署 → 回滚 → 校准
LAUNCHPAD_E2E_USERNAME=<admin> LAUNCHPAD_E2E_PASSWORD=<pw> \
  uv run python scripts/e2e_agent_dlc.py --base http://localhost:8000
# 双语走查九个控制台子页，遇到控制台报错或 5xx 即失败
uv run python scripts/e2e_agent_dlc_browser.py --ui http://localhost:5173 --lang zh-CN
```

第一个脚本会拒绝在 `prod` 级工作区运行，并把它改过的放行策略还原。两者都不在 `make verify` 之内
（需要真实 AWS 凭证和已引导的工作区）。

## 切换模型

除了你构建的 Agent 自己的模型，平台本身也调用模型：给意图起名、生成建议问题、评估打分、扮演模拟用户、提议 Agent、修复 Studio 代码。内置默认值假定账号能在 Bedrock 上调用 OpenAI 与 Anthropic 模型。有的账号不能；这些模型每一个都是可以切换的配置项，既可以在控制台改，也可以在主机上改。

| 环境变量（`launchpad.yaml` 的键 = 去掉 `LAUNCHPAD_` 后的小写名） | 默认值 | 用在哪里 |
|---|---|---|
| `LAUNCHPAD_AGENT_DEFAULT_MODEL_ID` | `global.zai.glm-5.3` | 新建 Agent 的默认模型：新建智能体向导预选它，提交时没有 `model_id` 的 spec 也用它。 |
| `LAUNCHPAD_PLATFORM_MODEL_ID` | `global.anthropic.claude-sonnet-5` | 意图归类与命名（可观测 → 意图视图）以及试聊面板的建议问题。调用失败时，意图视图退回机械归类，面板退回通用问题。 |
| `LAUNCHPAD_JUDGE_MODEL_ID` | `global.openai.gpt-6-sol` | 自定义与派生 AgentCore 评估器的默认 LLM 评审模型（新建评估器时评审模型字段的默认值）。 |
| `LAUNCHPAD_JUDGE_FALLBACK_MODEL_ID` | `global.anthropic.claude-sonnet-5-5` | AgentCore 的评估器校验拒绝所选评审模型时改用的模型。`""` 表示不回退，直接报出拒绝。 |
| `LAUNCHPAD_ASSISTANT_MODEL_ID` | `global.openai.gpt-6-sol` | 架构助手提议的 Agent（托管 Harness）的默认模型。 |
| `LAUNCHPAD_ARCHITECT_MODEL_ID` | `global.openai.gpt-6-astra` | 安装系统架构师 Agent 时使用的模型。只有 OpenAI 模型才预设 `reasoning_effort`，因为只有这一家接受它。 |
| `LAUNCHPAD_ACTOR_MODEL_ID` | `global.openai.gpt-6-luna` | 人设场景里控制台预选的模拟用户（actor）模型。它必须支持 Converse 的强制工具选择。 |
| `LAUNCHPAD_STUDIO_FALLBACK_MODEL_ID` | `global.anthropic.claude-sonnet-5` | Strands Studio 画布上没有指定模型的节点所用的模型。 |
| `LAUNCHPAD_PROMPT_OPT_DEFAULT_MODEL_ID` | `global.anthropic.claude-opus-5` | 实验 RECOMMEND 阶段在请求未指定模型时使用的反思模型（见[提示词优化](#提示词优化)）。 |
| `LAUNCHPAD_PROMPT_OPT_MODELS` | [提示词优化](#提示词优化)里的四个 id | 控制台提供的反思模型清单，JSON 数组（`'["global.zai.glm-5.3","deepseek.v3.2"]'`）。只能用环境变量或 `launchpad.yaml` 设置，模型设置页不编辑这份清单。 |
| `LAUNCHPAD_CLAUDE_SDK_MODEL_ID` | `global.anthropic.claude-sonnet-5` | Claude Agent SDK（容器）方式的默认模型。**仅限 Claude。** |
| `LAUNCHPAD_CODEGEN_MODEL` | `global.anthropic.claude-sonnet-5` | Studio 的 AI Fix，运行在 Claude Agent SDK 上（见[代码生成（AI Fix）](#代码生成ai-fix)）。**仅限 Claude。** |
| `LAUNCHPAD_SKILL_LAB_JUDGE_MODEL_ID` | `us.openai.gpt-6-sol` | Skill Lab 的评审。OpenAI id 走主机上的 `codex` CLI，Claude id 走主机上的 `claude` CLI。**仅限 OpenAI 或 Claude。** |
| `LAUNCHPAD_SKILL_LAB_TARGET_MODEL_ID` | `global.anthropic.claude-opus-5-5` | Skill Lab 的 `claude_code_exec` 目标。**仅限 Claude。** |
| `LAUNCHPAD_SKILL_LAB_CODEX_TARGET_MODEL_ID` | `global.openai.gpt-6-sol` | Skill Lab 的 `codex_exec` 目标。**仅限 OpenAI。** |

这些配置项是“接下来新建的东西”的默认值。已有 Agent 保留各自的 `model_id`，已安装的架构师保留它的模型，已有评估器保留创建时的评审模型（下面的脚本可以把它们迁过去）。

### 模型设置页

管理员在控制台里切换：**admin → 配置 → 模型设置**（V3；英文界面为 Configuration → Models）。每个配置项一行，显示当前模型和它的来源（`default` 默认值、`config` 即 `launchpad.yaml`、`env` 环境变量、`console` 控制台）。

1. 在下拉框里选一个模型。下拉框列出账号所在 Region 提供的文本模型（推理配置与按需基础模型）。“提供”不等于“有权调用”，所以：
2. 点 **测试**，用这个模型做一次真实的、很小的 Converse 调用。两个评审模型行还会跑 AgentCore Evaluations 的接受探测：用这个评审模型建一个临时的 `zz_probe_…` 评估器，再把它删掉。模拟用户（actor）行会检查强制工具选择；其余驱动工具调用的行（新建 Agent 默认、架构助手、架构师、Studio 兜底）会多做一项 `tool_use` 检查。
3. 点 **保存**，选择写入账本（`platform_settings` 表）并立即作用于正在运行的后端，无需重启。
4. 点 **恢复默认**，删除控制台上的选择，这一行退回环境变量、`launchpad.yaml` 或默认值。

仅限 Claude、仅限 OpenAI 的行会拒绝其他家族的模型（`models.wrong_family`）。页面背后的接口：`GET /api/models/defaults`（任何成员：控制台预选用的生效值）、`GET /api/models/settings`、`PUT` / `DELETE /api/models/settings/{field}`（管理员）、`GET /api/model-catalog`（成员）、`POST /api/model-probe`（管理员）。

### 脚本：scripts/switch_models.py

控制台还连不上的主机，或想一次设好全部配置项时，用 `scripts/switch_models.py`。它探测账号（同样是真实的 Converse 调用；评审模型另做一次 AgentCore `CreateEvaluator` 测试；actor 检查强制工具选择），每个配置项取第一个能用的候选（依次是当前值、默认值、`--prefer` 指定的 id、内置的备选），把 `LAUNCHPAD_*` 行写进 `/etc/launchpad-models.env`（权限 0600）。仅限 Claude 或仅限 OpenAI 的配置项只尝试该家族的候选；都不能用时报告为 `UNAVAILABLE`，不写进文件。

```bash
cd <repo>/backend
# 只探测并打印，什么也不改
uv run python ../scripts/switch_models.py --region us-east-1 --dry-run
# 写入 /etc/launchpad-models.env 并重启后端（写 /etc 需要 root）
sudo "$(command -v uv)" run --no-sync python ../scripts/switch_models.py \
  --region us-east-1 --prefer global.zai.glm-5.3 --restart
```

- `--prefer a,b`：在每个配置项的默认值之后紧接着尝试这些 id（家族符合时）。`--roles judge,actor` 只处理部分配置项；`--json` 额外输出机器可读的结果。
- `--restart`：写完后执行 `systemctl restart launchpad-backend`。不加它就要自己重启后端：环境变量文件只在启动时读取。
- 脚本会列出控制台创建的、评审模型已经不能用的评估器；加 `--update-evaluators` 会把它们改为选出的评审模型。
- 在模型设置页保存过的配置项优先于这个文件；脚本会报告它们，但从不修改。要让文件里的值生效，先在页面上点 **恢复默认**。
- `--env-file <路径>` 写到别处，例如在笔记本上用目标账号的凭证运行时。
- `--no-sync` 让 root 运行时不去重新同步服务用户拥有的后端 venv。退出码 0 表示每个可切换的配置项都选好了，2 表示有配置项（或评估器更新）找不到能用的模型，1 表示用法、凭证或写文件出错。

2026-10-12 在 us-east-1 实测：AgentCore Evaluations 接受 `global.zai.glm-5.3` 作为评审模型（GPT-6 Sol、Claude Sonnet 5.5、Kimi K3、DeepSeek R1 也接受）。模拟 OpenAI 与 Anthropic 都不可用时，脚本给平台、评审、架构助手、架构师、actor、Studio 兜底和提示词优化默认值都选了 `global.zai.glm-5.3`，评审回退选了 `deepseek.v3.2`，反思清单为 `global.zai.glm-5.3`、`deepseek.v3.2`、`global.moonshotai.kimi-k3`、`moonshotai.kimi-k2.5`，并把 Claude Agent SDK、codegen 和三个 Skill Lab 配置项报告为 `UNAVAILABLE`。

### 实测过的替代模型：DeepSeek 与 Kimi

除了 GLM-5.3，Bedrock 上的 DeepSeek 与 Kimi 模型也可以作为平台配置项的候选。实测探测结果，账号 784675006790，2026-10-12（探测用的评估器事后已全部删除）：

| 模型 | us-east-1 Converse（延迟） | us-west-2 Converse（延迟） | 工具调用（auto / 强制 any / 流式） | 推理块 | AgentCore 评审 | 可用于的配置项 |
|---|---|---|---|---|---|---|
| `us.deepseek.r1-v1:0` | 可用 2.1 秒 | 可用 1.7 秒 | 拒绝（“This model doesn't support tool use”） | 有 | 接受 | 平台、评审、评审回退、提示词优化 |
| `deepseek.v3.2` | 可用 0.6 秒 | 可用 0.7 秒 | 可用 / 可用 / 可用 | 无 | 接受 | 所有不限家族的配置项 |
| `deepseek.v3-v1:0`（DeepSeek V3.1） | 未提供 | 可用 0.6 秒 | 可用 / 可用 / 可用 | 无 | 接受（us-west-2） | 全部（仅 us-west-2） |
| `global.moonshotai.kimi-k3` | 可用 2.0 秒 | 可用 2.3 秒 | 可用 / 可用 / 可用 | 有 | 接受 | 全部 |
| `us.moonshotai.kimi-k3` | 可用 2.8 秒 | 可用 2.1 秒 | 可用 / 可用 / 可用 | 有 | 接受 | 全部 |
| `moonshotai.kimi-k2.5` | 可用 0.6 秒 | 可用 0.8 秒 | 可用 / 可用 / 可用 | 无 | 接受 | 全部 |
| `moonshot.kimi-k2-thinking` | 可用 1.2 秒 | 可用 0.9 秒 | 可用 / 可用 / 可用 | 有 | 接受 | 全部 |
| `global.zai.glm-5.3` | 可用 0.8 秒 | 可用 0.8 秒 | 可用 / 可用 / 可用 | 有 | 接受 | 全部 |

“全部”指所有不限定 Claude 或 OpenAI 的配置项。推理块没有影响：平台只读取响应里的文本块，推理内容会被忽略。

**不支持工具调用的模型不能用于需要工具的配置项。** 新建 Agent 默认、架构助手、架构师、actor 和 Studio 兜底这几个配置项驱动的是会调用工具的 Agent，后端会以 `models.no_tool_use` 拒绝不支持工具调用的模型（例如 DeepSeek R1）。在模型设置页上，这几行的 **测试** 会多做一项 `tool_use` 检查（actor 那一行改为检查强制工具选择）。

**价格**（每百万输入 / 输出 token，Standard 按需，美国东部（弗吉尼亚北部），2026-10-12，已写入后端价格表 `model_prices`）：取自 [Bedrock 价格页](https://aws.amazon.com/bedrock/pricing/)，并与 AWS Price List API 交叉核对：DeepSeek V3.2 $0.62 / $1.85，Kimi K2.5 $0.60 / $3.00，Kimi K2 Thinking $0.60 / $2.50。价格页上没有、取自 Price List API 的：DeepSeek V3.1 $0.58 / $1.68（us-west-2），DeepSeek R1 $1.35 / $5.40，Kimi K3 global $3.00 / $15.00（缓存读 $0.30、缓存写 $3.75），Kimi K3 US 推理配置 $3.30 / $16.50（$0.33 / $4.125）。GPT-6 Sol 两处都没有，按其[模型卡片](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-sol.html)定价（短上下文 ≤ 272K）：global 推理配置 $2.00 / $10.00（缓存读 $0.20、缓存写 $2.50），US 推理配置与 Mantle 区域内 $2.20 / $11.00（$0.22 / $2.75），因此默认评审不再显示为未定价。

### 手动设置

把配置行写进 `/etc/launchpad-models.env`（root 所有，权限 0600），并确认后端单元加载这个文件。开头的 `-` 让文件不存在时单元照常启动：

```bash
sudo install -m 0600 /dev/null /etc/launchpad-models.env   # 仅在文件还不存在时
sudo tee -a /etc/launchpad-models.env >/dev/null <<'ENV'
LAUNCHPAD_JUDGE_MODEL_ID=global.zai.glm-5.3
LAUNCHPAD_PLATFORM_MODEL_ID=global.zai.glm-5.3
ENV
systemctl cat launchpad-backend | grep -q launchpad-models.env || {
  sudo mkdir -p /etc/systemd/system/launchpad-backend.service.d
  printf '[Service]\nEnvironmentFile=-/etc/launchpad-models.env\n' \
    | sudo tee /etc/systemd/system/launchpad-backend.service.d/models.conf
  sudo systemctl daemon-reload
}
sudo systemctl restart launchpad-backend
```

也可以把同样的键（小写）写进 `config/launchpad.yaml`（`judge_model_id: global.zai.glm-5.3`，`prompt_opt_models:` 写成 YAML 列表）后重启。`make bootstrap` 对这个文件做合并，重新引导后这些键仍然保留。

**优先级：** 模型设置页（控制台）> 环境变量（含这个环境变量文件）> `config/launchpad.yaml` > 内置默认值。

### 不能换成其他家族的功能

有些功能被它运行所在的 SDK 或 CLI 绑定在某一个模型家族上。没有 Anthropic 权限的账号里，无论怎么设置，下面这些都不可用：**Claude Agent SDK** 部署方式、**Studio AI Fix**（codegen）、Skill Lab 的 `claude_code_exec` 目标和走 claude CLI 的评审。没有 OpenAI 权限时，Skill Lab 的 `codex_exec` 目标和走 codex 的评审不可用。Skill Lab 评审需要这两家之一。

### 换评审模型会改变分数

LLM 评审的分数是这个模型的判断。换了评审模型之后，评审类判据上的分数、通过率和放行判定与旧评审打出的结果不可比；为旧评审记录的校准（裁判—人 κ）也不能沿用：只在同一个评审模型内比较运行，在依赖评审拦截之前重新做一次标注任务。指标与代码类判据（token 数、延迟、断言）不受影响。

## 托管部署

> 本节说明如何在一台长期运行的主机上运行本示例，用于 workshop 或共享的评估环境。它不是生产部署
> 指南：本示例未针对生产使用进行加固，基于它运行的任何内容，其安全、测试与运维均由你自行负责。
> 在面向真实用户或真实数据开放之前，请先与你所在组织的安全和法务团队确认。

`./start.py --prod` 只是本地预览:构建前端、提供构建产物、关闭后端自动重载,并绑定到
`0.0.0.0`。长期运行的主机应改用进程管理器托管这两个服务,并在前面放一层终结 TLS 的
边缘。参考部署(workshop EC2 + CloudFront)的单元文件与实测过的更新流程见
[agent-runbook-prod.md](agent-runbook-prod.md#3-shape-b--systemd-reference-the-us-east-1-box),其拓扑为:

```text
浏览器 → CloudFront(TLS、不缓存、放通全部方法、注入一个密钥请求头)
           └─ 实例上的 nginx :80 —— 缺少该请求头的请求直接拒绝
                ├─ /api/、/v1/ → 127.0.0.1:8000   (后端,SSE 需要 proxy_buffering off)
                └─ /、/assets/ → 127.0.0.1:5173    (vite preview 提供 frontend/dist)
```

**1. 托管两个进程。** 认证配置写在后端单元里,没有别的东西会替你开启网关:

```ini
# /etc/systemd/system/launchpad-backend.service   (节选)
[Service]
WorkingDirectory=/home/ubuntu/workspace/agentcore_launchpad/backend
Environment=LAUNCHPAD_RUN_MODE=prod
Environment=LAUNCHPAD_AUTH_USERNAME=admin
Environment=LAUNCHPAD_AUTH_PASSWORD=<strong-password>
Environment=LAUNCHPAD_AUTH_COOKIE_SECURE=true
# 仅在使用单点登录时：浏览器访问地址与客户端密钥的加密密钥
Environment=LAUNCHPAD_PUBLIC_BASE_URL=https://<console-host>
Environment=LAUNCHPAD_SSO_SECRET_KEY=<fernet-key>
# 可选的模型配置，见“切换模型”；“-”表示文件可以不存在
EnvironmentFile=-/etc/launchpad-models.env
ExecStart=/home/ubuntu/.local/bin/uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
```

```ini
# /etc/systemd/system/launchpad-frontend.service  (节选)
[Service]
WorkingDirectory=/home/ubuntu/workspace/agentcore_launchpad/frontend
Requires=launchpad-backend.service
ExecStart=/usr/bin/npm run preview -- --host 127.0.0.1 --port 5173 --strictPort
Restart=on-failure
```

`vite preview` 提供的是 `frontend/dist`,所以**前端每次改动都必须先
`npm run build` 再重启**。两个进程都绑定 `127.0.0.1`,对外只暴露反向代理。

**2. 封闭 origin。** CloudFront 注入一个自定义请求头(如
`X-Launchpad-Origin-Key`),nginx 拒绝不带该头的请求,这样直连实例公网 IP 无法绕过
CDN:

```nginx
if ($http_x_launchpad_origin_key != "<shared-secret>") { return 403; }
proxy_set_header X-Forwarded-Proto https;   # TLS 在 CloudFront 终结
```

由于 TLS 在边缘终结,`LAUNCHPAD_AUTH_COOKIE_SECURE=true` 必须保持开启;纯 HTTP 下
浏览器会丢弃会话 Cookie。

**3. 更新已有主机。** 下面是简要步骤；从旧版本升级（备份、迁移演练、回滚、故障排查）请按 [upgrade.zh-CN.md](upgrade.zh-CN.md) 操作。

```bash
(cd backend && uv run python scripts/ledger_backup.py)   # 在线备份并校验；服务运行时不要 cp 台账
git fetch origin && git merge --ff-only origin/main
cd backend && uv sync --frozen && cd ..
cd frontend && npm ci && npm run build && cd ..          # 必须:preview 只吃 dist/
sudo systemctl restart launchpad-backend launchpad-frontend
curl -s localhost:8000/api/auth/status          # 预期 auth_required: true
```

新增的台账表(如 `users`)会在后端启动时自动创建,无需迁移步骤。网关一开启,注册
就是开放的 —— 如果不希望任何拿到 URL 的人都能提交申请,请设置
`LAUNCHPAD_AUTH_REGISTRATION_ENABLED=false` 或用
`LAUNCHPAD_AUTH_ALLOWED_EMAIL_DOMAINS` 限定公司域名。

**4. 在线更新（可选）。** 管理员可以在 **配置 → 系统更新** 页面更新控制台，代替手工执行第 3 步。先以 root 安装一次更新器：

```bash
sudo backend/scripts/install_updater.sh      # 可用 --user / --services / --health-url 覆盖默认值
systemctl status launchpad-updater.path      # 预期 active (waiting)
```

安装脚本把 `backend/scripts/apply_update.sh` 复制到 `/usr/local/lib/launchpad/`（更新不会替换执行它的脚本），写入 `/etc/launchpad-updater.env` 和两个单元：`launchpad-updater.path` 监视 `data/updates/apply.request`，`launchpad-updater.service`（`Type=oneshot`，以 root 运行）负责应用。

页面上的流程：

1. **检查**：比较当前运行的提交和所配置来源的最新提交，只接受快进。来源是部署配置，不是请求参数：`LAUNCHPAD_UPDATE_REPO`（默认 `https://github.com/aws-samples/sample-agentcore-launchpad`）和 `LAUNCHPAD_UPDATE_BRANCH`（默认 `main`）。
2. **准备更新**：把该提交检出到 `data/updates/stage-<sha>`，并以 `nice 10` 在其中只运行固定命令：`uv sync --frozen`、`npm ci`、`npm run build`，然后做导入自检，再用独立的临时台账在临时端口做启动自检。当前服务不受影响。失败（例如访问不了 npm 源）时更新状态为 `prepare_failed`。后端重启后，准备从最后完成的步骤继续。只保留最近两个暂存目录。
3. **排期重启**（“立即”即一分钟后，或 7 天内的指定时间）会发布公告。如果重启会中断进行中的任务，页面会列出这些任务，并要求先勾选“我已知晓”。到点后控制台写入 `data/updates/apply.request`。更新器随后在线备份台账（`ledger_backup.py`），快进检出，用重命名换入构建好的 `frontend/dist`，执行 `uv sync --frozen` 并重启服务，然后最多等待 120 秒，直到 `/api/auth/status` 返回的 `version.sha` 等于新提交。健康检查失败时，更新器重置到上一个提交，恢复旧的 `dist`，再次重启，并报告 `rolled_back`。控制台读回结果（`data/updates/apply.result`）并结束公告。
4. **回滚**：准备上一次更新所替换的提交，再像更新一样排期。

未安装更新器时，页面仍可检查和准备，然后显示手工应用暂存目录的命令。`LAUNCHPAD_UPDATE_ENABLED=false` 关闭整个功能。默认只有安装了更新器的主机才能排期。每一步都以 `system.update.*` 记入平台审计日志。另外两个可调参数：`LAUNCHPAD_UPDATE_MIN_FREE_GB`（默认 3，准备所需的空闲磁盘）和 `LAUNCHPAD_UPDATE_STAGE_MAX_GB`（默认 6，暂存目录大小上限）。

## 资源清理

```bash
cd backend
uv run python ../scripts/teardown.py --dry-run   # 列出将被移除的内容
uv run python ../scripts/teardown.py --yes        # 删除(memory → registry → CDK stack)
```

删除是尽力而为、依赖方优先的;S3 桶自动清空,ECR 仓库随栈强制删除。更完整的
清理指南(演示资源 vs 共享基础设施)见 [teardown.zh-CN.md](teardown.zh-CN.md)。
