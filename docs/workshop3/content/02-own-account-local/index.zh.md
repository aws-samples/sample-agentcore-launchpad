---
title: "02 可选：Self-paced 自有 AWS 账号与本地开发机"
weight: 20
---

# 可选章 · Self-paced 自有 AWS 账号与本地开发机

> **适用场景**：Self-paced 参与者使用自有 AWS 账号，在本地运行 Launchpad。本章替代
> [第 01 章的 Workshop Studio 预置 EC2 路径](../01-environment)，两章不要重复执行。
>
> **目标**：准备本地依赖、AWS 凭证和 Launchpad 共享资源，打开中文控制台。
>
> **前置条件**：自有 AWS 账号可使用 Bedrock AgentCore，并能调用
> bedrock mantle `openai.gpt-5.6-sol` 或回退模型 `global.amazon.nova-2-lite-v1:0`，
> 同时允许创建本章列出的共享资源。
>
> 确认服务健康后，直接进入[第 03 章](../03-deploy-harness)。
>
> **预计耗时**：首次约 15–20 分钟，其中 `make bootstrap` 约 8–12 分钟；已引导过的环境约 2 分钟。
>
> **费用和清理**：本章创建的资源由你的 AWS 账号计费，Workshop Studio 不会代为删除。
> 完成实验后请按[第 10 章](../10-wrapup-cleanup)清理。

---

## A.1 准备自有账号和本地工具

账号需在 `us-west-2` 开通实验涉及的 Bedrock AgentCore 服务。当前身份还需有权限创建 S3、
Cognito、IAM 和 CloudWatch 资源。请提前开启 **CloudWatch Transaction Search**，
否则第 06 章没有数据。

本地开发机需要安装以下工具：

```bash
uv --version          # >= 0.8
node --version        # >= 20
cdk --version         # v2
aws --version         # v2
```

AWS CLI v2 按官方文档安装即可：

```bash
# macOS
curl "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o "AWSCLIV2.pkg"
sudo installer -pkg AWSCLIV2.pkg -target /

# Windows（PowerShell 管理员），装完新开一个终端再验证
msiexec.exe /i https://awscli.amazonaws.com/AWSCLIV2.msi
```

本章走本地路径，不需要 Session Manager Plugin。

如果本机还没有源码：

```bash
git clone --depth 1 --branch v0.0.41 https://github.com/aws-samples/sample-agentcore-launchpad.git
cd sample-agentcore-launchpad
```

从这里开始，本章和后续章节的命令都在本地源码目录中执行。不要使用 Session Manager，也不要运行
Workshop Studio 提供的 `FrontendPortForwardCommand`。

## A.2 配置区域、凭证与 CDK

先在本地终端配置好自有账号的 AWS 凭证，再把区域设为 `us-west-2` 并核对身份：

```bash
export AWS_REGION=us-west-2
export AWS_DEFAULT_REGION="$AWS_REGION"
aws sts get-caller-identity
```

继续前请核对返回的账号和角色，后续资源和费用都会计入该账号。

每个账号和区域只需要执行一次 CDK bootstrap。已经执行过的可以跳过，不确定时重复执行也无妨：

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
cdk bootstrap "aws://${ACCOUNT_ID}/${AWS_REGION}"
```

## A.3 安装项目依赖

```bash
cd backend  && uv sync && cd ..
cd frontend && npm install && cd ..
cd infra    && uv sync && cd ..
```

命令执行完后，后端和 infra 目录会生成 `.venv`，前端目录会生成 `node_modules`。
本仓库的 Python 命令都通过 `uv run` 执行，不要直接使用 `python` 或 `pip`。

## A.4 一次性引导 AWS 资源

```bash
make bootstrap        # = cd backend && uv run python ../scripts/bootstrap.py
```

该命令可重复执行：缺少的 CDK 栈会部署，已有的 AgentCore 单例会复用。结果写入
`config/launchpad.yaml`：

| 资源类别 | 名称 |
|---|---|
| S3 产物桶 | `launchpad-artifacts-<ACCOUNT_ID>-us-west-2` |
| IAM 执行角色 | `launchpad-agent-execution-role` |
| Cognito 用户池 | `launchpad-users`（含演示用户 `admin` / `demo`） |
| AgentCore Registry | `launchpad-registry` |
| AgentCore Memory | `launchpad_memory` |
| AgentCore Gateway | `launchpad-gw-<suffix>` |
| AgentCore Policy Engine | `launchpad-pe-<suffix>` |

完成后，`config/launchpad.yaml` 应包含 `region`、`account_id` 和 `resources.*`。文件中有演示
用户密码，请按本地机密保管。

## A.5 启动本地栈

可以在后台启动，也可以留在前台查看日志：

```bash
./start.py            # 后台开发模式，日志与进程记录在 .run/
make dev              # 前台模式，占用当前终端
```

服务是否正常由控制台右上角的状态灯给出，不用在终端里另外验证。

| 服务 | 本地地址 | 端口覆盖变量 |
|---|---|---|
| 平台后端（FastAPI） | `http://127.0.0.1:8000` | `PLATFORM_API_PORT` |
| 平台控制台（Vite） | `http://127.0.0.1:5173` | `PLATFORM_UI_PORT` |
| API 文档（经前端代理） | `http://127.0.0.1:5173/api/docs` | - |

浏览器打开 `http://localhost:5173`，切换到中文界面，并确认右上角显示
`● 系统运行正常`、区域为 `us-west-2`，「服务健康」面板里 Gateway / Memory / Registry / Policy /
Observability 五项为「就绪」。Runtime 与 Evaluation 显示「尚未创建」是正常的，它们统计的是
你还没创建的 Agent 与评估资源。

## 本章验证清单

- [ ] `aws sts get-caller-identity` 返回预期的自有账号
- [ ] `AWS_REGION` 和 `AWS_DEFAULT_REGION` 均为 `us-west-2`
- [ ] `uv`、Node.js、CDK 和 AWS CLI 均可用
- [ ] `config/launchpad.yaml` 包含所需的 `resources.*` 标识符
- [ ] 本地浏览器可以打开控制台，右上角显示 `● 系统运行正常`，服务健康里 bootstrap 预建的 5 项已就绪
- [ ] CloudWatch Transaction Search 已开启
- [ ] 创建 Agent 时会把默认的 `Bedrock Mantle` 改成 `Bedrock`；选 `global.amazon.nova-2-lite-v1:0`

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 控制台打不开，端口 5173 无监听 | Vite 端口被占用后可能自动使用 5174 | 查看启动输出中的实际端口，或用 `PLATFORM_UI_PORT` 指定 |
| 「服务健康」某项为红或未就绪 | bootstrap 未完成、区域错误或权限不足 | 核对身份和区域后重跑 `make bootstrap`；仍失败时查[排障文档](https://github.com/aws-samples/sample-agentcore-launchpad/blob/main/docs/troubleshooting.zh-CN.md) |
| `make bootstrap` 报 CDK 未 bootstrap | 当前账号和区域缺少 CDK 引导 | 执行本章的 `cdk bootstrap` 命令后重跑 |
| 后端启动时报 `config` 相关 KeyError | 缺少 `config/launchpad.yaml` | 先执行 `make bootstrap` |
| 页面能打开但列表为空、统计为 0 | 自有账号中还没有本实验的 Agent | 正常，继续第 02 章 |
| 首次调用提示模型无权访问 | 模型来源仍是默认值，或 Sonnet 5 对当前账号不可用 | 先改为 `Bedrock`；Sonnet 5 不可用时，两个 Agent 都改用 Nova 2 Lite 后重新发布 |

---

下一章：[第 03 章 · 部署托管 Harness](../03-deploy-harness)
