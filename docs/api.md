# Public API (/v1) / 公开 API

Every deployed agent is callable through the platform's `/v1` surface — the
same invoke chain the Chat playground uses. Interactive docs: **`/api/docs`**.

Auth: `X-Api-Key` header. Create a key in the console (Chat → API KEYS) or:

```bash
curl -s -X POST localhost:8000/api/apikeys -H 'Content-Type: application/json' \
  -d '{"name": "integration"}'
# → {"id": "…", "prefix": "lp_live_ab12…", "key": "lp_live_<full-key-shown-once>"}
```

Keys are stored **hashed (sha256)** — the full key is shown exactly once.
密钥仅创建时展示一次,后端只保存哈希。

**Key lifecycle.** A key records the console account that minted it
(`created_by` = its `users.id`, plus `created_by_name`; both `null` on keys minted
before this existed, which the console shows as "unknown") and `last_used_at`
(stamped on a successful `/v1` call, at most once a minute). A key lives only as
long as its creator may hold it: once that account is **deleted, disabled or
expired**, or — for a member — **loses its grant** to the key's workspace, the key
answers `401 auth.invalid_api_key` (the same code as any unusable key; `/v1` never
says why) and is flipped to disabled. Admin disable / delete and grant removal
disable the creator's keys at once; expiry has no write event, so it lands on the
key's next use. Keys are disabled, never deleted — they stay listed in
`GET /api/apikeys` (`created_by`, `created_by_name`, `last_used_at`,
`creator_offboarded`: `deleted` | `disabled` | `expired` | `no_workspace_grant` |
`null`). In a `shared` workspace any member can disable / enable any key; under
`isolated` visibility a member lists and toggles only the keys they minted (others →
`404 apikey.not_found`; admins manage all). `POST /api/apikeys/{id}/enable` on an
offboarded creator's key is refused with
`409 apikey.creator_offboarded` (`detail.reason`). Keys minted by the config-driven
admin or the auth-disabled local operator (no `users` row) and legacy keys are never
offboarded. A key follows its **creator's current** workspace visibility: under
`isolated`, a member-minted key lists and invokes only the agents that member can see
(another agent → `404 agent.not_found`), re-evaluated on every call; keys minted by an
admin, the config admin or before creators were recorded are unrestricted.

**Key limits (optional).** `POST /api/apikeys` and `PATCH /api/apikeys/{id}` take
three optional limits; omitted means unrestricted (the legacy key), and on `PATCH`
an explicit `null` clears a limit:

| Field | Effect on `/v1` |
|---|---|
| `agent_ids` (≤ 50 agents of the key's workspace) | Only these agents are listed and invocable; any other → `404 agent.not_found` |
| `expires_at` (must be in the future) | From that moment the key answers `401 auth.expired_api_key` |
| `rate_per_minute` (1–100000) | Over the limit → `429 auth.rate_limited` with `Retry-After` (per backend process) |

The console creates keys by name only and can enable / disable them; the limits are
set through the API.
密钥可选限制 `agent_ids` / `expires_at` / `rate_per_minute` 通过 API 设置,控制台目前只能按名称新建、启用与停用。

## Sync invoke / 同步调用

```bash
curl -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke \
  -H "X-Api-Key: $LP_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "What is 2+2?", "session_id": null}'
# → {"agent":"…","text":"4","session_id":"…","latency_ms":1234}
```

Optional request field `context` (both invoke routes): this call's values for the
agent's `{{variables}}` — an object of at most 20 string values, keys matching
`^[A-Za-z_][A-Za-z0-9_]{0,63}$`, each value at most 2000 characters (otherwise
`422`). It rides in the runtime payload as `context`; a Strands (`zip_runtime`) agent
fills the prompt variables it declares (`spec.prompt_variables`) from it and ignores
any other key, before falling back to the environment variable of that name and then
the declared default. Other methods ignore it.

```bash
curl -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke \
  -H "X-Api-Key: $LP_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "Weather?", "context": {"city": "Oslo"}}'
# → {"agent":"…","text":"Sunny, 21 °C","structured":{"city":"Oslo","temp":21.0},"session_id":"…",…}
```

An agent with an `output_schema` (Strands method) also answers its parsed object as
`structured` beside `text` (the readable answer); without one the field is absent.
A PII-guardrail agent never returns `structured`: only the text is screened, so the
object is dropped.
A Strands agent with tool hooks (`tool_hooks.approval_tools`) runs a tool on its
approval list only after a person approves it in the console chat. `/v1` cannot
approve: a call whose agent tried such a tool answers `409
agent.tool_approval_requires_console` (`detail.tool`, `detail.agent_id`); the stream
ends with an `error` event of that code.

**Sync invoke has a deadline: use the stream for chat.** A synchronous invoke sends
nothing until the whole answer exists, and a proxy in front of the console cuts such a
request off (CloudFront's default origin read timeout is 60 s). So both sync routes
(`/v1/agents/{id}/invoke` and the console's `/api/agents/{id}/invoke`) wait at most
`sync_invoke_deadline_s` (default 50 s; env `LAUNCHPAD_SYNC_INVOKE_DEADLINE_S`, `0` = no
deadline when nothing in front of the console times out). Past it they answer `504
invoke.timeout` with `detail.session_id`, `detail.deadline_s` and `detail.stream_route`.
The turn is not cancelled: it finishes in that session, so a follow-up on the same
`session_id` continues from it. Use sync invoke for short, machine-to-machine answers;
use `invoke-stream` (console: `POST /api/chat/{id}`) for conversations and anything
that may run long — it sends heartbeats and never hits the proxy timeout.

An `error` event on the `/v1` stream carries only what the sync envelope would: a
platform error keeps its `code` and `message`; an `aws.*` one gets the generic AWS
sentence; an unexpected failure is `agent.stream_failed` with a generic message —
the raw exception text (role ARN, account, operation) stays server-side.
可选请求字段 `context` 为本次调用填入 Agent 的 `{{变量}}`;配置了 `output_schema` 的 Agent 另返回解析后的 `structured` 对象。

## Streaming invoke (SSE) / 流式调用

```bash
curl -N -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke-stream \
  -H "X-Api-Key: $LP_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "Tell me a two-sentence story."}'
# event: meta   → {"session_id": "…", "mode": "stream"}
# event: delta  → {"text": "Once"} … (incremental chunks)
# event: usage  → {"input_tokens": 812, "output_tokens": 64, "total_tokens": 876, "model_calls": 1} (once, when the runtime reports usage)
# event: structured → {"value": {"city": "Oslo", "temp": 21.0}} (output_schema agents, after the text)
# event: done   → {"latency_ms": 2100}
```

The stream takes the same `context` field as the sync call. `structured` follows the
text of an `output_schema` agent's answer once (never on a PII-guardrail agent).

Pass the returned `session_id` on the next call to continue the conversation
(session context + AgentCore Memory ride on it).

The stream may also carry `tool` (`{name, id}`, a tool call started) and, for a
Managed Harness, `policy_denied` — a Gateway tool call a Cedar policy refused:
`{tool, tool_use_id, reason, policy_id, gateway_id}` (`tool` is the catalog name
`<target>___<tool>` when the call went through the signed-in user's Gateway alias,
then `gateway_id` names the workspace gateway; otherwise the Harness tool name and
`null`; `policy_id` is set only when the reason names the determining policy).
The agent's own answer still follows; clients may ignore both events.

Ordinary Agent/proposal `timeout_seconds` defaults to **600 seconds** and `max_iterations` to **100**; explicit
values are retained. Harness executes the corresponding native `timeoutSeconds`
budget. A timeout returns `504 harness.execution_timeout`; cancellation returns
`502 harness.execution_cancelled`; exhausted execution limits return
`502 harness.execution_limit`; an otherwise unfinished response returns
`502 harness.incomplete_response`. Each carries `detail.stop_reason`. An already-open
SSE stream reports the error event instead of completing successfully. These are
execution outcomes, separate from the SDK/network read timeout.

## Python

```python
import requests

BASE, KEY, AGENT = "http://localhost:8000", "lp_live_…", "<AGENT_ID>"

# sync
r = requests.post(
    f"{BASE}/v1/agents/{AGENT}/invoke",
    headers={"X-Api-Key": KEY},
    json={"prompt": "How many vacation days does EMP-1024 have left?"},
    timeout=120,
)
print(r.json()["text"])

# streaming (SSE)
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

Errors use the platform envelope `{code, message, detail}` — e.g.
`auth.missing_api_key` (401), `auth.invalid_api_key` (401 — unknown, disabled, or
offboarded-creator key), `agent.not_active` (409), `agent.not_found` (404).

An AWS-side failure the platform did not map to a service code of its own
(`kb.not_found`, `memory.unavailable`, …) is still returned as an envelope, never
as a bare `500 Internal Server Error` or as botocore's
`An error occurred (…) when calling the … operation:` text. The global
`ClientError` handler in `app/core/errors.py` maps the AWS error code:

| AWS error code | HTTP | `code` |
|---|---|---|
| `ResourceNotFoundException` | 404 | `aws.not_found` |
| `ValidationException` | 400 | `aws.validation` |
| `AccessDeniedException`, `UnauthorizedException` | 403 | `aws.access_denied` |
| `ThrottlingException`, `TooManyRequestsException`, `ServiceQuotaExceededException` | 429 | `aws.throttled` |
| `ConflictException`, `ResourceInUseException`, `RetryableConflictException` | 409 | `aws.conflict` |

`message` is the AWS message with the botocore prefix stripped; `detail` is
`{"aws_error_code": "<AWS code>", "operation": "<boto operation>"}`. Any other
AWS error code (e.g. `InternalServerException`) remains an unhandled 500 with the
traceback in the backend log. A failed cross-account role assumption keeps its own
answer: 502 `workspace.assume_role_failed`. `/v1` shares the handler and returns
the same status and `code`, but its `message` is a generic per-code sentence
(`AWS resource not found`, `AWS rejected the request as invalid`, `AWS access
denied`, `AWS is throttling this request`, `AWS resource conflict`) and `detail`
carries only `aws_error_code` — the raw AWS text names the deployment's role ARN,
instance id and operation, which stay on the console side of the API-key boundary.

## Console Agents API — BYOC uploads

The `byoc` creation method deploys member-written code. The two zip artifact
kinds (`code_zip`, `container_source`) stage their archive here first; the
returned `upload_id` goes into the create body's `spec.byoc`.

| Method | Path | Result |
|---|---|---|
| `POST` | `/api/agents/uploads?python_version=PYTHON_3_13` | `perm:agents.deploy` — `multipart/form-data`, single part `file`, `.zip` only, ≤250 MiB (≤750 MiB uncompressed, ≤20k entries; zip-slip/absolute paths/symlinks refused). Stores `byoc/{workspace_id}/{upload_id}/source.zip` + `manifest.json` in the artifacts bucket → `201` `{upload_id, sha256, size_bytes, original_filename, uploaded_by, uploaded_at, entries_count, uncompressed_bytes, detected: {entrypoint_candidates[], has_requirements, has_dockerfile, agentcore_sdk_detected, adot_detected, adot_source, requirements: {status: ok\|failed\|skipped, package_count, error}}}` — `requirements` is an upload-time dry resolve of the zip's requirements.txt against the deploy target (linux/aarch64 + the optional `python_version`, default PYTHON_3_13); `failed` means the deploy's package stage would fail the same way, `skipped` (no requirements.txt, resolver timeout ~90 s, `uv` unavailable) says nothing either way |
| `GET` | `/api/agents/uploads/{upload_id}` | member — the stored manifest (same shape); another workspace's upload_id answers 404 |

Error codes: `byoc.invalid_upload` (400, missing/non-zip part or empty file),
`byoc.invalid_python_version` (422),
`byoc.upload_too_large` / `byoc.upload_request_too_large` (413),
`byoc.zip_invalid`, `byoc.zip_empty`, `byoc.zip_entry_unsafe`,
`byoc.zip_too_many_entries`, `byoc.zip_uncompressed_too_large` (422),
`byoc.upload_not_found` (404).

The zip's `requirements.txt` follows the pip file format (backslash
continuations, inline comments, environment markers all honoured); `--hash=`
options are dropped because the platform re-locks the file against its own
deploy target with fresh hashes. Refused with a clear error: `-r`/`-c`
includes, `-e`/editable installs, local paths, direct URL/VCS entries, index
options (`--index-url`/`--extra-index-url`/`--find-links` — the platform
installs from its own index only), and more than 500 entries.

`POST /api/agents` with `method: "byoc"` takes `spec.byoc`:
`{artifact_kind: code_zip|container_source|container_image, upload_id?,
image_uri?, entrypoint? (code_zip, default main.py), python_version?
(PYTHON_3_10…PYTHON_3_13, default PYTHON_3_13), install_requirements? (default
true), invoke_contract? (launchpad_prompt|raw), allowed_models? (1–20 unique
Bedrock foundation-model or inference-profile ids)}` — the zip kinds require
`upload_id`, `container_image` requires a private-ECR `image_uri` in the
workspace's account+region. `system_prompt` is optional for this method (it
serves as a description); tools/toolkits/skills/knowledge_bases and protocol
`a2a` are refused in v1. The server stamps `spec.byoc.provenance` from the
upload manifest during deploy.

**Allowed models.** The per-agent execution role scopes `bedrock:InvokeModel`
to exactly `byoc.allowed_models`, so user code calling any other model gets
`AccessDeniedException` at runtime. Entry `[0]` is the **primary** model and
must equal `spec.model_id`: send only `allowed_models` and the server sets
`model_id` to the first entry; send both and `model_id` must be in the list (it
is moved to the front). A spec without `allowed_models` — including every spec
written before the field existed — behaves as `[spec.model_id]`. Re-publish
rewrites the role policy, so an edited list takes effect on the next deploy.
The deployer passes the primary id to the runtime as env `MODEL_ID` and the
full list as env `ALLOWED_MODEL_IDS` (comma-separated, primary first) — for
either variable, a value already in `spec.env` wins.

## Console Agents API — code view

`GET /api/agents/{agent_id}/source` is the read-only view behind the agent's **Code**
page (`/v2/agents?view=source&id=`). It never re-renders a template: it reads back what
AWS holds for the agent, so a template change after the last deploy cannot make the
view disagree with what runs. Member route; the agent must be visible to the caller.

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/agents/{agent_id}/source` | `{agent_id, name, method, kind: code\|harness_config\|image, origin, files[{path, size, content, truncated, omitted: null\|binary\|budget}], files_total, ledger_version, deployment_status, …}`. **`zip_runtime` / `studio`** (`origin: deployed_package`): `GetAgentRuntime` names the S3 zip the Runtime runs; ranged GETs read only its central directory and the platform's files (the entrypoint, `requirements.txt`, `requirements.lock`, a conversion's `code_bundle` files, `skills/…`), never the installed dependencies; adds `entrypoint`, `runtime_version`, `package_updated_at`. **`container`** (`build_context`): `builds/{name}/source.zip`, the context CodeBuild last built from. **`byoc`** `code_zip` / `container_source` (`upload`): the member's staged upload, root prefix stripped, plus `artifact_kind`, `original_filename`, `uploaded_by`, `uploaded_at`, `sha256`; `container_image` → `kind: image` with `image_uri` / `image_digest` and no files. **`harness`** (`harness_config`): `GetHarness` projected through an allow-list (no `arn`, execution role, environment, environment artifact/variables or authorizer), tool header values, OAuth `customParameters` values and URL query strings masked; adds `harness_version`, `config`. Contents are capped at 512 KiB per file (`truncated`) and 4 MiB per response (`omitted: budget`); `?path=<listed path>` reads that one file regardless of the budget (unknown path → 404 `agent.source_file_not_found`). `deployment_status` is the latest deployment's; anything but `succeeded` means the live version may still run earlier code. No source → 409 `agent.source_unavailable` with `detail.reason` ∈ `method` (e.g. imported runtimes), `no_resource`, `deleted`, `gone` (resource or package missing), `not_code`, `unreadable`, `no_bucket`. Never mutates anything |

## Console Agents API — versions and endpoints

`GET /api/agents/{agent_id}/versions` is the read-only AWS view behind the agent
detail's VERSIONS & ENDPOINTS panel. It follows every `nextToken` page of the two
list operations for the agent's resource family and returns an allow-listed
projection — no environment values, artifact locations, execution roles or
authorizer configuration.

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/agents/{agent_id}/versions` | `{kind: runtime\|harness, resource_id, versions[{version, status, description, last_updated_at}], endpoints[{name, live_version, target_version, status, description, created_at, last_updated_at, failure_reason}], latest_version, ledger_version, canary_endpoints[]}` — `versions` newest first; `endpoints` with `DEFAULT` first then by name; `latest_version` is the highest version AWS reports and `ledger_version` the one the last Launchpad deploy recorded (`Agent.version`) — they may differ after an out-of-band update or a canary candidate mint; `canary_endpoints` lists the `stable`/`treatment` names still present. Resource family: `zip_runtime`/`studio`/`container` and imported rows whose `spec.discovery.resource_type` is absent or `runtime` → `ListAgentRuntimeVersions` + `ListAgentRuntimeEndpoints`; `harness` and imported rows with `resource_type == "harness"` → `ListHarnessVersions` + `ListHarnessEndpoints` (harness versions carry no description). Never mutates anything |
| `GET` | `/api/agents/{agent_id}/mcp` | member — MCP servers (`spec.protocol == "mcp"`) only, else `409 agent.not_mcp_server`: `{target_name, target_id, client_id, gateway_id, endpoint, target_status, status_reasons[], tools[{name, description}], tools_error}` — the Gateway target read back live (`MISSING` when AWS no longer has it) and the tools agents see through the Gateway (`<target>___<tool>`); never secrets. Plus `review {state: none\|reviewing\|approved\|rejected, open_review_id, approved_review_id, approved_hash, live_hash, drift}`, the tool-review gate (see *Console MCP tool review API* below): `live_hash` is the hash of the tools listed live (null unless the target is `READY`), and `drift` is true when it differs from `approved_hash`. This read records nothing; `POST /api/mcp-servers/{agent_id}/reviews/check` does. Plus the server's operations (§15.7, §15.8): `qualifier` (`DEFAULT` follows every version, `live` is pinned by a rollback), `live_version`, `health {status: healthy\|drift\|failing\|withheld, checked_at, tool_count, error, consecutive_failures, alert, review_id, repointed}` (the last hourly `mcp.health` check or `POST …/health`), `operation {kind: rollback\|scaffold, status: running\|succeeded\|failed, version?, actor, error, started_at, completed_at, results?}` and `rollback_versions[{version, review_id, decided_at}]` (approved tool lists per runtime version, newest first). `endpoint` carries the current qualifier |
| `GET` | `/api/agents/{agent_id}/network` | member — where the agent runs (projects §12.11): `{agent_id, mode: PUBLIC\|VPC, profile, vpc, enis}`. `profile` is `null` for PUBLIC and for a pre-profile inline network, else `{id, name, vpc_id, subnet_ids[], security_group_ids[], internet, status, checked_at, archived, blocking, warnings}` (the last preflight's blocking / warning counts), or `{id, missing: true}` when the workspace no longer has it. `vpc` is the resolved `{subnets[], security_groups[]}`. `enis` is `{status: ok, scope: "profile", count, in_use, interfaces[{id, subnet_id, availability_zone, status, private_ip, description}]}`, listed per profile (every agent in the same subnets and security groups shares them), or `{status: unavailable, error}` when the workspace role cannot read ENIs (an older spoke template). `404 agent.not_found` for another project's agent |
| `GET` | `/api/agents/{agent_id}/conversions` | member — `{source: {id, name, method, status}, conversions: [agent…]}`: the **Runtime twins** converted from this agent (`POST …/convert` stamps `spec.source_harness.agent_id` on the new `-rt` agent), newest first, each in the ordinary agent projection plus its latest `deployment`. Pure ledger read (no AWS call); deleted twins are omitted; unknown or deleted source → 404 `agent.not_found`. The assistant's NEXT STEPS reads it to switch to the twin once it is `active` and to find it again after a reload |
| `GET` | `/api/agents/{agent_id}/runtime-logs` | member — the Runtime's stdout/stderr (agent detail · Runtime logs). Query: `range` (`1h`\|`6h`\|`24h`\|`7d`, default `1h`), `session_id` (observability session-id shape), `keyword` (≤128, a quoted `FilterLogEvents` term), `include_otel` (default `false`: the shared `otel-rt-logs` stream is dropped), `limit` (≤200, default 100), `next_token`. Returns `{log_group, cloudwatch_url, events[{timestamp, stream, session_id, message}], next_token, session_match: stream\|content\|null, sessions[], hidden_unattributed, unavailable_reason: null\|no_runtime\|log_group_not_found\|access_denied, …}` — events oldest first; the log group is always derived from the row (`/aws/bedrock-agentcore/runtimes/<resource_id>-DEFAULT`, a Harness's backing-runtime group by prefix), never from the caller; a session reads the streams named after it, or (Harness: streams carry no session id) events containing the id; `next_token` pins the first page's time window; another principal's private session is 404 and its events are filtered out; on an agent that can host another principal's private session (a system preset, or the preset an assistant conversation was sent to), an event of a stream named after no session (a Harness's, `otel-rt-logs`) is shown only when its content names a session the caller may read (OTel `session.id` / app-log `sessionId`; `"-"`, blank, `null`, `none` are no session), so such an agent's print-style stdout never shows — `hidden_unattributed` counts the page's dropped lines (`null` under a `keyword`: the count would tell which hidden lines contain it); every other agent shows those lines and reports `0`; `sessions` (first unfiltered page only) lists the sessions owning a stream in the window. Never mutates anything |

Error codes: `agent.not_found` (404, unknown id or another workspace's agent),
`agent.no_resource` (409, the row has no AWS resource to ask about — deploy still
running, failed first deploy, deleted, or a shape that is neither Runtime nor
Harness; `message` is the human reason the panel shows). AWS `ClientError`s map to
the standard 4xx envelope.

## Console Agents API — telemetry status

`GET /api/agents/{agent_id}/telemetry` backs the agent detail's "Tools &
capabilities" card: has the agent's production endpoint written spans, and how
recently. A BYOC code zip deployed without the ADOT launcher emits no span, so it
never appears in Observability and cannot be evaluated; this read is where that
becomes visible. It reuses the evaluation probes (the service name the evaluation
resolver derives, then a `FilterLogEvents` scan of `aws/spans`) and writes nothing
to AWS or the ledger.

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/agents/{agent_id}/telemetry?refresh=` | `perm:project.use` — `{agent_id, endpoint, service_name, status: receiving\|none\|unknown\|unsupported\|not_deployed, last_span_within: 1h\|24h\|7d\|14d\|null, lookback_days, reason, byoc_no_adot, checked_at, cached}`. `endpoint` is the endpoint production traffic uses (`live` for a gated agent, else `DEFAULT`); `service_name` is `<runtimeName>.<endpoint>` (null when it could not be resolved). The windows are probed narrowest first, and `last_span_within` is the first one holding a span; `lookback_days` is the widest (14, the evaluation pre-check's lookback). `byoc_no_adot` is true for a `byoc` `code_zip` whose `spec.byoc.provenance.adot_launcher` is unset. Cached per agent, resource id, version and endpoint for 5 minutes (an `unknown` answer for 30 s); `refresh=true` bypasses the cache and probes again. `cached` says whether this answer came from the cache |

`status` and `reason`:

| `status` | Meaning | `reason` |
|---|---|---|
| `receiving` | A span arrived within `last_span_within` | `null` |
| `none` | No span in the last `lookback_days` days | `byoc_code_zip_no_adot` when `byoc_no_adot`; `no_log_group` for a Harness never invoked on this endpoint; else `null` |
| `unknown` | The probe could not tell (a page cap on a busy group, CloudWatch refused the read, the service name could not be resolved) | `probe_failed`, or the resolver's error code |
| `unsupported` | The method has no span telemetry to probe (an imported Harness, or a method outside the evaluation-supported set `zip_runtime`/`studio`/`container`/`harness`/`byoc`) | `method_unsupported` |
| `not_deployed` | The row has no AWS resource yet | `not_deployed` |

Error codes: `agent.not_found` (404, unknown id, or an agent of another workspace or
project or hidden from the caller). Probe failures never fail the request; they are
`status: unknown`.

## Console Agents API — invoke access

What your own backend needs to call an agent on AgentCore directly (invoke-access
§0.6; the detail page's "Call from your backend" card): its ARNs, the call mode, a
least-privilege IAM policy and call samples. Read-only; nothing here calls AWS.

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/agents/{agent_id}/invoke-access?qualifier=&include=` | `perm:project.use` — `{agent_id, name, method, kind: runtime\|harness, mode: iam\|jwt, account, region, partition, arn, qualifiers[], available_qualifiers[], production_qualifier, sample_qualifier, include[], resources[], policy, jwt, samples, notes[], actions{runtime, harness, harness_verified}}`. `qualifier` is `DEFAULT`, `live` or `candidate` (default: what production uses); `include` is a comma list of `stop_session` (session management actions) and `memory` (reading the agent's memory). `policy` is the IAM policy document a caller role needs; `jwt` is the inbound authorizer's facts for a JWT-mode agent (the token must satisfy them), else `null` |
| `GET` | `/api/projects/{project_id}/invoke-access?agents=&qualifier=&include=&format=json\|csv` | `pperm:project.use` on the path project (a non-member 404) — every callable agent of the project across the workspaces it is bound to: `{project_id, inventory[{agent_id, name, method, kind, mode, account, region, arn, qualifier, endpoint_arn, production}], policy, jwt_agents[], skipped[{agent_id, name, reason}], qualifier, include[]}`; `policy` merges every agent's statements (`null` when nothing is callable). `agents` narrows to a comma list of ids (an id outside the project → `404 agent.not_found`, `detail.agent_ids`). `format=csv` / `json` downloads the inventory as `<project_id>-agent-arns.csv` / `.json` |

Error codes: `agent.not_found` (404), `invoke_access.unknown_qualifier` /
`invoke_access.unknown_include` / `invoke_access.unknown_format` (400, `detail` lists
the allowed values).

## Console Identity API — Connections and bound Gateway targets

The console labels Connections **Outbound credentials** (出站凭证) and the caller's grants **My authorizations** (我的授权); paths, fields and error codes keep `connection`.

A **Connection** is an AgentCore Identity credential provider (OAuth2 or API key) in
the workspace's token vault; see [identity.md](identity.md) for the service-model
findings and the design. Reads are open to members; every mutation requires the
`identity.manage` agent permission (`perm:identity.manage` in `ROUTE_POLICY`; the
workspace-shared gateway-target writes are `infra+perm:identity.manage`). Connections
belong to a project or to the platform (`identity_providers.project_id` may be NULL):
a project sees its own and the platform's; names stay unique per workspace.
Secrets (`client_secret`, `api_key`) are forwarded to AWS and never stored in the
ledger nor returned.

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/identity/connections` | `{connections[{name, kind: oauth2\|api_key, vendor, arn, callback_url, client_id, scopes, template, description, created_at, created_by, system, source: system\|launchpad\|external, status: ready\|missing, referenced_by[{type: agent\|gateway_target, id, name}]}]}` — the token vault joined with the ledger; `missing` = recorded by Launchpad but gone from the vault |
| `GET` | `/api/identity/connections/templates` | `{templates[{id, kind, vendor, fields[], discovery_hint?}]}` — the create form's provider templates |
| `GET` | `/api/identity/connections/oidc-sources` | member — `{sources[{name, vendor, discovery_url, issuer, derived_from: discovery_url\|issuer}]}`: OAuth2 Connections whose stored `oauthDiscovery` yields an OIDC discovery URL (the inbound JWT form's "Choose from a Connection"). System providers and GitHub are left out; the Connection's client id is never returned |
| `GET` | `/api/identity/connections/{kind}/{name}` | one Connection (same shape), re-read from AWS — the callback URL dialog uses it |
| `POST` | `/api/identity/connections/oauth2` | `201` Connection. Body `{name, vendor, template?, description?, client_id, client_secret, discovery_url? \| issuer+authorization_endpoint+token_endpoint, scopes[], obo?{grant_type: TOKEN_EXCHANGE\|JWT_AUTHORIZATION_GRANT, actor_token_content?: NONE\|M2M, actor_token_scopes?[]}}`. `obo` (P3) sets `onBehalfOfTokenExchangeConfig` — CustomOauth2 only (`422 identity.obo_vendor_unsupported`), refused for Cognito or an IdP whose discovery document omits the grant (`422 identity.obo_unsupported`); the Connection item echoes it as `obo` |
| `POST` | `/api/identity/connections/api-key` | `201` Connection. Body `{name, description?, api_key}` |
| `DELETE` | `/api/identity/connections/{kind}/{name}` | `{deleted: true}`; refused with `409 identity.connection_referenced` while an agent spec or a gateway target references it |
| `GET` | `/api/identity/gateway-targets` | `{gateway_id, targets[{target_id, name, description, status, status_reasons, source, system, auth, connection, mode, scopes}]}` for the workspace gateway |
| `POST` | `/api/identity/gateway-targets` | `201` target + `warnings[{code, message, detail}]` (non-blocking). Body `{name, description?, source: openapi\|mcp, openapi_schema \| mcp_endpoint, connection, kind, mode: as_agent\|as_user\|obo, scopes[], api_key?{location: HEADER\|QUERY_PARAMETER, parameter_name, prefix?}}`; an MCP server target takes an OAuth2 Connection only. `mode: obo` sets `grantType: TOKEN_EXCHANGE` and needs a CUSTOM_JWT gateway (`409 identity.obo_needs_jwt_gateway`) and an OBO-configured Connection (`422 identity.obo_unsupported`). When the Connection's issuer differs from the gateway's JWT authorizer issuer, the target is still created and `warnings` carries `identity.obo_issuer_mismatch` `{connection, connection_issuer, gateway_issuer}`: the IdP must trust the gateway's inbound issuer |
| `DELETE` | `/api/identity/gateway-targets/{target_id}` | `{deleted: true}`; only Connection-bound, non-system targets |
| `POST` | `/api/identity/oauth/complete` | `identity.grant` — as_user (3LO) binding leg, called by the `/auth/return` page. Body `{session_id}` (the `session_id` AgentCore Identity appended to the return URL; an opaque one-time capability) → `{completed: true, provider, agent_id, agent_name, tool}` after `CompleteResourceTokenAuth` for the user recorded with the session. The caller must be that user. `404 identity.session_unknown` (never issued, already used, or dropped by a revoke), `409 identity.session_expired` (older than 15 minutes), `403 identity.session_user_mismatch`, `409 identity.session_token_unavailable`, `409 identity.as_user_requires_user_jwt`, `502 identity.session_completion_failed`. See [identity.md §7.4](identity.md#74-routes-permission-and-errors) |
| `GET` | `/api/identity/grants` | member — the caller's **own** as_user grants, never another member's: `{grants[{connection, agent_id, agent_name, tool, scopes[], status: pending\|authorized\|revoked, force_reauth, created_at, updated_at, authorized_at, revoked_at}]}`, newest first (My authorizations / 我的授权). A row with `agent_id` `gw:<gateway_id>` is a COA consent given through the managed COA gateway: `agent_name: null`, never `force_reauth`, and not revocable on the platform |
| `GET` | `/api/identity/grants/{connection}/status?agent_id=` | member — `{connection, agent_id, status: none\|pending\|authorized\|revoked, force_reauth, authorized_at}`; what the Chat auth card polls until the consent completes (`none` until an ask was recorded). A grant is usable only when `authorized` with `force_reauth: false`. The answer echoes the asked `agent_id`, but reads the grant the agent's consent is kept under: the console grant (`""`) for a direct COA Harness agent, `gw:<gateway_id>` for a gateway-mode COA agent |
| `DELETE` | `/api/identity/grants/{connection}` | `identity.grant` — revoke (force re-auth): `{revoked: true, provider, agents}`; the next call through this Connection carries `forceAuthentication=true` and asks for consent again. The revocation clears only when a fresh consent completes. Grants keyed `gw:<gateway_id>` (the managed COA gateway) and their in-flight sessions are left as they are, and `agents` does not count them |
| `GET` | `/api/identity/consent-portal` | member — `{gateway_id, portal: {id, name, status, status_reason, portal_url, execution_role_arn, connection, scopes[], audience, created_at, …} \| null}`, the workspace gateway's Consent Portal read back from AWS every time (`gateway_id: null` without a gateway) |
| `POST` | `/api/identity/consent-portal` | **admin** — `202 {gateway_id, portal}`. Body `{name, description?, connection, scopes[] (default [openid]), audience?, execution_role_arn}`: creates the portal for as_user Gateway targets (`connection` = the inbound IdP, the same issuer as the gateway's JWT authorizer; the execution role is operator-supplied). Not member-grantable: `identity.manage` does not reach it. `409 identity.gateway_missing` / `identity.consent_portal_exists`, `422 identity.invalid_portal_name` / `identity.invalid_role_arn` / `identity.role_account_mismatch` / `identity.portal_scopes`; see [identity.md §7.6](identity.md#76-consent-portal-as_user-gateway-targets) |
| `DELETE` | `/api/identity/consent-portal` | **admin** — `{deleted: true}`; `404 identity.consent_portal_not_found` |
| `GET` | `/api/agents/{agent_id}/identity` | member — `{agent_id, name, method, workload_identity{status, name, arn, allowed_return_urls}, inbound{mode: iam\|jwt, source}, downstreams[{type, name, tool_type, via: agent\|gateway, mode, connection, kind, scopes, connection_status: ready\|missing\|unbound}]}` — the read-only 身份 page |
| `POST` | `/api/agents/{agent_id}/inbound-auth` | `agent.deploy` — `202 {agent, job_id, deployment_id}`. Body `{inbound_auth: {mode: iam\|jwt, jwt?} \| null}` (`null` unpins → workspace default; the key is required and unknown keys are refused with `422 validation.invalid_request`, so a mistyped body never starts a redeploy). Re-publishes the stored spec with the pin swapped: UpdateAgentRuntime on the same runtime, new version. `422 agent.inbound_auth_unsupported` (JWT on harness / A2A), `422 agent.inbound_auth_invalid`, `409 agent.deploy_in_progress` |
| `GET` | `/api/identity/inbound-auth/default` | member — `{workspace_id, default: {mode: iam\|jwt, jwt?}, configured, cognito, cognito_issuer}`; `configured=false` = implicit IAM; `cognito` is a ready-to-use JWT config for the workspace pool, or null before bootstrap; `cognito_issuer` is the pool's issuer (the issuer of every token the console, `/v1` and evaluation present), or null |
| `PUT` | `/api/identity/inbound-auth/default` | `infra+perm:identity.manage` (a workspace-shared default: administrator-only once the workspace binds several projects) — body `{mode, jwt?: {discovery_url, allowed_clients[], allowed_audience[], allowed_scopes[], custom_claims[], source_connection?}}` → the GET shape. `source_connection` is display-only and never reaches the authorizer. The discovery document is probed first (`422 identity.discovery_unreachable` / `identity.discovery_invalid`). Deployed agents keep their authorizer until redeployed |

Error codes (`identity.*`): 409 — `connection_exists`, `target_exists`,
`connection_referenced`, `system_connection`, `system_target`, `target_unbound`,
`no_gateway`; 404 — `connection_not_found`, `target_not_found`; 422 —
`unsupported_vendor`, `missing_endpoints`, `endpoints_custom_only`,
`invalid_target_name`, `invalid_openapi`, `invalid_mcp_endpoint`,
`target_auth_unsupported`, `obo_invalid`, `obo_vendor_unsupported`, `obo_unsupported`,
`discovery_unreachable`, `discovery_invalid`; 409 — `obo_needs_jwt_gateway`. The as_user
consent and Consent Portal codes (`session_*`, `as_user_requires_user_jwt`,
`consent_portal_*`, `gateway_missing`, …) are listed in
[identity.md §7.4](identity.md#74-routes-permission-and-errors). Agent create/redeploy additionally rejects a `ToolRef.auth`
naming an unknown Connection (`connection_unknown`), a kind that does not match it
(`kind_mismatch`), `as_user` on an api_key Connection (422 from the schema), or `obo` on a
tool (`mode_unsupported`; obo runs on a Gateway target).

## Console System Agents API — managed presets

System-managed presets (see architecture → *System-managed presets*) are installed,
repaired and removed only here. Reads never touch AWS; the install is an explicit,
billable operator action that runs the normal deploy pipeline.

| Method | Path | Role | Result |
|---|---|---|---|
| `GET` | `/api/system-agents` | member | `{workspace_id, presets[{key, name, label, description, method, skill_version, installed_skill_version, update_available, status, requirements[{code, message}], name_collision, agent_id, agent_status, error, job_id, deployment_id, deployment_status, model_id, model_source, knowledge_bases[], allowed_tools[], memory, settings{model_id, model_source, max_tokens, reasoning_effort, system_prompt, max_iterations, timeout_seconds, knowledge_bases[]} | {}, defaults{same members}, editable_fields[], operation, can_install, can_repair, can_configure, can_uninstall, updated_at}]}` — `status ∈ configuration_required | not_installed | deploying | uninstalling | active | failed`; `operation` is `{kind: uninstall, job_id, job_status, attempt, error, retryable} | null` while a teardown job owns the row; requirement codes `bootstrap_not_ready | missing_artifacts_bucket | missing_execution_role | per_agent_roles_disabled | missing_oauth_provider`; `memory` is `disabled`; ledger-only |
| `POST` | `/api/system-agents/{key}/install` | admin | required JSON body, a partial edit: `{model_id?, model_source?, max_tokens? (1–131072, per model call = bedrockModelConfig.maxTokens), reasoning_effort? (low|medium|high; OpenAI model on model_source=bedrock only), system_prompt?, max_iterations? (1–100), timeout_seconds? (10–3600), knowledge_bases?[{kb_id, name?, description?}], reset?[editable field…], clear?["max_tokens"|"reasoning_effort"], force?}` (`{}` = preset defaults on install, stored choices on repair; omitted = keep, `reset` = build default, `clear` = send nothing; unknown members → `422`) → `202 {agent, job_id, deployment_id, created, changed, preset}` when a job is in flight (a bodiless repair of a deploying preset returns its existing job, `changed: false`; an explicit edit while deploying → `409 system_agent.deploy_in_progress {job_id}`; a racing install returns the winner's job unless it asked for different settings → `409 system_agent.deploy_in_progress`; a partial edit is resolved against the row inside the claiming transaction with a version-conditioned claim, re-resolved on a changed row and `409 system_agent.conflict` after three attempts), `200` with the last job's id when the active preset already matches; `422 system_agent.invalid_options` for an unsupported pairing (e.g. a reasoning effort on a Claude model). Knowledge bases are verified in the target workspace during the provision stage |
| `POST` | `/api/system-agents/{key}/skill-registration` | admin | no body (the resource is server-selected: the release the stored spec pins, proven against this build and read back from S3) → `200 {preset_key, record{system, record_id, name, type: AGENT_SKILLS, status, version, descriptors, …}, created, changed, submitted, note, skill{name, version, digest, path, files[]}, preset}` — registers the **active** preset's published Skill release as its own Registry record pointing at the immutable `system-skills/<name>/<version>-<digest12>/` prefix (no S3 write, no Harness re-publish, the agent's A2A record untouched). `created` ⇒ a new record submitted for review (never approved here); `changed` without `created` ⇒ the descriptor rolled forward to a newer release (DRAFT, review again, `recordVersion` bumped); neither ⇒ identical release, no-op (approval kept). Idempotent and race-safe (durable `clientToken`, per-preset lock); the deploy pipeline's register stage performs the same registration on install/repair |
| `DELETE` | `/api/system-agents/{key}` | admin | `202 {agent, job_id, operation: uninstall, attempt, started, preset}` — claims the row (`uninstalling`, optimistic CAS) and queues the teardown job in one commit; simultaneous requests share one job (`started: false` for the loser); the row keeps its identity until the exclusive (per-agent advisory lock, single host), fenced worker has verified every KB target, the Harness and the dedicated role are gone (per-step `progress` with exact resource ids on the job, carried into the next attempt only when verified); a failed teardown gets attempt N+1; `409 agent.deploy_in_progress` while a deploy runs |

`GET /api/system-agents` additionally reports `skill_registration` (`{record_id,
pending_record_id, status: creating | accepted | registered, release_version,
release_digest, path, updated_at} | null`, ledger-only; `accepted` = our create returned
`pending_record_id` but the read-back has not verified it, `record_id` stays `null`
until it does; `null` when nothing is mapped for the workspace's current registry) and
`can_register_skill`. Registry records (`GET
/api/registry/records[/{id}]`, search, action/update/reimport responses) carry a
server-derived `system` member — `{managed: true, preset_key, label, skill_version,
release_digest, path, protected_actions[], admin_actions[]} | null` — set exactly when
the workspace ledger maps the record (verified or accepted id) to a system preset's
Skill **in the workspace's current registry**; it is never read from descriptors or
tags. For such a record `PUT`, `POST …/reimport` and `DELETE` answer
`403 registry.system_skill_protected` for every caller, `POST …/action` answers it for
members (administrators may submit/approve/reject/disable), and an ordinary Skill
register/import under the reserved name answers `409 registry.name_reserved`.
Skill-registration error codes: `system_skill.preset_not_active` (409),
`system_skill.release_mismatch` (409, this build's bundle is not the installed
release), `system_skill.bundle_unverified` (409, the published S3 release differs from
the snapshot; nothing written), `system_skill.foreign_record` (409, a same-name Skill
record this platform cannot prove it created — also a lost-create replay the service
did not honour; nothing bound), `system_skill.stale_release` (409, the caller's release
is older than the installed / verified / intended / remote one, or pins another digest
of the same version),
`system_skill.record_deprecated` (409, terminal — delete in AWS and register again),
`system_skill.readback_mismatch` (409), `registry.unavailable` (503).

Error codes: `system_agent.unknown` (404), `system_agent.workspace_not_ready` (409,
`detail.requirements[{code, message}]`), `system_agent.name_collision` (409, an
ordinary agent holds the reserved name — never adopted), `system_agent.not_installed`
(404 on uninstall of an absent preset), `system_agent.uninstalling` (409 on install
or repair while a teardown job owns the row or its last attempt failed —
`detail.job_id/job_status/error`), `agent.deploy_in_progress` (409 on uninstall while
deploying). On the ordinary agent routes a preset answers
`agent.system_managed` (403, `detail.action ∈ redeploy | delete | convert |
experiment | canary | promote | …`, `detail.maintenance_route`) before any AWS call;
the experiment and runtime-canary action routes answer the same for rows referencing
a preset; `DELETE /api/knowledge-bases/{kb_id}` answers `kb.attached_to_system_agent`
(409, `detail.agents`) when the KB is mounted on a preset, with or without `force`;
and `POST /api/agents` with a reserved name answers `agent.name_reserved` (409).
Every agent projection carries `system: {managed, key, label, skill_version,
protected_actions} | null`. `AgentSpec.allowed_tools` (harness only) accepts 1–64
character entries matching `*|@?name(/tool)?`.
`AgentSpec.native_tools` is a unique list drawn from `shell` and `file_operations`,
defaults to `[]`, and is only supported for Harness. `allowed_tools: null` derives
runtime selectors from the resolved attachments, Skills and native selections;
deployment always sends `allowedTools`, including `[]` for an empty selection.
Explicit patterns remain expert overrides with the existing mounted-KB support.
The console shows preserved overrides and requires an explicit switch back to
selection-derived access before native checkboxes take effect. Presets retain
their server-owned overrides.

## Console Architect Assistant API — reviewed Harness proposals

The architect assistant (see architecture → *Architect assistant (SE-039)*) is a
member conversation with the protected `aws-agent-solution-architect` preset that ends
in an inert proposal for one new managed Harness. Discussion routes are `member`
(parity with Chat); the approval rides `perm:agents.deploy` (parity with `POST
/api/agents`) and additionally re-resolves the caller's account, permission and
workspace grant from the database inside its write transaction. Every route is
workspace-scoped **and principal-bound** (`user:<id>` / `config-admin` /
`local-operator`; usernames are display only): a conversation of another principal —
another member, an administrator, or a re-registered account with the same username —
answers `404 assistant.conversation_not_found`. Reads are ledger-only except where
noted.

Conversation detail additionally contains `preparation: {revision, knowledge_bases,
skills, tools, requirements}`. `tools` contains Registry MCP/Gateway catalog keys
(at most 20); Skills and KBs remain capped at 10 each. Each advisory requirement has `id`, `kind`
(`knowledge_base | skill | tool | clarification`), `title`, `reason`,
`materials: string[]`, and `required: boolean`. Selections and requirements are private
to the conversation owner; they do not grant resource access or approve deployment.

Conversation detail also contains `context_digest: {text, through_turn, updated_at,
generating}`, the confirmed-items digest. Turns that fall out of the replay window
are rolled, together with the previous digest, into a short Markdown list of what
was stated, suggested and confirmed, plus the open questions. The digest is placed
in the turn preamble before the current proposal and is marked as possibly
incomplete. After a turn ends, a background refresh starts when the next turn's
window would omit turns the digest does not cover yet. The refresh is one Bedrock
Converse call on the cheap default model (`assistant_digest_model_id`, else
`agent_default_model_id`), with a 60 s read timeout and no retries. A failure keeps
the previous digest, and no turn waits for it. `text`, `through_turn` (the last turn
covered) and `updated_at` are `null` while there is no digest. `generating` is true
while a refresh or rebuild is in flight in this process. The digest is conversation
state only: it never enters a proposal, its hash or its approval.

| Method | Path | Role | Result |
|---|---|---|---|
| `GET` | `/api/assistant/architect` | member | `{workspace_id, account_id, region, available, reasons[], preset{key, label, status, agent_id, requirements[], can_install}, can_deploy, deploy_requirements[{code, message}], capabilities{shared_memory, kb_gateway}, is_admin, owner, principal}` — `available` ⇔ the preset is `active`; `capabilities` says which prerequisites a proposal may bind to (never created here); ledger-only |
| `GET` | `/api/assistant/architect/conversations` | member | `{conversations[{id, title, owner, mine, read_only, shared, shared_by, shared_at, turns, turn_in_progress, status, proposal_status, proposal_revision, created_at, updated_at}]}` — the caller's own plus every conversation its owner or an admin shared in this workspace (`mine: false`), newest first (≤ 50); an **admin** lists every member's conversation of the workspace (≤ 200), another member's unshared one with `read_only: true`; `owner` is the creator's display username; each `messages[]` row of the detail carries `author` (the sender of a `user` row) |
| `POST` | `/api/assistant/architect/conversations` | member | body `{title?}` → `201` conversation detail (below); snapshots the workspace **catalog** (registry attachables, live gateway ARN + outbound-auth identity per gateway record, S3 content digest per skill, ACTIVE managed KBs, workspace prerequisites — the only AWS reads); `409 assistant.unavailable` (`detail.preset_status`) while the preset is not active |
| `GET` | `/api/assistant/architect/conversations/{id}` | member (owner, or anyone while shared); admin (any, `read_only` while unshared) | `{…summary, catalog{fetched_at, tools[{key, kind, name, description, attachable, reason, gateway_arn?, auth_type?, outbound_auth?, url?, record_id}], skills[{key, name, description, path, record_id, content_digest, object_count}], knowledge_bases[{kb_id, name, description}], warnings[], resources{memory_arn, kb_gateway_id, kb_gateway_arn, oauth_provider_arn, execution_role_arn}, target{workspace_id, account_id, region}}, messages[{id, turn, role: user|assistant|tool|error, text, name, at}], proposals[…]}` |
| `PUT` | `/api/assistant/architect/conversations/{id}/sharing` | member (owner) or admin | body `{shared: bool}` → summary. Opens (or closes) the conversation to every member of the workspace, stamping `shared_by` / `shared_at`; does not touch `updated_at`. Only a conversation the caller can already reach (its own, or one already shared; an admin reaches every conversation, so it may share any — that is how a read-only one becomes a collaboration) — another member's private one is a 404 for a non-admin; a collaborator on a shared conversation who is not its owner nor an admin is a 403 `assistant.share_forbidden`. While shared, every conversation route below — turns, catalog, preparation, proposal edit/reject/approve, evaluation plan and assets — accepts any member under that route's usual permission (re-checked inside each write, so unsharing takes effect immediately); only `footprint` and `DELETE` (CLEAR) stay with the owner (404 for a collaborator). An admin's read of an unshared conversation (`GET` detail, `evaluation-plan`, `evaluation-plan/operations/{id}`) never widens a write: every write route answers it 404 until the conversation is shared |
| `POST` | `/api/assistant/architect/conversations/{id}/catalog` | member | re-reads the catalog → `{catalog, conversation}`; consume the returned conversation because refreshed bindings can advance preparation and proposal revisions |
| `PUT` | `/api/assistant/architect/conversations/{id}/preparation` | member | `{expected_revision, knowledge_bases: [kb_id], skills: [catalog_key], tools?: [catalog_key]}` → full conversation detail, including `preparation`. Missing `tools` preserves existing MCP choices for older clients; `[]` explicitly removes them. Every list is unique. Validates live resource bindings and creates a new reviewable proposal revision when applicable, retaining evaluation rules for fresh validation rather than broadening them. Refuses a stale preparation revision or an in-flight turn. |
| `POST` | `/api/assistant/architect/conversations/{id}/preparation/skills` | `perm:agents.deploy` | `{expected_revision, staging_id, selections: [{index}]}` → `{conversation, results: [{name, ok, key?, error?}]}`. Compatibility endpoint for earlier assistant imports; the current console creates new Skills in Registry. Imports server-validated staged bundles into conversation-owned sources and selects successful imports; never accepts a client S3 path. |
| `POST` | `/api/assistant/architect/conversations/{id}/turns` | member | body `{prompt}` (≤ 100k chars / 300k bytes, and it must fit the request budget with the preamble) → SSE `meta{conversation_id, turn, session_id, agent, omitted_turns} → (tool|delta)* → proposal? → done` or `error{code?, message}`; a transient upstream failure first emits `retry{attempt, reason}` and replays the turn once in a fresh session (discard what was streamed so far) (errors are kept on the transcript; a stream closed by the client leaves the partial answer + an `interrupted` error row and no proposal). One `InvokeHarness` on the preset with the bounded replayed transcript (paired by turn, ≤ 12 turns / 160k chars incl. preamble); a `launchpad-proposal` block (≤ 64 000 bytes) becomes a new revision (`draft` or `invalid`); **no other write**. Before the stream opens: `409 assistant.unavailable`, `409 assistant.turn_in_progress` (`detail.active_turn` — one in-flight turn per conversation), `409 assistant.conversation_full` (200 turns), `413 assistant.prompt_too_large` |
| `POST` | `/api/assistant/architect/conversations/{id}/digest/regenerate` | `perm:project.use` (owner) | no body → `{context_digest}` (the shape above). The owner's "wrong, regenerate": rebuilds the digest synchronously from every turn outside the replay window, ignoring the stored digest (one Converse call, input bounded at 60 000 chars). Writes only the ledger, conditioned on the stored digest so a concurrent refresh cannot store a second result. A collaborator of a shared conversation reads the digest but gets `403 assistant.digest_owner_only`; anyone else gets `404 assistant.conversation_not_found`. `409 assistant.digest_nothing_to_summarize` (every turn is still in the replay window), `409 assistant.digest_busy` (a refresh or rebuild is already running for the conversation), `409 assistant.conversation_clearing`, `502 assistant.digest_failed` (the model call failed; the previous digest is kept) |
| `PUT` | `/api/assistant/architect/conversations/{id}/proposal` | member | body `{content}` (the proposal allowlist; unknown outer members → 422) → `{proposal}` — a **new** revision (`source: member`, unique monotonic number), never a mutation; invalid content is stored as `invalid` with `validation_errors`, never corrected; `413 assistant.proposal_too_large` above 64 000 serialized bytes (nothing stored; the normalized stored content is re-checked against the same cap); `409 assistant.conversation_full` at 50 revisions. Every assistant write is also bounded at ingress: `413 assistant.request_too_large` above 512 000 received bytes |
| `POST` | `/api/assistant/architect/conversations/{id}/proposal/reject` | member | body `{revision}` → `{proposal}` with `status: rejected` (non-executable); a conditional transition — `409 assistant.proposal_stale` for a non-current revision, `409 assistant.proposal_already_approved` (`detail.approval`) when the revision was executed meanwhile |
| `POST` | `/api/assistant/architect/conversations/{id}/proposal/approve` | `perm:agents.deploy` | body `{revision, content_hash}` → `202 {proposal, agent, job_id, deployment_id, started: true}` when this call claimed the revision, claimed the agent name (shared with `POST /api/agents`) and created the ordinary agent + deployment + `deploy_agent` job with their ids on the proposal in one commit; `200 … started: false` with the recorded outcome for a repeated, concurrent or historical (already approved, even if newer revisions exist) request — and a still-`queued` job with no live worker is re-woken. Refusals, all before any write: `401 auth.required` / `403 auth.permission_required` / `403 workspace.forbidden` (re-resolved from the database at the claim, after the live catalog read), `409 assistant.proposal_stale` (unknown revision / hash differs / changed while approving), `409 assistant.proposal_not_approvable` (invalid, rejected, superseded), `409 assistant.workspace_not_ready`, `409 assistant.proposal_invalid` (live catalog no longer has a referenced resource or prerequisite — e.g. the KB gateway), `409 agent.name_reserved`, `409 agent.name_exists` (atomic — one of two racing creators), `409 assistant.bindings_changed` (`detail.changed[]` — a key resolves to a different URL, gateway auth identity, skill content, memory or KB gateway than reviewed), `502 assistant.catalog_unavailable` (live catalog unreadable and no winner exists); every already-approved answer re-validates the caller first |

The turns body also accepts
`evaluation_plan_repair: {plan_revision: <positive integer>, plan_hash: <64 lowercase hex>}`.
The reference is strict and resolves the current saved invalid plan within the owned
conversation/workspace. The server appends its validation errors, content and source/latest
proposal context to the prompt; the ordinary SSE and inert revision lifecycle is unchanged.
Before streaming: `409 assistant.evaluation_repair_stale` for a changed reference/source,
`409 assistant.evaluation_repair_not_needed` for a plan that is no longer invalid,
`409 assistant.evaluation_plan_source_invalid` when the latest proposal needs correction,
and `413 assistant.evaluation_repair_too_large` when the complete context exceeds the
ordinary prompt/replay budgets. No evidence is truncated. The console explicitly prepares
a plan from the returned usable new proposal and displays its validation result for review;
repair creates no evaluation assets and does not approve or deploy the proposal.

Catalog tool entries optionally carry `runtime_tools: string[] | null`, the exact
Harness names usable in evaluation rules. Remote MCP names come from complete,
bounded read-only `tools/list` discovery; Gateway names come from approved Registry
descriptors. `null` means discovery is unavailable and a literal positive allowlist
cannot be verified. Resource selectors (`mcp:…`, `gateway:…`, `builtin:…`) are
rejected inside code-rule tool fields. Positive allowlists must include mounted
Skill/KB support calls; plan save and approval also reject names outside the selected
catalog. Refresh the conversation catalog before preparing a replacement draft.
The catalog also exposes `runtime_builtin_tools` for native Harness `shell` and
`file_operations`. A proposal's optional `native_tools` list selects these capabilities;
omission means no native tools. The selection is visible in review and bound to the
approval hash (`resources.tool_access_policy=selected-v1`). Evaluation rules cannot
grant an unselected native capability or silently expand an approved allowlist.

A proposal is `{id, conversation_id, revision, source: model|member, status: draft|invalid|
approved|rejected|superseded, content, content_hash, bindings, validation_errors[],
created_by, created_at, approval, rejected_by, rejected_at}`. `content` is the
allowlisted object `{version: 1, name, model_id, model_source, system_prompt, tools[key],
skills[key], knowledge_bases[kb_id], memory: disabled|workspace, max_iterations,
timeout_seconds, summary, requirements_baseline[], assumptions[], manual_tasks[],
golden_tests[{id, input, expected_response, expected_tools[], forbidden_behavior,
pass_criteria, evaluator, source}], evaluator_recommendations[]}`; `bindings` is the
resolved `{name, method, model_id, model_source, tools[ToolRef], skills[s3 path],
knowledge_bases[KnowledgeBaseRef], memory{short_term, long_term, memory_id},
max_iterations, timeout_seconds, resources{gateways{<gateway_id>: {gateway_arn,
gateway_name, record_id, auth_type, outbound_auth}}, remote_mcp{<name>: {url,
record_id}}, skills{<key>: {record_id, path, source_prefix, content_digest, object_count, total_bytes}},
kb_gateway{gateway_id, gateway_arn, oauth_provider_arn, url, authorizer_type, authorizer} | null, memory{mode, arn},
execution_role_arn}}` (null when invalid; `outbound_auth` is an identity — provider
ARN, grant type, scopes — never a credential value); `content_hash` = sha256 of
canonical `{content, bindings}`. The deploy job carries `{content, bindings}` on its
payload and re-checks them at job entry (drift → the job fails before any stage). The
Observability routes (`/api/observability/sessions*`, `/traces*`, `…/evaluate`) hide
another principal's assistant sessions and answer `404
observability.session_not_found` / `observability.trace_not_found` for their details. `approval` is
`{approved_by, approved_at, agent_id, agent_name, agent_status, agent_error,
deployment_id, job_id, job_status} | null`; the job and agent are ordinary rows readable
through `GET /api/jobs/{id}` and `GET /api/agents/{id}`. The generic invoke entrances
(`POST /api/chat/{id}`, `POST /api/agents/{id}/invoke`, `/v1 …/invoke[-stream]`) answer
`404 chat.session_not_found` for an assistant turn's `session_id` on a system-managed
agent.

### Evaluation-assets plan (SE-047)

Private to the conversation owner (foreign principal / workspace → `404
assistant.conversation_not_found`, administrators included). Preparing/editing is
`member`; creating and cleaning up is `admin` **and** owner. Reads are ledger-only.

Each newly created code evaluator receives a single-rule Lambda package and its own
resource chain. Operation resources may carry `code_group`, identifying the evaluator
key that owns the Lambda/role/log group/permission/grant. New plan summaries count
these chains individually; materialized historical plans use their recorded resource
counts. Existing single-function operation keys remain readable.

| Method | Path | Role | Result |
|---|---|---|---|
| `GET` | `/api/assistant/architect/conversations/{id}/evaluation-plan` | member | `{plans[…], operations[…], disclosure}` |
| `POST` | `…/evaluation-plan/prepare` | member | body `{revision}` (a shape-valid proposal revision, approved or not) → `201 {plan, plans, operations, disclosure}` — the platform draft as a new plan revision; `409 assistant.proposal_stale` / `409 assistant.evaluation_plan_source_invalid`; no side effects |
| `PUT` | `…/evaluation-plan` | member | body `{content}` (the plan contract; `content.source_revision` names the proposal revision) → `{plan, …}` — a **new** revision, `draft` or `invalid` with `validation_errors` (never corrected); `413 assistant.evaluation_plan_too_large` above 160 000 bytes |
| `POST` | `…/evaluation-plan/materialize` | `perm:agents.deploy` (owner) | body `{plan_revision, plan_hash, acknowledge_disclosure: true}` → `202 {operation, started: true}` when this call atomically claimed the plan (still draft, this hash, newest revision; worker launched), `200 … started: false` for a repeated / concurrent request (same operation). `422 assistant.disclosure_required`, `409 assistant.evaluation_plan_stale` (unknown revision / hash differs / edited, superseded or claimed meanwhile), `409 assistant.evaluation_plan_not_approvable` (invalid / superseded), `409 assistant.evaluation_plan_invalid` (no longer validates against its proposal), `409 assistant.workspace_not_ready`, `409 assistant.execution_role_untrusted` (grant requested but the workspace execution role is not platform-tagged); the caller (admin + owner) and the workspace identity are re-resolved from the database inside the claim and pinned on the operation |
| `GET` | `…/evaluation-plan/operations/{operation_id}` | member | `{operation}` — ledger only, no AWS call |
| `POST` | `…/evaluation-plan/operations/{operation_id}/retry` | `perm:agents.deploy` (owner) | `{operation, started}` — resumes the persisted intents of a `partial` / `failed` operation (same tokens/requests); `409 assistant.evaluation_assets_new_plan_required` rejects a legacy shared or unknown Lambda package before queuing or consuming an attempt; `409 assistant.evaluation_assets_exhausted` after 5 attempts |
| `POST` | `…/evaluation-plan/operations/{operation_id}/lambda-revision-review` | `perm:agents.deploy` (owner) | body `{plan_hash, expected_created_revision_id, expected_current_revision_id, cloudtrail_event_id, reason}` (`extra="forbid"`, non-empty reason) → `{operation, review, started}` — **SE-049 reviewed recovery of exactly one conflict**: the RevisionId `CreateFunction` answered with moved while the function was still provisioning (`Pending` → `Active`) and the worker refused to publish (`resources[lambda_function].review.kind = initial_revision_changed`). The server reads the nominated CloudTrail event itself (`LookupEvents` by `EventId`; the client's JSON is never trusted) and requires **exactly one** successful `CreateFunction20150331` record from `lambda.amazonaws.com` in the pinned account/region whose request fields equal the recorded request and whose response carries the recorded FunctionArn, the expected created RevisionId, the reviewed CodeSha256, `state = Pending` / `stateReasonCode = Creating` and a `lastModified`; then the settled `$LATEST` must be that answer plus **only** the lifecycle transition — `Active` / `LastUpdateStatus Successful`, exactly `expected_current_revision_id`, the same `LastModified`, every approved field and every optional security-relevant member (environment, layers, VPC, KMS, file systems, dead-letter, signing, architecture, tracing, logging, ephemeral storage, SnapStart, image config, runtime version) equal — with `$LATEST` the only version, no alias, no resource policy, no reserved concurrency, and the operation's role / log group still carrying their recorded identity. On success an **append-only** review entry (reviewer, reason, event id / time / request id, verified fields, old and new snapshot, exact plan binding) is stored on the resource (`reviews[]`; the original CreateFunction evidence is never overwritten), the baseline `revision_id` / `settled_revision_id` move to the reviewed value, the Lambda conflict and its blocked dependents (`lambda_permission`, `role_grant`, code evaluators) are re-queued and the ordinary worker resumes — its `PublishVersion` still carries both preconditions, so a later change fails there. **No cloud write happens in this route.** The exact same request is idempotent (`started: false`, the recorded review, no CloudTrail read); a different one after a review is `409 assistant.lambda_revision_review_stale`. Refusals (nothing recorded, nothing written): `422 assistant.lambda_revision_review_reason_required`, `409 assistant.evaluation_plan_stale` (hash), `409 assistant.lambda_revision_review_not_applicable` (operation not partial/failed, function not an owned accepted create blocked solely by the pre-publish RevisionId drift — a lost create, a published version, a publish intent or a re-pinned baseline — or another unrelated open outcome), `409 assistant.lambda_revision_review_stale` (created RevisionId / transition mismatch, prior different review), `409 assistant.lambda_revision_review_unverified` (`detail.fields` names the differing members; also 0 / 2+ / malformed events, an unterminated event history, `InProgress` / `Failed` update status, extra versions / aliases / policy / concurrency, changed role or log-group identity), `409 assistant.evaluation_assets_running`, `409 assistant.evaluation_assets_exhausted`, `409 assistant.evaluation_assets_stopped` (approver or workspace identity changed). The four sides are compared losslessly along the installed Lambda model: the **recorded request** ↔ the event's `requestParameters` (member for member; an extra member such as an environment is not approved), the **immutable accepted answer** (`create_response`, when present) ↔ the event's `responseElements`, that answer ↔ the **current `$LATEST`** (every non-lifecycle member present, absent and equal alike — an extra `DurableConfig` / `TenancyConfig` / `CapacityProviderConfig` / `MasterArn` or any member unknown to the platform is a difference), and every member of the answer must be fixed by the request or be a documented service default (`PackageType Zip`, `x86_64`, `PassThrough` tracing, 512 MB ephemeral storage, SnapStart off, text logging to `/aws/lambda/<name>`, a region-local runtime-version ARN). Only structure member names are casing-normalized; data-map keys / values (environment variables, tags) and empty strings are content; presence is compared with an explicit absent sentinel (a member present as `null` or with a wrong type is a difference, never absence), every side is type-validated against the installed Lambda model before comparison (a malformed value is refused, not normalized), the only absent-versus-empty equivalence is a documented envelope in its well-formed empty shape (`environment: {}` / `{"Variables": {}}`, `Layers` / `FileSystemConfigs` `[]`, an all-empty typed `VpcConfig`), and `CodeSize` is retained on the accepted answer and compared across answer, event and current function. Dependencies are re-compared with the recorded snapshot (RoleId / ARN / trust / inline policy / tags; log-group creationTime / ARN / retention / tags), and a resource policy is proven absent only by a `NotFound` — an empty or unreadable document is refused. The review's conditional UPDATE binds every value the review relied on as predicates of that one statement — the operation's status / lease token / attempts / plan id / plan revision / plan hash / owner / approver / exact pinned JSON / exact intents JSON, the approved plan row (id / revision / status / hash column **and** the exact JSON of the validated content, whose canonical hash was checked), the conversation owner (== the operation's recorded owner == the caller), the workspace account / region / role / external id **and** its exact `resources` JSON (execution role included), and the approver's **and** the reviewer's active, unexpired administrator rows — with the caller re-resolved from the database inside the host lock, so a change committed by another session up to that statement makes it a no-op (`409 assistant.evaluation_assets_stopped`). The verified state is persisted as `reviewed_baseline` on the resource and the resumed worker re-validates it immediately before its first mutation (whole configuration + tags, dependencies, `$LATEST` the only version, no alias, policy absent, no reserved concurrency) — an externally published same-code version, a foreign alias / policy / concurrency or any configuration drift is a `conflict`, never adopted or overwritten, and is not reviewable again. Independently of a review, the ordinary worker refuses to adopt a same-digest version after a `PublishVersion` refused on its first dispatch (only a lost answer of its own dispatch reconciles) and never overwrites a reserved concurrency it did not set. Limitations: CloudTrail is positive evidence for a human review, not proof that no other write happened (event history is eventually consistent and lists only what was recorded); the console needs `cloudtrail:LookupEvents` in the workspace role; the external-administrator check→write window remains one call wide |
| `DELETE` | `…/evaluation-plan/operations/{operation_id}/assets` | `perm:agents.deploy` (owner) | `{operation}` — deletes exactly the owned cloud artifacts whose identity still matches (evaluators first; grant / function / log group / role only once no owned evaluator remains), one checkpoint per effect; the local Dataset stays; `409 assistant.evaluation_assets_running` while a worker is live, `409 assistant.evaluation_assets_stopped` when the approver or workspace identity changed |
| `GET` | `/api/assistant/architect/conversations/{conversation_id}/footprint` | member (owner) | What CLEAR would remove: `{agents[{id,name,status,method}], canaries[{id,name,kind,status,agent_id,running_action,pending,resources{ab_test,gateway,endpoints[],online_evaluations}}], experiments[{id,name,agent_id,status,pending}], operations[{id,status,plan_revision,dataset_id,cloud_resources}], datasets[{id,name,item_count,cloud}], blockers[{kind: turn\|preparation\|operation\|job\|canary\|experiment, id, reason}], required_permissions[], can_clear, purge{job_id,status,error,blocker,step,updated_at}\|null, turns, proposals}` — `canaries` / `experiments` are those run on the conversation's Agents (`pending` = cleanup not run or left resources); `agents` also keeps one a failed clear already marked deleted but has not read back gone; `required_permissions` lists `agents.deploy` when cloud assets are involved and `agents.delete` when an Agent is; `can_clear` = the caller holds them all; `purge` = the newest clear job. Ledger read only |
| `DELETE` | `/api/assistant/architect/conversations/{conversation_id}` | member (owner) + every `required_permissions` key | A transcript-only conversation is deleted inline: `200 {deleted: true, conversation_id, operations_cleaned: [], datasets: [], agents: [], canaries: [], experiments: []}`. Anything else is a durable clear job: `202 {deleted: false, job_id, status, …}` — poll `GET /api/jobs/{job_id}` (`payload.step`, `payload.result`, `payload.blocker` on failure). In dependency order, each step read back on AWS before the next: canaries on the Agents (stop + `act_cleanup`, then wait until A/B test, online evaluations, gateway and both named endpoints are gone) → experiments → the fenced cleanup of every cleanable evaluation-assets operation → the local Datasets (a copy synced to AWS by hand stays) → every Agent (resource delete, wait until not found, its execution role, its own A2A Registry record, ledger) → canary candidate zips → the ledger rows. Refusals before anything is deleted: `409 assistant.conversation_busy` (a turn, import, operation, deployment job or canary / experiment action is live), `403 auth.permission_required`. A live job is returned again by a repeated DELETE; while it runs conversation writes answer `409 assistant.conversation_clearing` and the Agents' canary / experiment / redeploy / convert / delete routes `409 assistant.agent_clearing`. A job that cannot finish ends `failed` with `payload.blocker.code` one of `assistant.purge_canary_incomplete`, `purge_experiment_incomplete`, `conversation_assets_remain`, `purge_still_deleting`, `purge_delete_failed`, `purge_agent_dependency`, `purge_agent_conflict`, `purge_agent_busy`, `purge_role_remains`, `purge_dependency_busy`, `purge_unowned`, `purge_step_failed` (or a refusal's own code); the conversation is kept and the next DELETE resumes from the verified checkpoints |

A plan is `{id, conversation_id, proposal_id, source_revision, source_content_hash,
revision, source: platform|member|model, status: draft|invalid|approved|superseded,
content (scenarios carry `review_required`; a legacy draft is `invalid` until every
scenario is confirmed or blocked), content_hash, validation_errors[], summary{scenarios, blocked_golden_tests,
evaluators_by_kind, cloud_evaluators, lambda_functions, iam_roles, role_grants,
unresolved_recommendations} | null, created_by, created_at, operation_id}`. An operation
is `{id, conversation_id, plan_id, plan_revision, plan_hash, proposal_revision,
approved_by, account_id, region, pinned{workspace_id, account_id, region, role_arn,
execution_role_arn, execution_role_id?}, status: queued|running|succeeded|partial|failed|
cleaning|cleaned, attempts, max_attempts, dataset_id, error, resources[{kind: dataset|
lambda_role|log_group|lambda_function|lambda_permission|role_grant|evaluator|existing,
key, plan_key?, name, status: pending|accepted|ready|failed|conflict|blocked|skipped|
retained|unknown|delete_pending|deleted|delete_failed, definition?, error, digest?, rules_digest?,
reference_dependent?, owned?, recovered?, review?, reviews?, attempts, result, cleanup?, link?}],
created_at, updated_at, running, requires_new_plan}`. `requires_new_plan` is derived
from the recorded Lambda packages without AWS calls. When true, the console replaces
retry with a replacement-plan action and withholds evaluation next steps. This action
copies the saved plan's exact `content` through `PUT …/evaluation-plan`, preserving
member edits, scenarios and the source proposal binding; it creates only a new draft.
Review and explicit materialization approval remain separate, and the old operation
and its resources remain historical. A `lambda_function` result carries `revision_id` (the
current baseline the worker fences its writes on), `initial_revision_id` and the immutable
allowlisted `create_response` (`State`, `StateReasonCode`, `LastModified`, `RevisionId`, …)
plus `request_id` as CreateFunction answered them, `settled_revision_id` once the first
initialization is settled, and `revision_history[]`; `review` is the review-required marker
(`kind`, `observed_revision_id`, `observed_last_modified`, `resolved_by?`) and `reviews[]`
the append-only audit entries — no CloudTrail actor or token is ever stored or returned.
`revision_history` also records `lambda_permission_added` after a successful, verified
permission write advances the published version's cleanup snapshot. It retains the
before/after configuration revisions and modification times, policy revisions and request ID; `$LATEST`
and original creation evidence remain unchanged. Historical mismatches and lost
write responses remain review-required rather than being silently adopted.
`POST /api/eval/runs` answers
`422 run.judge_needs_ground_truth` for a managed reference-driven code evaluator whose
scope lacks the reference (a simulated persona item reports `<scenario>/simulated turns
lacks expected_response` — its turns are generated at run time), `422
run.evaluator_unverifiable` when a selected custom evaluator cannot be read (unknown
needs are never treated as verified; nothing downstream runs) and `422
run.evaluator_not_found` when it does not exist in the workspace; Observability SCORE NOW answers
`422 observability.evaluator_needs_ground_truth` before any Evaluate call; online evaluation
refuses such evaluators (`…evaluator_unsupported`). A cloud/existing evaluator mapped to a
subset of golden tests is refused at plan validation (selection is global). `link` is the classic console
deep link (`/evaluation?view=datasets&ds=…` / `?view=evaluators&ev=…`); the V2 architect builds
its own link from the same ids (`/v2/eval/data?tab=datasets&view=dataset&id=…` /
`/v2/eval/evaluators?view=detail&id=…`). The ordinary
`DELETE /api/eval/evaluators/{id}` answers `409 evaluator.managed_by_operation`
(`detail.operation_id`) for an evaluator an operation owns. `GET /api/assistant/architect`
gained `can_materialize_evaluation_assets` (= the caller holds `agents.deploy`).

## Console MCP tool review API

A hosted MCP server (`spec.protocol == "mcp"`) is not served until its tool list
has been reviewed (design projects §15.6). Every time the deploy pipeline creates or
re-syncs the server's Gateway target, it lists the tools through the Gateway and
snapshots them as `[{name, description, input_schema}]`, sorted by name with the
`<target>___` prefix stripped. The snapshot's SHA-256 is compared with the last
**approved** list. A match opens the gate. A first deploy, a re-publish that changed
a tool, or a drift opens a review instead, and the target is deleted again, so agents
can neither call nor list the new tools. A server whose target predates tool review
gets its live list recorded as an approved `baseline` (by `system:baseline`) on its
next deploy, so only a later change needs a review.

The routes are workspace + project scoped, like every agent route. Reads take
`perm:project.use`, deciding takes the project key `perm:mcp.approve`, and the drift
check takes `perm:project.write`. The deployer of the server (the actor of its latest
deployment, else its creator) never decides its own review, administrators included:
`409 mcp_review.self_decide`. A server of another project, or one the viewer may not
see, answers `404 agent.not_found`. An agent that is not an MCP server answers
`409 agent.not_mcp_server`.

A review is `{id, agent_id, version, target_name, tools_hash, tool_count,
base_review_id, diff{added[], removed[], description_changed[], schema_changed[]},
hints[{tool, kind, detail}], status: reviewing|approved|rejected|superseded, source:
deploy|drift|baseline, requested_by, decided_by, decided_at, note, publish_status:
publishing|published|failed|null, publish_error, created_at}`. Fields to note:

- `diff` is against the last approved list, which is what agents saw.
- `hints` flag what a reviewer should read twice. `kind` is `dangerous_verb`, `write_name`, `missing_description`, `injection_phrase`, `url`, `cross_tool_reference` or `long_description`. Hints never reject anything by themselves.
- A newer list supersedes an older open review, so at most one is `reviewing`.

| Method | Path | Permission | Result |
|---|---|---|---|
| `GET` | `/api/mcp-servers/{agent_id}/reviews` | `perm:project.use` | `{agent_id, state: none\|reviewing\|approved\|rejected, open_review_id, approved_review_id, approved_hash, reviews[review]}`, newest first, without the tool lists. `state` is `reviewing` while a list waits, else the latest decision, and `none` before the first list |
| `GET` | `/api/mcp-servers/{agent_id}/reviews/{review_id}` | `perm:project.use` | The review plus `tools[]` and `base_tools[]` (the approved list it is compared with, `null` for a first review), for a side-by-side reading. `404 mcp_review.not_found` for a review of another server |
| `POST` | `/api/mcp-servers/{agent_id}/reviews/{review_id}/approve` | `perm:mcp.approve` | Body `{note? (≤ 2000)}` → the review detail. Approving re-creates the withheld Gateway target in the background, through the gate again: the server must still list exactly the approved tools. `publish_status` reports how that went. `409 mcp_review.not_open` when the review is already decided |
| `POST` | `/api/mcp-servers/{agent_id}/reviews/{review_id}/reject` | `perm:mcp.approve` | Body `{note?}` → the review detail. The target stays withheld until a new deploy is approved |
| `POST` | `/api/mcp-servers/{agent_id}/reviews/check` | `perm:project.write` | Re-syncs the live target and compares its tools with the approved list. Returns `{checked, drift, review_id?, reason?}` merged with the gate state above. A server can change its tools without a re-deploy. A difference opens a `drift` review and withholds the target. A list that cannot be read also withholds it (fail closed). `checked: false, reason: "no_target"` when no target is live |
| `POST` | `/api/mcp-servers/{agent_id}/health` | `perm:project.write` | One health check now, as the hourly `mcp.health` task runs it: re-point the target if it calls another endpoint than the server's current one, re-sync, compare with the approved list (a drift opens a review and withholds the target) → `{health, …gate state}`. An AWS or Gateway failure is the `failing` outcome, not an error; three in a row set `alert` |
| `POST` | `/api/mcp-servers/{agent_id}/rollback` | `perm:release.sign` when the server's live endpoint is pinned (a gated agent), else `perm:agents.deploy`; production-protected | Body `{version? (≤ 16)}`, default the newest approved version other than the one served → `202 {version, review_id, qualifier, live_version, health, operation}`. In the background the named endpoint `live` is pointed at that version, its approved tool list becomes the current one (a `rollback` review) and the target is re-pointed there, through the gate again; progress in `operation`. The next deploy un-pins the server back to `DEFAULT`. `409 mcp.rollback_unavailable` (no other version has an approved list; `detail.approved_versions`), `409 mcp.rollback_noop` (that version is already served), `409 mcp.operation_in_flight` (a rollback or scaffold still runs) |
| `POST` | `/api/mcp-servers/{agent_id}/policies/scaffold` | `perm:mcp.approve`; with `mode`, `infra+perm:mcp.approve` | Body `{dry_run?, mode?: LOG_ONLY\|ENFORCE, confirmation_name?, override_reason?, evidence_range?: 1h\|6h\|24h\|7d, acknowledged_gateway_ids[]?}`. Plans the server's Cedar pair on the policy engine attached to the workspace Gateway: `lp_mcp_<target>_unreviewed` forbids the tools of an open review, `lp_mcp_<target>_project` permits the approved tools (conditioned on the project when the workspace binds several). `dry_run` → `{plan}` with `create`/`update`/`delete`/`keep` steps; otherwise `202 {plan, …operation state}`, applied in the background. A `mode` switch after the policies is shared infrastructure; `ENFORCE` takes the evidence or a typed override like Governance's mode change. `409 mcp.scaffold_nothing_reviewed` (no tool list yet), `503 gateway.not_bootstrapped` |

Opening a review journals `mcp.review.deploy` / `.drift`, and a decision `mcp.review.approve` / `.reject`. The V3 agent page's MCP server panel shows
the gate, the open review with its diff and hints, and the approve and reject actions.

**Registry records of MCP servers.** `POST /api/registry/records/{record_id}/action`
with `approve` or `publish` on a record whose `descriptorType` is `MCP` also requires
`perm:mcp.approve` (`403 auth.permission_required`). When the record names a hosted
MCP server of the workspace (its `target_name`), that server's tool review must be
`approved` first: otherwise `409 mcp.review_pending` (`detail.agent_id`). Other
record types and actions keep their lifecycle unchanged.

## Console Gateway tool targets API

Gateway tool targets besides hosted MCP servers (projects §15.12, §15.13, §15.15,
`routers/gateway_lambda.py`, `services/gateway_tools.py`). Workspace + project routes; a
row of another project answers 404 like a missing one. Their tool lists go through the
same tool review as hosted MCP servers (the review shape above): decisions take
`perm:mcp.approve` and are never the creator's (`409 mcp_review.self_decide`).

- **Lambda targets** are created by the console. Creating or deleting one changes the
  workspace Gateway every project shares, so both are `infra+perm:agents.deploy`: the
  project's `agents.deploy` while the workspace binds one project, administrators only
  once it binds several — unless every project there is isolated with its Cedar rules in
  force (projects §10.6-6): then the project's own `agents.deploy` decides again, and
  another project's target answers 404 (`INFRA_LOWERED_WHEN_ISOLATED`, like the
  Connection-bound targets of `/api/identity/gateway-targets`). Both are
  production-protected.
- **Externally hosted targets** are MCP-server targets made by hand on the Gateway that
  no hosted server owns. Adopting one records its current list as the approved baseline
  (agents already calling it are not cut off), and the hourly `mcp.health` task compares
  later lists with it. A drift opens a review, but the target is never withheld or
  re-pointed: it is the customer's.

A Lambda target is `{id, kind: "lambda", gateway_id, target_id, target_name, lambda_arn,
description, tool_names[], status: reviewing|publishing|live|failed|rejected, error,
created_by, created_at, review{…gate state}}`, plus `tool_schema[]` on create.
A target another project shared with this one (projects §11.2, kind `gateway_target`)
is listed with `shared_from: {project_id, name, access: read|use}` — in both lists
(the external list keys it on `tool_id`); only its own project changes it.

| Method | Path | Permission | Result |
|---|---|---|---|
| `GET` | `/api/gateway-tools/lambda` | `perm:project.use` | `{targets[lambda target]}`, newest first |
| `POST` | `/api/gateway-tools/lambda` | `infra+perm:agents.deploy` | Body (`extra="forbid"`) `{lambda_arn, target_name (MCP target-name pattern, ≤ 100), tool_schema[{name, description, inputSchema}] (1–100), description? (≤ 500)}` → `201` the target with `tool_schema` and `review_id`. Nothing changes on the Gateway yet: the schema waits for review. On approval, in the background, the Gateway role's `launchpad-lambda-tools` inline policy is rewritten to grant `lambda:InvokeFunction` on exactly the workspace's Lambda-target functions (each function and its `:*` versions and aliases), then the `mcp.lambda` target is created with the approved schema inline. `422 lambda_tool.invalid_arn`, `422 lambda_tool.wrong_environment` (another account or region than the workspace's), `422 lambda_tool.function_missing`, `422 lambda_tool.invalid_schema`, `409 lambda_tool.target_name_taken`, `503 gateway.not_bootstrapped` |
| `DELETE` | `/api/gateway-tools/lambda/{target_id}` | `infra+perm:agents.deploy` | `{deleted}`. `target_id` is the console id (the Gateway target id works too). Deletes the Gateway target, then removes the function from the role's policy (the policy is deleted when no Lambda target remains), then the row. `409 lambda_tool.publishing` while it is being published, `404 gateway_tool.not_found` |
| `GET` | `/api/gateway-tools/external` | `perm:project.use` | `{targets[{target_id, target_name, endpoint, status, hosting: "external", tool_id, adopted_elsewhere, review, health}]}`, sorted by name. `tool_id`, `review` and `health` are set once this project adopted it; `adopted_elsewhere` is true when another project did |
| `POST` | `/api/gateway-tools/external/{target_id}/adopt` | `perm:mcp.approve`; `infra+perm:mcp.approve` in the handler | → `201 {tool_id, target_id, target_name, review}`, its current tool list recorded as the approved baseline. `404 gateway_tool.not_found`, `409 gateway_tool.not_external` (a hosted server's or a non-MCP target), `409 gateway_tool.already_adopted` |
| `GET` | `/api/gateway-tools/{tool_id}/reviews` | `perm:project.use` | `{agent_id, …gate state, reviews[review]}` for a Lambda target or an adopted target |
| `GET` | `/api/gateway-tools/{tool_id}/reviews/{review_id}` | `perm:project.use` | The review with `tools[]` and `base_tools[]`. `404 mcp_review.not_found` |
| `POST` | `/api/gateway-tools/{tool_id}/reviews/{review_id}/approve` | `perm:mcp.approve` | Body `{note?}` → the review detail. A Lambda target in `reviewing` or `failed` moves to `publishing` and is published in the background. An adopted target's drift is accepted (`publish_status: published`) |
| `POST` | `/api/gateway-tools/{tool_id}/reviews/{review_id}/reject` | `perm:mcp.approve` | Body `{note?}` → the review detail. A Lambda target becomes `rejected` |

## Console Registry API — live agent card

`GET /api/registry/records/{record_id}/live-agent-card` is the LIVE CARD read in
the Registry drawer's AGENT CARD block: the A2A card the runtime behind the record
serves *right now*, next to the card the record stored at deploy time. It is an
on-demand data-plane call (`GetAgentCard`), made only when the operator asks —
never on drawer open — and nothing is persisted.

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/registry/records/{record_id}/live-agent-card` | `{agent_id, runtime_arn, status_code, card, diff}` — `card` is the JSON document the runtime serves (`GetAgentCard.agentCard`, what an A2A client reads at `/.well-known/agent-card.json`), `status_code` is `GetAgentCard.statusCode`; `diff` = `{identical, fields[{field, record, live}], skills_only_in_live[], skills_only_in_record[]}` comparing `name`/`url`/`version`/`protocolVersion` and the skill id sets against the record's `descriptors.a2a.agentCard.inlineContent` (description, capabilities and the platform `metadata` block are not compared). The route resolves record → ledger agent (`Agent.registry_record_id`, same workspace, not deleted) → `Agent.arn` server-side; the browser never supplies an ARN. No `runtimeSessionId` is sent; the session AWS opens to serve the card is ended with `StopRuntimeSession` fail-soft (a stop failure is logged, the card is still returned) |

Error codes, all decided on the ledger before AWS is called:
`registry.record_not_deployed` (404, no Launchpad agent owns the record),
`registry.record_not_a2a` (409, the agent's `spec.protocol` is not `a2a`),
`registry.agent_not_ready` (409, the agent is not `active` or has no runtime ARN
yet). A data-plane `ClientError` maps to the standard 4xx envelope (`aws.not_found`,
`aws.access_denied`, `aws.throttled`, …); a runtime-side failure with no mapping
(`RuntimeClientError`) is `registry.live_card_failed` (502) with
`detail.aws_error_code` — never a bare 500. No IAM change: the console's role
already carries `bedrock-agentcore:*`.

## Console Registry API — consumer view

The Registry page shows the same registry from two sides. The **publisher list**
(`GET /api/registry/records`, control plane `ListRegistryRecords`) is what the
operator manages: every record in every state. The **consumer view**
(`?view=discoverable`) is what a consumer or agent with data-plane access actually
sees — the GA discovery API `ListDiscoverableRegistryRecords`. Records in the first
list but not in the second are the ones approval has not exposed; the console chips
them NOT DISCOVERABLE once both lists are known. Read-only; nothing is persisted.

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/registry/records/discoverable?type=` | `{records[{record_id, name, display_name, description, type, descriptor_types[], status, status_reason, version, created_at, updated_at}], count}` — data-plane `ListDiscoverableRegistryRecords(registryId=<workspace registry>, maxResults=100)` paginated to completion with `nextToken`; `type` (optional) narrows with `filters=[{name: "recordType", values: [<GA type>]}]` and accepts the platform (`A2A`/`MCP`/`AGENT_SKILLS`) or GA (`agent`/`mcp`/`skill`) name; `type` in the rows is always the platform name. Summaries never carry `descriptors` — read `GET /api/registry/records/{record_id}` for the payload. `count` = number of rows |

Error codes: `registry.bad_type` (422, unknown `type`), `registry.unavailable` (503,
the workspace has no registry). AWS `ClientError`s map to the standard 4xx envelope
(`aws.access_denied`, `aws.throttled`, …), never a bare 500. Route policy: MEMBER,
like the other registry reads.

## Console Governance API

These `/api` routes back the authenticated console. They are not part of the
public `/v1` agent invocation contract.

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/governance/gateways` | Live MCP Gateway inventory |
| `GET` | `/api/governance/gateways/{id}` | Targets (each with `kind: {protocol, variant}`), actions + `actions_uncovered_targets`, Registry, Engine, IAM, and attachability detail |
| `POST/DELETE` | `/api/governance/gateways/{id}/manage` | Add/remove only Launchpad management tags |
| `GET` | `/api/governance/gateways/{id}/registry-preview` | Gateway-level record diff and legacy matches |
| `POST` | `/api/governance/gateways/{id}/registry-import` | Create/reuse/update and submit; never approve |
| `POST` | `/api/governance/gateways/{id}/retire-legacy-records` | Explicit retirement after Gateway record approval |
| `POST` | `/api/governance/gateways/{id}/engine` | Create/adopt and attach an Engine in selected mode (`ENFORCE` default) |
| `GET/POST` | `/api/governance/gateways/{id}/policies` | List or create `LOG_ONLY` policies |
| `PUT` | `/api/governance/gateways/{id}/policies/{policy_id}` | Update LOG_ONLY or create an ACTIVE-policy candidate |
| `POST` | `/api/governance/gateways/{id}/policies/{policy_id}/promote` | Evidence-gated activation/cutover |
| `POST` | `/api/governance/gateways/{id}/policies/{policy_id}/rollback` | Audited snapshot/candidate rollback |
| `POST` | `/api/governance/gateways/{id}/mode` | Gateway `LOG_ONLY`/`ENFORCE` transition |
| `POST` | `/api/governance/gateways/{id}/generations` | Start NL → Cedar generation for review only |
| `GET` | `/api/governance/gateways/{id}/generations/{generation_id}` | Poll generation status and read draft assets |
| `GET` | `/api/governance/gateways/{id}/decisions` | AWS decision projection or explicit unavailable state |
| `GET` | `/api/governance/gateways/{id}/rate-limits` | `{rate_limits: [...]}` — every Gateway rate limit (all `nextToken` pages); works on any Gateway |
| `POST` | `/api/governance/gateways/{id}/rate-limits` | Create a rate limit → `201` with the created record; managed Gateways only |
| `PUT` | `/api/governance/gateways/{id}/rate-limits/{rate_limit_id}` | Replace `entries` (+ optional `description`); `dimensionKeys` are immutable → `422` |
| `DELETE` | `/api/governance/gateways/{id}/rate-limits/{rate_limit_id}` | Delete → `{deleted: true, id, status}` |
| `POST` | `/api/governance/gateways/{id}/targets/{target_id}/synchronize` | `SynchronizeGatewayTargets` for one dynamic MCP-server target → `202` with the target projection (`status` = `SYNCHRONIZING`, same `kind` as the detail); managed Gateways only (`409 governance.gateway_not_managed`); non-synchronizable target → `409 governance.target_not_synchronizable`, `detail.reason` ∈ `not_mcp_server`, `static_tool_schema`, `pending_auth`, `synchronizing`, `not_ready`; journaled as `target.synchronize` |
| `GET` | `/api/governance/gateways/{id}/audit` | Immutable local change journal |
| `GET` | `/api/governance/operations/{operation_id}` | Async operation status |

Every target in the gateway detail and in the synchronize response is the same
projection `{id, name, status, status_reasons, description, kind, listing_mode,
last_synchronized_at, synchronizable, not_synchronizable_reason}`. `kind` is
`{"protocol": "mcp" | "http" | "inference" | "unknown", "variant": <union key> |
null}` — the `TargetConfiguration` member AWS set (`mcp/lambda`, `mcp/mcpServer`,
`mcp/openApiSchema`, `http/passthrough`, `http/agentcoreRuntime`,
`inference/provider`, …); an empty configuration is `unknown`/`null` and an
unrecognized member is `protocol: <key>` / `variant: null`. The detail also carries
`actions_uncovered_targets: [name, …]` — the `http` / `inference` targets, which
have no tool schema and therefore never appear in `actions`.

Policy and Gateway mutations return `202`:

```json
{"operation": {"id": "...", "status": "pending", "operation": "policy_create"}}
```

The rate-limit routes are **synchronous** — no operation to poll. A rate limit
is `{id, gateway_id, description, dimension_keys, entries, status, created_at,
updated_at}` with `status` ∈ `CREATING | ACTIVE | UPDATING | DELETING`. Create
takes:

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

Update takes `entries` (replace semantics) and optional `description`. Validation
runs before any AWS call and answers `422 governance.rate_limit_invalid` with
`detail.reason` ∈ `dimension_keys_count | dimension_key_unknown |
dimension_key_duplicate | entries_count | entry_dimensions_mismatch |
entry_dimension_empty | wildcard_not_trailing | entry_no_metric |
rate_config_count | rate_out_of_range | period_not_allowed |
description_too_long | dimension_keys_immutable`: 1–10 keys from `targetName`,
`toolName`, `qualifiedModelId`, `$.context.jwt.<claim>`,
`$.context.iam.principal`, `$.context.iam.sourceIdentity`; 1–1000 entries whose
`dimensions` carry exactly the parent keys; `*` only in trailing positions; at
least one metric per entry; `rate` 0–10 000 000; `requests` per
`second`/`minute`, `tokens` per `minute` only, `connections` per `second` only;
description ≤ 512 chars. Mutations on an unmanaged Gateway answer `409
governance.gateway_not_managed`; a duplicate dimension-key set or a busy Gateway
is AWS `ConflictException` → `409 aws.conflict`. Every mutation is journaled in
the audit route as `rate_limit.create` / `rate_limit.update` /
`rate_limit.delete` (`before` = prior record or `{}`, `requested` = payload,
`after` = AWS response, status `succeeded`/`failed`).

Generation start returns
`{"operation": …, "generation_id": …, "status": …}`; a generated asset is only
a draft for the editor and never activates a policy.

Poll the operation route until `succeeded`, `failed`, `partial`, or
`interrupted`. `interrupted` means a restart could not prove the AWS effect and
the operation must be retried explicitly — the backend never replays it.
Mutation requests carry the live timestamps and confirmations that apply to the
operation:

```json
{
  "expected_gateway_updated_at": "2026-07-16T09:00:00+00:00",
  "expected_policy_updated_at": "2026-07-16T09:01:00+00:00",
  "acknowledged_gateway_ids": ["gw-a", "gw-b"],
  "confirmation_name": "finance-gateway",
  "override_reason": null
}
```

Common conflict codes are `governance.gateway_not_managed`,
`governance.concurrent_change`, `governance.shared_engine_changed`,
`governance.iam_preflight_failed`, `governance.evidence_required`,
`governance.policy_engine_deleted`, and
`governance.registry_record_not_approved`.

When a Gateway still references a Policy Engine that was deleted out-of-band,
reads report the reference with `policy_engine.missing = true` and
`status = "DELETED"` instead of failing, policy mutations answer
`409 governance.policy_engine_deleted`, and `POST .../engine` treats the
reference as unattached: it creates a new Engine, attaches it in the selected
mode, and records the replaced ARN on the operation.

## Console resource creators

Every list and detail response of a user-managed resource carries two nullable
fields recording who created it through the console:

```json
{"created_by": "3f2a…", "created_by_name": "ada"}
```

`created_by` is the creator's `users.id` — NULL for the config admin and the
auth-disabled operator (no `users` row); `created_by_name` is the username at
creation, display only. Both are NULL on rows that predate the field and on
resources created outside the console or by a path with no human principal
(Skill Lab sample task sets, the bootstrap memory, experiment/canary-owned online
configs, built-in and third-party evaluators). They are always server-set: a
`created_by` / `created_by_name` in a request body is ignored.

| Resource | Routes carrying the fields |
|---|---|
| Knowledge base | `GET` / `POST /api/knowledge-bases`, `GET` / `PATCH /api/knowledge-bases/{kb_id}` |
| Memory resource | `GET` / `POST /api/memory/resources`, `GET` / `PUT /{memory_id}`, `POST /{memory_id}/adopt` — an adopted memory carries the adopter's name with `created_by: null` |
| Dataset | `GET` / `POST /api/eval/datasets` (+ `upload`, `from-sessions`, `PUT`) — a pipeline run's new dataset records whoever started the run; an architect-materialized one the plan approver |
| Pipeline | `GET` / `POST /api/eval/pipelines`, `GET` / `PUT /{id}`, `POST /{id}/run` |
| Evaluation run | `GET` / `POST /api/eval/runs`, `GET /api/eval/runs/{id}`; online report rows (`GET /api/eval/online/{id}/reports`) |
| Online config | `GET` / `POST /api/eval/online`, `GET /api/eval/online/{config_id}` |
| Agent | `GET` / `POST /api/agents`, `GET /api/agents/{id}`, `POST /{id}/convert` (the converter creates the twin), conversions, `/api/system-agents` rows, the architect approval reply (the approver); discovered runtimes and presets carry `null` — `owner` stays a provenance label |
| Evaluator | `GET /api/eval/evaluators`, `GET /{id}`, `POST` (the create reply) — an architect-materialized one records the plan approver |
| Experiment / canary | `GET` / `POST /api/experiments`, `GET /{id}`; `GET` / `POST /api/runtime-canaries`, `GET /{id}` |
| Skill Lab | job and task-set list / detail / create, `POST /jobs/{id}/import-taskset` (the importer creates the set) |

## Workspace resource visibility

An admin chooses per workspace whether members see each other's resources:

| Method | Path | Role | Body / response |
|---|---|---|---|
| `GET` | `/api/workspaces/{workspace_id}/settings/visibility` | admin | `{"resource_visibility": "shared" \| "isolated"}` — `shared` when never set |
| `PUT` | `/api/workspaces/{workspace_id}/settings/visibility` | admin | body and response `{"resource_visibility": "shared" \| "isolated"}`; any other value → `422 validation.invalid_request`; a member → `403 auth.forbidden`; an unknown workspace → `404 workspace.not_found` |

Both target the path parameter (not `X-Workspace`) and work on a workspace whose
bootstrap has not finished. Every workspace row returned by `GET /api/workspaces`
and the admin workspace routes carries `resource_visibility`. A switch takes
effect on the next request and changes no data.

Under `isolated`, a **member** sees a resource of the kinds in the table above iff
its `created_by` is their own `users.id`, is `null` (creator unknown — legacy rows,
samples, the config admin, resources made outside the console), or is the
`users.id` of an account whose role is **currently** `admin`. Admins see
everything. Built-in / third-party evaluators and the bootstrap memory are always
visible; a cloud dataset follows the local datasets linked to it (visible when any
is; one with no local row is shared), and an experiment/canary-owned online config
(`exp_<id8>_…` / `can_<id8>_…`) follows its experiment or canary.

A hidden resource is absent everywhere, with the same answer as a missing id:

| Surface | Hidden behaves as |
|---|---|
| Lists and pickers | omitted — filtered before paging, so `GET /api/eval/runs` `total` and the 20-row experiment / canary lists count only visible rows; `GET /api/memory/resources` drops a hidden managed memory entirely (it is not listed as detected) |
| By-id routes | the kind's not-found: `kb.not_found`, `memory.not_managed`, `dataset.not_found` (local and cloud), `pipeline.not_found`, `run.not_found`, `online_eval.not_found` (incl. `/results`; a hidden report run → `online_eval.report_not_found`), `aws.not_found` (custom evaluator `GET`/`PUT`/`DELETE`), `experiment.not_found`, `canary.not_found`, `skill_lab.job_not_found`, `skill_lab.taskset_not_found` |
| Create-time references | as for a missing id: a run on a hidden dataset → `404 dataset.not_found`, with a hidden evaluator → `422 run.evaluator_not_found`; a hidden evaluator on an online config / experiment `gateway` / canary → `400 {online_eval,experiment,canary}.evaluator_unsupported`; SCORE NOW (`POST /api/observability/sessions/{id}/evaluate`) → `404 aws.not_found`; a pipeline output / from-sessions / experiment or canary `traffic` dataset → `404 dataset.not_found`; an experiment `recommend_source_run_id` → `404 experiment.recommend_source_not_found`; a canary `source_experiment_id` → `400 canary.source_experiment_invalid`; a Skill Lab job's task set → `404 skill_lab.taskset_not_found` |
| Aggregates | `GET /api/overview` `eval_pass_rate` / `eval_runs` and `GET /api/overview/online-quality` cover only visible runs / configs (the quality tile is cached per viewer) |
| Architect catalog | lists only the KBs the conversation's owner can see |

Internal references are untouched: a visible resource that references a hidden
one (an admin's experiment replaying a member's dataset, a pipeline appending to
its configured dataset) keeps working.

**Agents** follow the same rule on the agent's `created_by` (discovered runtimes and
system presets are creator-unknown, hence shared):

| Surface | Hidden agent behaves as |
|---|---|
| Lists and pickers | omitted from `GET /api/agents` (every console picker), conversion twins and `GET /v1/agents` (for a member-minted key) |
| By-id routes | `404 agent.not_found` on `/api/agents/{id}` (+ `conversions`, `versions`, `identity`, `redeploy`, `inbound-auth`, `convert`, `invoke`, `DELETE`), `/api/chat/{id}/*` and `/v1/agents/{id}/invoke[-stream]`; `400 agent.not_active` on `POST /api/eval/runs`, `POST /api/eval/online`, `GET /api/eval/agents/{id}/log-streams`, `GET /api/experiments/readiness`, `POST /api/experiments`; `400 canary.agent_not_active` on `POST /api/runtime-canaries`; `404 registry.a2a_demo_agent`; `404 registry.record_not_deployed` on `live-agent-card`; `404 job.not_found` on its deploy jobs |
| Cross-module mutations | `404 agent.not_found` when accepting a recommendation of a visible run on a hidden agent, and on every experiment / canary action except `verdict` / `cleanup`; such a run's `recommendation-inputs` read as `source: "manual"` (the hidden agent's prompt / tools are not disclosed) |
| Agent-spec pins | create / convert: every pinned KB / memory must be visible (`404 kb.not_found`, `422 agent.memory_not_managed`); redeploy / inbound-auth check only the pins **added** to the stored spec |
| Registry | its A2A record is dropped from `GET /api/registry/records`, `/search`, `/discoverable`; `GET` / `PUT` / `DELETE` / `action` / `reimport` on it → `404 aws.not_found` |
| Observability | its traces / sessions are dropped from the lists; trace / session detail, `transcript` and `evaluate` → `404 observability.{trace,session}_not_found`; `/dashboard` stays a workspace total |
| Memory console | its actors (`<agent>__<human>`) are dropped from `/api/memory/actors` and extraction jobs; `sessions` / `events` / `namespaces` / `records` / `records/search` keyed by such an actor or its namespace → `404 memory.actor_not_found`; the shared `default` actor loses sessions the ledger attributes to it |
| Derived names | dropped from KB `attached_agents` (`attached_agent_count` keeps the true count) and the `kb.has_attached_agents` 409, memory-resource `agents` and the `memory.in_use` 409, Connections `referenced_by` (incl. the `identity.connection_referenced` 409), online `matched_agent`; discovery `managed_agent_id` / `managed_agent_name` and import `already_managed` read `null` with `managed: true` |
| Aggregates | `GET /api/overview` `active_sessions` and the A2A `registry_assets.agents` count only visible agents |
| Name uniqueness | workspace-wide; the `409 agent.name_exists` `detail.agent_id` is `null` when the holder is hidden |

KB force-delete still strips the KB from every mounting agent.

## Console Models API

The platform's own model roles (see [setup.md](setup.md#choosing-models)).
Hub-global — not workspace-scoped; overrides live in this deployment's ledger
(`platform_settings`) and apply at once, with no restart.

| Method | Path | Role | Body / response |
|---|---|---|---|
| `GET` | `/api/model-defaults` | member | `{"roles": [{role, model_id, default, overridden, constraint, allow_empty, needs_tools, updated_by, updated_at}]}` — one row per role; `model_id` is the effective value (console override > `LAUNCHPAD_*` env > `launchpad.yaml` > built-in), `default` what a reset returns to, `constraint` the accepted model families (`["anthropic"]`, …; `null` = any) |
| `PUT` | `/api/admin/models/{role}` | admin | body `{"model_id": "<id>"}` (`""` only where `allow_empty`); response: the role's row. `404 models.unknown_role`; `400 models.invalid_id` / `models.empty` / `models.wrong_family` / `models.no_tool_use` |
| `DELETE` | `/api/admin/models/{role}` | admin | Removes the override (idempotent); response: the role's row, back on its default. `404 models.unknown_role` |
| `POST` | `/api/admin/models/probe` | admin | body `{"model_id": "<id>", "check": "converse" \| "tool_use"}` — one small real Converse call (with a tool definition for `tool_use`) in the hub's default workspace; creates nothing. Response `{ok, model_id, check, region, latency_ms, stop_reason}` or, on an AWS error, `{ok: false, …, error_code, message}` (still `200`). `400 models.invalid_id` |

A member gets `403 auth.forbidden` on the three admin routes.

## Console Costs and Alerts API

Estimated spend and threshold alert rules (the V2 page `/v2/costs`). Every route is
**admin**; a member gets `403 auth.forbidden`. Workspace-scoped: spend is read from the
workspace's account and region, and rules live in the ledger's `alert_rules` table.

Spend is span tokens × the price map (`model_prices`, see [setup.md](setup.md#choosing-models)),
so every figure is an estimate. A model missing from the map adds tokens but no dollars and
is named in `unpriced_models`. Spans of **every** runtime in the account and region count:
a service that matches no agent in this console has `known: false` (another stack on the
same AWS account, or a deleted agent) and is still in the total. Spend by person joins the
span `session.id` to the console's Chat sessions; `/v1` and evaluation traffic have none
and land in the unattributed row (`actor: "—"`, `attributed: false`). Results are cached
for 60 s per workspace and range; `force=true` bypasses the cache.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/costs` | `?range=1h\|6h\|24h\|7d\|30d` (default `24h`) `&force=` → `{range, hours, generated_at, total_est_cost_usd, by_agent[{service, agent_id, agent_name, display_name, known, tokens, llm_calls, sessions, est_cost_usd}], by_actor[{actor, attributed, sessions, tokens, est_cost_usd}], unpriced_models[], estimate, cache}`. Another range → `422 costs.bad_range`; a Logs Insights failure or timeout → `502 observability.query_failed` |
| `GET` | `/api/costs/month-to-date` | `?force=` → `{workspace_id, est_cost_usd, hours, unpriced_models[], estimate: true}` — spend since 00:00 UTC on the 1st, over at most 720 h |
| `GET` | `/api/alerts` | Ledger read, no AWS call → `{rules[], firing, kinds[]}` |
| `POST` | `/api/alerts` | body `{kind, threshold, name?, comparison?, window?, enabled?, webhook_url?}` → `201` rule. `comparison` defaults to the kind's natural direction |
| `PATCH` | `/api/alerts/{id}` | body: any of `{name, threshold, window, enabled, webhook_url}`; only the fields sent change, and `webhook_url: null` or `""` removes the webhook. `kind` and `comparison` are fixed. Unknown id or another workspace's rule → `404 alert.not_found` |
| `DELETE` | `/api/alerts/{id}` | `{deleted: id}`; `404 alert.not_found` |
| `POST` | `/api/alerts/evaluate` | Re-reads every enabled rule now and records the state → `{workspace_id, evaluated_at, rules[], firing, unknown}`. **Never notifies**, whatever the query string |

A rule: `{id, kind, name, comparison, threshold, window, enabled, has_webhook, state, last_value,
last_detail, last_checked_at, last_fired_at, last_notified_at, created_by, created_by_name,
created_at, updated_at}`. The webhook URL is write-only — responses carry `has_webhook`.
`state` is `ok`, `firing` or `unknown`; a value that cannot be read (no traffic, a failed
query) is `unknown`, never `ok`. `last_fired_at` is when the rule last *started* firing
(the current or last episode); `last_notified_at` is the last delivery attempt.

| `kind` | Value | Direction | Window |
|---|---|---|---|
| `error_rate` | failed traces / traces, 0–1 | above | the rule's `window`, `1h`–`7d` |
| `latency_p95_ms` | p95 trace latency, ms | above | the rule's `window`, `1h`–`7d` |
| `online_quality` | online-evaluation mean, 0–1 | below | fixed 24 h (`window` ignored) |
| `spend_usd` | estimated spend, USD | above | the rule's `window`, `1h`–`30d` |
| `cost_mtd_usd` | estimated spend since the UTC month start, USD | above | the month (`window` ignored) |

Validation errors (`400`): `alert.unknown_kind`, `alert.bad_comparison`,
`alert.comparison_never_fires` (the inverse direction), `alert.bad_window`,
`alert.bad_threshold` (negative or not finite, or `error_rate` / `online_quality` outside
0–1 — an error rate is a fraction, `0.05` = 5 %).

**Webhooks.** One generic https JSON webhook per rule; a Slack or Feishu incoming webhook
renders its `text`. The body is `{text, workspace_id, rule_id, kind, value, threshold,
comparison, detail}`. It is posted only by the background check (the DLC scheduler's
60-second tick, `app/dlc/scheduler.py`, evaluates alert rules every 5 minutes under a
`scheduler_claims` row, so one backend process runs each pass), once per firing episode (a transition into `firing`); the outcome is appended
to `last_detail`. A manual re-check that sees the transition first posts nothing, and the
next background pass still sends that episode's notification — as it does when a webhook is
added to a rule that is already firing. The URL is checked on save and again on every send:
`alert.webhook_not_https`, `alert.webhook_credentials` (user or password in the URL),
`alert.webhook_invalid`, `alert.webhook_unresolvable`, `alert.webhook_non_public` (any
resolved address is private, loopback, link-local, reserved, multicast or unspecified,
IPv4-mapped IPv6 included). The request goes to the address that was checked, redirects are
not followed, the timeout is 5 s, and the response body is never read.

## Console Knowledge Bases API

`/api/knowledge-bases/*` backs the Knowledge Bases console (console 04) over
Bedrock *managed* knowledge bases — `bedrock-agent` for the control plane,
`bedrock-agent-runtime` for retrieval. Only `type == "MANAGED"` KBs are
addressable: a VECTOR KB in the same account answers `kb.not_found`. Only the
creator of a console-created KB is stored locally (see *Resource creators* below);
everything else is a live AWS call. See
[architecture.md](architecture.md#managed-knowledge-bases-console-04).

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/knowledge-bases?status=` | Every MANAGED KB with `{kb_id, name, description, status, updated_at, data_source_count, attached_agents}`; `status` is an optional exact-match filter applied after the read (e.g. `ACTIVE`) |
| `POST` | `/api/knowledge-bases` | `202` — `CreateKnowledgeBase` (`{name, description?, source: {mode: "upload"\|"existing", bucket?, prefix?}}`) returns the detail while it is still `CREATING`, plus `source_pending`; the data source is created off-request by a backend thread once the KB is `ACTIVE` (1.5–3 min), so the client polls `GET /{kb_id}` |
| `GET` | `/api/knowledge-bases/{kb_id}` | Detail: status, ARN, timestamps, `failure_reasons`, `attached_agents`, and each data source with bucket/prefix, status and its 10 most recent ingestion jobs |
| `PATCH` | `/api/knowledge-bases/{kb_id}` | `{description}` (≤1000 chars) → `UpdateKnowledgeBase` with the name, role and configuration read back unchanged; answers the fresh detail |
| `DELETE` | `/api/knowledge-bases/{kb_id}?force=` | Deletes data sources, the per-KB gateway `Retrieve` target and the per-KB inline S3 policy, then `DeleteKnowledgeBase`. `409 kb.has_attached_agents` while agents mount it; `force=true` strips it from every mounted agent's spec (and re-syncs the harness agents' agentic targets) first |
| `POST` | `/api/knowledge-bases/{kb_id}/files` | `multipart/form-data`, one or more parts named `files` (or `file`) → `{keys}` in the artifacts bucket under `kb/{kb_id}/`. Allowed while the data source does not exist yet; `409 kb.no_upload_target` for a KB whose sources are all elsewhere |
| `POST` | `/api/knowledge-bases/{kb_id}/data-sources` | `201` — creates a `MANAGED_KNOWLEDGE_BASE_CONNECTOR` source from the same `{mode, bucket?, prefix?}` body and answers the fresh detail. Idempotent per S3 location: an existing connector on the same bucket/prefix is returned instead of a second one. This is also the manual repair for a KB left with no data source |
| `DELETE` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}` | `DeleteDataSource` → `{deleted, ds_id}` (deletion is asynchronous on the AWS side) |
| `POST` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync` | `StartIngestionJob` → the job projection `{job_id, status, started_at, updated_at, statistics, failure_reasons}` |
| `GET` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/ingestion-jobs` | The 50 most recent ingestion jobs, newest first, in the same projection |
| `GET` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/documents?page_size=&token=` | One page of `ListKnowledgeBaseDocuments` (`page_size` 1–100, default 50) as `{documents, next_token, page_size}`; each document carries the KB-side `status`/`status_reason`/`indexed_at` plus S3-side `size_bytes`/`uploaded_at` joined by object key (absent when the backend cannot list the bucket) |
| `POST` | `/api/knowledge-bases/{kb_id}/query` | Retrieval playground — `{text, number_of_results?}` (1–100, default 8) → `Retrieve` with a `managedSearchConfiguration`, answering `{results}` of `{text, score, location_uri, metadata}` |
| `POST` | `/api/knowledge-bases/{kb_id}/migrate-to-project` | Admin (data). An isolated project's existing KB moves onto the project's own role, S3 prefix (`projects/<pid>/kb/<kb_id>/`) and tag: the uploads are copied, the role switched (`UpdateKnowledgeBase`), the KB tagged, and a data source on the new prefix created and synced → `{kb_id, project_id, role_arn, prefix, copied, old_data_sources[], data_source, sync?, sync_error?, state: syncing\|done}`. Idempotent and additive: the old data source keeps serving until `finish`. A KB of another project → `404 kb.not_found`; a project that is not isolated → `409 project.isolation_disabled`; a service that refuses the role change → `409 kb.role_update_unsupported` (rebuild the KB in the project) |
| `POST` | `/api/knowledge-bases/{kb_id}/migrate-to-project/finish` | Admin (data). Once the new data source's latest sync is `COMPLETE`: deletes the old `kb/<kb_id>/` data source and the shared role's inline policy for the KB → `{kb_id, deleted_data_sources[], state: done}`. The old objects stay. `409 kb.not_migrated` (migrate first), `409 kb.migration_syncing` (wait for the sync) |
| `GET` | `/api/knowledge-bases/source?uri=` | A presigned GET (5 min) for a document a retrieval result cites — `uri` is its S3 address (`s3://…` or the `https://<bucket>.s3.<region>.amazonaws.com/…` form `Retrieve` returns). Signed only when the object sits inside a data source of a managed KB the caller can see; answers `{url, name, kb_id, kb_name, expires_in}`, text sources served as `text/plain`. The console's chat links and playground source labels open through it |
| `POST` | `/api/knowledge-bases/ensure-gateway` | Create-if-missing the shared `launchpad-kb-gw` MCP gateway and persist `{id, arn, url}` onto the workspace. Idempotent; the harness deploy path calls the same helper, so this is only needed to provision the gateway ahead of time |

Error codes: `kb.not_found` (404 — unknown id, or a KB that is not MANAGED),
`kb.ds_not_found` (404), `kb.has_attached_agents` (409, with the blocking names
in `detail.agents`), `kb.delete_conflict` (409 — the KB is still `CREATING`),
`kb.no_upload_target` (409), `kb.no_files` (400 — no upload part in the form),
`kb.source_unsupported` (400 — not an S3 address), `kb.source_not_found` (404 — no
visible KB's data source holds it), `kb.source_missing` (404 — the object is gone),
`kb.source_unreadable` (502 — the backend cannot read the object),
`kb.sync_not_ready` (409 — `StartIngestionJob` hit `ValidationException` or
`ConflictException`: the data source is still provisioning, or a sync is already
running), `kb.bucket_required` / `kb.invalid_bucket` / `kb.invalid_prefix` /
`kb.invalid_source` (400 — source validation), `kb.query_failed` (502 —
retrieval failed on the KB side, e.g. the index is still building). Any other
AWS `ClientError` goes through the global mapping above (`aws.validation`,
`aws.conflict`, …). A workspace whose resource map has no `kb_role_arn` (create)
or no `artifacts_bucket` (uploads) has not been bootstrapped and raises a `500`
naming the missing key.

## Console Context Layer API (COA)

`/api/context-layer/*` connects a workspace to one COA (Context Ontology
Accelerator) deployment and reads COA **as the signed-in member**. The ledger
`context_layers` row holds identifiers only; runtime status, the callback
registration and agent OAuth are read live. No COA token, client secret or
consent session uri is ever in a response; an authorization URL goes only to the
member it was minted for. See [identity.md](identity.md) §7.7.

| Method | Path | Policy | Result |
|---|---|---|---|
| `GET` | `/api/context-layer` | member | `{connected, can_manage}`, plus when connected: `{name, prefix, env, region, console_url, api_endpoint, issuer, mcp_runtime_arn, serve_runtime_arn, connection, console_workload, runtimes: {mcp, serve}, ready, agent_oauth_enabled, callback_registered, authorization: {status: none\|pending\|authorized\|revoked, authorized_at, identity?}, demo_account: {available, username}, sign_out_url, referenced_by, gateway: {enabled, gateway_id}, probe_errors}`. `gateway` is the managed COA gateway from the ledger only (its live state is `GET …/gateway`). `authorization.identity` is present while authorized: `demo` \| `own` \| `null` (unknown), the vaulted token's `sub` compared with the demo account's (one vault read when not cached, which never starts a consent). `sign_out_url` is COA's web-client `/logout` (null before a re-connect discovered the web client). Administrators also get `callback_url`, `agent_client_id`, `user_pool_id`, `callback_context_key` (`agentOauthCallbackUrls`) and `demo_account_param` (the parameter name). A live probe that failed reads `null` and is listed in `probe_errors`; the demo account's password is never in it |
| `PUT` | `/api/context-layer` | admin | `{prefix, env="dev", region, connection?, demo_account_param?}` → discover from SSM `/<prefix>/*` + the `<prefix>-<env>-{api,web,auth}` stack outputs (COA region), check both runtimes are READY, create the OAuth2 Connection `coa-<prefix>` (or adopt `connection` / an existing `coa-<prefix>` whose client id is the COA agent client), ensure the console workload identity `launchpad-console-<workspace>`, upsert the row. `demo_account_param` names the shared COA demo account's SSM SecureString: it must be under `/<prefix>/launchpad/` (`422 context_layer.demo_param_scope`, before any AWS call) and parse to `{username, password}` (`409 context_layer.demo_param_invalid`, `detail.problem` = `not_found` \| `not_secure_string` \| `bad_shape`, never the value); `""` clears it, omitted keeps the current one. Answers the status |
| `DELETE` | `/api/context-layer` | admin | Forget the layer → `{disconnected, connection, console_workload}`; the Connection and the workload identity stay. `409 context_layer.referenced` (with `detail.referenced_by`) while an agent attaches it, `409 context_layer.gateway_enabled` while the managed COA gateway exists |
| `POST` | `/api/context-layer/authorize` | member | Body `{account?: demo\|own}` (optional). The caller's consent: `{authorized: true, connection}` when the vault holds their COA token, else `{authorized: false, authorization_url, connection}` (single-use; `/auth/return` binds it). `account: "demo"` is refused without a demo account (`404 context_layer.demo_unavailable`) and logged. The consent request is the same for every choice and never carries `customParameters` (the token vault keys a token by them, so a consent made with any would be invisible to every reader): nothing is prefilled at COA's login page, and a browser signed in to COA consents as that session's user |
| `GET` | `/api/context-layer/demo-account` | member | `{available, username, connection}` (all null/false when nothing is connected): the ledger plus the cached parameter, no other AWS call. The Chat consent card matches its ask's provider against `connection` |
| `POST` | `/api/context-layer/demo-account/reveal` | member | `{username, password}` with `Cache-Control: no-store`; logged with the Launchpad username, never the password. `404 context_layer.demo_unavailable` when none is configured, `409 context_layer.not_connected` when nothing is connected |
| `POST` | `/api/context-layer/revoke` | member | Force re-consent for the caller on the layer's Connection (`user_token_revocations`) → `{revoked, provider, agents}` |
| `GET` | `/api/context-layer/namespaces` | member | COA `GET /namespaces` as the caller → `{namespaces: [{namespace_id, name, display_name, description, owner, status, source_count, created_at, updated_at}]}` |
| `GET` | `/api/context-layer/namespaces/{namespace_id}` | member | COA `GET /namespaces/{id}` as the caller → `{namespace: {…the same fields}}`. `namespace_id` must be a UUID (422 otherwise; COA is never called) |
| `GET` | `/api/context-layer/namespaces/{namespace_id}/sources` | member | COA `GET …/sources` (every page) → `{namespace_id, sources: [{source_id, name, source_type, source_subtype, status, tables_discovered, created_at, updated_at}]}`. COA's epoch times read back as RFC 3339; statuses are COA's, verbatim |
| `GET` | `/api/context-layer/namespaces/{namespace_id}/metrics` | member | COA `GET …/metrics` (every page) → `{namespace_id, metrics: [{name, description, unit, return_type, default_time_grain, synonyms, dialects: [{dialect, expression}], source_table, data_source_id}]}`: only what COA returns to the caller |
| `GET` | `/api/context-layer/namespaces/{namespace_id}/ontology` | member | COA `GET …/ontologies` plus one `GET …/graph/ontology-overview?ontology_id=` per ontology (at most 8, 200 classes each) → `{namespace_id, ontologies: [{ontology_id, title, description, ontology_type, status, class_count, property_count, updated_at}], classes: [{uri, label, description, ontology_id, ontology}], failed: [ontology_id], truncated}`. An overview COA could not answer is listed in `failed`; the rest still show |
| `GET` | `/api/context-layer/health` | member | `{runtimes: {mcp, serve}, ready, authorization_required, coa: {status, components: [{key, status}]} \| null, coa_error: {code, status} \| null, probe_errors}`: `GetAgentRuntime` in the COA region plus COA `GET /system-health` as the caller. Without consent COA is not called (`authorization_required: true`) |
| `POST` | `/api/context-layer/playground` | member | `{namespace_id, query (≤ 4000), session_id?, mode?: standard\|deep-reasoning, strategy?: best\|ontop\|nl_to_sql\|ontop_first\|nl_to_sql_first\|deep-reasoning}` → **SSE**. Proxies COA's Serve runtime (`stream: true`) as the caller. Events: `step {name, status, duration_ms, tool, detail}`, `token {text}`, `done {answer, tier, confidence, rationale, columns, rows (≤ 200), row_count, rows_truncated, partial, guardrail_blocked, data_sources, citations, total_ms, session_id}`, `error {code, message, status, coa_error}`, and `auth_required {provider, tool: "context_layer", url, scopes, agent_id: ""}` when the caller has not consented (then the stream ends). `: keep-alive` comments while COA is silent. `session_id` from `done` continues COA's conversation. Not connected, a vault failure or a bad body answer as ordinary errors before any stream |
| `GET` | `/api/context-layer/gateway` | member | The managed COA gateway `launchpad-coa-gw` (gateway mode), read back from AWS: `{enabled, state: ready\|creating\|failed\|missing\|disabled\|unknown, usable, gateway_name, gateway_id, gateway_url, gateway_status, target_status, status_reasons, tools: [name], protocol_versions, return_url_ok, trusts_workspace_users, issues: [protocol\|authorizer\|endpoint\|connection\|return_url\|tools], agents: [{id, name, method}], probe_errors}`. `missing` = the ledger names a gateway or target AWS no longer has; `usable` = `ready` with no `issues`. Never a secret or the tool schema body |
| `PUT` | `/api/context-layer/gateway` | admin | Create or repair the managed gateway (idempotent, resumable; can take minutes): COA's `tools/list` read with the caller's console COA token (`409 context_layer.authorization_required` first, nothing created), the gateway (MCP `2025-11-25` + `2025-03-26`, CUSTOM_JWT on the workspace pool with the console + M2M clients, the workspace gateway role), the console return URL on the gateway's workload identity, and the target `coa` (MCP server at COA's MCP runtime, inline tool schema, `OAUTH` `AUTHORIZATION_CODE` on the layer's Connection). `409 context_layer.gateway_prerequisites` (`detail.missing`) on a workspace without the bootstrap resources; an AWS failure is `502 context_layer.gateway_failed` (`detail.step`, `code` or `status` + `reasons`). Answers the status |
| `POST` | `/api/context-layer/gateway/refresh` | admin | Re-read COA's tools (as the caller) and update the target's inline schema when it changed (also re-pointing it at the layer's current MCP runtime and Connection). `409 context_layer.gateway_not_enabled` without a gateway. Answers the status |
| `DELETE` | `/api/context-layer/gateway` | admin | Delete the target, then the gateway, clear the ledger key and drop the grant and pending rows keyed `gw:<gateway_id>` → `{disabled, gateway_id, grants_removed}`. `409 context_layer.gateway_referenced` (`detail.referenced_by`) while an agent uses gateway mode |

Every non-GET route is `FREE` in the billing table, the playground included: COA's
own spend is in COA's account and is not metered by Launchpad.

`AgentSpec.context_layer` = `{namespace_id (UUID), namespace_name?, via?}` attaches one
namespace to a `harness` or HTTP `zip_runtime` agent. Any other method, protocol
`a2a`, `code`/`code_bundle`, or a tool named `coa` answers `422
validation.invalid_request` with an error row of type
`context_layer.unsupported_method`; a create or redeploy while no layer is
connected answers `409 context_layer.not_connected`. `via` is `"direct"` (the
default, never stored or echoed) or `"gateway"`: the agent reaches COA through the
workspace's managed COA gateway as the asking member. A create or redeploy with
`via: "gateway"` while the gateway is not enabled answers `409
context_layer.gateway_unavailable` (the deploy's generate stage checks again). Both
methods support it: a Harness attaches the gateway, and a `zip_runtime` agent bakes its
URL into `main.py` (`CONTEXT_GATEWAY`). A COA-attached agent on `/v1`
answers `409 identity.as_user_requires_console` (an `error` event on the stream).
`POST /api/agents/{id}/invoke` on a COA-attached agent whose caller has not
consented yet answers `409 context_layer.authorization_required` (it has no consent
card); Chat shows its `auth_required` card instead.

Error codes: `context_layer.not_connected` (409), `context_layer.authorization_required`
(409, `detail.authorization_url`), `context_layer.forbidden` (403 — COA refused: no
grant), `context_layer.reauthorize` (409 — COA rejected the token; the next fetch
forces consent. Not 401, which would end the console session),
`context_layer.upstream` (502 / 504 — COA 429, 5xx or timeout),
`context_layer.coa_not_found` (404 — COA has no such namespace or item, or hides it
from the caller), `context_layer.invalid_namespace` (422 — not a UUID),
`context_layer.referenced`
(409), `context_layer.unsupported_method` (422 — also an evaluation replay or a
conversion of a COA-attached agent), `context_layer.gateway_unavailable` (409 — a
gateway-mode agent with no managed gateway to attach, a deployed configuration
without it, or a gateway that could not be verified for the turn), `context_layer.not_found` (404 — no SSM
parameters or stack), `context_layer.agent_oauth_disabled` (409),
`context_layer.cross_account` (422), `context_layer.runtime_not_ready` (409),
`context_layer.discovery_failed` (502), `context_layer.connection_mismatch` (422),
`context_layer.connection_not_found` (404), `context_layer.identity_failed` (502 —
the token vault refused; the detail names the step and the AWS code only),
`context_layer.invalid_request` (422). A COA refusal's `detail` may carry
`coa_message` (COA's own message, at most 200 characters) and `status`. The
playground's `error` events add `context_layer.no_result` (COA found no answer) and
`context_layer.coa_rejected` (another 4xx, e.g. an ambiguous question).

## Console Memory API

`/api/memory/*` backs the read-only Memory console (console 05) over the shared
`launchpad_memory` singleton. Every console route is a read: there is no endpoint
that writes events, deletes records or triggers extraction. The one mutating
surface — the `/api/memory/resources*` routes below, which manage the memory
*resources* themselves — lives in a separate router (`routers/memory_resources.py`).
See [architecture.md](architecture.md#the-memory-console-console-05).

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/memory/overview` | Resource config, long-term strategies, bounded actor count, sibling memories |
| `GET` | `/api/memory/actors` | Actors with the compound `<agent_id>__<human>` id decoded and the agent name resolved; `agent_deleted` marks a deleted agent's partition (sorted last, so a re-created namesake reads first). Evaluation scenario actors (`<agent>__eval-<run>-<n>`) are left out; up to 5 AWS pages are read to fill one page |
| `GET` | `/api/memory/sessions?actor_id=` | Sessions for one actor, joined to the ChatSession ledger when the console wrote them |
| `GET` | `/api/memory/events?actor_id=&session_id=` | Short-term events; each payload entry is `kind` `conversational` (role + full text), `json` (the JSON value serialized losslessly into `text` — `null`, `false`, `0` and `""` included) or `blob` (byte count only); unknown kinds are omitted |
| `GET` | `/api/memory/namespaces?actor_id=` | Strategy namespace templates with `{actorId}` substituted; trailing `{sessionId}` segments collapse into an actor-level prefix (`prefix: true`), a placeholder elsewhere yields `resolvable: false` |
| `GET` | `/api/memory/records?actor_id=&strategy_id=` or `?namespace=` | Long-term records for the resolved namespace |
| `POST` | `/api/memory/records/search` | Semantic retrieval (`{query, actor_id, strategy_id?, namespace?, top_k}`) with relevance scores |
| `GET` | `/api/memory/extraction-jobs` | Failed (retry-eligible) extraction jobs, filterable by `actor_id`/`session_id`/`strategy_id`/`status` — **not surfaced in the console**; AWS's `status` enum is `FAILED` only, so a healthy resource returns an empty list |

Memory resource management (`?view=resources`):

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/memory/resources` | Every memory in the workspace's account/region, default first, each with the live agents whose spec pins it and `managed` (see below) |
| `POST` | `/api/memory/resources` | `perm:memory.manage`. `CreateMemory` (`{name, description?, event_expiry_days?, strategies?, namespace_keys?}`) → `201` with the detail projection in `CREATING` state; the new id is registered as managed |
| `GET` | `/api/memory/resources/{memory_id}` | Detail projection: description, status, event expiry, execution role, strategies, namespace keys |
| `POST` | `/api/memory/resources/{memory_id}/adopt` | **admin**. Registers an existing account memory as managed by the workspace (after `GetMemory` resolves it — unknown id → `404 aws.not_found`); idempotent; replies with the detail projection |
| `PUT` | `/api/memory/resources/{memory_id}` | `perm:memory.manage`. `UpdateMemory` limited to `{description?, event_expiry_days?}` — at least one required (422 otherwise), `description` 1–4096 chars (it can be replaced, not cleared), `event_expiry_days` 7–365 (422 outside). Sends exactly `memoryId` + the given fields and never `namespaceKeys` (the API replaces that set wholesale); the reply is the detail projection read back with `GetMemory`. Not blocked by referencing agents or the platform default; unknown id → `404 aws.not_found` |
| `DELETE` | `/api/memory/resources/{memory_id}` | `perm:memory.manage`. `DeleteMemory` (irreversible); `409 memory.platform_protected` for the workspace default, `409 memory.in_use` (with the agents) while a live agent's spec pins it; the managed registration is dropped |

**Ownership.** The account can hold memories the platform never created, so a
memory is *managed* only when it is the workspace's bootstrap memory or a
`managed_memories` ledger row names it (written by `POST` above or by an
administrator's adopt — never from a client payload). Every per-id route answers
`404 memory.not_managed` for any other id, before any AWS call; the list still
shows such memories with `managed: false`. A spec's `memory.memory_id` must be
managed and `ACTIVE`: `POST /api/agents`, re-publish and convert refuse it with
`422 agent.memory_not_managed` / `409 agent.memory_not_active`, and the deploy job
re-checks before any stage. The Chat memory rail answers
`409 agent.memory_not_managed` for a legacy spec pinning an unmanaged id.

Every list route accepts and returns `next_token` (AWS pages at 100 items) and
accepts `max_results` (clamped to 100) — nothing is capped silently. Namespace
resolution order on `/records` and `/records/search`: an explicit `namespace`
wins, otherwise it derives from `actor_id` (+ optional `strategy_id`).

Error codes: `memory.not_configured` (409, bootstrap has not run — except
`/overview`, which instead returns `{"configured": false, …}` so the page can
render a setup state), `memory.namespace_required` (400, no namespace could be
derived), `memory.unavailable` (502, the underlying AWS call failed).

## Console Chat API

`/api/chat/*` backs the Chat playground over the same invoke chain as `/v1`
(`app.services.invoke`). Sessions are AgentCore Runtime sessions: the id the
console sends as `runtimeSessionId` is the one the ledger tracks.

| Method | Path | Result |
|---|---|---|
| `POST` | `/api/chat/{agent_id}` | One turn as SSE (`meta` → `delta`/`tool`/`auth_required`/`policy_denied`/`error` → `done`), plus `saved {message_id}` right before the event that closes a persisted answer (tool / error / done, or the stream's end without `done`) — the id thumbs feedback attaches to; `/v1` streams never carry it; `{prompt?, attachments?, session_id?, retry_of?}` (`retry_of`: see *Retrying a failed turn* below), a missing id starts a new session; message text or at least one attachment is required. `as_user?: bool` (JWT-inbound agents only): `true` sends the signed-in user's Cognito JWT, `false` the workspace M2M token, omitted = the user JWT when a pool sign-in exists, else M2M; `409 chat.as_user_unavailable` when `true` without a pool sign-in. `meta.inbound = {mode: jwt, caller: user_jwt\|m2m}`; the Memory actor is `scoped_actor` in both cases. `auth_required` `{provider, tool, scopes[], url, agent_id}` is an as_user (3LO) consent ask from a tool: `url` is the single-use authorization URL (the console links it only when it is `https:`), the `sessionUri` stays server-side, and the answer keeps streaming around it. History keeps the ask as a `role: "auth"` row (`text` = the Connection, `name` = the tool) **without** the URL, so a restored ask can only be retried; poll `GET /api/identity/grants/{connection}/status` to see when consent completes. `policy_denied` (Harness; payload under *Streaming invoke*) is kept as a `role: "policy"` row (`text` = the reason, `name` = the tool) and restored as the V2 Chat policy-deny card, which then links to the Governance landing page rather than a specific gateway. Console-only status events (chat-streaming S2; the public `/v1` stream and the share page never send them): `meta.started_at` and `tool.started_at` (epoch ms), `phase {phase: starting\|waiting_model\|streaming}`, `thinking {}` (a Harness is reasoning; throttled, never the reasoning text), `tool_end {id, status: success\|error\|denied, duration_ms}` (a code runtime's open tool is ended by the next event), a named `heartbeat {elapsed_ms}` in every quiet stretch (`/v1` keeps the `: keep-alive` comment), and `usage {input_tokens, output_tokens, total_tokens, model_calls}` once at the end when the runtime reports it |
| `GET` | `/api/chat/{agent_id}/turns/{turn_id}/events?after=` | Resume a running console turn (see *Disconnect-safe turns* below): the turn's events after SSE id `after` (`0` = the whole turn), then the live ones, closing when the turn has ended. A `gap {after, first_seq}` comes first when some of those events already left the buffer. `404 chat.turn_not_found` once the turn is gone from this process, or for a turn of another agent, workspace or project; `GET …/history` then has what the ledger saved |
| `POST` | `/api/chat/{agent_id}/turns/{turn_id}/stop` | The stop button: ends a running turn. The upstream is closed, and the partial answer and an `interrupted: stopped by the user` row are saved. Answers `{turn_id, stopped}` once the worker has recorded the stop (at most about 5 s), so the session's next turn is accepted at once. `stopped: false` when the turn had already ended |
| `GET` | `/api/chat/{agent_id}/sessions` | Replayable sessions for the agent: `{session_id, actor_id, turns, last_at, ended_at, preview}` — `ended_at` is set once the console explicitly ended the runtime session, `null` while it is live or merely idle |
| `GET` | `/api/chat/{agent_id}/history?session_id=` | The rendered thread items of one session, in replay order; each carries its ledger `id` and the caller's own `verdict` (`up`/`down`/`null`) |
| `POST` | `/api/chat/{agent_id}/sessions/{session_id}/stop` | **END SESSION** — data-plane `StopRuntimeSession(agentRuntimeArn, runtimeSessionId)` → `{session_id, ended: true, already_ended, ended_at}`. `already_ended: true` when AWS answered `ResourceNotFoundException` (the session had already ended or idle-expired) — a success, not an error. The ledger row is kept (history stays replayable) and stamped `ended_at`; a later turn posted under the same id starts a fresh runtime session and clears it. Only runtime-backed agents qualify (`zip_runtime`, `studio`, `container`, discovered runtimes); a managed Harness — deployed or imported — has no session-stop operation and answers 409 `chat.session_stop_unsupported` with `detail.reason_code` (`harness`). A session of another agent or workspace is 404 `chat.session_not_found`. A `RetryableConflictException` that outlives botocore's retries is 409 `aws.conflict` |

**Disconnect-safe turns.** A console chat turn runs on a background worker, not in
the HTTP response (`app/services/chat_turns.py`, chat-streaming §5.2). The worker reads
the upstream, persists the thread items and publishes every event into the turn's
in-memory buffer, numbered by `seq`. The `POST /api/chat/{agent_id}` response is only
the first subscriber of that buffer. When the browser goes away, the turn keeps
running and keeps saving.

- `meta.turn_id` names the turn. Every frame but a heartbeat carries its `seq` as the SSE `id:` line, which is what `…/turns/{turn_id}/events?after=` continues after. The console uses it to reconnect after a dropped connection, and its stop button calls `…/turns/{turn_id}/stop` as well as closing the stream.
- `GET …/history` adds `running_turn {turn_id, user_message_id} | null`, so a reopened session shows the thread up to that user row and follows the running turn from its first event. END SESSION stops a running turn of the session first.
- An `output_schema` agent's answer row in `GET …/history` carries `structured` (the parsed object the turn streamed); rows without one have no such field.
- Tool approvals (strands-builder SB-2, agents with `tool_hooks.approval_tools`): a turn's stream may carry `approval_required {tool, tool_use_id, input}` (`input` already masked by the runtime) — the tool was paused and the agent asks the user to approve it. History keeps it as a `role: "approval"` row (`text` = the masked input as JSON, `name` = the tool). To approve, send the next turn with `approved_tools: [<tool>]` (one entry = one call of that tool). Console turns of such an agent send the runtime `tool_approvals {interactive: true, approved[]}`; `/v1` never does, so there the runtime refuses the tool (`409 agent.tool_approval_requires_console`).
- One running turn per session: a new turn on a session whose turn is still running answers `409 chat.turn_in_progress` (`detail.turn_id`, `detail.session_id`) before the stream opens.
- A turn runs at most its agent's `timeout_seconds` + 60 s (3600 s + 60 s when the spec sets none). Past that, the upstream is closed and the stream ends with `error` `chat.turn_timeout` (`detail.limit_s`, `retry.category: timeout`, no retry button). A turn ended by the stop button ends with `chat.turn_stopped`.
- The buffer keeps at most 2000 events or 600 s of them, and a finished turn stays resumable for 60 s. After that, only the ledger has it.
- Single process (v1): the registry is the serving process's memory. A deployment with several workers must route a resume to the process running the turn, and a restart loses the running turns. Their partial answers stay in the ledger up to the last item flushed.

`/v1` and the share page are unchanged: their streams still end with the connection.

Ending is explicit: NEW SESSION in the console only forgets the id locally, so the
runtime session it leaves behind idles out on its own. END SESSION is what to press
after a re-publish — AgentCore pins a live session to the version that first
served it, so validation of the new version needs a fresh session.

**Retrying a failed turn.** The console stream carries the ledger ids a retry
needs. `/v1` and the share page do not send them.

| Event | Added members |
|---|---|
| `meta` | `user_message_id`: the ledger id of this turn's user row, the id a retry names |
| `error` | `message_id`: the ledger id of the persisted error row. `transient: bool`: whether the evaluation runner's transient-error test recognises the failure (throttling, 5xx, a dropped connection). `retry: {category: timeout\|connection\|busy\|config\|other, manual, auto, timeout_s?}`: how the console offers the retry |

`category` is one of these values:

- `timeout`: `harness.execution_timeout`, the agent used its whole budget. `timeout_s` is the agent's `timeout_seconds` when it is set.
- `connection`: the transport to the runtime dropped or timed out.
- `busy`: any other transient failure.
- `config`: any other named error, such as a permission, a parameter, a limit or a policy.
- `other`: everything else.

`manual` (the retry button is offered) is true for `connection`, `busy` and `other`,
never for `timeout` or `config`, which a replay cannot fix. `auto` (retry once
without asking) is true only for `connection` or `busy` when the turn has not yet
streamed a `delta`, `tool`, `auth_required` or `policy_denied`, so a replay repeats
nothing the user saw and no tool side effect.

To retry, post the same `session_id` with `retry_of` set to any ledger id of the
failed turn: its `user_message_id`, or the error's `message_id`. The turn must be the
session's latest and must have failed or answered nothing. The new user row records
`retry_of`. The failed turn stays in the ledger, but `GET …/history` no longer replays
it, so a restored session shows the message once. An interrupted stream whose turn a
retry has already replaced records no `interrupted` row. Refusals happen before the
stream opens:

- `404 chat.message_not_found`: the id does not exist, belongs to another session, agent, workspace or project, or `session_id` is missing.
- `409 chat.retry_not_allowed` (`detail.retry_of`): the turn is not the latest, or it answered without an error.

All invoke entrances accept optional
`attachments: [{name, media_type, data}]`, where `data` is standard base64.
`GET /api/agents` and `/v1/agents` expose each agent's `attachment_capability`,
including `images`, `text`, `pdf` (`native`/`text`/`unsupported`), accepted
extensions, limits and a reason code when native inputs are unavailable.

Limits are five files, 3 MiB per file and 10 MiB total decoded bytes; the request
body is capped at 15 MiB. PNG/JPEG/WebP/GIF, PDF and UTF-8 text/code formats are
validated by content before invocation. PDFs are limited to 50 pages. Combined
prompt and extracted text may not exceed 100,000 characters.

Harness accepts text files and explicitly labeled PDF text extraction. Images
and nonblank scanned/graphic-only PDF pages require a native-capable agent.
Generated Strands/Claude/Studio/converted HTTP runtimes need a compatible
published artifact and a fresh session; old entrypoints cannot silently discard
files. Native attachments are unavailable during an active canary.

The SSE `meta.attachments` and each history message's `attachments` contain
`{name, media_type, size, delivery}` metadata (`native`, `text`, or `pdf_text`).
Original file bytes are transient and are not stored in chat history. File
validation returns localized `chat.attachment_*` errors before SSE starts;
runtime acknowledgement/model failures use the existing stream error envelope.

## Console Evaluation Datasets API

`/api/eval/datasets` holds the local scenario datasets (SQLite, the editable source
of truth) and their one AWS Dataset each. AWS datasets have a **DRAFT** plus
immutable numbered **versions**: SYNC TO AWS creates the dataset once and afterwards
replaces the draft's examples in place; PUBLISH VERSION snapshots the draft.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/eval/datasets` | `{datasets[]}` — local rows with `items`, `kind`, `has_ground_truth`, the `cloud` blob and the Agent-DLC `role` (`scratch`/`golden`), `split` and `criteria_lineage_id` |
| `POST` | `/api/eval/datasets` | Create from items (devguide scenarios, simulated personas or legacy prompts; kind is inferred) → 201 |
| `PUT` · `DELETE` | `/api/eval/datasets/{dataset_id}` | Edit (kind is immutable → 400 `dataset.kind_immutable`) / delete the local row; a synced AWS copy stays |
| `POST` | `/api/eval/datasets/{dataset_id}/sync-to-aws` | Without a live cloud copy: `CreateDataset` (inline examples) polled to `ACTIVE`. With one: edit its **draft in place** — `ListDatasetExamples` → `DeleteDatasetExamples` (skipped when the draft is empty) → `AddDatasetExamples` with the normalized scenarios, each polled through `UPDATING` to `ACTIVE`; the dataset id and published versions survive and the draft reads `MODIFIED`. A copy AWS no longer knows (`ResourceNotFoundException` on `GetDataset`) or marked `deleted` is re-created. Returns the row; `CREATE_FAILED` / `UPDATE_FAILED` / timeout → 502 `dataset.sync_failed` with the AWS `failureReason`, also recorded on the blob |
| `POST` | `/api/eval/datasets/{dataset_id}/publish-version` | `CreateDatasetVersion` on the row's cloud copy, polled through `UPDATING` to `ACTIVE` → the row with the new version first in `cloud.versions` and `cloud.draft_status == "UNMODIFIED"`. No live copy → 409 `dataset.not_synced`; `UPDATE_FAILED` / timeout → 502 `dataset.publish_failed` (reason recorded on the blob, versions kept) |
| `GET` | `/api/eval/datasets/cloud` | Every AWS dataset in the workspace region: `{datasets[{datasetId, name, status, schemaType, exampleCount, draftStatus, updatedAt}]}` |
| `GET` | `/api/eval/datasets/cloud/{cloud_id}` | Draft detail: `{datasetId, name, status, schemaType, exampleCount, draft_status, failure_reason, versions[{version, example_count, created_at}], runnable, has_ground_truth}` — versions newest first |
| `POST` | `/api/eval/datasets/cloud/{cloud_id}/publish-version` | PUBLISH VERSION for a cloud-only dataset → the refreshed detail above; failures as for the local route |
| `DELETE` | `/api/eval/datasets/cloud/{cloud_id}` | `DeleteDataset` — the draft and every version; local rows pointing at it are marked `cloud.status = "deleted"` and re-create on the next sync |
| `DELETE` | `/api/eval/datasets/cloud/{cloud_id}/versions/{version}` | `DeleteDataset` with `datasetVersion` — one published version; the draft and the other versions stay, cached lists are refreshed |

The `cloud` blob on a local row: `{dataset_id, arn, status, synced_at, failure_reason,
draft_status (MODIFIED|UNMODIFIED), example_count, versions[{version, example_count,
created_at}]}`. It caches display state only — AWS is the source of truth and every
mutation re-reads `GetDataset` / `ListDatasetVersions`.

## Console Data Processing API (V2)

Console V2's 数据中心 turns observed sessions (Agent trajectories) into local
evaluation datasets. One extractor (`app/evaluation/pipelines.py`) serves both
routes below: it reads the session transcript through the observability service
(same private-session visibility rule as `/api/observability/sessions`), pairs each
user input with the following agent reply, and writes a **predefined** scenario per
session (`scenario_id = trace-<session>`, `turns[{input, expected_response}]`,
`metadata.source = "trace"`) — or a `{prompt, expected}` pair from the first
exchange when the target is a **legacy** dataset. Simulated-persona datasets are
refused (400 `dataset.kind_unsupported`). Merging skips a scenario id already
present, optionally an identical first input (`dedupe`), and anything past the
200-item cap; nothing here calls a model.

A pipeline source can instead read **CloudWatch log groups** (`source.type = "logs"`,
`app/evaluation/pipeline_logs.py`): one Logs Insights query takes the window's newest
records (at most 10,000; `keyword` narrows them, case-insensitive), and a format rule
embedded in the pipeline turns each record into `{role, text}` turns grouped by
session — `genai` (OTel GenAI content records such as `otel-rt-logs`, filtered
server-side and read like the session transcript), `message` (one record = one
message: `session_field` + `role_field` + `text_field`, roles mapped through
`user_roles` / `assistant_roles`) or `exchange` (one record = one question:
`input_field` + optional `output_field`; no `session_field` makes each record its own
scenario under a stable hashed id). Fields are JSON dot paths only (no regex);
`@logStream` as `session_field` makes a stream one session, and a text prefix before
a record's JSON is skipped. The turns then go through the trace extractor:
`scenario_id = log-<session>`, `metadata{source: "logs", session_id, log_group}`.
`processing.keep_replies: false` (either source) stores inputs only.

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/api/eval/datasets/from-sessions` | `{session_ids[1..50], range, dataset_id XOR name, description?, first_turn_only?, dedupe?}` → 201 `{dataset, added, skipped[{session_id, reason}]}`. Reasons: `not_found`, `no_transcript`, `no_exchange`, and (with an empty `session_id`) `duplicate` / `dataset_full`. A new dataset with nothing usable → 422 `dataset.nothing_extracted` with `detail.skipped` |
| `GET` · `POST` | `/api/eval/pipelines` | List / create a saved processing task: `{name, description, source{type: traces\|logs, range, max_sessions ≤ 50, agent, status: all\|ok\|error (traces), log_groups[1..10], keyword?, format{preset: genai\|message\|exchange, session_field, role_field, text_field, user_roles, assistant_roles, input_field, output_field} (logs)}, processing{first_turn_only, dedupe, min_input_chars, keep_replies}, output{dataset_id XOR dataset_name}}` (an unknown or simulated output dataset → 404 / 400; a source without `type` is `traces`) |
| `POST` | `/api/eval/pipelines/preview-logs` | `{source}` (a logs source) → what it reads and extracts now, through the converter a run uses: `{events, truncated, parsed, failed{not_json\|not_genai\|no_session\|no_text\|other_role: n}, sessions_found, sessions[≤8 {session_id, stream, log_group, last, records, turns}], samples[≤5 raw {stream, timestamp, message}], paths[≤60 dotted field paths seen in the records]}`; a traces source → 422 `pipeline.not_logs` |
| `GET` · `PUT` · `DELETE` | `/api/eval/pipelines/{pipeline_id}` | Read / replace (409 `pipeline.running` while running) / delete — the output dataset is kept |
| `POST` | `/api/eval/pipelines/{pipeline_id}/run` | `202` with the row as `running` (409 `pipeline.running` if it already is); the bounded run proceeds in the background: list the window's sessions, filter by agent/status, read at most `max_sessions` newest, extract, merge (a logs source: query, convert, take the `max_sessions` newest sessions). The outcome lands on the row as `last_run{at, scanned, matched, added, skipped, dataset_id, error}` (plus `log{events, parsed, failed, truncated}` for a logs source) with `status` `succeeded` / `failed` (a run interrupted by a server restart reads back as `failed`); a `dataset_name` output is created on the first run and the pipeline then targets its id |
| `GET` | `/api/eval/log-services?hours=1..336&log_group=…` | Service names seen in the spans of `aws/spans` (plus any `log_group` given — spans sent to an agent's own group), one Logs Insights query → `{services[{service_name, spans, sessions, last_seen, log_group_names, agent}], log_groups, hours}`. `log_group_names` is the suggested batch input: `aws/spans` + the content log group(s) the spans' resource names (`aws.log.group.names`); `agent` is the platform agent that owns the service, or `null`; `scopes` are its spans' instrumentation scopes and `evaluable` says whether AgentCore Evaluation reads any of them as agent spans (the supported-frameworks scopes, or the generic `opentelemetry.instrumentation.*` / `openinference.instrumentation.*` prefixes minus transport instrumentation such as botocore or starlette; `null` = unknown) — a service with only custom scopes fails every session with "No evaluable agent spans found" |
| `GET` | `/api/eval/log-groups?q=` | Log groups whose name contains `q` (case-insensitive `logGroupNamePattern`), at most 150 → `{log_groups[{name, created_at, retention_days, stored_bytes}], truncated}` |
| `GET` | `/api/eval/log-sessions?service_name=&log_group=…&hours=&q=` | The 日志 source of a `log_source` task: sessions of `service_name` in the given log groups (1–10), found from its spans (the records that always carry both `service.name` and `session.id`), newest first, at most 500 — in the `/agents/{id}/log-streams` row shape (`stream` = the log group · stream of the session's latest record, plus `traces`). `q` keeps sessions with any record containing it (case-insensitive regex match over spans and content logs, with the hit count and an excerpt). Private assistant sessions of other principals are omitted |
| `GET` | `/api/eval/agents/{agent_id}/log-streams?hours=1..336&q=` | The evaluation-task wizard's 日志 source (`app/evaluation/log_streams.py`): the streams of the agent's runtime log group with an event in the window, newest first → `{log_group, streams[{stream, session_id, kind, first_event, last_event, match, matches, snippet}], truncated, hours, q}`. `kind`: `session` (a code runtime's `[runtime-logs-<sessionId>]` stream), `otel_session` (one session's slice of `otel-rt-logs` — a Harness runtime names its streams per microVM, so its sessions exist only there) or `shared` (no single session; not selectable). `q` keeps rows whose stream name contains it (case-insensitive) or whose events contain it as a literal term (`FilterLogEvents`, case-sensitive; an `otel-rt-logs` hit is credited to the session its event names), with `match` `name` / `content`, the hit count and an excerpt. Both scans are capped (500 streams, 10 filter pages) — `truncated` says a cap hid sessions. Another principal's private assistant session is omitted. The picked `session_id`s start an ordinary `POST /api/eval/runs` with `session_ids` + `session_source: "logs"` |

All routes are `MEMBER` and workspace-scoped (`eval_pipelines.workspace_id`).

## Console Evaluators API

`/api/eval/evaluators` is the custom-evaluator CRUD behind the `?view=evaluators`
sub-page. AWS is the source of truth (no ledger row); built-in and third-party
evaluators are read-only. A custom evaluator has exactly one of three
**definitions**, chosen by which body field is present — `instructions`
(LLM-as-a-judge, `llmAsAJudge`), `base_evaluator_id` (derived, `derived`) or
`lambda_arn` (code-based, `codeBased.lambdaConfig`); two or none → 400
`evaluator.definition_ambiguous`.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/eval/evaluators` | `{evaluators[], builtin_count}` — local builtin catalog first (`source: builtin`, trajectory matchers flagged `requires_ground_truth`; when the listing succeeds and names built-ins, only the ones this region offers — a documented built-in still rolling out is left out, since `StartBatchEvaluation` refuses a run that names it), then the account's `ListEvaluators` rows (`source: third_party \| custom`, `evaluator_type`, `provider`, `status`). Custom rows carry `definition: judge \| derived \| code` (read off `evaluatorType` — the list has no config). A failed account listing still answers 200 with the builtins only plus `custom_unavailable: {code, message}` (`aws.*`, `workspace.assume_role_failed` or `evaluators.list_failed`; generic message, never the AWS text); the key is absent on success |
| `POST` | `/api/eval/evaluators` | Create → 201 `{evaluator_id, arn}`. Common: `name` (`^[a-zA-Z][a-zA-Z0-9_]{0,47}$`), `description`. **Judge**: `instructions` (10–4000 chars, ≥1 `{placeholder}` else 422 `evaluator.missing_placeholder`), `rating_scale[≥2]` (default pass/fail), `model_id`, `level` (TOOL_CALL \| TRACE \| SESSION, default TRACE). **Derived**: `base_evaluator_id` (`Builtin.*` \| `ThirdParty.*`; unknown → 400 `evaluator.base_not_found`), `model_id`; the level is the base's. **Code-based**: `lambda_arn` (`arn:aws[-partition]:lambda:<region>:<account>:function:<name>[:qualifier]`), `lambda_timeout_s` 1–300 (default 60), `level`; the Lambda must be in the workspace Region, else 422 `evaluator.lambda_region_mismatch` (`detail: {lambda_region, workspace_region}`). `rating_scale` with a derived or code-based body → 400 `evaluator.rating_scale_not_allowed`. `CreateEvaluator` requires `level` for every definition |
| `GET` | `/api/eval/evaluators/{evaluator_id}` | `{id, name, level, description, definition, instructions, rating_scale, model_id, base_evaluator_id, lambda_arn, lambda_timeout_s, evaluator_type, provider, status, shared_from?}` (`shared_from {project_id, name, access}` on an evaluator another project lent this one, §11.2) — the other definitions' fields are empty/null (a code-based evaluator has `instructions: ""`, `rating_scale: []`, `model_id: null`) |
| `PUT` | `/api/eval/evaluators/{evaluator_id}` | Full-config replace (`UpdateEvaluator` takes the complete config, so every field is sent back) with the same bodies as create minus `name` → the refreshed detail. The payload must be of the evaluator's **current** definition: a judge/derived payload against a code-based evaluator, or a code payload against a judge/derived one → 400 `evaluator.definition_mismatch` (`detail: {current, payload}`) — the evaluator is never converted. Managed ids → 400 `evaluator.builtin_immutable` |
| `DELETE` | `/api/eval/evaluators/{evaluator_id}` | `DeleteEvaluator` → `{deleted: true}`; managed ids → 400 `evaluator.builtin_immutable`. Evaluators referenced by an ENABLED online config are locked by AWS |

**Code-based (Lambda) contract.** The function is invoked by the service with
`{schemaVersion, evaluatorId, evaluatorName, evaluationLevel, evaluationInput.sessionSpans,
evaluationReferenceInputs, evaluationTarget}` and returns `{label, value?, explanation?}`
or `{errorCode, errorMessage}`; the invocation is capped at the configured timeout
(≤ 300 s) and 6 MB of payload. **The console manages no IAM for it**: the
evaluation execution role the platform passes as `evaluationExecutionRoleArn` on
batch and online runs needs `lambda:InvokeFunction` + `lambda:GetFunction` on the
function, and the function's resource policy must allow the
`bedrock-agentcore.amazonaws.com` principal (scope it with `aws:SourceAccount` /
`aws:SourceArn`). Neither is checked at create time — a run against a function the
role cannot invoke fails per session, like any evaluator error.

Live callbacks can omit both documented evaluator identity fields. Assistant-created
code evaluators therefore use one rule set per published Lambda package; they never
choose an arbitrary rule from a shared package. Selecting a known managed legacy
package with zero or multiple rule sets returns `422 run.evaluator_package_ambiguous`
before a new run, invocation or batch is created. The response identifies the
evaluator, owning operation and packaged rule-set count.

## Console Evaluation Runs API

`/api/eval/runs` drives batch evaluations / insights analyses through the bounded
run queue (`eval_max_concurrent_runs`, capped at the 5 active-batch-evaluations
account quota). Run status: `queued → invoking → waiting → evaluating → completed |
failed | stopped`. Every row carries `stop_requested` (an operator stop is pending
on a run whose batch is still STOPPING).

`POST /api/eval/runs` also takes an optional operator-facing `name` (1–64) and
`description` (≤ 1000) — console V2 lists runs as named evaluation tasks — and every
run row carries them (null on unnamed runs) plus `updated_at`.

Every run row (list, detail, create, stop, recheck, re-run) also carries `snapshot`:
the agent version the run evaluated, `{seq, source}` with `seq` the agent's spec
snapshot number (`#N` on the Agent page). `source` is `stamped` (recorded at submit:
the newest snapshot that had reached AWS — one still deploying is not what the run
invokes), `inferred` (an older row, read from the snapshot timeline: the newest
snapshot deployed by the time the run was created, else the oldest) or `observed` (a
time-window / chosen-session / log-stream run, whose sessions predate the run: the
version live when it was made). `null` for a log-source run, an agent with no
deployed snapshot, or a run older than every snapshot when a deployment of the agent
succeeded between the run and the oldest snapshot (the version it evaluated is
unknown). Inferred versions are never written back.

Before creating a run or invoking an agent, known managed code rules are compared
with the target Agent's actual `tools`, `knowledge_bases` and `skills`. An
unqualified zero-call rule or exact empty tool sequence against mounted resources
returns `422 run.evaluator_capability_conflict`, with evaluator/rule identifiers and
an explanation in `detail.evaluators`. A named forbidden write tool remains valid.
External code evaluators whose rule definitions are unknown are not classified by
their display names.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/eval/runs?limit&offset&mode&agent_id` | Newest-first page `{runs, total, limit, offset}` |
| `GET` | `/api/eval/runs/{run_id}` | One run (scores / insight trees / `batch_eval_id` / `budget_stops` — dataset scenarios that ended on the agent's own budget and are scored as they stand, `[{scenario_id, session_id, code, stop_reason}]` / `scenario_failures` — simulated persona scenarios skipped after their retries while the rest of the run went on, `[{scenario_id, retries, code, error}]` (the run fails only when every scenario failed) / `session_failures` — for a batch that ended COMPLETED_WITH_ERRORS, `{total, failed, kind, sessions: [{session_id, index, kind, error_type, message}]}` where `kind` is `telemetry_incomplete` (missing/incomplete-span errors only: the traces never fully reached CloudWatch, re-evaluating cannot recover them), `evaluator_error`, `mixed` or `unknown`, `index` the 1-based scenario position; null on a clean run or a row finished before it existed, backfilled the first time its results are read / `error` / `stop_requested`). |
| `GET` | `/api/eval/scorecard?agent_id=` | **Agent scorecards**, ledger-only (no AWS call) → `{runs, agents, truncated}`. `runs` = the workspace's evaluator-mode runs the viewer may see (insights runs excluded; under workspace isolation the run's creator rule applies, and runs of an agent hidden from the viewer are dropped), the newest 1000, oldest first (`truncated: true` when older ones were left out): `{id, name, agent_key, status, error, created_at, updated_at, evaluators, scores, sessions, scope, dataset_name, dataset_version, benchmark, snapshot, created_by_name}`. `agent_key` = the agent id, or `logs:<service_name>` for a log-source run. `scope`: `dataset` \| `cloud` \| `window` \| `sessions` \| `logs`. `benchmark` = `{kind, key, label, dataset_id, revision, size}`: a local dataset is `dataset` / `ds:<id>`, labelled with its current name (the name stored on the run when the dataset is gone or hidden from the viewer), with `revision` = the fingerprint of the items the run replayed (16 hex of sha256 over the normalized scenarios; null on older rows); a cloud dataset is `cloud` / `cloud:<id>` with `revision` `v<N>` (pinned) or `draft:<fingerprint>`; every passive run (window, sessions, logs) is the agent's `live` benchmark (`key: "live"`, null label/revision). `size` = the replayed scenario count. `agents` (newest evaluation first) = `{key, agent_id, name, method, log_source, deleted, current_seq, versions: [{seq, aws_version, created_at, created_by_name, note}]}` — `current_seq` is the newest deployed snapshot; a deleted agent keeps its runs under the stored name with an empty timeline. With `agent_id` the read narrows to that agent (listed even before its first run when visible) and each version adds `changes`: the diff groups (`prompt`, `model`, `tools`, …) against the newest deployed snapshot before it |
| `GET` | `/api/eval/runs/compare-pair?a=&b=` | **Two runs side by side** → `{runs: {a, b}, alignment, alignment_reason, cases, results, config}`. Both must be terminal evaluator runs (else 422 `run.compare_unsupported`); a run that is missing, of another workspace or hidden → 404 `run.not_found`. `runs.*` = the run payload plus its `benchmark`. `alignment`: `scenario` (both runs recorded which scenario each session replayed — `EvalRun.scenario_map`), `order` (an older run without that map, paired by position only when it skipped no scenario and has exactly one session per scenario of its dataset's current items, or of its pinned cloud version) or `none` with `alignment_reason` (`live_traffic`, `different_benchmark`, `scenarios_skipped`, `session_count_mismatch`, `dataset_unavailable` — gone, unreadable or hidden from the viewer): overall scores only, and no results are read. When aligned, `cases` = `[{scenario_id, input, a: {session_id, results} \| null, b: …}]` in B's scenario order, then A-only ones (`input` ≤ 200 chars), with each side's judge records from its batch results stream (one read per side, as `GET /runs/{id}/results`); `results` = `{a: {available, reason?, detail?, truncated?}, b: …}` — a side that cannot be read degrades alone (its cases carry empty `results`). `config` = `{agent_id, from_seq, to_seq, between: [{seq, aws_version, note, created_at, created_by_name}]}` when both versions are known and differ (`between` = the snapshots after the lower seq up to the higher), `{same: true, agent_id, seq}` on one version, `null` when a version is unknown, the agents differ or the agent is hidden from the viewer. The diff itself is `GET /api/agents/{id}/snapshots/diff?from_seq=&to_seq=` |
| `GET` | `/api/eval/runs/{run_id}/results` | **Per-session judgements** of a terminal evaluators run — `{available, sessions: [{session_id, results: [{evaluator_id, level, score, label, explanation, error_type, error_message}]}], count, truncated}`. The row only stores per-evaluator averages; the judge's explanation of every score lives in the batch's own results log stream (`GetBatchEvaluation.outputConfig.cloudWatchConfig`, the same `gen_ai.evaluation.result` records the online-evaluation views read), which this route reads on demand and never persists. Sessions follow the run's `session_ids` order. `available=false` + `reason` (`insights_run` \| `no_batch` \| `run_active` \| `stream_missing` \| `unreadable` + `detail`) instead of an error when there is nothing to read. Unknown → 404 `run.not_found` |
| `POST` | `/api/eval/runs` | Start a run (exactly one scope: `dataset_id` \| `cloud_dataset_id` \| `session_ids` \| `lookback_hours`) → 201. `session_source: "logs"` (display only, `session_ids` scope only — else 422 `run.session_source_scope`) records the scope as `dataset_name` `logs:<n>`, like `window:<N>h`. **Target**: exactly one of `agent_id` or `log_source {service_name, log_group_names[1..10]}` (else 422 `run.target_required`). A `log_source` run evaluates telemetry already in CloudWatch with no platform agent behind it (e.g. an agent not hosted on AgentCore Runtime): the batch's `cloudWatchLogs` source is `serviceNames: [service_name]` + `logGroupNames` as given, only passive scopes apply (`session_ids` / `lookback_hours`; a dataset scope → 422 `run.log_source_scope`), and every log group must exist (422 `run.log_group_missing`, `detail.missing`). The row echoes `log_source` with `agent_id: ""` and `agent_name` = the service name. A `cloud_dataset_id` scope may add `dataset_version` (a published version number such as `"2"`, never `DRAFT`; omitted = the draft): the version must exist in `ListDatasetVersions` (else 422 `run.dataset_version_unknown`, no run row) and `GetDataset` / `ListDatasetExamples` read that snapshot. `dataset_version` with any other scope → 422 `run.dataset_version_scope`. Every run row echoes `dataset_version` (`null` for draft, local, session and window runs). A BYOC code zip deployed without the ADOT launcher whose endpoint has written no span in 14 days → 400 `eval.no_telemetry` (`detail.service_name`), before any run row exists. |
| `POST` | `/api/eval/runs/{run_id}/stop` | **Stop an active run** → 202 with the run. A run whose batch exists on AWS (`batch_eval_id` set) is stopped with `StopBatchEvaluation`: the batch goes `STOPPING → STOPPED`, the sessions already judged keep their results, and the poller records the run as `stopped` with those partial scores / insight trees and `error = "stopped by operator"`. A run still `queued` is cancelled locally (the worker skips it, AWS is never called) and returns `stopped` at once. A run replaying its dataset or waiting for telemetry (no batch yet) stops between prompts and never calls `StartBatchEvaluation`. Terminal runs (`completed` / `failed` / `stopped`) → 409 `run.not_active`; unknown → 404 `run.not_found`. `DeleteBatchEvaluation` is deliberately not exposed — the ledger keeps the partial results AWS would drop |
| `POST` | `/api/eval/runs/{run_id}/recheck` | `eval.run` — **Re-read a failed run's batch from AWS** → 202 with the run. For a `failed` run that started a batch (`batch_eval_id` set) — or a partially failed `completed` evaluator run with no `session_failures` yet, which re-finishing backfills: one `GetBatchEvaluation`; a terminal batch settles the row exactly as the poller would have (e.g. `completed` with scores, or `failed` with the batch's own reason), a batch still running puts the row back to `evaluating` with a fresh poller. Reads only — nothing is re-run or billed. Other runs → 409 `run.not_recheckable`; unknown → 404 `run.not_found`. Every batch wait is bounded by `eval_batch_wait_s` (default 7200 s, 30 s polls); a run that outlives it fails with "batch evaluation still <STATUS> after the N-minute wait" and can be re-checked from the V2 task detail |
| `POST` | `/api/eval/runs/{run_id}/rerun` | `eval.run`, billable — **Evaluate the run's target again** on the same scope → 201 with the new run, started exactly as `POST /api/eval/runs` would start it: the same validation, visibility and project budget gate (409 `project.budget_exhausted`). Body `{name?}` (1–64; default `<source name> · 再评`). The same target (agent or `log_source`), evaluators and description, and the same dataset (a cloud run's pinned `dataset_version` stays pinned, the draft stays the draft — the examples are read again) or the same window length (`window:<N>h` → `lookback_hours: N`). A simulated-persona dataset plays the user with the platform's current actor model (`actor_model_id` on the Models page). A chosen-session or log-stream run (it would only re-score the same sessions) and an insights run → 422 `run.rerun_unsupported`; unknown → 404 `run.not_found` |
| `DELETE` | `/api/eval/runs/{run_id}` | `eval.run` — **Remove a run that produced no result** (`failed` / `stopped`) from the ledger → `{deleted, run_id, status, aws_batch_left_in_place}`. Only the row goes: a batch evaluation that reached AWS is left as it is (AWS stays the source of truth; the row was the console's pointer and the answer names it). `completed` runs are the evaluation history and active runs must be stopped first → 409 `run.not_deletable`; unknown → 404 `run.not_found`. The Evaluation runs table and the assistant's NEXT STEPS history show ✕ on such rows |
| `GET` | `/api/eval/runs/{run_id}/recommendation-inputs` | **What a recommendation from this run would revise** → `{source, system_prompt, tools: [{name, description, origin}], notes, evaluators, default_evaluator, eligible, reason_code}`. `source: "harness"` = a Managed Harness read live (`GetHarness`: the system prompt, each `inline_function` description, and every attached `agentcore_gateway`'s target tool schemas named `<target>___<tool>`, all narrowed by the Harness `allowedTools`); `"spec"` = the Launchpad agent spec (prompt + discoverable tools); `"manual"` = nothing readable (BYOC without a prompt, CloudWatch-sourced runs, deleted agents) — the console then requires both inputs. `notes` names what could not be read (`remote_mcp_runtime_only`, `gateway_unreadable`, and `harness_unreadable` when the run outlived its Harness or GetHarness is denied — the inputs then fall back to the spec / manual instead of failing). `evaluators` = every evaluator the job can optimize toward, as `[{id, name, level, group: run|builtin|third_party|custom, recommended}]`: the run's own first, then the devguide's two recommended targets (`Builtin.GoalSuccessRate` / `Builtin.Helpfulness`, `recommended: true`), then AWS built-ins, third-party managed evaluators and this account's ACTIVE custom ones (each custom judge read back with `GetEvaluator`). `excluded_evaluators` = `[{id, reason}]` for what cannot be a target — `lower_is_better` (penalty scores), `ground_truth` (trajectory matchers, judges reading `{expected_response}` & friends, managed reference-reading code evaluators), `categorical` (non-numeric judge scale), `unavailable` (the run's evaluator no longer exists). `POST` applies the same rules (422 `recommendation.evaluator_<reason>`). `tools_eligible` is false for a run without session ids (a time-window run) |
| `GET` | `/api/eval/runs/{run_id}/recommendations` | Recommendations started from this run, newest first → `{recommendations: [{id, kind, recommendation_id, status, input_source, system_prompt, evaluator, tools, skipped_tools, result, error, …}]}`. Each non-terminal row is refreshed with one `GetRecommendation` on read (no background poller); a job that ends without text reads as `FAILED` with AWS's own error. A tool job refused because some tools never appear in the traces is restarted **once** without them (`skipped_tools`) |
| `POST` | `/api/eval/runs/{run_id}/recommendations` | `eval.run` — **Start recommendations** `{kinds: ["system_prompt" \| "tool_descriptions"], input_source, system_prompt?, evaluator?, tools?: [{name, description}]}` → 201 `{recommendations}`. One `StartRecommendation` per kind, scoped to the run's sessions: the system-prompt job pins `agentTraces.batchEvaluation` to the run's batch; the tool-description job refuses that source (live `ValidationException`), so it gets the same sessions' spans inline (`sessionSpans`, read with the on-demand evaluation query, ≤ 20 000 spans) and tools whose name appears nowhere in them are skipped up front (`skipped_tools`). Only a `completed` run with a batch → else 409 `recommendation.run_not_completed` / `run_no_batch`; a tool job on a run without session ids → 409 `run_no_sessions`, with expired telemetry → 409 `no_spans`, with no traced tool → 422 `tools_not_traced`; empty prompt / no tools / a tool without description → 422. Every refusal happens before the first Start, so a request never leaves one kind running and the other unstarted; a custom judge with a categorical rating scale → 422 `recommendation.evaluator_categorical`. Results are shown for review/copy; only the accept route below applies one |
| `PUT` | `/api/eval/runs/{run_id}/recommendations/{rec_id}/edit` | `eval.run` — save a manual revision of a `COMPLETED`, not yet accepted system-prompt recommendation. Body `{system_prompt}`; `null`, blank or the generated text drops the revision. Stored as `edit {prompt, by, at}` apart from `result` (which GetRecommendation refreshes rewrite), returned by the list route; the console diff, COPY and ACCEPT then use it. Ledger-only — nothing is published. Accepted → 409 `recommendation.already_accepted`; tool descriptions → 400 `recommendation.edit_kind`; not completed → 409 `recommendation.not_completed` |
| `POST` | `/api/eval/runs/{run_id}/recommendations/{rec_id}/accept` | `agents.deploy` — **Accept** a `COMPLETED` system-prompt recommendation into a new Harness version → 202 `{agent, job_id, deployment_id, recommendation}`. Body `{system_prompt?}` — the reviewed text to publish; omitted = the saved `edit`, else the generated recommendation (`accepted.edited` records any difference from the generated text). Re-publishes the run's platform-deployed Harness through the same guards and update-mode deploy job as `POST /api/agents/{id}/redeploy` (UpdateHarness → a new immutable version; `DEFAULT` follows it). The platform-appended `## Knowledge bases` section is stripped from the recommended text first — the recommendation revised the *live* prompt, which already carries it. Records `accepted {by, at, agent_id, previous_version, job_id, deployment_id}` on the row (then exposed by the list route); accepted once → 409 `recommendation.already_accepted`; a tool-description recommendation → 400 `recommendation.accept_kind`; not completed / no text → 409 `recommendation.not_completed`; a non-Harness agent → 400 `recommendation.accept_not_harness` |
| `GET` | `/api/eval/queue` | `{running, queued, locked, max_concurrency}` — cancelled runs leave the queue immediately, so the count covers active runs only |

## Console Agent-DLC API — criteria, golden sets, calibration, the gate

`docs/agent-dlc-design.md` is the design; this is the surface. The methodology's
claim is that *a release is decided by evaluation, not by a meeting*, so these
routes own four things: the standard (a criteria table), the evidence (a golden
set), whether an LLM judge may stand in for a person (calibration), and the
decision itself (a gate in front of production traffic).

Three permissions here are granted to **named people, never by role** —
`criteria.sign`, `golden.admit`, `judge.calibrate` — because they decide what
"good" means. `criteria.manage` (member/operator), `waiver.approve` and
`release.sign` (operator) follow the ordinary separation of duties. Publishing,
signing, seeding, admitting, waiver approval and every release action are
`PROD_PROTECTED`: in a prod-tier workspace a member cannot reach them.

### Criteria tables

A criteria set is versioned under a `lineage_id`. Publishing freezes a version;
editing opens the next one. A published **agent** set must be signed by someone
other than its last editor (`403 criteria.self_sign` for an author, which an
administrator may override). Validation is enforced, not advisory: a red line
cannot be decided by an LLM judge, cost and performance criteria must be metrics,
every dimension needs a criterion or an explicit `n/a:<dimension>` note, and at
least one red line is required. A judge criterion's **effective tier** is
`observe` until a calibration record says it is aligned and unexpired, so the
table never claims to gate on something that cannot gate.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/criteria-sets?kind&agent_id` | `{sets: [...]}` — one row per lineage, newest version |
| `POST` | `/api/criteria-sets` | `criteria.manage` — `{kind: agent\|template, name, agent_id?, scenario?, template_lineage_id?, template_version?, from_evaluation_plan?}` → 201 the full payload. A template's rows are *copied* in; the template never changes under the agent afterwards |
| `GET` | `/api/criteria-sets/{lineage_id}?version` | `{set, criteria, summary, findings, calibration_policy, newer_template_version, versions}`. `summary` carries `tiers`, `judge_share`, `run_cadence`, `declared_gates` / `effective_gates` and `compound_gate_rate` — every gate holding at once (eight 95% gates compound to 66%) |
| `PUT` | `/api/criteria-sets/{lineage_id}` | `criteria.manage` — replace the draft's rows. `422 criteria.invalid` with `detail.findings` (each `{level, code, message, key}`); a published version → `409` |
| `POST` | `/api/criteria-sets/{lineage_id}/versions` | `criteria.manage` — open the next draft from the published version → 201 |
| `POST` | `/api/criteria-sets/{lineage_id}/publish` | `criteria.manage` — freeze this version. Refused while any `error` finding stands |
| `POST` | `/api/criteria-sets/{lineage_id}/sign` | `criteria.sign` — the business owner's signature `{note}`. The gate reports an unsigned standard as INVALID, so this is load-bearing |
| `POST` | `/api/criteria-sets/{lineage_id}/adopt-template?template_version=` | `criteria.manage` — copy a newer template version into a **new** agent version → 201. The new version is unsigned: a changed ruler needs a new signature |
| `DELETE` | `/api/criteria-sets/{lineage_id}` | `criteria.manage` — discard the open draft → `{discarded: <version>}` |
| `GET` | `/api/criteria-sets/{lineage_id}/diff?a=&b=` | `{from, to, changes: [{key, change, fields, before, after}]}` |

### Golden sets

A parent dataset groups three split datasets (`dev` / `regression` / `holdout`),
each synced to its own AWS Dataset with its own immutable versions. Item
provenance lives in `metadata.dlc` (`case_tier`, `criteria_ids`, `origin`,
`expected_source`, `retired`).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/golden-sets` | `{golden_sets: [{id, name, criteria_lineage_id, splits}]}` |
| `POST` | `/api/golden-sets` | `criteria.manage` — `{name, criteria_lineage_id?, description?}` → 201. The gate finds the golden set *through* its criteria lineage |
| `GET` | `/api/golden-sets/{dataset_id}` | The set plus `coverage`: criteria × case tier, `thin` on any criterion with fewer than three items (its rate is noise), `unmapped_items`, `agent_observed_items` and a per-origin `bias` sentence |
| `POST` | `/api/golden-sets/{id}/items` | `criteria.manage` — append to `dev` or `regression` → 201. The holdout is not accepted (`409 golden.holdout_closed`) |
| `POST` | `/api/golden-sets/{id}/seed` | `golden.admit` — **the only path that writes the holdout.** `{items: [{split?, scenario_id, turns, metadata}], shares?}` stratifies un-split items by case tier so all three splits see the same mix, then **seals** the holdout: a second call is `409 golden.holdout_sealed`. A holdout only means anything if nobody tunes against it |
| `POST` | `/api/golden-sets/{id}/move` | `criteria.manage` — `{scenario_id, to: dev\|regression}`. Items never move into or out of the holdout |
| `POST` | `/api/golden-sets/{id}/retire` | `criteria.manage` — `{split, scenario_id, reason}`. A retired item leaves the gate's denominator but is still replayed, so a fixed bug cannot come back unnoticed |
| `DELETE` | `/api/golden-sets/{id}` | admin — deletes the set **and all three splits, holdout included** → `{deleted, id, items}`, audited as `golden.delete`. The only way to replace a sealed holdout curated from a wrong file. `409 golden.delete_busy` while a run still replays a split. The plain `PUT`/`DELETE /api/eval/datasets/{id}` refuse golden rows (`409 dataset.golden_managed`) |

`items` and `seed` check every item before writing; if any item fails, the whole request is refused and nothing is written (so `seed` does not seal the holdout, and the corrected batch can be sent again):

| Error | When |
|---|---|
| `422 golden.empty_input` | `turns` is empty, or a turn has an empty `input` |
| `422 golden.empty_expected` | a turn has an empty `expected_response` (the same rule as `admission.no_expected`) |
| `422 golden.unknown_criteria` | `metadata.dlc.criteria_ids` names a key the served criteria table does not have (the lineage's latest published version plus any newer draft); not checked when the golden set has no criteria lineage |

`code` is the rule of the first failing item; `detail` is `{errors: [{index, scenario_id, code, criteria_ids?}], count, unknown_criteria?}`:
`errors` lists the first 20 problems (`index` is the item's position in the request's `items`; one item can appear twice), `count` is the total,
and `unknown_criteria` collects every unknown key. Items already written are not affected by these rules.

### Annotation and judge calibration

Labelling is **blind**: the judge's verdict is withheld from annotators until the
task closes, because seeing it first would make the agreement number meaningless.
`decide` refuses `aligned` when the numbers do not support it — κ floor, a
minimum n, and not worse than human–human κ − 0.05 — so a person chooses only
between the readings the data allows.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/annotation-tasks?agent_id&status` | `{tasks: [...]}` |
| `POST` | `/api/annotation-tasks` | `criteria.manage` — `{criterion_key, annotators[2..10], adjudicator?, run_id?\|items?, purpose, agent_id?, criteria_lineage_id?, dataset_id?}` → 201. Items taken from a completed run carry that run's judge verdicts (hidden until close). `422 annotation.no_items` when a run has no verdict for the criterion |
| `GET` | `/api/annotation-tasks/{id}` | The task as *this viewer* may see it. An annotator sees only their own labels; `judge.calibrate` (or an admin) sees everything |
| `DELETE` | `/api/annotation-tasks/{id}` | `criteria.manage` — discard a task that has not been decided, with its labels and links → `{deleted, id, labels}`, audited as `annotation.delete`. `409 annotation.closed` for a decided task (the evidence of its calibration) |
| `POST` | `/api/annotation-tasks/{id}/labels` | `{item_ref, label, rationale?, answer?}` → 201. Only an annotator on the task (`403 annotation.not_annotator`); while adjudicating, only the adjudicator |
| `POST` | `/api/annotation-tasks/{id}/adjudicate` | Move to adjudication — the adjudicator settles the disputed items |
| `GET` | `/api/annotation-tasks/{id}/agreement` | `{n, pairs, human_human_kappa, judge_human_kappa, kappa_ci, band, confusion, disagreements, accuracy, policy, suggested_verdict}`. Human–human comes **first**: if people cannot agree, the criterion is unwritable and no judge can fix that |
| `POST` | `/api/annotation-tasks/{id}/decide` | `judge.calibrate` — `{verdict: aligned\|not_aligned, note}` → the calibration record. `409 calibration.not_supported` with the numbers when `aligned` is not earned, and `403 calibration.own_labels` for anyone who produced the task's labels — an annotator, the adjudicator, or whoever issued one of its annotation links (two links minted by one person are one rater, not two): the labels are the evidence, so whoever wrote them does not also rule on them. `aligned` additionally needs a real human ceiling — items with only one rater contribute nothing, so a single person cannot certify a judge |
| `GET` | `/api/calibration/candidate-runs?agent_id&criterion_key&q&since&source&only_eligible&limit&offset` | The run picker of a calibration task: one agent's batch evaluator runs (`mode=evaluators`) as `/api/eval/runs` shows them to the viewer, newest finished first → `{total, runs[{id, name, dataset_name, status, split, created_at, finished_at, release_id, items, eligible, reason}]}`. `items` is the number of samples a task taken from the run would get; `q` matches the name or id; `source=gate` keeps runs a release record evaluated (`release_id`), `manual` the rest. A holdout run never appears (labelling it shows people what the holdout keeps unseen). `only_eligible=false` (default `true`) also returns the runs that cannot be picked, eligible first, with `reason` `not_completed` / `no_results` |
| `GET` | `/api/calibration/{criterion_key}?agent_id` | `{records, status, policy}`. A record expires after the workspace's `calibration.period_days` — after that the judge is observed again |

**Annotation links** let an expert label without a console account. A link *is* an
annotator: minting one appends `link_<id>` to the task's annotators, so its votes
count toward κ under a stable identity, and the label (the person's name) is for
display. A **prod-tier workspace refuses to mint one** (`409
annotation.links_not_allowed`, with the remedy named), and a link issued while the
workspace was dev stops resolving once it is prod.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/annotation-tasks/{id}/links` | `{links, allowed, reason}` — `allowed: false` in a prod workspace |
| `POST` | `/api/annotation-tasks/{id}/links` | `judge.calibrate` — `{label, expires_in_days?}` → 201 with `token` / `path` / `url`. The token is shown **once**; only its sha256 is stored |
| `DELETE` | `/api/annotation-tasks/{id}/links/{link_id}` | `judge.calibrate` — revoke. The labels it already cast stay: they were real votes |
| `GET` | `/share/annotate/{token}` | **PUBLIC** — the blind queue: the items and *this* annotator's own labels. Never the judge's verdict, another person's vote, or the agreement numbers |
| `POST` | `/share/annotate/{token}/label` | **PUBLIC** — `{item_ref, label, rationale?, answer?}` → the refreshed queue |

Every unusable link (unknown, malformed, revoked, expired, task gone, workspace
promoted to prod) is the same `404 share.not_found`: an outsider must not be able
to tell a revoked link from one that never existed. Page: `/r/annotate/<token>`.

### The release gate

`UpdateAgentRuntime` / `UpdateHarness` auto-roll the DEFAULT endpoint, so a gated
agent serves production through a **named `live` endpoint** and every invoke path
passes `qualifier="live"`. With the workspace policy `release_mode="gated"`, a
successful deploy points `candidate` at the new version and opens a release
record — `live` is untouched, so a candidate is gated without a single user
seeing it. Signing re-points `live`; rollback re-points it back. Nothing is
deleted.

The gate is applied in a fixed order: **red lines** (any violation → `BLOCKED`,
never waivable) → **denominator** (missing verdicts, or an undetermined share over
5% → `INVALID`) → **per-dimension thresholds** at integer-percent precision (a
miss → `BLOCKED` unless an active waiver covers it) → **observed** criteria
(recorded only). Provenance issues — an unsigned criteria version, a run against
another version or endpoint, no holdout evaluated — also make the verdict
`INVALID`. `INVALID` is **not** a failure: the evidence cannot decide, and the fix
is the evidence rather than a waiver.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/agents/{id}/release` | `{release_mode, state, criteria_set, pending, records, waivers}`. `state` carries `endpoint_mode`, the live/candidate versions read from AWS, and `gateable` + `gateable_reason` |
| `POST` | `/api/agents/{id}/release/migrate` | `release.sign` — create `live` at the current version and move production onto it. What production serves is a release decision, so the engineer who builds the agent may not do it. `409 release.not_gateable` names why (A2A, a system preset, nothing deployed yet) |
| `POST` | `/api/agents/{id}/release/evaluate` | `eval.run` — `{repeats?, confirm_cost?}` → 202. Queues the regression **and** holdout runs against `candidate`, priced first: `409 run.cost_over_limit` / `run.cost_confirm_required` carry the estimate, so the spend guard refuses before a session is spent |
| `GET` | `/api/agents/{id}/release/gate` | `{status: evaluating\|decided, report, record}`. The report is `{verdict, criteria: [...], redline_violations, invalid, gate_failures, waived, provenance, order}`; each row carries its measured rate with a Wilson interval, `threshold_inside_ci` (the sample cannot decide), the denominator and `trend` against the previous release |
| `POST` | `/api/agents/{id}/release/sign` | `release.sign` — release the candidate: `live` is re-pointed, so traffic moves. Only a `PASS` report (`409` otherwise), and never the requester (`403`) |
| `POST` | `/api/agents/{id}/release/block` | `release.sign` — the candidate stays off production, with the reason on the record |
| `POST` | `/api/agents/{id}/release/rollback` | `release.sign` — re-point `live` at the previous version → the new state. Nothing is deleted |
| `GET` | `/api/release-records?agent_id=` · `/api/release-records/{id}` | The decision history, each record with its gate report and provenance |

A **waiver** is a gate missed on purpose. It needs a reason, a named risk owner, an
expiry (≤ 30 days) and a second person, and a red line is refused outright
(`409 waiver.redline`). The gate report counts how often a criterion has been
waived — more than once is a standard problem, not an exception.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/agents/{id}/waivers` | `criteria.manage` — `{criterion_key, actual?, threshold?, reason, risk_owner, compensating_control?, expires_on}` → 201 |
| `POST` | `/api/waivers/{id}/approve` · `/reject` | `waiver.approve` — never the requester |
| `DELETE` | `/api/waivers/{id}` | `waiver.approve` — revoke an active waiver |

### Admission, watch, runs and read-backs

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/admission?agent_id&status&refresh` | The candidate queue from thumbs-down verdicts, SME corrections, the issue box and Insights clusters. `priority` ranks a candidate the evaluators scored as a **pass** first: it is both a missing case and a calibration sample |
| `GET` | `/api/admission/{id}` | One candidate with its PII `redaction` preview and the `nearest` existing golden items (exact and normalized), to catch duplicates |
| `POST` | `/api/admission/{id}/admit` | `golden.admit` — `{split, expected_response, expected_source, criteria_ids, case_tier}` → 201. The expected answer must be written or confirmed by a person (`agent_observed` is refused: an agent's own output is not a standard), the redaction must not have blocked, and the holdout is never written |
| `POST` | `/api/admission/{id}/reject` · `/duplicate` | `golden.admit` — a rejection needs a reason; the reason *is* the record |
| `GET` · `PUT` | `/api/agents/{id}/watch` | `eval.run` — the scheduled re-evaluation `{every, at_hour, tz, repeats, max_cost_usd, enabled}`, plus `series`, `alerts`, `drift` and the recent runs. A model can change under an agent without a line of code changing, so the standard is re-applied on a schedule |
| `POST` | `/api/agents/{id}/watch/run?split=` | `eval.run` → 202. A run over the cost ceiling is **skipped and reported** (`409 watch.over_cost_ceiling`) rather than spent |
| `GET` | `/api/eval/runs/{run_id}/criteria?criterion_key` | Per-criterion results of a run: `summary` (n, pass/fail, rate, Wilson bounds, `missing`, `undetermined_rate`, `pass_k`, or a metric `value` against its rule), the `denominator` accounting, `endpoint_qualifier`, and every per-session verdict with the judge's explanation |
| `POST` | `/api/eval/runs/{run_id}/criteria/snapshot` | Re-read a completed run's results stream (a retry after a transient read) → 202 |
| `GET` | `/api/eval/runs/compare?runs=a,b,c` | The fix ladder over 2+ runs, oldest first: per-criterion Δ with a two-proportion p-value, `fixed` / `new_failures` / `still_failing`, and `layers_changed` per rung — a gain across two simultaneous changes belongs to neither. Runs under different criteria versions are `comparable: false` and answered with `two_numbers` (did the agent improve, and how much harder is the standard) |
| `GET` | `/api/agents/{id}/ladder` | The same, over this agent's newest completed criteria runs |
| `POST` | `/api/eval/runs/estimate` | `{agent_id?, dataset_id?, items?, evaluators, repeats}` → items × k × (agent per-session + judge per-item) with its basis named (`history_7d` / `rough` / unpriced), the duration, and whether the workspace policy would ask for confirmation or refuse |
| `GET` | `/api/agents/{id}/scorecard` | The five dimensions in the methodology's fixed order, standard vs current, with the trend series, the metric measurements, the last gate verdict and its decision, open waivers, `calibration_debt` and golden coverage |
| `GET` | `/api/audit?action&target&project&actor&limit` | Administrator — the decision history every workbench shows: who signed the standard, admitted a sample, approved a waiver, released → `{events[{id, actor, actor_user_id, action, target, project_id, workspace_id, at}]}`. `action` is a prefix match. Cross-project: `project=<id>` is that project's events in every workspace it is bound to, `project=workspace` this workspace's events of no project; `actor` is a username or a `users.id` |

`POST /api/eval/runs` also accepts `repeats` (1–10, **pass^k**) and
`confirm_cost`. `repeats` replays each dataset scenario k times in distinct
sessions, which is how consistency is measured — AgentCore has no per-scenario
repetition parameter. It applies to a dataset scope against an agent only
(`422 run.repeats_scope`), multiplies the cost by k, and a run over a golden split
is automatically scored against that split's published criteria so it lands on the
fix ladder.

The release policy keys this module reads are set with
`PUT /api/release-policies/{workspace_id}` (administrator): `release_mode`
(`direct` | `gated`), `calibration` (`{period_days, kappa_floor}`) and the spend
guard `eval_cost_confirm_usd` / `eval_cost_max_usd`. The PUT replaces the policy
wholesale.

## Console Online Evaluation API

`/api/eval/online/*` manages AgentCore **online evaluation configs** — continuous,
sampled scoring of live sessions. AWS is the source of truth; the ledger keeps
identifiers only. Every config in the workspace account is listed and classified
by `owner`: `agent` (created here for an agent), `experiment` (`exp_*`/`can_*`
configs owned by A/B experiments and canaries — read-only, see `parent`), `external`
(anything else).

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/eval/online` | `{configs, total}` — all configs, newest first, with `owner`, both statuses, `failure_reason`, evaluators, sampling, timeout, `matched_agent` (external rows whose log group matches a workspace agent), `parent` (below), `duplicate_enabled` (two ENABLED agent configs on one agent), `results_log_group` |
| `POST` | `/api/eval/online` | Create for an active agent: `{agent_id, mode: scores\|insights (scores), evaluators[1..10] (scores mode), insights[1..3] ⊆ Builtin.Insight.FailureAnalysis\|UserIntent\|ExecutionSummary + clustering_frequencies[0..3] ⊆ DAILY\|WEEKLY\|MONTHLY (insights mode), sampling_percentage 0.01–100 (omit → 10 scores / 100 insights), session_timeout_minutes 1–1440 (15), filters[0..5], description?, enable_on_create (true)}` → 201 row (`status` starts `CREATING`). Mixing kinds → 422 `online_eval.mode_conflict`; rows carry `mode` (derived: `insights` non-empty) |
| `GET` | `/api/eval/online/{config_id}` | Full detail incl. `filters`, `data_source`, `execution_role_arn`, `parent` |
| `PATCH` | `/api/eval/online/{config_id}` | `owner=agent` only: any of `description, sampling_percentage, session_timeout_minutes, filters` plus the mode's own analysis field — `evaluators` (scores) or `insights` / `clustering_frequencies` (insights; complete lists, `[]` frequencies clears clustering); the other kind → 422 `online_eval.mode_conflict`, mode is immutable. The backend re-sends the complete `rule` (AWS replaces it as a unit) |
| `POST` | `/api/eval/online/{config_id}/pause` · `/resume` | Flip `executionStatus` (`agent` + `external`) |
| `DELETE` | `/api/eval/online/{config_id}` | Delete on AWS + drop the ledger row (`agent` + `external`); the results log group is left in place and named in the response |
| `GET` | `/api/eval/online/{config_id}/results?range=1h\|6h\|24h\|7d` | Logs Insights over the results log group: `evaluators[{evaluator_id, level, mean, count, sessions, labels}]`, `series{evaluator: [{bucket, mean, count}]}`, `recent[≤50]` with judge `explanation`, `errors{count, first_message}`; empty collections while nothing has been evaluated yet |
| `GET` | `/api/eval/online/{config_id}/reports` | Insights **reports** = batch evaluations sourced from the config: `{config_id, mode, reports[{batch_id, name, status, run_status, created_at, updated_at, insights, sessions{completed, failed, in_progress, total}, origin: aws_scheduled\|console, run_id, error}], aws_unavailable}` newest first (`aws_unavailable: true` when ListBatchEvaluations failed — console rows only) — console runs from the ledger (`EvalRun.dataset_name == "online:<config_id>"`) merged with AWS-scheduled batches attributed by `GetBatchEvaluation.dataSourceConfig.onlineEvaluationConfigSource.onlineEvaluationConfigArn` (only source-less summaries are candidates; one Get each, cached per batch id). Any owner may read |
| `POST` | `/api/eval/online/{config_id}/reports` | RUN REPORT NOW `{range: 1h\|6h\|24h\|7d (24h)}` → 202 `{run_id, status, queue_position}`: agent-owned insights configs only (403 / 422 otherwise); an `EvalRun(mode=insights, dataset_name="online:<config_id>")` through the bounded run queue whose batch uses `onlineEvaluationConfigSource` — it covers only the sessions the config **sampled** in the window and inherits the config's insights (AWS rejects explicit evaluators/insights on that source) |
| `GET` | `/api/eval/online/{config_id}/reports/{batch_id}` | `{batch_id, name, status, created_at, updated_at, time_range, sessions, insights{failures, userIntents, executionSummaries}, error_details}` (`parse_insights` trees, same as a Runs-page insights run); 404 `online_eval.report_not_found` when the batch is not sourced from this config |

Filter shape: `{key: "[a-zA-Z0-9._-]+", operator: Equals|NotEquals|GreaterThan|LessThan|
GreaterThanOrEqual|LessThanOrEqual|Contains|NotContains, value: {stringValue|doubleValue|booleanValue}}`
(exactly one typed value).

`parent` names the record behind an `owner=experiment` config (`null` on every other
owner): `{kind: experiment|canary, id, name, status, agent_name, arm: control|treatment|null}`.
It is resolved from the ledger by the 8-char id prefix in the name: `exp_<id8>_…` is a
configuration A/B experiment (`_oe1` scores both arms, so `arm` is `null`; a legacy
pre-split `_oe2` scored the experiment's built-in canary challenger, so `arm` is
`treatment`), and `can_<id8>_oec` / `_oet` is a runtime canary's control / treatment
arm. `agent_name` is the experiment's agent or the canary's champion. When the record is
gone, `id` / `name` / `status` / `agent_name` are `null` (`kind` and `arm` come from the
name). The list loads the owning records in one query per model; when several share the
prefix, the newest one the viewer can see wins.

Error codes: `online_eval.no_telemetry` (400, the agent has no telemetry log group
yet — run one session first), `online_eval.evaluator_unsupported` (400, trajectory
matcher / unknown built-in / custom judge that needs ground truth),
`online_eval.read_only` (403, action not allowed for that owner),
`online_eval.not_found` (404), `online_eval.conflict` (409, name collision after one
retry), `online_eval.workspace_not_bootstrapped` (400), `online_eval.invalid_filter`
/ `online_eval.bad_range` (422).

Results appear only after a session is idle for `session_timeout_minutes`; custom
evaluators referenced by an ENABLED config are locked by AWS (no edit/delete).

Online scores also surface where sessions are looked at:

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/observability/sessions/{session_id}` | The session detail carries `online_scores: {configs[{config_id, config_name, owner, agent{id,name}?, records[{time, evaluator_id, level, score, label, explanation, trace_id}]}], total, unavailable, configs_exist}` — every config's result records for that session (agent-owned blocks first), read with one prefix `SOURCE logGroups(namePrefix: ['/aws/bedrock-agentcore/evaluations/results/'])` query. Fail-soft: a results-query failure sets `unavailable: true` and never removes traces or transcript; `configs_exist` is whether the workspace has an agent-owned config (the UI hides the block when neither results nor configs exist) |
| `GET` | `/api/observability/sessions/{session_id}/transcript` | Conversation only — `{session_id, transcript}` with the same `transcript` shape as the session detail, and **no** Logs Insights query (the V2 evaluation result drawer's read). Optional `agent_id` attributes a session no ledger row claims (an id from another workspace is ignored); optional `since` (ISO time of its first span) then lets a session with nothing in Memory rebuild its turns from that agent's runtime content logs — the session detail passes its own first-span time the same way. Eval-run sessions with no memory events rebuild turns from the runtime's otel-rt-logs content records — for a harness, from its backing runtime's log group (`harness_<name>-<id>-DEFAULT`), which is the only copy when the harness has memory disabled |
| `POST` | `/api/observability/session-previews` | A read (POST only to carry per-session start times). Body `{sessions: [{session_id, first?}] (1–20), agent_id?}` → `{previews: [{session_id, available, reason, input, output, user_turns}]}` — each session's first user input and last agent reply (each clipped to 600 characters) and its user-turn count, from the same sources as `/transcript` read in parallel, without the long-term record count (the V2 task wizard's Agent-trajectory picker, one page per call). `agent_id` attributes sessions no ledger row claims; `first` (the session list's first-span time) lets such a session fall back to that agent's runtime content logs. A session the caller may not see, or whose read fails, keeps its row with `available: false` and a `reason` (`not_found` · `read_failed` · `not_platform_session` · `memory_unavailable` · `no_turns`) |
| `GET` | `/api/overview/online-quality` | ONLINE QUALITY · 24h tile: `{range: "24h", mean, scores, sessions, agents, configs, evaluators[{evaluator_id, mean, count, polarity}], cached}` — count-weighted mean over every (evaluator, agent-owned config) pair with lower-is-better evaluators inverted (`1 − mean`), so the tile always reads higher-is-better; `evaluators[].mean` stays raw; `configs` counts the workspace's agent-owned configs (ledger) and `agents` the agents that scored, so "configured, nothing judged yet" is distinguishable from "no config". 120 s per-workspace cache with single-flight, `force=true` bypasses; a workspace without agent-owned configs answers the empty payload without any AWS call |

## Console Observability API — on-demand session scoring

The Observability session detail (`/observability?session=<id>`) can score a
session **right now** with the AgentCore data-plane `Evaluate` API. This is the
third scoring mode next to batch runs (asynchronous, persisted, scoped by
dataset / session ids / window) and online evaluation (sampled, continuous):

| Mode | Call | Latency | Where results live |
|---|---|---|---|
| Batch run | `StartBatchEvaluation` (`POST /api/eval/runs`) | minutes, polled | AWS results log group + ledger `EvalRun` |
| Online | `CreateOnlineEvaluationConfig` (`POST /api/eval/online`) | continuous, ≈10 min judge lag | AWS results log groups, read back per session |
| **On demand** | **`Evaluate` (`POST /api/observability/sessions/{id}/evaluate`)** | **synchronous, one judge inference per evaluator** | **response body only — nothing is persisted** |

| Method | Path | Body / Result |
|---|---|---|
| `POST` | `/api/observability/sessions/{session_id}/evaluate` | Body `{evaluator_ids: string[] (1..5, `Builtin.*` / `ThirdParty.*` / custom id), range?: "1h"\|"6h"\|"24h"\|"7d" (default 24h)}`. Fetches the session's raw span records with one Logs Insights query over both telemetry layouts (`filter ispresent(scope.name) and attributes.session.id = "<id>" \| fields @message \| sort @timestamp asc \| limit 2000`, non-JSON rows skipped), then calls `evaluate(evaluatorId, evaluationInput={sessionSpans})` once per evaluator, sequentially (≤10 results per call). Returns `{session_id, range, span_count, results[{evaluator_id, evaluator_name, evaluator_arn, value, label, explanation, span_context{sessionId,traceId?,spanId?}, token_usage{input,output,total}, error_code, error_message}]}`. A result carrying `error_code` is a per-evaluator **partial failure** (row returned, request still 200). Session-level only: no `evaluationTarget`, no ground-truth reference inputs. |

Errors: `observability.session_spans_missing` (409 — no span records for the
session in the range yet; `detail.hint` explains spans land a couple of minutes
after the invoke), `observability.too_many_evaluators` (422), the standard
`validation.invalid_request` (422 — >5 ids, empty list, bad range or id shape),
`aws.validation` (400 — the AWS `ValidationException` for unsupported spans),
`observability.query_failed` (502 — Logs Insights failure/timeout). Results are
**never written to the ledger**; re-run any time (each run costs one judge
inference per evaluator).

## Console Skill Lab API — evaluation results

The Skill Lab evaluation detail (`/skill-lab?view=eval&job=<id>`) reads one
finished job's judged rows from the CLI's `out/results.json`; the file, not the
ledger, is authoritative and is re-read per request. The route is unchanged;
its response gained a validated token-usage projection. The same table carries
the two routes that save a reviewed taskgen result.

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/skill-lab/jobs/{job_id}/results` | `{summary, rows[]}` for an eval job (a taskgen job returns `{type: "taskgen", count, tasks, summary}` instead). `summary` = `{tasks, passed, invalid, pass_rate, soft_mean, duration_s, judge_prerequisite_missing[], token_usage}`; each row = `{id, task_type, hard, soft, score_valid, duration_s, judge_status, judge_reason, judge_error, error, judge_prerequisite, response (excerpt), artifacts[{path,size}], usage, judge_usage, token_usage}`. The job's status is not checked: the file is served as soon as the CLI has written it (before the process exits), and `404 skill_lab.results_pending` is the answer until then (also the final answer for a job that ended before scoring). |
| `POST` | `/api/skill-lab/jobs/{job_id}/import-taskset` | Save a **succeeded taskgen** job's generated tasks as a NEW single-mode task set. Body `{name, tasks?}`; `tasks` is the reviewed selection — `[{index, id?, question?, rubric?, task_type?}]` (max `MAX_TASKS_PER_SPLIT` entries) where `index` is the row's position in the job's `generated_tasks.json` (strict int, no bool/string/float coercion) and the four optional fields are the only author edits accepted; unknown keys (e.g. `files`, `attachments`) are `422 validation.invalid_request`. Rows absent from `tasks` are excluded, an omitted field keeps the generated value, `task_type: ""` clears it; the server rebuilds `files`/attachments from the job snapshot. `tasks` omitted or `null` → every generated row verbatim (legacy). `201 {job, taskset}`. Errors, all before any write: `400 skill_lab.not_a_taskgen_job`, `409 skill_lab.job_not_finished` / `skill_lab.already_imported` / `skill_lab.results_missing`, `422 skill_lab.taskgen_empty_selection` (empty `tasks`), `422 skill_lab.taskgen_bad_selection` (index out of range or repeated), `422 skill_lab.taskgen_duplicate_id` (edited ids not unique), `422 skill_lab.taskset_invalid` (the validator subprocess, e.g. an unsafe id), `404 skill_lab.job_not_found` outside the caller's workspace. |
| `POST` | `/api/skill-lab/jobs/{job_id}/apply-expansion` | Append a **succeeded expansion** job's generated tasks to its target task set/split. Body optional: `{tasks?}` with the same selection shape and rules as `import-taskset`; no body / no `tasks` appends every generated row. The **edited** ids are re-checked against every current split of the target (`409 skill_lab.expansion_conflict` names the colliding ids), other splits are preserved, and the write is a validated full-replace. `200 {job, taskset}`; `400 skill_lab.not_an_expansion_job`, plus the same `409`/`422`/`404` family as import. Both routes leave the job's `generated_tasks.json` and attachment snapshot untouched. |
| `GET` | `/api/skill-lab/jobs/{job_id}/artifacts?path=` | The job's `out/` tree, any status. A directory → `{kind: "dir", path, dirs[], files[{name, size}]}`; a file → `{kind: "text", path, size, truncated, content}` (UTF-8, `content` capped at 512 KB with `truncated: true` beyond it) or `{kind: "binary", path, size}` (NUL byte / undecodable). A job that has not created `out/` yet (queued, or running before the CLI wrote anything) answers an **empty root listing**, not an error; a missing or vanished sub-path is `404 skill_lab.artifact_not_found`. Absolute, `~`, backslash or NUL paths and anything resolving (symlinks included) outside `out/` are `400 skill_lab.bad_path`. |
| `GET` | `/api/skill-lab/jobs/{job_id}/artifacts/raw?path=` | The file's exact bytes as a download (`Content-Disposition` filename), never capped; `404 skill_lab.artifact_not_found` for a directory or missing file, same `400 skill_lab.bad_path` guard. Both routes 404 `skill_lab.job_not_found` for a job outside the caller's workspace. |

**`token_usage` (added).** Per row: `{target: <record>, judge: <record>}` where
a record is `{status: "reported"|"missing"|"malformed", input, cache_write,
cache_read, output, unattributed}` — every counter an integer or `null`. The
raw producer fields `usage` / `judge_usage` stay on the row unchanged.
The raw fields are made JSON-safe only where the file carried `NaN` /
`Infinity` tokens (which `json.loads` admits): those values are emitted as the
strings `"nan"` / `"inf"` / `"-inf"`; the file itself is never rewritten.
On the summary: `{scope: "reported", target: <side>, judge: <side>}` where a
side is `{rows, reported_rows, missing_rows, malformed_rows, reports_complete,
complete, input, cache_write, cache_read, output, unattributed,
counter_rows{<counter>: n}, counter_complete{<counter>: bool}}`.

Semantics: `null` is *unknown* (no row reported that counter), never zero. The
judge producers report `input`/`output` only, so judge `cache_*` is always
`null`. `unattributed` is what a transcript reported only as a `total` beyond
its counters (the codex form — when every counter is a zero placeholder under a
positive total, the counters are reported as `null`). Malformed counters (bool,
negative, NaN/inf, fractional, non-numeric) are dropped, never coerced; the
row's other valid counters still count and the row is tallied in
`malformed_rows`. Invalid-score rows (`score_valid: false`) keep their usage
in the sums while staying out of `pass_rate` / `soft_mean`. Two kinds of
completeness: `reports_complete` (report coverage — `reported_rows == rows`,
no malformed rows) and `complete` (breakdown completeness — reports complete
AND every counter at least one row reported was reported by every row). A
counter reported by only some rows is a **partial sum**: `counter_rows[k] <
rows`, `counter_complete[k] == false`, and the console marks the cell `k/n`
(e.g. a claude row next to a codex total-only row gives `input` from 1 of 2
rows and `complete: false` even though both rows reported). A counter no row
reported is unknown and does not by itself make the breakdown partial. A
total-only report of `0` is a report (`unattributed: 0`, counters `null`), not
a missing one. `scope` is always `reported`: observed usage over the tasks that
reported it — not a billing total; no cost is estimated. Legacy results written
before usage capture show every side as `missing_rows == rows` with `null`
counters.

## Console Agent IDE API

The coding console (`/v2/coding`) over the platform coding runtime (see
[architecture](architecture.md#agent-ide--a-remote-web-ide-over-codex-on-agentcore)).
A repo is **private to its creator in every visibility mode**: a foreign or
missing id is `404 coding.repo_not_found` on every by-id route; administrators
reach other members' repos only through `GET /repos?all=1` and `DELETE`.
Every 2xx answers JSON. A route that needs the runtime answers `409
coding.runtime_not_installed` before any AWS call when the workspace has none.

| Method | Path | Policy | Result |
|---|---|---|---|
| `GET` | `/api/coding/runtime` | member | `{installed, status: not_installed\|installing\|ready\|failed\|uninstalling, runtime_arn (admins), image_tag, job: {id, type, status, error, log_tail[] (admins)} \| null, models[], efforts[], default_model, default_effort, repair_recommended}` |
| `POST` | `/api/coding/runtime/install` | admin | Install or repair → `202 {job_id, status}`; `409 coding.runtime_busy` while an uninstall runs |
| `DELETE` | `/api/coding/runtime` | admin | Uninstall → `202 {job_id, status}`; `409 coding.runtime_in_use` while any repo runs a turn (snapshots are kept) |
| `GET` | `/api/coding/repos` | member | `{repos: [repo]}` — the caller's own; `?all=1` (admins) every repo of the workspace, `owned` telling which |
| `POST` | `/api/coding/repos` | member | Body `{name (≤ 80), description? (≤ 500)}` → `201 repo` (no runtime call) |
| `GET` | `/api/coding/repos/{repo_id}` | member (owner) | `repo` + `turns[]` (the newest 20, oldest first) + `active_turn` (codex turn id or null) |
| `PATCH` | `/api/coding/repos/{repo_id}` | member (owner) | Body `{name?, description?}` → `repo` |
| `DELETE` | `/api/coding/repos/{repo_id}` | member (owner or admin) | Stops the runtime session (best effort), deletes the repo's snapshots, exports, downloads and rows → `{deleted: true}`; `409 coding.turn_running` while a turn runs; `502 coding.delete_failed` when S3 refused (retryable) |
| `POST` | `/api/coding/repos/{repo_id}/turns` | member (owner) | Body `{prompt, model?, effort?}` → `202` turn row (`status: running`); the turn runs on in the microVM. `409 coding.turn_running`, `422 coding.model_invalid` / `coding.effort_invalid`, `413 coding.prompt_too_long` (> 32 KB), `409 coding.runtime_busy` (uninstalling), `502 coding.runtime_error` |
| `POST` | `/api/coding/repos/{repo_id}/interrupt` | member (owner) | `{interrupted: bool}` (false when no turn runs) |
| `GET` | `/api/coding/repos/{repo_id}/events` | member (owner) | SSE, `?after=<seq>` or `Last-Event-ID`: `event: status {repo_status, active_turn}` first; each record `id: <seq>` + `event: event` + `data: {seq, ts, method, params}`; `: keep-alive`; `event: reconnect {}` after 25 min; `event: error {code, message}` then the end |
| `GET` | `/api/coding/repos/{repo_id}/tree` | member (owner) | `?path=<dir>&depth=1..4` → `{path, entries: [{path (root-relative), dir, size, symlink?}], truncated}`; a vanished directory lists empty |
| `GET` | `/api/coding/repos/{repo_id}/file` | member (owner) | `?path=&rev=head\|turn` (none = working tree) → `{path, exists, content, sha256, size, binary}`; binary → `content: null`; absent → `exists: false` |
| `PUT` | `/api/coding/repos/{repo_id}/file` | member (owner) | Body `{path, content, expected_sha256?}` (omitted = overwrite; a hash = must still match; null = must not exist) → `{path, sha256, size}`; `409 coding.file_conflict` `detail: {sha256}`, `409 coding.turn_running`, `413 coding.file_too_large` (> 2 MB) |
| `GET` | `/api/coding/repos/{repo_id}/changes` | member (owner) | `?scope=worktree\|turn` → `{scope, changes: [{path, status: A\|M\|D\|R, old_path?}]}` (empty before the first turn) |
| `GET` | `/api/coding/repos/{repo_id}/diff` | member (owner) | `?scope=` → `{scope, diff, truncated}` |
| `POST` | `/api/coding/repos/{repo_id}/download` | member (owner) | Body `{path?}`: a path = that one file (text or binary, ≤ 200 MB), none = the whole repo as `<repo>.zip` (the deploy export's file set: tracked + untracked-not-ignored, no `.git`, no dependency dirs; ≤ 50 MB) → `{url, filename, size, expires_in}` — a presigned S3 GET valid `expires_in` (300) s that answers `Content-Disposition: attachment` (RFC 5987 `filename*`). Allowed while a turn runs. `404 coding.file_not_found`, `413 coding.download_too_large` (`detail: {max_bytes, size?}`), `422 coding.path_invalid` (also a directory), `409 coding.runtime_outdated` (a runtime installed before file downloads: repair it) |
| `POST` | `/api/coding/repos/{repo_id}/deploy` | `perm:agents.deploy` (owner) | Body `{agent_name?, description?, artifact_kind?: code_zip\|container_source, entrypoint?, python_version?}` → `202 {agent_id, job_id, created}`; creates a `byoc` Agent (creator = caller) or re-publishes the repo's Agent. `409 coding.turn_running`, `422 coding.deploy_failed` with `detail.reason` = `dockerfile_missing` (no root `Dockerfile` for `container_source`) \| `entrypoint_missing` (+ `entrypoint`, `candidates`) \| `invalid_spec` (+ `errors`), `413 byoc.upload_too_large` (an export over the 250 MiB BYOC zip cap, refused before the download), plus every `POST /api/agents` / redeploy refusal (`agent.name_exists`, `agent.deploy_in_progress`, …) |

`repo` = `{id, name, description, status: idle|running, active_turn_id, model,
effort, agent_id (null once that Agent is deleted), snapshot_at, created_at,
updated_at, created_by, created_by_name, owned}`; a turn row = `{id, turn_id,
thread_id, prompt (first 2000 chars), model, effort, status: running|completed|
interrupted|failed|abandoned, error, usage, started_at, completed_at}` with
`usage` = `{input, cached_input, cache_write, output, reasoning_output}` (codex
counts: `input` includes the cached and cache-write tokens, `output` the
reasoning tokens). Timestamps are naive-UTC ISO strings. Path errors are `422
coding.path_invalid` (absolute, `..`, NUL, `.git`) and never reach the runtime;
other runtime failures are `502 coding.runtime_error` (`detail.runtime_code`)
or `502 coding.restore_failed` when the repo's snapshot could not be restored.

## Console Accounts API

`/api/auth/*` gates the console and `/api/users/*` manages the accounts behind
it. Neither surface touches AWS. See
[architecture.md](architecture.md#console-authentication-and-accounts).

| Method | Path | Auth | Result |
|---|---|---|---|
| `GET` | `/api/auth/status` | open | `{auth_required, authenticated, registration_enabled, registration_requires_approval, username, role, email, account_expires_at, permissions, builtin, version: {sha}}` (`version.sha`: the running console commit, so an open tab notices an online update) — identity fields are null (`permissions`: `[]`) until authenticated |
| `POST` | `/api/auth/login` | open | Sets the `launchpad_session` cookie (12h, clamped to the account validity) and echoes the identity |
| `POST` | `/api/auth/register` | open | `201` — creates a `member` account; by default `status=pending` with `expires_at=null` until an admin approves it, then valid for `auth_registration_valid_days` (default 7) |
| `POST` | `/api/auth/logout` | session | Clears the cookie |
| `POST` | `/api/auth/password` | session | `{current_password, new_password}` — the signed-in account changes its own password (register's policy). For the built-in admin the new password is stored as a hash override that replaces the configured `auth_password`. Every other session of the account ends; the caller's cookie is re-issued. `auth.invalid_current_password` (400), `auth.not_enabled` (400, no gate) |
| `GET` | `/api/users?q=&status=all\|pending\|active\|expired\|disabled&role=&project=&group=&limit=&offset=` | admin | Paged account list with derived `state` / `days_remaining`, plus `builtin` — the built-in admin (id `builtin-admin`, `password_source: config\|console`) when it matches the search and state filter; it is outside `items` / `total`. Each item carries `projects[{id, name, status, is_default, role, sources[{source, role_key}]}]` (direct and group memberships; `role` is the direct role, else the group's) and `groups[{id, name, role, archived}]`. `role` filters the global role (`422 users.invalid_role` for an unknown one). `project` keeps the accounts that are members of that project, directly or through its group (not the admin bypass). `group` keeps the group's members. `project_id` / `group_id` are accepted as aliases. An unknown project or group is 404. A `project` or `group` filter leaves the built-in admin out, since it holds no membership |
| `POST` | `/api/users` | admin | `201` — creates an account that is **active at once** (no approval): `username`, `email` (register's rules, incl. the reserved built-in name and email domains), `password` (omitted = generated and returned once as `generated_password`), `role` (`member` default, `operator`, `admin`), `permissions`, `workspaces` (default `["default"]`; must be empty for `admin`), and at most one of `valid_days` (1-3650), a future `expires_at`, `never_expires: true` (none = `auth_registration_valid_days`). Optional `projects[{project_id, role_key, overrides?}]` (direct memberships: a built-in template, a custom role of that project, or `inherit` in the default project) and `groups[{group_id, role_key}]` (built-in templates only, no overrides). Returns the item shape of `GET /api/users`. `users.invalid_workspaces` / `users.admin_no_grants` / `users.invalid_role` (400), `users.invalid_expiry` (422), `auth.username_taken` / `auth.email_taken` (409) |
| `GET` | `/api/users/stats` | admin | Totals including the `pending` approval queue, `expiring_soon` (≤3 days), 7-day registration/sign-in counts, a 14-day registration series, top email domains |
| `PATCH` | `/api/users/{id}` | admin | Any of `status` (`pending`\|`active`\|`disabled`; `active` on a pending account approves it and starts its window), `role`, `extend_days`, `expires_at` (`null` = never expires), `password` (`null` = generate and return once), `permissions` (`{permission_key: bool}`, `null` = all granted), `workspaces` (full replacement of the account's workspace grants, `null` clears them), `projects` / `groups` (full replacement of the direct project and group memberships, same item shapes as on create; `null` clears them; the default project's `inherit` rows keep following `workspaces`; an item without `overrides` keeps the existing row's) |
| `GET` | `/api/users/{id}` | admin | One account in the `GET /api/users` item shape (grants, projects, groups), for the user detail page. `builtin-admin` returns its built-in projection |
| `GET` | `/api/users/{id}/access` | admin | The account's effective access: the `GET /api/me/access` shape as that account would see it, plus `state`. An administrator's access is every project by bypass; `state` says whether the account can sign in to use any of it |
| `POST` | `/api/users/bulk/projects` | admin | The user list's "add to project": body `{user_ids[1–200], project_id, role_key, overrides?}` → `{project_id, role_key, added[], updated[], unchanged[], skipped[{user_id, reason: "admin"}]}`. Every listed account gets a direct membership with `role_key`; an existing member's role is replaced (and its overrides, when sent). Nobody is removed and no workspace grant changes. Administrators are skipped, since they reach every project by role. An unknown id refuses the whole batch (`404 users.not_found`, `detail.unknown`). Each change is journaled as `project.members.add` / `.update`. `users.invalid_bulk` for an empty or oversized list, and the `PUT /api/projects/{id}/members` errors (`project.role_unknown`, `project.sod_conflict`, …) |
| `DELETE` | `/api/users/{id}` | admin | Removes the account |
| `POST` | `/api/users/{id}/sessions/revoke` | admin | Forced logout → `{ok: true, user_id, session_epoch}`. Cookies are stateless, so this bumps the account's `session_epoch`, which is part of the stamp every cookie carries; every session issued before answers `401 auth.required` on its next request. Journaled as the structure event `user.sessions.revoke`. On `builtin-admin` → `users.builtin_immutable` (400): its sessions end when its password changes |
| `GET` | `/api/admins` | admin | The administrator page's list: `{items[…], total, can_sign_in}`. `items` starts with the built-in admin while the login gate is on (`builtin: true`, `can_sign_in: true`, `can_demote: false`), then every admin-role account oldest first in the `GET /api/users` item shape plus `builtin: false`, `can_sign_in` (active and unexpired), `last_signable` (the only admin left who can sign in) and `can_demote` (false for the caller themselves and for `last_signable`). The top-level `can_sign_in` counts the administrators able to sign in right now, built-in included |

On `builtin-admin`, `PATCH` accepts only `password` (set, or `null` = generate) and
`DELETE` is refused (`users.builtin_immutable`, 400). A `PATCH` / `DELETE` that would
leave no administrator able to sign in answers `users.last_admin` (409); the built-in
admin counts, so while the gate is on it is a backstop. Every account write is
journaled in `audit_events` (hub-global rows, changed field names only, never a
password).

**Administrators.** A `PATCH {role}` that grants or revokes the admin role also
journals the structure event `admin.grant` / `admin.revoke` beside `user.update`.
Nobody removes their own admin role: such a `PATCH` answers `users.self_demote` (409),
unless it would also remove the last admin who can sign in, in which case
`users.last_admin` (409) answers first.

**Organization administrators (stewards, `role: steward`, projects §11.4).** A
steward manages the platform's structure — accounts, departments, projects, groups,
bindings, workspaces, the network and storage catalogues — and never its data. It
passes every `admin` route the route table classes as structure (`ADMIN_SPLIT`) and
is refused the data ones (`403 auth.forbidden`); it reaches every workspace for
structure (`GET /api/workspaces` answers `all_workspaces: true`), but its project
snapshot is empty, so project-scoped routes refuse it. It holds no grant or
membership: `PUT /api/workspaces/{id}/grants`, `PATCH /api/users/{id} {workspaces}`
and project / group member writes naming a steward answer `400 users.steward_no_grants`
or `409 project.steward_member`; a steward adding itself answers
`403 project.self_membership`. On the cross-platform reads it is kept to structure:
`GET /api/audit` lists the structure journal only (projects, groups, departments,
administrators, accounts, `steward.*`, `system.update.*`, workspace tiers, the network
and storage catalogues), `GET /api/inbox` only the account and workspace items, and
`GET /api/fleet` is counts only. Every membership change a steward makes — on a
project, on a group (once per project of the group) or by moving a project into a
group — is journaled as `steward.members.change` naming the project and noticed to
the project's owners (the accounts holding `project.members` there). With
`steward_two_person` on, a steward's project or group membership change is staged
(`pending: [id]`, nothing applied) until another steward, an administrator or the
project's owner confirms it (an owner of every project of the group, for a group's
change).

| Method | Path | Auth | Result |
|---|---|---|---|
| `GET` | `/api/steward/notices` | member | The caller's notices of stewards' changes in projects they own: `{items[{id, kind: changed\|pending, project_id, project_name, actor, actor_user_id, changes[{op: upsert\|remove, user_id, username, role_key?}], pending_id, created_at, read}], unread}` (at most 50) |
| `POST` | `/api/steward/notices/{notice_id}/ack` | member | Marks the caller's notice read → `{ok: true, notice_id}`. Someone else's notice → `404 steward.notice_not_found` |
| `GET` | `/api/steward/pending` | member | `{items[{id, project_id, project_name, group_id?, group_name?, project_ids?, changes[…], requested_by, requested_by_user_id, created_at, mine, can_confirm}]}`: the pending changes the caller may confirm, and the caller's own |
| `POST` | `/api/steward/pending/{pending_id}/confirm` | member | Applies the change as its requester made it → `{pending_id, project_id, group_id?, added[], updated[], removed[]}`; journaled as `steward.pending.confirm`. The requester → `403 steward.confirm_self`; a change to the confirmer's own membership → `403 project.self_membership`; anyone else who may not confirm it → `404 steward.pending_not_found` |
| `POST` | `/api/steward/pending/{pending_id}/reject` | member | Drops the change: withdrawn by its requester (`steward.pending.withdraw`) or refused by a confirmer (`steward.pending.reject`) → `{pending_id, project_id, group_id?, rejected: true}` |
| `GET` | `/api/steward/settings` | admin (structure) | `{steward_two_person}` |
| `PUT` | `/api/steward/settings` | admin (data: a steward does not loosen its own rule) | Body `{steward_two_person: bool}` → the GET shape; journaled as `steward.two_person` |

**Menu settings** (`routers/nav_settings.py`, hub-global): the entries an
administrator hid from everyone's V3 rail and ⌘K, administrators included. A module
entry is listed by rail key (the entry path before the V3 twin mapping, e.g.
`/v2/skill-lab`); the list may also carry the pinned entries `/v3` and `/v3/gate` and
the Command center sections `/v3?section=launch` and `/v3?section=turn`. The backend
checks only the path shape. A view filter only: no route closes, and
administrator-only entries are never hidden.

| Method | Path | Auth | Result |
|---|---|---|---|
| `GET` | `/api/nav/hidden-modules` | member | `{hidden: [path], updated_by, updated_at}` (sorted; never saved → `hidden: []` with `updated_at: null`, which the console reads as its default Normal preset; a saved empty list is Full) |
| `PUT` | `/api/nav/hidden-modules` | admin (data: a steward does not) | Body `{hidden: [path]}` (at most 100; deduplicated) → the GET shape; a change is journaled as `console.nav_hidden`. A path that is not `/v2/…` or `/v3/…` → `422 nav.bad_path`; over 100 → `422 nav.too_many` |

Registration error codes: `auth.registration_disabled` (400, gate off or
registration disabled), `auth.invalid_username` / `auth.invalid_email` /
`auth.email_domain_blocked` / `auth.weak_password` (400),
`auth.username_taken` / `auth.email_taken` (409).

Sign-in error codes: `auth.invalid_credentials` (401), plus
`auth.account_pending` / `auth.account_disabled` / `auth.account_expired` (401)
once the submitted credentials themselves are correct.

Membership errors on create / `PATCH`: `users.invalid_groups` (400, a group item carrying `overrides`), `group.role_unknown` (400, not a built-in template), `group.not_found` (404), `group.archived` (409), plus the project membership errors of `PUT /api/projects/{id}/members`.

Session and role errors: `auth.required` (401 — missing, tampered, or expired
cookie, and also an account that has since been disabled, expired, or deleted),
`auth.forbidden` (403 — member session on `/api/users*`), `users.not_found`
(404).

## Console Single Sign-On API (OIDC)

Console sign-in through an OpenID Connect identity provider (sso §2.6; SSO-1). The
login surface is public; configuring a provider is an administrator's (a steward is
refused: a mapping can grant the admin role). All routes are hub-global. See
[architecture.md](architecture.md#console-single-sign-on-oidc).

| Method | Path | Auth | Result |
|---|---|---|---|
| `GET` | `/api/auth/providers` | open | The login page's buttons: `{providers[{id, name, kind: oidc, preset}]}`, enabled providers only. `GET /api/auth/status` also carries them as `sso_providers` while the login gate is on and one is enabled |
| `GET` | `/api/auth/oidc/{provider_id}/start?return_to=` | open | `302` to the IdP's authorization endpoint (authorization code + PKCE S256; `state`, `nonce` and the verifier stay server side for 10 minutes). `return_to` is a same-origin path to land on after sign-in (anything else is `/`) |
| `GET` | `/api/auth/oidc/{provider_id}/callback?code=&state=` | open | The IdP's redirect back. Verifies the `id_token` (JWKS signature, asymmetric algorithms only; `iss`, `aud` / `azp`, `exp` / `iat` with 2 minutes of skew, the stored `nonce`), provisions or links the account, applies the group mappings, then `302` into the console with the session cookie set. Any failure is `302 /?sso_error=<code>&sso_provider=<id>` — never a JSON error mid-redirect |
| `GET` | `/api/admin/sso/providers` | admin | `{items[provider], presets{keycloak, authentik, okta, entra, google, oidc}, callback_url_template}`. A provider is `{id, name, kind, preset, enabled, sort_order, issuer, client_id, client_secret_set, client_secret_source: encrypted\|secrets_manager\|null, client_secret_ref, scopes, claims{username, email, name, groups}, account_policy: jit\|existing_only, allowed_email_domains[], default_role, valid_days, mappings[], last_test, callback_url, linked_accounts, created_by, created_at, updated_at}` — the client secret itself is never returned |
| `POST` | `/api/admin/sso/providers` | admin | `201` — body `{id?, name, preset?, enabled?, sort_order?, issuer?, client_id?, client_secret?, scopes?, claims?, account_policy?, allowed_email_domains?, default_role?, valid_days?}`; `id` (2-32 lowercase letters, digits, hyphens) defaults to `<preset>-<hex>`, the preset fills scopes and claim names. `client_secret`: a value is stored Fernet-encrypted, an `arn:aws:secretsmanager:` value is stored as a reference, `""` clears it (a public PKCE client). Journaled `auth.sso.config.create` |
| `GET` | `/api/admin/sso/providers/{provider_id}` | admin | One provider (the item shape) |
| `PATCH` | `/api/admin/sso/providers/{provider_id}` | admin | Any field of the create body except `id`; changing the issuer, client id or secret clears `last_test`. Journaled `auth.sso.config.update` (field names only) |
| `DELETE` | `/api/admin/sso/providers/{provider_id}` | admin | `{ok: true, id}`; `409 sso.provider_in_use` (`detail.linked_accounts`) while accounts sign in through it — disable it instead |
| `POST` | `/api/admin/sso/providers/{provider_id}/test` | admin | Reads discovery and the JWKS afresh → `{ok, checked_at, issuer, authorization_endpoint, token_endpoint, userinfo_endpoint, end_session_endpoint, scopes_supported, pkce_s256, jwks_keys, secret_configured, error?{code, message}}`, stored as `last_test`. An IdP-side problem is the answer (`ok: false`), not an error status |
| `GET` | `/api/admin/sso/providers/{provider_id}/mappings` | admin | `{mappings[{group, kind: role\|project\|group, target_id, role_key}], default_role}` |
| `PUT` | `/api/admin/sso/providers/{provider_id}/mappings` | admin | Body `{mappings[…]}` (full replacement) → the GET shape. `role` sets the global role (`target_id` empty), `project` a project role (a built-in template or the project's custom role), `group` a project-group role. Applied at every sign-in; journaled `auth.sso.config.mappings` |
| `GET` | `/api/admin/sso/providers/{provider_id}/logins?limit=` | admin | The provider's recent sign-ins from the journal, newest first (default 20, at most 100): `{items[{at, outcome: success\|refused\|failed, username, reason, jit, target}]}` |
| `GET` | `/api/admin/sso/users/{user_id}` | admin | The user page's account source: `{source: local\|sso, sso: {provider_id, provider_name, subject, last_login_at, role_conflict} \| null}` |

`GET /api/users` items carry `source` (`local` / `sso`), `external_idp_id` and
`external_subject`. An SSO account has no local password: `PATCH /api/users/{id}
{password}` answers `409 users.sso_account_password` and `POST /api/auth/password`
answers `409 auth.sso_account_no_password`. `POST /api/auth/logout` on an SSO session
also answers `end_session_url` (OIDC RP-initiated logout) when the IdP has one.

Configuration errors (400 unless noted): `sso.invalid_preset`, `sso.invalid_name`,
`sso.invalid_id`, `sso.invalid_scopes` (must include `openid`), `sso.invalid_policy`,
`sso.invalid_role`, `sso.invalid_url`, `sso.invalid_mappings`, `sso.provider_exists`
(409), `sso.incomplete` (409, enabling or testing without an issuer / client id),
`sso.provider_not_found` (404). Sign-in reasons in `?sso_error=` and the journal's
`auth.sso.login` rows: `auth.sso.state_invalid` / `state_expired`, `no_code`,
`no_id_token`, `bad_signature`, `bad_issuer`, `bad_audience`, `bad_nonce`, `expired`,
`no_subject`, `email_missing` / `email_invalid` / `email_domain_blocked` /
`email_taken`, `account_conflict`, `not_provisioned` (`existing_only` and no linked
account), `idp_error`, plus `sso.idp_unreachable` / `idp_http_error` /
`idp_bad_response` / `discovery_incomplete` / `issuer_mismatch` / `jwks_invalid` /
`secret_unreadable`.

## Console Single Sign-On API (SAML, SCIM, sign-out)

The SAML 2.0 channel (sso §2.2; SSO-2), SCIM 2.0 provisioning, single logout and the
"hide local login" switch (SSO-3). The SAML leg is public like the OIDC one; SCIM
carries its own credential — the provider's bearer token, minted on the provider's
page — and never reads a console session or a workspace. Everything is hub-global.
Setup walk-throughs: [sso-identity-center.md](sso-identity-center.md),
[sso-keycloak.md](sso-keycloak.md).

| Method | Path | Auth | Result |
|---|---|---|---|
| `GET` | `/api/auth/saml/{provider_id}/start?return_to=` | open | `302` to the IdP's SSO URL with an AuthnRequest (HTTP-Redirect binding, signed with the SP key when `sign_requests` is on). The request id is kept 10 minutes for the `InResponseTo` check; `return_to` as for OIDC |
| `POST` | `/api/auth/saml/{provider_id}/acs` | open | The Assertion Consumer Service (HTTP-POST form `SAMLResponse`, `RelayState`). Checked with `python3-saml` in strict mode: a signature (response or assertion) against the IdP certificates — unsigned is refused; issuer; audience = the SP entity ID; Destination / Recipient = this ACS; `NotBefore` / `NotOnOrAfter` with 2 minutes of skew; `InResponseTo` names a request this console sent to this IdP within 10 minutes (one-time); the assertion id is recorded for 10 minutes so a replay is refused. No `InResponseTo` is IdP-initiated (the Identity Center portal tile) and accepted only while `allow_idp_initiated` is on; `RelayState` is then the same-origin landing path. The NameID is the subject; then the same JIT / group-mapping path as OIDC and `303` into the console. Any failure is `302 /?sso_error=<code>&sso_provider=<id>` |
| `GET` | `/api/auth/saml/{provider_id}/metadata` | open | The SP metadata (`application/samlmetadata+xml`): entityID (this URL), ACS, SLO, NameID format, the SP certificate. Served while the provider is still disabled; `404 sso.provider_not_found` for an unknown or OIDC provider |
| `GET` | `/api/auth/saml/{provider_id}/sls?SAMLResponse=\|SAMLRequest=` | open | Single logout (HTTP-Redirect). The IdP's LogoutResponse to the console's LogoutRequest, or the IdP's own LogoutRequest — that one ends every session of the account (`session_epoch`) and answers the IdP. Then `302` to the console |
| `POST` | `/api/admin/sso/providers/{provider_id}/saml/metadata` | admin | Body `{url?}` or `{xml}`: (re)load the IdP metadata — from the URL (the stored one when none is sent) or an uploaded document — into `idp_entity_id`, `sso_url`, `slo_url`, `idp_certs`. Answers the provider; `409 sso.not_saml` for an OIDC provider. Journaled `auth.sso.config.metadata` |
| `POST` | `/api/admin/sso/providers/{provider_id}/saml/sp-key` | admin | A new SP signing key (RSA 2048) and self-signed certificate; the IdP must import the metadata again. The key is stored Fernet-encrypted like a client secret and never returned. Journaled `auth.sso.config.sp_key` |
| `POST` | `/api/admin/sso/providers/{provider_id}/scim/token` | admin | Mint the provider's SCIM bearer token (`lpscim_…`), replacing any previous one: `{token, scim}` — the value is in this answer only (stored as a SHA-256 hash). Journaled `auth.sso.config.scim_token` |
| `DELETE` | `/api/admin/sso/providers/{provider_id}/scim/token` | admin | Turn SCIM off: the token stops working at once; what it provisioned stays. `{scim}` |
| `GET` | `/api/admin/sso/settings` | admin | `{hide_local_login, local_login_hidden}` — the switch, and whether it is in effect (on and at least one provider enabled) |
| `PUT` | `/api/admin/sso/settings` | admin | Body `{hide_local_login}`. On: the login page shows the IdP buttons only, self-service registration is off, and `POST /api/auth/login` answers `403 auth.local_login_hidden` to every account but the built-in administrator (whose form `/?local=1` still opens). `409 sso.no_enabled_provider` while no provider is enabled. Journaled `auth.sso.config.settings` |
| `GET` | `/api/scim/v2/ServiceProviderConfig` | SCIM token | What this SCIM service supports (PATCH, filter, no bulk / sort / ETag) |
| `GET` | `/api/scim/v2/Users?filter=&startIndex=&count=` | SCIM token | `ListResponse` of the provider's SCIM users (at most 200 a page); `filter` is one `eq` on `userName`, `externalId`, `id` or `emails.value` |
| `POST` | `/api/scim/v2/Users` | SCIM token | `201` — create the account (`userName`, `name`, `displayName`, `emails`, `externalId`, `active`), keyed `(provider, subject)` like a sign-in; an account the IdP already signed in by JIT is adopted, not duplicated. `409 uniqueness` for an existing user or an email another account uses |
| `GET` | `/api/scim/v2/Users/{user_id}` | SCIM token | One user; `404` for another provider's user or a deleted one |
| `PUT` | `/api/scim/v2/Users/{user_id}` | SCIM token | Replace the user |
| `PATCH` | `/api/scim/v2/Users/{user_id}` | SCIM token | `PatchOp` (`add` / `replace` / `remove`). `active: false` disables the account and bumps its `session_epoch`, so its open sessions end on their next request; `active: true` enables it again |
| `DELETE` | `/api/scim/v2/Users/{user_id}` | SCIM token | `204` — disable the account, end its sessions, drop its SCIM group memberships (and the access they mapped); `404` from then on |
| `GET` | `/api/scim/v2/Groups?filter=&startIndex=&count=&excludedAttributes=members` | SCIM token | `ListResponse` of the provider's groups; `filter` is one `eq` on `displayName`, `externalId` or `id` |
| `POST` | `/api/scim/v2/Groups` | SCIM token | `201` — `{displayName, externalId?, members[{value}]}`; a mapping matches the group's `displayName` or `externalId`. The members' access is recomputed at once |
| `GET` | `/api/scim/v2/Groups/{group_id}` | SCIM token | One group (`excludedAttributes=members` drops the member list) |
| `PUT` | `/api/scim/v2/Groups/{group_id}` | SCIM token | Replace name and members |
| `PATCH` | `/api/scim/v2/Groups/{group_id}` | SCIM token | `PatchOp` on `displayName`, `externalId`, `members` (`members[value eq "…"]` removes one). Every member it touches has the provider's group mappings re-applied — only memberships a mapping produced change, manual ones win |
| `DELETE` | `/api/scim/v2/Groups/{group_id}` | SCIM token | `204` — the group and the access it mapped go |

A provider item (`GET /api/admin/sso/providers[/{id}]`) also carries `saml` (`null`
for OIDC) — `{metadata_url, idp_entity_id, sso_url, slo_url, idp_certs[{body, subject,
not_after, sha256, expired}], name_id_format, sign_requests, want_encrypted,
allow_idp_initiated, sp_entity_id, acs_url, sls_url, metadata_endpoint, sp_cert,
sp_cert_facts, sp_key_set}` — and `scim` `{enabled, base_url, token_hint,
token_created_at, subject: userName|externalId, counts{users, groups}}`. The list adds
`acs_url_template`, `scim_base_url` and `settings`; `presets` adds `identity-center`,
`entra-saml` and `saml` (`kind: saml`). The create / `PATCH` body adds
`saml_metadata_url` (fetched), `saml_metadata_xml` (parsed, not stored),
`saml_idp_entity_id`, `saml_sso_url`, `saml_slo_url`, `saml_idp_certs[]`,
`saml_name_id_format`, `saml_sign_requests`, `saml_want_encrypted`,
`saml_allow_idp_initiated` and `scim_subject`; a SAML provider gets its SP key pair
at creation and needs the IdP entity id, SSO URL and a certificate before it is
enabled (`409 sso.incomplete`). `POST …/test` on a SAML provider re-reads the metadata URL
(`metadata_matches`: still what is stored, else `sso.saml_metadata_changed`) and
checks the IdP certificates and the SP key: `{ok, checked_at, kind: saml,
entity_id, sso_url, slo_url, certs[], cert_valid, sp_cert, sp_key, metadata_fetched?,
metadata_matches?, error?}`.

`GET /api/auth/providers` items now include `kind: saml` providers and the answer
carries `hide_local_login`. `GET /api/auth/status` (and the login answer) carries
`account_source`: `sso` for a session that came from an identity provider, `local`
otherwise, absent before sign-in — the top bar hides "change password" for `sso`.
`POST /api/auth/logout` on a SAML session answers `end_session_url`, a LogoutRequest
to the IdP's SLO URL naming the NameID / SessionIndex of that sign-in, when the IdP
has an SLO URL.

SAML sign-in reasons (in `?sso_error=` and `auth.sso.login` rows), on top of the
shared `auth.sso.*` ones: `auth.saml.no_response`, `malformed`, `unsigned`,
`bad_signature`, `bad_issuer`, `bad_audience`, `bad_destination`, `bad_subject`,
`expired`, `in_response_to` (unknown, spent or older than 10 minutes), `replayed`,
`unsolicited` (IdP-initiated while that is off), `not_encrypted`, `no_subject`,
`idp_error`, `invalid`; `auth.saml.logout_invalid` for a refused logout message.
Configuration errors: `sso.saml_metadata_invalid`, `sso.saml_cert_invalid`,
`sso.saml_key_unreadable`, `sso.invalid_name_id_format`, `sso.invalid_field` (SAML
fields on an OIDC provider), `sso.invalid_scim_subject`, `sso.not_saml` (409). SCIM
refusals are SCIM error documents (`application/scim+json`, RFC 7644 §3.12): `401`
without or with an unknown token, `400 invalidFilter` / `invalidSyntax` /
`invalidValue` / `invalidPath`, `404`, `409 uniqueness`, `413` over 2 MB. SCIM changes
are journaled `auth.scim.user.*` / `auth.scim.group.*` with actor `scim:<provider>`.

## Console Projects API

A **project** groups people, agents and their derived rows inside one or more
workspaces. Members reach a project through a project role, and the keys that role
grants decide what they may do there. `/api/projects*`, `/api/role-templates` and
`/api/me/access` are hub-global: a project spans workspaces, so they read neither
`X-Workspace` nor `X-Project`, and the project is the one named in the path. Project
ids the caller cannot see answer `404 project.not_found`, never 403, because a
project's existence can itself be sensitive. Every write that changes who reaches
what bumps `access_epoch` in the same transaction, so the next request in every
process sees it. It also journals a structure event (`project.create`,
`project.update`, `project.archive`, `project.unarchive`, `project.workspaces.bind` /
`.unbind`, `project.members.add` / `.update` / `.remove`, `project.roles.upsert` /
`.delete`, `project.transfer`).

**Permission notation.** Besides `member`, `admin` and `perm:<key>`, the route table
(`core/route_policy.py`) uses `pperm:<key>`. It is a project-scoped permission,
resolved from the caller's role in the project named by `{project_id}` in the path,
never from `X-Project`:

- A non-member gets `404 project.not_found`.
- A member whose effective keys lack `<key>` gets `403 auth.permission_required` (`detail.permission`, `detail.project_id`).
- Administrators pass both checks.
- A non-read request on an archived project then gets `409 project.archived`, administrators included.

On workspace-scoped routes, `perm:<key>` is likewise checked against the effective
keys of the request's project, not the account's own keys. That project is
`X-Project`, or, when the header is absent, the caller's only project in the
workspace or the workspace's default project. Resolution failures:

- `400 project.header_required` (`detail.available`): the caller has several projects in the workspace and none is the default.
- `403 project.none`: the caller is a member of no project in the workspace.
- `409 project.unbound`: an administrator, but the workspace has no project.
- `404 project.not_found`: the named project is not bound to the workspace or is not reachable by the caller.
- `409 project.archived`: a non-read request on an archived project.

Two more policy values exist for workspace-scoped routes. `perm:project.use` (chat,
invoke, feedback, labels, the caller's own sessions) and `perm:project.write` (every
other ordinary write) are project keys, so a project role decides them. `infra`
marks a write to workspace-shared infrastructure (governance, registry MCP and skill
records, gateway targets and tools, marketplace publishing, price refresh): it is
`project.write` while the workspace holds one active project and administrator-only
(`403 auth.infra_admin_required`) once it holds several. Workspace-scoped routes in
`PROJECT_EXEMPT` (catalogues, workspace settings, `/api/costs`, `/api/inbox`, …)
resolve no project at all. The *Scope* column of the route index at the end of this
file says which applies to each route.

A member's **effective keys** in a project combine two sources. First the direct
role is united with the project group's role. The direct row's `overrides`
(`{key: bool}`) are then applied on top. Built-in role templates (code constants,
see `GET /api/role-templates`):

| Template | Keys |
|---|---|
| `project_owner` | `project.members`, `project.audit`, `project.share`, `project.use`, `project.write` and the build keys (`agents.*`, `eval.run`, `promotion.request`, `identity.manage`, `identity.grant`, `memory.manage`, `criteria.manage`). It deliberately has no approve or sign keys |
| `developer` | `project.use`, `project.write` and a member's default keys |
| `biz_owner` | `project.use`, `project.write`, `eval.run`, `criteria.manage`, `criteria.sign`, `golden.admit`, `judge.calibrate`, `identity.grant` |
| `release_ops` | `project.use`, `project.write`, `mcp.approve` and an operator's default keys (including `promotion.approve`, `waiver.approve`, `release.sign`) |
| `sme` | `project.use`, `identity.grant` |
| `tool_steward` | `project.use`, `mcp.approve`: reviews MCP servers' tool lists, and holds nothing it could approve for itself |
| `viewer` | none (read only) |
| `inherit` | the account's own global keys plus `project.use` / `project.write`, and `mcp.approve` when the account holds `promotion.approve`; it exists in the default project only |

The project keys are `project.use` (chat / invoke, feedback, labels, own sessions),
`project.write` (ordinary writes in the project), `project.members` (the project's
members and custom roles), `project.audit` / `project.share` (reserved for
phase 3), and `mcp.approve` (decide a hosted MCP server's tool review, never the
deployer's own; see *Console MCP tool review API*). `mcp.approve` is a project key, not
an agent key, so `/api/auth/status` and `users.permissions` never list it.

**Combined duties.** A member who holds both sides of a separation-of-duty pair in one
project (`agents.deploy` + `release.sign`, `criteria.manage` + `criteria.sign`) is
flagged in the member views (`sod_conflicts`). Under the admin-only setting
`settings.sod_strict`, a write that would leave someone holding such a pair is refused
instead with `409 project.sod_conflict` (`detail.conflicts[{user_id, username,
pairs}]`). This covers member and role writes, group membership and group project
changes, and turning `sod_strict` on. The object-level checks (no approving or signing
your own request) stay the backstop.

| Method | Path | Role | Result |
|---|---|---|---|
| `GET` | `/api/me/access` | member | The caller's access map, the data source of the project switcher and of the console's `can()`: `{username, user_id, role, is_admin, can_manage_structure, workspaces[{workspace_id, name, default_project_id, bound_projects, projects[{id, name, status, is_default, is_workspace_default, permissions[], role_sources[{source: direct\|group:<id>, role_key}], bypass, department_id, department_name}]}], managed_departments[{id, name, workspaces[]}]}`. An administrator sees every workspace and every bound project with all keys (`bypass: true` where they are not a member). `default_project_id` is the project a header-less request would use. `department_id` / `department_name` (null when the project has none) group the switcher's options. `bound_projects` counts the workspace's active bound projects, reachable or not: when the switcher is hidden (one reachable project) but `bound_projects > 1`, the V3 top bar shows the current project as a read-only chip. `managed_departments` lists the departments the caller administers (see *Department administrators* below) with the workspaces open to each, sorted; it is empty for everyone else, administrators included |
| `GET` | `/api/role-templates` | member | Static: `{templates[{key, permissions[]}], inherit: "inherit", agent_keys[], project_keys[]}` |
| `GET` | `/api/projects?department_id=&group_id=&status=&workspace_id=&q=` | member | `{projects[project]}`, every project for an administrator and the caller's own for anyone else; the default project first, then by name. `status` is `active`, `archived` or `all`. `q` matches the id or the name |
| `POST` | `/api/projects` | admin | Body `{id, name (1–64), description? (≤ 2000), department_id?, group_id?, workspaces[1–50], owners?[users.id] (≤ 50, added as `project_owner`), settings?{resource_visibility: shared\|isolated, sod_strict: bool}}` → `201` project detail. `id` matches `^[a-z0-9][a-z0-9-]{1,31}$`. Each newly bound workspace gets the project's own Skill Lab sample task sets, best effort after the commit |
| `GET` | `/api/projects/{project_id}` | member (404 to non-members) | Project detail: `{id, name, description, department_id, group_id, status: active\|archived, is_default, settings, created_by, created_by_name, created_at, updated_at, archived_at, workspaces[binding], member_count, agent_count, my_permissions[], can_manage}`. `can_manage` is true for an administrator and for an administrator of the project's department: the caller may change the project's structure (name, department, group, archive, workspaces, members, roles) |
| `PATCH` | `/api/projects/{project_id}` | `pperm:project.members` | Any of `{name, description, department_id, group_id, settings}` → detail. `name`, `department_id` and `group_id` are admin-only (`403 auth.forbidden`, `detail.fields`). `settings` is merged into the stored settings (`null` removes a key): an owner may change `resource_visibility`, and only an administrator may change `sod_strict`. Moving the project to another group re-evaluates API keys |
| `POST` | `/api/projects/{project_id}/archive` | admin | → detail + `{links_revoked, keys_disabled}`. The project becomes read-only: its share links are revoked and the keys whose creator no longer reaches it are disabled. Archiving an already archived project changes nothing (`0` / `0`). The default project → `409 project.default_locked` |
| `POST` | `/api/projects/{project_id}/unarchive` | admin | → detail; writable again. Revoked links and disabled keys stay so |
| `GET` | `/api/projects/{project_id}/workspaces` | member (404 to non-members) | `{workspaces[{workspace_id, name, region, tier, is_workspace_default, enabled_by, enabled_at}]}` |
| `PUT` | `/api/projects/{project_id}/workspaces` | admin | Body `{bind[], unbind[], make_default[]}` (each ≤ 50) → `{workspaces[binding], bound[]}`, where `bound` lists the newly bound workspaces, which get their Skill Lab samples. `make_default` makes this project the workspace's default (the project a header-less request means; one per workspace). An unbind is refused for the default project, for the workspace-default binding, and while the workspace still holds any of the project's rows (`detail.rows` = `{table: count}`) |
| `GET` | `/api/projects/{project_id}/members` | member (404 to non-members) | `{members[{user_id, username, email, account_role, status, direct{role_key, overrides, added_by, added_at} \| null, group{group_id, role_key} \| null, sources[{source, role_key}], bypass, permissions[], sod_conflicts[[key, key]]}], sod_strict}`: direct and group members with their effective keys. `sod_conflicts` lists the separation-of-duty pairs the member holds both sides of; `sod_strict` is the project's setting. The `PUT` below returns the same two fields. `email` is in full for an administrator and masked (`z***@corp.com`) otherwise. `bypass` marks an admin-role account, which holds every key anyway |
| `PUT` | `/api/projects/{project_id}/members` | `pperm:project.members` | Body `{upsert[{user_id, role_key, overrides?{key: bool}}] (≤ 500), remove[user_id] (≤ 500)}` → `{added[], updated[], removed[], members[…]}`. `role_key` is a built-in template, one of the project's custom roles, or `inherit` (default project only). Only direct rows are written; group-sourced access is managed on the group. Nobody but an administrator edits their own membership. A removed or narrowed member's API keys in the project are disabled |
| `DELETE` | `/api/projects/{project_id}/members/{user_id}` | `pperm:project.members` | `204`, the single-member form of `remove` |
| `GET` | `/api/projects/{project_id}/candidates?q=` | `pperm:project.members` | `{candidates[{user_id, username, email (masked), is_member}]}`: at most 20 active, non-admin accounts whose username or email matches `q` |
| `GET` | `/api/projects/{project_id}/roles` | member (404 to non-members) | The `role-templates` shape plus `inherit_available` (the default project only) and `custom[{key, name, permissions[], based_on, members, updated_at}]` |
| `PUT` | `/api/projects/{project_id}/roles/{role_key}` | `pperm:project.members` | Body `{name (1–64), permissions[] (≤ 64 known keys), based_on? (a built-in template)}` → the roles view. Creates or replaces a custom role; `role_key` matches `^[a-z][a-z0-9_]{1,31}$` and is not a built-in name. Members holding the role are re-evaluated at once, and their API keys with them |
| `DELETE` | `/api/projects/{project_id}/roles/{role_key}` | `pperm:project.members` | `204`; refused while a member holds the role |
| `POST` | `/api/projects/{project_id}/transfer` | admin | Body `{kind: "agent" (default; the only kind in phase 1), resource_id, to_project_id}` → `{agent_id, from_project_id, to_project_id, rows{table: count}}`. A ledger-only move in one transaction: the agent and every row derived from it (transitively), together with its jobs, are re-tagged to the target project, which must be active and bound to the agent's workspace. Source-project API keys scoped to the agent lose it, and a key left with no agent is disabled. Journaled as `project.transfer` in the target project |
| `GET` | `/api/projects/{project_id}/audit?action=&actor=&from=&to=&outcome=&limit=&cursor=` | `pperm:project.audit` (404 to non-members) | One page of the project's journal, newest first: `{events[{id, at, actor, actor_user_id, action, target, outcome, project_id, workspace_id}], next_cursor}` (`limit` 1–500, default 50; `next_cursor` is null on the last page). The rows are the project's own in every bound workspace plus the structure events naming it. The first page (no `cursor`) also carries `facets{actors[], actions[]}` for the filter lists. An unknown `outcome` → `400 audit.invalid_outcome` |
| `GET` | `/api/projects/{project_id}/audit/export?format=csv\|jsonl&…filters` | `pperm:project.audit` | The same filtered journal streamed as CSV or JSON Lines (at most 50,000 rows), `Content-Disposition: attachment; filename="audit-<project>-<stamp>.<format>"`. The download is journaled first as `audit.export` |
| `GET` | `/api/projects/{project_id}/lifecycle` | admin | `{project_id, status, is_default, archived_at, cooling_off_days, deletion{…, purge_after, purge_ready, seconds_left} \| null, deleted, exports[{id, created_at, created_by, bytes, sha256, tables{table: count}}], purge_job{id, status, stage, …} \| null}` |
| `POST` | `/api/projects/{project_id}/export` | admin | `201` `{id, created_at, created_by, bytes, sha256, tables{table: count}}`: writes the export bundle (a ZIP with `manifest.json`, one JSONL per table without secret-looking columns, the Agent specs as YAML, the release bundles and the audit CSV). Journaled as `project.export`; the bundle outlives the project |
| `GET` | `/api/projects/{project_id}/exports/{export_id}` | admin | The bundle as `application/zip` (`project-<id>-<export>.zip`, `Cache-Control: no-store`). Only an export recorded on this project → else `404 project.export_not_found` |
| `POST` | `/api/projects/{project_id}/delete-request` | admin | → the lifecycle view. Writes an export bundle, then starts the cooling-off (`project_deletion_cooling_off_days`, default 7). The project must be archived (`409 project.not_archived`); the default project and a workspace's default project are refused (`409 project.default_locked` / `project.workspace_default`) |
| `POST` | `/api/projects/{project_id}/delete-request/cancel` | admin | → the lifecycle view; drops the pending deletion. `409 project.deletion_not_requested` when there is none, and refused once the purge has started |
| `POST` | `/api/projects/{project_id}/purge?dry_run=` | admin | `dry_run=true` → `{dry_run: true, project_id, agents[{id, name, method, workspace_id, status}], rows{table: count}, retained[]}`, what the purge would remove. Otherwise `202` `{dry_run: false, job_id, …plan}`: the purge job tears down the agents and deletes the rows. Refused before the cooling-off runs out (`409 project.cooling_off`, `detail.purge_after`), without a deletion request (`project.deletion_not_requested`) or export (`project.export_required`), or on a deleted project (`project.deleted`) |
| `GET` | `/api/projects/{project_id}/isolation` | member (404 to non-members) | `{project_id, mode: shared\|isolated, enabled, gateway: shared\|dedicated, updated_by, updated_at, workspaces[{workspace_id, state, job_id, job_status, last_error, skipped_stages[], resources{kind: status}, updated_at}]}`. A member sees only the workspaces they reach the project in |
| `PUT` | `/api/projects/{project_id}/isolation` | admin | Body `{mode?: shared\|isolated, enabled?: bool, gateway?: shared\|dedicated}` (give `mode` or `enabled`; when both, they must agree) → the GET shape + `queued[]` (job ids). Switching on queues a provisioning run per bound workspace. An archived project → `409 project.archived`; an unknown value → `400 project.isolation_invalid` |
| `GET` | `/api/projects/{project_id}/resources?workspace=` | member (404 to non-members) | `{resources[{workspace_id, kind, resource_id, arn, status, detail, error, job_id, created_at, updated_at}]}`: the per-project resources isolation provisioned. A `workspace` the project is not bound to (or the caller does not reach it in) → `404 project.not_bound` |
| `GET` | `/api/projects/{project_id}/provisioning?workspace=` | member (404 to non-members) | One workspace's state (the `isolation` `workspaces[]` entry) + `job{id, status, error, stages[], preflight[], actor, created_at, updated_at} \| null` |
| `POST` | `/api/projects/{project_id}/provision` | admin | Body `{workspace}` → `202` the provisioning view + `created` (false when a run is already in flight). Retries the workspace's provisioning |
| `POST` | `/api/projects/{project_id}/isolation/migrate` | admin | Body `{workspace, direction?: to_isolated\|to_shared, agents?: [id]}` → `202` a migration run: per agent grant-new → republish → verify → switch, resumable (projects §10.11). Preflight failures (quotas, unprovisioned resources) → `409` with the blocking items |
| `GET` | `/api/projects/{project_id}/isolation/migration?workspace=` | member (404 to non-members) | The latest migration run: `{run_id, status, direction, agents[{agent_id, name, step, status, error}], started_at, finished_at, retire_ready}` |
| `POST` | `/api/projects/{project_id}/isolation/migration/{run_id}/retry` | admin | Retry the failed agents of a run from their last finished step → `202` the run |
| `POST` | `/api/projects/{project_id}/isolation/retire` | admin | Irreversible: drop the old shared grants (shared memory, workspace M2M, KB-gateway provider) from the project's boundary once every agent migrated → `{legacy_retired: true}`; `409` while agents remain on the shared resources |
| `GET` | `/api/projects/{project_id}/isolation/agents/{agent_id}` | member (404 to non-members) | One agent's boundary: `{agent_id, isolation_applied, boundary_arn, role_arn, tags{}, migrated}` (§10.14) |
| `GET` | `/api/projects/{project_id}/shares?direction=all\|outgoing\|incoming&include_closed=` | member (404 to non-members) | Cross-project shares (§11.2): `{outgoing[], incoming[]}` of `{id, kind, resource_id, resource_name, owner_project_id, grantee_project_id, access: read\|use, status, expires_at, granted_by, created_at}` |
| `POST` | `/api/projects/{project_id}/shares` | pperm:project.share | Body `{workspace_id, kind, resource_id, grantee_project_id, access, expires_in_days?, resource_name?, note?}` → `201` the share (pending admin approval when the grantee is in another department) |
| `GET` | `/api/projects/{project_id}/shares/options?workspace_id=` | pperm:project.share | What can be shared and with whom: `{resources[{kind, id, name, detail}], grantees[{id, name, cross_department}]}`. Kind `agent` lists the project's deployed Runtime agents (`detail` = method): `use` lets the grantee's Strands agents call it as a tool (agents-as-tools), and an isolated grantee's boundary then allows `InvokeAgentRuntime` / `InvokeAgentRuntimeForUser` on it |
| `DELETE` | `/api/projects/{project_id}/shares/{share_id}` | pperm:project.share | Revoke → the share with `revoked_at` and `pinned_by[]` (grantee agents that pinned it); the grantee's owners are notified |
| `POST` | `/api/projects/{project_id}/shares/{share_id}/approve` | admin | Approve a pending cross-department share |
| `POST` | `/api/projects/{project_id}/shares/{share_id}/reject` | admin | Reject a pending cross-department share |
| `GET` | `/api/me/share-notices` | member | The caller's notices of shares that ended in projects they own (a `project.members` holder of the grantee): `{items[{id, kind: revoked\|expired, share_id, project_id, project_name, owner_project_id, owner_project_name, resource_kind, resource_id, resource_name, access, workspace_id, pinned_by[], actor, created_at, read}], unread}` (newest first, at most 50). `pinned_by` names the grantee's agents that pinned the resource |
| `POST` | `/api/me/share-notices/{notice_id}/ack` | member | Marks the caller's notice read → `{ok: true, notice_id}`. Someone else's notice → `404 share.notice_not_found` |
| `GET` | `/api/agents/{agent_id}/story` | perm:project.use | Storyline facts for one agent (storyline-ux §2.3, cached 30 s): criteria draft / published / signed, golden sealed and items, dev run, endpoint mode, candidate, gate verdict, live release, API keys, Cedar mode, spans in 7 days (cached probe only), alert rules, issues, calibration, pending waivers. Another project's agent → `404 agent.not_found` |
| `GET` | `/api/me/next-steps` | member | The caller's turn across the current workspace's agents (X-Workspace / X-Project rules): `{mine[{agent_id, name, step, action, params}], waiting[{agent_id, name, step, role}]}`; MCP servers and presets are skipped |
| `GET` | `/api/projects/{project_id}/quotas` | member (404 to non-members); the project's department administrator | Usage against limits (projects §11.3): `{project_id, department_id, quotas{…the project's own}, items[{key, unit: count\|usd, enforcement: block\|queue\|budget\|throttle, project_value, limit, used, basis?, state?, workspace_id?}], budget, budget_basis, allow_overrun, department_budget, month_start, reset_at}`. `budget` is null without a monthly budget, else `{project_id, budget_usd, spent_usd, remaining_usd, fraction, exhausted, allow_overrun, block_at_usd, lines[{kind: warn\|alert\|block, fraction, usd}], month_start, reset_at, measured_at, detail, basis}` — lines 80 / 100 / 100 % with the overrun switch off, 80 / 105 / 110 % with it on. `department_budget` is null without a department, else `{department_id, name, monthly_budget_usd, allow_overrun, allocated_usd, unallocated_usd}`. Keys: `max_agents`, `max_api_keys` (block), `max_concurrent_eval_runs` (queue), `monthly_budget_usd` (budget), `v1_rate_per_minute` (throttle) |
| `PUT` | `/api/projects/{project_id}/quotas` | admin (no department overlay) | Body `{quotas{key: value \| null}}` (partial; `null` clears one) → the GET shape. Journaled as `project.quotas.update` (`project:<id> quotas:<changed keys>`). A `monthly_budget_usd` meets its department's allocation rules like `PUT …/budget` (`409 department.budget_overallocated`); a bad value → `400 project.setting_invalid` |
| `PUT` | `/api/projects/{project_id}/budget` | admin; the project's department administrator | Body `{monthly_budget_usd?: number \| null, allow_overrun?: bool}` (partial) → the quotas GET shape. Journaled as `project.budget.update`. The sum of the department's project budgets stays within the department's budget: `409 department.budget_overallocated` (`detail{department_id, budget_usd, allocated_usd, requested_usd, remaining_usd}`). A department with no budget is unlimited (no ceiling). The caller is either at the organization level (an admin or steward holding `budget.manage`) or the project's department administrator (no key needed); anyone else gets `403 budget.forbidden`. `allow_overrun: true` needs the department's switch on (`409 department.overrun_disabled`). A project with no department is the platform's only (404 to a department administrator). `409 project.archived`, `400 project.budget_invalid`, `403 project.setting_forbidden` |
| `GET` | `/api/budgets/platform` | admin (not a steward: cost detail) | The platform cost page: `{departments[{department_id, name, monthly_budget_usd, allow_overrun, archived, allocated_usd, spent_usd, projects[…as on the department page]}], unassigned_projects[…], projects_spent_usd, platform{spent_usd, workspaces[{workspace_id, name, estimate_usd, unattributed_usd, components[], detail}]}, total_usd, alerts[…], month_start, reset_at}`. The platform line is the month-to-date price-map spend no project carries in a multi-project workspace (platform presets, the shared COA / Skill Lab runtimes, deleted agents); `components` names the shared infrastructure (`shared_gateway`, `shared_memory`, `cognito`, `coa_runtime`, `coa_gateway`, `skill_lab_worker`) whose fixed fees only the billing view's shared row prices |
| `GET` | `/api/budgets/me` | member | `{platform: bool, departments[{id, name}], alerts[{department_id, name, level: 80\|100, fired_at, spent_usd, budget_usd}], budget_scope: "platform"\|"department"\|null, budget_departments: string[]}`: which cost pages the caller may open (platform: administrators; departments: every one for an administrator or steward, else the ones the caller administers), the department budget alerts firing for them this UTC month, and where they may set budgets (`budget.manage` + identity) |

Error codes (`project.*` unless noted):

- 409 `budget_exhausted` (`detail{project_id, budget_usd, spent_usd, allow_overrun, block_at_usd, reset_at, month_start, basis}`): a billable operation of a project at its block line — 100 % of its monthly budget, or 110 % with `allow_overrun`.
- 409 `quota_exceeded` (`detail{quota, used, limit}`): creating an agent (`POST /api/agents`, a discovery import, an architect approval) or an API key, re-enabling a disabled key, or starting an evaluation run (`POST /api/eval/runs`, `POST /api/agents/{id}/release/evaluate`) over the project's quotas. An evaluation's estimate is checked against `eval_run_max_usd`; this month's evaluation spend plus the estimate against `eval_monthly_usd_cap` (an unpriced run is refused only once the month is already over the cap). Runs past `max_concurrent_eval_runs` wait in the queue instead.

- 400: `invalid_id`, `workspaces_required`, `binding_conflict` (bound and unbound in one request), `member_conflict` (upserted and removed in one request), `role_unknown`, `override_unknown`, `role_invalid_key`, `setting_invalid`, `transfer_kind` (`detail.supported`), `transfer_same`.
- 400 or 403: `setting_forbidden` (`detail.keys`). It is 403 for an admin-only setting changed by an owner and 400 for an unknown setting.
- 403: `self_membership`, `auth.forbidden`, `auth.permission_required`.
- 404: `not_found`, `member_not_found` (no direct row), `role_not_found`, `department.not_found`, `group.not_found`, `workspace.not_found`, `user.not_found` (an unknown `user_id` in `upsert`), `agent.not_found` (transfer: not in the source project, or deleted).
- 409 on deploy (`POST /api/agents`, redeploy, convert) of an agent in an isolated project: `isolation_requires_per_agent_role` (the shared execution role carries no boundary, so an isolated project's agent needs its own role; per-agent roles are switched off, or the runtime reports none) and `boundary_unavailable` (the project's permissions boundary `launchpad-project-<pid>` could not be created or updated in the workspace, `detail.project_id`).
- 403: `department.not_managed` (a department administrator named a department they do not administer, or none), `department.workspace_not_open` (`detail.workspaces`: a department administrator bound a workspace not open to the department).
- 409: `department.project_limit` (`detail.limit`: the department's `settings.max_projects` is reached).
- 409: `exists`, `archived`, `default_locked`, `workspace_default`, `workspace_in_use`, `not_bound`, `member_from_group` (`detail.group_id`), `role_in_use` (`detail.members`), `sod_conflict` (`detail.conflicts`), `department.archived`, `group.archived`, `transfer_platform` (a system preset), `transfer_busy` (a job of the agent is queued or running), `transfer_linked` (a running canary pairs it with another agent; `detail.canary_id`).

### Project groups and departments

A **department** is an organizational label on projects and groups: it grants no
access, and the V3 project switcher groups its options by it. A **project group**
bundles projects across workspaces and grants its members a built-in role template in
every project of the group (a project is in at most one group). A member's effective
keys in a project unite the group's role with any direct role, and the direct row's
`overrides` apply on top. Both surfaces are hub-global (`/api/project-groups*`,
`/api/departments*` read neither `X-Workspace` nor `X-Project`). Every route is admin,
except `GET /api/departments`, which members may read, and the routes department
administrators pass (below). Group and department ids
follow the project id rule (`^[a-z0-9][a-z0-9-]{1,31}$`, `400 group.invalid_id` /
`department.invalid_id`). A group write that changes who reaches what bumps
`access_epoch` and disables the API keys whose creators lost a project. Writes are
journaled as structure events: `department.create` / `.update` / `.archive` /
`.unarchive` / `.delete`, `group.create` / `.update` / `.archive` / `.unarchive`,
`group.members.add` / `.update` / `.remove`, `group.projects.add` / `.remove`,
`department.admins.add` / `.remove`, `department.workspaces.add` / `.remove`,
`department.invite`.

| Method | Path | Role | Result |
|---|---|---|---|
| `GET` | `/api/departments?status=active\|archived\|all` | member | `{departments[{id, name, description, archived, archived_at, created_by, created_at, updated_at, project_count, group_count}]}`; `status` defaults to `active` (`400 structure.invalid_status` otherwise) |
| `POST` | `/api/departments` | admin | Body `{id, name (1–64), description? (≤ 2000)}` → `201` department. `409 department.exists` |
| `PATCH` | `/api/departments/{department_id}` | admin | Any of `{name, description, settings}` → department. `settings` is partial: `{can_create_users: bool (default false), max_projects: 1–10000 \| null (default null, no limit), user_validity_max_days: 1–3650 (default 90)}`; a `null` value restores that key's default. The department's monthly budget and overrun switch are not settings: `PUT /api/departments/{department_id}/budget`. An unknown key or a bad value → `400 department.setting_invalid` (`detail.key` / `detail.keys`) |
| `POST` | `/api/departments/{department_id}/archive` / `…/unarchive` | admin | → department. An archived department cannot be given to a project or group (`409 department.archived`) |
| `DELETE` | `/api/departments/{department_id}` | admin | `204`, only while no project and no group names it: `409 department.in_use` (`detail.projects`, `detail.groups`) |
| `GET` | `/api/departments/{department_id}/admins` | admin | `{admins[{user_id, username, email, account_role, status, added_by, added_at}]}`, by username |
| `PUT` | `/api/departments/{department_id}/admins` | admin | Body `{add[users.id] (≤ 200), remove[users.id] (≤ 200)}` → `{added[], removed[], admins[…]}`. Appoints or removes department administrators; adding one already appointed changes nothing. `400 department.admin_conflict` (a user both added and removed), `404 user.not_found`, `404 department.admin_not_found` (removing a user who is not one) |
| `GET` | `/api/departments/{department_id}/workspaces` | admin | `{workspaces[{workspace_id, name, region, added_by, added_at}]}`: the workspaces open to the department, where its administrators may bind projects |
| `GET` | `/api/departments/{department_id}/budget` | admin; the department's administrators | The department cost page: `{department_id, name, monthly_budget_usd, allow_overrun, allocated_usd, unallocated_usd, spent_usd, fraction, alert_fractions, projects[{project_id, name, status, budget_usd, spent_usd, fraction, allow_overrun, block_at_usd, exhausted, lines, basis}], alerts[{department_id, name, level, fired_at, spent_usd, budget_usd}], trend[{date, spent_usd}], month_start, reset_at}`. `spent_usd` is the sum of the projects' month-to-date spend (the figure the budget gate reads; null when one is unreadable). Another department's administrator → 403 |
| `PUT` | `/api/departments/{department_id}/budget` | admin (no department overlay) | Body `{monthly_budget_usd?: number \| null, allow_overrun?: bool}` (partial) → the GET shape. An allocation ceiling only — it never blocks an operation. Journaled as `department.budget.update`. `409 department.budget_below_allocated` (lower than what its projects hold), `409 department.overrun_in_use` (`detail.projects`: turn theirs off first), `400 department.budget_invalid`, `403 budget.forbidden` (needs `budget.manage` at the organization level) |
| `PUT` | `/api/departments/{department_id}/workspaces` | admin | Body `{add[] (≤ 200), remove[] (≤ 200)}` → `{added[], removed[], workspaces[…]}`. Closing a workspace stops new bindings only; bindings already made stay. `400 department.workspace_conflict`, `404 workspace.not_found`, `404 department.workspace_not_found` (removing one not open) |
| `POST` | `/api/departments/{department_id}/invitations` | admin, or the department's administrators when `settings.can_create_users` | Body `{username, email, project_id, role_key? (default developer), valid_days? (1–user_validity_max_days)}` → `201` `{user_id, username, email, expires_at, project_id, role_key, generated_password}`: a new `member` account, valid `user_validity_max_days` unless `valid_days` is shorter, added to one of the department's active projects with a project role (not `inherit`) and no workspace grant of its own. The password is shown once, like `POST /api/users`. `403 department.invitations_disabled`, `409 department.archived`, `404 project.not_found` (not a project of this department), `409 project.archived`, `400 project.role_unknown`, `422 department.validity_too_long` (`detail.max_days`) |
| `GET` | `/api/project-groups?status=&department_id=` | admin (a department administrator with `department_id` set to their department) | `{groups[{id, name, description, department_id, archived, archived_at, created_by, created_at, updated_at, projects[{id, name, status}], member_count}]}`, by name |
| `POST` | `/api/project-groups` | admin | Body `{id, name (1–64), description?, department_id?}` → `201` group detail. `409 group.exists` |
| `GET` | `/api/project-groups/{group_id}` | admin | Group detail: the list shape plus `members[{user_id, username, email, account_role, status, role_key, added_by, added_at, sod_conflicts[{project_id, pairs, strict}]}]`, where `sod_conflicts` lists the group's projects in which that member combines duties |
| `PATCH` | `/api/project-groups/{group_id}` | admin | Any of `{name, description, department_id}` → detail; none of it changes access |
| `DELETE` | `/api/project-groups/{group_id}` | admin | Archives the group → detail + `{keys_disabled}`. The access the group gave stops now; direct members keep theirs, and the projects keep their `group_id`, so an unarchive gives the same access back |
| `POST` | `/api/project-groups/{group_id}/unarchive` | admin | → detail. Under `sod_strict` it is refused if a member would regain a combined duty |
| `PUT` | `/api/project-groups/{group_id}/members` | admin | Body `{upsert[{user_id, role_key}] (≤ 500), remove[user_id] (≤ 500)}` → `{added[], updated[], removed[]}` + detail. `role_key` is a built-in template only: custom roles are per project (`400 group.role_invalid`, `detail.templates`). `400 group.member_conflict` (upserted and removed in one request), `404 group.member_not_found`, `404 user.not_found`, `409 group.archived` |
| `PUT` | `/api/project-groups/{group_id}/projects` | admin | Body `{add[] (≤ 200), remove[] (≤ 200)}` → `{added[], removed[]}` + detail. A project in another active group must leave it first (`409 group.project_in_other_group`, `detail.group_id`). Taking a project out revokes the access the group gave there. `400 group.project_conflict`, `404 group.project_not_found` (not in this group), `404 project.not_found`, `409 group.archived` (adding to an archived group) |

**Department administrators.** A platform administrator appoints department
administrators (`PUT /api/departments/{department_id}/admins`) and opens workspaces to
the department (`PUT …/workspaces`); both stay admin-only, as do the department's own
create, edit, archive and delete. A department administrator is an ordinary account
that, for the projects and groups of a department they administer, passes the
admin-only and `pperm:project.members` project-structure routes without being a member
(`DEPARTMENT_ADMIN_ROUTES` in `core/route_policy.py`):

- `POST /api/projects` (the body's `department_id` must be theirs, `403 department.not_managed`; its group must be the department's; its workspaces must be open to the department, `403 department.workspace_not_open`; `settings.max_projects` caps the count, `409 department.project_limit`);
- `PATCH /api/projects/{project_id}` (name, department within theirs, group), `POST …/archive` / `…/unarchive`;
- `PUT /api/projects/{project_id}/workspaces` with open workspaces only, and without `make_default` (`403 auth.forbidden`);
- `PUT /api/projects/{project_id}/members`, `DELETE …/members/{user_id}`, `GET …/candidates`, `PUT` / `DELETE …/roles/{role_key}`;
- every `/api/project-groups` route: `GET /api/project-groups` only with `?department_id=` set to their department, `POST` with the department named in the body, and the group routes for the department's groups (`PUT …/projects` takes only the department's projects);
- `POST /api/departments/{department_id}/invitations`, when the department's `settings.can_create_users` is on.

Another department's project answers `404 project.not_found` and its group
`404 group.not_found`, so its existence is not disclosed; a non-read call on an archived
project of their own department answers `409 project.archived` (archive / unarchive
excepted). They see the department's projects in `GET /api/projects`, but not their data:
workspace-scoped routes still need a project membership. The console reads
`GET /api/me/access` → `managed_departments[{id, name, workspaces}]` to show them the
Configure pages (projects, project groups, departments) and to fill the pickers, and
`GET /api/projects/{project_id}` → `can_manage` to show the structure controls.

## Console Network Profiles API

A **network profile** is a workspace VPC setting, kept by administrators, that a
project's agents may run in. An agent spec names it as `network: {profile_id}`, and
the deployer resolves it to the profile's subnets and security groups. Each project
has an **allowed set** of profiles per workspace, with at most one default. A
profile outside the request project's allowed set reads as missing
(`404 network_profile.not_found`), exactly like an id that does not exist.
Administrators see every profile of the workspace. A new profile is
`status: unchecked` until the preflight (`POST …/{profile_id}/check`) fills
`check_results` / `checked_at` / `status`. Only the preflight and the probe call AWS.
The routes
are workspace-scoped (`X-Workspace`). Reads run inside the request's project. Writes
and the per-project allowed set are admin and project-exempt, so the allowed-set
routes name their project in the path. Writes are journaled as workspace-level audit
events: `network_profile.create`, `.update`, `.archive`, `.project_set`, `.check`,
`.probe` and `.settings`.

A profile is `{id, workspace_id, name, vpc_id, subnet_ids[], security_group_ids[],
purpose: agents|tools|both, internet: none|nat, reachable[{cidr|host, port, note?}],
status, check_results, checked_at, legacy, created_by, created_at, updated_at,
archived_at, allowed, is_default}`. The fields mean the following:

- `id` is `np-<12 hex>`, or `legacy-<hash8>` (`legacy: true`) for a profile filed from an older inline spec.
- `allowed` and `is_default` are relative to the request's project.
- The administrator view adds `projects[{project_id, is_default}]` (every project allowed to use it) and `agents` (the live agents of any project naming it).

| Method | Path | Role | Result |
|---|---|---|---|
| `GET` | `/api/network-profiles?include_archived=` | member | `{profiles[…]}` sorted by name. A member gets the current project's allowed set (empty when it has none). An administrator gets every live profile of the workspace, and archived ones too with `include_archived=true` |
| `GET` | `/api/network-profiles/{profile_id}` | member | One profile; for a member, only when it is in the project's allowed set |
| `POST` | `/api/network-profiles` | admin | Body (`extra="forbid"`) `{name (1–64), vpc_id (vpc-…), subnet_ids (2–8 subnet-…; AgentCore wants two AZs), security_group_ids (1–5 sg-…), purpose? (default agents), internet? (default none), reachable? (≤ 50 `{cidr` or `host, port 1–65535, note? ≤ 200}`)}` → `201` profile (`status: unchecked`, `projects: []`, `agents: 0`). Duplicate ids are dropped. The name is unique among the workspace's live profiles |
| `PATCH` | `/api/network-profiles/{profile_id}` | admin | Any subset of the create fields → the administrator view. Changing `vpc_id`, `subnet_ids`, `security_group_ids` or `internet` moves the profile back to `unchecked` and clears `check_results` / `checked_at` |
| `DELETE` | `/api/network-profiles/{profile_id}` | admin | `204`. Archives the profile (the row stays because snapshots may still name it) and removes it from every project's allowed set. Refused while a live agent names it, because its runtime's ENIs sit in these subnets |
| `GET` | `/api/network-profiles/projects/{project_id}` | admin | `{project_id, workspace_id, profiles[…], default_profile_id}`: the project's allowed set in the request's workspace |
| `PUT` | `/api/network-profiles/projects/{project_id}` | admin | Body `{profile_ids[] (≤ 50), default_profile_id?}` → the GET shape. Replaces the project's allowed set and its default in this workspace |
| `POST` | `/api/network-profiles/{profile_id}/check` | admin | Preflight (design projects §12.4) → the administrator view with `check_results[{check, severity: ok\|warn\|block, message, details}]`, `checked_at` and `status` (`blocked` > `warn` > `ok`). It reads the VPC through the workspace's own read-only `ec2:Describe*` calls: subnets exist, sit in the VPC and in at least two supported AZs, have free IPs; security groups exist and allow egress on 443; VPC DNS; NAT routes when `internet: nat`; network ACLs; the interface / gateway endpoints AgentCore needs (blocking only on what every agent needs, warning on the rest); and routes to the declared `reachable` targets (presence and next-hop type only; the far side is reported as not verified by the platform). The last probe result is kept. `502 network_profile.check_failed` (`detail.aws_code`) when AWS cannot be read; an access denial names the spoke template update that grants the calls |
| `POST` | `/api/network-profiles/{profile_id}/probe` | admin | `202` → the administrator view with a `probe` entry `{status: running}` in `check_results`. In the background, a temporary runtime starts in the profile's subnets and security groups and tries DNS, TCP and TLS to every `reachable` target (§12.7). The entry then becomes `status: done` (`severity: warn` when a target is unreachable, `details.targets[]`) or `failed`. A network edit while it runs drops the late answer. `400 network_profile.nothing_to_probe` (no `reachable` targets), `409 network_profile.blocked` (the last preflight blocks), `409 network_profile.probe_running` (a probe started less than 30 minutes ago is still running) |
| `GET` | `/api/network-profiles/settings` | admin | `{network_enforcement, network_public_allowed}` of the workspace (defaults `false` / `true`) |
| `PUT` | `/api/network-profiles/settings` | admin | Body (`extra="forbid"`) `{network_enforcement?, network_public_allowed?}` → the GET shape; an omitted or `null` key keeps its value. `network_public_allowed: false` means every agent must name a profile. Only the platform can enforce it, since no IAM condition key expresses the network mode; the deploy-time network gate refuses an agent without one (`400 network.public_not_allowed`). `network_enforcement` records that the workspace means to hold AgentCore to the approved subnets and security groups at the IAM layer; the platform does not write that policy itself (see the next route) |
| `GET` | `/api/network-profiles/enforcement` | admin | `{settings, approved_subnet_ids[], approved_security_group_ids[], stack_parameters{ApprovedSubnetIds, ApprovedSecurityGroupIds}, policy, actions[], not_enforceable[], not_enforceable_codes[]}`. The approved sets are the subnets and security groups of every live profile not blocked by its last preflight. `stack_parameters` are the comma-joined values for the spoke stack (`infra/spoke/launchpad-workspace-role.yaml`), whose `EnforceNetwork` condition adds a `launchpad-network-enforcement` Deny to the workspace role once `ApprovedSubnetIds` is non-empty. `policy` is the equivalent document to apply as an SCP instead: Deny `bedrock-agentcore:CreateAgentRuntime`, `UpdateAgentRuntime`, `CreateCodeInterpreter` and `CreateBrowser` when `ForAnyValue:StringNotEquals` on `bedrock-agentcore:subnets` / `bedrock-agentcore:securityGroups` (an empty approved set denies every VPC placement). `not_enforceable` names the gaps: the Harness actions carry neither key, and no key expresses `networkMode`, so a PUBLIC request still passes at the IAM layer; `not_enforceable_codes` are stable codes index-aligned with it, which the console localizes |

`POST /api/agents` and `POST /api/agents/{id}/redeploy` normalize `spec.network`:

- A `{profile_id}` must be in the project's allowed set (`404 network_profile.not_found`) and must not be blocked by its last preflight (`409 network_profile.blocked`).
- Inline `{subnets, security_groups}` from an older client is accepted from administrators only (`400 network.inline_not_allowed` otherwise). The administrator's inline network is matched to, or filed as, the `legacy-<hash8>` profile and added to the project's allowed set.
- A redeploy keeps resolving the profile the agent already names, whatever its status.

Then, before any job exists, the deploy-time network gate (`pipeline.network_gate`,
also on `POST /api/agents/{id}/convert`) refuses what cannot work. Every deploy method
deploys into the named profile, so this applies to all of them:

- no network while the workspace sets `network_public_allowed: false` → `400 network.public_not_allowed`;
- a profile is preflighted again for this agent's features (container image pulls, Bedrock models, memory, Gateway tools, Identity, A2A, knowledge bases). Any blocking item → `409 network.preflight_failed`, `detail {profile_id, results[blocking items]}`. A `legacy-<hash8>` profile is not preflighted.

`GET /api/agents/{agent_id}/network` reads where an agent runs (see *Console Agents API — versions and endpoints*).

Error codes: `network_profile.not_found` (404, unknown, archived, another workspace's
profile, or outside the allowed set), `network_profile.name_exists` (409,
`detail.profile_id`), `network_profile.in_use` (409, `detail.agents`),
`network_profile.default_not_allowed` (400, the default is not among `profile_ids`),
`network_profile.check_failed` (502), `network_profile.nothing_to_probe` (400),
`network_profile.probe_running` (409), `network.public_not_allowed` (400),
`network.preflight_failed` (409), `project.not_found` (404, the project is not bound to the workspace), and
`validation.invalid_request` (422, a body outside the schema or a malformed
VPC / subnet / security-group id).

## Console Storage Catalog API

The **storage catalog** lists S3 Files and EFS access points an administrator registers
per workspace, so a project's agents can mount them by id instead of naming an ARN
(design storage-mounts §4). Like network profiles, each project has an **allowed set**
per workspace, and an entry outside the request project's allowed set reads as missing
(`404 storage.not_found`). Administrators see every entry, with the shared view: the
projects allowed to use it and the live agents mounting it. The routes are
workspace-scoped (`X-Workspace`); reads run inside the request's project, while writes
and the allowed set are admin and project-exempt. Writes are journaled as
workspace-level audit events: `storage.register`, `.update`, `.archive`, `.check`,
`.project_set`.

An entry is `{id, workspace_id, name, kind: s3files|efs, access_point_arn,
file_system_arn, bucket_arn, posix_uid, posix_gid, root_directory, mount_target_azs[],
mount_targets[], vpc_id, network_profile_id, max_mode: ro|rw, data_class:
reference|shared_state|scratch, origin, status: unchecked|ok|warn|blocked,
check_results, checked_at, created_by, created_at, updated_at, archived_at, allowed}`.
`allowed` is relative to the request's project. The administrator view adds
`projects[]`, `agents[{agent_id, name, project_id, version, status, endpoint_mode, mode,
mount_path}]`, `writers` and `multi_writer` (more than one read-write agent: S3 Files is
close-to-open consistent between NFS clients only, so several writers need their own
locking or directory split).

| Method | Path | Role | Result |
|---|---|---|---|
| `GET` | `/api/storage-mounts?include_archived=` | member | `{storage[…]}` by name: a member gets the current project's allowed set, an administrator every live entry (archived ones too with `include_archived=true`) |
| `POST` | `/api/storage-mounts` | admin | Body (`extra="forbid"`) `{id, name? (≤ 64, default the id), access_point_arn, max_mode? (default ro), data_class? (default reference), network_profile_id?, check? (default true)}` → `201` the administrator view (`status: unchecked`). `id` follows the project id rule (`^[a-z0-9][a-z0-9-]{1,31}$`). With `check` the check runs right away; if it fails the entry is registered anyway and the answer carries `check_error{code, message}`. `409 storage.id_exists` (a live entry has this id), `422 storage.invalid_access_point` (not an S3 Files or EFS access point ARN), `404 network_profile.not_found` |
| `GET` | `/api/storage-mounts/{storage_id}` | member | One entry; for a member only when it is in the project's allowed set |
| `GET` | `/api/storage-mounts/{storage_id}/usage` | member | `{storage_id, agents[…], writers, multi_writer}`: a member sees their own project's agents only, and the workspace-wide writer count |
| `PATCH` | `/api/storage-mounts/{storage_id}` | admin | Any of `{name, max_mode, data_class, network_profile_id ("" clears it)}` → the administrator view. The access point is immutable. Lowering `max_mode` to `ro` while an agent mounts it read-write → `409 storage.mode_exceeds_max` (`detail.agents`). A new default profile moves the entry back to `unchecked` |
| `DELETE` | `/api/storage-mounts/{storage_id}` | admin | `204`. Archives the entry (the row stays, since snapshots may name it) and removes it from every allowed set. The platform never deletes the storage itself. `409 storage.in_use` (`detail.agents`) while a live agent mounts it |
| `POST` | `/api/storage-mounts/{storage_id}/check` | admin | → the administrator view with `check_results[{check, severity: ok\|warn\|block, message, details}]`, `checked_at`, `status`. Reads the access point, its file system and mount targets with the workspace's read-only describe calls (S3 Files `GetAccessPoint` / `GetFileSystem` / `ListMountTargets` / `GetMountTarget`, or EFS `Describe*`) and fills the POSIX user, root directory, mount-target AZs and VPC. With a default network profile it also runs the preflight's network checks against it. `502 storage.check_failed` (`detail.aws_code`; an access denial names the spoke template update) |
| `GET` | `/api/storage-mounts/projects/{project_id}` | admin | `{project_id, workspace_id, storage[…]}`: the project's allowed set in the request's workspace. `404 project.not_found` when the project is not bound to it |
| `PUT` | `/api/storage-mounts/projects/{project_id}` | admin | Body `{storage_ids[] (≤ 50)}` → the GET shape. Replaces the allowed set. Taking an entry away from a project whose agents mount it is allowed; their next publish is refused until the mount is removed or the entry is allowed again |
| `POST` | `/api/storage-mounts/preflight` | `perm:project.use` | Body `{mounts[{storage_id, mount_path?, mode?: ro\|rw}] (1–4), network_profile_id?, method?, byoc_artifact_kind?}` → `{status: ok\|warn\|blocked, results[{check, severity, message, details, storage_id}]}`. The wizard's check of a spec's catalog mounts against its network (§5), writing nothing: the network profile is required (S3 Files / EFS need a VPC), then same VPC, a mount target in every subnet's AZ, NFS (TCP 2049) out of the agent's security groups and into the mount targets', the mode within `max_mode`, plus method and release-mode notes. A failed EC2 read degrades the network checks to warnings. An entry the project may not use → `404 storage.not_found` |
| `GET` | `/api/storage-mounts/agents/{agent_id}` | member | The agent detail page's storage card: `{agent_id, version, mounts[{storage_id, mount_path, mode, entry{name, kind, status, max_mode, data_class, archived, checked_at, blocking, warnings} \| null, missing}], inline[{kind, access_point_arn, mount_path, mode: rw}], session_storage}`. Another project's agent → `404 agent.not_found` |

An agent spec mounts an entry as `filesystem.mounts[{storage_id, mount_path? (default
/mnt/<storage_id>), mode? (default ro)}]`; together with the inline `s3_files` / `efs`
mounts an agent has at most 4, each entry once, all mount paths unique. On
`POST /api/agents` and redeploy each mount must be in the project's allowed set
(`404 storage.not_found`), not blocked by its last check (`409 storage.blocked`) and
within `max_mode` (`400 storage.mode_exceeds_max`); the server stamps the entry's `kind`
and `access_point_arn` into the stored spec, replacing whatever the client sent. The
deploy-time preflight refuses a blocking result before any job exists
(`409 storage.preflight_failed`, `detail.results`). The execution role gets
`ClientMount` for every mount and `ClientWrite` only for `rw` ones. Inline access-point
mounts stay administrator-only (`400 storage.inline_not_allowed`, `detail.mount_paths`).
On promotion the catalog id is the logical name (resource mapping kind `storage`): the
target workspace's entry of the same id is used unless a mapping row names another, and
it must be live and allowed for the project, else the release is refused with
`409 storage.unmapped`; inline access points carry the source account and are refused
with `409 storage.inline_not_promotable`.

## Console System Update API — online update

Online console update (online-update §2): the console fetches the configured source,
prepares the new version beside the running one, and an administrator schedules the
restart; the host's updater swaps it in (`backend/scripts/install_updater.sh`). Hub-global
(`/api/system`, no `X-Workspace`); every route but the version read is an
administrator's. No body names a repository or a branch: the source is deployment
configuration (bodies are `extra="forbid"`). Each step is journaled as a
`system.update.*` structure event.

An update row is `{id, kind: update\|rollback, target_sha, short, target_time, base_sha,
behind, commits[], status: preparing\|prepare_failed\|ready\|scheduled\|applying\|done\|rolled_back\|failed\|superseded,
staged_dir, progress{}, steps[], scheduled_at, requested_by, announcement_id, error,
previous_sha, finished_at, created_at, updated_at}`.

| Method | Path | Auth | Result |
|---|---|---|---|
| `GET` | `/api/system/version` | member | `{sha, short, committed_at, built_at, updater_installed, update_enabled, can_schedule}`: the running version and whether this host can update online |
| `GET` | `/api/system/update` | admin | `{version, source{repo, branch}, remote, current, history[], inflight, manual_commands}`: the running version, the configured source, the last remote check (cached ten minutes; `null` while the feature is off), the newest update row, the last 10 rows, the work a restart would interrupt (while the newest row is `ready` / `scheduled`), and the commands for a manual update (a `ready` row on a host without the updater) |
| `POST` | `/api/system/update/check` | admin | Fetches the source now → the remote check `{repo, branch, checked_at, sha, short, committed_at, running_sha, behind, fast_forward, up_to_date, commits[]}`. `502 update.check_failed`, `409 update.disabled`, `409 update.no_git` |
| `POST` | `/api/system/update/prepare` | admin | `202` the new update row (`preparing`): builds the newest fast-forward commit beside the running one, resumable after a restart. `409 update.up_to_date`, `409 update.not_fast_forward`, `409 update.in_progress`, `409 update.disk_low`, `409 update.disabled` |
| `POST` | `/api/system/update/rollback` | admin | `202` a `rollback` row preparing the previous version. `409 update.no_previous`, `409 update.not_fast_forward` (the previous version is not an ancestor), `409 update.in_progress` |
| `POST` | `/api/system/update/{update_id}/schedule` | admin | Body `{at?: datetime (omitted = now, after a 60-second countdown), acknowledge_inflight: bool}` → the row (`scheduled`); an announcement tells signed-in users. `409 update.updater_missing`, `409 update.not_ready`, `409 update.stage_missing`, `409 update.schedule_invalid`, `422 update.inflight_unacknowledged` (in-progress work would be interrupted; `detail` lists it) |
| `POST` | `/api/system/update/{update_id}/cancel` | admin | A `scheduled` row goes back to `ready` and its announcement is withdrawn → the row. Any other status → `409 update.not_cancellable` |
| `GET` | `/api/system/update/{update_id}/log` | admin | `{id, status, events[]}`: the update's step log. `404 update.not_found` |

## Console route index — the remaining routes

The sections above document the larger surfaces in depth. This index lists every other
route in `ROUTE_POLICY` (`backend/app/core/route_policy.py`), so that each classified
route appears somewhere in this file; `backend/scripts/check_api_docs.py` reports any
that do not. For the behavior behind a row, see the matching section of
[architecture.md](architecture.md).

**Columns.** *Role* is the `ROUTE_POLICY` value:

- `public` needs no session, `member` any live session, `admin` an administrator.
- `perm:<key>` needs `<key>`. On a project-scoped route the key is checked among the caller's effective keys in the request's project (see *Console Projects API*), otherwise among the account's own keys.
- `infra` / `infra+perm:<key>` changes workspace-shared infrastructure. While the workspace holds one active project it needs `project.write` (or `<key>`) in that project. Once it holds several, only an administrator passes (`403 auth.infra_admin_required`, `detail.projects`).

*Scope* says how the route is resolved:

- `hub`: hub-global. It reads neither `X-Workspace` nor `X-Project`.
- `ws`: workspace-scoped (`X-Workspace`) but in `PROJECT_EXEMPT`. It needs no project and sees workspace-level data only.
- `project`: workspace-scoped and run inside the request's project (`X-Project`, or the fallback described under *Console Projects API*). Lists hold only that project's rows (plus platform rows such as system presets), and an id of another project answers that kind's ordinary `404`.

### Health, docs and static catalogues

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `GET` | `/api/health` | public | hub | `{status: "ok", version, region}` |
| `GET` | `/api/docs` | public | hub | The Swagger UI page |
| `GET` | `/api/openapi.json` | public | hub | The OpenAPI document |
| `GET` | `/api/agent-templates` | member | hub | `{templates[…]}`: the wizard's scenario-template catalogue (T10). Static data, applied client-side |
| `GET` | `/api/models/defaults` | member | hub | The effective model id per role (configured value or console override) |
| `GET` | `/api/models/settings` | admin | hub | `{roles[…]}`: each model role with its configured default and console override |
| `PUT` | `/api/models/settings/{field}` | admin | hub | Body `{model_id}` → the role row. Sets the console override for one role |
| `DELETE` | `/api/models/settings/{field}` | admin | hub | → the role row. Clears the override; the configured default applies again |
| `GET` | `/api/model-catalog?refresh=` | member | ws | `{region, models[…], error}`: the Bedrock models callable in the workspace Region, cached (`refresh=true` re-reads) |
| `POST` | `/api/model-probe` | admin | ws | Body `{field, model_id}` → `{model_id, region, ok, checks[…]}`: whether that model works for that role in the workspace |

### Workspaces

See [architecture.md](architecture.md#workspaces--multi-accountmulti-region-environments).
A workspace is `{id, name, account_id, region, cross_account, bootstrap_status, tier,
is_default, resource_visibility, created_at, updated_at, role_arn}`. `external_id` is
never returned. Deleting or purging a workspace also removes its project bindings.

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `POST` | `/api/workspaces` | admin | hub | Body `{id, name, account_id, region, role_arn?, external_id?, tier?}` → `201` workspace (`bootstrap_status: registered`). `400 workspace.reserved_id` / `workspace.invalid_id`, `409 workspace.exists` / `workspace.environment_taken` (that account and Region already have a workspace) |
| `GET` | `/api/workspaces/hub-identity` | admin | hub | `{account_id, caller_arn, role_arn}`: the values a spoke stack trusts. `502 workspace.hub_identity_unavailable` |
| `POST` | `/api/workspaces/preflight` | admin | hub | Body `{account_id, region, role_arn, external_id}` → `{ok, caller_account, diagnostic}`. One AssumeRole + `GetCallerIdentity`; a refused AssumeRole is `ok: false` (200), not an error. Writes nothing |
| `PATCH` | `/api/workspaces/{workspace_id}` | admin | hub | Any of `{name, tier, confirm_tier_change}` → workspace. A move into or out of `prod` needs `confirm_tier_change: true` (`409 workspace.tier_change_unconfirmed`) and is journaled as `workspace.tier_change` |
| `DELETE` | `/api/workspaces/{workspace_id}` | admin | hub | `{deleted: true, workspace_id}`. Detach only: AWS is untouched. Refused for `default` (`400 workspace.reserved_id`) and while any scoped row names the workspace (`409 workspace.in_use`, `detail.rows`). Grants, project bindings and promotions aimed at it go with it |
| `POST` | `/api/workspaces/{workspace_id}/bootstrap` | admin | hub | `202 {job_id, workspace_id, bootstrap_status, stages}`. Poll `GET /api/jobs/{job_id}`. `400 workspace.default_not_bootstrappable`, `409` while bootstrapping or ready |
| `GET` | `/api/workspaces/{workspace_id}/bootstrap` | admin | hub | `{workspace_id, bootstrap_status, job: {id, status, stages} \| null}`: the latest bootstrap run |
| `POST` | `/api/workspaces/{workspace_id}/observability/repair` | admin | hub | Re-runs the observability stage (Transaction Search) for a ready workspace → `{workspace_id, …}`. `409 workspace.not_ready` |
| `GET` | `/api/workspaces/{workspace_id}/role-template?refresh=` | admin | hub | `{workspace_id, status: current\|outdated\|newer\|not_applicable\|unknown, deployed, expected, variant, template, stack_command, reason?}`: whether the spoke role stack runs the current template. It reads the role's `launchpad:template-version` tag through `GetRole` on itself. `outdated` with `reason: unreadable` means the role cannot read itself, which is a stack from before the tag. `not_applicable` means a same-account workspace. The answer is cached for 10 min; `refresh=true` re-reads it. An AssumeRole failure returns `502 workspace.assume_role_failed` |
| `POST` | `/api/workspaces/{workspace_id}/purge?dry_run=` | admin | hub | `{purged, dry_run, workspace_id, rows{table: count}, resource_keys[]}`. Deletes a `registered` or `failed`, agent-free, non-`default` workspace with all its rows in one transaction; `dry_run=true` only reports |
| `GET` | `/api/workspaces/{workspace_id}/grants?q=&granted=all\|granted\|ungranted&limit=&offset=` | admin | hub | `{workspace_id, users[{id, username, email, role, status, granted}], total, granted_total}`: the member accounts and whether each holds this workspace. Administrators are absent. `400 workspace.invalid_grant_filter` |
| `PUT` | `/api/workspaces/{workspace_id}/grants` | admin | hub | Body `{grant[user_id], revoke[user_id]}` → `{workspace_id, added, removed, granted_total}`. Writes `user_workspaces` and, in the same transaction, the default project's `inherit` memberships (see *Projects* in architecture.md); a revoked member's API keys in this workspace are disabled. `400 workspace.grant_conflict` / `workspace.invalid_grant_targets` (unknown id or an administrator) |

### Agents: lifecycle, discovery, snapshots

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `DELETE` | `/api/agents/{agent_id}` | `perm:agents.delete` | project | `{deleted: true, agent_id, aws_resource_deleted}`. Prod-protected. `409 assistant.agent_clearing` while its conversation is being cleared |
| `GET` | `/api/agents/discovery` | member | project | `{region, runtimes[…], harnesses[…], harness_scan_error}`: the Runtimes and Harnesses in the workspace account, each marked with the managing agent when one exists. Another project's managed runtime shows only that it is managed. Runtimes no agent manages are listed only for a caller holding `agents.import` |
| `POST` | `/api/agents/discovery/import` | `perm:agents.import` | project | Body `{runtime_ids[], harness_ids[], project_id?}` → per-kind result buckets (imported, already managed, failed). Imported agents belong to the request's business project (see below). Prod-protected |
| `GET` | `/api/agents/eligible-projects?permission=agents.deploy` | member | workspace | `{projects[{id, name, department_id, department_name, is_workspace_default}]}`: the business projects (active, not `default`, with a department) where the caller holds `permission` (`agents.deploy` / `agents.import` / `agents.convert`, else `422 agent.project_permission_invalid`); an administrator sees all of them. Feeds the create form's project picker |
| `POST` | `/api/agent-skills/import` | `perm:agents.deploy` | project | Body `{staging_id, selections[]}` → attachable `{name, path}` entries for the deploy wizard, uploaded to a per-request S3 prefix. A per-item failure does not abort the batch. Prod-protected |
| `GET` | `/api/agents/{agent_id}/snapshots` | member | project | `{snapshots[…]}`: the ledger spec snapshots, newest first, without specs |
| `GET` | `/api/agents/{agent_id}/snapshots/{seq}` | member | project | One snapshot with its `spec` |
| `GET` | `/api/agents/{agent_id}/snapshots/diff?from_seq=&to_seq=` | member | project | `{from, to, changes[…]}`: a field-level difference of two snapshots' specs |
| `POST` | `/api/agents/{agent_id}/snapshots/{seq}/rollback` | `perm:agents.deploy` | project | `202` like a redeploy: the stored spec is published again as a new version. `409 snapshot.spec_invalid` when it no longer validates. Prod-protected |
| `GET` | `/api/agents/{agent_id}/suggested-questions?lang=` | member | project | Three to five starter questions for the try-chat panel; cached, never a 500 when the model fails |
| `GET` | `/api/chat/{agent_id}/memory?session_id=` | member | project | The memory summary of one chat session plus `actor_id` (the compound `<agent>__<human>` actor). `502 memory.unavailable` |
| `POST` | `/api/chat/{agent_id}/feedback` | `perm:project.use` | project | Body `{session_id, message_id, verdict: up\|down, comment?}` → `{message_id, verdict}`. Records console thumbs feedback |

**New agents and their project.** `POST /api/agents` (`AgentSpec.project_id`, never
stored in the spec and ignored on redeploy), `POST /api/agents/{agent_id}/convert`
(optional body `{project_id}`), `POST /api/agents/discovery/import` and
`POST /api/assistant/architect/conversations/{id}/proposal/approve` (`project_id` in the
body) file the new agent under the body's `project_id`, else the request's project. The
project must be reachable by the caller, bound to the workspace, `active`, not a
workspace `default` project and carry a department, and the caller must hold the route's
key there. Refusals: `422 agent.project_mismatch` (body and `X-Project` differ),
`422 agent.project_required`, `422 agent.project_not_found` (missing or not reachable),
`422 agent.project_default_not_allowed`, `422 agent.project_archived`,
`422 agent.project_no_department` — each with `next: projects.open`. A promotion that
creates its target agent applies the same rule to its project in the target workspace
(`422` at execute, a failed stage in the worker). System presets are exempt.

### Release bundles, promotions and resource mappings

See [architecture.md](architecture.md#the-dev--ops-hand-off-operator-role-release-bundles-promotions-inbox-roadmap-t19t22).
A bundle is `{id, agent_id, agent_name, display_name, method, snapshot_seq, digest,
artifact, evaluation, policy, workspace_id, created_by, note, created_at}`. A
promotion is `{id, bundle_id, bundle, source_workspace_id, target_workspace_id,
status, gates, stages, action, job_id, previous_bundle_id, change_note,
rollback_note, requested_by, reviewed_by, reviewed_at, review_note, started_at,
finished_at, failed_stage, error, created_at, updated_at}`. A promotion keeps its
project: the target workspace must be bound to the bundle's project, otherwise
`404 project.not_found`.

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `GET` | `/api/agents/{agent_id}/release-bundles` | member | project | `{bundles[bundle]}`, newest first |
| `POST` | `/api/agents/{agent_id}/release-bundles` | `perm:promotion.request` | project | Body `{snapshot_seq?, note?}` → `201` bundle: the publish frozen as an immutable, digest-identified bundle |
| `GET` | `/api/release-bundles/{bundle_id}` | member | project | One bundle. `404 promotion.bundle_not_found` |
| `GET` | `/api/release-bundles/{bundle_id}/export` | member | project | The bundle as `application/yaml` (`<agent>.release.yaml`, header `X-Bundle-Digest`) |
| `GET` | `/api/release-bundles/{bundle_id}/resolution?target_workspace_id=` | member | project | `{target_workspace_id, complete, resolved[…], unmapped[…], spec}`: what promoting into the target would rewrite. Read-only; the caller must reach the target in this project |
| `GET` | `/api/promotions?status=&target=` | member | project | `{promotions[promotion]}` raised from this workspace, newest first. `target_visible` says whether the caller is granted on the target |
| `POST` | `/api/promotions` | `perm:promotion.request` | project | Body `{bundle_id, target_workspace_id, change_note?, rollback_note?}` → `201` promotion |
| `GET` | `/api/promotions/{promotion_id}` | member | project | The promotion plus `target_agent {id, status, version} \| null`, `target_name_taken` (the name is held by another project's agent in the target) and `diff[…]` against the target; target fields are blank for a caller not granted on the target |
| `GET` | `/api/promotions/{promotion_id}/plan` | member | project | What executing would create or change in the target. Read-only |
| `POST` | `/api/promotions/{promotion_id}/review` | `perm:promotion.approve` | project | Body `{decision: approve\|reject, note?}` → promotion. The requester never reviews their own (`promotion.self_approval`). Journaled |
| `POST` | `/api/promotions/{promotion_id}/execute` | `perm:promotion.approve` | project | `202 {promotion, job_id}`. Refused while a blocking gate fails (`409 promotion.gate_failed.<gate>`), `409 promotion.already_executing` / `promotion.not_executable`. Journaled |
| `GET` | `/api/promotions/{promotion_id}/execution` | member | project | `{promotion, log[…]}`: per-stage progress and the target job's log, for a caller granted on the target |
| `POST` | `/api/promotions/{promotion_id}/rollback` | `perm:promotion.approve` | project | `202`: re-deploys the bundle the target ran before. `409 promotion.not_rollbackable` / `promotion.no_previous_bundle` / `promotion.already_executing` |
| `GET` | `/api/release-policies/{workspace_id}` | member | ws | `{workspace_id, tier, policy, effective}`: a target workspace's release policy |
| `GET` | `/api/inbox` | admin | ws | `{items[{key, severity, count, to, sample[]}]}`: pending accounts, expiring accounts, workspaces needing attention, and the selected workspace's failed jobs and pending promotions |
| `GET` | `/api/resource-mappings` | member | project | `{workspace_id, kinds[], mappings[{id, key, kind, name, resource_id, note, scope: project\|workspace, project_id, workspace_id, updated_by, updated_at}]}`: the project's mappings and the workspace defaults |
| `PUT` | `/api/resource-mappings/{kind}/{name}?scope=project\|workspace` | `perm:promotion.approve` | project | Body `{resource_id, note?}` → mapping. `scope=project` (default) writes this project's mapping; `scope=workspace` writes the default every project falls back to and needs an administrator in a multi-project workspace (`403 mapping.workspace_scope_forbidden`). A `resource_id` naming another project's knowledge base or memory reads as missing. Journaled |
| `DELETE` | `/api/resource-mappings/{kind}/{name}?scope=` | `perm:promotion.approve` | project | `{deleted: true, key}`. `404 mapping.not_found` |

### Share, review and channel links; the `/share` surface

See [architecture.md](architecture.md#share-links-external-pages-and-thumbs-feedback-roadmap-t13t15).
A link is `{id, kind, agent_id, label, prefix, state: active|revoked|disabled|expired,
created_by, expires_at, revoked_at, last_used_at, use_count, created_at}`, plus
`channel` (platform and which secrets are set, never a value) on a channel link. The
raw token is returned once, at creation; only its hash is stored. A link belongs to
its agent's project: it stops resolving (`404`, like a revoked token) when the agent
leaves that project or the project is archived, and archiving revokes the project's
links. The `/share/*` routes run inside the link's project, ignore `X-Project`, and
are rate limited per link.

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `GET` | `/api/agents/{agent_id}/share-links` | member | project | `{links[link]}` of every kind for the agent, newest first |
| `POST` | `/api/agents/{agent_id}/share-links` | `perm:project.write` | project | Body `{label?, expires_in_days? (1–365)}` → `201` link + `{token, path, url, embed_url, embed_snippet}` |
| `GET` | `/api/agents/{agent_id}/review-links` | member | project | `{links[link]}` of kind `review` |
| `POST` | `/api/agents/{agent_id}/review-links` | `perm:project.write` | project | Body `{label?, expires_in_days?}` → `201` link + `{token, path, url}`: an SME review page |
| `POST` | `/api/agents/{agent_id}/channel-links` | `perm:project.write` | project | Body `{platform: slack\|feishu, label?, expires_in_days?, credentials{…}}` → `201` link + `{token, path, url}`: the webhook URL to configure in the IM platform. `422 channel.unsupported` / `channel.invalid_setup` |
| `POST` | `/api/share-links/{link_id}/revoke` | `perm:project.write` | project | → link. Idempotent: the first revocation time stands |
| `GET` | `/share/{token}?embed=` | public | hub | `{kind, embed, label, agent{display_name}, expires_at}` |
| `POST` | `/share/{token}/chat` | public | hub | Body `{prompt, session_id?}` → SSE of the visitor-visible events. A new session gets an anonymous actor derived from the link; a `session_id` must be one this link started |
| `POST` | `/share/{token}/feedback` | public | hub | Body `{session_id, message_id, verdict, comment?}` → `{message_id, verdict}` (source `share`) |
| `GET` | `/share/review/{token}` | public | hub | `{kind, label, agent{display_name}, expires_at, items[…]}`: the answers queued for review |
| `POST` | `/share/review/{token}/rate` | public | hub | Body `{message_id, verdict, comment?, correction?}` → `{message_id, verdict}` (source `review`) |
| `POST` | `/share/channels/{platform}/{token}` | public | hub | The IM platform's inbound webhook. The signature is verified per platform; a handshake is answered inline, a message is acknowledged at once (`{ok: true}`, `duplicate: true` for a redelivered event) and answered out of band |

### Business self-service: feedback, issues, intents, curated answers

See [architecture.md](architecture.md#business-self-service-intents-sme-review-curated-answers-issue-box-roadmap-t33t36).
An issue is `{id, agent_id, agent_name, session_id, message_id, kind, status:
open|fixed|wont_fix, question, answer, comment, correction, opened_by, fixes[…],
history[…], resolved_by, resolved_at, resolution_note, hours_to_close, created_at,
updated_at}`. A curated answer (rule) is `{id, agent_id, position, name, match,
pattern, answer, enabled, created_by, updated_by, source_issue_id, hit_count, …}`.

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `GET` | `/api/feedback?verdict=up\|down&agent_id=&limit=` | member | project | `{items[…], counts, down_session_ids[]}`: thumbs feedback from the console, share and review pages |
| `GET` | `/api/issues?status=&agent_id=&limit=` | member | project | `{items[issue], summary}` |
| `POST` | `/api/issues` | `perm:project.write` | project | Body `{agent_id, session_id, message_id, kind, note?}` → `201` issue |
| `POST` | `/api/issues/sync` | `perm:project.write` | project | `{opened}`: opens an issue for every thumbs-down that has none (idempotent) |
| `GET` | `/api/issues/{issue_id}` | member | project | Issue + `transcript[…]` |
| `POST` | `/api/issues/{issue_id}/fixes` | `perm:project.write` | project | Body `{action, ref?, note?}` → issue: records what was done about it |
| `POST` | `/api/issues/{issue_id}/resolve` | `perm:project.write` | project | Body `{status: fixed\|wont_fix, note?}` → issue |
| `POST` | `/api/issues/{issue_id}/reopen` | `perm:project.write` | project | Body `{note?}` → issue |
| `GET` | `/api/intents?agent_id=&days=1–30&lang=&refresh=` | member | project | `{generated_at, …}`: the project's chat turns clustered into user intents, cached per project |
| `GET` | `/api/agents/{agent_id}/rules` | member | project | `{agent_id, enabled, rules[rule], limit}` |
| `POST` | `/api/agents/{agent_id}/rules` | `perm:project.write` | project | Body `{name, match, pattern, answer, enabled?, issue_id?}` → `201` rule; with an open `issue_id` the rule is recorded as that issue's fix. `409 rule.limit_reached` |
| `PATCH` | `/api/agents/{agent_id}/rules/{rule_id}` | `perm:project.write` | project | Any of `{name, match, pattern, answer, enabled}` → rule |
| `DELETE` | `/api/agents/{agent_id}/rules/{rule_id}` | `perm:project.write` | project | `{deleted: true}` |
| `PUT` | `/api/agents/{agent_id}/rules-enabled` | `perm:project.write` | project | Body `{enabled}` → the rules view: curated answers on or off for the agent |
| `PUT` | `/api/agents/{agent_id}/rules-order` | `perm:project.write` | project | Body `{ids[]}` → the rules view, in the new order |
| `POST` | `/api/agents/{agent_id}/rules/test` | `perm:project.use` | project | Body `{question}` → `{agent_enabled, matched, rule \| null}`: which rule would answer. No counter moves, no model call |

### Agent-DLC: the remaining routes

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `GET` | `/api/agents/{agent_id}/watch` | member | project | The agent's drift watch: schedule, criteria, golden dataset, budget, last runs |
| `PUT` | `/api/agents/{agent_id}/watch` | `perm:criteria.manage` | project | Body `{criteria_lineage_id?, dataset_id?, every, at_hour, tz, repeats, max_cost_usd?, enabled}` → the watch view. The dataset must be a golden set of this project. Prod-protected |
| `GET` | `/api/calibration/run-criteria/{run_id}` | `perm:criteria.manage` | project | `{run_id, status, mode, agent_id, criteria[{key, items}]}`: the evaluator criteria a run can seed an annotation task for, with the sample count each gives |
| `POST` | `/api/admission/{candidate_id}/duplicate` | `perm:golden.admit` | project | Body `{duplicate_of}` → candidate: marks an admission candidate as a duplicate |
| `POST` | `/api/waivers/{waiver_id}/reject` | `perm:waiver.approve` | project | Body `{note?}` → waiver |

### Alerts, costs, fleet, governance health, marketplace, overview

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `GET` | `/api/alerts` | member | project | `{rules[rule], firing, kinds[]}`: the workspace-level rules and this project's rules. A rule is `{id, kind, name, comparison, threshold, window, enabled, has_webhook, scope: workspace\|project, project_id, state, last_value, last_detail, last_checked_at, last_fired_at, last_notified_at, created_by, created_at}` |
| `POST` | `/api/alerts` | admin | project | Body `{kind, name, threshold, comparison?, window, enabled?, webhook_url?, scope?: workspace\|project, project_id?}` → `201` rule. `scope=project` measures one project (default the request's), which must be bound to the workspace |
| `PATCH` | `/api/alerts/{rule_id}` | admin | project | Any of `{name, threshold, window, enabled, webhook_url}` → rule |
| `DELETE` | `/api/alerts/{rule_id}` | admin | project | `{deleted: rule_id}` |
| `POST` | `/api/alerts/evaluate?notify=` | admin | project | `{workspace_id, evaluated_at, rules[…], firing, unknown}`: re-reads every enabled rule now; `notify=false` does not deliver webhooks |
| `GET` | `/api/costs?range=&project=&force=` | admin | ws | `{range, hours, generated_at, by_agent[…], sessions, …}`: estimated spend by agent, person and model; `unpriced_models` names models with no price. `project` narrows to one bound project; without it the report spans the workspace, with an unattributed bucket. Each `by_agent` row carries `project_id` and `project_source: span\|inference_profile\|agent\|null` (how the project was known: the spans' `launchpad.project` attribute, the project's application inference profile in the model id, else the ledger agent); `by_project[{project_id, agents, tokens, est_cost_usd}]` sums them per project, most expensive first, `project_id: null` being the spend attributed to no project. `422 costs.bad_range` |
| `GET` | `/api/costs/month-to-date?project=&force=` | admin | ws | `{workspace_id, project?, est_cost_usd}`: the figure a `cost_mtd_usd` alert compares against |
| `GET` | `/api/fleet?project=` | admin | hub | Every workspace in one table (agents, failures, pending releases, firing alerts), each also per project; `project` narrows to one project. A ledger read; an unusable workspace reports `readable: false`. `404 project.not_found` |
| `GET` | `/api/governance/health` | member | project | `{workspace_id, generated_at, score, grade, agents_considered, findings[{…, to}]}` |
| `GET` | `/api/overview/ttfa` | admin | project | Time to First Agent for the workspace's accounts, across every project of the workspace. Ledger read |
| `GET` | `/api/marketplace/templates` | member | ws | `{templates[{id, title, summary, method, spec, requirements, source_workspace_id, source_agent_name, published_by, uses, own, created_at, updated_at}]}` from every workspace |
| `POST` | `/api/marketplace/templates` | `infra` | project | Body `{agent_id, title, summary?}` → `201` template: one of this project's active agents published as a template |
| `DELETE` | `/api/marketplace/templates/{template_id}` | `infra` | project | `{deleted: template_id}`. Only the publishing workspace and, in it, the publishing project (`403 marketplace.not_yours`); administrators always |
| `POST` | `/api/marketplace/templates/{template_id}/use` | `perm:project.write` | project | → template: the wizard defaults and what the consumer must still supply. Creates no agent |

### Cost allocation tags, billed cost and evaluation cost

Every platform-created resource carries the `launchpad:project`, `launchpad:workspace`,
`launchpad:agent-id`, `launchpad:managed-by` and `launchpad:department` tags plus the
project's custom cost tags; workspace-shared infrastructure is `launchpad:project=shared`.

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `GET` | `/api/cost-tags?force=` | admin | ws | `{tag_set, shared_value, custom_max, reserved_prefixes, activation{account_id, account_type: standalone\|management\|member, can_activate, management_account_id, project_tag_active, tags[{key, custom, status: active\|inactive\|not_found\|unknown, last_updated, last_used}], error, checked_at, activation_delay_hours}}`: whether Billing has the keys as cost allocation tags (`ce:ListCostAllocationTags`, cached 10 min) |
| `POST` | `/api/cost-tags/activate` | admin | ws | Body `{keys?}` (omitted: every inactive key) → `{activated, already_active, not_found, errors, status}` (`not_found`: keys Billing has not seen on a resource yet — activate again after a day). `409 cost_tags.org_member` (an organization member account: activate in the management account it names), `403 cost_tags.activation_denied` |
| `POST` | `/api/cost-tags/backfill` | admin | ws | Starts the workspace-wide back-fill (the resumable job `cost_tags_backfill`): every ledger agent's resources and the workspace infrastructure get the tag set. An identical run still queued is reused. → the run `{id, status, stages[plan, tag], coverage{total, taggable, tagged, percent, by_status, by_kind}, resources[not tagged], reason, actor, created_at}` |
| `GET` | `/api/cost-tags/backfill` | admin | ws | `{tag_keys, shared_project, max_cost_tags, run}`: the latest workspace-wide run (`null` before the first) with its coverage and the resources it could not tag (why: `unsupported`, `denied` — add the IAM permission —, `missing`, `failed`). A project rename, a department or custom-tag change and a transfer start a re-tag run of their own |
| `GET` | `/api/costs/billing?granularity=daily\|monthly&project=&force=` | admin | ws | What AWS billed, grouped by `launchpad:project` and service (`ce:GetCostAndUsage`, the workspace's Region; 30 days daily or 6 months monthly; cached 6 h). The `shared` row is allocated by each project's invocations, untagged cost is listed apart. `project_tag_active` says whether the figures can be per project yet. A project not bound to the workspace answers `404` |
| `GET` | `/api/projects/{project_id}/cost-tags` | member | hub | `{project_id, cost_tags, max, reserved_prefixes, budget_basis, budget_bases}` |
| `PUT` | `/api/projects/{project_id}/cost-tags` | `pperm:project.members` | hub | Body `{cost_tags: {key: value}}` (≤ 10, AWS tag rules, no `aws:` / `launchpad:` keys, keys unique regardless of case; `{}` clears) → the view; a change re-tags the project's resources |
| `PUT` | `/api/projects/{project_id}/budget-basis` | admin, department admin | hub | Body `{basis: estimate\|billing}` → the view. The project's monthly budget (`monthly_budget_usd`) compares this basis |
| `GET` | `/api/eval/run-costs?runs=` | `perm:project.use` | project | `{costs: {run_id: {usd, kind: actual\|live\|estimate, estimate_usd}}}` for the comma-separated runs the caller can see (each run payload also carries this as `cost`) |
| `GET` | `/api/eval/runs/{run_id}/cost` | `perm:project.use` | project | The run's cost card: `{run_id, status, final, running, actual, live, estimate, estimate_total_usd, estimate_range, summary, prices}` — measured against the estimate, live while it runs. Another project's run answers `404` |
| `GET` | `/api/eval/online/{config_id}/cost?days=1..30` | `perm:project.use` | project | An online evaluation's cost per day `{config_id, days[{day, judgements, judge_usd, reports_usd, usd, by_evaluator}], range_days, total_usd}`; another project's config answers `404` |

Evaluation entries are priced before they are submitted (`POST /api/eval/runs/estimate`):
a dataset replay, a passive run over picked sessions or a look-back window, and an
online config (per day). Above the workspace's confirm threshold the submit needs
`confirm_cost: true` (`409 run.cost_confirm_required`); above its limit only an
administrator may submit (`409 run.cost_over_limit`) — `POST /api/eval/runs` and
`POST /api/eval/online` alike. The limit is the smaller of the workspace policy's
`eval_cost_max_usd` and the project's `quotas.eval_run_max_usd`; the estimate names
whose it is (`max_source: project | workspace`). When the project's quota binds, no
administrator override exists — an administrator's submit is refused with
`409 project.quota_exceeded` — so the refusal says to raise the project's quota.

### Strands builder: custom Python tools and the sandboxed tools

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `POST` | `/api/custom-tools/check` | `perm:project.use` | project | Body `{name, code, requirements?, execution: runtime\|code_interpreter}` → `{ok, error{message, line, col}, findings[{kind, line, message}], code_hash, input_schema, needs_approval, approval}`: the static check (one top-level `@tool` function named after the tool; imports and calls outside the allow-list are findings) without running the code |
| `GET` | `/api/custom-tools/approvals` | `perm:project.use` | project | `{approvals[{code_hash, tool_name, execution, requirements, findings, approved_by, approved_at, note}]}` — this project's approved versions, newest first |
| `POST` | `/api/custom-tools/approvals` | admin | project | Body `{name, code, requirements?, execution, note?}` → `201` the approval of this exact version (keyed by its code hash, ≤ 200 per project); audited `custom_tool.approve` |
| `DELETE` | `/api/custom-tools/approvals/{code_hash}` | admin | project | `{revoked: code_hash}`; another project's hash answers `404` |
| `GET` | `/api/custom-tools/sandbox` | member | ws | `{allowed_tools, code_interpreter_id, effective_code_interpreter_id, updated_by, updated_at}` — which of `python_repl`, `shell`, `file_write`, `use_aws` the workspace opens (they only run in the AgentCore Code Interpreter) |
| `PUT` | `/api/custom-tools/sandbox` | admin | ws | Body `{allowed_tools[], code_interpreter_id?}` → the setting |

Saving or re-publishing an agent (`POST /api/agents`, `POST /api/agents/{id}/redeploy`)
refuses a custom tool version no administrator approved (`422
custom_tool.approval_required`, with the `code_hashes`) and a sandboxed tool the
workspace has not opened (`422 custom_tool.sandbox_tool_not_allowed`); the deploy's
generate stage checks again. The Code Interpreter id is the workspace's — a
client-sent one is dropped.

### Governance, registry and tools: the remaining routes

Governance policies, the registry's MCP and skill records and the gateway's tools are
workspace infrastructure: every project reads them, and their writes are `infra`.

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `GET` | `/api/governance/policies` | member | project | `{engine{id, name, status, attached_mode, attached}, policies[{id, name, status, statement}]}`: the Cedar policies on the gateway's policy engine. `503 policy.engine_deleted` |
| `DELETE` | `/api/governance/gateways/{gateway_id}/policies/{policy_id}` | `infra` | project | Body `{expected_gateway_updated_at, expected_policy_updated_at, acknowledged_gateway_ids[], confirmation_name, override_reason?}` → `202 {operation}`: a queued, journaled policy change (see *Console Governance API*) |
| `GET` | `/api/governance/decisions` | member | project | `{decisions[{at, principal, tool, outcome, reason, source}], source: "demo"}`: the last 30 decisions recorded by `policy-test` |
| `POST` | `/api/governance/policy-test` | `infra` | project | Body `{tool, arguments, username}` → `{principal, tool, outcome: ALLOW\|DENY\|ERROR, detail, policy_id, decision_id, recorded, enforced, policy_engine_id, note}`: a real `tools/call` as that principal. Only ALLOW and DENY are journaled, and only when the Gateway has a Policy Engine attached (`enforced: true`, `policy_engine_id` its id). Without one the call still runs, but the answer is `enforced: false`, `policy_engine_id: null`, `recorded: false` with a `note` — no Cedar policy decided it |
| `POST` | `/api/governance/policy-generation` | `infra` | project | Body `{text, name}` → `{available, generation_id, status}` (preview API; `available: false` with `error` when AWS refuses) |
| `GET` | `/api/governance/policy-generation/{generation_id}` | member | ws | `{status, assets[{id, statement}]}` |
| `GET` | `/api/governance/guardrail` | member | ws | Whether the workspace has the `launchpad-pii` guardrail, and its state |
| `POST` | `/api/governance/guardrail` | admin | project | `201` → the guardrail view. Creates the preset, or adopts one of that name |
| `GET` | `/api/traces/{session_id}?lookback_hours=` | member | project | The session's span trace. In a workspace bound to several projects, only a chat session of the current project is readable (`404 trace.not_found` otherwise) |
| `GET` | `/api/registry/records/search?q=` | member | project | `{records[…]}`: semantic registry search; A2A records of agents outside the project are dropped |
| `GET` | `/api/registry/attachables?refresh=` | member | project | The approved MCP servers and skills the create wizard can mount, cached 60 s per workspace |
| `POST` | `/api/registry/records` | `infra` | project | Body `{type: MCP\|AGENT_SKILLS, name, description, url? (MCP), skill_md? (skill)}` → `201` record. `400 registry.invalid_url` / `registry.skill_md_required` |
| `PUT` | `/api/registry/records/{record_id}` | `infra` | project | Any of `{description, url, skill_md, staging_id, index}` → record. A content edit bumps the record version; DEPRECATED, A2A and system-managed records are not editable. `400 registry.nothing_to_update` / `registry.field_conflict` / `registry.field_type_mismatch`, `410 registry.staging_expired` |
| `DELETE` | `/api/registry/records/{record_id}` | `infra` | project | `{deleted: true, record_id}` |
| `POST` | `/api/registry/records/{record_id}/reimport` | `infra` | project | → record: re-acquires a git- or URL-sourced skill and bumps its version. `400 registry.not_reimportable`, `422 registry.skill_invalid` |
| `POST` | `/api/registry/sync-defaults` | `infra` | project | `202 {job_id, status, done, results[], error, started}`: queues a `registry_sync_defaults` job that registers the gateway's MCP targets and the sample skill bundle (it waits each new record out of CREATING, longer than a 60 s origin timeout). A sync already queued or running is returned with `started: false`; a restart re-runs it (every step is an upsert) |
| `GET` | `/api/registry/sync-defaults/{job_id}` | `infra` | project | `{job_id, status: queued\|running\|succeeded\|failed, done, results[{name, type, record_id, created}], error}`: the sync's progress; `results` grows as each record settles. `404 job.not_found` |
| `POST` | `/api/registry/skills/inspect` | `perm:agents.deploy` | project | A multipart `.zip` or JSON `{source{…}}` (git) → `{staging_id, skills[…]}`: validated bundles parked in staging for preview, bound to the caller, workspace and project. Writes nothing to S3 |
| `POST` | `/api/registry/skills/import` | `infra` | project | Body `{staging_id, selections[{index, name?, name_override?, description_override?}]}` → `{records[{name, ok, record?, error?, error_code?}]}`. A per-item failure never aborts the batch. `410 registry.staging_expired` |
| `GET` | `/api/registry/skills/capabilities` | member | ws | `{git{…}}`: whether the `git` CLI is available for git imports |
| `POST` | `/api/registry/skills/capabilities/git-install` | `infra` | project | A best-effort install of the `git` CLI on the backend host |
| `POST` | `/api/registry/a2a-demo` | `perm:project.use` | project | Body `{agent_id, question}` → `{answer, trace[…], session_id, …}`: invokes a front-desk runtime agent of the project with SigV4. `404 registry.a2a_demo_agent`, `400 registry.a2a_demo_unsupported`, `409 registry.a2a_demo_jwt_unsupported`, `502 registry.a2a_demo_failed` |
| `GET` | `/api/tools?refresh=` | member | ws | `{gateway_url, gateway_error, tools[{name, source, target, description, inputSchema, auth}]}`: the gateway's tools (cached 60 s) plus the built-in ones |
| `POST` | `/api/tools/call` | `infra` | project | Body `{name, arguments}` → the MCP `tools/call` result |
| `GET` | `/api/demos/browser/options` | member | ws | `{browsers[…], profiles[…]}`: the account's custom AgentCore browsers and browser profiles. `502 tools.browser_options_failed` |
| `POST` | `/api/demos/browser` | `perm:project.write` | project | Body `{url, web_bot_auth?, browser_identifier?, profile_identifier?, save_profile?}` → `{url, title, session_id, latency_ms, live_view_url, live_view_expires_in, viewport, browser_identifier, web_bot_auth, profile_identifier, …}`: opens the page in an AgentCore Browser session kept alive for Live View |
| `DELETE` | `/api/demos/browser/{session_id}` | `perm:project.write` | project | `{session_id, …}`: stops a demo session of this project (saving the profile when asked at start). `502 tools.browser_stop_failed` |
| `POST` | `/api/demos/code-interpreter` | `perm:project.write` | project | Body `{code}` → `{stdout, session_id, latency_ms}`: one Python run in an AgentCore Code Interpreter session in the workspace account |

### Observability: the remaining routes

In a workspace bound to several projects, a span or session is shown only when it
resolves to an agent of the current project (or a platform agent); one that resolves
to no agent is hidden from non-administrators. A single-project workspace shows
everything, as before.

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `GET` | `/api/observability/dashboard?range=&force=` | member | project | The dashboard tiles and series for the range, cached per workspace and project |
| `GET` | `/api/observability/sessions?range=&force=` | member | project | `{sessions[…], count, …}`. A row carries `project` when its spans name their project (the `launchpad.project` resource attribute of an isolated project's agent); rows without it keep their old shape |
| `GET` | `/api/observability/traces?range=&agent=&status=&session=&force=` | member | project | `{traces[…], count, …}`; `session` matches a session or trace id substring. A row carries `project` when its spans name it, as above; one trace's `meta.project` likewise |
| `GET` | `/api/observability/traces/{trace_id}?range=&force=` | member | project | One trace with its spans. `404 observability.trace_not_found`, also for another project's trace |
| `GET` | `/api/observability/prices` | member | ws | `{prices, meta, source, refresh_hours}`: the model price table |
| `POST` | `/api/observability/prices/refresh` | `infra` | project | Pulls the latest prices from the configured litellm source into the local config. No AWS call |

### Evaluation, experiments and runtime canaries: the remaining routes

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `POST` | `/api/eval/datasets/upload` | `perm:project.write` | project | Body `{name, locale?, description?, jsonl}` → `201` dataset. `422 dataset.invalid_jsonl` / `dataset.invalid_item` / `dataset.empty` |
| `PUT` | `/api/eval/datasets/{dataset_id}` | `perm:project.write` | project | Any of `{name, description, items[]}` → dataset. Replacement items keep the dataset's kind (`400 dataset.kind_immutable`) |
| `DELETE` | `/api/eval/datasets/{dataset_id}` | `perm:project.write` | project | `{deleted: true}` |
| `GET` | `/api/eval/pipelines` | member | project | `{pipelines[…]}`: the data-processing pipelines (V2), newest first |
| `GET` | `/api/eval/pipelines/{pipeline_id}` | member | project | One pipeline |
| `PUT` | `/api/eval/pipelines/{pipeline_id}` | `perm:project.write` | project | Body `{name, description?, source, processing, output}` → pipeline. `409 pipeline.running`; the output dataset and the source log groups must be this project's |
| `DELETE` | `/api/eval/pipelines/{pipeline_id}` | `perm:project.write` | project | `{deleted: true}` |
| `GET` | `/api/eval/online/{config_id}/results?range=` | member | project | Per-evaluator means, labels, trend and recent records of an online evaluation config. Another project's config reads as missing in a multi-project workspace |
| `POST` | `/api/eval/online/{config_id}/resume` | `perm:eval.run` | project | → config: sets its execution status back to enabled |
| `GET` | `/api/experiments` | member | project | `{experiments[…]}`: the 20 newest |
| `GET` | `/api/experiments/providers` | member | ws | `{providers[…]}`: the recommend-stage generators (`agentcore`, `gepa_lite`) |
| `GET` | `/api/experiments/{exp_id}` | member | project | One experiment with its stages, artifacts and `running_action` |
| `POST` | `/api/experiments/{exp_id}/action` | `perm:project.write` | project | Body `{action: recommend\|accept\|bundles\|gateway\|abtest\|traffic\|verdict\|promote\|cleanup, …stage options}` → the experiment (`202` for an action that runs in the background). `409 experiment.action_in_flight` / `experiment.stage_not_ready`, `410 experiment.action_moved` for `canary` / `ramp`. Every action except `verdict` / `cleanup` also needs the agent to be visible. Prod-protected |
| `GET` | `/api/runtime-canaries` | member | project | `{canaries[…]}`: the 20 newest |
| `GET` | `/api/runtime-canaries/{canary_id}` | member | project | One canary |
| `POST` | `/api/runtime-canaries/{canary_id}/action` | `perm:project.write` | project | Body `{action: setup\|traffic\|verdict\|advance\|complete\|rollback\|cleanup, dataset_id? (traffic), allow_non_significant?}` → the canary. A legacy canary's separate challenger must be in the same project. `409 canary.action_in_flight` / `canary.stage_not_ready`, `422 canary.dataset_required` / `canary.dataset_unsupported`. Prod-protected |

### Skill Lab: jobs and task sets

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `GET` | `/api/skill-lab/status` | member | ws | `{provisioned, missing[], venv_ready, default_target_model, default_judge_model, default_codex_target_model, target_backends[], judge_modes[], agentic_judge_ready, judge_codex_ready, judge_claude_ready, judge_cli_ready}` |
| `GET` | `/api/skill-lab/tasksets` | member | project | `[taskset]`: the project's task sets |
| `POST` | `/api/skill-lab/tasksets` | `perm:project.write` | project | Body `{name, description?, mode, tasks_by_split{…}}` → `201` task set |
| `GET` | `/api/skill-lab/tasksets/{taskset_id}?full=` | member | project | One task set (`full=true` includes every task) |
| `PUT` | `/api/skill-lab/tasksets/{taskset_id}` | `perm:project.write` | project | Any of `{name, description, tasks_by_split}` → task set |
| `DELETE` | `/api/skill-lab/tasksets/{taskset_id}` | `perm:project.write` | project | `{ok: true}` |
| `POST` | `/api/skill-lab/task-assets` | `perm:project.write` | project | Multipart `files[]` → `201 {assets[…]}`: staged task attachments |
| `GET` | `/api/skill-lab/jobs?type=` | member | project | `[job]` |
| `POST` | `/api/skill-lab/jobs` | `perm:project.write` | project | Body `{type: eval\|train\|taskgen, skill_source, taskset_id, split?, target_split?, attachments?, params?}` → `201` job. `422 skill_lab.bad_params` |
| `GET` | `/api/skill-lab/jobs/{job_id}` | member | project | One job |
| `GET` | `/api/skill-lab/jobs/{job_id}/log?offset=` | member | project | The job log from `offset` |
| `GET` | `/api/skill-lab/jobs/{job_id}/diff` | member | project | The skill-version diff of a training job. `404 skill_lab.results_pending` |
| `GET` | `/api/skill-lab/jobs/{job_id}/train-summary` | member | project | The training steps. `404 skill_lab.results_pending` |
| `POST` | `/api/skill-lab/jobs/{job_id}/cancel` | `perm:project.write` | project | → job |
| `POST` | `/api/skill-lab/jobs/{job_id}/resume` | `perm:project.write` | project | → job |
| `POST` | `/api/skill-lab/jobs/{job_id}/publish` | `infra` | project | Body `{reapprove?}` → result: publishes the trained skill to the workspace registry |
| `DELETE` | `/api/skill-lab/jobs/{job_id}` | `perm:project.write` | project | `{ok: true}` |

### Architect assistant: evaluation plan and operations

See *Console Architect Assistant API* above for the plan and operation shapes.

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `POST` | `/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/prepare` | `perm:project.use` | project | Body `{revision}` → `201 {plan, …plan state}`: drafts a plan revision from one proposal revision. No resource side effects |
| `PUT` | `/api/assistant/architect/conversations/{conversation_id}/evaluation-plan` | `perm:project.use` | project | Body `{content}` → `{plan, …plan state}`: a member edit is a new plan revision (draft, or invalid with its errors) |
| `POST` | `/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/materialize` | `perm:agents.deploy` | project | Body `{plan_revision, plan_hash, acknowledge_disclosure: true}` → `202 {operation, started: true}` when this call created the operation, `200` with the recorded operation for a repeat. `422 assistant.disclosure_required` |
| `GET` | `/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/operations/{operation_id}` | member | project | `{operation}`. Ledger read |
| `POST` | `/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/operations/{operation_id}/retry` | `perm:agents.deploy` | project | `{operation, started}`: resumes the persisted intents of a partial or failed operation |
| `POST` | `/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/operations/{operation_id}/lambda-revision-review` | `perm:agents.deploy` | project | Body `{plan_hash, expected_created_revision_id, expected_current_revision_id, cloudtrail_event_id, reason}` → `{operation, review, started}`: the reviewed recovery of one Lambda revision conflict. No cloud write |
| `DELETE` | `/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/operations/{operation_id}/assets` | `perm:agents.deploy` | project | `{operation}`: deletes exactly the cloud artifacts the operation created; the local dataset stays |

### Studio local execution and code generation

These routes run generated agent code on the backend host (`local_exec`) and are
refused in prod unless explicitly enabled; that guard is the real boundary.

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `POST` | `/api/execute` | `perm:project.use` | project | Body `{code, input_data?, openai_api_key?, bedrock_api_key?}` → `{success, output \| error, execution_time_ms}` |
| `POST` | `/api/execute/stream` | `perm:project.use` | project | Same body → SSE of the subprocess stdout, ending with `[STREAM_COMPLETE:<seconds>]` |
| `POST` | `/api/fix-code/stream` | `perm:project.use` | project | Body `{code, error, flow_data, graph_mode, input_data?}` → SSE from the coding agent's diagnosis and fix |
| `GET` | `/api/generate-code/status` | member | ws | Coding-agent backend availability (drives the Fix button) |
| `GET` | `/api/conversations` | member | project | The studio conversation sessions |
| `POST` | `/api/conversations` | `perm:project.use` | project | Body `{generated_code, flow_data, project_id?, version?, openai_api_key?, bedrock_api_key?}` → session. `project_id` is the studio flow's id, not a Launchpad project |
| `GET` | `/api/conversations/{session_id}` | member | project | The session history |
| `DELETE` | `/api/conversations/{session_id}` | `perm:project.use` | project | Deletes the session |
| `PUT` | `/api/conversations/{session_id}/code` | `perm:project.use` | project | Body `{generated_code}` → session: replaces the code, keeps the messages |
| `GET` | `/api/conversations/{session_id}/messages` | member | project | `{messages[…]}` |
| `POST` | `/api/conversations/{session_id}/messages` | `perm:project.use` | project | Body `{message}` → the reply |
| `POST` | `/api/conversations/{session_id}/messages/stream` | `perm:project.use` | project | Body `{message}` → SSE, ending with `[CHAT_COMPLETE:<id>]` or `[CHAT_ERROR:<json>]` |

### API keys, environments, announcements and videos

| Method | Path | Role | Scope | Result |
|---|---|---|---|---|
| `POST` | `/api/apikeys/{key_id}/disable` | `perm:project.write` | project | → key. A key belongs to one project; `/v1` calls with it run in that project |
| `GET` | `/api/apikeys/{key_id}/usage?days=1–90` | member | project | `{key_id, total, last_used_at, days[…]}`: admitted `/v1` calls per UTC day, zero-filled |
| `GET` | `/api/environments/compare?agent=` | member | project | `{agent, reference_workspace, environments[…], summary}`: the agent of that name in every workspace the caller reaches and the current project is bound to |
| `GET` | `/api/environments/drift?agent=` | member | project | `{workspace_id, state, counts, agents[…], checked, truncated}`: ledger expectation against what AWS reports. Read-only |
| `GET` | `/api/announcements?limit=&offset=` | member | hub | `{announcements[…], total}`: the published projection |
| `GET` | `/api/announcements/manage?limit=&offset=` | admin | hub | `{announcements[…], total}` with drafts |
| `POST` | `/api/announcements` | admin | hub | Body `{title, body, link_url?, link_label?}` → `201` announcement `{id, status, revision, content, published_content, has_unpublished_changes, created_by, updated_by, created_at, updated_at, published_at}` |
| `GET` | `/api/announcements/{announcement_id}` | admin | hub | One announcement |
| `PUT` | `/api/announcements/{announcement_id}` | admin | hub | The create body + `expected_revision` → announcement (`409` on a stale revision) |
| `POST` | `/api/announcements/{announcement_id}/publish` / `…/unpublish` | admin | hub | Body `{expected_revision}` → announcement |
| `DELETE` | `/api/announcements/{announcement_id}?expected_revision=` | admin | hub | `{deleted: true}` |
| `GET` | `/api/videos` | member | hub | `{schemaVersion, categories, collections, videos}`: the published tutorial catalogue |
| `GET` | `/api/videos/manage` | admin | hub | `{taxonomy, videos[…]}` with drafts |
| `POST` | `/api/videos/manage` | admin | hub | Body `{category_id, section_id, console_version, title, description, cdn_url, webm_url?, poster_url?, caption_url?, duration_seconds, chapters[], sort_order}` → `201` video (same revisioned shape as an announcement) |
| `GET` | `/api/videos/manage/{video_id}` | admin | hub | One video |
| `PUT` | `/api/videos/manage/{video_id}` | admin | hub | The create body + `expected_revision` → video |
| `POST` | `/api/videos/manage/{video_id}/publish` / `…/unpublish` | admin | hub | Body `{expected_revision}` → video |
| `DELETE` | `/api/videos/manage/{video_id}?expected_revision=` | admin | hub | `{deleted: true}` |
