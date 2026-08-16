# 第 07 章 · 可观测性（Transaction Search · 追踪 · Token 与成本）

> **目标**：看懂平台的三层遥测视图：仪表盘（趋势与成本）、会话（业务视角的对话还原）、
> 追踪（技术视角的 span 瀑布图），并用它定位「一次回答慢在哪、花了多少 token」。
>
> **前置条件**：完成第 05 章，已经产生真实流量；第 06 章是可选支线。**span 有摄取延迟**，
> 刚聊完等 1–2 分钟。
>
> **本章将创建的 AWS 资源**：无（只读 CloudWatch Logs Insights 与 CloudWatch 指标；
> Logs Insights **按扫描量计费**）。

---

## 7.1 三个数据源，三个视图

| 视图 | 数据来自 | AgentCore/AWS 侧 |
|---|---|---|
| 仪表盘 | `aws/spans` 日志组 | CloudWatch Transaction Search（X-Ray → CloudWatch Logs） |
| 仪表盘的 Token 卡 | `bedrock-agentcore` 指标命名空间 | `ListMetrics` + `GetMetricData`（`gen_ai.client.token.usage`） |
| 会话对话还原 | AgentCore Memory `ListEvents` + 本地 ChatMessage 台账 | Memory 为主，台账用于修补滞后/不完整的分区 |

> 前置条件：账号需要开启 **CloudWatch Transaction Search**（把 X-Ray trace segment 目的地
> 设为 CloudWatch Logs）。没开的话这一章所有视图都会是空的。

## 7.2 仪表盘

**打开** `07 可观测` → `仪表盘`，右上角选时间范围（`1H / 6H / 24H / 7D`）。

![可观测仪表盘](images/07-obs-dashboard.png)
*图 7-1：仪表盘包含 5 个统计卡和 4 张图。时间窗口会汇总账号内所有 Agent 的调用，
因此可能包含实验之外的流量和多个模型。*

读取统计卡时，重点核对下面几项：

| 指标 | 怎么读 |
|---|---|
| 追踪 | 一次 invoke 对应一条 trace，可同时查看正常与错误数量 |
| 会话 | 显示选定时间范围内的会话数和活跃 Agent 数 |
| 错误率 | 用于判断失败调用占比 |
| 延迟 P50 / P95 | 统计**根 span** 的分位数；大量短请求会把分位数拉低 |
| Token · 预估成本 | 区分输入、输出和模型；成本来自本地价格表，不是账单 |

「热门工具」面板应能看到第 04–05 章挂载的工具，例如：

```
<KB_TARGET>___Retrieve
skills
agentic-lab-fund-advisor___AgenticRetrieveStream
current_utc_time
```

读取仪表盘时注意：

- **60 秒 TTL 缓存**：视图按 (视图, 时间范围) 缓存 60 秒（右上角会显示 `缓存于 N 秒前`），
  因为 Logs Insights 按扫描量计费。点 `⟳ 刷新` 强制绕过缓存。
- **成本是估算**：token 数 × `config/launchpad.yaml` 里的 `model_prices`（USD / 1M tokens）。
  界面用 `≈ / EST` 标注。`⟳ 更新价格` 按钮会从 litellm 公共价格表拉取当前账号见到过的每个模型的
  精确价格（含区域溢价与缓存读写价）。未知模型只显示 token 数、成本显示 `—`。
- **Token 按框架只统计一个携带用量的 span**：Strands 统计末端 LLM 调用
  （`chat` / `text_completion` / `generate_content`），Claude SDK 统计原生
  OpenInference `AGENT` 根 span。Strands 的 `invoke_agent` 与 wrapper span
  会重复子级/provider 用量，因此不参与求和。

## 7.3 会话视图：业务视角

切到 `会话` 标签。可以按 Agent 过滤、只看含错误的会话。

![会话列表](images/07-obs-sessions.png)
*图 7-2：每行一个会话：追踪数、LLM 调用数、token 与估算成本、起止时间、错误数。*

比较 `lab-fund-advisor` 和 `lab-fund-assistant` 的会话。知识库检索返回的 chunk 会进入模型上下文，
因此带知识库的调用通常会有更多 LLM 调用和 token。请用自己运行中的数据判断效果与成本是否匹配，
不要只比较单次调用的绝对值。

如果完成了第 06 章，列表里还应出现公共 API 产生的会话。公共 API 与控制台调用进入同一条遥测链路。

点开一个会话进入详情：

![会话详情](images/07-obs-session-detail.png)
*图 7-3：会话详情。上半是**对话还原**（数据源标注为 `AgentCore 记忆 · ListEvents · actor river`），
带 `在对话演练场打开 ↗` 回跳；下半是「会话内追踪」卡片列表。*

核对每张「会话内追踪」卡片的状态、耗时、span 数、LLM 调用数、token 和预估成本。
同一会话可能包含多轮调用，应逐条打开，不要把整场会话的指标误当成单次回答。

## 7.4 追踪视图：技术视角（span 瀑布图）

点任意一条「会话内追踪」卡片，或切到 `追踪` 标签选一条 trace。

![追踪瀑布图](images/07-obs-trace.png)
*图 7-4：瀑布图 + 右侧 span 抽屉。左侧按 `LLM / 工具 / 记忆 / 网关 / HTTP` 分色标注，
右侧是选中 span 的详情。*

一条有知识库的问答 trace 通常可以拆成下面这些步骤：

```
POST /invocations
├─ S3.ListObjectsV2 / HeadObject / GetObject       ← 技能包从 S3 拉取
├─ mcp tools/list                                  ← 发现挂载的工具
├─ Bedrock AgentCore.ListEvents                    ← 恢复短期记忆
├─ invoke_agent Strands Agents
│  ├─ RetrieveMemoryRecords                        ← 注入长期记忆
│  ├─ execute_event_loop_cycle
│  │  └─ chat <MODEL_ID>                           ← 模型调用
│  ├─ execute_tool <KB_TARGET>___Retrieve          ← KB 检索
│  ├─ execute_event_loop_cycle → chat
│  ├─ execute_tool skills                          ← 技能加载
│  └─ execute_event_loop_cycle → chat              ← 生成最终回答
└─ Bedrock AgentCore.GetResourceOauth2Token        ← 网关出站鉴权
```

先比较 `invoke_agent`、各次 `chat` 与 `execute_tool` 的耗时，再判断延迟主要来自模型循环还是检索。
如果多次模型调用占据大部分时间，应优先检查循环轮次；如果工具 span 明显更长，再排查检索或网关。

点某个 span 打开右侧抽屉：

![span 抽屉](images/07-obs-span-drawer.png)
*图 7-5：`chat` span 抽屉：操作、模型、提供方（`strands-agents`）、耗时、状态、
Token 用量、预估成本，以及**完整的输入消息与输出消息**。*

抽屉里的输入消息包含平台自动注入的系统提示词增量，这段内容由知识库挂载生成：

```
## Knowledge bases
Retrieval tools are mounted for you. Prefer `agentic-lab-fund-advisor___AgenticRetrieveStream`
(multi-step retrieval across every mounted knowledge base, returns a cited answer) for open
questions; use a per-KB `…___Retrieve` tool for a targeted single search.
Mounted knowledge bases:
- lab-fund-kb (tool `<KB_TARGET>___Retrieve`) — 摩根士丹利新兴市场领先企业股票基金…
```

> 第 04 章填写的 KB 描述会被原样拼进系统提示词，用于引导 Agent 判断何时检索。

## 7.5 交叉跳转

三个方向都通：

- 对话页 → `在可观测中打开 ↗`（当前会话详情）
- 会话详情 → `在对话演练场打开 ↗`
- 深链：`/observability?trace=<TRACE_ID>` 与 `/observability?session=<SESSION_ID>`

`service.name`（如 `<RUNTIME_NAME>.DEFAULT`、`harness_lab_fund_advisor.DEFAULT`）
会通过台账映射回平台 Agent 名显示。

## 7.6 不同创建方式的遥测差异

| 方式 | 遥测来源 |
|---|---|
| Strands（zip / studio） | Strands 原生发射 gen_ai span |
| 托管 Harness | 内部 Strands 运行时，`service.name = harness_{name}.DEFAULT`，scope 为 `strands.telemetry.tracer` |
| Claude SDK 容器 | AgentCore 支持的 OpenInference 插桩原生发射 `ClaudeAgentSDK.query` AGENT span 与工具子 span，scope 为 `openinference.instrumentation.claude_agent_sdk` |

> 容器模板用 `using_session(context.session_id)` 把 AgentCore Runtime session
> 传给 OpenInference，因此可观测性与评估看到的是同一个 `session.id`。输入输出位于
> 插桩自动发射的同 scope content event，模型与 token 位于 `llm.*` 原生属性；
> 平台会把这些数据与现有 `gen_ai.*` 字段统一投影到仪表盘、会话和追踪详情。

---

## 本章验证清单

- [ ] 仪表盘 5 个统计卡都有非零数据
- [ ] 「热门工具」里出现 `lab-fund-kb-…___Retrieve`
- [ ] 会话列表能看到 `lab-fund-advisor` 与 `lab-fund-assistant` 的会话
- [ ] 会话详情能还原完整对话，并显示「会话内追踪」
- [ ] 追踪瀑布图能看到 `invoke_agent` → `execute_event_loop_cycle` → `chat` / `execute_tool` 层级
- [ ] span 抽屉能看到 token 用量与预估成本
- [ ] 对话页 ↔ 可观测的双向跳转可用

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 所有视图都空 | 账号没开 CloudWatch Transaction Search | 开启 X-Ray → CloudWatch Logs 的 trace segment 目的地 |
| 刚聊完看不到 trace | 摄取延迟（约 1 分钟起） | 等一会点 `⟳ 刷新` |
| 数字过一会儿才变 | 60 秒 TTL 缓存 | 点 `⟳ 刷新` 绕过 |
| 成本显示 `—` | 该模型不在价格表里 | 点 `⟳ 更新价格` |
| Token 数看起来偏小/偏大 | Strands 统计末端 LLM span；Claude SDK 统计原生 AGENT 根 span，均避免重复累加 | 属预期设计 |
| 只有 Agent 名而没有会话 | 该 Agent 的调用没带会话（如某些 `/v1` 无状态调用） | 正常 |

---

上一章：[第 06 章 · 公共 API（可选）](06-public-api.md) ｜
下一章：[第 08 章 · 评估](08-evaluation.md)
