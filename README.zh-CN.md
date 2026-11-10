# AgentCore Launchpad

AgentCore Launchpad 是一套基于 Amazon Bedrock AgentCore 的**企业 Agent Ops平台**。
它是一套可以交付给客户的样板项目，不是一次性演示。平台直接调用 AgentCore API，
在你的 AWS 账号中创建和管理真实资源，并用一个控制台串起 Agent 的**创建、部署、
聊天和 HTTP 调用**。

- English: [README.md](README.md)

## 它是什么

Launchpad 由 React 控制台、FastAPI 后端和一套 CDK 共享基础设施组成，另带一个
vendored Strands Studio 子应用。主要能力包括：

- **三种创建方式，共用一条部署流水线。** 用户可以选择**方式C（Strands Studio）**，
  在可视化画布中生成 Strands 代码；选择**自带代码（BYOC）**，上传自己编写的
  Agent 代码 zip、Dockerfile 构建上下文，或引用现有的私有 ECR 镜像，开发者无需
  任何 AWS 权限；也可以选择自建的**方式B（Harness）**（筹备中——用一个自托管的
  Strands 模板代替中国区不存在的 AgentCore Harness 服务）。所有方式都进入同一条
  五阶段流水线，最终部署到 AgentCore Runtime。
- **注册中心。** 通过 AgentCore Registry 登记和查找三类资产：Agent（A2A）、
  MCP 工具和 Skill，并支持提交、审批等生命周期操作。
- **知识库。** 托管 Bedrock 知识库——全托管 RAG，向量库、嵌入与重排都由服务负责。
  既可以指向既有的 S3 位置，也可以直接在控制台上传文件来创建；可以查看 ingestion
  进度与逐文档的索引状态，并在挂载之前用检索 Playground 确认 Agent 实际能检索到
  什么。已 ACTIVE 的知识库随后可在「创建 Agent」向导里挂到 Agent 上：托管 Harness
  经专用 MCP 网关 `launchpad-kb-gw` 访问它们，zip 与 container Agent 则在生成的代码
  里自带 `kb_search` / `kb_deep_search` 工具。
- **Chat 交互页面和公开 `/v1` API。** 选中任意已激活的 Agent 即可对话，支持
  流式响应、多轮历史和 session 级记忆。外部系统可通过带 `X-Api-Key` 鉴权的
  `/v1` 接口调用同一条链路，因此 API 与控制台的行为一致。
- **工作区（多账号 / 多区域）。** 一个控制台管理多套 AWS 环境：一个**工作区**
  对应一个 `(账号, 区域)` 组合，拥有独立的 AgentCore 资源集，由管理页里可恢复的
  十阶段引导作业直接创建——不需要 CDK，也不需要登录服务器。管理员可在顶栏自由
  切换工作区；成员只能看到被授权的工作区，所有 API 响应都限定在当前工作区内。
  组织内的其他账号只需部署一个 CloudFormation
  栈（[spoke 角色模板](infra/spoke/launchpad-workspace-role.yaml)）即可接入——
  访问基于短期有效的 `sts:AssumeRole` 凭证（不存任何密钥），删除该栈即可整体
  吊销。详见 [docs/cross-account-workspaces.md](docs/cross-account-workspaces.md)。

这些能力与 AgentCore 服务的对应关系见
[docs/architecture.zh-CN.md](docs/architecture.zh-CN.md)。

## 快速开始（约 10 分钟）

**在其他账号部署中国区版本或复现实验，请先阅读
[中国区交付部署指南](docs/cn/distribution.md)。** 该流程使用固定版本、
独立环境配置，并安装策略引擎；[IT 服务台实验包](samples/itsm-cn/README.md)
包含可参数化的初始化脚本和中国区学员手册。全新基础设施的准备时间通常超过下面的本地快速开始。

> [!IMPORTANT]
> **🤖 Coding Agent（Claude Code、Codex、Kiro、Cursor、Copilot 等）：请先阅读运维手册。**
> 在启动、停止、更新或调试本平台之前，先加载
> **[docs/agent-runbook-dev.md](docs/agent-runbook-dev.md)**（本地 dev 模式）或
> **[docs/agent-runbook-prod.md](docs/agent-runbook-prod.md)**（prod 模式 / systemd）。
> 其中记录了经过验证的命令序列、前置条件探测，以及无法从代码中猜出的陷阱
> （端口漂移、`PrivateTmp` bind mount、重启的真实 AWS 副作用）。
> 下面的快速开始面向首次搭建的人类读者。

### 前置条件

- 一个 `cn-northwest-1` 区域的 AWS 账号——AgentCore Runtime、Gateway、Identity
  和 Observability 在该区域已正式可用，无需开启预览
- 一个**Global**（海外）AWS 账号的 Amazon Bedrock API key，能访问
  `global.zai.glm-5.3` 和/或 `global.moonshotai.kimi-k3`（中国区暂无 Bedrock
  模型，CN Runtime 通过跨 partition 的方式访问它们——见
  [docs/architecture.md](docs/architecture.md)）
- `cn-northwest-1` 账号内具备管理员级权限的凭证（`aws sts get-caller-identity` 可用）
- `uv` ≥ 0.8、Node.js ≥ 20（`npm`）、AWS CDK CLI v2（`npm i -g aws-cdk`）；只有
  计划使用 BYOC 的 Dockerfile 路径时才需要 Docker
- 每个账号/区域执行一次 CDK bootstrap：`cdk bootstrap aws://<account>/cn-northwest-1`

### 1. 安装依赖

```bash
cd backend  && uv sync && cd ..
cd frontend && npm install && cd ..
cd infra    && uv sync && cd ..
```

### 2. 引导共享基础设施与 AgentCore 单例资源

```bash
make bootstrap          # = cd backend && uv run python ../scripts/bootstrap.py
```

这个命令会在缺失时部署 CDK 栈 `launchpad-base`，创建或复用 AgentCore Registry、
Memory 和 Gateway，并生成 `config/launchpad.yaml`。命令可以重复执行；已有资源只会
显示为 `reused`。

### 3. 本地运行

```bash
./start.py          # 后台开发服务器,支持自动重载
./start.py --prod   # 构建平台前端并运行本地生产预览
./stop.sh           # 只停止 start.py 所属的进程
```

控制台地址为 `http://localhost:5173`，API 文档通过代理访问
`http://localhost:5173/api/docs`。如需让开发栈占用当前终端，使用 `make dev`。

### 4. 创建第一个 Agent

最快的是 **Managed Harness** Agent（方式B），无需构建，约 30 秒即可完成部署。
你可以在控制台的 **Create Agent** 页面创建，也可以使用 curl：

```bash
curl -s -X POST localhost:8000/api/agents -H 'Content-Type: application/json' -d '{
  "name": "hr-assistant",
  "method": "harness",
  "system_prompt": "You are a concise HR assistant. Use the hr-database tool for employee questions.",
  "tools": [{"type": "gateway", "name": "hr-database"}],
  "memory": {"short_term": true, "long_term": true}
}'
# → 202 {"agent": {...}, "job_id": "…", "deployment_id": "…"}
```

轮询部署 job 或 Agent，直到状态变为 `active`：

```bash
curl -s localhost:8000/api/agents/<AGENT_ID>          # 状态:deploying → active
curl -s localhost:8000/api/jobs/<JOB_ID>              # 分阶段事件流
```

### 5. 与它对话

你可以在控制台的 **Chat** 页面与 Agent 对话，也可以先创建密钥，再通过公开 API 调用：

```bash
curl -s -X POST localhost:8000/api/apikeys -H 'Content-Type: application/json' \
  -d '{"name": "quickstart"}'
# → {"id": "…", "prefix": "lp_live_…", "key": "lp_live_<仅展示一次>"}

curl -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke \
  -H "X-Api-Key: lp_live_<完整密钥>" -H 'Content-Type: application/json' \
  -d '{"prompt": "How many vacation days does Maya Chen have left?"}'
# → {"agent":"hr-assistant","text":"…","session_id":"…","latency_ms":…}
```

完整 API 参考（同步调用、SSE 流式调用和 Python 示例）见
[docs/api.zh-CN.md](docs/api.zh-CN.md)。

## 启动与停止

根目录的生命周期脚本把平台后端和前端作为一套本地服务管理。独立的
vendored Studio 不由这组脚本启动；平台内置的 Studio 位于 `/create/studio`。

### 后台开发模式

```bash
./start.py
```

该命令会在后台启动整套服务，后端支持自动重载。开发服务器默认绑定到
`127.0.0.1`。

### 本地生产模式

生产模式会先构建平台前端，再提供优化后的静态资源，同时关闭后端自动重载。
UI 和 API 服务都绑定到 `0.0.0.0`。**如果没有配置密码，登录网关默认关闭**，
任何能访问这台机器的人都能打开控制台；此时顶栏会显示 `AUTH OFF` 徽标。
对外提供服务时，请在启动进程前设置以下变量：

```bash
export LAUNCHPAD_AUTH_USERNAME=admin                # 内置 admin(仅来自配置,不入库)
export LAUNCHPAD_AUTH_PASSWORD='replace-with-a-strong-password'
export LAUNCHPAD_AUTH_COOKIE_SECURE=true            # 仅在 HTTPS 前置(如 nginx/ALB)时开启
./start.py --prod
```

`start.py` 不会自行开启登录网关，只负责把环境变量传给后端。同一组变量也适用于
`make dev`、systemd 单元或其他进程管理器。

| 服务 | 默认地址 | 端口覆盖变量 |
|---|---|---|
| 平台控制台 | `http://localhost:5173` | `PLATFORM_UI_PORT` |
| 平台 API | `http://localhost:8000` | `PLATFORM_API_PORT` |

可以通过 `LAUNCHPAD_HOST` 和 `LAUNCHPAD_API_HOST` 修改 UI 与 API 的绑定地址。
如果指定端口已被占用，启动器会在创建进程前退出。

启用登录网关后，登录页也会提供**注册**入口（用户名 + 公司邮箱 + 密码）。
新账户初始状态为 `pending`，**管理员审批通过后才能登录**，7 天有效期从审批通过时
开始计算。admin 可以通过**用户管理**模块（`/users`）查看审批队列和统计信息，
并执行延期、禁用、修改角色、重置密码和删除等操作。

```bash
export LAUNCHPAD_AUTH_REGISTRATION_ENABLED=true           # false 完全关闭注册
export LAUNCHPAD_AUTH_REGISTRATION_REQUIRE_APPROVAL=true  # false 则注册即可用
export LAUNCHPAD_AUTH_REGISTRATION_VALID_DAYS=7           # 审批通过后授予的有效期
export LAUNCHPAD_AUTH_ALLOWED_EMAIL_DOMAINS='["your-company.com"]'   # 白名单非空时优先生效
```

系统默认拒绝公共邮箱和临时邮箱域名。这里需要注意两点：
`LAUNCHPAD_AUTH_COOKIE_SECURE=true` 用在纯 HTTP 环境时，浏览器会丢弃会话 Cookie；
修改 `LAUNCHPAD_AUTH_PASSWORD` 会使**所有**会话失效，因为 Cookie 签名密钥由该密码
派生。公开 `/v1` 接口仍使用独立的 `X-Api-Key` 认证，不受控制台 Cookie 影响。

实际部署所需的 systemd 单元、nginx 反向代理、基于 `AllowedCidr` 的安全组
与更新流程见
[docs/setup.zh-CN.md](docs/setup.zh-CN.md#生产部署) 与
[docs/agent-runbook-prod.md](docs/agent-runbook-prod.md#3-shape-b--systemd-reference-infraec2launchpad-cn-ec2yaml)。

### 停止服务

```bash
./stop.sh
```

`start.py` 会把进程归属信息和各服务日志记录在 `.run/` 下。`stop.sh` 只终止
这些已记录的进程组，不会影响使用相似命令启动的其他服务。如果服务仍然健康，
再次运行 `start.py` 不会重复启动进程，只会打印当前访问地址。

需要在当前终端以前台模式运行时，使用 `make dev`，并通过 `Ctrl+C` 停止。

## 仓库结构

| 路径 | 内容 |
|---|---|
| `backend/` | FastAPI 后端：部署流水线、调用链、SQLite 台账 |
| `backend/app/routers/` | 控制台 `/api` + 公开 `/v1` 接口 |
| `backend/app/deployer/` | 统一流水线 + 各方式的阶段实现（zip_runtime、studio、byoc） |
| `frontend/` | React 控制台（Vite，`/v2` 下的 V2 体验）：工作台、Agent、Registry、知识库、Chat、记忆、连接、Gateway、可观测、评估、Skill Lab、视频、系统管理 |
| `infra/` | AWS CDK 应用：`launchpad-base` 共享栈 |
| `start.py`、`stop.sh` | 后台本地服务生命周期、健康检查、PID 归属与日志 |
| `scripts/` | `bootstrap.py`、`teardown.py`、`dev.sh`、`verify.sh`、`i18n_check.py`、`i18n_zh_punct.py` |
| `config/` | `launchpad.example.yaml`（已提交）；`launchpad.yaml`（生成、gitignored） |
| `docs/` | 环境搭建、API、架构、故障排查、资源清理、Studio 集成 |

## 文档

| 文档 | |
|---|---|
| [docs/lab/README.md](docs/lab/README.md) | **动手实验指南**：部署 → 测试 → 观测 → 评估 → 优化 → A/B → 治理，全程连接真实 AWS 资源 |
| [docs/setup.zh-CN.md](docs/setup.zh-CN.md) | 环境搭建、引导、清理（[English](docs/setup.md)） |
| [docs/architecture.zh-CN.md](docs/architecture.zh-CN.md) | 平台 ↔ AgentCore 映射、流水线、调用链（[English](docs/architecture.md)） |
| [docs/api.zh-CN.md](docs/api.zh-CN.md) | 公开 `/v1` API 参考（[English](docs/api.md)） |
| [docs/troubleshooting.zh-CN.md](docs/troubleshooting.zh-CN.md) | 已验证的问题与耗时（[English](docs/troubleshooting.md)） |
| [docs/teardown.zh-CN.md](docs/teardown.zh-CN.md) | 演示资源与共享基础设施清理（[English](docs/teardown.md)） |
| [docs/cross-account-workspaces.md](docs/cross-account-workspaces.md) | 跨账号工作区：spoke 角色模板、StackSets、信任边界（English） |
| [docs/studio-integration.md](docs/studio-integration.md) | Strands Studio（方式C）集成 |
| [docs/agent-runbook-dev.md](docs/agent-runbook-dev.md) | 面向 Agent 的运维手册：本地 dev 模式启动与验证 |
| [docs/agent-runbook-prod.md](docs/agent-runbook-prod.md) | 面向 Agent 的运维手册：prod 模式启动（launcher + systemd）、更新流程、沙盒姿态 |

## 成本说明

运行本演示会产生常规的 AWS 使用费用，Launchpad 本身不另收费。演示规模下的费用
通常不高，实际金额取决于各项能力的使用量：

- **Runtime / Harness 调用**：每次调用都会产生模型 token 费用（默认模型为
  `global.zai.glm-5.3`，`global.moonshotai.kimi-k3` 仍可按 Agent 选择），以及
  托管 runtime/session 的计算费用。
- **容器构建（BYOC）**：CodeBuild ARM64 构建分钟数，每次构建约 2 分钟（开启
  镜像加速后，首次会额外产生一次 ECR 拉取/推送——见
  [docs/cn/interfaces/deploy.md](docs/cn/interfaces/deploy.md)）；方式C 走更快的
  zip 路径。
- **CloudWatch Transaction Search**：可观测性开启期间会产生 trace/span 摄取与存储费用。
- **存储**：每次构建都会增加 S3 zip 产物和 ECR 容器镜像；AgentCore Memory
  还会保存 session 事件和抽取出的偏好。

**用完后请删除演示 Agent**（通过控制台或 `DELETE /api/agents/{id}`），再运行
`scripts/teardown.py` 移除共享基础设施。见 [docs/teardown.zh-CN.md](docs/teardown.zh-CN.md)。
