# 第 03 章 · 另外两种创建方式：托管 Harness 与其他 Agent SDK 容器

> **目标**：体验另外两条创建路径：免构建的**托管 Harness**（方式B）与经 CodeBuild
> 打包的 **其他 Agent SDK 容器**（方式A，目前唯一的 SDK 是 Claude Agent SDK），
> 并理解两者如何复用同一条五阶段流水线。
>
> **前置条件**：完成[第 02 章](02-deploy-runtime.md)。容器方式需要账号里有
> `launchpad-agent-builder` CodeBuild 项目（`make bootstrap` 已创建）。
>
> **本章将创建的 AWS 资源**：1 个 AgentCore Harness、1 个 AgentCore Runtime（容器）、
> 1 个 ECR 镜像 tag、1 次 CodeBuild 构建、2 条 Registry A2A 记录。

---

## 3.1 方式B：托管 Harness（`lab-fund-advisor`）

Harness 是**声明式**的：你给出模型、提示词、工具、技能、知识库、记忆开关，AgentCore 直接托管
运行，没有任何构建产物。本实验用它承载「基金资料问答」；第 04 章会通过专用知识库网关给它挂上
托管知识库。其他 Agent SDK 容器与 Strands ZIP 也能挂知识库，但走的是直接注入检索工具的通道。

1. **打开** `02 Agent 管理`，选择第一张卡片 **托管 Harness**，点 **下一步 ▸**。
2. **填写**：

   | 字段 | 取值 |
   |---|---|
   | AGENT 名称 | `lab-fund-advisor` |
   | 模型来源 | 点 `Bedrock`（默认是 `Bedrock Mantle`，理由同[第 02 章 2.2](02-deploy-runtime.md#22-配置-agent)） |
   | 模型 | `Claude Sonnet 5 (global) · global.anthropic.claude-sonnet-5` |
   | 系统提示词 | `你是摩根士丹利新兴市场领先企业股票基金（MS INVF Emerging Leaders Equity Fund）的产品知识助手。只依据挂载的基金资料回答问题；资料中没有的内容，明确说明无法确认，不要猜测数字。` |
   | 工具 / 技能 / 知识库 | **本章都不选**，第 04 章再挂载 |
   | 记忆 | 短期 + 长期都开启 |

![Harness 配置页](images/03-harness-config.png)
*图 3-1：Harness 配置页。`模型来源` 已按上表切到 `Bedrock`（Converse API），模型下拉随之
换成 Bedrock 侧的目录。工具区直接列出账号里可挂载的 gateway 工具（`office-facts`、
`hr-database`）与 MCP 服务器（`aws-knowledge`、`deepwiki`），技能区列出 Registry 里已审批的
技能，知识库区列出 ACTIVE 的托管 KB。*

> **注意**：选择一个 gateway 条目会挂载整个 AgentCore Gateway 及其全部工具。
> Registry 审批只决定「目录可见性」，不是按工具的授权边界；按动作授权要用第 11 章的 Cedar 策略。

3. **点击** `▲ 启动 AGENT`。

![Harness 部署完成](images/03-harness-deploy.png)
*图 3-2：Harness 部署完成。注意 `打包` 阶段显示 `skipped · harness — no build required`。*

> 图中展示的是重新发布后的状态，所以 `deploy` 行为 `UpdateHarness`，下方还显示第 04 章
> 挂载的 `lab-fund-kb`。你在本章首次创建时会看到 `CreateHarness`，且没有知识库。

首次创建时，事件流应包含以下内容：

```json
{"stage":"generate","msg":"harness request generated for lab_fund_advisor · model global.anthropic.claude-sonnet-5"}
{"stage":"package","msg":"skipped · harness — no build required"}
{"stage":"provision","msg":"reusing shared execution role arn:aws:iam::…:role/launchpad-agent-execution-role"}
{"stage":"deploy","msg":"CreateHarness accepted · harnessId <HARNESS_ID>"}
{"stage":"deploy","msg":"harness READY · arn:aws:bedrock-agentcore:us-west-2:<ACCT>:harness/<HARNESS_ID>"}
{"stage":"register","msg":"a2a record created · <RECORD_ID> · auto-submitted"}
```

在 Agent 列表或 API 返回中确认以下字段：

```json
{
  "id": "<ADVISOR_ID>",
  "name": "lab-fund-advisor",
  "method": "harness",
  "status": "active",
  "arn": "arn:aws:bedrock-agentcore:us-west-2:<ACCT>:harness/<HARNESS_ID>",
  "registry_record_id": "<RECORD_ID>"
}
```

> **记录**：记下这个 id，后面记作 `<ADVISOR_ID>`。

**Harness 的两个约束**（第 08–10 章会用到）：

- Harness 背后确实有一个 Runtime（名字形如 `harness_lab_fund_advisor`），但它是**被托管的**：
  直接 `InvokeAgentRuntime` 会报 `ValidationException … managed by a harness`，必须走
  `InvokeHarness`。
- 它的日志组只在**第一次被调用之后**才存在。所以第 08 章评估它之前，必须先在第 05 章跟它聊过一次，
  否则评估会报 `eval.harness_no_telemetry`。

## 3.2 方式A：其他 Agent SDK · 容器（`lab-fund-packager`）

方式A 是控制台里的 **其他 Agent SDK** 入口：它是一个**类别**，具体用哪个 SDK 由配置页上的
二级选项 `AGENT SDK` 决定（对应 `AgentSpec.agent_sdk`）。目前该类别只有一个成员
**Claude Agent SDK**，所以本节跑出来的就是一个完整的 Claude Agent SDK 应用（支持子 Agent、
Hooks、MCP 服务器），经 CodeBuild 打成 **ARM64** 镜像推到 ECR，再创建 Runtime。
它是唯一能把 **Registry 技能物理打进镜像**（`.claude/skills/`）的方式。

1. **打开** `02 Agent 管理`，选择第三张卡片 **其他 Agent SDK**，点 **下一步 ▸**。
2. **填写**：

   | 字段 | 取值 |
   |---|---|
   | AGENT 名称 | `lab-fund-packager` |
   | AGENT SDK | `Claude Agent SDK`（这个类别目前只有它，默认已选中） |
   | 模型 | 保持默认的 Claude 模型（这条路径固定走 Bedrock Converse，没有 `模型来源` 选择器） |
   | 系统提示词 | `你是基金材料分析助手，负责把基金产品文档整理成结构化摘要，可调用子 Agent 与技能完成多步任务。` |
   | 技能 | 勾选**任意一个已发布（APPROVED）的技能**。第 04 章会登记一个新技能，这里只验证技能会被物理打进镜像 |
   | 文件系统 | 保持 `托管会话存储 ✓`，挂载路径 `/mnt/workspace` |

![容器配置页](images/03-container-config.png)
*图 3-3：容器配置页。顶部的 `AGENT SDK` 就是这个类别的二级选项，目前只有
`Claude Agent SDK` 一个成员且已默认选中；正因为它只能驱动 Claude 模型，这一页没有
`模型来源` 选择器，模型下拉也只列 Claude。除了技能，这里还能填自定义 MCP 服务器 JSON、
挂载 S3 Files 或 EFS。「托管会话存储」让同一会话在停止/恢复之间保留 `/mnt` 文件
（闲置 14 天过期）。*

3. **点击** `▲ 启动 AGENT`，然后观察 `打包` 阶段的构建过程。

![容器构建中](images/03-container-build.png)
*图 3-4：`打包` 阶段逐步打印 CodeBuild 的 phase：`QUEUED → PRE_BUILD → BUILD → POST_BUILD
→ COMPLETED`。生成阶段的日志显示所选技能已被打进 `.claude/skills/<SKILL_NAME>/SKILL.md`。*

![容器部署完成](images/03-container-done.png)
*图 3-5：容器方式五阶段完成，镜像 tag 为 `launchpad-agents:lab-fund-packager-v1`。*

事件流应体现构建上下文、CodeBuild 阶段、ECR 镜像和 Runtime 创建：

```json
{"stage":"generate","msg":"skills bundled: <SKILL_NAME> (<FILE_COUNT> files, <SIZE>)"}
{"stage":"generate","msg":"build context assembled: .claude/skills/<SKILL_NAME>/SKILL.md, Dockerfile, README.md, buildspec.yml, main.py, requirements.txt, tracing.py"}
{"stage":"package","msg":"source zip uploaded → s3://launchpad-artifacts-…/builds/lab-fund-packager/source.zip"}
{"stage":"package","msg":"codebuild started · launchpad-agent-builder:<BUILD_ID>"}
{"stage":"package","msg":"codebuild phase: QUEUED → PRE_BUILD → BUILD → POST_BUILD → COMPLETED"}
{"stage":"package","msg":"image pushed · …dkr.ecr.us-west-2.amazonaws.com/launchpad-agents:lab-fund-packager-v1"}
{"stage":"package","msg":"codebuild · arm64 · <DURATION> → :lab-fund-packager-v1"}
{"stage":"deploy","msg":"CreateAgentRuntime accepted · runtimeId <RUNTIME_ID>"}
{"stage":"register","msg":"a2a record created · <RECORD_ID> · auto-submitted"}
```

> `tracing.py` 出现在构建上下文里，是因为 Claude SDK 容器把 `claude` CLI 当子进程跑，
> ADOT 自动埋点看不见它，所以生成的 Agent **手工发射** gen_ai 遥测（`invoke_agent` 根 span、
> 每次工具调用一个 `execute_tool`、一个聚合 `chat` span 带 token 用量）。第 07 章因此也能
> 在追踪瀑布图中显示容器 Agent。

> **注意**：容器方式部署完成后，务必手工调一次再往下走。五阶段全绿只说明镜像已推送、
> Runtime 到了 `READY`，**不代表容器进程能起来**。平台目前没有部署后探活。验证方法见
> [第 05 章末](05-chat-memory.md#容器-agent-部署后的探活)。

## 3.3 三种方式对照

列序与 `/create` 页上的卡片顺序一致：

| | 方式B 托管 Harness | 方式C Strands ZIP | 方式A 其他 Agent SDK · 容器 |
|---|---|---|---|
| 本实验 Agent | `lab-fund-advisor` | `lab-fund-assistant` | `lab-fund-packager` |
| `generate` | 组装 `CreateHarness` 请求 | 渲染 Strands 模板 | 组装 ARM64 构建上下文 |
| `package` | **跳过**（无产物） | pip(arm64) → zip → S3 | zip → S3 → CodeBuild → ECR |
| `provision` | 复用共享执行角色 | 复用共享执行角色 | 复用共享执行角色 |
| `deploy` | `CreateHarness` | `CreateAgentRuntime` | `CreateAgentRuntime(containerConfiguration)` |
| `register` | A2A 记录，自动提交 | A2A 记录，自动提交 | A2A 记录，自动提交 |
| 落在哪 | Harness 服务 | AgentCore Runtime | AgentCore Runtime |
| 挂知识库 | 支持：网关 `Retrieve` + `AgenticRetrieveStream` | 支持：`kb_search` + `kb_deep_search` | 支持：`kb_search` + `kb_deep_search` |
| 技能进镜像 | 声明式挂载 | 不支持 | 支持，位于 `.claude/skills/` |
| 配置包 A/B | 不支持 | 支持 | 不支持 |
| 金丝雀 | 不支持 | 支持 | 暂不支持 |
| `模型来源` | 有（默认 Mantle） | 有（A2A 子模式固定 Bedrock） | 无，由 `AGENT SDK` 决定，固定 Claude |

三种方式都支持单次检索和多步 agentic 检索，只是接入路径不同。Harness 通过
`launchpad-kb-gw` 调用逐库 `Retrieve` 与跨库 `AgenticRetrieveStream`；其他 Agent SDK 容器和
Strands ZIP 则在生成产物中注入 `kb_search` 与 `kb_deep_search`，后者会调用
`AgenticRetrieveStream` 拆分子查询并执行多轮检索。具体挂载与验证步骤见
[第 04 章 4.7](04-capabilities.md)。

**本章结束时，三个 Agent 应该都是 `运行中`**：

```bash
curl -s http://127.0.0.1:8000/api/agents | python3 -c "
import sys,json
for a in json.load(sys.stdin)['agents']:
  if a['name'].startswith('lab-'): print(a['id'],a['name'],a['method'],a['status'])"
```

```text
<PACKAGER_ID>  lab-fund-packager   container    active
<ADVISOR_ID>   lab-fund-advisor    harness      active
<ASSISTANT_ID> lab-fund-assistant  zip_runtime  active
```

---

## 本章验证清单

- [ ] `lab-fund-advisor`（harness）状态 active，ARN 里是 `:harness/`
- [ ] Harness 的 `打包` 阶段显示 `skipped`
- [ ] `lab-fund-packager`（container）状态 active，ARN 里是 `:runtime/`
- [ ] 容器日志出现 `image pushed · …/launchpad-agents:lab-fund-packager-v1`
- [ ] 容器生成日志出现 `skills bundled: <你勾选的技能名>`
- [ ] 三个 Agent 各自都有 `registry_record_id`

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 容器 `打包` 阶段停在 `QUEUED` 很久 | CodeBuild 并发排队 | 继续观察日志；进入 `PRE_BUILD` 后说明已开始执行 |
| 容器构建失败在 `BUILD` | Dockerfile 依赖拉取失败或 ECR 权限 | 去 CodeBuild 控制台看 `launchpad-agent-builder` 的完整日志 |
| Harness 创建报模型不可用，或创建成功但**首次调用**报 `404 … does not exist` | 该模型未在账号/区域开通；或在 us-west-2 选了只在 us-east-1 提供的 Mantle 模型（`openai.gpt-5.6-sol`、`openai.gpt-5.5`）。Harness 只能在自己所在区域解析 Mantle 模型 | 在当前 `模型来源` 的模型下拉里换一个本区域可用的模型；下拉里没有的 id 走 `Custom model ID…` 手填 |
| Harness 评估报 `eval.harness_no_telemetry` | 还没被调用过，日志组不存在 | 先完成第 05 章的对话，再回来评估 |
| 想把 Harness 变成可做 A/B 的 Runtime | 列表行有「转换 ⇄ RT」 | 转换会导出代码并植入 config-bundle graft，转换后就能做 A/B（本实验不走这条路） |
| 容器部署成功但**调用**报 `RuntimeClientError` | 容器进程未正常启动，旧检出还可能包含已修复的 OTEL 依赖问题 | 先查 Runtime 日志；旧检出请更新代码后重新发布，说明见[第 05 章末](05-chat-memory.md#容器-agent-部署后的探活) |

---

上一章：[第 02 章 · 部署第一个 Agent](02-deploy-runtime.md) ｜
下一章：[第 04 章 · 挂载能力：Registry 资产与知识库](04-capabilities.md)
