# 第 05 章 · 对话测试与记忆（Chat Playground + Memory 控制台）

> **目标**：在对话演练场里验证两个 Agent 的真实行为差异（有知识库 vs 无知识库），确认技能生效，
> 然后到 Memory 控制台看清「短期事件」与「长期记录」到底存了什么、按什么分区存。
>
> **前置条件**：完成[第 04 章](04-capabilities.md)。`lab-fund-advisor` 版本 2（已挂 KB + 技能）。
>
> **预计耗时**：约 15 分钟。
>
> **本章将创建的 AWS 资源**：AgentCore Memory 中的短期事件与长期记录（写入共享
> `launchpad_memory`，按 Agent 分区）；不创建新的计算资源。

---

## 5.1 与「有知识库」的 Agent 对话

1. **打开** `06 对话演练场`，在顶部下拉里选择 `lab-fund-advisor`。
2. **提问**（答案确定在 PDF 里）：

```
这只基金的成立日期是什么时候？截至 2021 年 8 月 31 日持有多少只股票？
```

![与 advisor 对话](images/05-chat-advisor.png)
*图 5-1：流式回答。中间可以看到工具调用轨迹：`lab-fund-kb-2mbgunvms4___Retrieve` 被调用两次、
`skills` 被调用一次——知识库检索与技能都真实生效了。*

**本次实际回答**（摘录）：

> 📅 基金成立日期：**2012 年 8 月 17 日**
> 📊 截至 2021 年 8 月 31 日的持股数量：**28 只股票**
> 作为对比，同期基准指数 MSCI 新兴市场净收益指数包含 1,123 只成分股，可见本基金为高度集中的
> 精选组合（Active Share 达 89.63%）。
> 数据来源：MS INVF Emerging Leaders Equity Fund 产品资料（截至 2021 年 8 月 31 日）。
> **声明：以上信息摘自基金产品资料，仅供专业投资者参考，过往业绩不代表未来表现。**

三个要点：

- **数字全部正确**，且与 PDF 一致（成立日 2012-08-17、持仓 28 只、Active Share 89.63%）。
- **最后一行合规声明**来自第 04 章挂载的 `lab-fund-disclaimer` 技能——技能不是摆设，它真的改变了输出格式。
- 回答里主动标注了**数据截止日期**，也是技能里写的规则。

3. **再问一轮**（验证多轮上下文）：

```
帮我把它的组合构建规则整理成要点：持仓数量区间、前十大占比、换手率、ROIC 门槛。
```

Agent 用「它」指代上一轮的基金，答出：目标持仓 25–40 只 / 实际 28 只、前十大约 50–60%、
换手率目标 30–40%（实际 17.86%）、ROIC > 15%。**这些都能在 PDF 第 14 页与第 30 页核对。**

## 5.2 对照实验：与「无知识库」的 Agent 对话

切换到 `lab-fund-assistant`（第 02 章创建的 zip runtime，**没有挂知识库**）。

1. 第一轮先声明一个偏好：

```
我是负责 EMEA 区域的销售，之后回答请一律用中文，并且优先用表格呈现关键数字。
```

2. 第二轮考它业务问题：

```
这只基金的投资理念一句话怎么讲？再给我三个能对客户说的卖点。
```

![与 assistant 对话](images/05-chat-assistant.png)
*图 5-2：assistant 遵守了上一轮的格式偏好（中文 + 表格），但具体数字没有任何资料支撑。*

⚠️ **本章最重要的观察**：本次实跑它回答「组合通常持有约 **20–35 只**股票」——而真实资料是
**目标 25–40 只、实际 28 只**。没有知识库接地，模型给出的是听起来专业但**没有出处**的数字。

> **这个现象不是每次都会出现**。同一个提示词，模型有时会蒙对（在另一次实跑里它答的
> 「约 25–40 只」恰好是对的）。如果你这次没看到明显错误，追问一个资料里**根本不存在**的
> 事实，效果更稳定：
>
> ```
> 这只基金 2024 年第三季度的净值涨幅是多少？请给出具体数字。
> ```
>
> 有 KB 的 `lab-fund-advisor` 会明确说资料截至 2021 年 8 月、无法确认；无 KB 的
> `lab-fund-assistant` 通常会给出一个编造的数字或含糊回避。**无论它这次蒙对与否，都不影响
> 第 08 章**——评估器比对的是真值，接地程度一样能被量化成分数。

| | `lab-fund-advisor`（有 KB + 技能） | `lab-fund-assistant`（无 KB） |
|---|---|---|
| 成立日期 | 2012 年 8 月 17 日 ✅ | 未提及 |
| 持仓数量 | 28 只（目标 25–40）✅ | 「约 20–35 只」❌（本次实跑；也可能蒙对） |
| 数据出处 | 标注资料与截止日期 | 无 |
| 合规声明 | 有（技能强制） | 无 |

> 这个差异不是为了演示而制造的——它就是**为什么要做评估**。第 08 章会用 PDF 里的事实做基准答案，
> 让评估器自动量化这种差距；第 09 章会尝试用提示词优化 + A/B 实验去改善无接地 Agent 的表现。

## 5.3 右侧三条轨道：会话、追踪、记忆

对话页右侧有三块，是后面章节的**入口**：

- **历史会话**：同一个 Agent 的历史对话，按轮数与时间列出，可点回去继续。
- **链路追踪**：显示当前会话的 trace id 与 `在可观测中打开 ↗`。刚聊完常显示
  *「暂无 SPAN — 链路约 1 分钟后落地，请重试」*——CloudWatch Transaction Search 有摄取延迟，
  这是正常的（第 07 章详述）。
- **会话记忆**：短期事件数、长期记录数，以及 `在记忆中打开 ↗` 深链到 Memory 控制台。
- 面板底部还给出**等价 API 调用**（`curl -N … /v1/agents/<id>/invoke-stream`）——第 06 章会真的跑它。

![会话记忆轨道](images/05-chat-memory-rail.png)
*图 5-3：两轮对话后，短期事件 20 条、长期记录 1 条，并直接显示抽取出的偏好
`/preferences · "对基金基本信息（成立日期、持仓数量等）感兴趣"`。*

> 每轮回答下方那行 `◈ memory.create_event — 本轮已存入短期记忆` 说明这一轮的 USER/ASSISTANT
> 对已经**恰好一次**写入 Memory。

## 5.4 Memory 控制台：资源概览

**打开** `05 记忆`。这个控制台是**只读**的（后端根本没有实现写操作的包装函数），四个标签页：
`资源` / `短期记忆` / `长期记忆` / `抽取任务`。

![记忆资源概览](images/05-memory-overview.png)
*图 5-4：平台共享的 `launchpad_memory-hurAGN3EnF`：事件过期 30 天、AWS 托管密钥、执行角色，
以及两个长期策略——`semantic_facts`（SEMANTIC，命名空间 `/facts/{actorId}`）与
`user_preferences`（USER_PREFERENCE，命名空间 `/preferences/{actorId}`）。
下方还列出账号里其它 memory 资源，并标注哪个属于本平台（`平台` vs `外部`）。*

**关键概念——为什么命名空间只有 `{actorId}`**：AgentCore 的命名空间模板里**没有 `{agentId}`**。
平台的做法是把 Agent id 折进 actor：`scoped_actor(agent_id, human)` → `<agent_id>__<human>`。
所以同一个人和不同 Agent 聊天，短期事件与长期记录**天然隔离**，一个 Agent 学到的偏好不会串到另一个。

## 5.5 短期记忆：参与者 → 会话 → 事件

切到 `短期记忆` 标签。左列「参与者」就是上面说的复合 actor，控制台把它解码成
`Agent 名 · 用户名` 显示（原始值形如 `26f7707c0d…1e1__river`）。

1. **点击** `lab-fund-advisor · river`
2. **点击** 出现的会话（`c50a8d665f… · 9 条控制台消息`）
3. 右侧「事件」按轮次展开原始短期记忆

![短期记忆钻取](images/05-memory-shortterm.png)
*图 5-5：事件时间线。可以看到 `ASSISTANT` 轮的完整原文，以及一条 `二进制 850 B` 的事件——
非会话型 payload 只显示字节数，不做解码。*

> 那些 `xxx 非智能体分区`（`api`、`default`、`river`…）是历史上直接以裸 actor 写入的分区，
> 或来自 `/v1` 调用等非控制台入口。控制台按第一个 `__` 分隔符解码，解不出 Agent 的就标为非智能体分区。

## 5.6 长期记忆：策略、命名空间与记录详情

切到 `长期记忆` 标签，选 参与者 `lab-fund-advisor · river`，策略选 `user_preferences`。

![长期记忆记录](images/05-memory-longterm.png)
*图 5-6：命名空间被服务端解析成具体值 `/preferences/26f7707c0d964f988360e6a5b4f161e1__river`，
两条偏好记录来自刚才的两轮对话。右侧记录详情展开了结构化内容。*

本次实际抽取到的一条记录：

```json
{
  "context": "用户请求将基金的组合构建规则整理成要点，涵盖持仓数量区间、前十大占比、换手率、ROIC门槛等维度，显示用户偏好以简洁要点形式呈现结构化的基金投资规则信息。",
  "preference": "偏好将基金组合构建规则整理成结构化要点形式呈现",
  "categories": ["finance", "investment", "funds", "information presentation"]
}
```

记录 ID `mem-791000a7-…`，策略 `user_preferences-QjgQrn4j6R`。

> 两种策略的 payload 形状不同：`SEMANTIC` 存散文（`content.text`），
> `USER_PREFERENCE` / `SUMMARIZATION` 存上面这种 JSON 对象。控制台与对话页共用同一个解码函数，
> 所以两边都不会把序列化对象直接糊在界面上。

**`抽取任务` 标签**的用处很具体：当你看到「短期事件有了，但长期记录还是 0」时，去这里按
参与者/会话/策略筛选，就能区分「抽取还没跑完」和「抽取失败了」。

---

## 本章验证清单

- [ ] `lab-fund-advisor` 回答里出现 KB 检索工具调用（`…___Retrieve`）
- [ ] 回答末尾有技能强制的合规声明
- [ ] 关键数字与 PDF 一致（成立日 2012-08-17、持仓 28 只）
- [ ] `lab-fund-assistant` 能遵守上一轮的格式偏好（多轮上下文生效）
- [ ] 记忆轨道显示短期事件 > 0
- [ ] Memory 控制台能看到 `<agent_id>__river` 形式的参与者分区
- [ ] 长期记忆里有 `/preferences/<agent_id>__river` 命名空间下的记录
- [ ] （为第 08 章准备）`lab-fund-advisor` 已被调用过——它的日志组现在才存在

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 长期记录一直是 0 | 抽取是异步的，且需要足够语义信息 | 多聊 1–2 轮，再看 `抽取任务` 标签的状态 |
| 追踪面板长时间「暂无 SPAN」 | Transaction Search 摄取延迟约 1 分钟起 | 等一会点 `⟳ 加载链路`；第 07 章统一看 |
| 重新发布后行为没变 | 已有会话被钉在旧版本 | 点 `新会话` 再验证 |
| 容器 Agent 调用报 `RuntimeClientError` | 修复前的依赖漂移缺陷，见下 | **已修复**；若仍出现请确认代码是最新的 |

### 关于容器 Agent 调用失败（本次实测）

> **已修复（2026-07-26）**。下面保留的是本次实跑真实遇到的现象与根因——一个很好的
> 「部署成功 ≠ 能用」教学案例。**当前代码库已修好**，你按本指南跑不会再遇到；
> 只有在修复前的旧检出上才会复现。

本次实验中 `lab-fund-packager`（方式A 容器）**部署成功但调用失败**：

```
RuntimeClientError: An error occurred when starting the runtime.
```

CloudWatch 日志（`/aws/bedrock-agentcore/runtimes/lab_fund_packager_…-DEFAULT`）里的根因是
容器启动时导入遥测模块失败：

```
File "/app/main.py", line 27, in <module>
    import tracing
File "/app/tracing.py", line 34, in <module>
    from opentelemetry._events import Event, get_event_logger
ModuleNotFoundError: No module named 'opentelemetry._events'
```

原因是容器模板的依赖 `aws-opentelemetry-distro>=0.10,<1` 未锁小版本，新构建拉到的
OpenTelemetry 里已经没有 `opentelemetry._events` 这个实验模块（上游 1.39.0 起废弃、其后移除）。
**这是平台侧的依赖漂移问题，不是你操作错误**；已有的旧容器镜像不受影响，所以只有**新构建**会挂。

**修复做了三件事**：把 `tracing.py` 迁到 OpenTelemetry 的 logs API（事件记录在链路上
逐字段保持不变，否则 AgentCore 评估会静默解析失败）；把模板依赖锁到实测过的小版本区间；
补上单测锁住事件形状。**修复后的真机复验**（重新发布 `lab-fund-packager` → Runtime v2）：

```
10:18:09 UTC  修复前调用  → 容器启动崩溃（上面那段 traceback）
10:21:24 UTC  重建后启动  → 干净启动，无 traceback
10:21:30 UTC  真实调用    → 5.5 秒返回正确回答
              追踪        → 7 个 span，含手工发射的 invoke_agent 与 chat（3 in / 58 out tokens）
              内容事件    → scope=strands.telemetry.tracer，input/output 消息形状正确
```

完整根因与验证记录见
[docs/issues/2026-07-26-container-otel-events-import.md](../issues/2026-07-26-container-otel-events-import.md)。

> **值得记住的教学点**：五阶段流水线全绿只证明「资源建出来了」，不证明「容器能起来」。
> 平台目前**没有**部署后的探活调用，所以一个启动即崩的容器仍会显示为 `运行中`——
> 这个上报缺口是已知的、尚未关闭的问题。新部署一个容器 Agent 后，**先手工调一次**再交付。

---

上一章：[第 04 章 · 挂载能力](04-capabilities.md) ｜
下一章：[第 06 章 · 公共 /v1 API 调用](06-public-api.md)（**可选支线**）｜
跳过：[第 07 章 · 可观测性](07-observability.md)
