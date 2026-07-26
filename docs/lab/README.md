# AgentCore Launchpad 动手实验指南

> 用一个真实业务场景，把 Amazon Bedrock AgentCore 的**创建 → 部署 → 测试 → 观测 → 评估 →
> 优化 → A/B → 治理**整条链路亲手跑一遍。
>
> 全程对接**你自己 AWS 账号里的真实资源**（`us-west-2`），没有 mock。

---

## 这个实验在做什么

**业务场景**：为摩根士丹利新兴市场领先企业股票基金（MS INVF Emerging Leaders Equity Fund）
搭一个「基金产品智能助手」，服务销售与客服团队。素材是一份真实的产品路演材料
[`assets/Morgan_Stanley_Oct_21_(EMEA).pdf`](assets/Morgan_Stanley_Oct_21_%28EMEA%29.pdf)。

**你会亲手建这几个东西**：

| Agent | 创建方式 | 承担什么 |
|---|---|---|
| `lab-fund-assistant` | 方式C · Strands ZIP 通道 | 主线：对话 / 公共 API / 可观测 / 评估 / A/B / 金丝雀 |
| `lab-fund-advisor` | 方式B · 托管 Harness | 挂知识库与技能，做有依据的文档问答 |
| `lab-fund-packager` | 方式A · Claude SDK 容器 | 演示 CodeBuild → ECR → Runtime 路径 |

**贯穿全程的一条主线**：第 05 章你会亲眼看到没有知识库的 Agent 把持仓数编成「20–35 只」
（真实是 25–40 目标 / 28 实际）；第 08 章用带真值的数据集把它量化成分数（接地度只有 0.60）；
第 09 章优化器读 trace **独立得出同一个结论**并给出改进提示词，做成 A/B；第 10 章用金丝雀把
改进版本灰度到真实流量上。**这就是这套平台的意义：每一步都有证据。**

## 章节目录

| # | 章节 | 内容 | 本次实测耗时 |
|---|---|---|---|
| 01 | [环境准备与控制台导览](01-environment.md) | 前置条件、`make bootstrap`、启动本地栈、11 个模块导览 | 首次 15–20 分钟 |
| 02 | [部署第一个 Agent（ZIP 通道）](02-deploy-runtime.md) | 统一五阶段流水线、方式能力矩阵、异步可恢复部署 | 部署 69 秒 |
| 03 | [Harness 与容器方式](03-deploy-harness.md) | 免构建 Harness、CodeBuild 容器、三种方式实测对照 | 18 秒 / 125 秒 |
| 04 | [挂载能力：Registry 与知识库](04-capabilities.md) | PDF → 托管 KB、技能登记与审批、Harness 重新发布 | 约 15 分钟 |
| 05 | [对话测试与记忆](05-chat-memory.md) | 有/无知识库对照、会话记忆、Memory 控制台四视图 | 约 15 分钟 |
| 06 | [公共 `/v1` API](06-public-api.md) 🔀 **可选** | API Key、同步与 SSE 流式、鉴权失败、等价 curl | 约 10 分钟 |
| 07 | [可观测性](07-observability.md) | 仪表盘、会话还原、trace 瀑布图、token 与成本估算 | 约 15 分钟 |
| 08 | [评估](08-evaluation.md) | 带真值数据集、自定义 LLM 评审、批量评估四阶段 | 评估 4 分 38 秒 |
| 09 | [配置包 A/B 实验](09-experiment-ab.md) | 推荐 → 配置包 → 网关 → 50/50 → 判定 → 清理 | 约 30 分钟（判定占 15 分钟） |
| 10 | [Runtime 金丝雀](10-canary.md) | 候选版本铸造、真实流量分档放量、每档证据门禁 | setup 1 分钟 + 判定 15 分钟 |
| 11 | [治理](11-governance.md) | Gateway 纳管标签、Cedar LOG_ONLY 策略、决策与审计 | 约 25 分钟 |
| 12 | [收尾与资源清理](12-wrapup-cleanup.md) | 资源清单、清理顺序、成本提示 | 5–15 分钟 |

**完整跑一遍约 3 小时**（其中相当一部分是等待：容器构建、评估排队、A/B 与金丝雀判定各需 10–15 分钟）。

标 🔀 **可选** 的章节是支线：讲的是怎么把 Agent 接进外部系统，跳过它不影响后续任何章节——
第 07 章起用到的 trace、数据集、实验对象全部来自第 02–05 章。

### 想快速看核心链路？

时间有限时的最短路径：**01 → 02 → 04 → 05 → 07 → 08**（约 1.5 小时），
这条路已经覆盖「创建 → 部署 → 接地 → 观测 → 量化」的完整闭环。
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

本次实跑的量级：Bedrock 模型调用几十次（对话 + 评估回放 + LLM 评审 + A/B 流量 + 优化器），
一份 ~1MB PDF 的知识库索引与若干次检索，以及若干次 CloudWatch Logs Insights 查询
（按扫描量计费，平台已用 60 秒 TTL 缓存降低频次）。可观测页显示的估算成本合计在 **$0.2 量级**。
详见[第 12 章 · 成本提示](12-wrapup-cleanup.md#125-成本提示)。

### 命名约定

- 实验创建的资源统一用 `lab-` 前缀，方便和你环境里已有的资源区分。
- 指南里 `<ASSISTANT_ID>` / `<ADVISOR_ID>` / `<KB_ID>` / `<ACCT>` 这类尖括号占位符需要替换成
  你自己的值；代码块里出现的具体 id（如 `600f1e6695e6…`）是**本次实跑的真实值**，仅供对照。

### 关于本指南里的数据

所有截图、日志、分数、耗时都来自 **2026-07-26 的一次真实完整实跑**，不是示意图。
个别未实跑的步骤会显式标注「本次未实跑」并说明原因，共三处：

| 未实跑的部分 | 原因 |
|---|---|
| 第 01 章 `make bootstrap` | 实验环境早已完成引导；命令与产物清单按 `docs/setup.zh-CN.md` 与现存 `config/launchpad.yaml` 校对 |
| 第 10 章 50/50 与 1/99 两档 | 90/10 档判定为 `insufficient-data`，**门禁按设计阻断放量**——这本身就是该章要演示的机制 |
| 第 11 章 Registry 导入 Gateway 记录 | 只跑了只读 `预览`；不执行导入以免在共享演示账号里留下不可恢复的目录改动（`DEPRECATED` 是终态） |

另外本次实跑真实遇到一个产品缺陷：**方式A 容器部署成功但调用失败**（依赖漂移导致容器启动即崩）。
该缺陷**已于同日修复并真机复验**（重新发布 → Runtime v2 → 调用 5.5 秒正常返回 → 遥测正常落地），
所以你按本指南跑不会再遇到。现象与修复见[第 05 章末](05-chat-memory.md#关于容器-agent-调用失败本次实测)，
完整根因与验证记录见
[docs/issues/2026-07-26-container-otel-events-import.md](../issues/2026-07-26-container-otel-events-import.md)。
指南按实际情况如实记录，未做粉饰。

顺带留下一条仍未关闭的已知问题：**部署全绿不代表容器能起来**——平台没有部署后探活调用，
所以新部署容器 Agent 后请手工调一次再交付。

## 清理

跑完后**唯一必须清理**的是实验与金丝雀的中间资源（它们会占用共享网关互斥锁）：
见第 09 章的 `CLEANUP` 与第 10 章的 `清理`。其余资源的完整清理清单见
[第 12 章](12-wrapup-cleanup.md)。

## 相关文档

- [docs/architecture.zh-CN.md](../architecture.zh-CN.md) — 控制台功能 ↔ AgentCore 服务的权威映射
- [docs/setup.zh-CN.md](../setup.zh-CN.md) — 环境搭建细节
- [docs/api.zh-CN.md](../api.zh-CN.md) — API 参考
- [docs/troubleshooting.zh-CN.md](../troubleshooting.zh-CN.md) — 排障
- [docs/teardown.zh-CN.md](../teardown.zh-CN.md) — 整体环境拆除
