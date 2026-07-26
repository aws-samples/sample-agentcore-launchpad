# AgentCore Launchpad

基于 Amazon Bedrock AgentCore 构建的**生产级企业 Agent 平台**。它是一个可交付
给客户的样板资产(sample asset)——而非一次性演示 demo——把 AgentCore 各核心
组件真正接入真实 API、真实资源,直接运行在你自己的 AWS 账号里,并为用户提供一个
统一入口来**创建 Agent、部署到 AgentCore Runtime、并通过聊天或 HTTP 消费**。

- English: [README.md](README.md)

## 它是什么

Launchpad 是一个控制台(React)+ 一个 FastAPI 后端,外加共享 AWS 基础设施
(CDK)和一个 vendored 的 Strands Studio 子应用。它交付:

- **三种创建方式,一条部署管道。** 用户可通过 **方式A — Claude Agent SDK**
  (打包为 ARM64 容器镜像)、**方式B — Managed Harness**(声明式 `CreateHarness`
  ——模型、提示词、工具、技能、记忆;无代码、无需构建)、或 **方式C — Strands
  Studio**(可视化拖拽画布,生成 Strands 代码)创建 Agent。三种方式统一收敛到
  同一条五阶段管道,最终部署到 AgentCore Runtime(方式A/C)或托管 Harness 服务
  (方式B)。
- **注册表控制台。** AgentCore Registry 的可视化前端,用于编目和发现三类资产
  ——Agent(A2A)、MCP 工具、Skill——并提供 submit → approve 的生命周期操作。
- **Chat 交互页面 + 公开 `/v1` API。** 选中任意已激活的 Agent 即可对话,支持
  流式响应、多轮历史、以及 session 级记忆。同一套调用链以 `X-Api-Key` 鉴权的
  `/v1` 接口对外暴露,供系统集成,两个入口行为完全一致。
- **治理。** 在 AgentCore Gateway 处强制生效的 Cedar 策略(Allow/Deny,并带上
  作出判定的 policy id)、决策日志、以及从 CloudWatch Transaction Search
  (`aws/spans`)读取的端到端 trace。
- **评估与优化。** 真实的 batch 与 online 评估,含 13 个内置评估器加自定义
  LLM-as-a-judge、失败归因 insights,以及一条优化闭环——产出 control/treatment
  **配置捆绑(configuration bundles)**、通过网关跑 A/B 与 canary 流量、并晋级
  胜出方。

这些能力如何映射到 AgentCore 服务,见 [docs/architecture.zh-CN.md](docs/architecture.zh-CN.md)。

## 快速开始(约 10 分钟)

### 前置条件

- 已在 `us-west-2` 开启 Bedrock AgentCore 预览的 AWS 账号(Runtime、Harness、
  Registry、Gateway、Policy、Evaluation)
- 具备管理员级别权限的凭证(`aws sts get-caller-identity` 可用)
- `uv` ≥ 0.8、Node.js ≥ 20(`npm`)、AWS CDK CLI v2(`npm i -g aws-cdk`)、
  Docker(支持 ARM64——仅方式A容器路径需要)
- 每个账号/区域执行一次 CDK bootstrap:`cdk bootstrap aws://<account>/us-west-2`

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

它会部署 CDK 栈 `launchpad-base`(仅在缺失时),一次性确保 AgentCore registry、
memory、gateway 与 policy engine,并写出 `config/launchpad.yaml`。该步骤是
**幂等**的——再次运行只会打印 `reused`。

### 3. 本地运行

```bash
./start.py          # 后台开发服务器,支持自动重载
./start.py --prod   # 构建平台前端并运行本地生产预览
./stop.sh           # 只停止 start.py 所属的进程
```

在 `http://localhost:5173` 打开控制台;API 文档通过代理访问
`http://localhost:5173/api/docs`。需要将同一套开发栈绑定到当前终端时,
使用 `make dev`。

### 4. 创建第一个 Agent

最快的路径是 **Managed Harness** Agent(方式B)——约 30 秒完成部署,无需构建
步骤。可在控制台 **Create Agent** 页面创建,或使用 curl:

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

轮询部署 job 或 Agent,直到状态变为 `active`:

```bash
curl -s localhost:8000/api/agents/<AGENT_ID>          # 状态:deploying → active
curl -s localhost:8000/api/jobs/<JOB_ID>              # 分阶段事件流
```

### 5. 与它对话

在控制台 **Chat** 页面,或通过公开 API——先创建一个密钥:

```bash
curl -s -X POST localhost:8000/api/apikeys -H 'Content-Type: application/json' \
  -d '{"name": "quickstart"}'
# → {"id": "…", "prefix": "lp_live_…", "key": "lp_live_<仅展示一次>"}

curl -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke \
  -H "X-Api-Key: lp_live_<完整密钥>" -H 'Content-Type: application/json' \
  -d '{"prompt": "How many vacation days does Maya Chen have left?"}'
# → {"agent":"hr-assistant","text":"…","session_id":"…","latency_ms":…}
```

完整 API 参考(同步 + SSE 流式、Python):[docs/api.zh-CN.md](docs/api.zh-CN.md)。

## 启动与停止

根目录的生命周期脚本把平台后端与前端作为一套本地服务统一管理。独立的
vendored Studio 不属于该生命周期;平台内置的 Studio 位于 `/create/studio`。

### 后台开发模式

```bash
./start.py
```

该命令在后台启动整套服务,后端支持自动重载。开发服务器默认绑定到
`127.0.0.1`。

### 本地生产模式

```bash
./start.py --prod
```

生产模式会构建平台前端、提供优化后的静态资源,并关闭后端自动重载。UI 与 API
服务都绑定到 `0.0.0.0`。

| 服务 | 默认地址 | 端口覆盖变量 |
|---|---|---|
| 平台控制台 | `http://localhost:5173` | `PLATFORM_UI_PORT` |
| 平台 API | `http://localhost:8000` | `PLATFORM_API_PORT` |

可通过 `LAUNCHPAD_HOST` 和 `LAUNCHPAD_API_HOST` 覆盖 UI 与 API 的绑定地址。
若任一配置端口已被占用,启动器会在创建进程前失败。

### 停止服务

```bash
./stop.sh
```

`start.py` 把进程归属信息和每个服务的日志记录在 `.run/` 下。`stop.sh` 只会
优雅终止这些已记录的进程组,不会误杀使用相似命令的无关服务。服务健康时重复
运行 `start.py` 是幂等的,只会打印当前访问地址。

需要绑定当前终端的前台开发模式时,使用 `make dev`,并通过 `Ctrl+C` 停止。

## 仓库结构

| 路径 | 内容 |
|---|---|
| `backend/` | FastAPI 后端——部署管道、调用链、评估与优化、SQLite 台账 |
| `backend/app/routers/` | 控制台 `/api` + 公开 `/v1` 接口 |
| `backend/app/deployer/` | 统一管道 + 各方式的阶段实现(harness、zip_runtime、container、studio) |
| `frontend/` | React 控制台(Vite)——Overview、Create Agent、Registry、Chat、Governance、Evaluation |
| `infra/` | AWS CDK 应用——`launchpad-base` 共享栈 |
| `apps/studio/` | vendored 的 Strands Studio 子应用(方式C),已改接平台管道 |
| `start.py`、`stop.sh` | 后台本地服务生命周期、健康检查、PID 归属与日志 |
| `scripts/` | `bootstrap.py`、`teardown.py`、`dev.sh`、`verify.sh`、`i18n_check.py` |
| `config/` | `launchpad.example.yaml`(已提交);`launchpad.yaml`(生成、gitignored) |
| `docs/` | 环境搭建、API、架构、故障排查、资源清理、Studio 集成 |

## 文档

| 文档 | |
|---|---|
| [docs/lab/README.md](docs/lab/README.md) | **动手实验指南** — 部署 → 测试 → 观测 → 评估 → 优化 → A/B → 治理 全链路逐步实操（对接真实 AWS） |
| [docs/setup.zh-CN.md](docs/setup.zh-CN.md) | 环境搭建、引导、清理([English](docs/setup.md)) |
| [docs/architecture.zh-CN.md](docs/architecture.zh-CN.md) | 平台 ↔ AgentCore 映射、管道、调用链([English](docs/architecture.md)) |
| [docs/api.zh-CN.md](docs/api.zh-CN.md) | 公开 `/v1` API 参考([English](docs/api.md)) |
| [docs/troubleshooting.zh-CN.md](docs/troubleshooting.zh-CN.md) | 已验证的坑与耗时([English](docs/troubleshooting.md)) |
| [docs/teardown.zh-CN.md](docs/teardown.zh-CN.md) | 演示资源 vs 共享基础设施清理([English](docs/teardown.md)) |
| [docs/studio-integration.md](docs/studio-integration.md) | Strands Studio(方式C)集成 |

## 成本说明

运行本演示会产生常规的 AWS 使用费用——Launchpad 本身不额外收费。在演示规模下
成本是定性的、较小的,但会随你对每一层的使用强度而增长:

- **Runtime / Harness 调用**——每次调用计费模型 token(默认
  `global.anthropic.claude-sonnet-4-6`)加托管 runtime/session 计算。
- **容器构建(方式A)**——CodeBuild ARM64 构建分钟数,每个 Agent 构建约 2 分钟;
  方式B(harness)无构建,方式C 走更快的 zip 路径。
- **批量评估(batch evaluation)**——LLM-as-a-judge 调用(模型 token),随
  评估器数 × 数据集条目数增长;insights 运行更重、更耗时。
- **CloudWatch Transaction Search**——开启可观测性期间的 trace/span 摄取与存储。
- **存储**——S3 产物 zip 与 ECR 容器镜像随每次 Agent 构建累积;AgentCore Memory
  存储 session 事件与抽取出的偏好。

**用完后请删除演示 Agent**(控制台,或 `DELETE /api/agents/{id}`),再运行
`scripts/teardown.py` 移除共享基础设施。见 [docs/teardown.zh-CN.md](docs/teardown.zh-CN.md)。
