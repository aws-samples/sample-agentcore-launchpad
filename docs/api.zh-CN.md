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

**密钥生命周期。** 每个密钥记录创建它的控制台账号（`created_by` = 其 `users.id`，另有
`created_by_name`；在此之前创建的密钥两者均为 `null`，控制台显示为“未知”）以及
`last_used_at`（`/v1` 调用成功时写入，每分钟最多一次）。密钥只在其创建者仍有资格持有时有效：
一旦该账号被 **删除、停用或过期**，或（成员账号）**失去** 密钥所属工作空间的 **授权**，
该密钥返回 `401 auth.invalid_api_key`（与任何不可用密钥相同；`/v1` 从不说明原因）并被置为停用。
管理员停用 / 删除账号以及移除授权会立即停用创建者的密钥；过期没有写入事件，因此在密钥下一次被使用时生效。
失效只会停用、从不删除——这些密钥仍列在 `GET /api/apikeys` 中（`created_by`、`created_by_name`、
`last_used_at`、`creator_offboarded`：`deleted` | `disabled` | `expired` |
`no_workspace_grant` | `null`）；只有控制台显式的 `DELETE /api/apikeys/{id}` 才会删除一行。
对创建者已失效的密钥调用 `POST /api/apikeys/{id}/enable` 会被拒绝，返回 `409 apikey.creator_offboarded`
（`detail.reason`）。**成员规则（CN）：** 成员只能列出、切换与删除自己创建的密钥——其他成员的密钥返回
`404 apikey.not_found`——管理员则可操作工作空间内的每个密钥，包括创建者为 NULL 的旧密钥。由配置内置管理员
或未开启鉴权时的本地操作者（没有 `users` 行）创建的密钥，以及旧密钥，永远不会被判定为创建者失效。
请求体中携带 `created_by` / `created_by_name` 会被拒绝（422）。过期时间、权限范围与 Agent 白名单尚未实现。

**时间(契约说明，N17-obs463，2026-10-26)。** API 返回的每个日期时间——`/v1` 与控制台 `/api`
一样，JSON 响应体与流式事件均包括——都是部署显示时区(`display_timezone`，默认 `Asia/Shanghai`)
下的 ISO 8601，例如 `"2026-10-24T18:29:54.079198+08:00"`。它与之前是 **同一时刻**：此前同一个值以
无时区的 UTC(`"2026-10-24T10:29:54.079198"`)或 `+00:00` 返回。请解析偏移量(任何 ISO 8601
解析器都支持)，需要 UTC 时再换算，切勿去掉偏移量。只有平台自己的时间戳字段会被换算(`*_at`、
`*At`、`at`、`ts`、`time`、`timestamp`、`start` / `end`、`since` / `until`、`first` / `last`、
`bucket` / `bin` 等)。由你或 Agent 写入的内容原样返回，即使它是一个日期时间字符串：数据集条目与
场景、方案内容、对话消息文本与流式文本增量、工具参数与结果、span 属性、记忆 / 知识库元数据、
评估器配置、Agent spec。存储、日志与 AWS 仍为 UTC。`GET /api/capabilities`
给出该时区(`display_timezone`)。调用方发送的时间可带任意偏移量(`Z`、`+00:00`、`+08:00`)，
平台会换算而不是改写标签。

## 同步调用 / Sync invoke

```bash
curl -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke \
  -H "X-Api-Key: $LP_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "What is 2+2?", "session_id": null}'
# → {"agent":"…","text":"4","session_id":"…","latency_ms":1234}
# 可选字段 "truncated": true —— 回合在模型 token 上限处被截断时出现（N-SCN-bug4），完整回答时不出现
```

每一次 `/v1` 调用（同步与流式）都会像控制台回合一样写入会话账本，因此该会话会出现在控制台的
Chat 会话列表、历史与可观测性中，并标记 `source: "public_api"`（N-SCN-obs1）；行内保留请求的
`actor_id`（默认 `api`）。

调用方自带的 `session_id` 必须为 **33–100 个字符**（AgentCore Runtime `runtimeSessionId` 的
长度约束）。不满足时在 `/v1/agents/{id}/invoke`、`/v1/agents/{id}/invoke-stream`、
`POST /api/chat/{id}` 与控制台测试调用上一律前置拒绝，返回
`422 chat.session_id_invalid`（`detail {length, min_length: 33, max_length: 100}`）——在策略网关
记录决策之前、在任何 AWS 调用之前（N-SCN-obs16）。N16-obs72：id 还必须匹配
`[a-zA-Z0-9][a-zA-Z0-9-_]*`（botocore 会话 id 形状；含 CJK 或空白的 id 此前能通过长度校验，却在 AWS 侧
以 SigV4 签名不匹配失败）——同一 422，`detail` 多出 `pattern`；按 id 的聊天路由（history / memory /
stop / 重新接入）在读取台账之前同样做字符类校验。省略该字段（或传 `null`）即开启新会话：
平台生成的 id 为 64 个字符，不受此校验影响。同理，`AgentSpec.env` 最多接受 22 个变量：Runtime
`environmentVariables` 上限为 50，平台为自身键预留 28 个（`deployer/environment.PLATFORM_ENV_KEYS`；
N16-obs90 的 `LAUNCHPAD_A2A_ACTOR_KEY` 使预留从 27 变为 28，成员预算因此从 23 变为 22）；
`422 validation.invalid_request` 会点明这一预算，而不是跑完整条流水线后才收到 ValidationException；`generate` 阶段还会在任何 AWS 调用之前推导合并后的环境（spec + 平台键），超过 50 时以 `agent.env_budget_exceeded {user, platform, limit}` 使任务失败。

**N16-obs88：**`session_id` 对应的聊天会话已被结束（`POST /api/chat/{id}/sessions/{sid}/stop`）时，
`/v1/agents/{id}/invoke` 与 `/v1/agents/{id}/invoke-stream` 与 `POST /api/chat/{id}`（N16-obs79）完全一致地返回
`409 chat.session_ended {session_id, ended_at}`——在策略网关与任何 runtime 调用之前、在流打开之前以普通响应返回，
台账行不被触碰（标记保留；此前 `/v1` 入口会重新打开该会话）。该门禁位于共享的调用链中
（`services.invoke.require_session_open`），因此两个入口不可能不一致；请改用新的 `session_id`（或不带 id）继续。

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

流中还可能出现 `tool`(`{name, id}`,一次工具调用开始)、紧随其后的新增摘要事件 `tool_input` / `tool_result`(N16-obs85,见“控制台 Chat API”;`{id, name, input ≤ 512 字符}` / `{id, status, summary ≤ 256 字符}`),以及托管 Harness 的
`policy_denied`——Cedar 策略拒绝了一次 Gateway 工具调用:
`{tool, tool_use_id, reason, policy_id, gateway_id}`(经登录用户的 Gateway 别名调用时,
`tool` 为目录名 `<target>___<tool>`,`gateway_id` 为工作区网关;否则为 Harness 工具名与
`null`;仅当原因中指明判定策略时才有 `policy_id`)。智能体自己的回答仍会继续输出;
客户端可忽略这两类事件。

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
`auth.missing_api_key`(401)、`auth.invalid_api_key`(401——未知、已停用或创建者已失效的密钥)、`agent.not_active`(409)、`agent.not_found`(404)、
`agent.runtime_error`（502——**N17-obs95**：运行时的流以 AgentCore SDK 的终止错误帧 `{"error", "error_type", "message"}` 结束——Agent 内部的模型超时、限流或校验错误、过期的 bearer、单体 `{"error": …}` 拒绝、残缺帧或结构化的 `REJECTED` 响应体——发生在**同步**入口 `/v1/agents/{id}/invoke` 与控制台 `POST /api/agents/{id}/invoke` 上时（这里没有流可以承载 Chat 的 `error` 事件）；`message` 为该帧有界且脱敏后的文本 `runtime returned error: <error_type>: <error>`，`detail` 为结构化的 `{error_type, error, message}`；不写任何账本，会话的轮次数不变）、
`agent.empty_answer`（502——**N17-obs114**：运行时结束了却没有产出任何可展示的内容——空的 `complete` / `{"result": ""}` 响应体（实测形状：BYOC 的模型把全部 token 预算花在推理上、没有返回文本块），或没有任何帧就结束的流。同步入口返回错误信封而不是 `text: ""` 的 `200`：运行时跑了却没有回答，调用方必须能把它与成功的一轮区分开——在这个协议上，无话可说的运行时会输出 `error` 或提示，绝不会沉默。Chat 中同样的情况以带该错误码的 `error` 事件结束这一轮，控制台因此显示一条错误行，而过去什么都不显示；流出过工具卡片、授权请求、策略卡片或 `truncated: true` 提示的一轮不算空）。
密钥校验返回的每个 401 都会按调用方源 IP 计数：在 `LAUNCHPAD_AUTH_API_KEY_IP_WINDOW_SECONDS`（默认 300）内累计 `LAUNCHPAD_AUTH_API_KEY_IP_LIMIT`（默认 20）次后，返回 `auth.rate_limited`（429，带 `Retry-After` 头与 `detail.retry_after` 秒数）——仅针对*之后的无效*尝试，直到窗口空出；有效密钥从不计数、也从不被该窗口拒绝（在 nginx 或 NLB 之后所有调用方共用一个对端地址，因此该窗口只是粗粒度的辅助控制）。设为 `0` 可关闭该窗口。

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

**传输层失败（N17-obs94）。** AWS 根本没有应答的调用不是 `ClientError`，上表看不到它；自
`cn/r17-o` 起 botocore 传输层异常族有自己的处理器，同样**绝不会是裸 500**：超时（`ReadTimeoutError`、
`ConnectTimeoutError`——请求已发出、限时内无回应）为 **504 `aws.timeout`**；无法建立或被中断的连接
（`EndpointConnectionError`、`ConnectionClosedError`、`SSLError`、`ProxyConnectionError`、
`ResponseStreamingError`、`IncompleteReadError` 等）为 **502 `aws.unreachable`**。控制台与 `/v1`
的 `message` 都是固定句子（`AWS did not answer in time` / `AWS could not be reached`）——botocore
原文内嵌端点 URL，即运行时 ARN——`detail` 为 `{"error_type": "<botocore 类名>"}`，原文只进后端日志。
调用（invoke）遇到它时，调用方无法判断运行时是否已跑完这一轮（长轮次后的 504 通常意味着已跑完）：
请换新会话重试或读取历史。其他 `BotoCoreError`（`ParamValidationError`、`NoCredentialsError`）属于
编程/配置错误，仍是上文所述的未处理 500。

## 健康检查 / Health

| 方法 | 路径 | 鉴权 | 响应 |
|---|---|---|---|
| `GET` | `/api/health` | 公开（无需会话） | `{status: "ok", version, region, build{sha, short, built_at, source, dist_sha}}`——`build`（N13-obs10，增量字段）为当前部署的提交：`sha` / `short`（8 位）/ `built_at`（ISO-8601 UTC）/ `source` = `env`（`LAUNCHPAD_BUILD_SHA` [+ `LAUNCHPAD_BUILD_AT`]）\| `file`（`data/build-info.json`，由 `scripts/host-update.sh` 第 7 步每次更新时写入）\| `dist_stamp`（`frontend/dist/.launchpad-build`，`built_at` 取其 mtime）；主机没有任何构建戳时各键均为 null。V2 侧栏据此显示「构建 <short>」，演示视频的 `recorded_version`（E01-obs1）可直接与运行中的控制台核对。`dist_sha`（N17-obs466，增量字段）为当前**所服务的**控制台前端包的构建提交：取 dist 戳，否则取 `data/build-info.json` 的 `dist_built_from`，否则为 null（从不取环境变量 / HEAD 的 sha：`frontend/` 未变时 host-update 保留 `dist/`，仅后端更新时 `sha` 会变而前端包不变）。已打开的 V2 页面将其与自身编译进的 sha 比较，不一致时显示不阻塞的新版本横幅与「刷新」按钮（从不自动刷新）。obs466 后续：侧栏「构建 <short>」显示**本页**前端包的 sha（仅开发服务器页面回退为部署的 sha），提示为「本页 … · 服务器 <dist_sha> · 部署的提交 <sha>，时间 <built_at>」，与横幅判定一致时标注「有新版本」。控制台（30 s 轮询）、nginx / NLB 目标组与 host-update 第 14 步均轮询此接口 |

## 控制台系统 Agent API——托管预置 / Console System Agents API

系统托管预置（见架构文档“系统托管预置”）只能在这里安装、修复与移除。读取不触达 AWS；
安装是显式、计费的操作员动作，走标准部署管道。

| 方法 | 路径 | 角色 | 结果 |
|---|---|---|---|
| `GET` | `/api/system-agents` | 成员 | `{workspace_id, presets[{key, name, label, description, method, skill_version, installed_skill_version, update_available, status, requirements[{code, message}], name_collision, agent_id, agent_status, error, job_id, deployment_id, deployment_status, model_id, model_source, model_origin, default_changed, knowledge_bases[], allowed_tools[], memory, settings{model_id, model_source, max_tokens, reasoning_effort, system_prompt, max_iterations, timeout_seconds, knowledge_bases[]} | {}, defaults{同样成员}, editable_fields[], operation, catalog_version{installed, current}, snapshot_behind, can_install, can_repair, can_configure, can_uninstall, updated_at}]}`——`status ∈ configuration_required | not_installed | deploying | uninstalling | active | failed`；`model_origin ∈ default | override | null`（N17-obs202：`default` = 已存 `model_id` 是上次安装 / 修复时的 `agent_default` 模型角色——预置跟随模型默认值页；`override` = 管理员通过设置编辑固定了模型；未安装时为 `null`），`default_changed` 在一个跟随角色的已安装预置的已存模型不等于角色当前解析值时为 true（覆盖永不为 true）——`defaults.model_id` 始终是角色当前值；`catalog_version.installed` 为最新一次**未失败**部署固定的模型目录版本——即运行时实际提供的快照（尚无此类部署时为 `null`；失败的重新发布所固定的版本会被忽略，N17-obs259），`snapshot_behind`（N17-obs234）在该固定版本早于台账目录版本时为 true——与模型提供方页的 `preset_snapshot_behind` 横幅同一判定；拆除任务持有该行时 `operation` 为 `{kind: uninstall, job_id, job_status, attempt, error, retryable}`，否则为 `null`；条件 code 为 `bootstrap_not_ready | missing_artifacts_bucket | missing_execution_role | per_agent_roles_disabled | missing_oauth_provider`；`memory` 为 `disabled`；仅读台账 |
| `POST` | `/api/system-agents/{key}/install` | 管理员 | 必需的 JSON 请求体，为局部编辑：`{model_id?, model_source?, max_tokens?（1–131072，单次模型调用 = bedrockModelConfig.maxTokens）, reasoning_effort?（low|medium|high；仅 model_source=bedrock 上的 OpenAI 模型）, system_prompt?, max_iterations?（1–100）, timeout_seconds?（10–3600）, knowledge_bases?[{kb_id, name?, description?}], reset?[可编辑字段…], clear?["max_tokens"|"reasoning_effort"], force?}`（`{}` = 安装时预置默认值，修复时已存选择；省略 = 保持，`reset` = 构建默认值，`clear` = 不发送；未知成员 → `422`；N17-obs202：`default_changed` 为 true 的跟随型预置在无请求体 `{}` 修复时按 `agent_default` 角色重新解析 `model_id` 并重新发布（`202`，`repair_reason: "model default changed <old> -> <new>"`）；请求体中显式的 `model_id` 固定模型（`model_origin: override`，之后每次修复都保留，直到 `reset: ["model_id"]` 让其重新跟随角色））→ 有任务在途时 `202 {agent, job_id, deployment_id, created, changed, preset}`（对部署中预置的无请求体修复返回其既有任务且 `changed: false`；部署中的显式编辑 → `409 system_agent.deploy_in_progress {job_id}`；竞争安装返回胜出方的任务，但若请求了不同设置则返回 `409 system_agent.deploy_in_progress`；局部编辑在声明事务内针对当前行解析，声明以版本为条件，行变化后重新解析，三次仍失败返回 `409 system_agent.conflict`），运行中的预置已匹配时 `200` 并带上一任务的 ID——`snapshot_behind` 为 true 的运行中预置永不视为「已匹配」：无请求体的 `{}` 会像 `force` 一样重新发布（`202`，任务载荷 `repair_reason: "catalog snapshot behind v<installed> < v<current>"`，N17-obs234）；不支持的组合（如给 Claude 模型设置推理强度）返回 `422 system_agent.invalid_options`。知识库在 provision 阶段于目标 Workspace 中核验 |
| `POST` | `/api/system-agents/{key}/skill-registration` | 管理员 | 无请求体（资源由服务端选定：已存 spec 固定的版本，与本构建比对并从 S3 回读）→ `200 {preset_key, record{system, record_id, name, type: AGENT_SKILLS, status, version, descriptors, …}, created, changed, submitted, note, skill{name, version, digest, path, files[]}, preset}`——把**运行中**预置已发布的 Skill 版本注册为独立的 Registry 记录，指向不可变的 `system-skills/<name>/<version>-<digest12>/` 前缀（不写 S3，不重新发布 Harness，Agent 的 A2A 记录不动）。`created` ⇒ 新记录已提交审核（此处绝不批准）；`changed` 而非 `created` ⇒ 描述符前滚到更新版本（DRAFT，重新审核，`recordVersion` 递增）；两者皆否 ⇒ 版本相同、空操作（保留批准）。幂等且竞争安全（持久 `clientToken`、按预置加锁）；部署管道的 register 阶段在安装/修复时执行同样的注册 |
| `DELETE` | `/api/system-agents/{key}` | 管理员 | `202 {agent, job_id, operation: uninstall, attempt, started, preset}`——在一次提交中声明该行（`uninstalling`，乐观条件更新）并排入拆除任务；同时到达的请求共享一个任务（落败方 `started: false`）；独占（按 Agent 的建议锁，单主机）且带围栏的 worker 核实每个知识库目标、Harness 与专用角色均已消失之前（任务上有含精确资源 ID 的逐步 `progress`，仅在已核实时继承到下一次尝试）该行保留身份；拆除失败则进入第 N+1 次尝试；部署进行中返回 `409 agent.deploy_in_progress`。当一个状态尚未终结的 Runtime 金丝雀仍以该预置为 arm 时返回 `409 agent.in_use {canaries}`（N16-obs111a；创建路由已拒绝预置作为 arm，只有遗留行会触发）——先结束金丝雀再卸载 |

`GET /api/system-agents` 另外报告 `release`（本构建对该预置的发布版本 `{version, digest, tag}`，不可用时为
`null`）与 `release_error`（N-ARCH-obs19：`release` 为 `null` 的原因——本构建无法按已发布的模型目录校验或
渲染该预置的技能包；可用时为 `null`）。在这样的构建上 `POST /api/system-agents/{key}/install` 返回
`409 system_agent.bundle_invalid {preset_key, skill_version, catalog_version, reason}`，携带同一条消息，
且发生在任何行、任务或 AWS 调用之前——绝不会是 500。

`GET /api/system-agents[].last_job` 另外携带 `trigger`（`auto` = 主机的自动修复巡检，`null` = 卡片或
API 操作）与 `repair_reason`（N17-obs206）：主机会在**所有** Workspace 中自行重新发布每个 `_repair`
判定为落后的已安装、运行中预置——启动后 60 秒，以及最后一次影响目录或模型角色的注册表写入后 180 秒
（去抖），逐个声明并部署，上次重新发布失败的预置留给管理员处理（`system_presets_auto_repair`，
model-registry.md §9）；预置面板把运行中任务为
`trigger: auto` 的 `deploying` 卡片显示为「自动修复中」。

`GET /api/system-agents` 另外报告 `skill_registration`（`{record_id, pending_record_id, status: creating |
accepted | registered, release_version, release_digest, path, updated_at} | null`，仅读台账；`accepted`
表示我们的创建返回了 `pending_record_id` 但回读尚未核验，`record_id` 在核验前保持 `null`；当前 Registry
中没有映射时为 `null`）与 `can_register_skill`。
Registry 记录（`GET /api/registry/records[/{id}]`、搜索、action/update/reimport 的响应）携带服务端
推导的 `system` 成员——`{managed: true, preset_key, label, skill_version, release_digest, path,
protected_actions[], admin_actions[]} | null`——当且仅当 Workspace 台账在其**当前 Registry** 中把该记录（已核验或已接受的 id）映射到系统预置的 Skill
时设置；绝不从描述符或标签读取。对这类记录，`PUT`、`POST …/reimport` 与 `DELETE` 对所有调用方返回
`403 registry.system_skill_protected`，`POST …/action` 对成员返回同样错误（管理员可提交/批准/驳回/停用），
以保留名称进行普通 Skill 注册/导入返回 `409 registry.name_reserved`；平台示例技能的名称（`expense-report-writer`）对所有调用方返回 `409 registry.sample_name_reserved`（由默认同步注册）；在名称检查与占用之间出现的同名记录返回 `409 registry.name_exists` 且不上传任何文件——记录在任何文件写入 `skills/<name>/` 之前先被占用（N17-obs265）。每条记录还携带创建者——`created_by`（`users.id`；部署创建的 A2A 记录、Gateway 导入、`sync-defaults`、配置管理员与历史行为 `null`）与 `created_by_name`——在 `POST /api/registry/records` / `POST …/skills/import` 时写入；`PUT`、`POST …/reimport` 与 `DELETE` 仅限创建者或管理员（`403 registry.forbidden {created_by, created_by_name, record_id, action}`，`null` 创建者视为管理员的记录；N15-obs7），读取对所有成员开放（users-and-ownership.md §8）。`POST /api/registry/records/{id}/action`（`{action: submit | approve | publish | reject | disable}`）：能看到该记录的任何成员都可以 `submit`；`approve`、`publish`、`reject` 与 `disable` 是管理员的决定——成员调用时在可见性检查与任何后端调用之前返回 `403 registry.approval_admin_only {action}`（隐藏、可见与不存在的记录返回相同结果），状态不会改变（N17-obs248，2026-10-22）。平台自有记录（共享 Gateway 的 Target、`sync-defaults`、示例技能）仍在各自路径内由 `system` 执行者批准；此规则生效前成员已自行批准的记录保持原状。平台 Target 记录指服务器 URL 为本 Workspace 的 Gateway **且**没有任何账户创建的记录；`system` 对齐绝不接管成员创建的记录，导入时把它报告为 `registry.name_held`（N17-obs256）。示例技能遵循同样的来源规则：只有没有创建者的记录才属于平台（N17-obs262）。`POST /api/registry/records` 与 `PUT …/{id}` 拒绝等于本 Workspace 平台 Gateway URL 的 MCP `url`（忽略协议与主机大小写、末尾斜杠及查询串），对所有调用方返回 `422 registry.platform_gateway_url_reserved`——这类记录只来自导入路径。Skill 注册错误码：
`system_skill.preset_not_active`（409）、`system_skill.release_mismatch`（409，本构建的技能包不是
已安装版本）、`system_skill.bundle_unverified`（409，S3 已发布版本与快照不一致；未写入）、
`system_skill.foreign_record`（409，平台无法证明由自己创建的同名 Skill 记录——包括服务不予兑现的
丢失创建重放；不绑定）、`system_skill.stale_release`（409，调用方版本旧于已安装/已核验/意图/远端版本，
或同版本号不同摘要）、
`system_skill.record_deprecated`（409，终态——在 AWS 中删除后重新注册）、
`system_skill.readback_mismatch`（409）、`registry.unavailable`（503）。

错误码：`system_agent.unknown`（404）、`system_agent.workspace_not_ready`（409，
`detail.requirements[{code, message}]`）、`system_agent.name_collision`（409，普通 Agent
占用保留名称——绝不接管）、`system_agent.not_installed`（卸载不存在的预置时 404）、
`system_agent.uninstalling`（拆除任务持有该行或上次尝试失败时，安装/修复返回 409，
`detail.job_id/job_status/error`）、`agent.deploy_in_progress`（部署中卸载时 409）。普通 Agent 路由上，预置在任何 AWS 调用之前返回
`agent.system_managed`（403，`detail.action ∈ redeploy | delete | convert |
canary | promote | …`，`detail.maintenance_route`）；运行时金丝雀的 action 路由对引用预置的
记录返回同样的错误；`DELETE /api/knowledge-bases/{kb_id}` 在知识库挂载于预置时返回
`kb.attached_to_system_agent`（409，`detail.agents`），无论是否 `force`；用保留名称
`POST /api/agents`（以及架构师审批）返回 `agent.name_reserved`（422，`detail {name, reserved_for}`——预置名、其基础名及所有 `<基础名>-<后缀>`，如 `launchpad-eval-worker`；N16-obs82，`docs/cn/interfaces/agents.md` §3）。**创建期校验（第 16 轮，N15-obs16）。**`POST /api/agents`、每次重新发布
（`POST /api/agents/{id}/redeploy`、推荐采纳）与架构师审批都会先按方式的声明域校验规格——
即该方式 generate 阶段能渲染什么（`deployer.pipeline.MethodCapabilities`，由
`GET /api/capabilities.methods` 下发）——**在任何 Agent 行、任务或 AWS 调用之前**：
`422 agent.method_unsupported {method, field, value, supported}` 对应内置工具
（`field: tools.builtin`）、该方式任何通道都不渲染的工具类型（`tools.type`，例如 `zip_runtime`
上未认证的 `mcp`）、代理模式（`tools.auth.mode`）、原生工具、协议、记忆、知识库、byoc 制品种类，
以及不在本工作区账号/区域内的容器镜像（`byoc.image_uri`）；当工作区缺少部署首阶段就需要的资源时
（`artifacts_bucket`；网关工具还需 `gateway_id`）返回 `409 workspace.resource_missing
{resource, how_to_fix}`。当规格引用了本工作区不存在的行时返回 `422 agent.reference_missing {field, value, kind, how_to_fix}`
（`knowledge_bases[].kb_id` → `kind: knowledge_base`；N16-obs73——此前一个虚构的 id 会部署为 ACTIVE
并在首次检索时失败；被隔离模式隐藏的知识库仍先返回 404 `kb.not_found`）。
`agent.builtin_unsupported`（N15-obs16 的错误码）在线上保留一轮作为别名：`tools.builtin` 的拒绝会在
`detail.legacy_code` 中携带它。成员提供的、会被模板嵌入的任何文本（提示词、描述、名称）中出现
`__LAUNCHPAD_` 子串会被 schema 以 422 拒绝并标明字段；`container_source` 制品需要工作区的
`codebuild_project`（409 `workspace.resource_missing`，与 `gateway_id` 同理）。目前两个内置工具（`code-interpreter`、`browser`）在 Harness
上均可渲染；每种方式的权威列表是 `GET /api/capabilities.methods[m].builtin_tools`（同时镜像为
`GET /api/tools` 内置工具行的 `methods` 字段）。契约：`docs/cn/interfaces/testing.md` §6。`POST /api/agents` 与
`POST /api/agents/{id}/redeploy` 的请求体是 `AgentSpecRequest`（N16-obs78）：未知的顶层键
（`frobnicate`、`system_key`、`owner` 等）返回 `422 validation.invalid_request`，`detail[].loc`
指明该键（`extra_forbidden`），与 `OnlineConfigPatch` / `UserPatch` 一致；`created_by` /
`created_by_name` 保留各自的说明——仍是 `422 validation.invalid_request`，`detail[].msg` 带
`created_by: the creator is recorded by the server and cannot be set by the client`（N17-obs116：此路由
从不返回 `validation.creator_not_settable`，那是调用 `creators.reject_client_creator` 的字典请求体路由的码）。
从台账读回的规格对已退役的键保持宽容。
`POST /api/agents/{id}/redeploy` 以编辑后的规格重新发布（名称与方式不可变→ `400 agent.redeploy_immutable`）：
返回 `202 {agent, job_id, deployment_id}`（与 `POST /api/agents` 同形），并在更新模式任务运行期间把该行翻为
`deploying`——期间 Chat 返回 `409 agent.not_active`，重新发布 / 入站认证 / 删除返回 `409 agent.deploy_in_progress`；**N17-obs16：**该 Agent 上有 `running` 的 Runtime 金丝雀时，重新发布返回 `409 agent.in_use {canaries: [{id, name, status, stage}]}`——与删除相同的信封，成员与管理员一致，不写入任何内容；先用 `cleanup` / `rollback` / `complete` 结束金丝雀（`docs/cn/interfaces/agents.md` §1.1）；
上一版本继续提供服务。`failed` 的 Agent（尚无在线版本）以同样方式重新发布（冷重新发布：deploy 阶段退回 Create；
失败则再次标为 `failed`）。已有在线版本的 Agent 更新失败时留在 `active`、继续以上一版本服务，投影带
`last_deploy_failed: true` / `last_deploy_error`（N16-obs110；`docs/cn/interfaces/agents.md` §4）。
`DELETE /api/agents/{id}` 返回 200 时在同一次提交中把该行标为 `deleted` 并释放其名称占用，因此同名的
`POST /api/agents` 再次返回 202（新 id；`409 agent.name_exists` 只针对未删除的行——agents.md §4.4）。
`DELETE /api/agents/{id}` 在该 Agent 的部署任务仍为 `queued` / `running` 时返回
`409 agent.deploy_in_progress {job_id, stage, job_status}`（N16-obs83——成员与管理员一致，无 force；
等任务成功或失败后再删除）；在一个状态尚未终结（`running`）的 Runtime 金丝雀以它为 champion 或
challenger 时返回 `409 agent.in_use {canaries: [{id, name, status, stage}]}`（N16-obs111——成员与管理员
一致，无 force；`completed` / `rolled_back` / `cleaned` 的金丝雀不会阻止；先用 `cleanup` / `rollback` /
`complete` 结束金丝雀再删除；架构师会话的清除保持自己的顺序——先金丝雀、再 Agent）。两项守卫都在任何
AWS 调用之前判定，且不触碰该行、名称占用与金丝雀（`docs/cn/interfaces/agents.md` §1、§1.1）。
`DELETE /api/agents/{id}` 的结果 `{deleted, agent_id, aws_resource_deleted, removed[], skipped[],
failed[], failed_detail{}, steps[]}` 中只有确实存在过的资源才计入 `removed`（N16-obs76）：部署从未
到达 provision 的 Agent 返回 `aws_resource_deleted: false`，`runtime` 与 `execution_role` 列在
`skipped`（原因见 `steps[].detail`），不再写成 "deleted"。**N17-obs151：**package 阶段留在本机的
`data/builds/<name>` 暂存目录是又一个残留步骤 `build_dir`（位于 `registry_record` 与 `log_groups` 之间；
`deleted` 带字节数，不存在时 `skipped`，主机拒绝删除时 `failed` 带原因——绝不阻塞删除）。**N16-obs87：**调用 `DeleteAgentRuntime` 之前先读回
Runtime——已不存在则 `runtime` 记为 `skipped`（"already gone"），台账行照常删除；控制面对已不存在 id 的删除
返回的 `AccessDeniedException` 会再读一次，Runtime 已消失则按已删除处理——只有 Runtime 仍在时的 AccessDenied
才像以前一样返回 `403 aws.access_denied`。每个 Agent 投影都带
`system: {managed, key, label, skill_version, protected_actions} | null`。
`AgentSpec.allowed_tools`（仅 Harness）接受 1–64 字符、匹配 `*|@?name(/tool)?` 的条目。
**N17-obs18：**该 Agent 上有进行中（`queued` / `invoking` / `waiting` / `evaluating`）的评估运行时，删除同样返回 `409 agent.in_use`，`detail.runs: [{id, name, status}]` 列出这些运行（`detail.canaries` 与 `detail.runs` 总是同时存在，可为空）；先停止运行或等其结束再删除（`docs/cn/interfaces/agents.md` §1.3）。运行的 worker 在调度时会重新读取 Agent：Agent 已删除则运行以 `agent.not_found` 失败，不记录任何版本。

## 控制台架构助手 API——经审阅的 Harness 提案 / Console Architect Assistant API

架构助手（见 architecture.zh-CN.md →“架构助手（SE-039）”）是成员与受保护预置
`aws-agent-solution-architect` 的对话，终点是一个新托管 Harness 的惰性提案。讨论路由为
`成员`（与对话演练场对等）；批准使用 `perm:agents.deploy`（与 `POST /api/agents` 对等），并在其
写事务内从数据库重新解析调用者的账号、权限与 Workspace 授权。所有路由按 Workspace 限定**且按
principal 绑定**（`user:<id>` / `config-admin` / `local-operator`；用户名仅用于显示）：其他
principal——其他成员、管理员、或同名重新注册的账号——的会话返回
`404 assistant.conversation_not_found`。除特别说明外读取仅访问台账。

| 方法 | 路径 | 角色 | 结果 |
|---|---|---|---|
| `GET` | `/api/assistant/architect` | 成员 | `{workspace_id, account_id, region, available, reasons[], preset{key, label, status, agent_id, requirements[], can_install}, can_deploy, deploy_requirements[{code, message}], capabilities{shared_memory, memory_kind: selfhosted|agentcore|null, knowledge_bases}, is_admin, owner, principal}`——`available` ⇔ 预置为 `active`；`capabilities` 说明提案可绑定哪些前置资源（此处绝不创建）；仅读台账 |
| `GET` | `/api/assistant/architect/conversations` | 成员 | `{conversations[{id, title, owner, mine, read_only, shared, shared_by, shared_at, turns, turn_in_progress, live_turn, status, proposal_status, proposal_revision, created_at, updated_at}]}`——调用者自己的会话，加上所有者或管理员在本 Workspace 共享的会话（`mine: false`），最新在前（≤ 50）；**管理员**列出本 Workspace 所有成员的会话（≤ 200），其他成员未共享的会话带 `read_only: true`（共享之前所有写入对其返回 404）；`owner` 为创建人的显示用户名；详情中每条 `messages[]` 带 `author`（`user` 消息的发送人） |
| `POST` | `/api/assistant/architect/conversations` | 成员 | 请求体 `{title?}` → `201` 会话详情（见下）；快照 Workspace **目录**（Registry attachables、每条 Gateway 记录的实时 Gateway ARN + 出站认证身份、每个技能的 S3 内容摘要、ACTIVE 托管知识库、Workspace 前置资源——唯一的 AWS 读取）；预置未运行时 `409 assistant.unavailable`（`detail.preset_status`） |
| `GET` | `/api/assistant/architect/conversations/{id}` | 成员（所有者；共享期间所有成员）；管理员（任意会话，未共享时 `read_only`） | `{…摘要, catalog{fetched_at, tools[{key, kind, name, description, attachable, reason, gateway_arn?, auth_type?, outbound_auth?, url?, record_id}], skills[{key, name, description, path, record_id, content_digest, object_count}], knowledge_bases[{kb_id, name, description}], warnings[], resources{memory_arn, memory_id, memory_kind, kb_gateway_id, kb_gateway_arn, oauth_provider_arn, execution_role_arn}, target{workspace_id, account_id, region}}, messages[{id, turn, role: user|assistant|tool|error, text, name, at}], proposals[…]}` |
| `PUT` | `/api/assistant/architect/conversations/{id}/sharing` | 成员（所有者）或管理员 | 请求体 `{shared: bool}` → 摘要。向本 Workspace 所有成员开放（或收回）该会话，记录 `shared_by` / `shared_at`；不改变 `updated_at`。只能共享调用者本身可访问的会话（自己的，或已共享的；管理员可访问所有会话，因此可共享任意会话——只读会话由此转为协作），非管理员访问其他成员的私有会话返回 404；对已共享会话，既非所有者也非管理员的协作者返回 403 `assistant.share_forbidden`。共享期间，下列会话路由（发消息、目录、资源准备、提案编辑/驳回/批准、评估计划与评估资产）对所有成员开放，各自原有的权限要求不变（在每次写入内复查，取消共享立即生效）；仅 `footprint` 与 `DELETE`（清除）限所有者（协作者得到 404）。管理员对未共享会话的读取（`GET` 详情、`evaluation-plan`、`evaluation-plan/operations/{id}`、`turns/{turn}/stream` 只读跟随）不会放宽任何写入：共享之前，所有写入路由对其返回 404 |
| `POST` | `/api/assistant/architect/conversations/{id}/catalog` | 成员 | 重新读取目录 → `{catalog}` |
| `POST` | `/api/assistant/architect/conversations/{id}/turns` | 成员 | 请求体 `{prompt}`（≤ 100k 字符 / 300k 字节，且须与前言一起放入请求预算）→ SSE `meta{conversation_id, turn, session_id, agent, omitted_turns} → (tool|delta)* → proposal? → done` 或 `error{code?, message}`（保留在记录中）。**N-ARCH-obs15：**无论本连接如何，这一轮都在服务端运行到终态——每帧带 `id: <seq>`，关闭响应只是断开订阅，`GET …/turns/{turn}/stream` 可重新接入；只有生产者自身失败、丢失声明或进程死亡才会留下部分回答 + 一条 `turn_interrupted` 错误行（已完整传输的提案块仍会记录）。对预置发起一次携带有界重放记录（按轮次配对，≤ 12 轮 / 含前言 160k 字符）的 `InvokeHarness`；`launchpad-proposal` 块（≤ 64 000 字节）成为新修订（`draft` 或 `invalid`）；**无其他写入**。**自动修正（N17-obs295）：**模型提案只违反结构性评估计划路由规则（依赖参考数据的评估器所需的参考字段在某个场景缺失、TRACE 级参考评审有一轮缺少 `expected_response`、评估器只映射到部分黄金测试）时，这一轮先发出 `autocorrect{revision, errors[], validation_error_details[]}`，再把这些错误作为内部跟进（从不存为成员消息）发起**一次**模型调用，随后是 `(tool|delta|retry{…, phase: "autocorrect"})*` 和修正后修订的 `proposal`；首版修订变为 `superseded`。记录中保留一条通知行 `proposal_autocorrected`（修正仍未通过校验时为 `proposal_autocorrect_failed` 加通常的 `proposal_rejected` 行）；`LAUNCHPAD_ASSISTANT_PROPOSAL_AUTOCORRECT=false` 可关闭。契约见 [assistant-turns.md](cn/interfaces/assistant-turns.md) §11。**无提案块（N17-obs468）：**回复中没有提案块，却模仿了重放占位符或声称已提交修订时，记录一条 `proposal_missing_pending` 通知，发出 `reask{revision, reason}` 并发起**一次**纠正性内部调用（`retry{…, phase: "reask"}`）；通知最终为 `proposal_missing_recovered`（已生成修订）或 `proposal_missing`（提案未更新）；`LAUNCHPAD_ASSISTANT_PROPOSAL_REASK=false` 时只保留通知；详情新增 `turn_revisions`（§12）。流打开前：`409 assistant.unavailable`、`409 assistant.turn_in_progress`（`detail.active_turn`——每会话单轮在途）、`409 assistant.conversation_full`（200 轮）、`413 assistant.prompt_too_large` |
| `GET` | `/api/assistant/architect/conversations/{id}/turns/{turn}/stream?from=<seq>` | 成员 | 重新接入某一轮（N-ARCH-obs15）：重放 `seq > from`（默认 0）的已持久化事件，这一轮仍在运行时持续跟随，最后给出终态 `done`/`error`。`404 assistant.turn_not_found`；他人发送的轮次 `409 assistant.turn_not_yours {author}`；`422 assistant.invalid_seq`。**只读查看者**（管理员查看其他成员未共享的会话，`read_only: true`）可跟随该会话的任意一轮——流只是订阅、不写任何内容，且该查看者没有自己的轮次；发送者规则（409）只对协作者生效。契约见 [docs/cn/interfaces/assistant-turns.md](cn/interfaces/assistant-turns.md) |
| `PUT` | `/api/assistant/architect/conversations/{id}/proposal` | 成员 | 请求体 `{content}`（提案白名单；未知外层成员 → 422）→ `{proposal}`——一个**新**修订（`source: member`，唯一单调编号），绝非就地修改；无效内容按 `invalid` 保存并附 `validation_errors`，从不被更正；序列化超过 64 000 字节返回 `413 assistant.proposal_too_large`（不存储任何内容；规范化后的存储内容按同一上限复查）；达到 50 个修订返回 `409 assistant.conversation_full`。所有助手写请求同时在入口受限：实际接收超过 512 000 字节返回 `413 assistant.request_too_large`。**`evaluation_plan`（N15-obs13）：**请求体**缺少**该键而最新修订带有种子时，种子原样带入新修订，并记录 `notes: ["proposal_repaired: evaluation_plan carried from revision N — the editor did not submit it"]`；显式 `evaluation_plan: null` 表示删除种子（不记录说明）；提交了种子对象则按提交内容存储。所有提案载荷（此处、详情的 `proposals[]`、批准 / 拒绝）均带 `notes[]`（未修复时为 `[]`） |
| `POST` | `/api/assistant/architect/conversations/{id}/proposal/reject` | 成员 | 请求体 `{revision}` → `{proposal}`，`status: rejected`（不可执行）；条件转换——非当前修订返回 `409 assistant.proposal_stale`，期间已执行的修订返回 `409 assistant.proposal_already_approved`（`detail.approval`） |
| `POST` | `/api/assistant/architect/conversations/{id}/proposal/approve` | `perm:agents.deploy` | 请求体 `{revision, content_hash}` → 本次调用声明该修订、声明 Agent 名称（与 `POST /api/agents` 共享）并在一次提交中创建普通 Agent + Deployment + `deploy_agent` 任务且把其 ID 写到提案上时返回 `202 {proposal, agent, job_id, deployment_id, started: true}`；重复、并发或历史（已批准，即使有更新修订）请求返回已记录结果 `200 … started: false`——并重新唤醒仍 `queued` 且无活跃 worker 的任务。拒绝均发生在任何写入之前：`401 auth.required` / `403 auth.permission_required` / `403 workspace.forbidden`（实时目录读取之后、在声明处从数据库重新解析）、`409 assistant.proposal_stale`（修订不存在 / 哈希不同 / 批准期间变化）、`409 assistant.proposal_not_approvable`（无效、已取消、已被替代）、`409 assistant.already_approved`（`detail.agent_id`、`revision`、`job_id`、`agent_name`、`agent_status`——该会话已从更早的修订创建了它唯一的 Agent；即便该 Agent 已删除，本会话也不再接受第二次创建——N16-obs40）、`409 assistant.workspace_not_ready`、`409 assistant.proposal_invalid`（实时目录已无所引用资源或前置条件——如知识库 Gateway）、`422 agent.builtin_unsupported`（该方式不渲染的内置工具——N15-obs16）、`422 agent.name_reserved`、`409 agent.name_exists`（原子——两个竞争创建者之一；N17-obs469：每个草稿 / 无效提案的载荷已按同一在用规则带 `name_conflict: {name} | null`，控制台显示为常驻警告并禁用批准，架构助手的前言列出工作区内已占用的 Agent 名称——409 仍是最后防线）、`409 assistant.bindings_changed`（`detail.changed[]`——某个 key 解析到了与审阅时不同的 URL、Gateway 认证身份、技能内容、记忆或知识库 Gateway）、`502 assistant.catalog_unavailable`（实时目录无法读取且无胜出方）；每个“已批准”响应都先重新校验调用者 |

提案为 `{id, conversation_id, revision, source: model|member, status: draft|invalid|approved|
rejected|superseded, content, content_hash, bindings, validation_errors[],
validation_error_details[], created_by, created_at, approval, rejected_by, rejected_at}`。
`validation_error_details[]`（N17-obs296）把每条 `validation_errors[]` 表示为
`{code, params, message}`——`message` 为原句；`code` 为
`evaluation_plan.reference_missing {evaluator, scenario, fields[]}`、
`evaluation_plan.trace_turn_missing_response {evaluator, scenario}`、
`evaluation_plan.evaluator_partial_routing {evaluator, golden_tests[]}` 或通用的
`proposal.validation_error {}`；评估计划修订带同一字段，记录中的通知行（`proposal_rejected`、
`proposal_autocorrect*`）带同形的 `errors[]`。`content` 是白名单对象 `{version: 1, name,
model_id, model_source, system_prompt, tools[key], skills[key], knowledge_bases[kb_id],
memory: disabled|workspace, max_iterations, timeout_seconds, summary,
requirements_baseline[], assumptions[], manual_tasks[], golden_tests[{id, input,
expected_response, expected_tools[], forbidden_behavior, pass_criteria, evaluator,
source}], evaluator_recommendations[]}`；`bindings` 是解析后的 `{name, method, model_id,
model_source, tools[ToolRef], skills[s3 路径], knowledge_bases[KnowledgeBaseRef],
memory{short_term, long_term, memory_id}, max_iterations, timeout_seconds,
resources{gateways{<gateway_id>: {gateway_arn, gateway_name, record_id, auth_type,
outbound_auth}}, remote_mcp{<name>: {url, record_id}}, skills{<key>: {record_id, path,
source_prefix, content_digest, object_count, total_bytes}}, kb_gateway{gateway_id, gateway_arn,
oauth_provider_arn, url, authorizer_type, authorizer} | null, memory{mode, arn}, execution_role_arn}}`（无效时为 null；`outbound_auth` 是身份——提供方
ARN、授权类型、scope——绝非凭据值）；`content_hash` = 规范化 `{content, bindings}` 的 sha256。
部署任务的 payload 携带 `{content, bindings}` 并在任务入口重新检查（漂移 → 任务在任何阶段之前
失败）。可观测路由（`/api/observability/sessions*`、`/traces*`、`…/evaluate`）隐藏其他 principal
的助手会话，其详情返回 `404 observability.session_not_found` / `observability.trace_not_found`。`approval` 为 `{approved_by, approved_at, agent_id,
agent_name, agent_status, agent_error, deployment_id, job_id, job_status} | null`；任务与
Agent 是普通行，可通过 `GET /api/jobs/{id}` 与 `GET /api/agents/{id}` 读取。通用调用入口
（`POST /api/chat/{id}`、`POST /api/agents/{id}/invoke`、`/v1 …/invoke[-stream]`）对系统托管
Agent 上助手轮次的 `session_id` 返回 `404 chat.session_not_found`。

### 评估资产计划（SE-047）

仅对话所有者可见（其他主体 / Workspace → `404 assistant.conversation_not_found`，管理员亦然）。准备 / 编辑为
`member`；创建与清理为 `admin` **且**为所有者。读取仅访问账本。

| 方法 | 路径 | 角色 | 结果 |
|---|---|---|---|
> **中国区（N12 W-E5，CA-5）。** 计划契约与这组路由与 Global 相同，但落地到 Launchpad 自建的评估基底：评估器写入 `eval_evaluators` 行（`evaluators_local`），数据集仍是账本行；**code** 评估器的 Lambda（独立角色 + 日志组 + 一个已发布版本）仅在 `Settings.eval_code_evaluators_enabled` 打开时创建，否则该链与该评估器记为 `skipped`，操作照常成功。没有 AgentCore 评估器、Lambda 资源策略或执行角色附加授权；SE-049 的 `lambda-revision-review` 路由不存在（`ALLOWED_DELTA`，CA-5）。judge / derived 模型必须在中国区目录内（materialize 时 `422 model.not_allowed`）。

| `GET` | `/api/assistant/architect/conversations/{id}/evaluation-plan` | member | `{plans[…], operations[…], disclosure}` |
| `POST` | `…/evaluation-plan/prepare` | member | `{revision}` → `201 {plan, …}` 平台草稿（旧版黄金测试的场景带 `review_required`，草稿在全部确认或阻止前为 `invalid`）；无副作用 |
| `PUT` | `…/evaluation-plan` | member | `{content}` → 新修订（`draft` 或带 `validation_errors` 的 `invalid`）；超过 160 000 字节 → `413` |
| `POST` | `…/evaluation-plan/materialize` | `perm:agents.deploy`（所有者） | `{plan_revision, plan_hash, acknowledge_disclosure: true}` → `202 {operation, started: true}`（原子声明：仍为 draft、同哈希、最新版本；启动 worker）或 `200 … started: false`（重复 / 并发请求，同一操作）。`422 assistant.disclosure_required`、`409 assistant.evaluation_plan_stale`（版本 / 哈希不符或期间被编辑 / 取代 / 声明）、`409 assistant.evaluation_plan_not_approvable`、`409 assistant.evaluation_plan_invalid`、`409 assistant.workspace_not_ready`、`409 assistant.execution_role_untrusted`（请求授权但执行角色无平台标签）；调用者与 Workspace 身份在声明事务内重新解析并钉在操作上 |
| `GET` | `…/evaluation-plan/operations/{operation_id}` | member | `{operation}`——仅账本，不调用 AWS |
| `POST` | `…/operations/{operation_id}/retry` | `perm:agents.deploy`（所有者） | 恢复持久化意图（同令牌 / 同请求）；5 次后 `409 assistant.evaluation_assets_exhausted` |
| `DELETE` | `…/operations/{operation_id}/assets` | `perm:agents.deploy`（所有者） | 按依赖顺序仅删除身份仍匹配的自有云端资产，每次生效后持久化；本地 Dataset 保留；worker 活跃时 `409 assistant.evaluation_assets_running`，批准者或 Workspace 身份变化时 `409 assistant.evaluation_assets_stopped` |
| `GET` | `…/conversations/{conversation_id}/footprint` | 成员（所有者） | 「清除」将移除的内容：`{agents[], canaries[], operations[], datasets[], blockers[], required_permissions[], can_clear, purge, turns, proposals}`（`canaries` 为在本会话 Agent 上运行的 Runtime 金丝雀，`kind` 恒为 `runtime`，`resources{ab_test, endpoints[], online_evaluations}`——`ab_test` 为本地 A/B 行，CN 没有专属网关；`pending` 表示清理尚未执行或有残留；`blockers[].kind` 可为 `turn` / `preparation` / `operation` / `job` / `canary`；`purge` 为最近一次清除任务；涉及云端资产时 `required_permissions` 含 `agents.deploy`，涉及 Agent 时含 `agents.delete`；`can_clear` 表示调用者全部具备）；仅读账本 |
| `DELETE` | `…/conversations/{conversation_id}` | 成员（所有者），并须具备 `required_permissions` 中的每项权限，且为每个金丝雀与 Agent 的创建者或管理员 | 只有对话记录的会话直接删除（`200 {deleted: true, …}`）；其余情况为持久化清除任务：`202 {deleted: false, job_id, status, …}`，通过 `GET /api/jobs/{job_id}` 轮询（`payload.step`、`payload.result`，失败时 `payload.blocker`），重启后由 `resume_pending_jobs` 恢复。按依赖顺序，每步回读确认后才进行下一步：Agent 上的金丝雀（在其自身 `running_action` 认领下停止并 `act_cleanup`，等待两个分支的在线评估配置与两个命名端点均不存在）→ 对每次可清理的评估资产操作执行带围栏的清理 → 本地 Dataset（手动同步到 AWS 的副本保留）→ 每个 Agent（删除 Runtime、等待 `GetAgentRuntime` 返回不存在，再执行残留清理——执行角色、制品、记忆快照 / 记录、其自身的 A2A Registry 记录、日志组——与账本）→ 金丝雀候选包 → 账本行。删除任何内容之前的拒绝：`409 assistant.conversation_busy`（轮次、导入、操作、部署任务或金丝雀动作在进行）、`403 auth.permission_required`、`403 canary.forbidden` / `403 agent.forbidden`（将被拆除的金丝雀或 Agent 由其他成员创建且调用者不是管理员——`users-and-ownership.md` §8）。任务进行时重复 DELETE 返回同一任务；期间会话写入返回 `409 assistant.conversation_clearing`，其 Agent 的金丝雀 / 重新发布 / 删除路由返回 `409 assistant.agent_clearing`。无法完成的任务以 `failed` 结束，`payload.blocker.code` 为 `assistant.purge_canary_incomplete`、`conversation_assets_remain`、`purge_still_deleting`、`purge_delete_failed`、`purge_agent_dependency`、`purge_agent_conflict`、`purge_agent_busy`、`purge_role_remains`、`purge_dependency_busy`、`purge_unowned`、`purge_step_failed`（含 Registry 记录步骤失败）之一（或拒绝本身的代码）；会话保留，下一次 DELETE 从已确认的检查点继续 |

操作为 `{id, plan_id, plan_revision, plan_hash, proposal_revision, approved_by, account_id, region,
pinned{…}, status: queued|running|succeeded|partial|failed|cleaning|cleaned, attempts, max_attempts,
dataset_id, error, resources[{kind, key, plan_key?, name, status: pending|accepted|ready|failed|conflict|
blocked|skipped|retained|deleted|delete_failed, error, digest?, reference_dependent?, owned?, recovered?,
result, cleanup?, link?}], running}`。`POST /api/eval/runs` 对缺少参考的托管代码评估器返回
`422 run.judge_needs_ground_truth`（模拟人设条目的轮次在运行时才生成，报告为 `<scenario>/simulated turns
lacks expected_response`）；所选自定义评估器读取失败时返回 `422 run.evaluator_unverifiable`（需求未知不等于已验证，
不会启动任何下游步骤），不存在时返回 `422 run.evaluator_not_found`；在线评估拒绝此类评估器。普通 `DELETE /api/eval/evaluators/{id}` 对操作拥有
的评估器返回 `409 evaluator.managed_by_operation`。`GET /api/assistant/architect` 新增
`can_materialize_evaluation_assets`（= 调用者具备 `agents.deploy`）。

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
| `GET` | `/api/registry/records/counts` | `{pending, approved, draft, rejected, deprecated, total}`——发布者列表的分状态计数（N17-obs267）：用于 Registry 页面的状态标签页，以及侧边栏「注册中心」入口上的待审批角标（仅管理员可见，为 0 时隐藏；路由切换时以及批准／驳回／提交／删除之后重新读取，不轮询）。MEMBER，按 workspace 划分，可见性与 `GET /api/registry/records` 相同：隔离模式下，成员的计数不包含对其隐藏的 Agent 的 A2A 记录。服务端对一次 `list_records` 读取计数（不做逐条投影，不返回载荷）；`total` 也计入五个分类之外的状态 |
| `GET` | `/api/registry/records/discoverable?type=` | `{records[{record_id, name, display_name, description, type, descriptor_types[], status, status_reason, version, created_at, updated_at}], count}`——数据面 `ListDiscoverableRegistryRecords(registryId=<workspace 注册中心>, maxResults=100)`，按 `nextToken` 翻页到底；可选的 `type` 以 `filters=[{name: "recordType", values: [<GA 类型>]}]` 收窄，接受平台名（`A2A`/`MCP`/`AGENT_SKILLS`）或 GA 名（`agent`/`mcp`/`skill`）；行内的 `type` 始终是平台名。摘要从不包含 `descriptors`——需要载荷时读取 `GET /api/registry/records/{record_id}`。`count` 为行数 |

错误码：`registry.bad_type`（422，未知的 `type`）、`registry.unavailable`（503，该 workspace 没有注册中心）。AWS `ClientError` 映射为标准 4xx 信封（`aws.access_denied`、`aws.throttled` 等），绝不返回裸 500。路由策略为 MEMBER，与其他 Registry 读接口一致。

**删除技能记录会一并删除其文件（N16-obs35，`cn/r17-j`）。**`DELETE /api/registry/records/{record_id}` 作用于
AGENT_SKILLS 记录时，先删除记录行，再删除 artifacts 桶中 `skills/<name>/` 前缀下的当前对象——除非仍有其他记录或
存活 Agent 的 `spec.skills` 引用该前缀，此时文件保留，`objects.kept_for` 列出持有者；`system-skills/<name>/<version>/`
（预置）永不作为删除目标。清理为尽力而为（`objects: {objects_deleted, prefix, kept_for?, error?}`）；开启版本控制的
桶中，非当前版本遵循桶的生命周期规则。被拒绝删除的对象同样算作 S3 失败（N17-obs118，`cn/r17-t`）：清理经由唯一的
上报型删除器（`services/s3_sweep.py`——分批 `DeleteObjects` 并读取其 `Errors[]`），因此无论 `DeleteObjects` 抛错，
还是 200 应答中被 S3 拒绝的 key，都回答 `objects_deleted: false` 并附带脱敏的 `error`（`ClientError: AccessDenied
(DeleteObjects)` / `1 of 1 objects refused (AccessDenied)`——只有类型、错误码、操作，绝不带 S3 消息）；
`objects_deleted: true` 表示列出的每个对象都已删除。

## 控制台 Registry API——导入 Gateway Target / Console Registry API: Import Gateway targets

Registry →「导入 Gateway Target」（两套控制台一致）打开一个对话框，背后是这两条路由：选择一个已接入的 Gateway（治理清单，平台 Gateway 排第一）、勾选 Target（默认全选）、导入、逐条查看结果。平台 Gateway 沿用 `sync-defaults` 的语义（实时 `tools/list`、由 `system` 即时批准、卡在草稿／待审批的记录被对齐）——**仅限管理员发起的请求**（N17-obs257，用户裁定 2026-10-22）；成员导入平台 Gateway 时只创建新记录并提交审批（PENDING_APPROVAL，之后由管理员决定），不对齐任何记录；响应中的 `auto_approved` 说明属于哪种情况。其他 Gateway 走成员导入（创建并提交，PENDING_APPROVAL，由管理员批准）。细节见 [docs/cn/interfaces/registry-backend.md](cn/interfaces/registry-backend.md#import-gateway-targets--selective-gateway--mcp-record-import-wb-005)。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/registry/import-gateway/preview` | `{gateways[{gateway_id, name, gateway_arn, gateway_url, auth, auth_warning, platform, live_error, will_approve, member_submissions_to_approve, targets[{target_id, target, target_status, tools, tool_source, record_id, status, imported, held, held_by, endpoint, endpoint_kind, origin, will_approve}]}]}`——只读；`auth` ∈ `aws_iam \| none \| oauth \| unmapped`，授权方式没有 Launchpad 托管凭证时给出 `auth_warning`；`tool_source` ∈ `live \| schema \| none`；`record_id`/`status` 为已占用该 Target 名称的记录，属于本 Gateway 时 `imported`，被其他服务器占用时 `held`（导入将拒绝）；`held` 时 `held_by`（N17-obs250）说明占用者，否则为 `null`——`{record_id, name, status, origin, gateway_id?, gateway_name?}`：占用记录的服务器 URL 是同一清单中某个 Gateway 时 `origin: gateway`（附其 id 与名称，不额外调用 AWS），成员注册的外部 MCP 服务器则为 `member`。N17-obs261（增量字段）：`endpoint` 为 Target 指向的端点，取自已列出的清单，不额外调用 AWS。它是 MCP 服务器 URL、Lambda 函数 ARN、API Gateway 的 `restApiId/stage`，仅有 schema 的 Target 为 `null`。`endpoint_kind` 为 `targetConfiguration` 的成员名。`origin` 针对本 Gateway 自己的记录：因成员的同步 / 导入只提交了它而处于 PENDING_APPROVAL 时为 `member`，否则为 `platform`，没有此类记录时为 `null`。调用者的导入会批准该 DRAFT / PENDING_APPROVAL 记录时 `will_approve` 为 true，这只发生在管理员导入平台 Gateway 时。Gateway 级的 `will_approve` / `member_submissions_to_approve` 统计所有 Target 中的这类行 |
| `POST` | `/api/registry/import-gateway` | 请求体 `{gateway_id?, target_ids?}`（省略 `gateway_id` = 平台 Gateway，省略 `target_ids` = 全部 Target）→ `{gateway_id, gateway_name, gateway_arn, auth, platform, live_error, results[{name, target, target_id, gateway_id, record_id, created, reconciled, status, tool_source, auth_warning?, error?, error_code?, held_by?}]}`——每个 Target 一条：已创建／已导入过（`created: false`）／已对齐／失败（`error`，`error_code: registry.name_held`，并带同样的 `held_by`——此处只按 URL 识别平台 Gateway 与本次请求的 Gateway，平台 Gateway 以其 id 作名称）。幂等；绝不产生重复或覆盖。记录携带 `descriptors.mcp.gateway = {gateway_id, gateway_arn, auth, runtime_tools[, auth_note]}` |
| `POST` | `/api/registry/sync-defaults` | `{results[{name, type, record_id, created, reconciled, status}], auto_approved}`——整个平台 Gateway 的导入（全部 Target + 示例技能）。N17-obs257：只有主机初始化（`make bootstrap`、Workspace 初始化作业）与管理员调用会自动批准——新 Target 记录由 `system` 批准、卡住的草稿／待审批平台记录被对齐、成员调用只创建新记录并**提交**给管理员审批（PENDING_APPROVAL），不对齐任何记录；`auto_approved: false`。示例技能无论由谁触发都遵循自己的规则（N17-obs262，用户裁定 2026-10-22）：新记录提交给管理员审批；技能包版本变化时，已批准的平台记录由 `system` 重新批准，因为其文件来自仓库本身；仍在审批中的记录重新提交。成员以示例技能名称创建的技能绝不会被刷新或批准，其结果行带 `name_held: true`（技能结果行另有 `updated`、`removed_duplicates`）。成员可以向平台 Gateway 添加 Target（身份 → Gateway Target），因此成员发起的同步不能直接发布它们。成员运行留下的待审批平台记录会在之后的管理员同步／导入或主机初始化时被批准；管理员也可以先在「待审批」标签页中逐条批准或驳回 |

错误码：`registry.gateway_target_not_found`（404，`detail.target_ids`，写入前即返回）、`governance.gateway_not_found`（404）、`gateway.not_bootstrapped`（503，没有平台 Gateway 且未传 `gateway_id`）、`feature.unavailable`（503，未注册 Registry 后端）。

## 控制台治理 API / Console Governance API

以下 `/api` 路由支撑需要鉴权的控制台，不属于公开的 `/v1` Agent 调用契约。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/tools` | 工具目录 → `{gateway_url, gateway_error, tools[]}`：共享 Gateway 的实时 MCP 工具（`source: gateway`，`target`），其后是内置工具行（`source: builtin`：`code-interpreter`、`browser`），每个内置工具带 `methods: string[]`——会渲染它的部署方式（`["harness"]`；未列出的方式在创建时返回 `422 agent.builtin_unsupported`，N15-obs16）。`?refresh=true` 绕过 60 秒的 Gateway 缓存 |
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
| `GET` | `/api/knowledge-bases/{kb_id}` | 详情：状态、ARN、时间戳、`failure_reasons`、`attached_agents`，以及每个数据源的桶/前缀、状态、最近的 ingestion 作业、`jobs_degraded` 与 `has_objects`（前缀下存在 `_text/` 以外的对象为 `true`，没有为 `false`，列举失败为 `null`——控制台的自动首次同步仅在 `true` 时执行；N17-obs204） |
| `PATCH` | `/api/knowledge-bases/{kb_id}` | `{description}`（≤1000 字符）→ `UpdateKnowledgeBase`，名称、角色与配置原样读回后回传；响应是刷新后的详情 |
| `DELETE` | `/api/knowledge-bases/{kb_id}?force=` | 依次删除数据源、按知识库的网关 `Retrieve` 目标与按知识库的内联 S3 策略，最后 `DeleteKnowledgeBase`。仍有 Agent 挂载时返回 `409 kb.has_attached_agents`；`force=true` 会先把它从每个挂载它的 Agent spec 里摘掉（并重新同步 harness 类 Agent 的 agentic 目标）。**N17-obs31（= N16-obs34）：**`force=true` 会在共享的路由层、在调用任何后端之前，于同一事务内把该知识库从**每个**挂载它的 Agent 规格中剥离（包括因可见性而对调用者隐藏的 Agent）；被剥离的 Agent 标记为 `kb_stale`（投影上的 `last_strip {kb_ids, at, by}`）——已部署的 Runtime 在下一次重新发布之前仍带着检索工具，重新发布后标记清除；剥离不是发布（不写快照，记入该 Agent 最近一次部署任务的日志）；响应新增 `stripped_agents: [{id, name}]`。**`force` 是管理员覆盖（N17-obs4）：**成员带 `?force=true` 无论是否有 Agent 挂载都返回 `403 auth.forbidden`（否则会剥离其他成员的 Agent 规格）；成员的普通删除保持原路由策略（挂载中返回 `409 kb.has_attached_agents`）。挂载该知识库的某个 Agent 的部署任务为 `queued` / `running` 时，整个强制删除返回 `409 agent.deploy_in_progress {kb_id, agents: [{id, name, job_id, stage, job_status}]}`，不写入任何内容（`docs/cn/interfaces/agents.md` §1）。CN 没有需要重新同步的按 Agent 网关 `Retrieve` 目标（检索是 Runtime 包内的工具） |
| `POST` | `/api/knowledge-bases/{kb_id}/files` | `multipart/form-data`，一个或多个名为 `files`（或 `file`）的部件 → `{keys, job}`：artifacts 桶 `kb/{kb_id}/` 下已存储的键，以及全部文件落地后由服务端发起的 ingestion 同步——`job` 为 `{job_id, status, coalesced, covers_upload}`；该数据源仍在运行的作业会被直接返回（`coalesced: true`）而不再排队第二个，若它早于本次上传开始则 `covers_upload: false`（早于本次上传开始的同步可能不含这些文件；下一次上传或手动同步会覆盖）；文件已存储但同步无法启动时 `job: null`（需手动同步）。文件存储成功即为 200（N17-obs204）。没有 `upload` 模式数据源的知识库（最后一个被移除，或数据源全为 `existing`）由本次上传自动创建一个——一条新的 `kb/{kb_id}/{ds_id}/` 前缀记录，与 `POST …/data-sources {mode: "upload"}` 所建相同——此时响应另带 `data_source: {ds_id, name, mode, bucket, prefix, status, created_at}`（已有上传数据源时不带；`keys` 位于该前缀下；两次并发的首次上传只创建一个数据源）（N17-obs242） |
| `POST` | `/api/knowledge-bases/{kb_id}/data-sources` | `201`——用同样的 `{mode, bucket?, prefix?}` 请求体创建 `MANAGED_KNOWLEDGE_BASE_CONNECTOR` 数据源，并返回刷新后的详情。按 S3 位置幂等：同一桶/前缀上已有连接器时直接返回它，而不是再建一个。这同时也是「知识库没有数据源」时的手动补建入口。自托管（中国区）后端：响应体为数据源本身加 `created`；`upload` 模式按知识库幂等——已有 `upload` 模式数据源的知识库返回 **200**、即该数据源且 `created: false`，新建时返回 **201**、`created: true`（并发调用只建一个）；`existing` 模式每次都追加，返回 201、`created: true`（N17-obs243） |
| `DELETE` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}` | `DeleteDataSource` → `{deleted, ds_id}`（AWS 侧为异步删除）。允许移除知识库最后一个 `upload` 模式数据源；下一次上传会自动创建一个新的（N17-obs242） |
| `POST` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync` | 可选 JSON 请求体 `{full: bool}`（默认 `false`；未知字段 → 422）。写入一条 `STARTING` ingestion 作业并把前缀投递给抽取 Lambda → `{job_id, status}`。默认同步为增量：自上次索引以来未变化的文档（S3 ETag 与大小相同）被跳过（N17-obs246）；`{"full": true}` 强制全量重建，重新切片并生成每个文档的向量（N17-obs272，即控制台的「全量重建」）。能同步者即能重建：知识库的任一可见者（不可见的知识库两者都返回 404 `kb.not_found`）。该数据源有未过期的作业正在运行（或该检查超时）时，全量重建返回 409 `kb.sync_in_progress {job_id}`，不投递任何消息（N17-obs275）；增量同步不做此检查。已完成作业的 `statistics` 含 `numberOfDocumentsScanned` / `numberOfNewDocumentsIndexed` / `numberOfDocumentsUnchanged` / `numberOfDocumentsFailed`（大于 0 时另有 `numberOfDocumentsDeleted`），列举到的前缀为空时另带 `prefix_empty: true`（N17-obs204）。作业文档的 PUT 若超时未确认，hub 会先 GET 一次确认再放弃（写入已落地时同步照常返回）；作业文档已存在而队列投递失败时，作业标为 `FAILED`、`failure_reasons: ["enqueue_failed: <异常类名>"]`，路由返回 502 `kb.unavailable`（N17-obs236） |
| `GET` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/ingestion-jobs` | 最近 50 个 ingestion 作业，最新优先，投影同上。启动 30 分钟后仍为 `STARTING` 且 `statistics` 为空的作业，会被呈现并持久化为 `FAILED`、`failure_reasons: ["never_dispatched: no worker picked the job up within 30 min"]`（N17-obs236；详情里的作业列表同样适用） |
| `GET` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/documents?page_size=&token=` | `ListKnowledgeBaseDocuments` 的一页（`page_size` 1–100，默认 50），形如 `{documents, next_token, page_size}`；每个文档带知识库侧的 `status`/`status_reason`/`indexed_at`，以及按对象 key 联结进来的 S3 侧 `size_bytes`/`uploaded_at`（后端无权列举该桶时为空） |
| `POST` | `/api/knowledge-bases/{kb_id}/query` | 检索 Playground——`{text, number_of_results?}`（1–100，默认 8）→ 带 `managedSearchConfiguration` 的 `Retrieve`，响应 `{results}`，元素为 `{text, score, location_uri, metadata}` |

错误码：`kb.not_found`（404——未知 id，或该知识库不是 MANAGED）、`kb.ds_not_found`
（404）、`kb.has_attached_agents`（409，阻塞的 Agent 名在 `detail.agents` 里）、
`kb.delete_conflict`（409——知识库仍在 `CREATING`）、
`kb.no_files`（400——表单里没有上传部件）、`kb.sync_not_ready`（409——
`StartIngestionJob` 抛出 `ValidationException` 或 `ConflictException`：数据源仍在预置，
或已有同步在跑）、`kb.bucket_required` / `kb.invalid_bucket` / `kb.invalid_prefix` /
`kb.invalid_source`（400——数据源校验）、`kb.query_failed`（502——知识库侧检索失败，
例如索引仍在构建）。其余任何 AWS `ClientError` 都走上文的全局映射（`aws.validation`、
`aws.conflict` 等）。资源映射里没有 `kb_role_arn`（创建）或没有 `artifacts_bucket`
（上传）的工作区尚未完成引导，会返回点明缺失键的 `500`。

## 控制台 Memory API / Console Memory API

`/api/memory/*` 支撑只读的 Memory 控制台（控制台 05），底层是共享的 `launchpad_memory` 单例。
控制台的路由除一条之外都是读操作：没有任何接口会写事件或触发抽取，唯一的记录写操作是下面的
`DELETE /api/memory/records/{record_id}?actor_id=`（N10v2-bug6）。另一组会写的接口是下面的
`/api/memory/resources*`——管理记忆*资源*本身，位于独立的路由模块（`routers/memory_resources.py`）。
记录路由返回自己的错误码而非 AWS 映射：未知、属于其他 actor 或其他 store 的记录返回
`404 memory.not_found`，删除时还有 `403 memory.record_forbidden`（N16-obs113；
`docs/cn/interfaces/memory-backend.md`「Error codes」）。详见
[architecture.zh-CN.md](architecture.zh-CN.md)「Memory 控制台」一节。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/memory/overview` | 资源配置、长期策略、有界的 actor 计数、同级记忆 |
| `GET` | `/api/memory/actors` | actor 列表，复合 id `<agent_id>__<human>` 已解码并解析出 Agent 名称 |
| `GET` | `/api/memory/sessions?actor_id=` | 单个 actor 的会话；由控制台写入的会话会关联到 ChatSession 台账 |
| `GET` | `/api/memory/events?actor_id=&session_id=` | 短期事件；每条载荷的 `kind` 为 `conversational`（角色 + 全文）、`json`（JSON 值无损序列化到 `text`，含 `null`、`false`、`0` 与 `""`）或 `blob`（只带字节数）；未知类型省略 |
| `GET` | `/api/memory/namespaces?actor_id=` | 已替换 `{actorId}` 的策略命名空间模板；尾部的 `{sessionId}` 段折叠为 actor 级前缀（`prefix: true`），其他位置的占位符则产生 `resolvable: false` |
| `GET` | `/api/memory/records?actor_id=&strategy_id=` 或 `?namespace=` | 解析所得命名空间下的长期记录。r14-a：被合并策略替代、软删除或过期的记录默认隐藏，`include_superseded=true` 时一并列出；每条记录带 `updated_at`、`confidence`、`superseded_by`、`superseded_at`、`supersedes[]`、`deleted_at`、`expired_at`、`last_op`、`active` |
| `POST` | `/api/memory/records/search` | 对有效记录做语义检索（`{query, actor_id, strategy_id?, namespace?, top_k}`）；r14-a：`score` = 相似度 × 时效衰减 × 置信度下限，`similarity` = 原始余弦相似度 |
| `DELETE` | `/api/memory/records/{record_id}?actor_id=` | `perm:memory.manage`。删除一条长期记录；后端先确认该记录属于该 actor 与本工作区的 store 再删除（N10v2-bug6）——未知 id、其他 actor 或其他 store 的记录返回 `404 memory.not_found`，且不删除任何内容。处理器另外要求管理员或该 actor 所属 Agent 的创建者（`decode_actor`）：否则 `403 memory.record_forbidden {actor_id, agent_id}`（未限定 Agent 的 actor 仅管理员可删）；被隐藏的 actor 先返回 404。同一次操作中解除该记录在 merge-v2 历史链上的邻居链接（N16-obs114）。`200 {record_id, actor_id, namespace, strategy_id, deleted: true}` |
| `GET` | `/api/memory/extraction-jobs` | 失败（可重试）的抽取作业，可按 `actor_id`/`session_id`/`strategy_id`/`status` 过滤——**控制台未展示**；AWS 的 `status` 枚举只有 `FAILED`，因此健康的资源返回空列表 |

记忆资源管理（`?view=resources`）：

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/memory/resources` | 工作区账号/区域内的全部记忆,默认记忆排首位,每条附带 spec 绑定了它的在线 Agent 与 `managed`(见下文) |
| `POST` | `/api/memory/resources` | `perm:memory.manage`。`CreateMemory`(`{name, description?, event_expiry_days?, strategies?, namespace_keys?}`)→ `201`,返回 `CREATING` 状态的详情投影;新 id 登记为已纳管 |
| `GET` | `/api/memory/resources/{memory_id}` | 详情投影:描述、状态、事件过期、执行角色、策略、命名空间键 |
| `POST` | `/api/memory/resources/{memory_id}/adopt` | **admin**。将账号内已有的记忆登记为本工作区纳管(先经 `GetMemory` 确认存在——未知 id → `404 aws.not_found`);幂等;响应为详情投影 |
| `GET`/`POST`/`PUT` | `/api/memory/resources[/{memory_id}]`——`merge`（r14-a） | 详情带 `merge {recall_half_life_days 90, max_facts_per_actor 200, max_preferences_per_actor 100, long_term_ttl_days null, actor_summary_interval 1}`（已填默认值；`actor_summary_interval` 在 2026-10-29 之前默认为 5：现在每次抽取后都会重写按参与者滚动的摘要，且召回还会直接读取该参与者最近的按会话摘要，新会话因此能立即看到上一场对话）。`POST` 与 `PUT` 接受部分 `merge` 对象；`PUT` 中 `null` 表示把该项重置为默认。未知键或越界值（1–3650 / 10–10000 / 10–10000 / 7–3650 / 1–100）返回 `422`。只提交 `merge` 补丁也满足“至少提供一项”的规则 |
| `PUT` | `/api/memory/resources/{memory_id}` | `perm:memory.manage`。仅限 `{description?, event_expiry_days?}` 的 `UpdateMemory`——至少提供一项(否则 422),`description` 1–4096 字符(只能替换、不能清空),`event_expiry_days` 7–365(越界 422)。只发送 `memoryId` 加给出的字段,绝不发送 `namespaceKeys`(API 会整体替换该集合);响应是用 `GetMemory` 读回的详情投影。不会因被 Agent 引用或是平台默认而被阻止;未知 id → `404 aws.not_found` |
| `DELETE` | `/api/memory/resources/{memory_id}` | `perm:memory.manage`。`DeleteMemory`(不可逆);工作区默认记忆返回 `409 memory.default_protected`（N16-obs113——以代码中的名称为准；`memory.platform_protected` 从未是一个错误码）,仍被在线 Agent 的 spec 绑定时返回 `409 memory.in_use`(附 Agent 列表);同时删除纳管登记。响应 `{id, deleted: true, cleanup: {s3, index: ok\|skipped}, cleanup_warnings: [...]}`——返回 200 即表示账本行已删除；两步存储清理（S3 前缀、记忆索引的 `delete_by_query`）均为尽力而为，失败的一步记为 `skipped` 并附带脱敏告警（异常类型 + 状态码 / 错误码 + 操作名，绝不带响应体；N14-obs4、N17-obs93） |

**归属。** 账号内可能存在平台从未创建的记忆,因此只有工作区的 bootstrap 记忆,或 `managed_memories` 账本行登记的记忆(由上面的 `POST` 或管理员纳管写入——从不取自客户端请求)才算*已纳管*。其余 id 在任何 AWS 调用之前,所有按 id 的路由都返回 `404 memory.not_managed`;列表仍以 `managed: false` 展示它们。spec 的 `memory.memory_id` 必须已纳管且为 `ACTIVE`:`POST /api/agents`、重新发布与转换会以 `422 agent.memory_not_managed` / `409 agent.memory_not_active` 拒绝,部署作业在任何阶段之前再次校验。对绑定了未纳管 id 的旧 spec,Chat 记忆侧栏返回 `409 agent.memory_not_managed`。

每条列表路由都接受并返回 `next_token`（AWS 按 100 条分页），并接受 `max_results`（上限 100）——
不会有任何静默截断。`/records` 与 `/records/search` 的命名空间解析顺序：显式的 `namespace` 优先，
否则由 `actor_id`（+ 可选的 `strategy_id`）推导。

错误码：`memory.not_configured`（409，尚未运行 bootstrap——`/overview` 例外，它改为返回
`{"configured": false, …}`，以便页面渲染初始化状态）、`memory.namespace_required`（400，无法推导出
命名空间）、`memory.unavailable`（502，底层 AWS / OpenSearch 调用以控制台无法翻译的方式失败；`message`
固定为 `memory lookup failed` / `memory operation failed`，`detail.error_type` 给出异常类型——绝不携带后端
自身的文本，OpenSearch 403 / 400 的文本就是原始响应体，N17-obs92）。

## 控制台 Chat API / Console Chat API

`/api/chat/*` 支撑 Chat 交互页面，与 `/v1` 共用同一条调用链（`app.services.invoke`）。
这里的会话就是 AgentCore Runtime 会话：控制台作为 `runtimeSessionId` 发出的 id，正是台账所记录的 id。

| 方法 | 路径 | 结果 |
|---|---|---|
| `POST` | `/api/chat/{agent_id}` | 一轮对话，SSE 形式（`meta` → `delta`/`tool`/`auth_required`/`policy_denied`/`error` → `done`）；**N-ARCH-obs15 v2：**无论本连接如何，这一轮都在服务端运行到终态——每帧带 `id: <seq>`，`meta` 带 `turn`，关闭响应只是断开订阅，`GET …/sessions/{session_id}/turns/{turn}/stream` 可重新接入；该会话上一轮仍在运行时，流打开前返回 `409 chat.turn_in_progress {active_turn}`（每会话单轮在途）。`{prompt?, attachments?, session_id?}`，不带 id 即开启新会话（自带的 id 必须为 33–100 个 `[a-zA-Z0-9][a-zA-Z0-9-_]*` 字符，否则在策略网关与任何 AWS 调用之前返回 `422 chat.session_id_invalid`；N-SCN-obs16 / N16-obs72；且必须是调用者自己的会话——管理员可续聊任何会话——其他成员的闲置会话在两种可见性模式下都返回 `404 chat.session_not_found`，绝不会把轮次追加进去；N16-obs71）。**N16-obs78：**请求体只接受这几个键——未知键（已退役的 `as_user`、`actor_id`、拼写错误）返回 `422 validation.invalid_request`，`detail[].loc` 指明该键（`extra_forbidden`），不再静默丢弃。**N16-obs79：**`session_id` 对应的会话已被 `…/stop` 结束时，在任何 runtime 调用之前返回 `409 chat.session_ended {session_id, ended_at}`，标记保留；请开启新会话。`as_user`（仅 JWT 入站智能体）：`true` 发送登录用户的 Cognito JWT，`false` 使用工作区 M2M 令牌，省略时有用户池登录则用用户 JWT，否则用 M2M；未经用户池登录却传 `true` 返回 `409 chat.as_user_unavailable`。`meta.inbound = {mode: jwt, caller: user_jwt\|m2m}`；两种情况下 Memory actor 都是 `scoped_actor`。`auth_required` `{provider, tool, scopes[], url, agent_id}` 是工具发起的 as_user（3LO）授权请求：`url` 为一次性授权 URL（控制台仅在其为 `https:` 时显示链接），`sessionUri` 留在服务端，回答会在其前后继续流式输出。历史中该请求保存为不含 URL 的 `role: "auth"` 行（`text` 为凭证名，`name` 为工具），因此恢复后只能重试；轮询 `GET /api/identity/grants/{connection}/status` 可得知授权何时完成。`policy_denied`（Harness；字段见“流式调用”）保存为 `role: "policy"` 行（`text` 为原因，`name` 为工具），恢复后显示为 V2 Chat 的策略拦截卡片，此时链接指向治理首页而非某个网关。**N16-obs85（新增字段）：**生成的运行时还会在所属 `tool` 事件之后流式输出 `tool_input {id, name, input}`（调用参数，≤ 512 字符，空白折叠，超长以 `… [+N chars]` 标记截断——从不解析）与 `tool_result {id, status, summary}`（≤ 256 字符；`browser` 内置工具为 `title: … | url: … | text: N chars`——最终 URL、标题与文本长度，绝不包含页面正文；其余工具为结果的前若干字符；**C4：**形如凭证的 `name=value` / `name: value` 键值对与裸 `Bearer <value>` 在两个字段中都会被替换为 `<name>=[redacted]`，原生 `shell` / `file_operations` 工具只输出 `status`、`summary: ""`），按 `id` 匹配（运行时未给 id 时按 `name`、再按最新的工具行）。两者都不关闭回答气泡；不认识这两个事件的 hub 或控制台会直接忽略 |
| `POST` | `/api/agents/{agent_id}/inbound-auth` | `agent.deploy`——`202 {agent, job_id, deployment_id}`。请求体 `{inbound_auth: {mode: iam\|jwt, jwt?} \| null}`（`null` 取消固定，改为继承工作区默认值）。以替换后的固定值重新发布已存规格：对同一 Runtime 执行 UpdateAgentRuntime，产生新版本。`422 agent.inbound_auth_unsupported`（harness／A2A 不支持 JWT）、`422 agent.inbound_auth_invalid`、`409 agent.deploy_in_progress` |
| `GET` | `/api/identity/inbound-auth/default` | 成员——`{workspace_id, default: {mode: iam\|jwt, jwt?}, configured, cognito, cognito_issuer}`；`configured=false` 表示隐式 IAM；`cognito` 是面向工作区用户池的现成 JWT 配置，引导前为 null；`cognito_issuer` 是该用户池的 issuer（控制台、`/v1` 与评估所出示令牌的 issuer），无用户池时为 null |
| `PUT` | `/api/identity/inbound-auth/default` | `identity.manage`——请求体 `{mode, jwt?: {discovery_url, allowed_clients[], allowed_audience[], allowed_scopes[], custom_claims[], source_connection?}}` → 同 GET 形状。`source_connection` 仅用于展示，不进入授权器配置。持久化前先探测发现文档（`422 identity.discovery_unreachable`／`identity.discovery_invalid`）。已部署的智能体在重新部署前保持原授权器 |
| `GET` | `/api/identity/connections/oidc-sources` | 成员——`{sources[{name, vendor, discovery_url, issuer, derived_from: discovery_url\|issuer}]}`：存储的 `oauthDiscovery` 可推导出 OIDC 发现 URL 的 OAuth2 凭证（入站 JWT 表单的“从凭证选择”）。不含系统凭证与 GitHub，从不返回凭证的 client id |
| `POST` | `/api/identity/connections/oauth2` | `identity.manage`——可选 `obo{grant_type: TOKEN_EXCHANGE\|JWT_AUTHORIZATION_GRANT, actor_token_content?: NONE\|M2M, actor_token_scopes?[]}` 设置 `onBehalfOfTokenExchangeConfig`：仅限 CustomOauth2（`422 identity.obo_vendor_unsupported`），Cognito 或发现文档未声明该授权类型的 IdP 返回 `422 identity.obo_unsupported`，`obo_invalid` 表示字段非法 |
| `DELETE` | `/api/identity/connections/{kind}/{name}` | `identity.manage`——`{deleted: true}`；被某个 Agent 的工具鉴权或网关目标引用时拒绝 `409 identity.connection_referenced`；作为某个 Agent 的 `model_credential` 或模型注册表某提供方的 `credential_provider` 使用时拒绝 `409 identity.in_use {referenced_by[≤20]{type: agent\|model_provider, id, name}, total, truncated}`（N13-obs22——`launchpad-global-bedrock` 返回的是它而不是 `identity.external_connection`；隔离隐藏的 Agent 从列表中去掉，`total` / `truncated` 随脱敏后的列表重算，成员不会得知有多少隐藏 Agent 引用它；即使可见项为空也仍拒绝删除——`referenced_by: []`、`total: 0`）。在线评估配置不持有凭证引用。无人引用的仅存在于 vault 的提供方仍是 `409 identity.external_connection` |
| `POST` | `/api/identity/gateway-targets` | `identity.manage`——`mode: obo` 设置 `grantType: TOKEN_EXCHANGE`，需要 CUSTOM_JWT 网关（`409 identity.obo_needs_jwt_gateway`）与已配置 OBO 的凭证（`422 identity.obo_unsupported`）。凭证的 issuer 与网关 JWT 授权器的 issuer 不同时仍会创建目标，响应的 `warnings` 带有 `identity.obo_issuer_mismatch` `{connection, connection_issuer, gateway_issuer}`：该 IdP 必须信任网关的入站 issuer。完整说明见英文版 |
| `POST` | `/api/identity/oauth/complete` | `identity.grant`——as_user（3LO）绑定环节，由 `/auth/return` 页面调用。请求体 `{session_id}`（AgentCore Identity 附加到返回 URL 上的 `session_id`，一次性的不透明凭据）→ 为该会话记录的用户执行 `CompleteResourceTokenAuth` 后返回 `{completed: true, provider, agent_id, agent_name, tool}`；调用者必须就是该用户。`404 identity.session_unknown`（从未签发、已用过或已被撤销清除）、`409 identity.session_expired`（超过 15 分钟）、`403 identity.session_user_mismatch`、`502 identity.session_completion_failed`。见 [identity.md §7.4](identity.md#74-routes-permission-and-errors) |
| `GET` | `/api/identity/grants` | 成员——调用者**自己**的 as_user 授权，绝不返回其他成员的：`{grants[{connection, agent_id, agent_name, tool, scopes[], status: pending\|authorized\|revoked, force_reauth, created_at, updated_at, authorized_at, revoked_at}]}`，按时间倒序（我的授权） |
| `GET` | `/api/identity/grants/{connection}/status?agent_id=` | 成员——`{connection, agent_id, status: none\|pending\|authorized\|revoked, force_reauth, authorized_at}`；Chat 授权卡片轮询此接口直到授权完成（记录到请求前为 `none`）。只有 `authorized` 且 `force_reauth: false` 时授权才可用 |
| `DELETE` | `/api/identity/grants/{connection}` | `identity.grant`——撤销（强制重新授权）：`{revoked: true, provider, agents}`；之后经此凭证的调用会带上 `forceAuthentication=true` 并再次请求授权，只有新的授权完成后撤销才解除 |
| `GET` | `/api/identity/consent-portal` | 成员——`{gateway_id, portal: {id, name, status, status_reason, portal_url, execution_role_arn, connection, scopes[], audience, created_at, …} \| null}`，每次都从 AWS 读回工作区网关的 Consent Portal（无网关时 `gateway_id: null`） |
| `POST` | `/api/identity/consent-portal` | **管理员**——`202 {gateway_id, portal}`。请求体 `{name, description?, connection, scopes[]（默认 [openid]）, audience?, execution_role_arn}`：为 as_user 网关目标创建门户（`connection` 为入站 IdP，与网关 JWT 授权器同一 issuer；执行角色由运维提供）。不可授予成员：`identity.manage` 不覆盖此接口。`409 identity.gateway_missing`／`identity.consent_portal_exists`，`422 identity.invalid_portal_name`／`identity.invalid_role_arn`／`identity.role_account_mismatch`／`identity.portal_scopes`；见 [identity.md §7.6](identity.md#76-consent-portal-as_user-gateway-targets) |
| `DELETE` | `/api/identity/consent-portal` | **管理员**——`{deleted: true}`；`404 identity.consent_portal_not_found` |
| `GET` | `/api/chat/{agent_id}/sessions` | 该 agent 可回放的会话：`{session_id, actor_id, turns, last_at, ended_at, preview, source, live_turn}`——`ended_at` 在控制台显式结束 runtime 会话后写入，仍存活或只是空闲时为 `null`；`source` 为 `console` 或 `public_api`（经 `POST /v1/agents/{id}/invoke[-stream]` 发起的会话，N-SCN-obs1——此类行记录调用方的 `actor_id`，默认 `api`） |
| `GET` | `/api/chat/{agent_id}/history?session_id=` | 某会话已渲染的对话条目，按回放顺序；`live_turn`（N-ARCH-obs15 v2，附加字段）在仍有控制台轮次在服务端运行时为 `{turn, seq, started_at, state: running\|finishing, author}`，否则为 `null`——页面加载时据此重新接入。仅本人会话或管理员，两种可见性模式下一致：其他成员的会话返回 `404 chat.session_not_found`（N16-obs71）；尚无台账行的 id 为空线程。**N16-obs85（新增，仅 `role: "tool"` 行）：**`tool_input`（≤ 512 字符）、`tool_result_summary`（≤ 256 字符）与 `tool_result_status`（`success` \| `error`），仅在运行时流式输出了对应帧时出现（`chat_messages.tool_call`）；早于该列的行与不输出摘要的运行时三者皆无 |
| `GET` | `/api/chat/{agent_id}/memory?session_id=` | **Chat 检查器 → 记忆栏**：该会话在已注册记忆后端中的短期事件与长期记录，并附 `actor_id` = 控制台深链所用的复合 actor `<agent_id>__<会话所有者>`。仅本人会话或管理员，两种可见性模式下一致（N16-obs71——此前在 `shared` 下会回显其他成员的 actor）：`404 chat.session_not_found`；无后端时 `503 feature.unavailable`；外部固定记忆 `409 agent.memory_not_managed` |
| `GET` | `/api/chat/{agent_id}/sessions/{session_id}/turns/{turn}/stream?from=<seq>` | 重新接入某一控制台轮次（N-ARCH-obs15 v2）：重放 `seq > from`（默认 0）的已持久化事件，这一轮仍在运行时持续跟随，最后给出终态 `done`/`error`。未知 id、其他 agent / workspace 的会话，以及——N16-obs71，两种可见性模式下——其他成员的会话一律 `404 chat.session_not_found`（在查看任何轮次之前解析，因此既不泄露会话存在也不泄露作者）；`404 chat.turn_not_found`；`409 chat.turn_not_yours {author}` 只会出现在调用者可读的会话上、且该轮由另一账号发送时（管理员在成员会话中发送的轮次，或反之——归属按轮次计；`meta.author` 即该轮的发送者）；`from` 不是 `0..2^63-1` 范围内的整数时 `422 chat.invalid_seq`，`turn` 超出该范围时 `422 validation.invalid_request`（N16-obs86：无界的值此前会以 Python int 到达 SQLite 并抛出 `OverflowError`）。通过同一会话上的 `POST /v1/agents/{id}/invoke`（同步、无事件日志）产生的轮次也算会话轮次，重新接入它返回 `200`，且只有一帧裸的 `event: done {"turn": n}`——没有 `meta`、没有 `delta`（N16-obs112；答案在 `…/history` 中）。契约见 [docs/cn/interfaces/assistant-turns.md §8](cn/interfaces/assistant-turns.md) |
| `GET` | `/api/traces/{session_id}?lookback_hours=` | **Chat 检查器 → 链路追踪栏**（N-CHAT-obs2，对齐上游）：通过与可观测页相同的多日志组 Logs Insights 数据源（`services/traces.py`）读取该会话的 span → `{session_id, span_count, spans[{name, category: model\|tool\|memory\|policy\|runtime\|other, start_ms, duration_ms, trace_id}], log_groups[], log_group, lookback_hours, cloudwatch_url, log_group_url, unavailable_reason}`。`lookback_hours` 取值 1–336，默认 3。span 按开始时间排序，时间相对于最早的 span。该 id 必须是调用者所在 workspace 的某个 chat 会话（任一 agent——与聊天历史相同的归属规则），且不是其他主体的私有助手会话，否则在任何查询之前返回 404 `chat.session_not_found`；id 形状沿用可观测页的会话 id 字母表（否则 422）。失败降级：Logs Insights 查询失败时返回 200、空追踪栏并带 `unavailable_reason`（如 `observability.query_failed`）。**控制台链接按分区生成**（C2）：`cloudwatch_url` 是工作区控制台主机上的 CloudWatch **Logs Insights** 深链（aws-cn 为 `cn-northwest-1.console.amazonaws.cn`，aws 为 `<region>.console.aws.amazon.com`），预填本追踪栏自身的查询（`filter @message like "<session_id>" \| filter ispresent(name) or ispresent(spanName)`），以贡献 span 的日志组为数据源（无结果时为共享的 `aws/spans`），并以 `lookback_hours` 为相对时间窗；`log_group_url` 打开主日志组（贡献 span 最多的那个） |
| `POST` | `/api/chat/{agent_id}/sessions/{session_id}/stop` | **结束会话**——数据面 `StopRuntimeSession(agentRuntimeArn, runtimeSessionId)` → `{session_id, ended: true, already_ended, ended_at}`。AWS 回 `ResourceNotFoundException`（会话早已结束或因空闲过期）时 `already_ended: true`，视为成功而非错误。台账行保留（历史仍可回放）并打上 `ended_at`；之后若在同一 id 下再发一轮，会被拒绝（`409 chat.session_ended`，N16-obs79）——控制台的下一条提示词会开启新会话。只有 runtime 支撑的 agent 才可结束（`zip_runtime`、`studio`、`container`、已发现的 runtime）；托管 Harness——无论自建还是导入——没有结束会话的操作，返回 409 `chat.session_stop_unsupported`，`detail.reason_code` 为 `harness`。其他 agent 或其他 workspace 的会话——以及（N16-obs70）两种可见性模式下其他成员的会话（成员只能结束自己开启的会话，管理员可结束任何会话）——返回 404 `chat.session_not_found`。撑过 botocore 重试仍然出现的 `RetryableConflictException` 映射为 409 `aws.conflict` |

**残缺与拒绝型的运行时应答（N17-obs96 / N17-obs97，`cn/r17-o`）。** 运行时的应答由所有入口共用的同一
归一化器解析（`services/agentcore/runtime._runtime_payload_events`）。以下两种过去会被渲染成助手回复的
形状属于协议错误：这一轮以终止 `error` 事件结束（Chat；`detail` 带 `error_type`），或在同步入口返回
502 `agent.runtime_error` 信封——绝不会成为文本，也绝不会以 `done` 结束：（1）**不是 JSON** 的 SSE
`data:` 行——AgentCore SDK 用 `json.dumps` 序列化每一帧，因此解析失败的行意味着连接在帧中途断开
（实测的线上形状是 Agent 模板自身的错误帧被截到 300 字符）或运行时在协议之外写了别的东西；
`error_type: MalformedFrame`，`detail.error` 为被截断的文本（有界），无论该行是整个响应还是跟在
正常的 delta 之后（这些 delta 保留，见 assistant-turns.md §8.2）；（2）带 **`code` 且带 `state` 或
`error` 键**的 JSON 响应体——结构化拒绝，例如评估 worker 的 `{"state": "REJECTED", "code":
"evaluation.kind_unsupported", "message": …}` 应答（运行时拒绝了收到的载荷）——`error_type:
RuntimeRejected`，`detail {code, state, error, message}`；自由格式的 `message` 键不再胜出。纯文本
`text/plain` 响应体、开发指南的 `{"answer": …}` / BYOC 的 `{"result": …}` 响应体以及 SDK 的
JSON 字符串帧照常渲染。

结束是显式动作：控制台的「新会话」只在本地忘掉 id，留下的 runtime 会话会自行空闲过期。
重新发布之后应当按「结束会话」——AgentCore 会把存活的会话钉在首次服务它的版本上，
验证新版本需要一个全新的会话。

## 控制台 Agent API——BYOC 上传 / Console Agents API: BYOC uploads

`byoc` 创建方式部署成员自己编写的代码。两种 zip 构件类型（`code_zip`、
`container_source`）先在此暂存归档；返回的 `upload_id` 填入创建请求体的
`spec.byoc`。

| Method | Path | Result |
|---|---|---|
| `POST` | `/api/agents/uploads?python_version=PYTHON_3_13` | `perm:agents.deploy`——`multipart/form-data`，单个名为 `file` 的部件，仅限 `.zip`，≤250 MiB（解压后 ≤750 MiB、条目 ≤2 万；zip-slip/绝对路径/符号链接会被拒绝）。存入制品桶 `byoc/{workspace_id}/{upload_id}/source.zip` + `manifest.json` → `201` `{upload_id, sha256, size_bytes, original_filename, uploaded_by, uploaded_at, entries_count, uncompressed_bytes, detected: {entrypoint_candidates[], has_requirements, has_dockerfile, agentcore_sdk_detected, requirements: {status: ok\|failed\|skipped, package_count, error}}}`——`requirements` 是对 zip 内 requirements.txt 针对部署目标（linux/aarch64 + 可选 `python_version`，默认 PYTHON_3_13）的上传期干跑解析；`failed` 表示部署的 package 阶段会以同样方式失败，`skipped`（无 requirements.txt、解析超时约 90 秒、`uv` 不可用）不代表任何结论 |
| `GET` | `/api/agents/uploads/{upload_id}` | member——已存储的清单（同一形状，另含 `uploaded_by_id`：上传者的 `users.id`，配置文件管理员为 `null`）；其他工作区的 upload_id 返回 404 `byoc.upload_not_found`。**N16-obs77：**套用工作区可见性规则（users-and-ownership §7）——`isolated` 模式下成员只能读取自己上传、管理员上传或未记录上传者（字段出现之前暂存）的清单；被隐藏的清单与不存在的清单同为 404。`shared` 模式下按 id 读取保持开放 |

错误码：`byoc.invalid_upload`（400，缺少部件/非 zip/空文件）、
`byoc.invalid_python_version`（422）、
`byoc.upload_too_large` / `byoc.upload_request_too_large`（413）、
`byoc.zip_invalid`、`byoc.zip_empty`、`byoc.zip_entry_unsafe`、
`byoc.zip_too_many_entries`、`byoc.zip_uncompressed_too_large`（422）、
`byoc.upload_not_found`（404）。

zip 内的 `requirements.txt` 按 pip 文件格式解析（反斜杠续行、行内注释、环境标记
均被支持）；`--hash=` 选项会被丢弃——平台会针对自己的部署目标重新锁定并生成新的
hash。以下内容会被明确报错拒绝：`-r`/`-c` 引用、`-e`/可编辑安装、本地路径、直接
URL/VCS 条目、索引选项（`--index-url`/`--extra-index-url`/`--find-links`——平台
只从自己的索引安装），以及超过 500 条的清单。

`POST /api/agents` 使用 `method: "byoc"` 时携带 `spec.byoc`：
`{artifact_kind: code_zip|container_source|container_image, upload_id?,
image_uri?, entrypoint?（code_zip，默认 main.py）, python_version?
（PYTHON_3_10…PYTHON_3_13，默认 PYTHON_3_13）, install_requirements?（默认
true）, invoke_contract?（launchpad_prompt|raw）, allowed_models?（1–20 个不重复的
Bedrock 基础模型或推理配置文件 ID）}`——zip 类型必须提供
`upload_id`，`container_image` 必须提供本工作区账户+区域内的私有 ECR
`image_uri`。该方式的 `system_prompt` 可选（作为描述使用）；v1 拒绝
tools/toolkits/skills/knowledge_bases 与 `a2a` 协议。部署时服务端会把
`spec.byoc.provenance` 写入 spec（来自上传清单）。

**允许的模型。** 按 Agent 的执行角色把 `bedrock:InvokeModel` 精确限定到
`byoc.allowed_models` 这些模型，用户代码调用其他模型会在运行时收到
`AccessDeniedException`。第 `[0]` 个条目是**主模型**，必须等于 `spec.model_id`：
只发送 `allowed_models` 时服务端把 `model_id` 设为第一个条目；两者都发送时
`model_id` 必须在列表中（会被移到最前）。没有 `allowed_models` 的 spec——包括该
字段出现之前写入的所有行——按 `[spec.model_id]` 处理。重新发布会重写角色策略，
因此编辑后的列表在下次部署生效。部署器把主模型 ID 以环境变量 `MODEL_ID`、完整
列表以环境变量 `ALLOWED_MODEL_IDS`（逗号分隔，主模型在前）传入运行时——两个变量
只要 `spec.env` 已自行设置就以用户值优先。

### 中国区的 TypeScript(Node)容器 Agent(N17-obs465)

上传结果的 `detected.telemetry` 也会识别 Node zip:`{"adot": "node", "runtime": "node",
"adot_node": "<ADOT Node 版本范围>", "cn_exporter": "will_apply" | "not_applicable"}`——依赖
`@aws/aws-distro-opentelemetry-node-autoinstrumentation` 的 `package.json` 过去被读作 `adot: "absent"`。
在中国区，Node 的 `container_source` / `container_image` Agent 部署时会在其镜像之上加上平台的中国区
trace 导出层(ADOT Node 自身无法为中国区导出签名——见 evaluation-engine.md §33.7);
`byoc.cn_trace_exporter: false`(默认 `true`)可关闭。该行会记录 `spec.byoc.telemetry.cn_exporter`
(`applied` | `not_aws_cn` | `opted_out` | `not_node` | `unknown_runtime`)、`member_image` 与
`layer_image`。

`GET /api/agents/{id}/trace-status`(成员可读，只读)返回 `{cn_exporter, member_image, layer_image,
hook: {state, reason, at} | null, traces: {session_id, arriving} | null, badge}`:`hook` 是导出层在
运行时日志中的启动行(`attached` | `native` | `skipped` | `unavailable` | `not_started`),`traces`
表示最新会话(7 天内)是否在 `aws/spans` 中留下 span;`badge` = `ok` | `unavailable` |
`not_arriving` | `unknown`。不存在时返回 404 `agent.not_found`。

## 部署任务对金丝雀发布动作的占有 / Deploy-job ownership of canary publishes (N17-obs13)

`POST /api/runtime-canaries/{id}/action` 的 `action: complete`（晋升）或 `rollback`，在 champion Agent 存在
`queued` / `running` 的部署任务时返回 `409 agent.deploy_in_progress {job_id, stage, job_status}`——与
`DELETE /api/agents/{id}` 相同的信封：二者都会发布该 Agent 的线上版本，因此任务在终结之前占有该 Agent
（`docs/cn/interfaces/agents.md` §1.2）。不写入任何内容（Agent 行、金丝雀行、快照、A/B 分流均不变）；该检查在
`canary.stage_not_ready` 之后、裁决门禁之前。`setup`、`traffic`、`verdict`、`advance`、`cleanup` 只作用于分流，照常进行。
任务终结后，同样的 `complete` / `rollback` 照常返回 202，且重新发布的快照记录的是该任务实际打包的 spec——绝不会是任务进行中
其他写入者放到行上的 spec。**N17-obs16：**自金丝雀 `setup` 以来 champion 的版本已变化时，`complete` / `rollback` 返回 `409 canary.stale_base {setup_version, current_version, action}`（候选版本所基于的基线已不再是生产版本）；金丝雀保持 `running`——请 `cleanup` 后重新发起。

## 变更的所有权 / Ownership of mutations (r13-g)

自 r13-g 起，Agent 重新发布 / 删除所用的「仅创建者或管理员」规则由 `services/ownership.require_creator_or_admin`
统一实现，并覆盖每一类用户管理资源的变更：Runtime 金丝雀除 `verdict` 外的全部动作（`POST
/api/runtime-canaries/{id}/action` → 403 `canary.forbidden` `{canary_id, action, created_by, created_by_name}`）、
Skill Lab 作业的 cancel / resume / publish / delete 与任务集的 PUT / DELETE / apply-expansion
（`skill_lab.forbidden`）、数据集的 PUT / DELETE（`dataset.forbidden`）、自定义评估器的 PUT / DELETE
（`evaluator.forbidden`）。没有创建者记录（`created_by` 为 NULL）的行归管理员。`GET /api/auth/status` 与登录响应携带
调用者的 `user_id`，供控制台镜像该规则。契约：`docs/cn/interfaces/users-and-ownership.md` §8。

## 拒绝优先级——各路由组的门禁顺序 / Refusal precedence

一张表收口 DOC 系列 N17-obs12 / 15 / 17 / 30 / 50 / 51（第 17 轮）：当多个门禁都可能拒绝同一请求时，代码按此顺序作答。
**裁定（负责人，2026-10-20）：所有权优先于一切——所有权门禁不放行的调用者不得获知任何状态。文档遵循代码**——某路由组偏离通用顺序时，偏离在此记录并即为契约；矩阵（`backend/tests/graph/refusal_matrix.py` 的
`KNOWN_ORDER` / `REGISTERED_PAIRS`）引用本节。通用顺序从左到右：schema 422（框架；任何 id 的信封相同）→ 可见性 404
（users-and-ownership.md §7：隐藏 id 得到该类资源原有的 not-found）→ 所有权 403（§8 创建者或管理员）→ 系统 / 预置拒绝 →
`agent.deploy_in_progress` 409（agents.md §1）→ `*.in_use` 409（agents.md §1.1 / §1.3、evaluation-engine.md §22）→ 对象自身的
查找（404 / 422）→ Agent 状态 400 → 语义性请求体校验 422 → 范围查找 404。

| 路由组 | 有序门禁（代码 → 契约） | 相对通用顺序的偏离 |
|---|---|---|
| `POST /api/chat/{id}` 及所有 `/api/chat/{id}/sessions/{sid}/*` 发送 | schema 422 → 隐藏 / 已删除 Agent `404 agent.not_found`（§7、api.md Chat）→ **会话所有权 `404 chat.session_not_found`**（N16-obs70/71）→ Agent 状态 `409 agent.not_active`（N16-obs74）→ 会话自身状态（`409 chat.turn_in_progress`、`409 chat.session_ended`） | 自 `cn/r17-j` 起无偏离——**N17-obs12 即此前的偏离（状态先于所有权），已作为 PRODUCT 修复**：其他成员向正在部署的 Agent 的会话发送读到会话的 404，而非状态 409；所有者的发送仍为 409 |
| `/v1/agents/{id}/invoke[-stream]` | 密钥认证 401 → 密钥范围 / 隐藏 Agent `404 agent.not_found` → Agent 状态 `409 agent.not_active` → `409 chat.session_ended`（N16-obs88：在任何 AWS 调用之前） | 与 Chat 路由相同（`services/invoke.py` 中 `require_session_open`，`invoke_agent_text` 与 `invoke_agent_events` 皆然）。与 Global 一致（2026-10-27）：调用 Agent 不再是策略判定——原先的 `403 policy.denied` / `503 policy.undecidable` 调用门禁已移除；策略只在 Gateway 判定工具调用 |
| `DELETE /api/agents/{id}` | 隐藏 `404 agent.not_found`（§7）→ **创建者或管理员 `403 agent.forbidden`**（§8）→ 预置 `403 agent.system_managed` → `409 agent.deploy_in_progress`（agents.md §1）→ `409 agent.in_use {canaries, runs}`（§1.1 / §1.3）→ 拆除 | **所有权先于部署任务门禁**（N17-obs51）：其他成员删除正在重新发布的 Agent 读到 403 而非 409——与 Chat 系列"状态优先"相反 |
| `POST /api/agents/{id}/redeploy`（所有 `republish_agent` 调用方） | 隐藏 404 → 创建者或管理员 403 → 预置 403 → `409 agent.deploy_in_progress` → `409 agent.in_use {canaries}`（§1.1；运行不阻止重新发布，§22）→ 方式 / 引用 / 工具认证校验 422 / 409 | 与删除相同（N17-obs51） |
| `POST /api/runtime-canaries` | schema 422 → **Agent 状态 `400 canary.agent_not_active`**（隐藏 Agent 同样，E13 §3）→ `400 canary.agent_unsupported`（能力，含预置）→ `409 canary.already_running` | **状态先于 already-running 门禁**（N17-obs15） |
| `POST /api/runtime-canaries/{id}/action` | 隐藏金丝雀 `404 canary.not_found` → 隐藏对照 Agent `404 agent.not_found` → `403 canary.forbidden`（§8，除 `verdict`）→ 系统对象 403 → 清除 `409 assistant.purge_*` → `409 canary.action_in_flight` → `409 canary.stage_not_ready` → 仅 `cleanup`：`409 canary.cleanup_requires_decision`（N17-obs452，运行中且已初始化的金丝雀须先推广或回滚）→ `409 agent.deploy_in_progress`（agents.md §1.2）→ `409 canary.stale_base` → 动作自身门禁（`verdict_blocked` / `override_required`、`422 dataset_required`） | ——（已写于 agents.md §1.2） |
| `POST /api/eval/runs` | schema 422 → `422 run.target_required` → **Agent 状态 `400 agent.not_active`**（隐藏 / 部署中 / 已删除同样，§23）→ 语义性请求体（`422 run.no_evaluators`、范围规则）→ 范围查找（`404 dataset.not_found`） | **状态先于语义性请求体校验与数据集查找**（N17-obs17） |
| `POST /api/eval/runs/{id}/rerun` | 隐藏 / 不存在的运行 `404 run.not_found` → 运行自身规则 `422 run.rerun_unsupported` → Agent 状态 `400 agent.not_active` → 存储请求体校验 `422 run.no_evaluators` → 数据集 `404 dataset.not_found` | **运行自身的拒绝先于 Agent 状态门禁**（N17-obs50，对 rerun 细化 obs17） |
| `DELETE /api/eval/datasets/{id}` / `…/evaluators/{id}`（`?force=`） | 隐藏 404 → **创建者或管理员 `403 dataset.forbidden`**（§8）→ 成员 force `403 auth.forbidden`（§22）→ `409 *.in_use`（§22）→ 删除 | **所有权先于 force 门禁**（N17-obs30）：非创建者成员无论是否带 `force` 都读到 `dataset.forbidden` |
| `DELETE /api/knowledge-bases/{kb}`（`?force=`） | 隐藏 404 → 成员 force `403 auth.forbidden`（N17-obs4）→ 预置挂载 `409 kb.attached_to_system_agent` → `409 kb.has_attached_agents`（除非 force）→ 挂载 Agent 部署中 `409 agent.deploy_in_progress`（N17-obs31）→ 剥离 + 删除 | —— |

本表蕴含的经验法则：（1）隐藏或已删除的 id 总是先得到该类资源的 not-found（§7）；（2）所有权紧随其后，在每条路由上——**Agent 变更**路由（删除 / 重新发布）上
创建者或管理员 403 先于部署任务 409 与 in_use 409（N17-obs51，DOC：agents.md §1 的“成员与管理员一致”指所有权门禁放行的每个调用者），**Chat** 路由上个人会话的 404 先于状态 409（N17-obs12，已修复）；
（3）对所有权门禁放行的调用者，在**创建与发送**路由（Chat、`/v1`、金丝雀创建、运行创建）上 Agent 状态门禁先于对象自身的会话 / 金丝雀 / 运行门禁，但在 `rerun` 上后于运行自身的查找；
（4）成员的 `force` 在数据集 / 评估器上于所有权之后被拒绝，在知识库上则最先被拒绝。

## 控制台 Agent API——版本与端点 / Console Agents API

`GET /api/agents/{agent_id}/versions` 是 Agent 详情「版本与端点」面板背后的只读 AWS 视图。它对该 Agent
所属资源族的两个列表操作跟随每一页 `nextToken`,并返回白名单投影——不含环境变量、制品位置、执行角色或
鉴权配置。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/agents/{agent_id}/versions` | `{kind: runtime\|harness, resource_id, versions[{version, status, description, last_updated_at}], endpoints[{name, live_version, target_version, status, description, created_at, last_updated_at, failure_reason}], latest_version, ledger_version, canary_endpoints[]}`——`versions` 最新在前;`endpoints` 先 `DEFAULT` 再按名称;`latest_version` 是 AWS 报告的最高版本,`ledger_version` 是最近一次 Launchpad 部署记录的版本(`Agent.version`),带外更新或金丝雀候选版本铸造后二者可能不同;`canary_endpoints` 列出仍然存在的 `stable`/`treatment` 端点名。资源族:`zip_runtime`/`studio`/`container` 以及 `spec.discovery.resource_type` 缺省或为 `runtime` 的导入行 → `ListAgentRuntimeVersions` + `ListAgentRuntimeEndpoints`;`harness` 以及 `resource_type == "harness"` 的导入行 → `ListHarnessVersions` + `ListHarnessEndpoints`(harness 版本没有描述字段)。不改变任何状态 |
| `GET` | `/api/agents/{agent_id}/conversions` | 成员——`{source: {id, name, method, status}, conversions: [agent…]}`：由该 Agent 转换出的 **Runtime 孪生**（`POST …/convert` 会在新的 `-rt` Agent 上标记 `spec.source_harness.agent_id`），最新在前，每行是普通的 Agent 投影加上它最近一次的 `deployment`。纯账本读取（不调用 AWS）；已删除的孪生不列出；来源不存在或已删除 → 404 `agent.not_found`。助手的 NEXT STEPS 用它在孪生 `active` 后切换目标，并在刷新页面后重新找到孪生 |

**规格快照（上游 #277，CN 仅只读）。** 每次发布都会在台账（`spec_snapshots`，按 workspace 划分）留下一条带编号的 Agent 规格快照：创建、重新发布、接受推荐、架构师批准与预置发布都经 `create_deployment` 写入；Runtime 金丝雀的晋升自行记录一条。早于该表的 Agent 会在启动时获得快照 #1（`note: "baseline"`）。发布成功后，快照会盖上它上线的 AWS 版本，并把规格替换为**实际部署**的规格；失败的发布保持 `aws_version: null`。评估记分卡把 `seq` 读作某次运行所评估的 Agent 版本。已删除 Agent 的历史仍可读取（其运行保留版本）；其他 workspace 的 Agent 或调用者看不到的 Agent → `404 agent.not_found`。上游的 `POST …/snapshots/{seq}/rollback` **未**移植——重新发布即可发布任意规格。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/agents/{agent_id}/snapshots` | 成员——`{snapshots[{seq, agent_id, aws_version, deployed, state, deployment_id, note, created_by, created_by_name, created_at}]}`，最新在前，不含 `spec`。`state` 为 `deployed` \| `deploying` \| `failed` \| `not_deployed` |
| `GET` | `/api/agents/{agent_id}/snapshots/diff` | 成员——`?from_seq=&to_seq=`。省略的一侧即 Agent 的**当前**规格；两侧都省略 → `422 snapshot.diff_side_required`。结果 `{from, to, changes[{path, group, kind, before, after, added?, removed?, elided?}]}`，其中 `from`/`to` 是快照或 `null`（当前）。`group` 为 `prompt` \| `model` \| `tools` \| `skills` \| `knowledge_bases` \| `memory` \| `guardrail` \| `other`，`kind` 为 `added` \| `removed` \| `changed`。嵌套字段按点号路径比较，列表按成员比较（`added`/`removed`），`code`、`code_bundle`、`studio_flow` 与 `system_prompt` 整体比较；超过 2 KB 的内容以 `{length}` 返回并带 `elided: true` |
| `GET` | `/api/agents/{agent_id}/snapshots/{seq}` | 成员——单条快照，含 `spec`。`404 snapshot.not_found` |

错误码:`agent.not_found`(404,未知 id 或其他 workspace 的 Agent)、`agent.no_resource`(409,该行没有可查询的
AWS 资源——部署仍在进行、首次部署失败、已删除,或既非 Runtime 也非 Harness 的形态;`message` 即面板展示的
人类可读原因)。AWS `ClientError` 映射为标准 4xx 信封。

## 控制台 Tools API —— 内置工具演示 / Console Tools API — builtin demos

`/api/demos/*` 以 hub 自身凭证从 Tools 页驱动两个 AgentCore 内置工具（跨账号工作区会被提前拒绝：`409 workspace.cross_account_tool_unavailable`）。

| 方法 | 路径 | 结果 |
|---|---|---|
| `POST` | `/api/demos/browser` | `{url, web_bot_auth?, browser_identifier?, profile_identifier?, save_profile?}` → 启动一个 `aws.browser.v1`（或所选 Web Bot Auth 浏览器）会话，通过会话的 CDP 自动化流打开一次 `url`，返回 `{url, title, session_id, latency_ms, live_view_url, live_view_expires_in, viewport, browser_identifier, web_bot_auth, profile_identifier, save_profile}`；会话为 DCV 实时画面保留至 `DELETE` 或 300 s 到期。**N15-obs17（C2）：**流与实时画面 URL 由 hub 按 boto3 为工作区区域解析出的数据面端点自行签名（`services/agentcore/browser_stream.py`——与 Harness `browser` 内置工具相同的做法；SDK 1.17.0 自带的构造器会拨向仅 Global 存在的主机，在 aws-cn 为 `ENOTFOUND`）。`422 tools.url_blocked`（私网／链路本地／字面 IP 目标）、`422 tools.browser_profile_required`、**`409 browser.signing_unsupported_in_partition {partition}`**（N17-obs258：在没有 Web Bot Auth 签名服务的分区——即所有 aws-cn 工作区——请求 `web_bot_auth: true`；先于任何 AgentCore 调用判定；`GET /api/capabilities` 带 `tools: {browser_signing}`，控制台据此在该分区隐藏开关）、`409 tools.browser_required` / `tools.browser_web_bot_auth_unavailable` / `tools.browser_profile_unavailable`、`502 tools.browser_configuration_failed`；**`502 demo.browser_failed {reason}`**：会话启动后的任何失败（启动、拨流、导航——`reason` 为一行有界的 `Type: message`，≤ 300 字符，绝无堆栈），且 SDK 会话总是先被停止 |
| `DELETE` | `/api/demos/browser/{session_id}` | 停止保留中的演示会话 → `{session_id, stopped, profile_saved}`；未知、已过期或属于其他工作区的 id 返回 `stopped: false`（从不 404）。`502 tools.browser_stop_failed` |
| `GET` | `/api/demos/browser/options` | 演示表单可选的 Web Bot Auth 浏览器与浏览器配置档。`502 tools.browser_options_failed` |
| `POST` | `/api/demos/code-interpreter` | `{code}` → 在工作区的 `aws.codeinterpreter.v1` 会话中执行并返回输出 |

## 控制台评估数据集 API / Console Evaluation Datasets API

`/api/eval/datasets` 保存本地 scenario 数据集(SQLite,可编辑的事实来源)及其各自对应的一个 AWS Dataset。AWS 数据集由一份**草稿(DRAFT)**加若干不可变的编号**版本**组成:「同步 AWS」首次创建数据集,之后原地替换草稿中的示例;「发布版本」把草稿快照为版本。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/eval/datasets` | `{datasets[]}`——本地行,含 `items`、`kind`、`has_ground_truth` 与 `cloud` blob |
| `POST` | `/api/eval/datasets` | 由条目创建(devguide scenario、模拟 persona 或旧式 prompt;kind 自动推断)→ 201 |
| `PUT` · `DELETE` | `/api/eval/datasets/{dataset_id}` | **仅创建者或管理员**（403 `dataset.forbidden` `{dataset_id, action, created_by, created_by_name}`；无创建者记录的行归管理员）。编辑(kind 不可变 → 400 `dataset.kind_immutable`)/ 删除本地行;已同步的 AWS 副本保留。删除时若仍有**进行中**的运行在回放它，或某个数据处理任务以它为输出 → **409 `dataset.in_use`** `{id, referrers[{kind, id, name, status}], total, truncated, force_allowed: "admin"}`（已结束的运行只保留快照，永不阻止）；管理员可加 `?force=true` → `{deleted, forced: true}`（成员带 `force` → 403 `auth.forbidden`）。契约：`docs/cn/interfaces/evaluation-engine.md` §22**两个 403 的先后（N17-obs30）：**先是创建者或管理员门禁——非创建者的成员无论是否带 `force` 都得到 `403 dataset.forbidden`——再是 force 门禁：创建者自己带 `?force=true` 得到 `403 auth.forbidden`（该标志是管理员覆盖）。 |
| `POST` | `/api/eval/datasets/{dataset_id}/sync-to-aws` | **仅创建者或管理员**（403 `dataset.forbidden {dataset_id, action: "sync"}`——它会重写该行的 `cloud` 块与共享的云端草稿）。没有存活云端副本时:`CreateDataset`(内联示例)并轮询到 `ACTIVE`。有副本时:**原地编辑其草稿**——`ListDatasetExamples` → `DeleteDatasetExamples`(草稿为空时跳过)→ 用归一化后的 scenario 调 `AddDatasetExamples`,每步经 `UPDATING` 轮询到 `ACTIVE`;数据集 id 与已发布版本保留,草稿变为 `MODIFIED`。AWS 已不认识的副本(`GetDataset` 返回 `ResourceNotFoundException`)或标为 `deleted` 的副本会重新创建。返回该行;`CREATE_FAILED` / `UPDATE_FAILED` / 超时 → 502 `dataset.sync_failed`,携带 AWS `failureReason` 并记录到 blob |
| `POST` | `/api/eval/datasets/{dataset_id}/publish-version` | **仅创建者或管理员**（403 `dataset.forbidden {dataset_id, action: "publish"}`）。对该行的云端副本调 `CreateDatasetVersion`,经 `UPDATING` 轮询到 `ACTIVE` → 返回该行,新版本位于 `cloud.versions` 首位且 `cloud.draft_status == "UNMODIFIED"`。无存活副本 → 409 `dataset.not_synced`;`UPDATE_FAILED` / 超时 → 502 `dataset.publish_failed`(原因记录到 blob,版本列表保留) |
| `GET` | `/api/eval/datasets/cloud` | workspace 区域内的全部 AWS 数据集:`{datasets[{datasetId, name, status, schemaType, exampleCount, draftStatus, updatedAt}]}` |
| `GET` | `/api/eval/datasets/cloud/{cloud_id}` | 草稿详情:`{datasetId, name, status, schemaType, exampleCount, draft_status, failure_reason, versions[{version, example_count, created_at}], runnable, has_ground_truth}`——版本最新在前 |
| `POST` | `/api/eval/datasets/cloud/{cloud_id}/publish-version` | **派生的创建者或管理员规则**：调用者必须对指向该存储的每一个本地行都有变更权（403 `dataset.forbidden {cloud_id, dataset_id, action}` 指出第一个不允许的行）；没有任何本地行指向的存储保持开放。仅云端数据集的「发布版本」→ 返回上述刷新后的详情;失败语义与本地路由相同 |
| `DELETE` | `/api/eval/datasets/cloud/{cloud_id}` | **派生的创建者或管理员规则**（对每个指向它的本地行，403 `dataset.forbidden {cloud_id, dataset_id, action}`，先于 409）。`DeleteDataset`——删除草稿与全部版本;指向它的本地行标为 `cloud.status = "deleted"`,下次同步重新创建 |
| `DELETE` | `/api/eval/datasets/cloud/{cloud_id}/versions/{version}` | **派生的创建者或管理员规则**（对每个指向它的本地行，403 `dataset.forbidden`，先于 `dataset.version_pinned` 409）。带 `datasetVersion` 的 `DeleteDataset`——删除单个已发布版本;草稿与其他版本保留,缓存列表随之刷新 |

本地行上的 `cloud` blob:`{dataset_id, arn, status, synced_at, failure_reason, draft_status (MODIFIED|UNMODIFIED), example_count, versions[{version, example_count, created_at}]}`。它只缓存展示状态——AWS 是事实来源,每次变更都会重新读取 `GetDataset` / `ListDatasetVersions`。

## 控制台数据处理 API（V2）/ Console Data Processing API (V2)

控制台 V2 的数据中心把已观测的会话（Agent 轨迹）转换为本地评估数据集。下面两类路由共用同一个提取器（`app/evaluation/pipelines.py`）：通过可观测服务读取会话对话（与 `/api/observability/sessions` 相同的私有会话可见性规则），把每条用户输入与其后的 Agent 回复配对，每个会话写入一个 **predefined** 场景（`scenario_id = trace-<session>`、`turns[{input, expected_response}]`、`metadata.source = "trace"`）；目标为 **legacy** 数据集时写入首轮的 `{prompt, expected}`。模拟用户数据集会被拒绝（400 `dataset.kind_unsupported`）。合并时跳过已存在的 scenario id、可选跳过相同的首轮输入（`dedupe`），并遵守 200 条上限；这里不调用任何模型。

| 方法 | 路径 | 行为 |
|---|---|---|
| `POST` | `/api/eval/datasets/from-sessions` | `{session_ids[1..50], range, dataset_id 与 name 二选一, description?, first_turn_only?, dedupe?}`；可选 `?sync=true|false`。**≤ 10 个会话（或 `?sync=true`）** → 201 `{dataset, added, skipped[{session_id, reason}], timing{elapsed_ms, sessions, read, workers}}`（逐会话的 span 读取在 4 个工作线程上并发执行，N-SCN-obs10；条目保持请求顺序）。**> 10 个会话（或 `?sync=false`）** → 202 `{job_id, status: "queued", sessions, poll: "/api/jobs/{job_id}"}`——数据集由任务（`type: eval_dataset_from_sessions`）写入；轮询 `GET /api/jobs/{job_id}`：先是 `payload.progress{read, total}`，`succeeded` 时给出 `payload.result{dataset{id, name, kind, item_count}, added, skipped, timing}`，`failed` 时给出 `payload.error{code, message, detail}`。超过 10 个会话仍带 `?sync=true` → 400 `from_sessions.sync_too_large`。原因：`not_found`、`no_transcript`、`no_exchange`，以及（`session_id` 为空时）`duplicate` / `dataset_full`。新建数据集却没有可用内容 → 422 `dataset.nothing_extracted`，`detail.skipped` 给出原因（任务路径下见 `payload.error`） |
| `GET` · `POST` | `/api/eval/pipelines` | 列出 / 创建保存的处理任务：`{name, description, source{agent, range, status: all\|ok\|error, max_sessions ≤ 50}, processing{first_turn_only, dedupe, min_input_chars}, output{dataset_id 与 dataset_name 二选一}}`（输出数据集不存在或为模拟用户 → 404 / 400） |
| `GET` · `PUT` · `DELETE` | `/api/eval/pipelines/{pipeline_id}` | 读取 / 整体替换（运行中 → 409 `pipeline.running`）/ 删除——输出数据集保留 |
| `POST` | `/api/eval/pipelines/{pipeline_id}/run` | 返回 `202` 与状态为 `running` 的行（已在运行 → 409 `pipeline.running`），有界的运行在后台进行：列出时间窗口内的会话，按 Agent / 状态过滤，读取最新的至多 `max_sessions` 个，提取并合并。结果写入该行的 `last_run{at, scanned, matched, added, skipped, dataset_id, error}`，`status` 为 `succeeded` / `failed`（服务重启中断的运行读回为 `failed`）；`dataset_name` 输出在首次运行时创建，之后该 Pipeline 指向其 id |
| `GET` | `/api/eval/log-services?hours=1..336&log_group=…` | 在 `aws/spans`（以及所给的 `log_group`——span 发往 Agent 自身日志组的情形）的 span 中出现过的服务名称，一次 Logs Insights 查询 → `{services[{service_name, spans, sessions, last_seen, log_group_names, agent}], log_groups, hours}`。`log_group_names` 是建议的批量评估输入：`aws/spans` 加 span 资源属性（`aws.log.group.names`）指向的内容日志组；`agent` 为拥有该服务的平台 Agent，没有则为 `null`；`scopes` 为其 span 的插桩 scope，`evaluable` 表示 AgentCore Evaluation 是否会把其中任一 scope 作为 Agent span 读取（受支持框架的 scope，或通用前缀 `opentelemetry.instrumentation.*` / `openinference.instrumentation.*`，但不含 botocore、starlette 等传输层插桩；`null` 表示未知）——只有自定义 scope 的服务每个会话都会以「No evaluable agent spans found」失败 |
| `GET` | `/api/eval/log-groups?q=` | 名称包含 `q` 的日志组（不区分大小写的 `logGroupNamePattern`），最多 150 个 → `{log_groups[{name, created_at, retention_days, stored_bytes}], truncated}` |
| `GET` | `/api/eval/log-sessions?service_name=&log_group=…&hours=&q=` | `log_source` 任务的「日志」数据源：所给日志组（1–10 个）中 `service_name` 的会话，从其 span 中发现（span 是同时带有 `service.name` 与 `session.id` 的记录），按最近活动倒序，最多 500 个；行结构与 `/agents/{id}/log-streams` 相同（`stream` 为会话最近一条记录所在的日志组 · 日志流，另有 `traces`）。`q` 保留任意记录（span 或内容日志）包含该关键字的会话（不区分大小写的正则匹配，附命中数与摘录）。其他主体的私有助手会话不会列出 |
| `GET` | `/api/eval/agents/{agent_id}/log-streams?hours=1..336&q=` | 评估任务向导的「日志」数据源（`app/evaluation/log_streams.py`）：Agent 运行日志组中在时间窗口内有事件的日志流，按最近事件倒序 → `{log_group, streams[{stream, session_id, kind, first_event, last_event, match, matches, snippet}], truncated, hours, q}`。`kind`：`session`（代码运行时的 `[runtime-logs-<sessionId>]` 日志流）、`otel_session`（某会话在 `otel-rt-logs` 中的切片——Harness 运行时按 microVM 命名日志流，会话只存在于该共享流）或 `shared`（不属于单个会话，不可选）。`q` 保留流名包含该关键字（不区分大小写）或事件中包含该完整词（`FilterLogEvents`，区分大小写；`otel-rt-logs` 中的命中归到事件所属会话）的行，`match` 为 `name` / `content`，并给出命中数与摘录。两类扫描都有上限（500 个日志流、10 页过滤结果），`truncated` 表示上限遮住了部分会话。其他主体的私有助手会话不会列出。选中的 `session_id` 以普通 `POST /api/eval/runs`（`session_ids` + `session_source: "logs"`）发起评估 |

所有路由均为 `MEMBER` 且按工作区隔离（`eval_pipelines.workspace_id`）。

## 控制台评估器 API / Console Evaluators API

`/api/eval/evaluators` 是 `?view=evaluators` 子页背后的自定义评估器 CRUD。AWS 是唯一事实来源（没有 ledger 行）；内置与第三方评估器只读。一个自定义评估器恰好有三种**定义**之一，由载荷里出现的字段决定——`instructions`（LLM 评审，`llmAsAJudge`）、`base_evaluator_id`（派生，`derived`）或 `lambda_arn`（代码评估器，`codeBased.lambdaConfig`）；同时给出两个或一个都没有 → 400 `evaluator.definition_ambiguous`。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/eval/evaluators` | `{evaluators[], builtin_count}`——先是本地内置目录（`source: builtin`，轨迹匹配器标 `requires_ground_truth`），再是本 workspace 账本中的自定义评估器行（`eval_evaluators`；`source: custom`、`name`、`evaluator_type`、`provider`、`status`、`definition: judge \| derived \| code`、创建者字段；按 workspace 隔离过滤）。CN 没有账号级 `ListEvaluators` 调用，因此没有部分失败标记：上游的 `custom_unavailable`（#257）在此不存在——账本读取失败即 500，控制台显示为加载错误，绝不会显示为空的自定义区 |
| `POST` | `/api/eval/evaluators` | 创建 → 201 `{evaluator_id, arn}`。公共字段：`name`（`^[a-zA-Z][a-zA-Z0-9_]{0,47}$`）、`description`。**评审**：`instructions`（10–4000 字符，至少一个 `{placeholder}`，否则 422 `evaluator.missing_placeholder`）、`rating_scale[≥2]`（默认 pass/fail）、`model_id`、`level`（TOOL_CALL \| TRACE \| SESSION，默认 TRACE）。**派生**：`base_evaluator_id`（`Builtin.*` \| `ThirdParty.*`；不存在 → 400 `evaluator.base_not_found`）、`model_id`；级别取自基础评估器。**代码评估器**：`lambda_arn`（`arn:aws[-partition]:lambda:<region>:<account>:function:<name>[:qualifier]`）、`lambda_timeout_s` 1–300（默认 60）、`level`；Lambda 必须与 workspace 同区域，否则 422 `evaluator.lambda_region_mismatch`（`detail: {lambda_region, workspace_region}`）。派生或代码载荷携带 `rating_scale` → 400 `evaluator.rating_scale_not_allowed`。任何定义在 `CreateEvaluator` 上都必须带 `level` |
| `GET` | `/api/eval/evaluators/{evaluator_id}` | `{id, name, level, description, definition, instructions, rating_scale, model_id, base_evaluator_id, lambda_arn, lambda_timeout_s, evaluator_type, provider, status}`——其他定义的字段为空/null（代码评估器的 `instructions: ""`、`rating_scale: []`、`model_id: null`） |
| `PUT` | `/api/eval/evaluators/{evaluator_id}` | **仅创建者或管理员**（403 `evaluator.forbidden` `{evaluator_id, action, created_by, created_by_name}`；无创建者 → 仅管理员）。全量配置替换（`UpdateEvaluator` 接收完整配置，因此每个字段都要回传），载荷与创建相同但不含 `name` → 返回刷新后的详情。载荷必须与评估器**当前**定义一致：用评审/派生载荷更新代码评估器，或用代码载荷更新评审/派生评估器 → 400 `evaluator.definition_mismatch`（`detail: {current, payload}`）——评估器绝不会被转换。托管 id → 400 `evaluator.builtin_immutable` |
| `DELETE` | `/api/eval/evaluators/{evaluator_id}` | **仅创建者或管理员**（403 `evaluator.forbidden`）。→ `{deleted: true}`（强制删除时附加 `forced: true`）；托管 id → 400 `evaluator.builtin_immutable`；由助手操作托管的 → 409 `evaluator.managed_by_operation`（最先判定，带不带 `force` 都一样）。若仍被进行中的运行、任意状态的在线评估配置或运行中的金丝雀引用 → **409 `evaluator.in_use`** `{id, referrers[{kind: run\|online_config\|canary, id, name, status}], total, truncated, force_allowed: "admin"}`（已结束的运行永不阻止）；管理员可 `?force=true`（引用方保留悬空 id），成员带 `force` → 403 `auth.forbidden`。契约：`docs/cn/interfaces/evaluation-engine.md` §22 |

**代码评估器（Lambda）契约。** 服务以 `{schemaVersion, evaluatorId, evaluatorName, evaluationLevel, evaluationInput.sessionSpans, evaluationReferenceInputs, evaluationTarget}` 调用函数，函数返回 `{label, value?, explanation?}` 或 `{errorCode, errorMessage}`；单次调用受所配置的超时（≤ 300 秒）与 6 MB 载荷限制。**控制台不管理其 IAM**：平台在批量与在线运行中作为 `evaluationExecutionRoleArn` 传入的评估执行角色需要对该函数拥有 `lambda:InvokeFunction` + `lambda:GetFunction` 权限，且函数的资源策略须允许 `bedrock-agentcore.amazonaws.com` 主体（用 `aws:SourceAccount` / `aws:SourceArn` 收窄）。创建时两者都不做检查——针对角色无法调用的函数发起运行会像任何评估器错误一样按会话失败。

## 控制台评估运行 API / Console Evaluation Runs API

`/api/eval/runs` 通过有界运行队列(`eval_max_concurrent_runs`,上限 20；CN 构建的每次运行是自托管评估 worker 上的一个作业,不受账户 5 个活跃批量评估配额约束)驱动批量评估 / insights 分析。运行状态:`queued → invoking → waiting → evaluating → completed | failed | stopped`。每一行都带 `stop_requested`(操作员已请求停止,批次仍在 STOPPING)。

`POST /api/eval/runs` 还接受可选的 `name`（1–64）与 `description`（≤ 1000）——控制台 V2 以命名的评估任务列出运行——每一行都带这两个字段（未命名时为 null）以及 `updated_at`。每一行还带 `hidden: {agent, dataset}`（N15-obs8）：当 Workspace 隔离对调用方隐藏了该运行引用的数据集（本地行或云端存储）或 Agent 时，引用保留 id，`dataset_name` / `agent_name` 为 `null` 且该标志为 `true`——控制台渲染占位符；管理员、运行的创建者与 shared 模式始终读到名称。

每个运行行（列表、详情、创建、停止、重新检查、再评一次）还带 `snapshot`：该运行评估的 Agent 版本，`{seq, source}`，`seq` 是 Agent 配置快照的序号（Agent 页面上的「快照 #N」）。`source` 取 `stamped`（提交时记录：已部署到 AWS 的最新快照——仍在部署中的快照并不是运行实际调用的版本）、`inferred`（旧行，按快照时间线推断：运行创建时已部署的最新快照，没有则取最早的快照）或 `observed`（时间窗口 / 选定会话 / 日志流运行，其会话早于运行本身：取运行创建时线上的版本）。日志来源运行、没有已部署快照的 Agent，以及早于所有快照、且在该运行与最早快照之间 Agent 又成功部署过的运行（无法确定评估的是哪个版本）为 `null`。推断出的版本从不回写。

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/api/eval/runs?limit&offset&mode&agent_id` | 最新在前的分页 `{runs, total, limit, offset}` |
| `GET` | `/api/eval/runs/{run_id}` | 单个运行(分数 / insight 树 / `batch_eval_id` / `budget_stops` / `scenario_failures`——重试后仍失败而被跳过的模拟用户场景，其余场景照常运行，`[{scenario_id, retries, code, error}]`（无则为 `[]`；所有场景都失败时运行才失败——回放失败的预定义场景记在 `session_failures` 中）/ `errors` / `session_failures` / `error` / `stop_requested`)。 |
| `GET` | `/api/eval/scorecard?agent_id=` | **Agent 评分卡**，只读账本（不调用 AWS）→ `{runs, agents, truncated}`。`runs` = 工作空间中查看者可见的评估器模式运行（不含 insights 运行；工作空间隔离时按运行创建者的规则过滤，且查看者看不到的 Agent 的运行会被去掉），取最新的 1000 个，按时间正序（截断了更早的运行时 `truncated: true`）：`{id, name, agent_key, status, error, created_at, updated_at, evaluators, scores, sessions, scope, dataset_name, dataset_version, benchmark, snapshot, created_by_name}`。`agent_key` 为 Agent id，日志来源运行为 `logs:<service_name>`。`scope`：`dataset` \| `cloud` \| `window` \| `sessions` \| `logs`。`benchmark` = `{kind, key, label, dataset_id, revision, size}`：本地数据集为 `dataset` / `ds:<id>`，label 为其当前名称（数据集已删除时为运行上存储的名称），`revision` 为运行所回放用例的指纹（对规范化后的场景做 sha256 取前 16 位十六进制；旧行为 null）；云端数据集为 `cloud` / `cloud:<id>`，`revision` 为 `v<N>`（固定版本）或 `draft:<fingerprint>`；所有被动运行（时间窗口、会话、日志）都归入该 Agent 的 `live` 基准（`key: "live"`，label / revision 为 null）。`size` 为回放的场景数。`agents`（按最近评估倒序）= `{key, agent_id, name, method, log_source, deleted, current_seq, versions: [{seq, aws_version, created_at, created_by_name, note}]}`——`current_seq` 是已部署的最新快照；已删除的 Agent 保留其运行，使用存储的名称，版本时间线为空。带 `agent_id` 时只读该 Agent（可见时即使还没有运行也会列出），每个版本另带 `changes`：相对其之前已部署的最新快照的差异分组（`prompt`、`model`、`tools` 等）。**N15-obs15（隔离）：** 数据集对查看者不可见的运行保留 `dataset_id` / `benchmark.key`，但 `dataset_name: null`、`benchmark.label: null`、`benchmark.hidden: true`、`hidden: {agent: false, dataset: true}`（`benchmark.hidden` / `hidden` 为附加字段，始终存在）；管理员、创建者与共享工作空间看到名称 |
| `GET` | `/api/eval/runs/compare?a=&b=` | **并排比较两次运行** → `{runs: {a, b}, alignment, alignment_reason, cases, results, config}`。两次运行都必须是已结束的评估器运行（否则 422 `run.compare_unsupported`）；不存在、属于其他工作空间或被隐藏 → 404 `run.not_found`。`runs.*` 为运行行加上其 `benchmark`。`alignment`：`scenario`（两次运行都记录了每个会话回放的场景——`EvalRun.scenario_map`）、`order`（没有该映射的旧运行，仅当它没有跳过任何场景、且会话数恰好等于其数据集当前用例（或其固定的云端版本）的场景数时按位置配对）或 `none` 并附 `alignment_reason`（`live_traffic`、`different_benchmark`、`scenarios_skipped`、`session_count_mismatch`、`dataset_unavailable`——已删除、无法读取或对查看者隐藏）：只比较总体分数，不读取任何结果。对齐时，`cases` = `[{scenario_id, input, a: {session_id, results} \| null, b: …}]`，按 B 的场景顺序排列，其后是只有 A 有的场景（`input` ≤ 200 个字符），每一侧的评审记录来自其批量评估结果流（每侧读取一次，与 `GET /runs/{id}/results` 相同）；`results` = `{a: {available, reason?, detail?, truncated?}, b: …}`——某一侧无法读取时只影响该侧（其用例的 `results` 为空）。两个版本都已知且不同时，`config` = `{agent_id, from_seq, to_seq, between: [{seq, aws_version, note, created_at, created_by_name}]}`（`between` 为较小序号之后直到较大序号的快照）；同一版本时为 `{same: true, agent_id, seq}`；版本未知、不是同一个 Agent 或该 Agent 对查看者隐藏时为 `null`。差异本身由 `GET /api/agents/{id}/snapshots/diff?from_seq=&to_seq=` 提供。每一侧为运行负载（隔离下 `dataset_name` / `agent_name` 按 `GET /api/eval/runs` 的规则遮蔽）加 `benchmark`；数据集对查看者隐藏时其 `label` 为 null、`hidden` 为 true（N15-obs15） |
| `GET` | `/api/eval/runs/{run_id}/results` | **终态评估器运行的逐会话评审记录** —— `{available, sessions: [{session_id, results: [{evaluator_id, level, score, label, explanation, error_type, error_message}]}], count, truncated}`。账本行只保存每个评估器的平均分;每个分数背后评审模型给出的理由只存在于批次自己的结果日志流中(`GetBatchEvaluation.outputConfig.cloudWatchConfig`,与在线评估视图读取的 `gen_ai.evaluation.result` 记录同族),该路由按需读取、绝不持久化。会话顺序与运行的 `session_ids` 一致。无可读内容时返回 `available=false` + `reason`(`insights_run` \| `no_batch` \| `run_active` \| `stream_missing` \| `unreadable` + `detail`)而非错误。不存在 → 404 `run.not_found` |
| `POST` | `/api/eval/runs` | 启动运行(范围四选一:`dataset_id` \| `cloud_dataset_id` \| `session_ids` \| `lookback_hours`)→ 201。`session_source: "logs"`（仅用于展示，只能搭配 `session_ids` 范围，否则 422 `run.session_source_scope`）把范围记为 `dataset_name` `logs:<n>`，与 `window:<N>h` 相同。**评估对象**：`agent_id` 与 `log_source {service_name, log_group_names[1..10]}` 二选一（否则 422 `run.target_required`）。`log_source` 运行评估已在 CloudWatch 中、背后没有平台 Agent 的遥测（例如未托管在 AgentCore Runtime 上的 Agent）：批量评估的 `cloudWatchLogs` 数据源即 `serviceNames: [service_name]` 加所给的 `logGroupNames`，只支持被动范围（`session_ids` / `lookback_hours`；数据集范围 → 422 `run.log_source_scope`），且每个日志组必须存在（422 `run.log_group_missing`，`detail.missing`）。运行行回显 `log_source`，`agent_id` 为 `""`，`agent_name` 为服务名称。`cloud_dataset_id` 范围可附带 `dataset_version`(已发布版本号,如 `"2"`,绝不是 `DRAFT`;省略即草稿):版本必须存在于 `ListDatasetVersions`(否则 422 `run.dataset_version_unknown`,不创建运行行),`GetDataset` / `ListDatasetExamples` 读取该快照。`dataset_version` 搭配其他范围 → 422 `run.dataset_version_scope`。每个运行行都回显 `dataset_version`(草稿、本地、session 与时间窗口运行为 `null`)。**评估器**（`evaluators` 模式，默认）：省略 → 内置集合；显式 `evaluators: []` → 在创建任何行之前 422 `run.no_evaluators`（N13-obs18——什么都不评分的运行会被拒绝；insights 运行忽略该字段）。**insights 时间窗口**（`mode: insights` + `lookback_hours`）：窗口在请求时即解析为 session id（一次 Logs Insights 查询）；零个 session → 在创建任何行或作业之前 409 `run.no_sessions`（`detail.lookback_hours`，N13-obs19），否则运行行从创建起就带有解析出的 `session_ids`。evaluators 模式的窗口仍在队列 worker 上解析。**拒绝优先级（N17-obs17）：**先判目标状态——`deploying` / `failed` / `deleted` / 不可见的 `agent_id` 返回 `400 agent.not_active`——再校验请求体（显式 `evaluators: []` 返回 `422 run.no_evaluators`、`422 run.target_required` 等），最后查找范围（`404 dataset.not_found`）；因此非活跃 Agent 上的 `evaluators: []` 读到的是 400 而非 422。 |
| `POST` | `/api/eval/runs/{run_id}/stop` | **停止活跃运行** → 202 返回该运行。批次已在 AWS 上存在(`batch_eval_id` 非空)时调用 `StopBatchEvaluation`:批次经 `STOPPING → STOPPED`,已评判的会话保留结果,轮询器把运行记为 `stopped`,附带这些部分分数 / insight 树以及 `error = "stopped by operator"`。仍在 `queued` 的运行在本地取消(worker 出队时跳过,不调用 AWS),立即返回 `stopped`。正在回放数据集或等待遥测(尚无批次)的运行在提示词之间停止,绝不会调用 `StartBatchEvaluation`。终态运行(`completed` / `failed` / `stopped`)→ 409 `run.not_active`;不存在 → 404 `run.not_found`。刻意不暴露 `DeleteBatchEvaluation`——账本保留的部分结果 AWS 会丢弃 |
| `POST` | `/api/eval/runs/{run_id}/recheck` | `eval.run`——**重新从 AWS 读取失败运行的批次** → 202 返回该运行。仅适用于已启动批次（`batch_eval_id` 非空）的 `failed` 运行：调用一次 `GetBatchEvaluation`；批次已终态时按轮询器的方式落定该行（例如带分数的 `completed`，或带批次自身原因的 `failed`），仍在运行时该行回到 `evaluating` 并重新开始轮询。只读——不会重新运行，也不产生费用。其他运行 → 409 `run.not_recheckable`；不存在 → 404 `run.not_found`。每次批次等待都受 `eval_batch_wait_s` 限制（默认 7200 秒，每 30 秒轮询一次）；超时的运行以「batch evaluation still <STATUS> after the N-minute wait」失败，可在 V2 任务详情中重新检查 |
| `POST` | `/api/eval/runs/{run_id}/rerun` | `eval.run`——**在相同范围上再次评估该运行的对象** → 201 返回新运行，与 `POST /api/eval/runs` 的启动方式完全相同：相同的校验与可见性拒绝（包括源运行存储的评估器列表为空时的 422 `run.no_evaluators`）；调用者即新运行的创建者（CN：没有按人预算门，预算未移植）。请求体 `{name?}`（1–64；默认 `<原名称> · 再评`）。评估对象（Agent 或 `log_source`）、评估器和描述保持不变，数据集也相同（云端运行固定的 `dataset_version` 仍然固定，草稿仍是草稿——用例会重新读取），或使用相同的时间窗口长度（`window:<N>h` → `lookback_hours: N`）。模拟用户数据集使用平台当前的用户模拟模型（`eval_actor_model_id`）。选定会话或日志流运行（只会重复给同一批会话打分）以及 insights 运行 → 422 `run.rerun_unsupported`；不存在 → 404 `run.not_found`**拒绝优先级（N17-obs17）：**与 `POST /api/eval/runs` 相同——先是源 Agent 的状态门禁（`400 agent.not_active`），再是存储请求体的校验（`422 run.no_evaluators`），最后是数据集查找（数据集已删除时 `404 dataset.not_found`）。 |
| `DELETE` | `/api/eval/runs/{run_id}` | `eval.run`——**移除没有产出结果的运行**（`failed` / `stopped`）的账本行 → `{deleted, run_id, status, aws_batch_left_in_place}`。只删账本行：已到达 AWS 的批量评估保持不动（AWS 仍是事实来源，该行只是控制台的指针，响应里会指明）。`completed` 运行是评估历史，活跃运行须先停止 → 409 `run.not_deletable`；不存在 → 404 `run.not_found`。评估页运行表和助手 NEXT STEPS 历史在这类行上显示 ✕ |
| `GET` | `/api/eval/recommendation-providers` | **控制台可提供的推荐生成器** → `{providers: [{id, label, requires_source, supports, models: [{model_id, label, available}], default_model_id, enabled}]}`（默认 `gepa_lite`；`harness_optimizer` 仅在工作区开启后 `enabled`）。不调用 AWS。配置实验退役（N13-obs2）之前位于 `GET /api/experiments/providers` |
| `GET` | `/api/eval/runs/{run_id}/recommendation-inputs` | **基于该运行的优化建议将改写的内容** → `{source, system_prompt, tools: [{name, description, origin}], notes, evaluators, default_evaluator, eligible, reason_code}`。`source: "harness"` = 实时读取 Managed Harness（`GetHarness`：系统提示词、每个 `inline_function` 的描述，以及每个 `agentcore_gateway` 的 target 工具 schema，命名为 `<target>___<tool>`，均按 Harness `allowedTools` 过滤）；`"spec"` = Launchpad Agent 配置（提示词 + 可发现的工具）；`"manual"` = 无可读内容（无提示词的 BYOC、CloudWatch 来源的运行、已删除的 Agent），控制台要求用户填写两项输入。`notes` 列出无法读取的内容（`remote_mcp_runtime_only`、`gateway_unreadable`，以及运行比其 Harness 存在更久或 GetHarness 被拒绝时的 `harness_unreadable`——此时输入回退到配置 / 手动输入，而不是报错）。`evaluators` = 所有可作为优化目标的评估器，形如 `[{id, name, level, group: run|builtin|third_party|custom, recommended}]`：该运行自身的评估器在前，其后是开发者指南推荐的两个目标（`Builtin.GoalSuccessRate` / `Builtin.Helpfulness`，`recommended: true`），再是 AWS 内置、第三方托管评估器和本账号 ACTIVE 的自定义评估器（每个自定义评审都用 `GetEvaluator` 读取配置）。`excluded_evaluators` = `[{id, reason}]`，列出不能作为目标的评估器：`lower_is_better`（惩罚型分数）、`ground_truth`（轨迹匹配器、读取 `{expected_response}` 等占位符的评审、读取参考答案的托管代码评估器）、`categorical`（非数值评分）、`unavailable`（该运行用过但已不存在）。`POST` 使用相同规则（422 `recommendation.evaluator_<reason>`）。没有会话 id 的运行（时间窗口运行）`tools_eligible` 为 false |
| `GET` | `/api/eval/runs/{run_id}/recommendations` | 从该运行启动的优化建议，最新在前 → `{recommendations: [{id, kind, recommendation_id, status, input_source, system_prompt, evaluator, tools, skipped_tools, result, error, …}]}`。每次读取时对未终态的行调用一次 `GetRecommendation` 刷新（无后台轮询）；结束但没有产出文本的任务显示为 `FAILED` 并附 AWS 自身的错误。工具任务若因部分工具未出现在 trace 中被拒绝，会剔除这些工具后**重试一次**（`skipped_tools`） |
| `POST` | `/api/eval/runs/{run_id}/recommendations` | `eval.run`——**启动优化建议** `{kinds: ["system_prompt" \| "tool_descriptions"], input_source, system_prompt?, evaluator?, tools?: [{name, description}]}` → 201 `{recommendations}`。每种类型一次 `StartRecommendation`，范围均为该运行的会话：系统提示词任务将 `agentTraces.batchEvaluation` 固定为该运行的批次；工具描述任务不接受该来源（实测 `ValidationException`），因此内联传入相同会话的 span（`sessionSpans`，用按需评估的查询读取，最多 20 000 个 span），名称从未出现在其中的工具会预先剔除（`skipped_tools`）。仅限带批次的 `completed` 运行，否则 409 `recommendation.run_not_completed` / `run_no_batch`；无会话 id 的运行上的工具任务 → 409 `run_no_sessions`，遥测已过期 → 409 `no_spans`，没有被调用过的工具 → 422 `tools_not_traced`；提示词为空 / 无工具 / 工具缺描述 → 422。所有拒绝都发生在第一次 Start 之前，不会出现一种类型已启动、另一种未启动的情况；分类评分的自定义评审 → 422 `recommendation.evaluator_categorical`。结果供查看和复制；只有下面的接受路由会应用建议 |
| `POST` | `/api/eval/runs/{run_id}/recommendations/{rec_id}/accept` | `agents.deploy`——**接受**一条 `COMPLETED` 的系统提示词建议，发布为新的 Harness 版本 → 202 `{agent, job_id, deployment_id, recommendation}`。与 `POST /api/agents/{id}/redeploy` 使用相同的校验和 update 模式部署任务，重新发布该运行所属的平台 Harness（UpdateHarness → 新的不可变版本，`DEFAULT` 随之切换）。建议改写的是*线上*提示词，其中已含平台追加的 `## Knowledge bases` 段落，因此会先剔除该段。行上记录 `accepted {by, at, agent_id, previous_version, job_id, deployment_id}`（列表路由随之返回）；只能接受一次 → 409 `recommendation.already_accepted`；工具描述建议 → 400 `recommendation.accept_kind`；未完成或无文本 → 409 `recommendation.not_completed`；非 Harness Agent → 400 `recommendation.accept_not_harness` |
| `POST` | `/api/eval/runs/{run_id}/recommendations/{rec_id}/revise` | `agents.deploy`——**发布对已接受建议的手动修改**（N17-obs464），请求体 `{system_prompt, expected_version}` → 202 `{agent, job_id, deployment_id, recommendation}`：经与接受相同的路径（相同的适用条件、创建者或管理员、预置 / 已转换的拒绝；快照备注 `recommendation edited`）在线上版本之上发布新的 Harness 版本，记录到 `accepted.edits[] {by, at, version_before, job_id, deployment_id, system_prompt}`。按版本锁定：Agent 必须仍为 `active` 且处于 `expected_version`——先校验，再在发布事务中以比较并设置占用，因此并发发布会得到 409 `recommendation.version_moved {expected_version, current_version}`；尚未接受 → 409 `recommendation.not_accepted`（由接受承载修改）；为空 → 422 `recommendation.accept_prompt_empty`。行数据新增 `edits` 与 `final_prompt`（修改的起点文本） |
| `GET` | `/api/eval/queue` | `{running, queued, locked, max_concurrency}`——取消的运行立即离开队列,计数只覆盖活跃运行 |

## 控制台在线评估 API / Console Online Evaluation API

`/api/eval/online/*` 管理 AgentCore **在线评估配置**:按采样比例持续给真实会话打分。AWS 是唯一事实来源,
ledger 只存标识。列表返回 workspace 账号内全部配置并按 `owner` 归类:`agent`(本控制台为 agent 创建)、
`experiment`(`can_*` 金丝雀 arm，以及已退役配置实验遗留的 `exp_*` arm——N13-obs2，只读)、`external`(其他来源)。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/eval/online` | `{configs, total}`:全部配置,含 `owner`、双状态、`failure_reason`、evaluators、采样率、超时、`matched_agent`、`duplicate_enabled`、`results_log_group` |
| `POST` | `/api/eval/online` | 为活跃 agent 创建:`{agent_id, mode: scores\|insights(默认 scores), evaluators[1..10](scores 模式), insights[1..3] ⊆ Builtin.Insight.FailureAnalysis\|UserIntent\|ExecutionSummary + clustering_frequencies[0..3] ⊆ DAILY\|WEEKLY\|MONTHLY(insights 模式), sampling_percentage 0.01–100(省略 → scores 10 / insights 100), session_timeout_minutes 1–1440(15), filters[0..5], description?, enable_on_create(true)}` → 201 行(`status` 从 `CREATING` 开始)。混用两类 → 422 `online_eval.mode_conflict`;行带 `mode`(由 `insights` 非空推导) |
| `GET` | `/api/eval/online/{config_id}` | 完整详情(`filters`、`data_source`、`execution_role_arn`) |
| `PATCH` | `/api/eval/online/{config_id}` | 仅 `owner=agent`:`description, sampling_percentage, session_timeout_minutes, filters` 之任意,加上本模式自己的分析字段——`evaluators`(scores)或 `insights` / `clustering_frequencies`(insights;完整列表,`[]` 频率即清除聚类);另一类 → 422 `online_eval.mode_conflict`,模式不可变。请求体是**封闭的**（N13-obs21）：任何其他键——受保护的 `agent_id` / `mode` / `config_id` 或拼写错误——都是 422 `validation.invalid_request`（`detail[].type: extra_forbidden`，指明该键），且行不变；不会静默忽略。后端总是重发完整 `rule`(AWS 整体替换) |
| `POST` | `/api/eval/online/{config_id}/pause` · `/resume` | 切换 `executionStatus`(`agent` 与 `external`) |
| `DELETE` | `/api/eval/online/{config_id}` | 删除 AWS 配置并删掉 ledger 行(`agent` 与 `external`);结果日志组保留并在响应里给出 |
| `GET` | `/api/eval/online/{config_id}/results?range=1h\|6h\|24h\|7d` | Logs Insights 聚合结果日志组:每个 evaluator 的均值 / 计数 / 会话数 / 标签分布、按时间分桶的趋势、最近 ≤50 条带 judge 解释的记录、错误计数 |
| `GET` | `/api/eval/online/{config_id}/reports` | insights **报告** = 以该配置为数据源的批量评估:`{config_id, mode, reports[{batch_id, name, status, run_status, created_at, updated_at, insights, sessions{completed, failed, in_progress, total}, origin: aws_scheduled\|console, run_id, error}], aws_unavailable}` 最新在前(ListBatchEvaluations 失败时 `aws_unavailable: true`,仅有控制台行)——账本里的控制台运行(`EvalRun.dataset_name == "online:<config_id>"`)与 AWS 定期批次合并,后者靠 `GetBatchEvaluation.dataSourceConfig.onlineEvaluationConfigSource.onlineEvaluationConfigArn` 归属(只有摘要里没有 evaluators/insights 的批次才是候选;每个一次 Get,按 batch id 缓存)。任何归属均可读 |
| `POST` | `/api/eval/online/{config_id}/reports` | 立即出报告 `{range: 1h\|6h\|24h\|7d(24h)}` → 202 `{run_id, status, queue_position}`:仅 agent 持有的 insights 配置(否则 403 / 422);经有界运行队列提交 `EvalRun(mode=insights, dataset_name="online:<config_id>")`,批次使用 `onlineEvaluationConfigSource`——只覆盖该配置在窗口内**采样过**的会话,并继承配置的 insights(该数据源下显式传 evaluators/insights 会被 AWS 拒绝) |
| `GET` | `/api/eval/online/{config_id}/reports/{batch_id}` | `{batch_id, name, status, created_at, updated_at, time_range, sessions, insights{failures, userIntents, executionSummaries}, error_details}`(`parse_insights` 树,与运行页的 insights 运行相同);批次不是该配置的数据源时 404 `online_eval.report_not_found` |

错误码:`online_eval.no_telemetry`(400,agent 还没有遥测日志组,先跑一次会话)、`online_eval.evaluator_unsupported`(400)、
`online_eval.read_only`(403;实验/金丝雀臂配置只读——其所属金丝雀或实验结束后可暂停/恢复,N17-obs445;实验配置附带 `owner_run`)、`online_eval.not_found`(404)、`online_eval.conflict`(409)、
`online_eval.workspace_not_bootstrapped`(400)、`online_eval.invalid_filter` / `online_eval.bad_range`(422)。
结果最早在会话空闲 `session_timeout_minutes` 之后出现;ENABLED 配置引用的自定义 evaluator 会被 AWS 锁定。

在线评分也出现在查看会话的地方:

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/observability/sessions/{session_id}` | 会话详情附带 `online_scores: {configs[{config_id, config_name, owner, agent{id,name}?, records[{time, evaluator_id, level, score, label, explanation, trace_id}]}], total, unavailable, configs_exist}`——该会话在所有配置下的结果记录(agent 持有的块排在最前),用一条前缀 `SOURCE logGroups(namePrefix: ['/aws/bedrock-agentcore/evaluations/results/'])` 查询读取。失败降级:结果查询失败只置 `unavailable: true`,绝不影响追踪与对话记录;`configs_exist` 表示 workspace 是否有 agent 持有的配置(结果与配置都没有时 UI 隐藏该区块) |
| `GET` | `/api/observability/sessions/{session_id}/transcript` | 只取对话内容——返回 `{session_id, transcript}`，`transcript` 结构与会话详情相同，**不**跑 Logs Insights 查询（V2 评估结果抽屉用它）。可选 `agent_id` 用于归属没有账本记录的会话（其他 workspace 的 id 会被忽略）；再加上可选的 `since`（其首个 span 的 ISO 时间），Memory 中没有内容的会话可从该 Agent 的 runtime 内容日志重建对话——会话详情也以自身的首个 span 时间这样回退。没有 Memory 事件的评估会话从 runtime 的 otel-rt-logs 内容记录重建对话；Harness 读其 backing runtime 的日志组（`harness_<name>-<id>-DEFAULT`），关闭记忆的 Harness 只有这一份记录。评估运行回放出的会话（其 `scenario_map` 的键）两个参数都不需要：由该运行提供 Agent 与开始时间，`transcript` 带 `source: "eval"` 与 `run_id`（N17-obs432）；看不到该运行的成员得到 `404 observability.session_not_found` |
| `GET` | `/api/observability/traces/{trace_id}?range=` | 链路瀑布图 `{trace_id, range, meta{root_operation, service, agent, session_id, session_ids[], start, duration_ms, span_count, llm_count, status: ok\|error, tokens{input,output,cache_read,cache_write,total}, malformed{rows, attrs, status, resource}}, tree[], spans[]}`。**`meta.malformed`（追加字段，N16-obs53 / rev-r16）：**Logs Insights 结果中不是 OTEL span 对象的行绝不会导致 500——非 JSON 或非对象的 `@message` 行被跳过（`rows`），`attributes` / `status` / `resource` 不是对象的 span 以空值渲染（`attrs` / `status` / `resource`）——每次跳过或降级都在此计数，并在后端日志中按 trace 打印一条 `WARNING`，只列形状、绝不含行内容。干净的导出四项均为 `0`；非零意味着瀑布图显示的 span 比该 trace 实际拥有的更少或更简。 |
| `POST` | `/api/observability/session-previews` | 只读（用 POST 只是为了携带每个会话的开始时间）。请求体 `{sessions: [{session_id, first?}]（1–20 个）, agent_id?}` → `{previews: [{session_id, available, reason, input, output, user_turns}]}`——每个会话的首条用户输入、最后一条 Agent 回复（各截断至 600 字符）与用户轮次数。数据来源与 `/transcript` 相同，并行读取，但不统计长期记忆记录（V2 创建评估任务里「Agent 轨迹」选择器用它，每次读一页）。`agent_id` 用于归属没有账本记录的会话；`first`（会话列表中的首个 span 时间）让这类会话可回退到该 Agent 的 runtime 内容日志。调用方无权查看或读取失败的会话仍保留该行，`available: false` 并给出 `reason`（`not_found` · `read_failed` · `not_platform_session` · `memory_unavailable` · `no_turns`） |
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

## 控制台 Skill Lab API——变更的所有权（r13-g）/ Console Skill Lab API: ownership of mutations

Skill Lab 的全部路由都是 `MEMBER`；其中的变更操作**仅创建者或管理员**可执行（403
`skill_lab.forbidden` `{job_id | taskset_id, action, created_by, created_by_name}`；没有创建者记录的行——
种子样例、历史行——归管理员）：`POST …/jobs/{id}/cancel`、`…/resume`、`…/publish`、`DELETE …/jobs/{id}`
（作业的创建者）；`PUT`、`DELETE …/tasksets/{id}` 与 `POST …/jobs/{id}/apply-expansion`（**目标任务集**的创建者——
写入落在那里）。`POST …/jobs/{id}/import-taskset` 对能看到该作业的每位成员开放（它创建一个归导入者所有的新任务集）。
该门禁在可见性 404 之后、任何状态检查（`skill_lab.job_not_cancellable`、`skill_lab.taskset_in_use`、
`skill_lab.sample_readonly`……）之前执行。契约：`docs/cn/interfaces/users-and-ownership.md` §8。

## 控制台 Skill Lab API——评估结果 / Console Skill Lab API

Skill Lab 评估详情页（`/skill-lab?view=eval&job=<id>`）从 CLI 的 `out/results.json` 读取一个
作业的逐任务评审行；权威来源是这个文件而不是台账，每次请求都会重新读取。路由本身不变，
其响应新增了一层经过校验的 token 用量投影。同一张表也收录了保存审阅后 taskgen 结果的两条路由。

| 方法 | 路径 | 结果 |
|---|---|---|
| `POST` | `/api/skill-lab/jobs`、`/api/skill-lab/jobs/{job_id}/resume` | 提交 `eval` / `train` / `taskgen` 作业（resume：被中断或失败的 train 作业）→ 201 作业行，进入队列。**在创建任何行之前**先探测 hub：exec worker 与制品桶（503 `skill_lab.not_provisioned`）、assumed-role 工作区（400 `skill_lab.workspace_unsupported`），以及宿主侧 CLI 与 judge 所需的 Global Bedrock bearer——缺少 `AWS_BEARER_TOKEN_BEDROCK` 的 hub 在提交时即答复 503 `skill_lab.model_credential_missing`（N13-obs20；此前为 201 后 `failed "failed to start: the hub cannot reach Global Bedrock: …"`）。|
| `GET` | `/api/skill-lab/jobs/{job_id}/results` | eval 作业返回 `{summary, rows[]}`（taskgen 作业则返回 `{type: "taskgen", count, tasks, summary}`）。`summary` = `{tasks, passed, invalid, pass_rate, soft_mean, duration_s, judge_prerequisite_missing[], token_usage}`；每行 = `{id, task_type, hard, soft, score_valid, duration_s, judge_status, judge_reason, judge_error, error, judge_prerequisite, response（摘录）, artifacts[{path,size}], usage, judge_usage, token_usage}`。不检查作业状态：CLI 一写出文件（在进程退出之前）就会返回；此前返回 `404 skill_lab.results_pending`（对在评分阶段之前就结束的作业，这也是最终答复）。 |
| `POST` | `/api/skill-lab/jobs/{job_id}/import-taskset` | 把一个**已成功的 taskgen** 作业生成的任务保存为新的 single 模式任务集。请求体 `{name, tasks?}`；`tasks` 是审阅后的选择——`[{index, id?, question?, rubric?, task_type?}]`（最多 `MAX_TASKS_PER_SPLIT` 项），`index` 是该行在作业 `generated_tasks.json` 中的位置（严格整数，不接受 bool/字符串/浮点强转），四个可选字段是唯一接受的作者编辑；未知键（如 `files`、`attachments`）返回 `422 validation.invalid_request`。未出现在 `tasks` 中的行被排除，省略的字段保留生成值，`task_type: ""` 清除该字段；服务端从作业快照重建 `files`/附件。省略 `tasks` 或传 `null` → 原样保存全部生成行（旧行为）。`201 {job, taskset}`。所有错误都发生在任何写入之前：`400 skill_lab.not_a_taskgen_job`、`409 skill_lab.job_not_finished` / `skill_lab.already_imported` / `skill_lab.results_missing`、`422 skill_lab.taskgen_empty_selection`（`tasks` 为空）、`422 skill_lab.taskgen_bad_selection`（索引越界或重复）、`422 skill_lab.taskgen_duplicate_id`（编辑后 id 不唯一）、`422 skill_lab.taskset_invalid`（校验器子进程，例如不安全的 id）、调用方 workspace 之外返回 `404 skill_lab.job_not_found`。 |
| `POST` | `/api/skill-lab/jobs/{job_id}/apply-expansion` | 把一个**已成功的扩展**作业生成的任务追加到其目标任务集/分割。请求体可选：`{tasks?}`，选择的形状与规则同 `import-taskset`；无请求体或无 `tasks` 时追加全部生成行。**编辑后的** id 会对照目标任务集当前的每个分割重新检查（`409 skill_lab.expansion_conflict` 列出冲突的 id），其他分割保持不变，写入为经过校验的全量替换。`200 {job, taskset}`；`400 skill_lab.not_an_expansion_job`，以及与导入相同的 `409`/`422`/`404` 族。两条路由都不会改动作业的 `generated_tasks.json` 与附件快照。 |
| `GET` | `/api/skill-lab/jobs/{job_id}/artifacts?path=` | 作业的 `out/` 目录树，不限状态。目录 → `{kind: "dir", path, dirs[], files[{name, size}]}`；文件 → `{kind: "text", path, size, truncated, content}`（UTF-8，`content` 上限 512 KB，超出部分以 `truncated: true` 标记）或 `{kind: "binary", path, size}`（含 NUL 字节／无法解码）。尚未创建 `out/` 的作业（排队中，或 CLI 尚未写出任何内容的运行中作业）返回**空的根目录列表**而不是错误；不存在或已消失的子路径返回 `404 skill_lab.artifact_not_found`。绝对路径、`~`、反斜杠、NUL，以及（含符号链接）解析到 `out/` 之外的任何路径返回 `400 skill_lab.bad_path`。 |
| `GET` | `/api/skill-lab/jobs/{job_id}/artifacts/raw?path=` | 以下载形式返回文件的精确字节（`Content-Disposition` 带文件名），永不截断；目录或不存在的文件返回 `404 skill_lab.artifact_not_found`，同样受 `400 skill_lab.bad_path` 守卫。两条路由对调用方 workspace 之外的作业均返回 `404 skill_lab.job_not_found`。 |
| `POST` | `/api/skill-lab/jobs/{job_id}/publish` | 请求体 `{reapprove?: bool}` → `200 {record_id, name, new_version, status_before, status_after, reapproved, pending_approval, unchanged}`——把一个**已成功的 train** 作业的 `best_skill.md` 以递增次版本号写回其来源 Registry 记录（`update_record`；记录回到 DRAFT；`reapprove` 让此前 APPROVED 的记录经生命周期重新审批——仅限管理员；成员的 `reapprove` 只会重新提交，`status_after: PENDING_APPROVAL` 且 `pending_approval: true`，N17-obs248）。**按内容幂等（N16-obs44）：**当记录的最新版本已经就是这份 best_skill.md（其 SKILL.md 描述符一致，或本作业上次发布推送了它且记录仍停在该版本）时不写入任何内容，返回现有 `new_version` 且 `unchanged: true`、`status_after == status_before`。作业须为创建者或管理员（`403 skill_lab.forbidden`），**来源记录也须为创建者或管理员**（`403 registry.forbidden {created_by, created_by_name, record_id, action: "publish"}`——成员可以在任何可读记录上训练，但只能发布到自己创建的记录；平台示例技能等 `null` 创建者记录归管理员；在任何拆分或写入之前检查，记录的文件与状态保持不变——N17-obs263）；`400 skill_lab.publish_unsupported`（非已成功的 train 作业 / 上传来源）、`400 skill_lab.publish_no_change`（训练未接受任何修改）、`404 skill_lab.results_pending` |

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

## 控制台 Workspaces API——成员可见性 / Console Workspaces API: member visibility

决定成员在一个 workspace 里创建的资源对谁可见（Family B 第 2 步；上游 #262 / #263）。中枢全局、
仅管理员；目标是路径参数，与 grants 相同。

| 方法 | 路径 | 权限 | 结果 |
|---|---|---|---|
| `GET` | `/api/workspaces/{workspace_id}/settings/visibility` | 管理员 | `{resource_visibility: "shared" \| "isolated"}`——缺失或未知的存储值按 `shared` 读取。未知 id 返回 `404 workspace.not_found`。`GET /api/workspaces` 的每一行同样带 `resource_visibility` |
| `PUT` | `/api/workspaces/{workspace_id}/settings/visibility` | 管理员 | 请求体 `{resource_visibility: "shared" \| "isolated"}` → 存储后的值；其他值返回 `422`。不改动任何资源，自每位成员的下一次请求起生效（workspace 行每次请求重新读取）。对尚未 bootstrap 的 workspace 同样可用 |

**`isolated` 改变了什么。** 成员只有在资源的 `created_by` 是自己的 `users.id`、为 `null` 或指向一位
*当前*管理员时才能看到它；管理员看到全部。被隐藏的资源返回该种类**既有**的 not-found——
`dataset.not_found`、`run.not_found`、`pipeline.not_found`、`online_eval.not_found`、
`evaluator.not_found`、`canary.not_found`、`skill_lab.job_not_found` /
`skill_lab.taskset_not_found`、`kb.not_found`、`memory.not_managed`、`agent.not_found`（所有
`/api/agents/{id}*` 与 `/api/chat/{id}*` 路由、`/v1`）、`job.not_found`、`registry.record_not_found`
（被隐藏 Agent 的 A2A 记录）、`memory.actor_not_found`——因此被隐藏的 id 与不存在无法区分，且在任何
AWS 调用之前、在 D20 策略门禁之前就返回。列表在 SQL 中先过滤再计算 `total` / 分页（`GET /api/eval/runs`、
`/api/runtime-canaries` 过滤后再截断）；创建时的引用会被校验（`POST /api/eval/runs`、
`/api/eval/pipelines`、`/api/eval/datasets/from-sessions`、金丝雀动作：被隐藏的数据集或运行视同
不存在；被隐藏的自定义评估器视同未知——`run.evaluator_not_found`、`online_eval.evaluator_unsupported`、
`canary.evaluator_unsupported`）。Agent：`POST /api/agents` 与
`…/redeploy` 拒绝调用者看不到的知识库 / 记忆绑定（`kb.not_found` / `agent.memory_not_managed`；重新发布
只校验相对已存 spec **新增**的绑定）；持有者被隐藏时 `agent.name_exists` 409 的 `agent_id` 为 `null`；
`GET /api/agents/discovery` 与 `…/discovery/import` 对被隐藏 Agent 管理的资源返回 `managed: true`，
`managed_agent_id` / `agent_id` 与名称为 `null`；知识库摘要 / 详情在 `attached_agents`（仅可见名称）旁
带 `attached_agent_count`（全部挂载 Agent 数）；`memory.in_use` / `kb.has_attached_agents` /
`identity.connection_referenced` 的 409 保留真实数量但只列出可见 Agent。`/v1`：密钥跟随其**创建者当前**
的可见性——被隐藏的 Agent 调用时返回 `404 agent.not_found`、不出现在 `GET /v1/agents`；管理员、配置内置
管理员与旧（创建者为 NULL）密钥不受限制。可归属于被隐藏 Agent 的可观测行（trace、会话、Chat 链路追踪栏
`GET /api/traces/{session_id}`）被丢弃，其详情返回 `404`；Memory 控制台中 `<被隐藏 Agent>__<human>` 的
actor 及携带它们的会话 / 命名空间被丢弃或返回 `404`。概览：`eval_runs` / `eval_pass_rate`、
`active_sessions` 与 `registry_assets.agents` 按 viewer 计算；`GET /api/overview/online-quality` 按
viewer 缓存。内部路径（部署、恢复、worker、purge、预置）永不过滤。CN：第 1 步的成员规则（自己的 API 密钥、
自己的聊天会话）在两种模式下都成立；D20 策略保持共享。**按 id 访问聊天会话。** 聊天会话在两种模式下都是个人的（N16-obs70 / N16-obs71）：
`GET …/history`、`GET …/memory?session_id=`、`POST …/sessions/{sid}/stop`、重新接入流以及携带已有
`session_id` 的 `POST /api/chat/{agent}`，在 `shared` 与 `isolated` 下一律对其他成员的会话返回
`404 chat.session_not_found`（本人会话，或管理员的任何会话——同一解析器 `routers/chat._session_for`）。
可观测的按 id 界面（`GET /api/traces/{sid}`、可观测会话 / 链路详情）在 `shared` 下保持开放；`isolated` 下
同一“自己的会话”谓词约束它们以及可观测的 `traces` / `sessions` 列表：其他成员的会话返回
`observability.session_not_found` / `observability.trace_not_found`，其行被丢弃。没有账本行的会话保持可见。
`GET /api/policies/entities` 对成员保持可读，继续列出密钥 id / 名称 / 创建者（编写策略需要它们；id 不是秘密）。

## 控制台模型提供方 API——属主绑定的提供方（N17-obs203，r17-mp）/ Console Model Providers API

用户决定（2026-10-21）：模型提供方是属主绑定的资源。任一已登录成员都可以登记自己的提供方
（`scope: personal`——仅本人与管理员可见），读取、修改、删除它、添加模型、探测并登记密钥；管理员
可以做一切，可把提供方共享给工作区（`scope: workspace`），也可以修改或删除成员的行。中枢全局
（`/api/settings` 前缀）。契约：`docs/cn/interfaces/model-registry.md` §4 / §4.1；所有权规则
`docs/cn/interfaces/users-and-ownership.md` §10。

| 方法 | 路径 | 权限 | 结果 |
|---|---|---|---|
| `GET` | `/api/settings/model-providers` | 成员 | `{catalog_version, providers[], models[], workspaces[], warnings[]}`，按调用者可见范围收窄：工作区范围的提供方 + 调用者自己的个人提供方（以及仅这些提供方的模型 / 探测告警）；成员的 `workspaces` 为 `[]`。每个提供方带 `created_by`（users.id）、`created_by_name`、`scope`、`owned` |
| `POST` | `/api/settings/model-providers` | 成员 | 请求体 `ProviderCreate` + `scope?` + `api_key?`（只写——先用该密钥探测端点，`422 model_provider.probe_failed` 时不保存任何内容；随后由控制台主机写入该行的 SSM SecureString 并登记到 Identity；响应只带密钥末四位（`ProviderOut.key_last4`——N17-obs283：`ProviderOut` 只保留一种扁平的凭据字段形状，嵌套的 `credential` 镜像已移除）；`502 model_provider.ssm_write_failed` 指明 IAM 语句；Bedrock 行 → `422 model_provider.api_key_unsupported`）。成员创建的行为 `personal`（省略或显式；`workspace` → `403 model_provider.forbidden {scope, action: scope}`）；管理员默认 `workspace`。个人提供方的 SSM 参数 / Identity 提供方由所有者派生（`launchpad-user-<uid>-<name>-api-key` / `launchpad-user-<uid>-<name>`，无主机环境变量回退）——传入其他 `credential_ssm_parameter` 为 `422 model_provider.invalid_ssm_parameter {expected}` |
| `GET` | `/api/settings/model-providers/{provider_id}` | 成员 | 该行；不存在或属于其他成员的个人提供方时均为 `404 model_provider.not_found`（不可区分） |
| `PUT` / `DELETE` | `/api/settings/model-providers/{provider_id}` | 成员（创建者或管理员） | `DELETE`：仅中枢 Global Bedrock 行返回 `409 model_provider.builtin`（遗留的 `siliconflow` 预置行可删——N17-obs207）；当未删除的 Agent 规格（含系统预设）、平台角色覆盖或评估器仍引用其模型时为 `409 model_provider.in_use {referrers, agents[{id, name, workspace_id, system_key}], roles[{role, field, model_ids}]}`。可见性 404 之后：非创建者且非管理员为 `403 model_provider.forbidden {provider_id, action, created_by, created_by_name}`，在请求体校验之前返回；创建者为 NULL 的行（内置 / 历史行）属于管理员。`PUT` 的 `scope` 仅管理员可用；**共享（`personal → workspace`）会把该行转交给工作区——`created_by` 变为 NULL，原创建者失去全部修改权（N17-obs225）**；工作区范围行上的 `base_url`、`credential_ssm_parameter` 与 `api_key` 同样仅管理员可改——非管理员创建者（历史重定向行）更改其中任一项为 `403 model_provider.forbidden {action: base_url \| credential_ssm_parameter \| api_key, scope: workspace}`（原样回传同一值不算更改）；个人行的 `credential_ssm_parameter` 不可更改（422）。**N17-obs228：**持有已登记密钥的行更改 `base_url` 时需在同一请求体中带上 `api_key`，否则已保存的密钥会被清除（控制台写入的 SSM 参数、Identity 提供方、中枢记录、缓存——尽力而为），该行在再次输入密钥前为 `configured: false`；日志备注记录新旧主机 |
| `POST` | `…/{provider_id}/credential`、`…/probe`、`…/models`；`…/models/{model_ref}` 上的 `PUT` / `DELETE` / `POST …/probe` | 成员（对该提供方为创建者或管理员） | 同样两道门；模型的 `is_default` 仍仅管理员可改（`403 … action: is_default`）；工作区范围行上的 `POST …/credential`（有无请求体均然）仅管理员可用（`403 … action: credential`，N17-obs225——无请求体的调用会把运维的参数登记到该行的 Identity 提供方，带密钥的调用会覆盖该参数）。`POST …/credential {api_key}`（或 `PUT /{id} {api_key}`）= 「替换密钥」：探测 → 写入 SSM → Identity 轮换，返回 `CredentialOut.last4`；空请求体保持 D21 的读 SSM 登记。`DELETE /{id}` 尽力删除由控制台写入的参数（残留写入日志备注），同时丢弃中枢为该名称缓存的密钥，并把中枢 / 分支工作区的 `model_credentials[<name>]` 记录置为 `configured: false`——同名的后继提供方从未配置状态开始（N17-obs226 / obs229） |
| `GET` | `/api/settings/model-providers/changes?target_id=` | 管理员 | 变更日志；`target_id` 收窄为某一提供方的审计轨迹（N17-obs203 §4：每次创建 / 修改 / 范围切换 / 录入密钥 / 删除各一行——控制台用户、提供方、动作、时间，`before` / `after` 中含主机与范围，密钥最多记末四位） |
| `PUT` | `/api/settings/model-providers/workspaces/{workspace_id}/models` | 管理员 | 按工作区的启用开关（不变） |

**用户可以使用什么（N17-obs203 §6）。** `GET /api/capabilities` 对调用者列出工作区范围提供方的模型
加上调用者自己个人提供方的模型（管理员：全部）——所有选择器都读它。凡是点名其他成员个人提供方的模型，
都在点名处被拒：`POST /api/agents` / `POST /api/agents/{id}/redeploy` → `422 agent.model_not_visible
{model_id}`（其所有者或管理员仍可部署）；评估运行的 `actor_model_id` → `422 run.model_not_visible`；
平台角色 → `422 models.model_not_allowed`，reason 为 `personal_scope`；评审器 / 派生评估器的 `model_id`
（`POST` / `PUT /api/eval/evaluators`）、助手评估计划审批时的评审器模型、推荐任务的 `model_id`
（`POST /api/eval/runs/{id}/recommendations`）→ `422 models.model_not_visible {model_id}`
（N17-obs227——否则中枢会替调用者花掉其他成员的密钥）。

`base_url` 不再需要运维允许列表（N17-obs203 §2）：任何公网 https 主机都可通过；控制台主机在每次请求前
解析主机名，拒绝回环 / 私网 / 链路本地 / IMDS 地址与内部域名（`422 model_provider.invalid_base_url`）。
`model_provider_allowed_hosts`（非空 = 锁定模式）与 `model_provider_denied_hosts` 是运维侧 yaml / 环境变量
设置；被拒时为 `422 model_provider.host_not_allowed {host, setting, mode: lock | deny}`。

## 控制台模型角色 API——平台各项工作所用模型（r15-e）/ Console Model Roles API

平台自身各项工作所使用或预选的目录模型（上游 #275 子集：仅角色——不含价格表、Claude SDK、Studio 与
codex 角色）。中枢全局；页签位于 Settings → Model Providers（`?view=roles`）。契约：
`docs/cn/interfaces/model-registry.md` §12。

| 方法 | 路径 | 权限 | 结果 |
|---|---|---|---|
| `GET` | `/api/model-defaults` | 成员 | `{roles: [{role, field, model_id, default, overridden_by, updated_at, needs_tools, allow_empty, default_disabled, kind, feature}]}`——角色为 `agent_default`、`judge`、`judge_fallback`、`actor`、`prompt_opt`、`skill_lab_judge`、`skill_lab_target`、`memory_extract`、`codegen`（均为 Settings 字段，`kind: chat`）与 `embedding`（N17-obs252：注册表当前启用的向量模型行，`field: ""`、`kind: embedding`；`feature ∈ skill_lab | studio | null` 标明该角色随哪个控制台功能一起隐藏）；成员读取同样的行，但被覆盖时 `overridden_by` 脱敏为 `""`（N17-obs251——模型角色页对成员只读；写操作仍为管理员）；`model_id` 为管理员覆盖值，否则为环境变量 / `launchpad.yaml` / 内置默认（`default`）；未覆盖时 `overridden_by` 为 null。V2 各选择器从此处预选 |
| `PUT` | `/api/settings/model-roles/{role}` | 管理员 | 请求体 `{model_id}` → 该角色行（`embedding`：走注册表受保护的默认切换——任一向量索引含文档时 `409 embedding.reindex_required`，N17-obs252）。`404 models.unknown_role`；`422 models.model_not_allowed {role, model_id, reason}`（`empty \| not_in_catalog \| not_chat \| not_embedding \| disabled \| personal_scope \| no_tool_use`——角色只接受注册表中已启用的对话模型，运行工具的角色还需支持工具调用；仅 `judge_fallback` 可为 `""`）。写入 `platform_settings`，立即生效（无需重启），记入 `model_catalog_changes` 的 `model_role.set`（仅审计：目录版本不变） |
| `DELETE` | `/api/settings/model-roles/{role}` | 管理员 | → 恢复默认后的该角色行；记入 `model_role.reset`（无覆盖时为空操作，不写记录） |

被某个角色指向的模型（或提供方）既不能删除，也**不能停用**——返回 `409 model.in_use` /
`model_provider.in_use`，附带设置类引用方（`{kind: setting, id: <field>, role, model_ids}`，
`suggest: "repoint"`），直到角色被重新指向。探测角色所用模型即注册表自身的
`POST /api/settings/model-providers/{provider_id}/models/{model_ref}/probe`。

## 控制台账户 API / Console Accounts API

`/api/auth/*` 守住控制台入口，`/api/users/*` 管理其背后的账户。两组接口都不触碰 AWS。详见
[architecture.zh-CN.md](architecture.zh-CN.md)「控制台认证与账户」一节。

| 方法 | 路径 | 鉴权 | 结果 |
|---|---|---|---|
| `GET` | `/api/auth/status` | 开放 | `{auth_required, authenticated, registration_enabled, registration_requires_approval, password_policy, username, role, email, account_expires_at, permissions}`——身份字段在认证前为 null（`permissions` 为 `[]`）；`password_policy: {mode: strict \| relaxed, min_length}` 为注册表单据以提示的当前密码策略（新增字段，N14-obs3；登录响应同样携带） |
| `POST` | `/api/auth/login` | 开放 | 设置 `launchpad_session` cookie（`HttpOnly`、`SameSite=Lax`、生产环境 `Secure`；绝对有效期 `LAUNCHPAD_AUTH_SESSION_TTL_SECONDS`，默认 12 小时，且不超过账户有效期，活动不续期）并回显身份。受按账户锁定与按 IP 窗口保护（见下文"登录保护"） |
| `POST` | `/api/auth/register` | 开放 | `201`——创建一个 `member` 账户；默认 `status=pending` 且 `expires_at=null`，直到管理员批准，之后有效期为 `auth_registration_valid_days`（默认 7 天）。针对*失败*尝试的按 IP 窗口（`LAUNCHPAD_AUTH_REGISTER_IP_LIMIT` 10 / `…_WINDOW_SECONDS` 3600 → 之后的失败 `429 auth.rate_limited`；有效注册从不被它拒绝）、待审批上限（`LAUNCHPAD_AUTH_REGISTRATION_PENDING_MAX` 50 → `409 auth.registration_backlog`），且不泄露账户存在性：审批模式下已占用的用户名/邮箱（含保留的 admin 名）**与真实的待审批注册返回完全一致**且不创建任何行；即时生效模式下两种冲突统一返回 `409 auth.registration_rejected` |
| `POST` | `/api/auth/logout` | 会话 | 清除 cookie |
| `GET` | `/api/users?q=&status=all\|pending\|active\|expired\|disabled&limit=&offset=` | 管理员 | 分页账户列表，带派生的 `state` / `days_remaining`，以及实时锁定状态 `failed_logins` / `locked_until`（ISO 或 `null`） |
| `GET` | `/api/users/stats` | 管理员 | 汇总数据，包括 `pending` 审批队列、`expiring_soon`（≤3 天）、7 天内的注册/登录计数、14 天注册序列、邮箱域名排行 |
| `PATCH` | `/api/users/{id}` | 管理员 | 以下任意字段：`status`（`pending`\|`active`\|`disabled`；对待审批账户设为 `active` 即批准并启动其有效期）、`role`、`extend_days`、`expires_at`（`null` = 永不过期）、`password`（`null` = 生成并一次性返回）、`permissions`（`{permission_key: bool}`，`null` = 全部授予）、`workspaces`（整体替换该账户的 workspace 授权，`null` 清空）、`unlock`（`true` 清除登录锁定与失败计数） |
| `DELETE` | `/api/users/{id}` | 管理员 | 删除该账户。删除自己的行 → `409 users.self_delete`；该行是最后一个有效且未过期的管理员 → `409 users.last_admin`（配置文件中的内置 admin 不计入） |

注册错误码：`auth.registration_disabled`（400，认证门未开启或注册已关闭）、
`auth.invalid_username` / `auth.invalid_email` / `auth.email_domain_blocked` / `auth.weak_password`（400）、
`auth.registration_rejected`（409——用户名**或**邮箱已占用，仅即时生效模式；审批模式下冲突返回 `201` 的待审批形态且不创建行，公开表单因此不会泄露哪些名字存在——冲突记录在日志中）、
`auth.registration_backlog`（409——已有 `auth_registration_pending_max` 个待审批账户；`detail {pending, max}`；日志 `WARNING`）、
`auth.rate_limited`（429——同一地址在 `auth_register_ip_window_seconds` 内*失败*尝试达 `auth_register_ip_limit` 次；仅影响之后的失败，带 `Retry-After`）。
`auth.username_taken` / `auth.email_taken`（409）仍是管理员创建账户时服务层报告的精确错误码。

**最后管理员 / 自身保护**（`PATCH` 与 `DELETE /api/users/{id}`，写入前检查）：若变更会使有效且未过期的 `role=admin` 行数归零——降级（`role=member`）、禁用、改回待审批、设置已过去的 `expires_at` 或删除唯一一个，包括管理员对自己这样做——返回 `409 users.last_admin`。配置文件中的内置 admin **不计入**：它是应急凭证，因此至少必须保留一个真实管理员行（先提升另一个账户）。管理员删除自己的行 → `409 users.self_delete`（内置 admin 没有行）。`detail {user_id, username}`。

**密码策略**（注册与管理员 `password` 修改；已存哈希不会被重新校验，因此既有账户在下次改密前不受影响）。`LAUNCHPAD_AUTH_PASSWORD_POLICY` 选择模式：`strict`（默认）为至少 `LAUNCHPAD_AUTH_PASSWORD_MIN_LENGTH` 个字符（默认 12，下限 6），不能等于用户名、邮箱或邮箱 `@` 前部分（不区分大小写），且不在内置常见密码表中；`relaxed` 仅检查最小长度。`auth.weak_password` 携带 `detail: {reason: too_short | matches_identity | common, min_length}`（relaxed 模式只会出现 `too_short`）；`password` 传 `null` 时生成的密码满足该策略。`GET /api/auth/status` 以 `password_policy` 报告当前生效的策略。

登录错误码：`auth.invalid_credentials`（401——未知账户与密码错误使用同一错误码与同一文案，并消耗同样的哈希计算），以及在提交的凭据本身正确之后的
`auth.account_pending` / `auth.account_disabled` / `auth.account_expired`（401）。

**登录保护**（`POST /api/auth/login`；进程内状态，后端重启即清空）：`auth.locked`（429）——提交的账户名（无论是否存在）连续 `LAUNCHPAD_AUTH_LOGIN_LOCKOUT_THRESHOLD`（默认 5）次密码错误后被锁定 `LAUNCHPAD_AUTH_LOGIN_LOCKOUT_BASE_SECONDS`（默认 60）秒，之后每再失败一次翻倍，上限 `LAUNCHPAD_AUTH_LOGIN_LOCKOUT_MAX_SECONDS`（默认 1800）；锁定期间的尝试直接拒绝且不计数；登录成功或管理员 `PATCH /api/users/{id} {"unlock": true}` 即清除。这是主控制。内置 admin（`LAUNCHPAD_AUTH_USERNAME`）**不受**该锁定约束——它是持有强配置密码的应急凭证，不能被互联网上的任何人锁住；它只受对端窗口保护。`auth.rate_limited`（429）——调用方源 IP 在 `LAUNCHPAD_AUTH_LOGIN_IP_WINDOW_SECONDS`（默认 300）内登录失败达 `LAUNCHPAD_AUTH_LOGIN_IP_LIMIT`（默认 20）次；该窗口只在一次*失败*校验之后才被查询，因此它把该对端之后的密码错误变为 429，但从不拒绝正确密码——粗粒度的辅助控制：在 EIP 路径上对端是真实客户端（nginx `X-Forwarded-For`，由 uvicorn `--proxy-headers --forwarded-allow-ips=127.0.0.1` 采信），在 NLB 之后则是所有人共用的 NLB 私有 IP。两种 429 都带 `Retry-After` 头与 `detail: {retry_after}`（秒）。每次锁定与每次窗口触发都会在 `launchpad.auth` 日志（journald）写一行 `WARNING`，记录账户键、计数与对端地址——从不记录密码。

**会话密钥。** cookie 由首次启动时生成并持久化到 `data/session_secret`（0600；`LAUNCHPAD_SESSION_SECRET_FILE`）的随机 32 字节密钥签名，或在设置了 `LAUNCHPAD_SESSION_SECRET` 时使用该值——不再由 admin 密码派生。轮换密钥（新的环境变量值，或删除文件后重启）会一次性注销所有会话，引入该机制的那次部署也会如此。**修改密码仍会结束该账户的其他会话：**cookie 携带会话纪元——`users.session_epoch`（每次 `password` 修改递增），内置 admin 则为配置密码的摘要——纪元过期即 `401 auth.required`。

会话与角色错误：`auth.required`（401——cookie 缺失、被篡改或已过期，也包括账户此后被禁用、过期或删除）、
`auth.forbidden`（403——member 会话访问 `/api/users*`）、`users.not_found`（404）。
