---
title: "03 部署托管 Harness"
weight: 30
---

# 第 03 章 · 部署托管 Harness

> **目标**：通过托管 Harness 创建 `lab-quota-advisor`，并理解托管 Harness 与 Strands ZIP
> 两条创建路径的能力边界。
>
> **前置条件**：任选一种环境路径完成准备，并确认 bootstrap 预建的 5 项服务已经就绪。
> Workshop Studio 主线见[第 01 章](../01-environment)，自有账号路径见
> [可选第 02 章](../02-own-account-local)。
>
> **预计耗时**：约 5 分钟。Harness 无需打包，部署通常在数十秒内完成。
>
> **本章将创建的 AWS 资源**：1 个 AgentCore Harness、1 条 AgentCore Registry A2A 记录。

---

## 3.1 配置 `lab-quota-advisor`

Harness 由 AgentCore 托管运行，不需要构建部署包。本实验先创建一个未挂载知识库的 Harness，
第 04 章再为它添加 AgentCore 配额资料和回答规范技能。

控制台提供四条创建路径，本实验用到两条：本章的托管 Harness，以及第 07 章的 Strands ZIP
快速通道。两者的区别决定了后面哪个 Agent 能参加哪种实验。

| 创建方式 | 本实验 Agent | 特点 | 后续用途 |
|---|---|---|---|
| 托管 Harness | `lab-quota-advisor` | 声明式，无需构建部署包，可挂载知识库与技能 | 第 04–06 章的主线对象 |
| Strands ZIP 快速通道 | `lab-hr-assistant`（第 07 章创建） | 生成代码并打包部署，内置 config-bundle 契约与 ADOT 埋点 | 第 07 章评估、第 08 章配置包 A/B |

托管 Harness 的后端 Runtime 被锁定为直调，不消费 config bundle，因此不能直接参加配置包实验。
第 07 章因此另建一个 ZIP Runtime，评估与 A/B 都用它。

1. 打开 `02 Agent 管理`，选择 **托管 Harness**，点 **下一步 ▸**。
2. 按下表填写：

   | 字段 | 取值 |
   |---|---|
   | AGENT 名称 | `lab-quota-advisor` |
   | 模型来源 | **`Bedrock`**。页面默认是 `Bedrock Mantle`，必须手动修改 |
   | 模型 | `Nova 2 Lite (global) · global.amazon.nova-2-lite-v1:0` |
   | 系统提示词 | `你是平台工程团队的 AgentCore 配额与容量规划顾问。回答具体配额时先检索挂载资料，准确说明服务、配额名称、数值、单位、区域范围和是否可调整；资料中没有的内容明确说明无法确认，不要猜测。` |
   | 工具 / 技能 / 知识库 | 本章都不选，第 04 章再挂载 |
   | 记忆 | 短期与长期都保持开启 |

![Harness 配置页](/static/images/03-harness-config.png)
*图 3-1：Harness 配置页。模型来源为 `Bedrock`，工具、技能和知识库暂时留空。*

全新活动账号中，技能区和知识库区没有可选条目属于正常现象。第 04 章创建知识库并批准技能后，
编辑这个 Agent 即可看到它们。

> **不要沿用默认值。** 保留 `Bedrock Mantle` 时，Harness 可能创建成功并进入 `READY`，
> 但首次调用仍会因模型访问权限失败。遇到这种情况，编辑 Agent，改成 `Bedrock` 和本章指定模型
> 后重新发布。

## 3.2 部署并读取 Harness 日志

点 `▲ 启动 AGENT`。Harness 仍经过统一的五阶段流水线，但 `打包` 阶段会跳过。

![Harness 部署完成](/static/images/03-harness-deploy.png)
*图 3-2：Harness 部署完成，`打包` 显示
`skipped · harness — no build required`。请以自己的任务日志为准。*

在自己的任务日志中核对以下关键行。尖括号中的 Harness ID、ARN 和 Registry 记录 ID
应使用当前任务的实际值：

```text
generate  harness request generated for lab_quota_advisor · model <SELECTED_MODEL_ID>
package   skipped · harness — no build required
provision reusing shared execution role · launchpad-agent-execution-role
deploy    CreateHarness accepted · harnessId <HARNESS_ID>
deploy    harness READY · <HARNESS_ARN>
register  a2a record created · <REGISTRY_RECORD_ID> · auto-submitted
```

用 `generate` 行核对模型，确认 `package` 行显示 `skipped`，并等待 `deploy` 行变为
`READY`。记录 Harness ID 和 Registry 记录 ID。Harness ARN 使用 `:harness/`，不是
`:runtime/`；背后的运行环境由服务托管，调用时走 `InvokeHarness`。

**预期结果**：五个节点全绿，「现有 AGENT」中的 `lab-quota-advisor` 状态变为 `运行中`。

Harness 还有一项与遥测有关的行为：日志组在首次调用后才出现。第 05 章完成第一次对话后，
第 06 章的可观测页面才有它的 trace 可看。

## 3.3 核对部署结果

回到「现有 AGENT」表格，确认能看到这一行：

| 名称 | 方式 | 状态 | 版本 |
|---|---|---|---|
| `lab-quota-advisor` | `HARNESS` | `运行中` | `1` |

第 04 章会重新发布 `lab-quota-advisor`，届时它会升到版本 `2`。

两条创建路径的差别在部署阶段就能看出来：Harness 的 `打包` 阶段显示 `skipped`，
`deploy` 调用 `CreateHarness`；ZIP 快速通道要安装依赖、生成部署包，`deploy` 调用
`CreateAgentRuntime`。第 07 章创建 ZIP Runtime 时可以对照这两组阶段日志。

---

## 本章验证清单

- [ ] `lab-quota-advisor` 的模型来源为 `Bedrock`，模型为 Nova 2 Lite
- [ ] Harness 的 `打包` 阶段显示 `skipped`
- [ ] 任务日志给出以 `lab_quota_advisor-` 开头的 Harness ID，且已记录当前活动的实际值
- [ ] `register` 阶段给出 Registry 记录 ID，且已记录当前活动的实际值
- [ ] 「现有 AGENT」中能看到 `lab-quota-advisor`，状态 `运行中`、版本 `1`

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| Harness 配置页找不到 Nova 2 Lite | 模型来源仍是默认的 `Bedrock Mantle` | 改成 `Bedrock`，再选择 Nova 2 Lite |
| 回退模型也不可用 | 活动账号没有可用的实验模型 | 停止创建并联系讲师更换 team |
| 可观测页面看不到 Harness 的 trace | Agent 还没有被调用，日志组尚未创建 | 先完成第 05 章的 Harness 对话 |
| 想把 Harness 用于评估或配置包 A/B | Harness 批量评估当前受上游平台问题影响；其后端 Runtime 也锁定直调、不读配置包 | 第 07 章另建一个 ZIP Runtime，评估与第 08 章的 A/B 都用它 |

---

上一章：[第 01 章 · 环境准备与控制台导览](../01-environment) ｜
下一章：[第 04 章 · 挂载能力：Registry 资产与知识库](../04-capabilities)
