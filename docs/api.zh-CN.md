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

## 控制台 Registry API——实时名片 / Console Registry API: live agent card

`GET /api/registry/records/{record_id}/live-agent-card` 是 Registry 抽屉「AGENT 名片」区块中「实时名片」按钮背后的读取：
该记录背后的运行时*此刻*实际提供的 A2A 名片，与记录在部署时存储的名片并列展示。这是一次按需的数据面调用（`GetAgentCard`），
只在操作者点击时发起——打开抽屉不会触发——且不落任何状态。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/registry/records/{record_id}/live-agent-card` | `{agent_id, runtime_arn, status_code, card, diff}`——`card` 是运行时提供的 JSON 文档（`GetAgentCard.agentCard`，即 A2A 客户端在 `/.well-known/agent-card.json` 读到的内容），`status_code` 为 `GetAgentCard.statusCode`；`diff` = `{identical, fields[{field, record, live}], skills_only_in_live[], skills_only_in_record[]}`，将 `name`/`url`/`version`/`protocolVersion` 与技能 id 集合同记录的 `descriptors.a2a.agentCard.inlineContent` 比较（描述、capabilities 与平台 `metadata` 块不参与比较）。路由在服务端完成 记录 → 账本 Agent（`Agent.registry_record_id`，同 workspace，未删除）→ `Agent.arn` 的解析；浏览器从不提供 ARN。不发送 `runtimeSessionId`；AWS 为提供名片而打开的会话随即以 `StopRuntimeSession` 结束，失败时仅记录日志，名片仍会返回 |

错误码（均在调用 AWS 之前由账本判定）：`registry.record_not_deployed`（404，没有 Launchpad Agent 拥有该记录）、
`registry.record_not_a2a`（409，Agent 的 `spec.protocol` 不是 `a2a`）、`registry.agent_not_ready`（409，Agent 不处于
`active` 或尚无运行时 ARN）。数据面 `ClientError` 映射为标准 4xx 信封（`aws.not_found`、`aws.access_denied`、
`aws.throttled` 等）；没有映射的运行时侧失败（`RuntimeClientError`）为 `registry.live_card_failed`（502），并带
`detail.aws_error_code`——绝不会是裸 500。无需 IAM 变更：控制台角色已具备 `bedrock-agentcore:*`。

## 控制台 Registry API——消费者视图 / Console Registry API: consumer view

Registry 页面从两个侧面展示同一个注册中心。**发布者列表**（`GET /api/registry/records`，控制面 `ListRegistryRecords`）是运维者管理的全集：所有状态的全部记录。**消费者视图**（`?view=discoverable`）则是拥有数据面访问权限的消费者或 Agent 实际能看到的记录——即 GA 发现 API `ListDiscoverableRegistryRecords`。出现在前者而不在后者中的记录，就是尚未经批准对外暴露的记录；两份列表都拿到后，控制台会给它们打上「不可发现」标签。只读，不落任何数据。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/registry/records/discoverable?type=` | `{records[{record_id, name, display_name, description, type, descriptor_types[], status, status_reason, version, created_at, updated_at}], count}`——数据面 `ListDiscoverableRegistryRecords(registryId=<workspace 注册中心>, maxResults=100)`，按 `nextToken` 翻页到底；可选的 `type` 以 `filters=[{name: "recordType", values: [<GA 类型>]}]` 收窄，接受平台名（`A2A`/`MCP`/`AGENT_SKILLS`）或 GA 名（`agent`/`mcp`/`skill`）；行内的 `type` 始终是平台名。摘要从不包含 `descriptors`——需要载荷时读取 `GET /api/registry/records/{record_id}`。`count` 为行数 |

错误码：`registry.bad_type`（422，未知的 `type`）、`registry.unavailable`（503，该 workspace 没有注册中心）。AWS `ClientError` 映射为标准 4xx 信封（`aws.access_denied`、`aws.throttled` 等），绝不返回裸 500。路由策略为 MEMBER，与其他 Registry 读接口一致。

## 控制台治理 API / Console Governance API

以下 `/api` 路由支撑需要鉴权的控制台，不属于公开的 `/v1` Agent 调用契约。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/governance/gateways` | 实时的 MCP Gateway 清单 |
| `GET` | `/api/governance/gateways/{id}` | 目标（每个带 `kind: {protocol, variant}`）、actions 与 `actions_uncovered_targets`、Registry、Engine、IAM 以及可挂接性详情 |
| `POST/DELETE` | `/api/governance/gateways/{id}/manage` | 仅添加/移除 Launchpad 纳管标签 |
| `GET` | `/api/governance/gateways/{id}/registry-preview` | Gateway 级记录差异与遗留记录匹配 |
| `POST` | `/api/governance/gateways/{id}/registry-import` | 创建/复用/更新并提交；绝不批准 |
| `POST` | `/api/governance/gateways/{id}/retire-legacy-records` | Gateway 记录获批后的显式退役 |
| `POST` | `/api/governance/gateways/{id}/engine` | 以所选模式（默认 `ENFORCE`）创建/采用并挂接一个 Engine |
| `GET/POST` | `/api/governance/gateways/{id}/policies` | 列出或创建 `LOG_ONLY` 策略 |
| `PUT` | `/api/governance/gateways/{id}/policies/{policy_id}` | 更新 LOG_ONLY 策略，或创建 ACTIVE 策略候选 |
| `POST` | `/api/governance/gateways/{id}/policies/{policy_id}/promote` | 以证据为门禁的激活/切换 |
| `POST` | `/api/governance/gateways/{id}/policies/{policy_id}/rollback` | 有审计记录的快照/候选回滚 |
| `POST` | `/api/governance/gateways/{id}/mode` | Gateway `LOG_ONLY`/`ENFORCE` 模式切换 |
| `POST` | `/api/governance/gateways/{id}/generations` | 启动自然语言 → Cedar 生成，仅供审阅 |
| `GET` | `/api/governance/gateways/{id}/generations/{generation_id}` | 轮询生成状态并读取草稿资产 |
| `GET` | `/api/governance/gateways/{id}/decisions` | AWS 决策投影，或显式的不可用状态 |
| `GET` | `/api/governance/gateways/{id}/rate-limits` | `{rate_limits: [...]}`：该 Gateway 的全部限流规则（跟完所有 `nextToken` 分页）；对任意 Gateway 可读 |
| `POST` | `/api/governance/gateways/{id}/rate-limits` | 创建 → `201` 返回创建的记录；仅限已纳管 Gateway |
| `PUT` | `/api/governance/gateways/{id}/rate-limits/{rate_limit_id}` | 整体替换 `entries`（可带 `description`）；`dimensionKeys` 不可变，携带则 `422` |
| `DELETE` | `/api/governance/gateways/{id}/rate-limits/{rate_limit_id}` | 删除 → `{deleted: true, id, status}` |
| `POST` | `/api/governance/gateways/{id}/targets/{target_id}/synchronize` | 对单个动态 MCP 服务器目标执行 `SynchronizeGatewayTargets` → `202` 返回目标投影（`status` = `SYNCHRONIZING`，`kind` 与详情一致）；仅限已纳管 Gateway（`409 governance.gateway_not_managed`）；目标不可同步 → `409 governance.target_not_synchronizable`，`detail.reason` ∈ `not_mcp_server`、`static_tool_schema`、`pending_auth`、`synchronizing`、`not_ready`；审计操作名 `target.synchronize` |
| `GET` | `/api/governance/gateways/{id}/audit` | 不可变的本地变更日志 |
| `GET` | `/api/governance/operations/{operation_id}` | 异步 operation 状态 |

`GET /api/governance/gateways/{id}` 详情与同步响应中的每个目标都是同一投影
`{id, name, status, status_reasons, description, kind, listing_mode, last_synchronized_at,
synchronizable, not_synchronizable_reason}`。`kind` 为 `{"protocol": "mcp" | "http" | "inference" |
"unknown", "variant": <联合成员键> | null}`，即 AWS 实际设置的 `TargetConfiguration` 成员（`mcp/lambda`、
`mcp/mcpServer`、`mcp/openApiSchema`、`http/passthrough`、`http/agentcoreRuntime`、`inference/provider` 等）；
空配置为 `unknown`/`null`，未识别的成员映射为 `protocol: <key>` / `variant: null`。详情还带有
`actions_uncovered_targets: [name, …]`，即没有工具 schema、因此绝不会出现在 `actions` 中的 `http` /
`inference` 目标。

策略与 Gateway 变更返回 `202`：

```json
{"operation": {"id": "...", "status": "pending", "operation": "policy_create"}}
```

限流路由（AgentCore **Gateway 限流**，2026 年 8 月 GA）是**同步**的——没有可轮询的 operation。
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

生成启动返回 `{"operation": …, "generation_id": …, "status": …}`；生成出的资产只是供编辑器使用的草稿，
绝不会激活任何策略。

轮询 operation 路由，直到状态为 `succeeded`、`failed`、`partial` 或 `interrupted`。`interrupted` 表示重启后
无法证明 AWS 侧的效果，该 operation 必须被显式重试——后端绝不自动重放。变更请求携带适用于该 operation 的
实时时间戳与确认信息：

```json
{
  "expected_gateway_updated_at": "2026-07-16T09:00:00+00:00",
  "expected_policy_updated_at": "2026-07-16T09:01:00+00:00",
  "acknowledged_gateway_ids": ["gw-a", "gw-b"],
  "confirmation_name": "finance-gateway",
  "override_reason": null
}
```

常见冲突码有 `governance.gateway_not_managed`、`governance.concurrent_change`、
`governance.shared_engine_changed`、`governance.iam_preflight_failed`、`governance.evidence_required`、
`governance.policy_engine_deleted` 与 `governance.registry_record_not_approved`。

当 Gateway 仍引用一个已被带外删除的 Policy Engine 时，读操作不会失败，而是以 `policy_engine.missing = true`
与 `status = "DELETED"` 报告该引用；策略变更返回 `409 governance.policy_engine_deleted`；`POST .../engine`
把该引用视为未挂接：创建一个新的 Engine，以所选模式挂接，并把被替换的 ARN 记录在 operation 上。

## 控制台知识库 API / Console Knowledge Bases API

`/api/knowledge-bases/*` 支撑知识库控制台（控制台 04），底层是 Bedrock 的*托管*知识库
——控制面走 `bedrock-agent`，检索走 `bedrock-agent-runtime`。只有
`type == "MANAGED"` 的知识库可寻址：同一账号内的 VECTOR 知识库会返回
`kb.not_found`。本地不存任何状态，因此每条路由都是一次实时 AWS 调用。详见
[architecture.zh-CN.md](architecture.zh-CN.md)「托管知识库」一节。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/knowledge-bases?status=` | 全部 MANAGED 知识库，字段为 `{kb_id, name, description, status, updated_at, data_source_count, attached_agents}`；`status` 是读取后再施加的可选精确匹配过滤（例如 `ACTIVE`） |
| `POST` | `/api/knowledge-bases` | `202`——`CreateKnowledgeBase`（`{name, description?, source: {mode: "upload"\|"existing", bucket?, prefix?}}`）在知识库仍处于 `CREATING` 时就返回详情，并附 `source_pending`；数据源由后端线程在知识库变为 `ACTIVE` 之后（1.5–3 分钟）在请求之外创建，因此客户端轮询 `GET /{kb_id}` |
| `GET` | `/api/knowledge-bases/{kb_id}` | 详情：状态、ARN、时间戳、`failure_reasons`、`attached_agents`，以及每个数据源的桶/前缀、状态与最近 10 个 ingestion 作业 |
| `PATCH` | `/api/knowledge-bases/{kb_id}` | `{description}`（≤1000 字符）→ `UpdateKnowledgeBase`，名称、角色与配置原样读回后回传；响应是刷新后的详情 |
| `DELETE` | `/api/knowledge-bases/{kb_id}?force=` | 依次删除数据源、按知识库的网关 `Retrieve` 目标与按知识库的内联 S3 策略，最后 `DeleteKnowledgeBase`。仍有 Agent 挂载时返回 `409 kb.has_attached_agents`；`force=true` 会先把它从每个挂载它的 Agent spec 里摘掉（并重新同步 harness 类 Agent 的 agentic 目标） |
| `POST` | `/api/knowledge-bases/{kb_id}/files` | `multipart/form-data`，一个或多个名为 `files`（或 `file`）的部件 → artifacts 桶 `kb/{kb_id}/` 下的 `{keys}`。数据源尚不存在时也允许上传；数据源全在别处的知识库返回 `409 kb.no_upload_target` |
| `POST` | `/api/knowledge-bases/{kb_id}/data-sources` | `201`——用同样的 `{mode, bucket?, prefix?}` 请求体创建 `MANAGED_KNOWLEDGE_BASE_CONNECTOR` 数据源，并返回刷新后的详情。按 S3 位置幂等：同一桶/前缀上已有连接器时直接返回它，而不是再建一个。这同时也是「知识库没有数据源」时的手动补建入口 |
| `DELETE` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}` | `DeleteDataSource` → `{deleted, ds_id}`（AWS 侧为异步删除） |
| `POST` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync` | `StartIngestionJob` → 作业投影 `{job_id, status, started_at, updated_at, statistics, failure_reasons}` |
| `GET` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/ingestion-jobs` | 最近 50 个 ingestion 作业，最新优先，投影同上 |
| `GET` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/documents?page_size=&token=` | `ListKnowledgeBaseDocuments` 的一页（`page_size` 1–100，默认 50），形如 `{documents, next_token, page_size}`；每个文档带知识库侧的 `status`/`status_reason`/`indexed_at`，以及按对象 key 联结进来的 S3 侧 `size_bytes`/`uploaded_at`（后端无权列举该桶时为空） |
| `POST` | `/api/knowledge-bases/{kb_id}/query` | 检索 Playground——`{text, number_of_results?}`（1–100，默认 8）→ 带 `managedSearchConfiguration` 的 `Retrieve`，响应 `{results}`，元素为 `{text, score, location_uri, metadata}` |
| `POST` | `/api/knowledge-bases/ensure-gateway` | 按名字「不存在才创建」共享的 `launchpad-kb-gw` MCP 网关，并把 `{id, arn, url}` 持久化到工作区。幂等；harness 部署路径调用的是同一个 helper，因此这条路由只用于提前预置网关 |

错误码：`kb.not_found`（404——未知 id，或该知识库不是 MANAGED）、`kb.ds_not_found`
（404）、`kb.has_attached_agents`（409，阻塞的 Agent 名在 `detail.agents` 里）、
`kb.delete_conflict`（409——知识库仍在 `CREATING`）、`kb.no_upload_target`（409）、
`kb.no_files`（400——表单里没有上传部件）、`kb.sync_not_ready`（409——
`StartIngestionJob` 抛出 `ValidationException` 或 `ConflictException`：数据源仍在预置，
或已有同步在跑）、`kb.bucket_required` / `kb.invalid_bucket` / `kb.invalid_prefix` /
`kb.invalid_source`（400——数据源校验）、`kb.query_failed`（502——知识库侧检索失败，
例如索引仍在构建）。其余任何 AWS `ClientError` 都走上文的全局映射（`aws.validation`、
`aws.conflict` 等）。资源映射里没有 `kb_role_arn`（创建）或没有 `artifacts_bucket`
（上传）的工作区尚未完成引导，会返回点明缺失键的 `500`。

## 控制台 Memory API / Console Memory API

`/api/memory/*` 支撑只读的 Memory 控制台（控制台 05），底层是共享的 `launchpad_memory` 单例。
控制台的每条路由都是读操作：没有任何接口会写事件、删记录或触发抽取。
唯一会写的一组接口是下面的 `/api/memory/resources*`——管理记忆*资源*本身，位于独立的路由模块
（`routers/memory_resources.py`）。详见
[architecture.zh-CN.md](architecture.zh-CN.md)「Memory 控制台」一节。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/memory/overview` | 资源配置、长期策略、有界的 actor 计数、同级记忆 |
| `GET` | `/api/memory/actors` | actor 列表，复合 id `<agent_id>__<human>` 已解码并解析出 Agent 名称 |
| `GET` | `/api/memory/sessions?actor_id=` | 单个 actor 的会话；由控制台写入的会话会关联到 ChatSession 台账 |
| `GET` | `/api/memory/events?actor_id=&session_id=` | 短期事件；每条载荷的 `kind` 为 `conversational`（角色 + 全文）、`json`（JSON 值无损序列化到 `text`，含 `null`、`false`、`0` 与 `""`）或 `blob`（只带字节数）；未知类型省略 |
| `GET` | `/api/memory/namespaces?actor_id=` | 已替换 `{actorId}` 的策略命名空间模板；尾部的 `{sessionId}` 段折叠为 actor 级前缀（`prefix: true`），其他位置的占位符则产生 `resolvable: false` |
| `GET` | `/api/memory/records?actor_id=&strategy_id=` 或 `?namespace=` | 解析所得命名空间下的长期记录 |
| `POST` | `/api/memory/records/search` | 语义检索（`{query, actor_id, strategy_id?, namespace?, top_k}`），带相关性分数 |
| `GET` | `/api/memory/extraction-jobs` | 失败（可重试）的抽取作业，可按 `actor_id`/`session_id`/`strategy_id`/`status` 过滤——**控制台未展示**；AWS 的 `status` 枚举只有 `FAILED`，因此健康的资源返回空列表 |

记忆资源管理（`?view=resources`）：

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/memory/resources` | 工作区账号/区域内的全部记忆,默认记忆排首位,每条附带 spec 绑定了它的在线 Agent |
| `POST` | `/api/memory/resources` | `CreateMemory`(`{name, description?, event_expiry_days?, strategies?, namespace_keys?}`)→ `201`,返回 `CREATING` 状态的详情投影 |
| `GET` | `/api/memory/resources/{memory_id}` | 详情投影:描述、状态、事件过期、执行角色、策略、命名空间键 |
| `PUT` | `/api/memory/resources/{memory_id}` | 仅限 `{description?, event_expiry_days?}` 的 `UpdateMemory`——至少提供一项(否则 422),`description` 1–4096 字符(只能替换、不能清空),`event_expiry_days` 7–365(越界 422)。只发送 `memoryId` 加给出的字段,绝不发送 `namespaceKeys`(API 会整体替换该集合);响应是用 `GetMemory` 读回的详情投影。不会因被 Agent 引用或是平台默认而被阻止;未知 id → `404 aws.not_found` |
| `DELETE` | `/api/memory/resources/{memory_id}` | `DeleteMemory`;工作区默认记忆返回 `409 memory.platform_protected`,仍被在线 Agent 的 spec 绑定时返回 `409 memory.in_use`(附 Agent 列表) |

每条列表路由都接受并返回 `next_token`（AWS 按 100 条分页），并接受 `max_results`（上限 100）——
不会有任何静默截断。`/records` 与 `/records/search` 的命名空间解析顺序：显式的 `namespace` 优先，
否则由 `actor_id`（+ 可选的 `strategy_id`）推导。

错误码：`memory.not_configured`（409，尚未运行 bootstrap——`/overview` 例外，它改为返回
`{"configured": false, …}`，以便页面渲染初始化状态）、`memory.namespace_required`（400，无法推导出
命名空间）、`memory.unavailable`（502，底层 AWS 调用失败）。

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

## 控制台评估器 API / Console Evaluators API

`/api/eval/evaluators` 是 `?view=evaluators` 子页背后的自定义评估器 CRUD。AWS 是唯一事实来源（没有 ledger 行）；内置与第三方评估器只读。一个自定义评估器恰好有三种**定义**之一，由载荷里出现的字段决定——`instructions`（LLM 评审，`llmAsAJudge`）、`base_evaluator_id`（派生，`derived`）或 `lambda_arn`（代码评估器，`codeBased.lambdaConfig`）；同时给出两个或一个都没有 → 400 `evaluator.definition_ambiguous`。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/eval/evaluators` | `{evaluators[], builtin_count}`——先是本地内置目录（`source: builtin`，轨迹匹配器标 `requires_ground_truth`），再是账户 `ListEvaluators` 的行（`source: third_party \| custom`、`evaluator_type`、`provider`、`status`）。自定义行带 `definition: judge \| derived \| code`（由 `evaluatorType` 推导——列表不含配置） |
| `POST` | `/api/eval/evaluators` | 创建 → 201 `{evaluator_id, arn}`。公共字段：`name`（`^[a-zA-Z][a-zA-Z0-9_]{0,47}$`）、`description`。**评审**：`instructions`（10–4000 字符，至少一个 `{placeholder}`，否则 422 `evaluator.missing_placeholder`）、`rating_scale[≥2]`（默认 pass/fail）、`model_id`、`level`（TOOL_CALL \| TRACE \| SESSION，默认 TRACE）。**派生**：`base_evaluator_id`（`Builtin.*` \| `ThirdParty.*`；不存在 → 400 `evaluator.base_not_found`）、`model_id`；级别取自基础评估器。**代码评估器**：`lambda_arn`（`arn:aws[-partition]:lambda:<region>:<account>:function:<name>[:qualifier]`）、`lambda_timeout_s` 1–300（默认 60）、`level`；Lambda 必须与 workspace 同区域，否则 422 `evaluator.lambda_region_mismatch`（`detail: {lambda_region, workspace_region}`）。派生或代码载荷携带 `rating_scale` → 400 `evaluator.rating_scale_not_allowed`。任何定义在 `CreateEvaluator` 上都必须带 `level` |
| `GET` | `/api/eval/evaluators/{evaluator_id}` | `{id, name, level, description, definition, instructions, rating_scale, model_id, base_evaluator_id, lambda_arn, lambda_timeout_s, evaluator_type, provider, status}`——其他定义的字段为空/null（代码评估器的 `instructions: ""`、`rating_scale: []`、`model_id: null`） |
| `PUT` | `/api/eval/evaluators/{evaluator_id}` | 全量配置替换（`UpdateEvaluator` 接收完整配置，因此每个字段都要回传），载荷与创建相同但不含 `name` → 返回刷新后的详情。载荷必须与评估器**当前**定义一致：用评审/派生载荷更新代码评估器，或用代码载荷更新评审/派生评估器 → 400 `evaluator.definition_mismatch`（`detail: {current, payload}`）——评估器绝不会被转换。托管 id → 400 `evaluator.builtin_immutable` |
| `DELETE` | `/api/eval/evaluators/{evaluator_id}` | `DeleteEvaluator` → `{deleted: true}`；托管 id → 400 `evaluator.builtin_immutable`。被 ENABLED 在线配置引用的评估器会被 AWS 锁定 |

**代码评估器（Lambda）契约。** 服务以 `{schemaVersion, evaluatorId, evaluatorName, evaluationLevel, evaluationInput.sessionSpans, evaluationReferenceInputs, evaluationTarget}` 调用函数，函数返回 `{label, value?, explanation?}` 或 `{errorCode, errorMessage}`；单次调用受所配置的超时（≤ 300 秒）与 6 MB 载荷限制。**控制台不管理其 IAM**：平台在批量与在线运行中作为 `evaluationExecutionRoleArn` 传入的评估执行角色需要对该函数拥有 `lambda:InvokeFunction` + `lambda:GetFunction` 权限，且函数的资源策略须允许 `bedrock-agentcore.amazonaws.com` 主体（用 `aws:SourceAccount` / `aws:SourceArn` 收窄）。创建时两者都不做检查——针对角色无法调用的函数发起运行会像任何评估器错误一样按会话失败。

## 控制台评估运行 API / Console Evaluation Runs API

`/api/eval/runs` 通过有界运行队列(`eval_max_concurrent_runs`,上限为账户 5 个活跃批量评估的配额)驱动批量评估 / insights 分析。运行状态:`queued → invoking → waiting → evaluating → completed | failed | stopped`。每一行都带 `stop_requested`(操作员已请求停止,批次仍在 STOPPING)。

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/api/eval/runs?limit&offset&mode&agent_id` | 最新在前的分页 `{runs, total, limit, offset}` |
| `GET` | `/api/eval/runs/{run_id}` | 单个运行(分数 / insight 树 / `batch_eval_id` / `error` / `stop_requested`) |
| `GET` | `/api/eval/runs/{run_id}/results` | **终态评估器运行的逐会话评审记录** —— `{available, sessions: [{session_id, results: [{evaluator_id, level, score, label, explanation, error_type, error_message}]}], count, truncated}`。账本行只保存每个评估器的平均分;每个分数背后评审模型给出的理由只存在于批次自己的结果日志流中(`GetBatchEvaluation.outputConfig.cloudWatchConfig`,与在线评估视图读取的 `gen_ai.evaluation.result` 记录同族),该路由按需读取、绝不持久化。会话顺序与运行的 `session_ids` 一致。无可读内容时返回 `available=false` + `reason`(`insights_run` \| `no_batch` \| `run_active` \| `stream_missing` \| `unreadable` + `detail`)而非错误。不存在 → 404 `run.not_found` |
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

## 控制台可观测 API——会话即时评分 / Console Observability API

可观测会话详情(`/observability?session=<id>`)可以用 AgentCore 数据面 `Evaluate` API
**立刻**对会话打分。这是批量运行(异步、持久化、按数据集 / 会话 id / 时间窗口取范围)与
在线评估(抽样、持续)之外的第三种评分模式:

| 模式 | 调用 | 时延 | 结果存放 |
|---|---|---|---|
| 批量运行 | `StartBatchEvaluation`(`POST /api/eval/runs`) | 分钟级,轮询 | AWS 结果日志组 + 台账 `EvalRun` |
| 在线 | `CreateOnlineEvaluationConfig`(`POST /api/eval/online`) | 持续,judge 延迟约 10 分钟 | AWS 结果日志组,按会话读回 |
| **即时** | **`Evaluate`(`POST /api/observability/sessions/{id}/evaluate`)** | **同步,每个 evaluator 一次 judge 推理** | **仅响应体——不持久化** |

| Method | Path | Body / Result |
|---|---|---|
| `POST` | `/api/observability/sessions/{session_id}/evaluate` | Body `{evaluator_ids: string[](1..5,`Builtin.*` / `ThirdParty.*` / 自定义 id), range?: "1h"\|"6h"\|"24h"\|"7d"(默认 24h)}`。先用一条覆盖两种遥测布局的 Logs Insights 查询取回该会话的原始 span 记录(`filter ispresent(scope.name) and attributes.session.id = "<id>" \| fields @message \| sort @timestamp asc \| limit 2000`,非 JSON 行跳过),再按 evaluator 逐个、顺序调用 `evaluate(evaluatorId, evaluationInput={sessionSpans})`(每次 ≤10 条结果)。返回 `{session_id, range, span_count, results[{evaluator_id, evaluator_name, evaluator_arn, value, label, explanation, span_context{sessionId,traceId?,spanId?}, token_usage{input,output,total}, error_code, error_message}]}`。带 `error_code` 的结果是该 evaluator 的**部分失败**(仍返回该行,请求仍为 200)。仅会话级:没有 `evaluationTarget`,也没有 ground truth 参考输入。 |

错误:`observability.session_spans_missing`(409——所选范围内尚无该会话的 span 记录;
`detail.hint` 说明调用完成后 span 需要几分钟才会落地)、`observability.too_many_evaluators`(422)、
标准的 `validation.invalid_request`(422——超过 5 个 id、空列表、范围或 id 形状不合法)、
`aws.validation`(400——AWS 对不支持的 span 返回的 `ValidationException`)、
`observability.query_failed`(502——Logs Insights 失败/超时)。结果**绝不写入台账**;
可随时重跑(每次运行按 evaluator 各计一次 judge 推理)。

## 控制台 Skill Lab API——评估结果 / Console Skill Lab API

Skill Lab 评估详情页（`/skill-lab?view=eval&job=<id>`）从 CLI 的 `out/results.json` 读取一个
作业的逐任务评审行；权威来源是这个文件而不是台账，每次请求都会重新读取。路由本身不变，
其响应新增了一层经过校验的 token 用量投影。同一张表也收录了保存审阅后 taskgen 结果的两条路由。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/skill-lab/jobs/{job_id}/results` | eval 作业返回 `{summary, rows[]}`（taskgen 作业则返回 `{type: "taskgen", count, tasks, summary}`）。`summary` = `{tasks, passed, invalid, pass_rate, soft_mean, duration_s, judge_prerequisite_missing[], token_usage}`；每行 = `{id, task_type, hard, soft, score_valid, duration_s, judge_status, judge_reason, judge_error, error, judge_prerequisite, response（摘录）, artifacts[{path,size}], usage, judge_usage, token_usage}`。不检查作业状态：CLI 一写出文件（在进程退出之前）就会返回；此前返回 `404 skill_lab.results_pending`（对在评分阶段之前就结束的作业，这也是最终答复）。 |
| `POST` | `/api/skill-lab/jobs/{job_id}/import-taskset` | 把一个**已成功的 taskgen** 作业生成的任务保存为新的 single 模式任务集。请求体 `{name, tasks?}`；`tasks` 是审阅后的选择——`[{index, id?, question?, rubric?, task_type?}]`（最多 `MAX_TASKS_PER_SPLIT` 项），`index` 是该行在作业 `generated_tasks.json` 中的位置（严格整数，不接受 bool/字符串/浮点强转），四个可选字段是唯一接受的作者编辑；未知键（如 `files`、`attachments`）返回 `422 validation.invalid_request`。未出现在 `tasks` 中的行被排除，省略的字段保留生成值，`task_type: ""` 清除该字段；服务端从作业快照重建 `files`/附件。省略 `tasks` 或传 `null` → 原样保存全部生成行（旧行为）。`201 {job, taskset}`。所有错误都发生在任何写入之前：`400 skill_lab.not_a_taskgen_job`、`409 skill_lab.job_not_finished` / `skill_lab.already_imported` / `skill_lab.results_missing`、`422 skill_lab.taskgen_empty_selection`（`tasks` 为空）、`422 skill_lab.taskgen_bad_selection`（索引越界或重复）、`422 skill_lab.taskgen_duplicate_id`（编辑后 id 不唯一）、`422 skill_lab.taskset_invalid`（校验器子进程，例如不安全的 id）、调用方 workspace 之外返回 `404 skill_lab.job_not_found`。 |
| `POST` | `/api/skill-lab/jobs/{job_id}/apply-expansion` | 把一个**已成功的扩展**作业生成的任务追加到其目标任务集/分割。请求体可选：`{tasks?}`，选择的形状与规则同 `import-taskset`；无请求体或无 `tasks` 时追加全部生成行。**编辑后的** id 会对照目标任务集当前的每个分割重新检查（`409 skill_lab.expansion_conflict` 列出冲突的 id），其他分割保持不变，写入为经过校验的全量替换。`200 {job, taskset}`；`400 skill_lab.not_an_expansion_job`，以及与导入相同的 `409`/`422`/`404` 族。两条路由都不会改动作业的 `generated_tasks.json` 与附件快照。 |
| `GET` | `/api/skill-lab/jobs/{job_id}/artifacts?path=` | 作业的 `out/` 目录树，不限状态。目录 → `{kind: "dir", path, dirs[], files[{name, size}]}`；文件 → `{kind: "text", path, size, truncated, content}`（UTF-8，`content` 上限 512 KB，超出部分以 `truncated: true` 标记）或 `{kind: "binary", path, size}`（含 NUL 字节／无法解码）。尚未创建 `out/` 的作业（排队中，或 CLI 尚未写出任何内容的运行中作业）返回**空的根目录列表**而不是错误；不存在或已消失的子路径返回 `404 skill_lab.artifact_not_found`。绝对路径、`~`、反斜杠、NUL，以及（含符号链接）解析到 `out/` 之外的任何路径返回 `400 skill_lab.bad_path`。 |
| `GET` | `/api/skill-lab/jobs/{job_id}/artifacts/raw?path=` | 以下载形式返回文件的精确字节（`Content-Disposition` 带文件名），永不截断；目录或不存在的文件返回 `404 skill_lab.artifact_not_found`，同样受 `400 skill_lab.bad_path` 守卫。两条路由对调用方 workspace 之外的作业均返回 `404 skill_lab.job_not_found`。 |

**`token_usage`（新增）。** 逐行：`{target: <record>, judge: <record>}`，其中 record 为
`{status: "reported"|"missing"|"malformed", input, cache_write, cache_read, output,
unattributed}`——每个计数是整数或 `null`。原始生产者字段 `usage` / `judge_usage` 原样保留在行上；
仅当文件里带有 `NaN` / `Infinity` 字面量（`json.loads` 会接受它们）时，这些值以字符串 `"nan"` /
`"inf"` / `"-inf"` 输出以保证可序列化，文件本身绝不改写。
summary 上：`{scope: "reported", target: <side>, judge: <side>}`，其中 side 为
`{rows, reported_rows, missing_rows, malformed_rows, reports_complete, complete, input,
cache_write, cache_read, output, unattributed, counter_rows{<counter>: n},
counter_complete{<counter>: bool}}`。

语义：`null` 表示*未知*（没有任何一行上报该计数），绝不是零。评审生产者只上报 `input`/`output`，
因此评审侧的 `cache_*` 恒为 `null`。`unattributed` 是 transcript 只以 `total` 形式上报、超出分项
之和的部分（codex 形态——当所有分项都是正总数之下的零占位符时，分项按 `null` 上报）；只上报了
`total: 0` 也算一次上报（`unattributed: 0`，分项为 `null`），不算缺失。格式异常的计数（bool、
负数、NaN/inf、小数、非数值）会被丢弃而不是折算；该行其余有效计数照常计入，且该行计入
`malformed_rows`。score 无效的行（`score_valid: false`）用量照常求和，但不进入 `pass_rate` /
`soft_mean`。完整性分两种：`reports_complete` 是上报覆盖度（`reported_rows == rows` 且没有格式
异常行）；`complete` 是拆分完整性（上报完整，且凡有任何一行上报过的计数都被每一行上报了）。只有
部分行上报的计数是**部分求和**：`counter_rows[k] < rows`、`counter_complete[k] == false`，控制台
在该单元格标出 `k/n`（例如一行 claude 加一行 codex 仅总数，`input` 只来自 2 行中的 1 行，即便两行
都已上报，`complete` 也为 false）。没有任何一行上报的计数是未知，本身不会让拆分变成部分。`scope`
恒为 `reported`：这是对上报了用量的任务的观测统计——不是计费总额，也不做任何费用估算。在用量
采集之前写出的旧结果，每一侧都表现为 `missing_rows == rows` 且计数全为 `null`。

## 控制台账户 API / Console Accounts API

`/api/auth/*` 守住控制台入口，`/api/users/*` 管理其背后的账户。两组接口都不触碰 AWS。详见
[architecture.zh-CN.md](architecture.zh-CN.md)「控制台认证与账户」一节。

| 方法 | 路径 | 鉴权 | 结果 |
|---|---|---|---|
| `GET` | `/api/auth/status` | 开放 | `{auth_required, authenticated, registration_enabled, registration_requires_approval, username, role, email, account_expires_at, permissions}`——身份字段在认证前为 null（`permissions` 为 `[]`） |
| `POST` | `/api/auth/login` | 开放 | 设置 `launchpad_session` cookie（12 小时，且不超过账户有效期）并回显身份 |
| `POST` | `/api/auth/register` | 开放 | `201`——创建一个 `member` 账户；默认 `status=pending` 且 `expires_at=null`，直到管理员批准，之后有效期为 `auth_registration_valid_days`（默认 7 天） |
| `POST` | `/api/auth/logout` | 会话 | 清除 cookie |
| `GET` | `/api/users?q=&status=all\|pending\|active\|expired\|disabled&limit=&offset=` | 管理员 | 分页账户列表，带派生的 `state` / `days_remaining` |
| `GET` | `/api/users/stats` | 管理员 | 汇总数据，包括 `pending` 审批队列、`expiring_soon`（≤3 天）、7 天内的注册/登录计数、14 天注册序列、邮箱域名排行 |
| `PATCH` | `/api/users/{id}` | 管理员 | 以下任意字段：`status`（`pending`\|`active`\|`disabled`；对待审批账户设为 `active` 即批准并启动其有效期）、`role`、`extend_days`、`expires_at`（`null` = 永不过期）、`password`（`null` = 生成并一次性返回）、`permissions`（`{permission_key: bool}`，`null` = 全部授予）、`workspaces`（整体替换该账户的 workspace 授权，`null` 清空） |
| `DELETE` | `/api/users/{id}` | 管理员 | 删除该账户 |

注册错误码：`auth.registration_disabled`（400，认证门未开启或注册已关闭）、
`auth.invalid_username` / `auth.invalid_email` / `auth.email_domain_blocked` / `auth.weak_password`（400）、
`auth.username_taken` / `auth.email_taken`（409）。

登录错误码：`auth.invalid_credentials`（401），以及在提交的凭据本身正确之后的
`auth.account_pending` / `auth.account_disabled` / `auth.account_expired`（401）。

会话与角色错误：`auth.required`（401——cookie 缺失、被篡改或已过期，也包括账户此后被禁用、过期或删除）、
`auth.forbidden`（403——member 会话访问 `/api/users*`）、`users.not_found`（404）。
