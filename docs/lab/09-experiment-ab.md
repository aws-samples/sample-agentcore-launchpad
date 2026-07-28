# 第 09 章 · 优化与配置包 A/B 实验

> **目标**：用第 08 章量化出来的问题驱动一次 A/B 实验：让优化器分析 trace 生成改进版
> 系统提示词，做成 control / treatment 两个**配置包**，通过专属实验网关按 50/50 分流，
> 采样打分，得出判定，再决定晋升还是丢弃。
>
> **前置条件**：完成[第 08 章](08-evaluation.md)。**实验对象必须是 zip_runtime 方式的 Agent**
> （见 9.0）。同时满足：账号里没有其它 `running` 实验，共享网关未被别的实验占用。
>
> **预计耗时**：约 30 分钟（8 个阶段逐个手动触发，每步 10 秒到几分钟）。
> 想把样本量跑到能下结论，见本章末的[9.9 大样本版](#99-可选把样本量做到能下结论160-条流量)，约 1 小时 20 分。
>
> **本章将创建的 AWS 资源**：2 个 Configuration Bundle、1 个专属实验 Gateway（含 runtime target）、
> 1 个 A/B 测试、1–2 个在线评估配置。**这些都会在 `CLEANUP` 阶段被删除**。

---

## 9.0 为什么只能对 zip_runtime 做配置包实验

打开 `08 评估` → `⚗ 实验` → `+ 新建实验`，Agent 下拉里会**直接把不合格的原因写出来**：

```
lab-fund-assistant · zip_runtime          ← 可选
aurora-support-rt · zip_runtime           ← 可选
lab-fund-packager · container — 只有 Launchpad 托管的 HTTP runtime Agent 支持 config-bundle 实验
lab-fund-advisor · harness   — 只有 Launchpad 托管的 HTTP runtime Agent 支持 config-bundle 实验
```

![新建实验](images/09-exp-new.png)
*图 9-1：新建实验。右侧「配置 A/B 如何执行」列出 8 个阶段：recommend → bundles → gateway
→ abtest → traffic → verdict → promote → cleanup。*

限制来自实现方式：

- **配置包要被 Agent 代码主动读取**。ZIP 通道生成的 Strands 运行时内置 `get_config_bundle()`
  契约（第 02 章配置页那条提示），会在启动时读取路由下发的系统提示词与工具描述。
- **Harness 不行**：它背后的 runtime 被托管、不能直接 invoke，而且导出的 harness 代码里没有任何
  配置包读取逻辑，A/B 变体对它是**空操作**。
- **容器不行**：同理，且候选版本铸造需要 CodeBuild 推镜像（属后续能力）。
- 自定义代码的 zip agent 也会被判为 `custom-source-unverified`，平台不能假设你的代码会读配置包。

> 所以本实验的对象是 `lab-fund-assistant`（第 02 章用表单创建、平台生成代码、无自定义源码）。

选中它，点 `▸ 启动实验`。实验以 `RECOMMEND` 阶段、`RUNNING` 状态创建。

> 每个阶段都要手动触发。结果即时持久化，可以刷新页面或离开后再回来。

## 9.1 RECOMMEND — 让优化器读 trace 提改进方案

实验创建后，详情面板里 8 个阶段依次列出，第一个动作按钮是 `▸ 生成 AI 推荐`。

> **注意**：八个阶段都要手动点击按钮，平台不会自动串行执行。
> 而且顶部的 `<阶段> · RUNNING` 里的 `RUNNING` 是**这个实验整体还在进行中**，不代表该阶段
> 正在执行：停在 `BUNDLES · RUNNING` 通常只是在等你点 `▸ 创建 CONTROL + TREATMENT BUNDLE`。
> 真正在跑的阶段会显示 `◐ 运行中…` 加一行进度文字。

![实验阶段面板](images/09-exp-stage-recommend.png)
*图 9-1b：实验详情。左侧是阶段卡片（当前 `RECOMMEND · RUNNING`），
提示语说明每个阶段都由操作人手动触发，结果即时持久化。*

点 `▸ 生成 AI 推荐`。进度条会显示 `generating system-prompt recommendation from recent traces…`。

![推荐结果](images/09-exp-recommendation.png)
*图 9-2：左「当前」右「推荐」并排 diff，右上角标 `CHANGED`。*

本次优化器给出的推荐提示词（在原提示词后追加了"回答规则"）：

```text
你是一名基金产品投顾助手，服务于摩根士丹利新兴市场领先企业股票基金（MS INVF Emerging
Leaders Equity Fund）的销售与客服团队。回答基金的策略、团队、规模与投资流程相关问题。

回答规则：
- 严格遵循用户对格式和长度的要求（如"一句话"、"两句话"、使用表格等）。
- 对于定性的策略与理念问题，直接、简洁地回答。
- 当被问及具体数字（AUM、日期、持股数、业绩、人名）且你无法确认来源时，明确告知用户你没有
  该数据，建议查阅官方 Factsheet 或联系 MSIM 团队，不得编造任何具体数值或人名。
- 在执行任何有实际影响的操作前，先用简明语言说明计划并等待用户明确确认，不得将沉默视为同意。
```

它给出的分析依据（节选，原文英文）：

> The optimizer examined eight trajectories — three with reward 1.0 and five with reward 0.0 —
> and identified a dominant failure pattern… Across every failed trace, the agent **fabricated
> precise factual data it had no verified source for**. In the trace asking about total AUM and
> the Emerging Leaders strategy size, the agent invented specific figures. In the trace asking
> about the fund's founding date and number of holdings, it produced a specific date and a
> holding count…

优化器读取第 08 章评估回放产生的 trace，也找出了与自定义评审相同的问题：
**编造数字**是主要失败模式。推荐结果有对应的 trace 作为依据。

> 旁边还有 `▸ 生成工具描述推荐`。本实验的 Agent 只用了模板内置工具、没有 gateway 工具，
> 所以它是禁用状态（`tool_status: no-tools`）。有工具的 Agent 可以同时优化工具描述。

## 9.2 ACCEPT — 人工确认

点 `▸ 接受并继续`。这一步是**同步**的（不起后台线程），把推荐的提示词固化为 treatment 的内容。

![接受推荐](images/09-exp-accept.png)
*图 9-3：接受后进入 `BUNDLES` 阶段。接受前仍可手工编辑推荐文本。*

## 9.3 BUNDLES — 生成 control / treatment 配置包

点 `▸ 创建 CONTROL + TREATMENT`。

![配置包](images/09-exp-bundles.png)
*图 9-4：两个真实的 AgentCore Configuration Bundle，各自带 bundle id 与 version。*

本次结果：

```json
{"control":  {"bundle_id": "exp_fe7b1d5a_control-zjfvZ16B47",
              "version": "1dedaf98-feab-4d9e-a41d-767eaad20d4a"},
 "treatment":{"bundle_id": "exp_fe7b1d5a_treatment-yEFjiK2smo",
              "version": "4fbfc7dd-c251-4157-8e54-1dca6653546b"}}
```

- **control** = 现状（原系统提示词）
- **treatment** = 接受的推荐提示词

> 创建是幂等的：如果同名包已存在，平台会通过 `ListConfigurationBundles` 冲突认领而不是报错，
> 中断后可以重试。

## 9.4 GATEWAY — 建专属实验网关与在线评估

这一步先选**在线评估器**，也就是两个分组用什么打分，再点 `▸ 创建网关 + 在线评估`。
默认勾选 `目标达成率` + `有用性`，直接按默认走就是本节记录的结果；进度会显示
`creating v1 runtime target…`。

![在线评估器选择](images/09-exp-online-evaluators.png)
*图 9-5a：GATEWAY 卡片上的评估器选择。13 个内置项加账号里的自定义评审（`◆`），最多 10 个。
真值匹配器（`Builtin.Trajectory*Match`）不在列表里，在线评估读的是实时 trace、没有真值。*

> **选它是有讲究的**：treatment 提示词改了什么，就用能测出那件事的评估器。默认那两个
> 未必对得上你的改动，[9.9](#99-可选把样本量做到能下结论160-条流量) 换了一组评估器重跑，
> 结论完全不同。选择会记在 `gateway` artifact 的 `online_evaluators` 里。

![实验网关](images/09-exp-gateway.png)
*图 9-5：为本次实验单独创建的网关与 runtime target，以及一个在线评估配置。*

本次结果（默认评估器）：

```json
{"gateway_id": "launchpad-exp-gw-dskpucnugn",
 "gateway_url": "https://launchpad-exp-gw-dskpucnugn.gateway.bedrock-agentcore.us-west-2.amazonaws.com",
 "target_v1": "expfe7b1dv1", "target_id_v1": "YPECPAHHWZ",
 "online_eval_id": "exp_fe7b1d5a_oe1-wKhd95F84Z"}
```

> **共享网关互斥**：平台会检查共享实验网关是否已被别的实验占用（`assert_shared_gateway_available`）。
> 如果看到相关报错，说明有实验没清理干净，先把它 `清理` 掉。
>
> **在线评估配置按名字幂等认领**：配置名是 `exp_<实验前 8 位>_oe1`。如果这一步中途失败、
> 重试时同名配置已经存在，平台会认领旧的那个，**旧配置的评估器集合不会被改写**。
> 想换评估器就得先把那个配置删掉再重试。

## 9.5 ABTEST — 50/50 分流

点 `▸ 创建 A/B 测试 50/50`。

![A/B 测试](images/09-exp-abtest.png)
*图 9-6：A/B 测试创建完成，两个变体 `C`（control）与 `T1`（treatment）各占 50 权重，
每个变体绑定各自的配置包 ARN + 版本。*

```json
{"ab_test_id": "exp_fe7b1d5a_bundle-115e6d3187",
 "variants": [
   {"name": "C",  "weight": 50, "variantConfiguration": {"configurationBundle": {"bundleArn": "…control-zjfvZ16B47"}}},
   {"name": "T1", "weight": 50, "variantConfiguration": {"configurationBundle": {"bundleArn": "…treatment-yEFjiK2smo"}}}
 ]}
```

## 9.6 TRAFFIC — 采样调用，同时经过两个分组

选择流量来源，然后点 `▸ 发送流量`。可选：`内置演示提示词（12 条）` 或任意本地数据集。
**选 `lab-fund-dataset (5)`**，这样 A/B 用的问题和第 08 章评估口径一致。

![发送流量](images/09-exp-traffic.png)
*图 9-7：流量发送中，进度显示 `sent 3/5 (0 failed)`。请求经实验网关按权重路由到两个变体。*

本次结果：`sent 5 · failed 0 · 数据集 lab-fund-dataset`，产生 5 个会话 id。

> 注意样本量：5 个请求按 50/50 分流后，两组各只有 2–3 个样本，**必然无法达到统计显著**。
> 平台会在判定中明确标出这一点。实际使用时需要上百条流量；
> [9.9](#99-可选把样本量做到能下结论160-条流量) 按功率算过样本量重跑了一遍，可以直接对照。

## 9.7 VERDICT — 判定（含统计显著性）

点 `▸ 监控结果`。进度显示 `aggregating · status RUNNING — results take ~10–15 min after the
last session`。在线评估器需要时间给两组打分。

![判定](images/09-exp-verdict.png)
*图 9-8：判定卡。每个评估器一行 control / treat 对比条，下方给出 `n`、`p` 值与显著性结论。*

本次实测判定（总耗时约 15 分钟）：

| 评估器 | control (C) | treatment (T1) | n | p | 显著 |
|---|---|---|---|---|---|
| 目标达成率 GoalSuccessRate | 0.00 | 0.00 | 3 / 2 | — | 否 |
| 有用性 Helpfulness | **0.61** | **0.50** | 3 / 2 | 0.945 | 否 |

```json
{"verdict": "control-wins", "avg_delta": -0.0567, "n": 10, "significant": false}
```

界面结论：**`◉ 无显著差异 · Δ -0.0567 · n=10 · 观察值: control-wins`**，并给出三条建议：

> 在当前样本量下，两组差异未达统计显著，表面上的胜者可能只是噪声。
> · 继续积累证据：给两个变体多打流量后重新启动实验（判定只计算一次）。
> · PROMOTE 仍然可用，但这属于主观决策，仅凭当前数据不足以支持。
> · 或 CLEANUP 保留现有冠军，不做变更也是合理的结论。

判定需要分三层读：

1. **判定语义要分清**："观察值 control-wins" 只是**观察到的方向**，`significant: false` 才是结论。
   平台仍会把它标成未达显著，不会将观察方向当作最终结论。
2. **treatment 反而略低不代表推荐没用**：更严格的提示词让模型在不确定时拒答，
   `Helpfulness` 这种"用户觉得有用吗"的指标本来就可能下降。**要用与你目标一致的评估器去判定**，
   评估器在 9.4 那一步就能改（[9.9](#99-可选把样本量做到能下结论160-条流量) 换成 `忠实性` 之后，
   同一对配置包才出现了唯一一个显著结论），否则会得出"越诚实越差"的错误结论。
3. **n=10 就是不够**。本实验为控制时长只打了 5 条流量；工程实践里应该先算样本量
   （见 [9.9](#99-可选把样本量做到能下结论160-条流量)，那里给了粗算公式并实跑到了显著）。

> **注意**：`判定只计算一次`。想要更多证据，需要重新打流量并重启实验，而不是反复点监控。

## 9.8 PROMOTE / CLEANUP — 做决定

两个出口：

- **`晋级 ▸`**：停掉 A/B，把 treatment 配置部署到生产（Agent 的系统提示词被替换成推荐版本）。
- **`清理`**：删除本次实验创建的全部 AWS 资源，保留现有冠军（不做变更）。

**本次实验选择 `清理`**。判定不显著，按上面第 2、3 条，晋级只能依赖主观决定。
（如果确实要晋级，界面会在实验记录上留下一条 `⚠ 已在未达统计显著时手动部署` 的警示，
像列表里那条历史实验 `EXP-aurora-support-rt` 一样。）

![清理](images/09-exp-cleanup.png)
*图 9-9：清理完成，逐项列出删除结果。*

本次清理清单（**全部 deleted，幂等**）：

```
deleted  abtest:exp_fe7b1d5a_bundle-115e6d3187
deleted  online-eval:exp_fe7b1d5a_oe1-wKhd95F84Z
deleted  bundle:exp_fe7b1d5a_control-zjfvZ16B47
deleted  bundle:exp_fe7b1d5a_treatment-yEFjiK2smo
deleted  gateway-target:YPECPAHHWZ
```

> **必须执行清理**：实验会占用共享实验网关的互斥锁，不清理会让下一次实验（或第 10 章的金丝雀）
> 直接失败。清理是幂等的，已删的项会显示 `skipped`。

---

## 9.9 可选：把样本量做到能下结论（160 条流量）

9.6 那轮只发了 5 条流量，判定其实什么都没证明：n=10 时两边谁高谁低都是噪声。想拿到一个能拍板的
判定，有两件事要同时做对：**样本量够**，**评估器口径对得上 treatment 改的东西**。本节把两件事
一起做齐，重跑同一个实验。

**耗时约 1 小时 30 分**（本次实测 07:25 创建、08:51 出判定）：160 条流量串行发送用了 59 分钟，
之后等在线评估打完分又等了 18 分钟。前置同 9.0，账号里不能有别的 `running` 实验。

### 9.9.1 样本量要按功率算，不是随手加

两组均值差的粗算是每组 `n ≈ 2(z(α/2)+z(β))²σ²/δ²`。在 α=0.05、power=0.8 下：

| 想检出的差异 δ | 打分波动 σ | 每组需要 | 总流量 |
|---|---|---|---|
| 0.10 | 0.30 | 约 140 | 约 280 |
| 0.18 | 0.375 | 约 70 | 约 **140** |
| 0.20 | 0.30 | 约 35 | 约 70 |

`拒答` 这类接近 0/1 的检测器，均值落在 0.1–0.3 时 σ≈0.375，要抓 0.18 量级的行为差异就需要每组
70 条左右。**160 条**（每组期望 80）留了余量，也是控制台单个数据集 200 条上限内比较实用的量。

> 公式给的是 power=0.8 所需的量，不是显著的门槛。本次最后在每组 84/55 上就拿到了 p=0.047
> （δ=0.11，反推 σ≈0.31），比公式要求的少：**功率不足只意味着更容易漏掉真实差异，
> 不代表已经出现的显著结论不可信**。反过来，如果跑完是"未达显著"，先确认样本量够不够，
> 再谈结论。

**打开** `08 评估` → `▤ 数据集` → `+ 新建数据集` → **JSON 导入**，粘贴
[`assets/lab-fund-dataset-160.json`](assets/lab-fund-dataset-160.json) 的**全部内容**，名称填
`lab-fund-dataset-160`。界面会提示 `已解析 160 条，可以提交`。

160 个 scenario 全部单轮、全部带真值（真值来自实验素材 PDF），按四类行为配比，覆盖不同的失败面。
同一个问题抄 160 遍是没用的，同质流量只会把同一种偏差放大：

| 组别 | 条数 | 问什么 | 想测什么 |
|---|---|---|---|
| 资料内事实数字 | 72 | 成立日期、持仓 28 只、前十大 55.81%、AUM 19,217 百万美元、P/E 52.19、夏普 1.24、逐只持仓的权重与买入月份…… | 该说得出的数字有没有说对 |
| 流程与理念 | 41 | 三步投资流程、覆盖漏斗、买卖纪律、三道风险防线、代理投票、团队与策略沿革 | 定性问题会不会因为怕出错而变得没用 |
| 越界应拒答 | 25 | 2024Q3 净值、当前 AUM、TER、ISIN、注册地、份额类别、税务、明年涨幅、买不买建议…… | 资料里没有的会不会硬答 |
| 格式遵循 | 22 | 一句话/两句话、Markdown 表格、JSON、只输出数字、字数上限、用英文答 | 长度与格式约束守不守 |

![160 条数据集](images/09-exp-dataset-160.png)
*图 9-10：`lab-fund-dataset-160` 创建完成，`160` 条、`predefined`、真值列 `◆ 真值`。
它只是本地 SQLite 数据集，A/B 流量不需要同步到 AWS。*

> 这份数据集带真值，所以也能直接拿去第 08 章跑批量评估。A/B 阶段用不到真值，
> 在线评估器不吃 `expected_response` / `assertions`。

### 9.9.2 评估器要对准 treatment 改了什么

`⚗ 实验` → `▸ 启动实验` → 选 `lab-fund-assistant`，`RECOMMEND` → `ACCEPT` → `BUNDLES` 和
9.1–9.3 一样。到 `GATEWAY`，把评估器从默认两个改成五个：

| 评估器 | 为什么选它 |
|---|---|
| `拒答 Refusal` | treatment 提示词直接改了"什么时候该拒答"，这是主指标 |
| `忠实性 Faithfulness` | 少拒答就可能多编造，用它盯住反面风险 |
| `目标达成率 GoalSuccessRate` | 会话级，看这一轮到底有没有把用户的问题解决 |
| `指令遵循 InstructionFollowing` | 数据集里 22 条格式题的对照指标 |
| `有用性 Helpfulness` | 保留默认项之一，作为总体体验的参照 |

![在线评估器选择](images/09-exp-online-evaluators.png)
*图 9-11：GATEWAY 卡片上的评估器选择。13 个内置项加账号里的自定义评审（`◆`），最多 10 个。*

> **自定义评审在这条路上用不了**：`fund_fact_grounding` 的评分规则依赖 `{expected_response}`，
> 而在线评估打的是实时 trace、没有真值，勾上它只会得到一个降级口径。要用真值判接地度，
> 回第 08 章的批量评估。
>
> **别忘了先读推荐**：每次 `RECOMMEND` 都重新分析 trace，方向可能和上一次相反。
> 本次优化器读到的 trace 里有大量"以查阅官方资料为由不作答"的回答，于是给出的推荐反过来要求
> **不要一味拒答、对精确数据可给出合理区间并提示核实、回答要有结构和深度**。
> 这正好让 `拒答` + `忠实性` 成为这次的关键指标：少拒答的同时会不会开始编造。

### 9.9.3 发 160 条流量

`TRAFFIC` 阶段流量来源选 `lab-fund-dataset-160 (160)`，然后 `▸ 发送流量`。

卡片上会显示 `sent N/160 (M failed)` 进度，这个阶段跑在后台线程上，可以离开页面。
本次实测：**07:33:29 → 08:32:29，59 分钟，`sent 138 · failed 22`**（每条约 22 秒，
一条就是一次真实 Agent 调用）。

> **失败要自己去查原因**：`failed` 只按 HTTP 状态码累加，不记原因。去 Agent 的 runtime
> 日志组翻 `Invocation failed` 就能看到本次的真凶：
>
> ```
> ERROR Invocation failed (20.957s)  errorType: InternalServerException
> An error occurred (InternalServerException) when calling the ConverseStream operation
> (reached max retries: 4): The system encountered an unexpected error during processing.
> ```
>
> 是 Bedrock 侧的瞬时 5xx（重试 4 次仍失败），**不是限流**，整个窗口里
> `ThrottlingException` 一条都没有。这类失败在长时间连续压流量时会零星出现，
> 13.75% 的失败率把有效样本从 160 压到 138，所以**样本量要按功率算完再往上留一点余量**。
>
> **另一个副作用在判定里能看见**：失败的调用同样产生了会话与 trace，所以**会话级**的
> `目标达成率` 把这 22 条也算了进去（当成没达成目标），而**trace 级**的四个评估器只评到了
> 有回答的那些。下面的 `n 97/64` 与 `n 84/55` 差的正好是 22。

### 9.9.4 判定

**先等打分追平再点 `▸ 监控结果`。** 在线评估逐个会话打分，明显滞后于流量，而**判定只算一次**，
按钮点过就从卡片上消失。流量发完立刻点，判定只会用到已经打完分的那部分会话。

本次流量 08:32:29 发完，在线评估到 **08:50** 才把会话打完分（晚约 18 分钟）。
打满之后再点判定：

> 下面 trace 级的 `n 84/55` 合计 139，比 `sent 138` 多一条：流量跑完后我们又手工向网关发过
> 一次请求确认它仍然正常，那一条同样被在线评估算了进去。

![最终判定](images/09-exp-verdict-final.png)
*图 9-12：五个评估器各一行。`忠实性` 那一行右下角是 `✓ 统计显著`。*

| 评估器 | control (C) | treatment (T1) | n (C / T1) | Δ | p | 显著 |
|---|---|---|---|---|---|---|
| **忠实性 Faithfulness** | 0.86 | **0.97** | 84 / 55 | **+0.11** | **0.047** | **✓ 是** |
| 目标达成率 GoalSuccessRate | **0.49** | 0.36 | 97 / 64 | −0.14 | 0.256 | 否 |
| 拒答 Refusal | 0.00 | 0.07 | 84 / 55 | +0.07 | 0.106 | 否 |
| 指令遵循 InstructionFollowing | 0.86 | 0.82 | 84 / 55 | −0.04 | 0.840 | 否 |
| 有用性 Helpfulness | 0.79 | 0.77 | 84 / 55 | −0.01 | 0.926 | 否 |

```json
{"verdict": "control-wins", "avg_delta": -0.0021, "n": 717, "significant": true}
```

界面结论：**`◎ CONTROL-WINS · Δ -0.0021 · n=717 · ✓ 统计显著`**。

**这行结论要拆开读，两个字段说的不是一回事：**

- `significant: true` 是**任一评估器达到显著就为真**（`compute_verdict` 里对各变体的
  `isSignificant` 取或）。这里为真只因为 `忠实性` 一项 p=0.047。
- `verdict: control-wins` 是**所有评估器 Δ 的平均方向**，这里平均只有 −0.0021，
  基本是平的，方向由 `目标达成率` 的 −0.14 拉出来。

所以正确的读法是：**treatment 在"忠实性"上真的更好（+0.11，p=0.047），但在"有没有解决用户问题"
上反而更差（−0.14，未达显著），综合起来是一场平局。** 不是"control 显著更好"。

四条能带走的结论：

1. **样本量按功率算是有效的。** 9.7 那轮 5 条流量、n=10，最小 p 值是 0.945，什么都读不出来；
   有效样本提到 138（每组 84/55）之后，`忠实性` 的 p 落到 0.047，越过了 0.05 这条线。
   n 不够时看到的"某组更高"确实只是噪声。
2. **评估器选错，就算样本量够也测不出来。** 这轮 5 个评估器里只有 `忠实性` 出了显著结论，
   而它正是这次 treatment 提示词直接作用的地方（推荐要求"不要一味拒答、对精确数据给出合理区间
   并提示核实"，回答因此更成体系、内部更自洽）。`有用性` 和 `指令遵循` 的 p 都在 0.84 以上，
   拿它们当判据只会得到"没差别"。
3. **忠实性高不等于答得对，这一章和第 08 章在同一个坑上给出了同样的结论。** `忠实性` 测的是
   回答与它自己给出的上下文是否自洽（[第 08 章 8.3](08-evaluation.md#83-创建自定义-llm-评审)）。
   treatment 变得"更自洽"的同时，`目标达成率` 掉了 0.14：它更愿意给出成体系的答案，也更容易
   把没有依据的数字说得像真的。本次实跑里就抓到一条：问成立日期，它答 `2015-05-29`
   （真值 2012-08-17），而且用 JSON 包得很整齐。
4. **所以这次不晋级。** 唯一显著的胜项是一个"自洽度"指标，方向相反的是"有没有解决问题"，
   再加上编造具体数字的证据，`晋级` 会把一个更能编的提示词推到生产。要判"数字有没有依据"，
   得回第 08 章用带真值的批量评估 + `fund_fact_grounding`，A/B 的在线评估拿不到真值。

最后执行 `清理`，本次实验创建的 AWS 资源全部删除（数据集不属于实验资源，不会被删）：

```
deleted  abtest:exp_2b7d98bb_bundle-96afcb9ea8
deleted  online-eval:exp_2b7d98bb_oe1-qdkClt2ggm
deleted  bundle:exp_2b7d98bb_control-JHEkmq37Xy
deleted  bundle:exp_2b7d98bb_treatment-J8wR7x2jcO
deleted  gateway-target:TSW7DQ1UBN
```

---

## 本章验证清单

- [ ] 只有 zip_runtime Agent 可选，其它方式在下拉里显示不合格原因
- [ ] `RECOMMEND` 产出的推荐提示词与解释能对应到真实 trace 的失败模式
- [ ] `BUNDLES` 生成了两个真实的 Configuration Bundle（各带 version）
- [ ] `GATEWAY` 创建了本次实验专属的网关与在线评估配置
- [ ] `ABTEST` 的两个变体权重各 50，分别绑定 control / treatment 包
- [ ] `TRAFFIC` 发送完成，`failed` 的原因能在 runtime 日志里对上
- [ ] `VERDICT` 给出每个评估器的 control/treat 均值、n、p 值与显著性
- [ ] 判定不显著时界面明确提示"表面胜者可能是噪声"
- [ ] `CLEANUP` 后所有实验资源均为 `deleted`
- [ ]（可选，9.9）`lab-fund-dataset-160` 导入成功：`160` 条、`predefined`、带真值
- [ ]（可选，9.9）GATEWAY 卡片能勾选评估器，判定里每个勾选的评估器都有独立一行
- [ ]（可选，9.9）大样本那轮至少有一个评估器给出 `✓ 统计显著`，并能说清 `significant` 与 `verdict` 的区别

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `+ 新建实验` 是禁用的 | 已有一个 `running` 实验 | 先把它 `清理` 或走到终态 |
| 报共享网关被占用 | 上一次实验没清理 | 找到那条实验执行 `清理` |
| Agent 下拉里选不到目标 Agent | 方式不支持（harness/container/A2A/自定义代码） | 用表单创建的 zip_runtime Agent |
| 判定一直 `INSUFFICIENT-DATA` | 样本太少或在线评估还没出分 | 多打流量；判定内部最多等 15 分钟 |
| 判定的 `n` 比会话数小很多 | 点判定时在线评估还没给所有会话打分，而判定只算一次 | 发完流量等 15 分钟以上再点（见 9.9.4） |
| 加大样本后还是不显著 | 效应本身太小，或评估器与优化目标不一致 | 按 9.9.1 粗算所需样本；再看评估器测的是不是 treatment 改的那件事（9.9.2） |
| 流量里出现零星 `failed` | Bedrock `ConverseStream` 瞬时 `InternalServerException` | 去 runtime 日志组核对原因；样本量要按 9.9.1 留余量 |
| `生成工具描述推荐` 是禁用的 | 该 Agent 没有可优化的工具 | 属预期（`tool_status: no-tools`） |
| 晋级后老会话行为没变 | 会话被钉在旧版本 | 开新会话验证 |

---

上一章：[第 08 章 · 评估](08-evaluation.md) ｜
下一章：[第 10 章 · Runtime 金丝雀](10-canary.md)
