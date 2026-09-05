# 公开 API(/v1) / Public API

English: [api.md](api.md)

每个已部署的 Agent 都可通过平台的 `/v1` 接口调用——与 Chat 交互页面使用的是同一
条调用链。交互式文档:**`/api/docs`**。

鉴权:`X-Api-Key` 请求头。在控制台创建密钥(Chat → API KEYS),或:

```bash
curl -s -X POST localhost:8000/api/apikeys -H 'Content-Type: application/json' \
  -d '{"name": "integration"}'
# → {"id": "…", "prefix": "lp_live_ab12…", "key": "lp_live_<完整密钥,仅此一次展示>"}
```

密钥以 **哈希(sha256)** 存储——完整密钥仅在创建时展示一次。

## 同步调用 / Sync invoke

```bash
curl -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke \
  -H "X-Api-Key: $LP_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "What is 2+2?", "session_id": null}'
# → {"agent":"…","text":"4","session_id":"…","latency_ms":1234}
```

## 流式调用(SSE) / Streaming invoke

```bash
curl -N -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke-stream \
  -H "X-Api-Key: $LP_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "Tell me a two-sentence story."}'
# event: meta   → {"session_id": "…", "mode": "stream"}
# event: delta  → {"text": "Once"} … (增量分片)
# event: done   → {"latency_ms": 2100}
```

在下一次调用时传回返回的 `session_id` 即可延续对话(session 上下文与
AgentCore Memory 随之而来)。

## Python

```python
import requests

BASE, KEY, AGENT = "http://localhost:8000", "lp_live_…", "<AGENT_ID>"

# 同步
r = requests.post(
    f"{BASE}/v1/agents/{AGENT}/invoke",
    headers={"X-Api-Key": KEY},
    json={"prompt": "How many vacation days does EMP-1024 have left?"},
    timeout=120,
)
print(r.json()["text"])

# 流式(SSE)
with requests.post(
    f"{BASE}/v1/agents/{AGENT}/invoke-stream",
    headers={"X-Api-Key": KEY},
    json={"prompt": "Summarize our HR policy in one line."},
    stream=True, timeout=300,
) as stream:
    for line in stream.iter_lines(decode_unicode=True):
        if line.startswith("data:"):
            print(line[5:].strip())
```

错误使用平台统一信封 `{code, message, detail}`——例如
`auth.missing_api_key`(401)、`agent.not_active`(409)、`agent.not_found`(404)。

平台没有映射成自有服务错误码(`kb.not_found`、`memory.unavailable` 等)的 AWS 侧失败,
同样以信封返回,而不是裸的 `500 Internal Server Error` 或 botocore 的
`An error occurred (…) when calling the … operation:` 原文。`app/core/errors.py` 中的全局
`ClientError` 处理器按 AWS 错误码映射:

| AWS 错误码 | HTTP | `code` |
|---|---|---|
| `ResourceNotFoundException` | 404 | `aws.not_found` |
| `ValidationException` | 400 | `aws.validation` |
| `AccessDeniedException`、`UnauthorizedException` | 403 | `aws.access_denied` |
| `ThrottlingException`、`TooManyRequestsException`、`ServiceQuotaExceededException` | 429 | `aws.throttled` |
| `ConflictException`、`ResourceInUseException`、`RetryableConflictException` | 409 | `aws.conflict` |

`message` 是去掉 botocore 前缀后的 AWS 消息;`detail` 为
`{"aws_error_code": "<AWS 错误码>", "operation": "<boto 操作名>"}`。其他 AWS 错误码
(如 `InternalServerException`)仍是未处理的 500,后端日志保留完整堆栈。跨账号角色扮演失败
保持原有答复:502 `workspace.assume_role_failed`。`/v1` 共用同一处理器,状态码与 `code` 相同,
但 `message` 是按错误码固定的通用句子(`AWS resource not found`、`AWS rejected the request as
invalid`、`AWS access denied`、`AWS is throttling this request`、`AWS resource conflict`),
`detail` 只含 `aws_error_code`——AWS 原文会暴露本部署的角色 ARN、实例 id 与操作名,这些只留在
API-key 信任边界的控制台一侧。

## 控制台治理 API：Gateway 限流 / Console Governance API: Gateway rate limits

`/api/governance/gateways/{id}/rate-limits` 管理 AgentCore **Gateway 限流**（2026 年 8 月 GA）。这些路由是**同步**的，
没有可轮询的 operation；AWS 是唯一事实来源，每次变更都记入本地审计日志。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/governance/gateways/{id}/rate-limits` | `{rate_limits: [...]}`：该 Gateway 的全部限流规则（跟完所有 `nextToken` 分页）；对任意 Gateway 可读 |
| `POST` | `/api/governance/gateways/{id}/rate-limits` | 创建 → `201` 返回创建的记录；仅限已纳管 Gateway |
| `PUT` | `/api/governance/gateways/{id}/rate-limits/{rate_limit_id}` | 整体替换 `entries`（可带 `description`）；`dimensionKeys` 不可变，携带则 `422` |
| `DELETE` | `/api/governance/gateways/{id}/rate-limits/{rate_limit_id}` | 删除 → `{deleted: true, id, status}` |
| `POST` | `/api/governance/gateways/{id}/targets/{target_id}/synchronize` | 对单个动态 MCP 服务器目标执行 `SynchronizeGatewayTargets` → `202` 返回目标投影（`status` = `SYNCHRONIZING`）；仅限已纳管 Gateway（`409 governance.gateway_not_managed`）；目标不可同步 → `409 governance.target_not_synchronizable`，`detail.reason` ∈ `not_mcp_server`、`static_tool_schema`、`pending_auth`、`synchronizing`、`not_ready`；审计操作名 `target.synchronize` |

一条限流规则为 `{id, gateway_id, description, dimension_keys, entries, status, created_at, updated_at}`，
`status` ∈ `CREATING | ACTIVE | UPDATING | DELETING`。创建请求体：

```json
{
  "dimension_keys": ["targetName", "$.context.jwt.sub"],
  "entries": [
    {"dimensions": {"targetName": "office-facts", "$.context.jwt.sub": "*"},
     "requests": [{"rate": 10, "period": "second"}],
     "tokens": [{"rate": 5000, "period": "minute"}]},
    {"dimensions": {"targetName": "*", "$.context.jwt.sub": "*"},
     "requests": [{"rate": 60, "period": "minute"}]}
  ],
  "description": "per-target RPS with a default bucket"
}
```

校验在任何 AWS 调用之前完成，失败返回 `422 governance.rate_limit_invalid`，`detail.reason` ∈
`dimension_keys_count | dimension_key_unknown | dimension_key_duplicate | entries_count | entry_dimensions_mismatch |
entry_dimension_empty | wildcard_not_trailing | entry_no_metric | rate_config_count | rate_out_of_range |
period_not_allowed | description_too_long | dimension_keys_immutable`：1–10 个键，取自 `targetName`、`toolName`、
`qualifiedModelId`、`$.context.jwt.<claim>`、`$.context.iam.principal`、`$.context.iam.sourceIdentity`；1–1000 个条目，
每个条目的 `dimensions` 恰好包含父级键；`*` 只能出现在尾部位置；每个条目至少一个指标；`rate` 0–10 000 000；
`requests` 按 `second`/`minute`，`tokens` 仅 `minute`，`connections` 仅 `second`；描述 ≤ 512 字符。
未纳管 Gateway 上的变更返回 `409 governance.gateway_not_managed`；键集合重复或 Gateway 正忙时 AWS 抛出
`ConflictException` → `409 aws.conflict`。每次变更都以 `rate_limit.create` / `rate_limit.update` / `rate_limit.delete`
记入审计路由（`before` = 变更前记录或 `{}`，`requested` = 载荷，`after` = AWS 响应，状态 `succeeded`/`failed`）。

## 控制台 Memory 资源 API / Console Memory Resources API

`/api/memory/*` 支撑只读的 Memory 控制台(控制台 05):没有任何接口会写事件、删记录或触发抽取。
唯一会写的一组接口是下面的 `/api/memory/resources*`——管理记忆*资源*本身,位于独立的路由模块
(`routers/memory_resources.py`)。详见
[architecture.zh-CN.md](architecture.zh-CN.md)「Memory 控制台」一节。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/memory/resources` | 工作区账号/区域内的全部记忆,默认记忆排首位,每条附带 spec 绑定了它的在线 Agent |
| `POST` | `/api/memory/resources` | `CreateMemory`(`{name, description?, event_expiry_days?, strategies?, namespace_keys?}`)→ `201`,返回 `CREATING` 状态的详情投影 |
| `GET` | `/api/memory/resources/{memory_id}` | 详情投影:描述、状态、事件过期、执行角色、策略、命名空间键 |
| `PUT` | `/api/memory/resources/{memory_id}` | 仅限 `{description?, event_expiry_days?}` 的 `UpdateMemory`——至少提供一项(否则 422),`description` 1–4096 字符(只能替换、不能清空),`event_expiry_days` 7–365(越界 422)。只发送 `memoryId` 加给出的字段,绝不发送 `namespaceKeys`(API 会整体替换该集合);响应是用 `GetMemory` 读回的详情投影。不会因被 Agent 引用或是平台默认而被阻止;未知 id → `404 aws.not_found` |
| `DELETE` | `/api/memory/resources/{memory_id}` | `DeleteMemory`;工作区默认记忆返回 `409 memory.platform_protected`,仍被在线 Agent 的 spec 绑定时返回 `409 memory.in_use`(附 Agent 列表) |

## 控制台 Chat API / Console Chat API

`/api/chat/*` 支撑 Chat 交互页面，与 `/v1` 共用同一条调用链（`app.services.invoke`）。
这里的会话就是 AgentCore Runtime 会话：控制台作为 `runtimeSessionId` 发出的 id，正是台账所记录的 id。

| 方法 | 路径 | 结果 |
|---|---|---|
| `POST` | `/api/chat/{agent_id}` | 一轮对话，SSE 形式（`meta` → `delta`/`tool`/`error` → `done`）；`{prompt, session_id?}`，不带 id 即开启新会话 |
| `GET` | `/api/chat/{agent_id}/sessions` | 该 agent 可回放的会话：`{session_id, actor_id, turns, last_at, ended_at, preview}`——`ended_at` 在控制台显式结束 runtime 会话后写入，仍存活或只是空闲时为 `null` |
| `GET` | `/api/chat/{agent_id}/history?session_id=` | 某会话已渲染的对话条目，按回放顺序 |
| `POST` | `/api/chat/{agent_id}/sessions/{session_id}/stop` | **结束会话**——数据面 `StopRuntimeSession(agentRuntimeArn, runtimeSessionId)` → `{session_id, ended: true, already_ended, ended_at}`。AWS 回 `ResourceNotFoundException`（会话早已结束或因空闲过期）时 `already_ended: true`，视为成功而非错误。台账行保留（历史仍可回放）并打上 `ended_at`；之后若在同一 id 下再发一轮，会开启新的 runtime 会话并清掉该标记。只有 runtime 支撑的 agent 才可结束（`zip_runtime`、`studio`、`container`、已发现的 runtime）；托管 Harness——无论自建还是导入——没有结束会话的操作，返回 409 `chat.session_stop_unsupported`，`detail.reason_code` 为 `harness`。其他 agent 或其他 workspace 的会话返回 404 `chat.session_not_found`。撑过 botocore 重试仍然出现的 `RetryableConflictException` 映射为 409 `aws.conflict` |

结束是显式动作：控制台的「新会话」只在本地忘掉 id，留下的 runtime 会话会自行空闲过期。
重新发布之后应当按「结束会话」——AgentCore 会把存活的会话钉在首次服务它的版本上，
验证新版本需要一个全新的会话。

## 控制台 Agent API——版本与端点 / Console Agents API

`GET /api/agents/{agent_id}/versions` 是 Agent 详情「版本与端点」面板背后的只读 AWS 视图。它对该 Agent
所属资源族的两个列表操作跟随每一页 `nextToken`,并返回白名单投影——不含环境变量、制品位置、执行角色或
鉴权配置。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/agents/{agent_id}/versions` | `{kind: runtime\|harness, resource_id, versions[{version, status, description, last_updated_at}], endpoints[{name, live_version, target_version, status, description, created_at, last_updated_at, failure_reason}], latest_version, ledger_version, canary_endpoints[]}`——`versions` 最新在前;`endpoints` 先 `DEFAULT` 再按名称;`latest_version` 是 AWS 报告的最高版本,`ledger_version` 是最近一次 Launchpad 部署记录的版本(`Agent.version`),带外更新或金丝雀候选版本铸造后二者可能不同;`canary_endpoints` 列出仍然存在的 `stable`/`treatment` 端点名。资源族:`zip_runtime`/`studio`/`container` 以及 `spec.discovery.resource_type` 缺省或为 `runtime` 的导入行 → `ListAgentRuntimeVersions` + `ListAgentRuntimeEndpoints`;`harness` 以及 `resource_type == "harness"` 的导入行 → `ListHarnessVersions` + `ListHarnessEndpoints`(harness 版本没有描述字段)。不改变任何状态 |

错误码:`agent.not_found`(404,未知 id 或其他 workspace 的 Agent)、`agent.no_resource`(409,该行没有可查询的
AWS 资源——部署仍在进行、首次部署失败、已删除,或既非 Runtime 也非 Harness 的形态;`message` 即面板展示的
人类可读原因)。AWS `ClientError` 映射为标准 4xx 信封。

## 控制台评估数据集 API / Console Evaluation Datasets API

`/api/eval/datasets` 保存本地 scenario 数据集(SQLite,可编辑的事实来源)及其各自对应的一个 AWS Dataset。AWS 数据集由一份**草稿(DRAFT)**加若干不可变的编号**版本**组成:「同步 AWS」首次创建数据集,之后原地替换草稿中的示例;「发布版本」把草稿快照为版本。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/eval/datasets` | `{datasets[]}`——本地行,含 `items`、`kind`、`has_ground_truth` 与 `cloud` blob |
| `POST` | `/api/eval/datasets` | 由条目创建(devguide scenario、模拟 persona 或旧式 prompt;kind 自动推断)→ 201 |
| `PUT` · `DELETE` | `/api/eval/datasets/{dataset_id}` | 编辑(kind 不可变 → 400 `dataset.kind_immutable`)/ 删除本地行;已同步的 AWS 副本保留 |
| `POST` | `/api/eval/datasets/{dataset_id}/sync-to-aws` | 没有存活云端副本时:`CreateDataset`(内联示例)并轮询到 `ACTIVE`。有副本时:**原地编辑其草稿**——`ListDatasetExamples` → `DeleteDatasetExamples`(草稿为空时跳过)→ 用归一化后的 scenario 调 `AddDatasetExamples`,每步经 `UPDATING` 轮询到 `ACTIVE`;数据集 id 与已发布版本保留,草稿变为 `MODIFIED`。AWS 已不认识的副本(`GetDataset` 返回 `ResourceNotFoundException`)或标为 `deleted` 的副本会重新创建。返回该行;`CREATE_FAILED` / `UPDATE_FAILED` / 超时 → 502 `dataset.sync_failed`,携带 AWS `failureReason` 并记录到 blob |
| `POST` | `/api/eval/datasets/{dataset_id}/publish-version` | 对该行的云端副本调 `CreateDatasetVersion`,经 `UPDATING` 轮询到 `ACTIVE` → 返回该行,新版本位于 `cloud.versions` 首位且 `cloud.draft_status == "UNMODIFIED"`。无存活副本 → 409 `dataset.not_synced`;`UPDATE_FAILED` / 超时 → 502 `dataset.publish_failed`(原因记录到 blob,版本列表保留) |
| `GET` | `/api/eval/datasets/cloud` | workspace 区域内的全部 AWS 数据集:`{datasets[{datasetId, name, status, schemaType, exampleCount, draftStatus, updatedAt}]}` |
| `GET` | `/api/eval/datasets/cloud/{cloud_id}` | 草稿详情:`{datasetId, name, status, schemaType, exampleCount, draft_status, failure_reason, versions[{version, example_count, created_at}], runnable, has_ground_truth}`——版本最新在前 |
| `POST` | `/api/eval/datasets/cloud/{cloud_id}/publish-version` | 仅云端数据集的「发布版本」→ 返回上述刷新后的详情;失败语义与本地路由相同 |
| `DELETE` | `/api/eval/datasets/cloud/{cloud_id}` | `DeleteDataset`——删除草稿与全部版本;指向它的本地行标为 `cloud.status = "deleted"`,下次同步重新创建 |
| `DELETE` | `/api/eval/datasets/cloud/{cloud_id}/versions/{version}` | 带 `datasetVersion` 的 `DeleteDataset`——删除单个已发布版本;草稿与其他版本保留,缓存列表随之刷新 |

本地行上的 `cloud` blob:`{dataset_id, arn, status, synced_at, failure_reason, draft_status (MODIFIED|UNMODIFIED), example_count, versions[{version, example_count, created_at}]}`。它只缓存展示状态——AWS 是事实来源,每次变更都会重新读取 `GetDataset` / `ListDatasetVersions`。

## 控制台评估运行 API / Console Evaluation Runs API

`/api/eval/runs` 通过有界运行队列(`eval_max_concurrent_runs`,上限为账户 5 个活跃批量评估的配额)驱动批量评估 / insights 分析。运行状态:`queued → invoking → waiting → evaluating → completed | failed | stopped`。每一行都带 `stop_requested`(操作员已请求停止,批次仍在 STOPPING)。

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/api/eval/runs?limit&offset&mode&agent_id` | 最新在前的分页 `{runs, total, limit, offset}` |
| `GET` | `/api/eval/runs/{run_id}` | 单个运行(分数 / insight 树 / `batch_eval_id` / `error` / `stop_requested`) |
| `POST` | `/api/eval/runs` | 启动运行(范围四选一:`dataset_id` \| `cloud_dataset_id` \| `session_ids` \| `lookback_hours`)→ 201。`cloud_dataset_id` 范围可附带 `dataset_version`(已发布版本号,如 `"2"`,绝不是 `DRAFT`;省略即草稿):版本必须存在于 `ListDatasetVersions`(否则 422 `run.dataset_version_unknown`,不创建运行行),`GetDataset` / `ListDatasetExamples` 读取该快照。`dataset_version` 搭配其他范围 → 422 `run.dataset_version_scope`。每个运行行都回显 `dataset_version`(草稿、本地、session 与时间窗口运行为 `null`)|
| `POST` | `/api/eval/runs/{run_id}/stop` | **停止活跃运行** → 202 返回该运行。批次已在 AWS 上存在(`batch_eval_id` 非空)时调用 `StopBatchEvaluation`:批次经 `STOPPING → STOPPED`,已评判的会话保留结果,轮询器把运行记为 `stopped`,附带这些部分分数 / insight 树以及 `error = "stopped by operator"`。仍在 `queued` 的运行在本地取消(worker 出队时跳过,不调用 AWS),立即返回 `stopped`。正在回放数据集或等待遥测(尚无批次)的运行在提示词之间停止,绝不会调用 `StartBatchEvaluation`。终态运行(`completed` / `failed` / `stopped`)→ 409 `run.not_active`;不存在 → 404 `run.not_found`。刻意不暴露 `DeleteBatchEvaluation`——账本保留的部分结果 AWS 会丢弃 |
| `GET` | `/api/eval/queue` | `{running, queued, locked, max_concurrency}`——取消的运行立即离开队列,计数只覆盖活跃运行 |

## 控制台在线评估 API / Console Online Evaluation API

`/api/eval/online/*` 管理 AgentCore **在线评估配置**:按采样比例持续给真实会话打分。AWS 是唯一事实来源,
ledger 只存标识。列表返回 workspace 账号内全部配置并按 `owner` 归类:`agent`(本控制台为 agent 创建)、
`experiment`(`exp_*`/`can_*` 实验 arm,只读)、`external`(其他来源)。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/eval/online` | `{configs, total}`:全部配置,含 `owner`、双状态、`failure_reason`、evaluators、采样率、超时、`matched_agent`、`duplicate_enabled`、`results_log_group` |
| `POST` | `/api/eval/online` | 为活跃 agent 创建:`{agent_id, mode: scores\|insights(默认 scores), evaluators[1..10](scores 模式), insights[1..3] ⊆ Builtin.Insight.FailureAnalysis\|UserIntent\|ExecutionSummary + clustering_frequencies[0..3] ⊆ DAILY\|WEEKLY\|MONTHLY(insights 模式), sampling_percentage 0.01–100(省略 → scores 10 / insights 100), session_timeout_minutes 1–1440(15), filters[0..5], description?, enable_on_create(true)}` → 201 行(`status` 从 `CREATING` 开始)。混用两类 → 422 `online_eval.mode_conflict`;行带 `mode`(由 `insights` 非空推导) |
| `GET` | `/api/eval/online/{config_id}` | 完整详情(`filters`、`data_source`、`execution_role_arn`) |
| `PATCH` | `/api/eval/online/{config_id}` | 仅 `owner=agent`:`description, sampling_percentage, session_timeout_minutes, filters` 之任意,加上本模式自己的分析字段——`evaluators`(scores)或 `insights` / `clustering_frequencies`(insights;完整列表,`[]` 频率即清除聚类);另一类 → 422 `online_eval.mode_conflict`,模式不可变。后端总是重发完整 `rule`(AWS 整体替换) |
| `POST` | `/api/eval/online/{config_id}/pause` · `/resume` | 切换 `executionStatus`(`agent` 与 `external`) |
| `DELETE` | `/api/eval/online/{config_id}` | 删除 AWS 配置并删掉 ledger 行(`agent` 与 `external`);结果日志组保留并在响应里给出 |
| `GET` | `/api/eval/online/{config_id}/results?range=1h\|6h\|24h\|7d` | Logs Insights 聚合结果日志组:每个 evaluator 的均值 / 计数 / 会话数 / 标签分布、按时间分桶的趋势、最近 ≤50 条带 judge 解释的记录、错误计数 |
| `GET` | `/api/eval/online/{config_id}/reports` | insights **报告** = 以该配置为数据源的批量评估:`{config_id, mode, reports[{batch_id, name, status, run_status, created_at, updated_at, insights, sessions{completed, failed, in_progress, total}, origin: aws_scheduled\|console, run_id, error}], aws_unavailable}` 最新在前(ListBatchEvaluations 失败时 `aws_unavailable: true`,仅有控制台行)——账本里的控制台运行(`EvalRun.dataset_name == "online:<config_id>"`)与 AWS 定期批次合并,后者靠 `GetBatchEvaluation.dataSourceConfig.onlineEvaluationConfigSource.onlineEvaluationConfigArn` 归属(只有摘要里没有 evaluators/insights 的批次才是候选;每个一次 Get,按 batch id 缓存)。任何归属均可读 |
| `POST` | `/api/eval/online/{config_id}/reports` | 立即出报告 `{range: 1h\|6h\|24h\|7d(24h)}` → 202 `{run_id, status, queue_position}`:仅 agent 持有的 insights 配置(否则 403 / 422);经有界运行队列提交 `EvalRun(mode=insights, dataset_name="online:<config_id>")`,批次使用 `onlineEvaluationConfigSource`——只覆盖该配置在窗口内**采样过**的会话,并继承配置的 insights(该数据源下显式传 evaluators/insights 会被 AWS 拒绝) |
| `GET` | `/api/eval/online/{config_id}/reports/{batch_id}` | `{batch_id, name, status, created_at, updated_at, time_range, sessions, insights{failures, userIntents, executionSummaries}, error_details}`(`parse_insights` 树,与运行页的 insights 运行相同);批次不是该配置的数据源时 404 `online_eval.report_not_found` |

错误码:`online_eval.no_telemetry`(400,agent 还没有遥测日志组,先跑一次会话)、`online_eval.evaluator_unsupported`(400)、
`online_eval.read_only`(403)、`online_eval.not_found`(404)、`online_eval.conflict`(409)、
`online_eval.workspace_not_bootstrapped`(400)、`online_eval.invalid_filter` / `online_eval.bad_range`(422)。
结果最早在会话空闲 `session_timeout_minutes` 之后出现;ENABLED 配置引用的自定义 evaluator 会被 AWS 锁定。

在线评分也出现在查看会话的地方:

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/observability/sessions/{session_id}` | 会话详情附带 `online_scores: {configs[{config_id, config_name, owner, agent{id,name}?, records[{time, evaluator_id, level, score, label, explanation, trace_id}]}], total, unavailable, configs_exist}`——该会话在所有配置下的结果记录(agent 持有的块排在最前),用一条前缀 `SOURCE logGroups(namePrefix: ['/aws/bedrock-agentcore/evaluations/results/'])` 查询读取。失败降级:结果查询失败只置 `unavailable: true`,绝不影响追踪与对话记录;`configs_exist` 表示 workspace 是否有 agent 持有的配置(结果与配置都没有时 UI 隐藏该区块) |
| `GET` | `/api/overview/online-quality` | 「在线质量 · 24h」tile:`{range: "24h", mean, scores, sessions, agents, configs, evaluators[{evaluator_id, mean, count, polarity}], cached}`——对每个 (evaluator, agent 持有配置) 组合按计数加权求均值,lower-is-better 的 evaluator 取 `1 − mean`,因此 tile 始终「越高越好」;`evaluators[].mean` 保持原始值;`configs` 统计 workspace 内 agent 持有的配置数(账本),`agents` 统计有评分的 agent 数,因此「已配置但尚无评分」与「没有配置」可区分。按 workspace 缓存 120 秒并单飞,`force=true` 绕过;没有 agent 持有配置的 workspace 直接返回空载荷,不调用 AWS |
