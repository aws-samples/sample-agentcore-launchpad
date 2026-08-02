# AgentCore Launchpad 动手实验指南

> 用一个真实业务场景，跑通 Amazon Bedrock AgentCore 的**创建 → 部署 → 测试 → 观测 → 评估 →
> 优化 → A/B → 治理**链路。
>
> 全程对接**你自己 AWS 账号里的真实资源**（`us-west-2`），没有 mock。

---

## 这个实验在做什么

**业务场景**：为摩根士丹利新兴市场领先企业股票基金（MS INVF Emerging Leaders Equity Fund）
搭一个「基金产品智能助手」，服务销售与客服团队。素材是一份真实的产品路演材料
[`assets/Morgan_Stanley_Oct_21_(EMEA).pdf`](assets/Morgan_Stanley_Oct_21_%28EMEA%29.pdf)。

**实验会创建以下 Agent**：

| Agent | 创建方式 | 承担什么 |
|---|---|---|
| `lab-fund-assistant` | 方式C · Strands ZIP 通道 | 主线：对话 / 公共 API / 可观测 / 评估 / A/B / 金丝雀 |
| `lab-fund-advisor` | 方式B · 托管 Harness | 挂知识库与技能，做有依据的文档问答 |
| `lab-fund-packager` | 方式A · 其他 Agent SDK 容器 | 演示 CodeBuild → ECR → Runtime 路径 |

**实验主线**：第 05 章比较有无知识库时的回答差异；第 08 章用带真值的数据集量化回答质量；
第 09 章让优化器分析 trace，再用改进后的提示词做 A/B；第 10 章用金丝雀把改进版本逐步放到
真实流量上。每一步都可以通过日志、trace、评估结果或判定结果核对。

## 章节目录

| # | 章节 | 内容 |
|---|---|---|
| 01 | [环境准备与控制台导览](01-environment.md) | 前置条件、`make bootstrap`、启动本地栈、11 个模块导览 |
| 02 | [部署第一个 Agent（ZIP 通道）](02-deploy-runtime.md) | 统一五阶段流水线、方式能力矩阵、异步可恢复部署 |
| 03 | [Harness 与容器方式](03-deploy-harness.md) | 免构建 Harness、CodeBuild 容器、三种方式对照 |
| 04 | [挂载能力：Registry 与知识库](04-capabilities.md) | PDF → 托管 KB、技能登记与审批、Harness 重新发布 |
| 05 | [对话测试与记忆](05-chat-memory.md) | 有/无知识库对照、会话记忆、Memory 控制台四视图 |
| 06 | [公共 `/v1` API](06-public-api.md)（可选） | API Key、同步与 SSE 流式、鉴权失败、等价 curl |
| 07 | [可观测性](07-observability.md) | 仪表盘、会话还原、trace 瀑布图、token 与成本估算 |
| 08 | [评估](08-evaluation.md) | 带真值数据集、自定义 LLM 评审、批量评估四阶段 |
| 09 | [配置包 A/B 实验](09-experiment-ab.md) | 推荐 → 配置包 → 网关 → 50/50 → 判定 → 清理（+ 可选 160 条大样本版） |
| 10 | [Runtime 金丝雀](10-canary.md) | 候选版本铸造、真实流量分档放量、每档证据门禁 |
| 11 | [治理](11-governance.md) | Gateway 纳管标签、Cedar LOG_ONLY 策略、决策与审计 |
| 12 | [收尾与资源清理](12-wrapup-cleanup.md) | 资源清单、清理顺序、成本提示 |

标为**可选**的章节是支线，讲的是怎么把 Agent 接进外部系统。跳过它不影响后续章节，
第 07 章起用到的 trace、数据集、实验对象全部来自第 02–05 章。

### 快速路径

时间有限时可走最短路径：**01 → 02 → 04 → 05 → 07 → 08**，
覆盖「创建 → 部署 → 接地 → 观测 → 量化」。
09/10（优化与灰度）与 11（治理）可以单独另开一场。

## 开始之前

### 前置条件检查单

- [ ] AWS 账号已在 `us-west-2` 开启 Bedrock AgentCore 预览（Runtime、Harness、Registry、
      Gateway、Policy、Evaluation）
- [ ] 管理员级凭证可用（`aws sts get-caller-identity` 有输出）
- [ ] 已开启 **CloudWatch Transaction Search**（否则第 07 章所有视图为空）
- [ ] `uv ≥ 0.8`、`Node.js ≥ 20`、AWS CDK CLI v2、Docker（仅方式A 需要）
- [ ] 每账号/区域一次性 `cdk bootstrap aws://<ACCOUNT_ID>/us-west-2`
- [ ] 已执行 `make bootstrap`，`config/launchpad.yaml` 已生成
- [ ] 本地栈已启动（`./start.py` 或 `make dev`），控制台 http://localhost:5173 可打开
- [ ] 控制台右上角已切到 **中文**（本指南所有截图为中文界面）

### 会花多少钱

费用主要来自 Bedrock 模型调用、知识库索引与检索，以及 CloudWatch Logs Insights 查询。
Logs Insights 按扫描量计费，平台使用 60 秒 TTL 缓存减少重复查询。开始实验前，请查看
[第 12 章 · 成本提示](12-wrapup-cleanup.md#125-成本提示)。

### 命名约定

- 实验创建的资源统一用 `lab-` 前缀，方便和你环境里已有的资源区分。
- 指南里 `<ASSISTANT_ID>` / `<ADVISOR_ID>` / `<KB_ID>` / `<ACCT>` 这类尖括号占位符需要替换成
  你自己的值。命令输出中的 `<RUNTIME_ID>`、`<RECORD_ID>` 等占位符也以你的实际结果为准。


## 清理

完成实验后，**必须清理**实验与金丝雀的中间资源（它们会占用共享网关互斥锁）：
见第 09 章的 `CLEANUP` 与第 10 章的 `清理`。其余资源的完整清理清单见
[第 12 章](12-wrapup-cleanup.md)。

## 相关文档

- [docs/architecture.zh-CN.md](../architecture.zh-CN.md) — 控制台功能 ↔ AgentCore 服务的权威映射
- [docs/setup.zh-CN.md](../setup.zh-CN.md) — 环境搭建细节
- [docs/api.zh-CN.md](../api.zh-CN.md) — API 参考
- [docs/troubleshooting.zh-CN.md](../troubleshooting.zh-CN.md) — 排障
- [docs/teardown.zh-CN.md](../teardown.zh-CN.md) — 整体环境拆除
