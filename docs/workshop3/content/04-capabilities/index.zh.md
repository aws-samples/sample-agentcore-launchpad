---
title: "04 挂载 Registry 与知识库"
weight: 40
---

# 第 04 章 · 挂载能力：Registry 资产与托管知识库

> **目标**：把 AgentCore 配额 Markdown 文档建成托管知识库，注册并批准一份 Registry 技能，
> 再将两者挂到 `lab-quota-advisor`。
>
> **前置条件**：完成[第 03 章](../03-deploy-harness)，`lab-quota-advisor` 状态为 `运行中`。
> 下载并准备好 :link[AgentCore 配额文档]{href=":assetUrl{path="/assets/bedrock-agentcore-limits.md" source=repo}" action=download}。
> 总览页「服务健康」里 Registry 应显示 `就绪`；若显示「等待引导初始化」或「注册中心不可用」，
> 见本章末尾常见问题的第一行。
>
> **预计耗时**：约 15 分钟。KB 创建和 ingestion 耗时会随服务状态变化。
>
> **本章将创建的 AWS 资源**：1 个 Bedrock 托管知识库（含向量库）、
> 1 个 S3 数据源 + 上传的 Markdown 文档、1 条 AgentCore Registry AGENT_SKILLS 记录、
> `launchpad-kb-gw` 上的 KB 连接器 target。

---

## 4.1 创建托管知识库并摄取 Markdown 文档

1. 打开 `04 知识库`。页面列出账号里已有的托管 KB 及其状态、数据源数、被几个 Agent 挂载。

![知识库列表](/static/images/04-kb-list.png)
*图 4-1：托管知识库列表。图是本章建好之后的样子，刚进来时是空列表加一段说明。
向量库、embedding 与重排都由 Bedrock 托管，不用自建基础设施。*

2. 点 `+ 创建知识库`，填写：

   | 字段 | 取值 |
   |---|---|
   | 名称 | `lab-quota-kb` |
   | 描述 | `Amazon Bedrock AgentCore 服务配额资料，涵盖 Runtime、Memory、Identity、Gateway、Browser、Code Interpreter、Evaluation、A/B Testing、Policy 与 Registry。回答具体配额值、单位、区域差异、可调整性或容量边界时查询。` |
   | 数据源 | `上传文件` |
   | 文件 | `bedrock-agentcore-limits.md` |

   > **注意**：描述最多 200 个字符，也会影响 Agent 何时查询这个知识库。写清楚资料范围和
   > 适用问题，便于 Agent 选择正确的检索工具。

![创建知识库](/static/images/04-kb-create.png)
*图 4-2：创建表单。右侧「工作流程」说明了四步：创建（2–5 分钟）→ ingestion 自动运行 →
用检索 Playground 验证 → 在创建 Agent 向导里挂载到 harness。v1 只支持 S3 数据源。*

3. 点 `▲ 创建`。

**预期结果**：KB 进入 `CREATING`，几分钟后变 `ACTIVE`。文件被上传到制品桶
`s3://launchpad-artifacts-<ACCT>-<AWS_REGION>/kb/<KB_ID>/`，数据源可用后自动触发 ingestion。

知识库列表页会显示状态与数据源数量，如 `● 可用` / 数据源 `1`。

> **记录**：`<KB_ID>` 在 KB 详情页的「概览」区，字段名就叫 `KB ID`。
> 同一行下面的 ARN 也带这个值。

## 4.2 确认 ingestion 与文档索引状态

点列表行打开 KB 详情页。这一页有四块：概览、关联 Agent、数据源（含 ingestion 任务统计）、
检索 Playground。

![知识库详情](/static/images/04-kb-detail.png)
*图 4-3：KB 详情示例。ingestion 完成后，页面会列出扫描、新增索引和失败文档数；
数据源名称由制品桶名和 KB ID 拼成。具体 ID 和计数以当前页面为准。*

点数据源那行的 `▤ 文档 ▸` 可以展开逐文档的索引状态：

![文档级索引状态](/static/images/04-kb-documents.png)
*图 4-4：逐文档视图会列出文件名、大小、上传时间、索引状态和完成时间。
这一层能区分「文档没上传」和「上传了但没索引成功」。*

> KB 创建与 ingestion 都是异步的，控制台不会替你阻塞等待。不要在 `CREATING` 状态挂载，
> 挂载列表只显示 `ACTIVE` 的 KB。
>
> 数据源由后端异步创建。如果 KB 已是 `ACTIVE` 却没有数据源，点详情页的 `补建数据源`，
> 这个操作可以安全重试。

## 4.3 用检索 Playground 验证质量（挂载前必做）

在详情页底部「检索 PLAYGROUND」输入一个答案确定在文档里的问题，例如：

```
AgentCore Runtime 在 us-east-1、us-west-2 和其它区域的活跃会话工作负载默认配额分别是多少？是否可调整？
```

点 `检索`。

![检索 Playground](/static/images/04-kb-playground.png)
*图 4-5：检索结果。每个 chunk 都带 `score` 与来源元数据，例如 `_document_title`、
`_chunk_id`、`_source_uri` 和 `_data_source_id`。具体字段以当前文档类型和页面为准。*

相关 chunk 应包含以下事实：

```
Active session workloads per account
5,000 in US East (N. Virginia) and US West (Oregon), and 2,500 in other AWS Regions.
Adjustable: Yes
```

**预期结果**：结果包含上述区域差异和可调整性。记录自己页面中相关 chunk 的 `score`、标题和
来源 URI；截图分数只用于说明界面，不是验收值。如果检索结果无关，先检查上传文件和知识库描述，
不要继续挂载。

## 4.4 注册一份技能到 AgentCore Registry

Registry 编目 A2A Agent、MCP 工具和 AGENT_SKILLS 技能。只有 `APPROVED` 记录能挂到 Agent 上。

1. 打开 `03 注册中心`。顶部三个计数按钮既是统计也是类型筛选器：
   `AGENT · A2A`、`MCP 工具`、`技能`。

![注册中心](/static/images/04-registry-list.png)
*图 4-6：注册中心。左上角图例 `○ 草稿 → ◍ 已提交 → ● 已发布` 就是记录状态机。
第 02/03 章部署的两个 Agent 已自动登记为 A2A 记录。*

> 部署日志里写的是 `a2a record created · <id> · auto-submitted`，但列表里的状态可能显示
> `○ 草稿`。A2A 记录的审批状态不影响本实验后续章节，
> 需要挂载的只有 4.5 节那条技能记录。

2. 点 `+ 注册`，记录类型选 `AGENT_SKILLS · 技能`，来源选 `粘贴 SKILL.MD`，填写：

   - 名称：`lab-quota-answering`
   - 描述：`AgentCore 配额回答规范：要求说明服务、配额、数值与单位、区域范围、可调整性及来源边界。`
   - SKILL.MD 内容：

```markdown
---
name: quota-answering
description: 规范 AgentCore 配额与容量规划回答。当问题涉及配额值、单位、区域范围、可调整性或上线容量时使用。
---

# AgentCore 配额回答技能

回答 AgentCore 配额问题时，按以下规则输出：

1. 明确写出 AgentCore 服务、配额名称、准确数值和单位。
2. 配额存在区域差异时逐项说明；没有区域差异时不要自行补充。
3. 说明配额是否可调整。区分「可通过 Service Quotas 或支持工单提高」与「可通过 API 参数配置」。
4. 不要混淆 TPS、TPM、并发数、资源数量、容量、大小和时长。
5. 只使用挂载资料中的事实。资料没有覆盖时，明确说明「资料中未提供」，不要推测。
6. 最后给出一句容量规划影响，且不得把建议写成已获批的配额提升。
```

![登记技能](/static/images/04-registry-register.png)
*图 4-7：登记表单。技能除了粘贴 SKILL.md，还支持上传 ZIP、从 git 导入和 URL 拉取。*

3. 点 `▲ 注册`。

**预期结果**：记录以 `DRAFT` 创建，SKILL.md 被上传到
`s3://launchpad-artifacts-<ACCT>-<AWS_REGION>/skills/lab-quota-answering/`。

在列表里点开这条记录，右侧详情面板给出「记录 ID」、版本 `1.0.0-skill`
和状态 `○ 草稿`。记下当前页面的记录 ID，第 10 章清理时会用到。

## 4.5 走完审批：提交 → 批准

1. 点顶部 `技能` 按钮筛选，在列表里点开 `lab-quota-answering`。

![按技能类型筛选](/static/images/04-registry-skills.png)
*图 4-7b：点 `技能` 后列表只显示 AGENT_SKILLS 记录，能看到刚创建的 `lab-quota-answering`
处于 `○ 草稿`。*
2. 点 `提交`，状态变 `PENDING_APPROVAL`，按钮换成 `批准 · 发布` 与 `驳回`。

![记录详情与提交](/static/images/04-registry-detail.png)
*图 4-8：记录详情。可以看到平台把粘贴的 SKILL.md 原样存进了 Registry 记录，
并生成了 `skillDefinition`（含 S3 路径与文件清单）。*

![已提交待审批](/static/images/04-registry-submitted.png)
*图 4-9：`已提交` 状态下出现 `批准 · 发布` / `驳回` 两个审批动作。*

3. 点 `批准 · 发布`。

![已发布](/static/images/04-registry-approved.png)
*图 4-10：记录状态变为 `● 已发布`（APPROVED）。从这一刻起它才会出现在创建/编辑 Agent 的
技能选择列表里。*

```bash
# <SKILL_RECORD_ID> APPROVED 1.0.0-skill
```

> **注意**：`DEPRECATED` 是终态，停用后不能再改回来。更新一条记录
> （`UpdateRegistryRecord`）会把状态重置回 DRAFT，需要重新走审批。

## 4.6 把知识库 + 技能挂到 Harness 并重新发布

1. 打开 `02 Agent 管理`，在「现有 AGENT」表里找到 `lab-quota-advisor`，点 **编辑**。
2. 顶部会出现提示：*正在编辑「lab-quota-advisor」。重新发布会就地更新。AgentCore 在同一资源上
   发布一个新版本（ARN 不变，DEFAULT 端点自动切换到新版本），几乎无停机。*
3. 勾选刚批准的技能 `lab-quota-answering · skill` 与知识库 `lab-quota-kb · kb`。

![挂载技能与知识库](/static/images/04-harness-attach.png)
*图 4-11：两个 chip 变成绿色 ✓ 即为已选。右侧「重新发布方案」说明影响：同 ARN、新版本、
DEFAULT 自动切换。*

4. 点 `⟳ 重新发布`，在确认弹窗里再次确认。

![重新发布确认](/static/images/04-harness-republish-confirm.png)
*图 4-12：重新发布前的二次确认。*

**预期结果**：流水线再跑一遍（Harness 依旧跳过打包），`供给` 阶段多出 KB 网关工作，
`部署` 阶段变成 `UpdateHarness`。以下日志仅示意关键字段；请核对自己输出中的状态、版本和 ID：

```json
{"stage":"provision","msg":"reusing shared execution role arn:aws:iam::…:role/launchpad-agent-execution-role"}
{"stage":"provision","msg":"kb gateway ready · 1 knowledge base(s) mounted"}
{"stage":"provision","msg":"iam role reused · kb targets ready (1)"}
{"stage":"deploy","msg":"UpdateHarness accepted · harnessId <HARNESS_ID> · new version <NEW_VERSION>"}
{"stage":"deploy","msg":"harness READY · …"}
{"stage":"register","msg":"a2a record refreshed · <A2A_RECORD_ID> · auto-submitted"}
```

![重新发布完成](/static/images/04-harness-republish.png)
*图 4-13：重新发布完成示例。按页面显示核对自己的 Harness 版本和状态。*

复核挂载结果，两处都在页面上：

- `02 Agent 管理` 的「现有 AGENT」里，`lab-quota-advisor` 版本高于重新发布前、状态为 `运行中`。
- `04 知识库` 里 `lab-quota-kb` 的「关联 AGENT」区，从「还没有 Agent 使用此知识库」变成
  列出 `lab-quota-advisor`；技能挂载结果可以在该 Agent 的「编辑」表单里看到勾选状态。

> Harness 通过专用网关 `launchpad-kb-gw` 调用逐库检索和跨库多步检索。

重新发布还会刷新 A2A 记录，按 4.5 那条 `UpdateRegistryRecord`
规则把状态重置为 `DRAFT`。要回 `APPROVED` 需重新审批，本实验不需要。本章结束时
`03 注册中心` 里应当能看到这条 A2A 记录，记录 ID 以当前页面为准：

```
<ADVISOR_RECORD_ID>   lab-quota-advisor   A2A   ← 重新发布后状态被重置为草稿
```

---

## 本章验证清单

- [ ] `lab-quota-kb` 状态 `ACTIVE`，`data_source_count = 1`
- [ ] ingestion 任务 `COMPLETE`，`New Documents Indexed = 1`、`Documents Failed = 0`
- [ ] 文档级状态为 `INDEXED`
- [ ] 检索 Playground 能返回带 score 与来源元数据的相关 chunk
- [ ] `lab-quota-answering` 记录状态为 `APPROVED`
- [ ] `lab-quota-advisor` 版本高于重新发布前，编辑表单里技能与知识库两个 chip 均为 ✓
- [ ] KB 详情页「关联 AGENT」现在能看到 `lab-quota-advisor`

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 总览页 Registry 显示「等待引导初始化」或 `03 注册中心` 显示「注册中心不可用」 | 账号预热发生在 Registry 权限放行之前，bootstrap 当时跳过了 Registry 创建 | 打开 Stack Outputs 的 `SessionManagerUrl`（浏览器 shell），执行 `sudo -iu ubuntu` 后 `cd ~/agentcore_launchpad && make bootstrap`（幂等，摘要应出现 `registry created`），再 `exit` 回 ssm-user 执行 `sudo systemctl restart launchpad-backend`，刷新页面 |
| 注册中心列表是空的，看不到图 4-6 说的两条 A2A 记录 | 第 02/03 章的 Agent 部署发生在 Registry 补建之前，当时 `register` 阶段被跳过 | 不影响本章操作；4.6 重新发布 `lab-quota-advisor` 时 `register` 阶段会自动补登记它的 A2A 记录 |
| 创建 Agent 时看不到刚建的 KB | KB 还是 `CREATING`，或 ingestion 未完成 | 等到 `ACTIVE` 再刷新页面 |
| 创建 KB 返回 `HTTP 500` | 描述超过 Bedrock 的 200 字符上限 | 使用表格中的短描述后重试 |
| KB 已 `ACTIVE` 但数据源为 0、ingestion 从未开始 | 后端补齐数据源的后台任务没跑成（例如期间重启过服务），或该 KB 建于此修复之前 | 详情页橙色告警里点 `补建数据源`；文件已在制品桶里，补建后 ingestion 会自动开始 |
| ingestion `COMPLETE` 但 `Documents Failed = 1` | 文件格式、编码或解析失败 | 确认上传的是本实验提供的 `bedrock-agentcore-limits.md`，文件可正常打开后重新创建 |
| 技能不出现在挂载列表 | 状态还是 `DRAFT` / `PENDING_APPROVAL` | 必须先 `批准 · 发布` |
| 注册中心搜索框搜不到刚建的记录 | 搜索走 AWS `SearchRegistryRecords`，索引有延迟 | 用顶部类型筛选按钮（`技能`）在列表里找 |
| 重新发布点了没反应 | 有二次确认弹窗 | 在弹窗里再点一次 `重新发布` |
| 重新发布后旧对话还是旧行为 | AgentCore 把已有会话钉在首次服务它的版本上 | 开一个新会话验证（第 05 章会用到） |

---

上一章：[第 03 章 · 部署托管 Harness](../03-deploy-harness) ｜
下一章：[第 05 章 · 对话测试与记忆](../05-chat-memory)
