---
title: "10 收尾与资源清理"
weight: 100
---

# 第 10 章 · 收尾：本次实验创建了什么、怎么清理

> **目标**：回顾实验流程，核对资源并按依赖顺序清理。
>
> **前置条件**：完成基础部分（第 03–06 章）或进阶部分（第 07–09 章）之一。本章可选：
> Workshop Studio 临时账号会随活动结束自动回收，不清理也不会持续计费。下面的清理清单里，
> 未做的部分对应的资源不存在，直接略过那几行即可。
>
> **预计耗时**：回顾 5 分钟；如执行完整清理另加 10 分钟。

---

## 10.1 流程回顾

```
第00章 账号          邮箱 OTP 登录 Workshop Studio → 加入活动
第01章 环境          预热的 bootstrap → 共享基础设施 + AgentCore 单例；ConsoleUrl 登录
第02章 环境（可选）  self-paced 自有账号与本地开发机

── Part 1 · 基础：AgentCore 基础 ──────────────────────
第03章 部署          托管 Harness → lab-quota-advisor
   ↓
第04章 挂能力        AgentCore 配额 Markdown → 托管知识库；注册并批准一份技能 →
                     两者一起挂到 Harness
   ↓
第05章 测试          对话验证：挂载 KB 后的检索作答；记忆分区与长期偏好抽取
   ↓
第06章 观测          仪表盘 / 会话还原 / trace 瀑布图：核对模型调用与检索

── Part 2 · 进阶：评估、优化与治理 ────────────────────
第07章 评估          Strands ZIP → lab-hr-assistant；复现越权缺陷 → 自建评估器 → 两次基线
   ↓
第08章 A/B           对基线会话做洞察 → AI 推荐 → 配置包 A/B 50/50 → 判定 → 晋级与清理
   ↓
第09章 治理          Gateway 纳管（只加标签）→ Cedar LOG_ONLY 策略 → 真实 ALLOW/DENY 决策证据
                     → 不可变审计
```

## 10.2 本次实验创建的资源清单

### Agent 与运行时

| 资源 | 标识 | 清理方式 |
|---|---|---|
| Agent `lab-quota-advisor`（harness） | `harness/lab_quota_advisor-<后缀>` | 同上 |
| Agent `lab-hr-assistant`（第 07 章创建） | 以 Agent 详情中的 Runtime ARN 为准 | 同上；只在完成第 07 章后存在 |
| S3 部署包 | `s3://launchpad-artifacts-<ACCT>-<REGION>/agents/lab-hr-assistant/deployment_package.zip` | 只在完成第 07 章后存在；S3 手动删，也可以留着 |

> 控制台的 `删除` 会一并删除 AWS 侧的 Runtime / Harness 资源与本地台账行。

### 知识库

| 资源 | 标识 | 清理方式 |
|---|---|---|
| 托管知识库 `lab-quota-kb` | `<KB_ID>` | 先从 `lab-quota-advisor` 取消挂载，再到 `04 知识库` 删除 |
| S3 数据源对象 | `s3://launchpad-artifacts-<ACCT>-<REGION>/kb/<KB_ID>/` | 随 KB 删除清理，S3 对象可手动确认 |
| `launchpad-kb-gw` 上的 KB target | 平台自动管理 | 取消挂载后由平台回收。网关本身是共享资源，**不要删** |

### Registry 记录

| 记录 | id | 状态 | 清理方式 |
|---|---|---|---|
| `lab-quota-answering`（技能） | `<SKILL_RECORD_ID>` | `APPROVED` | `03 注册中心` 详情 → `删除` |
| `lab-quota-advisor`（A2A） | `<ADVISOR_RECORD_ID>` | 以当前页面为准 | 同上 |
| `lab-hr-assistant`（A2A） | 以当前页面为准 | 以当前页面为准 | 只在完成第 07 章后存在；删 Agent 时一并处理 |

> **不要用「停用 / DEPRECATED」来清理**。它是终态，之后既不能恢复也不能改。要清就直接 `删除`。

### 评估相关

| 资源 | 标识 | 清理方式 |
|---|---|---|
| 本地数据集 `lab-hr-zh-ab` / `lab-hr-zh-controls`（第 07 章） | `<DATASET_ID>` | `?view=datasets` 选中 → `删除`；实验的 `CLEANUP` 不会删除数据集 |
| AWS 数据集快照（若做过第 07 章可选的`同步 AWS`） | `dataset/<名称>-<后缀>` | 云端副本不可编辑，只能删除；没同步过则这一行不存在 |
| 自定义评估器 `HrThirdPartyDisclosureZh`（第 07 章） | `HrThirdPartyDisclosureZh-<后缀>` | `?view=evaluators` 选中 → `删除`；实验的 `CLEANUP` 不会删除评估器 |
| 评估运行记录 | `run-<后缀>` | 本地台账记录，留着做对比即可 |
| 回放产生的 Runtime 会话与遥测（第 07 章两次基线 + 第 08 章可选回归；洞察复用基线会话，不产生新调用） | 以运行记录为准 | CloudWatch 日志按保留期自动过期 |
| A/B 流量产生的会话与遥测（第 08 章） | 28 个 session | 同上 |

### 实验（**必须清理**）

| 资源 | 标识 | 清理方式 |
|---|---|---|
| 配置包 control / treatment | `exp_<expid>_control-…` / `exp_<expid>_treatment-…` | 实验详情 → `清理` |
| 实验共享 Gateway + target | `launchpad-exp-gw-…` / `exp<expid>v1` | 同上。`清理` 只删 target，共享网关留给下一次实验 |
| A/B 测试 | `exp_<expid>_bundle-…` | 同上 |
| 在线评估配置 | `exp_<expid>_oe1-…` | 同上 |

> **注意**：实验的中间资源是唯一一类不清理会影响后续实验的资源。它们会占用共享实验网关
> 互斥锁，不清理会让下一次实验直接失败，在线评估配置也会持续存在。
> 清理动作本身有幂等的删除清单，`deleted` / `skipped` 逐项列出。

### 其它

| 资源 | 标识 | 清理方式 |
|---|---|---|
| Cedar 策略 `lab_readonly_tools`（仅跑过第 09 章才有） | `lab_readonly_tools-<后缀>` | `09 治理` → 策略列表 → 删除。它是 `LOG_ONLY`，留着也不拦流量 |
| `launchpad-kb-gw` 纳管标签（仅第 09 章） | 第 10.3 节验证后已移除 | — |
| `launchpad-gw` 纳管标签（仅第 09 章） | 第 10.3 节末尾加上，供 10.5 创建策略 | `09 治理` → 打开 `launchpad-gw` → `取消纳管`，只删标签，不影响已建策略 |
| 记忆事件与长期记录 | `launchpad_memory` 中 `<agent_id>__<USER_NAME>` 分区 | Memory 控制台是只读的，短期事件按资源的 30 天过期策略自动清除。要立刻清就删对应 Agent 分区，需自行调 AWS API |

## 10.3 本次实验的处理方式

Workshop Studio 临时账号会在活动结束后回收，活动进行期间可以保留 `lab-` 资源供后续演示。
实验的中间资源会占用共享网关互斥锁，完成第 08 章后必须立即清理。

Self-paced 使用自有账号，建议按以下顺序清理：

```text
1. 实验 → 实验记录的「清理」按钮
2. 若完成第 07 章，删除 lab-hr-assistant（会同时删除对应 A2A 记录）
3. 取消 lab-quota-advisor 上的 KB 挂载 → 重新发布 → 删除知识库
4. 删除基础 Agent：lab-quota-advisor
5. 删除剩余 Registry 记录（技能 + 两条基础 Agent 的 A2A）
6. 完成第 07 章时删除该章的三个 lab-hr-zh-* 数据集、HrThirdPartyDisclosureZh 评估器和云端快照（若同步过）
7. 删除 lab_readonly_tools 策略，再取消纳管 launchpad-gw（若跑过第 09 章）
```

## 10.4 共享基础设施：什么时候才动它

`make bootstrap` 建的东西是整个平台共享的，不属于本实验的清理范围：

`launchpad-artifacts-*` 桶、`launchpad-users` Cognito、`launchpad-agent-execution-role`、`launchpad-registry`、
`launchpad_memory`、`launchpad-gw`、`launchpad-kb-gw`、`launchpad_pe`。

只有在确定要拆掉整个环境时才执行：

```bash
cd backend
uv run python ../scripts/teardown.py --dry-run   # 先看会删什么
uv run python ../scripts/teardown.py --yes        # 真删（memory → registry → CDK 栈）
```

删除是尽力而为、依赖方优先；S3 桶会被自动清空。更细的说明见
[docs/teardown.zh-CN.md](https://github.com/aws-samples/sample-agentcore-launchpad/blob/main/docs/teardown.zh-CN.md)。

## 10.5 成本提示

完整实验的花费集中在三处，量级不大，但演示时仍应说明：

| 来源 | 参考量级 |
|---|---|
| 所选 Bedrock 模型调用（对话 + 评估回放 + 评审 + A/B 流量 + 优化器） | 两个 Agent 均使用同一模型；完整流程为数十次调用 |
| 知识库（向量库 + embedding + 检索） | 1 份约 38 KB 的 Markdown 文档索引与若干次检索 |
| CloudWatch Logs Insights（可观测页每次查询按扫描量计费） | 平台已用 60 秒 TTL 缓存降低查询次数 |

> 可观测页的成本是估算值，token × 本地价格表，界面标注 `≈ / EST`，不要拿它当账单。

> 本实验首选 `global.anthropic.claude-sonnet-5`，账号不可用时回退到
> `global.amazon.nova-2-lite-v1:0`。价格表在 `config/launchpad.yaml` 的 `model_prices` 中，
> 由 litellm 刷新；若界面显示 `≈ —`，只按 token 数讲调用量，不推算账单。

## 10.6 继续往下走

- 想改这套实验做自己的 demo：替换第 04 章的知识文档，或重写第 07 章的数据集语料和评估规则。
- 想深入某个模块：[docs/architecture.zh-CN.md](https://github.com/aws-samples/sample-agentcore-launchpad/blob/main/docs/architecture.zh-CN.md) 是权威的
  「控制台功能 ↔ AgentCore 服务」映射表。
- 遇到问题：[docs/troubleshooting.zh-CN.md](https://github.com/aws-samples/sample-agentcore-launchpad/blob/main/docs/troubleshooting.zh-CN.md)。

---

## 本章验证清单

- [ ] 基础流程有 `lab-quota-advisor`；完成第 07 章后还应看到 `lab-hr-assistant`
- [ ] 第 08 章创建的实验中间资源已经清理
- [ ] Self-paced 环境先删除转换 Runtime，再解除 Harness 的 KB 挂载，随后删除 KB、基础 Agent、Registry 与评估资源
- [ ] 完成第 07 章时，三个 `lab-hr-zh-*` 数据集与 `HrThirdPartyDisclosureZh` 评估器也已删除
- [ ] 没有误删 `launchpad-*` 共享基础设施
- [ ] 已说明可观测页成本是估算值，实际账单以 AWS 账单为准

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| Workshop Studio 活动里还看得到 `lab-` 资源 | 活动进行期间账号不会自动清空 | 可以保留供后续演示；活动结束后临时账号会回收 |
| 知识库删除失败 | Harness 仍引用该 KB | 编辑 `lab-quota-advisor` 取消知识库并重新发布，再删除 KB |
| 新实验提示共享网关被占用 | 上一次实验的中间资源没有清理 | 回到对应记录执行 `清理`，确认逐项显示 `deleted` 或预期的 `skipped` |

---

上一章：[第 09 章 · 治理](../09-governance) ｜ 返回：[实验总目录](..)
