---
title: "无代码端到端 Agent Ops 实战"
weight: 0
---

# 无代码端到端 Agent Ops 实战

> 使用 AgentCore Launchpad UI，以亚马逊财报分析助手为场景，跑通 Amazon Bedrock
> AgentCore 的**创建 → 部署 → 测试 → 观测 → 评估 → 优化 → A/B → 治理**链路。
>
> 本实验直接使用你的 AWS 账号，并在本地开发机运行 Launchpad。所有 AWS 资源、调用费用和
> 实验结束后的清理均由账号所有者负责。

---

## 这个实验在做什么

**业务场景**：投资研究团队要在亚马逊 2026 年第二季度财报发布后快速产出点评，需要核对
收入、利润、现金流、分部业绩和业绩指引的口径。实验会搭建一个「亚马逊财报分析助手」，
知识来源是随实验提供的
[Amazon Q2 2026 财报新闻稿](static/assets/AMZN-Q2-2026-Earnings-Release.pdf)。

**面向人群**：Agentic 产品、运维和开发人员。

**实验会创建以下 Agent**：

| Agent | 创建方式 | 承担什么 |
|---|---|---|
| `lab-earnings-assistant` | Strands ZIP | 无知识库基线：只能靠模型通用知识回答；用于对话、可观测、评估，以及可选的公共 API 和金丝雀支线 |
| `lab-earnings-advisor` | 托管 Harness | 挂载财报知识库与回答规范技能；第 09 章转换为 Runtime 后参加 A/B |

实验围绕一个结构性事实展开：这份财报发布于 **2026 年 7 月 30 日**，晚于任何可用模型的
训练截止时间。没有挂载财报的 Agent 不可能「记得」这些数字——它要么拒答，要么给出旧季度
或编造的数值。这让「有依据 vs 无依据」的对比是**设计出来的必然坏例**，而不是碰运气。
第 05 章比较有依据的检索回答与无知识库基线，第 08 章用带真值的数据集把两者的差距量化成
分数，第 09 章把保留知识库的 `lab-earnings-advisor` 转为 Runtime，针对两类已观测的回答
缺陷优化提示词并做 A/B；可选的第 10 章用金丝雀灰度 `lab-earnings-assistant` 的候选版本。

两个 Agent 都明确选择模型来源 `Bedrock`，模型统一使用
`global.amazon.nova-2-lite-v1:0`（Nova 2 Lite）。选择轻量模型是有意的：第 05、08 章的
基线缺陷和第 09 章要优化的回答缺陷在轻量模型上更容易复现，评估与优化的前后对比更明显，
调用成本也更低。同一次实验始终使用同一模型，避免把模型差异混入对照。两个 Agent 的创建
方式和系统提示词仍不同，第 05 章因此只做行为对照，不作单变量因果判断；单变量对照留给
第 09 章在同一个 Runtime 上完成。

## 章节目录

| # | 章节 | 内容 | 关键操作参考耗时 |
|---|---|---|---|
| 01 | [自有 AWS 账号环境准备](01-environment) | 安装本地依赖、配置凭证、引导 AWS 资源、启动控制台 | 首次 15–20 分钟 |
| 02 | [部署第一个 Agent（Strands ZIP）](02-deploy-runtime) | 五阶段流水线、ZIP 打包、Runtime 与 Registry 自动登记 | 通常不到 1 分钟 |
| 03 | [部署托管 Harness](03-deploy-harness) | 免打包创建 Harness，并核对两个 Agent | 通常不到 1 分钟 |
| 04 | [挂载能力：Registry 与知识库](04-capabilities) | 财报 PDF → 托管 KB、技能登记与审批、Harness 重新发布 | 约 15 分钟 |
| 05 | [对话测试与记忆](05-chat-memory) | 有/无知识库对照、会话记忆、Memory 控制台四视图 | 约 15 分钟 |
| 06 | [公共 `/v1` API](06-public-api)（可选） | API Key、同步与 SSE 流式、鉴权失败、等价 curl | 约 10 分钟 |
| 07 | [可观测性](07-observability) | 仪表盘、会话还原、trace 瀑布图、token 与成本估算 | 约 5 分钟 |
| 08 | [评估](08-evaluation) | 带真值数据集、自定义 LLM 评估器、双 Agent 对照批量评估 | 两次批量评估各需数分钟 |
| 09 | [配置包 A/B 实验](09-experiment-ab) | Harness 转换 → Runtime 评估 → 自建无参考评估器 → 推荐 → 配置包 → 网关 → 50/50 → 判定 → 清理 | 约 40–55 分钟 |
| 10 | [Runtime 金丝雀](10-canary)（可选） | 候选版本铸造、真实流量分档放量、每档证据门禁 | 约 25 分钟（每档判定最多 15 分钟） |
| 11 | [治理](11-governance)（可选） | Gateway 纳管标签、Cedar LOG_ONLY 策略、决策与审计 | 约 25 分钟 |
| 12 | [收尾与资源清理](12-wrapup-cleanup) | 资源清单、清理顺序、成本提示 | 5–15 分钟 |

**第 06、10、11 三章都是可选支线**，跳过不影响其它章节：后续章节依赖的对象都在第 02–05 章创建，
第 10 章的金丝雀和第 11 章的治理链路都不被任何其它章节引用（只在第 12 章的清理清单里出现）。

### 按时长选一条路径

| 路径 | 章节 | 累计耗时 |
|---|---|---|
| **精简版** | 01 → 02 → 03 → 04 → 05 → 07 → 08 → 12 | **约 1 小时 40 分** |
| 主线 | 精简版 + 09（优化与 A/B） | 约 2 小时 20 分–2 小时 35 分 |
| 完整版 | 主线 + 06、10、11 三条可选支线 | 约 3 小时–3 小时 20 分 |

> 走完整版但只有 3 小时时，从三条支线里去掉一条：去掉第 06 章省 10 分钟，
> 去掉第 11 章省 15–25 分钟。第 09 章的 12 条流量不要削减——流量是并发发送的，12 条只占
> 2–3 分钟，削掉省不下时间；而条数越少，随机分流让某一组样本过小的概率越高，判定会退回
> `INSUFFICIENT-N`。

这里的耗时按各章开头的 `预计耗时` 累加，含讲解和核对；章节目录最后一列只给出关键操作的
参考时间，所以两张表的数不一样。总时长已预留评估排队以及 A/B 与金丝雀判定的等待时间。

**要控制在 1 小时 45 分以内，走精简版**：覆盖「创建 → 部署 → 接地 → 观测 → 量化」，
并完成 Strands ZIP 与托管 Harness 两种创建方式。

第 09 章（优化与 A/B）和第 10 章（灰度）等待较多：转换后 Runtime 的前置批量评估、
`RECOMMEND`、12 条并发流量（约 2–3 分钟）、`VERDICT` 和金丝雀分档判定都要以页面进度为准。
第 09 章的纯等待主要是在线评估聚合（约 10–15 分钟）和推荐生成（成功一次约 4–15 分钟）。
它们和可选的第 11 章（治理）适合单独另开一场。

## 开始之前

### 前置条件检查单

- [ ] 自有 AWS 账号可在 `us-west-2` 使用实验涉及的 Bedrock AgentCore 服务
- [ ] 当前 AWS 身份具备管理员级权限，`aws sts get-caller-identity` 返回预期账号
- [ ] 本机已安装 `uv` ≥ 0.8、Node.js ≥ 20、AWS CLI v2、AWS CDK CLI v2 和 Git
- [ ] 当前账号和 `us-west-2` 已完成 CDK bootstrap
- [ ] 已了解实验资源和模型调用会计入自己的 AWS 账单

第 01 章会安装项目依赖、执行 `make bootstrap`，并检查 CloudWatch Transaction Search 和
五项共享 AgentCore 资源。

### 会花多少钱

一次完整实验约调用模型几十次，索引一份约 530 KB 的 PDF 财报文档，并执行若干次 CloudWatch
Logs Insights 查询。共享基础设施、模型调用、评估、日志摄取和存储都会在自有账号中计费。
Nova 2 Lite 是轻量模型，单次调用成本很低。请在可观测页记录自己的估算值，并在实验结束后按
[第 12 章 · 成本提示](12-wrapup-cleanup#125-成本提示)核对和清理。

### 命名约定

- 实验创建的资源统一用 `lab-` 前缀，方便和 `launchpad-` 共享基础设施区分。
- 指南里 `<ASSISTANT_ID>` / `<KB_ID>` / `<ACCT>` 这类尖括号占位符需要替换成
  当前 AWS 账号里的实际值。代码块里的具体 id 只用于说明字段格式，操作时以自己页面上的值为准。
- 财报金额指南里统一换算为亿美元表述（如 $200.6 billion = 2,006 亿美元），与 PDF 原文的
  billion 口径一一对应。

## 清理

第 09 章的实验和可选第 10 章的金丝雀会占用共享网关互斥锁，完成后必须立即执行各自的
`清理`。实验结束后还要按[第 12 章](12-wrapup-cleanup)删除其余 `lab-` 资源，并决定是否保留
`launchpad-` 共享基础设施。

## 相关文档

- [docs/architecture.zh-CN.md](https://github.com/aws-samples/sample-agentcore-launchpad/blob/main/docs/architecture.zh-CN.md) — 控制台功能 ↔ AgentCore 服务的权威映射
- [docs/setup.zh-CN.md](https://github.com/aws-samples/sample-agentcore-launchpad/blob/main/docs/setup.zh-CN.md) — 环境搭建细节
- [docs/api.zh-CN.md](https://github.com/aws-samples/sample-agentcore-launchpad/blob/main/docs/api.zh-CN.md) — API 参考
- [docs/troubleshooting.zh-CN.md](https://github.com/aws-samples/sample-agentcore-launchpad/blob/main/docs/troubleshooting.zh-CN.md) — 排障
- [docs/teardown.zh-CN.md](https://github.com/aws-samples/sample-agentcore-launchpad/blob/main/docs/teardown.zh-CN.md) — 整体环境拆除
