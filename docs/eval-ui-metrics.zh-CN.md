# 评估模块 UI 统计口径说明

- 分析对象：`v0.0.8`（`77d36b83`，2026-10-25），V2 控制台（`frontend/src/v2/`）及其后端接口
- 范围：只包括评估相关 UI 页面上展示的分数、均值、通过率、阈值等统计口径。A/B 实验、canary、优化证据不在本文范围内。
- 方法：代码阅读。口径以代码为准，文中给出文件和函数位置，方便核对。

## 1. 基础概念

### 1.1 原始分

原始分（raw score）是评估器对一条结果记录给出的分数，来自 AWS batch evaluation 或 online evaluation 的结果日志，字段为 `gen_ai.evaluation.score.value`。系统内置的评估器和默认的自定义量表（pass = 1.0，fail = 0.0）都在 0 到 1 之间。

一个 session 对一个评估器可能产生**多条**记录：session 级评估器产生 1 条；span 级评估器（例如工具调用类）每次工具调用产生 1 条。

### 1.2 极性

极性表示分数的方向，定义在后端 `evaluation/agentcore_eval.py` 的 `evaluator_polarity` 和前端 `lib/evaluators.ts` 的 `evaluatorPolarity`，两边各维护一份相同的列表：

| 极性 | 评估器 |
|---|---|
| -1（越低越好） | `Builtin.Refusal`、`ThirdParty.DeepEval.Bias`、`ThirdParty.DeepEval.Toxicity` |
| +1（越高越好） | 其余全部，包括所有自定义 judge |

说明：
- `Builtin.Harmfulness` 和 `Builtin.Stereotyping` 是 +1。线上实测 1.0 表示"无害"或"无刻板印象"。
- 自定义 judge 一律按 +1 处理，因为 AWS 的 `ratingScale` 不提供方向信息。即使自定义 judge 的名字叫 "Refusal"，也不会被当成内置的 Refusal。

### 1.3 归一化分

归一化分把所有评估器统一成"0 到 1，越高越好"，定义在 `v2/format.ts` 的 `normalizedScore`：

```
归一化分 = 极性 < 0 ? 1 − 原始分 : 原始分
```

UI 上的"归一化均分"、颜色、Bad Case、通过率、对比涨跌，**都基于归一化分**计算。

### 1.4 阈值

| 用途 | 规则（基于归一化分） | 位置 |
|---|---|---|
| Bad Case（判为失败） | `< 0.7` | `format.ts` `BAD_CASE_THRESHOLD` |
| 通过（passed） | `≥ 0.7` | `results.ts` `outcomeOf` |
| 异常（error） | 评估器报错，或没有分数 | `results.ts` `outcomeOf` |
| 分数颜色 | `≥ 0.7` 绿，`0.4–0.7` 黄，`< 0.4` 红 | `format.ts` `scoreTone` |
| 结果筛选分档 | 低 `[0, 0.4)`，中 `[0.4, 0.7)`，高 `[0.7, 1]` | `results.ts` `SCORE_BANDS` |
| KPI"归一化均分"的颜色 | `≥ 0.7` 绿，否则红 | `ResultsView.tsx` |

## 2. 单个评估器的 run 级均值

run 列表、评分卡、对比页用到的"某个评估器在某次 run 上的均值"，来自后端 `agentcore_eval.parse_eval_scores`，结果存在 `EvalRun.scores` 中：

- 默认直接使用 AWS 返回的 `evaluatorSummaries[].statistics.averageScore`，即该评估器所有记录原始分的均值；`count` 取 `totalEvaluated`。
- code-based 评估器的 `statistics` 为空，后端会从结果日志流逐条读取原始分，自己计算均值，保留 2 位小数。

前端使用时，再用 `normalizedScore` 把这个均值转换成归一化分（`compare.ts` 的 `runScore`）。对极性为 -1 的评估器，"均值的归一化"等于"归一化后的均值"，两者结果一致。

## 3. 各页面的统计口径

### 3.1 评估任务列表（`TaskList.tsx`）

| 指标 | 口径 | 函数 |
|---|---|---|
| 平均分（每行） | 该 run 各评估器归一化均值的**算术平均**，每个评估器只算一次，**不按记录数加权**。只有 evaluators 模式的 run 有这个值，insights run 显示为空 | `scorecard.runMean` |

### 3.2 任务详情 / 结果页（`TaskDetail.tsx`，KPI 区和结果表由 `ResultsView.tsx` 渲染）

数据来源：`GET /api/eval/runs/{id}/results`，即该 run 结果日志流里的**所有记录**，每次最多读 5000 条（`RESULT_RECORDS_MAX`，超过时标记 `truncated`）。

| 指标 | 口径 | 函数 |
|---|---|---|
| 评估数量 | 结果记录总条数，副标题拆成通过、Bad Case、异常三类 | `results.summarize` |
| 归一化均分 | 所有有分数的记录的归一化分**逐条平均**，相当于按记录数加权 | `results.summarize` |
| Bad Case | 归一化分 `< 0.7` 的记录条数 | `results.summarize` |
| 通过率 | `通过数 / (总数 − 异常数)`，四舍五入到整数百分比，**不含异常** | `ResultsView.tsx` |
| 按评估器分组 | 每个评估器的记录数、归一化分逐条平均、Bad Case 数，按均分从低到高排序 | `results.summarize` 的 `byEvaluator` |
| 结果表 | 每条记录同时显示原始分和归一化分 | `ResultsView.tsx` |

### 3.3 按 Agent 评分卡（`AgentScorecard.tsx`、`AgentScoreList.tsx`）

| 指标 | 口径 | 函数 |
|---|---|---|
| 每次 run 的平均分 | 同 3.1，不加权的 `runMean` | `scorecard.runMean` |
| Benchmark 综合分（趋势线、最新分） | 只取**该 benchmark 下所有已完成 run 都包含的评估器**（`coreEvaluators`），对这些评估器的归一化均值求算术平均。目的是让评估器增减时，趋势线的计算范围保持不变 | `scorecard.composite` |
| 与上一版本的变化 | 最新 run 的综合分减去"上一个版本中最新的已完成 run"的综合分。同一版本的重跑不参与比较；没有版本信息的 run（日志来源）和它前一次已完成的 run 比较 | `scorecard.ts` |
| 回退判定 | 综合分下降 `≥ 0.05`，或任一评估器的归一化均值下降 `≥ 0.1` | `REGRESSION_COMPOSITE`、`REGRESSION_EVALUATOR` |
| 提升判定 | 上升 `≥ 0.05` | `IMPROVEMENT` |

Benchmark 指 run 的评测对象：一个本地 dataset、一个云端 dataset，或者 agent 的线上流量。

### 3.4 两次运行对比（`RunCompare.tsx`）

| 指标 | 口径 | 函数 |
|---|---|---|
| 综合分（A、B） | 只取**两次 run 都打过分的评估器**，对各自的归一化均值求算术平均，两边用同一组评估器 | `compare.runComposite` |
| 单个 case 的分数 | 先按评估器，对该 case 的所有记录求归一化分均值（span 级评估器会有多条），再对选定范围内的评估器求算术平均 | `compare.sideScore`、`caseScore` |
| 单个 case 的变化 | `B − A`，只用两边都有分数的评估器 | `compare.caseDelta` |
| case 分类 | 变化 `≥ +0.1` 为上升，`≤ −0.1` 为下降，其余为持平；两边都没有可比分数为"未评分"；只有一边有 session 为"仅 A"或"仅 B" | `compare.classify`（`CASE_DELTA = 0.1`） |
| 变化标签颜色 | 绝对值 `< 0.02` 显示为持平（灰色） | `FLAT_DELTA` |

对比范围可以选"综合"（所有共有评估器），也可以选单个评估器。

### 3.5 洞察页（`Insights.tsx`）

数据来源：所选时间范围内**最近 12 次**已完成的 evaluators 模式 run 的全部结果记录（`MAX_RUNS = 12`），加上每个 online 配置的**最近 50 条**记录（`RECENT_LIMIT = 50`）。KPI 区和结果表与 3.2 相同（`summarize`），并且会随筛选条件（评估器、结果、agent、来源、分档、关键词）重新计算。

### 3.6 在线评估详情（`OnlineDetail.tsx`，scores 模式）

这个页面上的数字来自两个不同的数据源：

| 区域 | 数据源 | 口径 |
|---|---|---|
| KPI 区（评估数量、归一化均分、Bad Case、通过率） | 接口返回的 `recent`，即**最近 50 条**记录 | 同 3.2 的 `summarize` |
| 按评估器统计表的均值 | 服务端 Logs Insights 查询 `stats avg(score) by evaluator`，覆盖**整个所选时间范围** | **原始分**均值，保留 2 位小数；只有颜色按归一化分计算 |
| 标签分布 | `stats count(*) by evaluator, label` | 整个时间范围内各标签的条数 |
| 趋势图 | `stats avg(score), count(*) by evaluator, bin(...)` | 每个时间桶的原始分均值；颜色按最后一个点的归一化分 |

### 3.7 概览页 ONLINE QUALITY · 24h（`routers/overview.py` → `online.parse_quality`）

| 指标 | 口径 |
|---|---|
| 质量均分 | 对 workspace 内 agent 所属的所有 online 配置，按 (评估器, 配置) 分组取原始分均值和条数，计算 `Σ(归一化均值 × 条数) / Σ条数`，即**按记录数加权** |
| 按评估器明细 | 保留**原始分**均值和极性，供页面展示 |
| 缓存 | 每个 workspace、每个查看者缓存 120 秒；没有 online 配置时不调用 AWS |

### 3.8 架构助手（`NextSteps.tsx`、`RunHistory.tsx`）

| 指标 | 口径 | 函数 |
|---|---|---|
| 下一步卡片中的 run 均分 | `Σ(归一化均值 × count) / Σcount`，**按记录数加权**；没有 `count` 时权重记为 1 | `lib/evaluation.runMeanScore` |
| 运行历史的迷你趋势图 | 同 3.3 的 benchmark 综合分（不加权） | `scorecard.composite` |

### 3.9 汇总对照

| 页面 | 指标 | 计算单位 | 是否按记录数加权 | 是否归一化 |
|---|---|---|---|---|
| 任务列表、评分卡 | 平均分 | 评估器 | 否 | 是 |
| 评分卡趋势、助手迷你图 | Benchmark 综合分 | 评估器（所有 run 共有的） | 否 | 是 |
| 运行对比 | 综合分 | 评估器（两次 run 共有的） | 否 | 是 |
| 任务详情、洞察、在线详情 KPI | 归一化均分 | 记录 | 是 | 是 |
| 在线详情 | 评估器均值、趋势 | 记录 | 是 | **否**（原始分） |
| 概览 ONLINE QUALITY | 质量均分 | 记录 | 是 | 是 |
| 架构助手下一步卡片 | run 均分 | 记录 | 是 | 是 |

## 4. 计算示例

以下示例的数据都是虚构的，只用来说明计算规则。为了方便心算，假设 AWS 返回的 `averageScore` 恰好等于该评估器所有记录原始分的均值。

### 4.1 一个 run 的结果页 KPI 和列表平均分

一次 run 回放了 2 个场景（session S1、S2），选用了 3 个评估器：

- `Builtin.GoalSuccessRate`：session 级，极性 +1
- `Builtin.Refusal`：session 级，极性 -1（越低越好）
- `Builtin.ToolSelectionAccuracy`：工具调用级，极性 +1，每次工具调用产生一条记录

**原始记录**（共 10 条）：

| Session | 评估器 | 原始分 | 归一化分 | 结果 |
|---|---|---|---|---|
| S1 | GoalSuccessRate | 1.0 | 1.0 | 通过 |
| S1 | Refusal | 0.0 | 1 − 0.0 = 1.0 | 通过 |
| S1 | ToolSelectionAccuracy（4 次调用） | 1, 1, 1, 0 | 1, 1, 1, 0 | 3 次通过，1 次 Bad Case |
| S2 | GoalSuccessRate | 0.0 | 0.0 | Bad Case |
| S2 | Refusal | 1.0 | 1 − 1.0 = 0.0 | Bad Case |
| S2 | ToolSelectionAccuracy（2 次调用） | 1, 0 | 1, 0 | 1 次通过，1 次 Bad Case |

S1 中 Refusal 的原始分是 0.0，表示 agent 没有拒答，这是好结果，所以归一化后是 1.0。

**任务详情 KPI**（按记录逐条计算，见 3.2）：

| 指标 | 计算 | 结果 |
|---|---|---|
| 评估数量 | 10 条记录 | 10（6 通过 · 4 Bad Case · 0 异常） |
| 归一化均分 | (1+1+1+1+1+0 + 0+0+1+0) / 10 | **0.60** |
| Bad Case | 归一化分 < 0.7 的记录 | 4 |
| 通过率 | 6 / (10 − 0) | 60% |

**按评估器分组**（结果页 `byEvaluator`，按均分从低到高排序）：

| 评估器 | 记录数 | 归一化均分 | Bad Case |
|---|---|---|---|
| GoalSuccessRate | 2 | (1 + 0) / 2 = 0.50 | 1 |
| Refusal | 2 | (1 + 0) / 2 = 0.50 | 1 |
| ToolSelectionAccuracy | 6 | 4 / 6 = 0.67 | 2 |

**任务列表的平均分**（`runMean`，每个评估器算一次，不加权，见 3.1）：

```
run 级原始均值：GoalSuccessRate 0.50，Refusal 0.50，ToolSelectionAccuracy 0.67
归一化：      0.50，         1 − 0.50 = 0.50，                     0.67
平均分 = (0.50 + 0.50 + 0.67) / 3 = 0.56
```

**架构助手卡片的均分**（`runMeanScore`，按 `count` 加权，见 3.8）：

```
(0.50 × 2 + 0.50 × 2 + 0.67 × 6) / (2 + 2 + 6) = 0.60
```

可以看到，同一个 run 在任务列表显示 **0.56**，在任务详情和助手卡片显示 **0.60**。差异来自 ToolSelectionAccuracy：它有 6 条记录，在加权计算里占了 60% 的权重，在不加权计算里只占三分之一（见 5.1）。

**有评估异常时**：如果 S2 还有一条 ToolSelectionAccuracy 记录因为 judge 报错而没有分数，评估数量变成 11（6 通过 · 4 Bad Case · 1 异常）。归一化均分仍是 0.60，因为异常记录不参与计算；通过率是 6 / (11 − 1) = 60%，分母同样不含异常。

### 4.2 两次运行对比

在同一个 dataset 上对新版本再跑一次 run B，并额外加了一个评估器 `Builtin.Correctness`：

| Session | GoalSuccessRate | Refusal（原始分） | ToolSelectionAccuracy | Correctness |
|---|---|---|---|---|
| S1 | 1.0 | 0.0 | 1, 1, 1, 1 | 0.8 |
| S2 | 1.0 | 0.0 | 1, 1 | 0.6 |

**综合分**（`runComposite`，见 3.4）：

- 共有评估器：GoalSuccessRate、Refusal、ToolSelectionAccuracy。Correctness 只有 B 有，**不参与对比**。
- A 的综合分 = 0.56（与 4.1 中列表的平均分相同）。
- B 的综合分 = (1.0 + (1 − 0.0) + 1.0) / 3 = 1.00。
- 变化 = 1.00 − 0.56 = **+0.44**。

**单个 case 的分类**（对比范围选"综合"）：

| Case | A 的分数 | B 的分数 | 变化 | 分类 |
|---|---|---|---|---|
| S1 | (1.0 + 1.0 + 0.75) / 3 = 0.92 | 1.00 | +0.08 | 持平（< 0.1） |
| S2 | (0.0 + 0.0 + 0.50) / 3 = 0.17 | 1.00 | +0.83 | 上升 |

S1 中 ToolSelectionAccuracy 的 0.75，是 4 条记录 (1+1+1+0)/4 的均值；S2 中的 0.50 是 (1+0)/2。

**对比范围改为单个评估器 ToolSelectionAccuracy 时**：S1 从 0.75 变成 1.00，变化 +0.25，分类从"持平"变成**上升**。所以同一个 case 在不同的对比范围下，分类可能不同。

### 4.3 评分卡的综合分和回退判定

某个 agent 在同一个 benchmark 上有 3 次已完成的 run：

| Run | Agent 版本 | 评估器 | 归一化均值 |
|---|---|---|---|
| r1 | v3 | GoalSuccessRate、Refusal、ToolSelectionAccuracy | 0.88、0.95、0.78 |
| r2 | v3（重跑） | GoalSuccessRate、Refusal、ToolSelectionAccuracy | 0.90、0.95、0.80 |
| r3 | v4 | GoalSuccessRate、Refusal、ToolSelectionAccuracy、Correctness | 0.75、0.95、0.90、0.60 |

**计算过程**：

1. **共有评估器**（`coreEvaluators`）：3 次 run 都包含的是 GoalSuccessRate、Refusal、ToolSelectionAccuracy。Correctness 只有 r3 有，不计入综合分。
2. **比较对象**：r3 属于 v4，要和"上一个版本中最新的已完成 run"比较，也就是 r2。r1 和 r2 是同一版本，r1 不参与比较。
3. **综合分**：
   - r2 = (0.90 + 0.95 + 0.80) / 3 = 0.883
   - r3 = (0.75 + 0.95 + 0.90) / 3 = 0.867
   - 变化 = −0.017，下降幅度没有达到 0.05，综合分本身**不算回退**。
4. **单个评估器**：GoalSuccessRate 从 0.90 降到 0.75，下降 0.15，达到了 0.1 的阈值，所以 v4 **判为回退**。

另外，r3 在任务列表上的平均分是 (0.75 + 0.95 + 0.90 + 0.60) / 4 = **0.80**，因为列表的平均分包含 Correctness；评分卡趋势线上显示的综合分却是 **0.867**。两者使用的评估器范围不同。

### 4.4 概览页 ONLINE QUALITY

workspace 中有两个 online 配置，过去 24 小时的统计如下：

| 配置 | 评估器 | 原始分均值 | 记录数 | 归一化均值 |
|---|---|---|---|---|
| config-1 | GoalSuccessRate | 0.80 | 100 | 0.80 |
| config-1 | Refusal | 0.10 | 100 | 1 − 0.10 = 0.90 |
| config-2 | GoalSuccessRate | 0.50 | 20 | 0.50 |

```
质量均分 = (0.80 × 100 + 0.90 × 100 + 0.50 × 20) / (100 + 100 + 20)
        = 180 / 220
        = 0.818
```

config-2 只有 20 条记录，对结果影响很小。如果按配置不加权平均，结果会是 ((0.80 + 0.90) / 2 + 0.50) / 2 = 0.675。

在 config-1 的在线评估详情页上，按评估器统计表里 Refusal 显示的是**原始分 0.10**（颜色为绿色，表示好），KPI 区显示的是归一化均分，两者方向相反（见 5.3）。

### 4.5 量表不在 0–1 之间时会出现的问题

假设新建了一个自定义 judge，量表是 1 到 5 分（目前后端不会拦截，见 5.2），某次 run 中它的均值是 4.0，同时 GoalSuccessRate 的均值是 0.50：

- 自定义 judge 的极性固定为 +1，所以归一化分就是 4.0，**超出了 0–1 的范围**。
- 任务列表的平均分 = (4.0 + 0.50) / 2 = **2.25**，看起来比满分 1.0 还高。
- 这个 judge 即使打出最低的 1 分，归一化分也是 1.0，大于 0.7，**永远不会被判为 Bad Case**。
- 对比页的阈值 0.1 也失去意义：1 到 5 分的量表上，差 1 分就是 1.0，远超 0.1 的阈值，几乎任何实际变化都会被判为上升或下降。

## 5. 口径问题与建议

### 5.1 同一个 run 的"平均分"，不同页面显示不同的数

- 任务列表和评分卡用不加权的 `runMean`；任务详情 KPI 和助手卡片按记录数加权。
- span 级评估器每次工具调用都会产生一条记录。工具调用多的 run，加权均分会明显偏向这类评估器，两个数字的差距可能很大。
- `docs/architecture.md` 写着助手卡片的 `runMeanScore` 与"任务详情的归一化均值"相同，这一点成立；但任务列表显示的并不是这个数，文档没有说明。

**建议**：统一一种口径，并在 UI 上用不同名称区分。例如把不加权的叫"评估器均分"，把加权的叫"记录均分"，并在 tooltip 里写明计算方式。

### 5.2 默认分数在 0–1 之间，但没有校验

- 自定义 judge 的量表项 `RatingScaleItem.value` 只声明为 `float`，没有上下界（`evaluation/routers.py`）。
- 如果建了 1 到 5 分的评估器：归一化分会超出 0–1 范围；0.7 的 Bad Case 阈值、0.1 的变化阈值、颜色分档都会失效；在不加权平均里，这个评估器还会占主导。
- 当前自定义 judge 的极性固定为 +1，所以不会出现 `1 − 原始分` 变成负数的情况，但所有阈值都会失真。

**建议**：后端校验量表取值在 `[0, 1]`；如果要支持其他范围，就在入库或展示时按量表的最小值和最大值线性换算到 0–1。

### 5.3 在线评估详情页的 KPI 和统计表口径不一致

- KPI 只基于最近 50 条记录，按评估器统计表和趋势图却覆盖整个时间范围。时间范围选 7 天或 30 天时，两者描述的不是同一批数据。
- 统计表显示的是原始分均值，KPI 显示的是归一化均分。对 Refusal 这类评估器，统计表上的数字越低越好，和 KPI 的方向相反，只能靠颜色区分。

**建议**：KPI 改用服务端对整个时间范围的统计（`summary` 查询已经有这些数据），或者在 KPI 上注明"最近 50 条"。统计表增加一列归一化均值，或者在列名里标明"原始分"。

### 5.4 洞察页的样本有上限，页面上没有提示

洞察页只取最近 12 次 run 和每个 online 配置的最近 50 条记录。代码会记录被跳过的 run 数（`skippedRuns`），但 KPI 的数字本身不反映这个限制。时间范围较长时，用户容易把它理解为整段时间的全量统计。

**建议**：在 KPI 区域显示样本范围，例如"基于最近 12 次运行、每个在线配置最近 50 条"。

### 5.5 极性列表在前后端各维护一份

前端 `lib/evaluators.ts` 和后端 `agentcore_eval.py` 各有一份极性列表，只靠代码注释提醒两边同步。UI 上所有的归一化计算都使用前端这份列表。一旦两边不一致，概览页（后端计算）和其他页面（前端计算）的结果就会对不上。

**建议**：由后端在接口里返回每个评估器的极性（A/B 指标已经这样做了），前端只在缺少这个字段时才使用本地列表；或者增加一个测试，检查两份列表一致。
