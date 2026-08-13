---
title: "无代码端到端 Agent Ops 实战"
weight: 0
---

# 无代码端到端 Agent Ops 实战

> 使用 AgentCore Launchpad UI，以 AgentCore 配额与容量规划助手为场景，跑通 Amazon Bedrock
> AgentCore 的**创建 → 部署 → 测试 → 观测 → 评估 → 优化 → A/B → 治理**链路。
>
> Workshop Studio 会为参与者分配临时 AWS 账号，并部署一台预装实验工具和源码的
> Ubuntu 24.04 Graviton EC2。默认主线都在这台 EC2 上操作。

---

## 这个实验在做什么

**业务场景**：平台工程团队正在准备 AgentCore 工作负载上线，需要核对固定限制、可调整配额、
区域差异和容量风险。实验会搭建一个「AgentCore 配额与容量规划助手」，知识来源是随实验提供的
:link[AgentCore 配额文档]{href=":assetUrl{path="/assets/bedrock-agentcore-limits.md" source=repo}" action=download}。

**面向人群**：Agentic 产品、运维和开发人员。

**实验会创建以下 Agent**：

| Agent | 创建方式 | 承担什么 |
|---|---|---|
| `lab-quota-advisor` | 托管 Harness | 挂载配额知识库与回答规范技能；基础部分（第 03–06 章）的主线对象 |
| `lab-hr-assistant` | Strands ZIP 快速通道 | 进阶部分第 07 章创建，带五个 HR 工具，用于评估与配置包 A/B |

实验围绕两个问题展开，分别对应两个部分：没有指定资料的 Agent 很难稳定核对配额值、单位、
区域范围和可调整性——基础部分用挂载知识库的 `lab-quota-advisor` 核对检索回答；而带工具的
Agent 会把数据交给无权查看的人——进阶部分新建 `lab-hr-assistant`，自建策略评估器把这个
缺陷变成分数，据此优化提示词、做配置包 A/B，最后用 Cedar 策略治理工具调用。

## 实验结构：两个独立部分

实验分为**基础**和**进阶**两个部分，互相不依赖对方创建的资源。可以只做其中一个部分，
也可以按顺序都做；无论选哪种，先完成环境准备（第 00–01 章，约 10 分钟；自有账号
走第 02 章）。

![实验流程：环境准备后按需选择基础或进阶部分](/static/images/index-parts-flow.png)  
*图 0-1：两个部分互不依赖，可单独完成；都做时按章节顺序进行。*

| 部分 | 章节 | 目的 | 预计耗时（不含环境准备） |
|---|---|---|---|
| **Part 1 · 基础** | 03–06 | 了解 AgentCore 基础：创建部署 Agent、挂载知识库与技能、对话与记忆、可观测性 | **约 35 分钟** |
| **Part 2 · 进阶** | 07–09 | 了解 AgentCore 评估、优化与治理：自定义评估器量化缺陷、配置包 A/B 修复、Cedar 策略治理 | **约 1 小时 20 分** |
| 收尾（可选） | 10 | 资源清单与清理顺序 | 5–15 分钟 |

Part 2 从零创建自己的实验对象（`lab-hr-assistant`），第 09 章治理的是 bootstrap 预建的
共享 Gateway——都不需要 Part 1 的产物。两个部分都做时按章节顺序进行，全程约
2 小时 15 分–2 小时 25 分。

## 章节目录

### 环境准备（两个部分都需要）

| # | 章节 | 内容 | 关键操作参考耗时 |
|---|---|---|---|
| 00 | [获取 Workshop Studio 实验账号](00-access-account) | 邮箱 OTP 登录、加入活动 | 约 5 分钟 |
| 01 | [环境准备与控制台导览](01-environment) | 浏览器打开控制台并登录、验证环境、控制台导览 | 约 5 分钟 |
| 02 | [可选：Self-paced 自有 AWS 账号与本地开发机](02-own-account-local) | 不使用 Workshop Studio 账号和 EC2 时的自助环境路径 | 首次 15–20 分钟 |

环境准备只选一种：Workshop Studio 走第 00–01 章，使用临时账号和预置 EC2；self-paced
走第 02 章，使用自有账号和本地开发机。Self-paced 首次准备环境约多用 10 分钟。

### Part 1 · 基础：AgentCore 基础

| # | 章节 | 内容 | 关键操作参考耗时 |
|---|---|---|---|
| 03 | [部署托管 Harness](03-deploy-harness) | 免打包创建 Harness，两条创建路径的能力边界 | 约 5 分钟 |
| 04 | [挂载能力：Registry 与知识库](04-capabilities) | Markdown 配额文档 → 托管 KB、技能登记与审批、Harness 重新发布 | 约 15 分钟 |
| 05 | [对话测试与记忆](05-chat-memory) | 挂载知识库后的接地回答、会话记忆、Memory 控制台四视图 | 约 10 分钟 |
| 06 | [可观测性](06-observability) | 仪表盘、会话还原、trace 瀑布图、token 与成本估算 | 约 5 分钟 |

### Part 2 · 进阶：评估、优化与治理

| # | 章节 | 内容 | 关键操作参考耗时 |
|---|---|---|---|
| 07 | [评估](07-evaluation) | 新建带工具的 ZIP Runtime、复现越权缺陷、自定义 LLM 评估器、批量评估四阶段 | 约 30 分钟 |
| 08 | [配置包 A/B 实验](08-experiment-ab) | 洞察 → AI 推荐 → 配置包 → 网关 → 50/50 → 判定 → 晋级与清理 | 约 20–30 分钟 |
| 09 | [治理](09-governance) | Gateway 纳管标签、Cedar LOG_ONLY 策略、决策与审计 | 约 25 分钟 |

### 收尾（可选）

| # | 章节 | 内容 | 关键操作参考耗时 |
|---|---|---|---|
| 10 | [收尾与资源清理](10-wrapup-cleanup) | 资源清单、清理顺序、成本提示 | 5–15 分钟 |

## 开始之前

### 前置条件检查单

下面的检查项只适用于 Workshop Studio 主线。self-paced 参与者请改按
[可选第 02 章](02-own-account-local)准备环境。

- [ ] 已按[第 00 章](00-access-account)加入本 Workshop Studio 活动，且活动状态为可开始
- [ ] Workshop Studio 分配的 AWS 账号和基础设施均显示部署成功
- [ ] 一个现代浏览器（Chrome / Edge / Safari）
- [ ] 可以在 Stack Outputs 中看到 `ConsoleUrl`、`ConsoleUsername` 和 `ConsolePassword`
- [ ] 活动账号已预先开启 CloudWatch Transaction Search（第 06 章需要）

> **不需要安装 AWS CLI，也不需要配置本机凭证。** 控制台已发布到公网地址，浏览器登录即可。

`uv`、Node.js、AWS CDK CLI、项目依赖和源码都已预置在实例上，共享 AgentCore
资源也在活动部署阶段完成了预热，个人电脑上不用装。控制台在部署阶段就以生产模式发布完成，
参与者全程在浏览器里操作。AWS 区域由活动创建者选择，推荐 `us-east-1`、备选 `us-west-2`，
控制台的「环境信息」会显示当前区域。

### 会花多少钱

Workshop Studio 临时账号不会向参与者的个人账号计费。一次完整实验约调用模型几十次，索引一份
约 38 KB 的 Markdown 文档，并执行若干次 CloudWatch Logs Insights 查询。模型调用费用会随
实际调用次数、输入长度和输出长度变化，不含预置 EC2 和基础设施。请在可观测页记录自己的估算值，详见
[第 10 章 · 成本提示](10-wrapup-cleanup#115-成本提示)。

### 命名约定

- 实验创建的资源统一用 `lab-` 前缀，方便和 Workshop Studio 预置资源区分。
- 指南里 `<ASSISTANT_ID>` / `<KB_ID>` / `<ACCT>` 这类尖括号占位符需要替换成
  当前活动账号里的实际值。代码块里的具体 id 只用于说明字段格式，操作时以自己页面上的值为准。

## 清理

跑完后**唯一必须清理**的是实验的中间资源，它们会占用共享网关互斥锁：
见第 08 章的`清理`。其余资源的完整清理清单见
[第 10 章](10-wrapup-cleanup)。

## 相关文档

- [docs/architecture.zh-CN.md](https://github.com/aws-samples/sample-agentcore-launchpad/blob/main/docs/architecture.zh-CN.md) — 控制台功能 ↔ AgentCore 服务的权威映射
- [docs/setup.zh-CN.md](https://github.com/aws-samples/sample-agentcore-launchpad/blob/main/docs/setup.zh-CN.md) — 环境搭建细节
- [docs/api.zh-CN.md](https://github.com/aws-samples/sample-agentcore-launchpad/blob/main/docs/api.zh-CN.md) — API 参考
- [docs/troubleshooting.zh-CN.md](https://github.com/aws-samples/sample-agentcore-launchpad/blob/main/docs/troubleshooting.zh-CN.md) — 排障
- [docs/teardown.zh-CN.md](https://github.com/aws-samples/sample-agentcore-launchpad/blob/main/docs/teardown.zh-CN.md) — 整体环境拆除
