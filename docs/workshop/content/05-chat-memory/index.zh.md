---
title: "05 对话测试与记忆"
weight: 50
---

# 第 05 章 · 对话测试与记忆（Chat Playground + Memory 控制台）

> **目标**：比较有无知识库时的回答，确认技能生效，并查看短期事件、长期记录及其分区。
>
> **前置条件**：完成[第 04 章](../04-capabilities)。`lab-quota-advisor` 已挂载 KB 和技能，
> 状态为 `运行中`。
>
> **预计耗时**：约 15 分钟。
>
> **本章将创建的 AWS 资源**：AgentCore Memory 中的短期事件与长期记录（写入共享
> `launchpad_memory`，按 Agent 分区）；不创建新的计算资源。

本章的两名 Agent 使用第 02/03 章共同选定的同一模型，即 Sonnet 5 或回退的 Nova 2 Lite。
它们的创建方式和系统提示词也不同，所以这里是行为对照，不是只改变知识库的严格实验；
重点是核对回答有没有资料依据。

---

## 5.1 与「有知识库」的 Agent 对话

1. 打开 `06 对话演练场`，在顶部下拉里选 `lab-quota-advisor`。
2. 提一个答案确定在配额文档里的问题：

```
AgentCore Runtime 的活跃会话工作负载默认配额是多少？请区分 us-east-1、us-west-2 和其它区域，并说明是否可调整。
```

![与 advisor 对话](../static/images/05-chat-advisor.png)
*图 5-1：advisor 调用单知识库检索、技能与 agentic 检索后，按区域返回配额和可调整性。*

检查自己页面中的工具轨迹和回答。回答应说明 `us-east-1` 与 `us-west-2` 为 **5,000**，
其它 AWS 区域为 **2,500**，单位是每账号活跃会话工作负载，并注明该配额可通过
Service Quotas 提高。工具轨迹应出现 `agentic-lab-quota-advisor___AgenticRetrieveStream`
或知识库的单库 `Retrieve`，并调用 `skills` 加载回答规范。

3. 继续用资料中的字段名做定向追问：

```
请检索 Runtime resource allocation limits，列出 Docker 镜像、直接代码部署压缩包和解压后部署包的最大大小，并说明是否可调整。
```

再次检查工具轨迹、数字和资料依据。正确事实是 Docker 镜像最大 **2 GB**，直接代码部署包
压缩后最大 **250 MB**、解压后最大 **750 MB**，三项均不可调整。工具选择和措辞可能不同；
如果开放问题漏掉某项，可用资料中的英文配额名缩小检索范围。

## 5.2 对照实验：与「无知识库」的 Agent 对话

切换到 `lab-quota-assistant`，也就是第 02 章创建的 ZIP Runtime，没有挂知识库。

1. 第一轮先声明一个偏好：

```
我是负责 us-east-1 和 ap-southeast-1 上线规划的平台工程师。之后请用中文回答，关键配额用表格列出，并单独标注是否可调整。本轮只回复“收到”。
```

2. 第二轮考它业务问题：

```
我们准备上线 AgentCore Runtime。请用不超过 6 行的表格列出需要优先核对的容量边界，并简要说明原因。
```

![与 assistant 对话](../static/images/05-chat-assistant.png)
*图 5-2：assistant 对话界面示例。检查它是否保留平台工程师的区域、语言和表格偏好，以及回答
是否给出可核查来源。截图中的回答只用于说明界面，数值仍要与配额文档核对。*

3. 第三轮追问同一个可核对的问题：

```
AgentCore Runtime 在 us-east-1 和 ap-southeast-1 的活跃会话工作负载默认配额各是多少？请给出单位、可调整性和来源。
```

检查三轮回答是否保留中文、表格、区域和可调整性偏好，并记录它有没有给出可核查的资料来源。
模型输出可能不同；如果它给出数值，要与配额文档中的 `us-east-1 = 5,000`、
`ap-southeast-1 = 2,500` 比对。与文档不符或没有可核查来源的具体数值应标记为无依据回答。
无知识库 Agent 即使答对，也不能把模型记忆当作来源。

| 核对项 | `lab-quota-advisor`（有 KB + 技能） | `lab-quota-assistant`（无 KB） |
|---|---|---|
| 工具轨迹 | 应出现 KB 检索工具；记录实际调用 | 记录是否只根据通用知识作答 |
| 区域配额 | `us-east-1` 5,000；`ap-southeast-1` 2,500 | 如有回答，也与配额文档比对 |
| 部署包大小 | Docker 2 GB；压缩包 250 MB；解压后 750 MB | 记录是拒答、给出来源，还是给出无依据数字 |
| 可调整性 | 区域会话配额可提高；三项部署大小不可调整 | 记录是否区分两类限制 |
| 数据依据 | 应使用已挂载的 AgentCore 配额文档 | 未挂载本章的配额文档 |
| 回答结构 | 应包含服务、配额、值与单位、区域范围、可调整性 | 未挂载该技能，不作要求 |

> 第 08 章会用配额文档真值量化无知识库基线。第 09 章改用保留知识库的
> `lab-quota-advisor-rt`，在同一 Runtime 上单独测试提示词变化。

## 5.3 右侧三条轨道：会话、追踪、记忆

对话页右侧有三块，是后面章节的入口：

- **历史会话**：同一个 Agent 的历史对话，按轮数与时间列出，可点回去继续。
- **链路追踪**：显示当前会话的 trace id 与 `在可观测中打开 ↗`。刚聊完常显示
  *「暂无 SPAN — 链路约 1 分钟后落地，请重试」*。CloudWatch Transaction Search 有摄取延迟，
  属正常，第 07 章详述。
- **会话记忆**：短期事件数、长期记录数，以及 `在记忆中打开 ↗` 深链到 Memory 控制台。
- 面板底部还给出等价 API 调用（`curl -N … /v1/agents/<id>/invoke-stream`），第 06 章会执行它。

![会话记忆轨道](../static/images/05-chat-memory-rail.png)
*图 5-3：会话记忆轨道同时显示短期事件与长期记录。长期记录由服务端异步抽取，
刚聊完时数量可能暂未更新。*

记录自己页面上的短期事件数和长期记录数。计数会随调用方式、异步抽取进度和后续对话变化，
截图中的数值只用于说明界面。

> 每轮回答下方那行 `◈ memory.create_event — 本轮已存入短期记忆` 表示这一轮的
> USER/ASSISTANT 消息已写入一次 Memory。

## 5.4 Memory 控制台：资源概览

打开 `05 记忆`。这个控制台是只读的，后端没有实现写操作的包装函数，三个标签页：
`资源` / `短期记忆` / `长期记忆`。

![记忆资源概览](../static/images/05-memory-overview.png)
*图 5-4：平台共享的 `launchpad_memory-<后缀>`：事件过期 30 天、AWS 托管密钥、执行角色，
以及两个长期策略：`semantic_facts`（SEMANTIC，命名空间 `/facts/{actorId}`）与
`user_preferences`（USER_PREFERENCE，命名空间 `/preferences/{actorId}`）。
下方还列出账号里其它 memory 资源，并标注哪个属于本平台（`平台` vs `外部`）。*

AgentCore 的命名空间模板没有 `{agentId}`，平台将 Agent id 写入 actor：
`<agent_id>__<human>`。不同 Agent 的记忆因此相互隔离。

## 5.5 短期记忆：参与者 → 会话 → 事件

先记下对话演练场显示的当前用户名，以下记作 `<USER_NAME>`。切到 `短期记忆` 标签。
左列「参与者」就是上面说的复合 actor，控制台把它解码成 `Agent 名 · 用户名` 显示。
选择 `lab-quota-assistant · <USER_NAME>`，并确认 actor 符合
`<agent_id>__<USER_NAME>` 格式。

1. 点 `lab-quota-assistant · <USER_NAME>`
2. 点刚才的三轮会话，确认消息数与自己的对话轮次对应
3. 右侧「事件」按轮次展开原始短期记忆

![短期记忆钻取](../static/images/05-memory-shortterm.png)
*图 5-5：短期记忆按参与者、会话和事件逐层展开。`ASSISTANT` 事件可查看完整原文，
非会话型 payload 只显示字节数。*

> 如果页面出现 `xxx 非智能体分区`，通常是直接以裸 actor 写入，或来自 `/v1` 调用等
> 非控制台入口。控制台按第一个 `__` 分隔符解码，解不出 Agent 的就标为非智能体分区。

## 5.6 长期记忆：策略、命名空间与记录详情

切到 `长期记忆` 标签，选参与者 `lab-quota-assistant · <USER_NAME>`，策略选
`user_preferences`。

![长期记忆记录](../static/images/05-memory-longterm.png)
*图 5-6：服务端将命名空间解析为 `/preferences/<agent_id>__<USER_NAME>`，右侧可以展开
从该 Agent 对话中抽取的结构化偏好。*

展开自己页面中的记录，检查是否抽取出中文回答、表格呈现、负责区域或标注可调整性等偏好，并记录
当前数量。存法按策略类型分：`SEMANTIC` 存 `content.text`，`USER_PREFERENCE` /
`SUMMARIZATION` 存 JSON 对象。长期记录由 AgentCore Memory 异步抽取，仍为 0 时见本章末尾的
常见问题。

> `ListMemoryExtractionJobs` 只列出可重试的失败任务，不是抽取历史。排障时使用
> `GET /api/memory/extraction-jobs`。

---

## 本章验证清单

- [ ] `lab-quota-advisor` 回答里出现 KB 检索工具调用（`…___Retrieve`）
- [ ] 回答包含技能要求的服务、配额、值与单位、区域范围和可调整性
- [ ] 关键事实与配额文档一致（区域会话配额 5,000 / 2,500；部署大小 2 GB / 250 MB / 750 MB）
- [ ] `lab-quota-assistant` 能遵守上一轮的格式偏好（多轮上下文生效）
- [ ] 记忆轨道显示短期事件 > 0
- [ ] Memory 控制台能看到 `<agent_id>__<USER_NAME>` 形式的参与者分区
- [ ] 长期记忆里有 `/preferences/<agent_id>__<USER_NAME>` 命名空间下的记录
- [ ] （为第 08 章准备）`lab-quota-advisor` 已被调用过，它的日志组现在才存在

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 长期记录一直是 0 | 服务侧抽取是异步的，且需要足够语义信息 | 多聊 1–2 轮，等一会儿再刷新 `长期记忆` 标签 |
| 追踪面板长时间「暂无 SPAN」 | Transaction Search 摄取延迟约 1 分钟起 | 等一会点 `⟳ 加载链路`；第 07 章统一看 |
| 重新发布后行为没变 | 已有会话被钉在旧版本 | 点 `新会话` 再验证 |

---

上一章：[第 04 章 · 挂载能力](../04-capabilities) ｜
下一章：[第 06 章 · 公共 /v1 API 调用](../06-public-api)（**可选支线**）｜
跳过：[第 07 章 · 可观测性](../07-observability)
