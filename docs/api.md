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

**Key lifecycle.** A key records the console account that minted it
(`created_by` = its `users.id`, plus `created_by_name`; both `null` on keys minted
before this existed, which the console shows as "unknown") and `last_used_at`
(stamped on a successful `/v1` call, at most once a minute). A key lives only as
long as its creator may hold it: once that account is **deleted, disabled or
expired**, or — for a member — **loses its grant** to the key's workspace, the key
answers `401 auth.invalid_api_key` (the same code as any unusable key; `/v1` never
says why) and is flipped to disabled. Admin disable / delete and grant removal
disable the creator's keys at once; expiry has no write event, so it lands on the
key's next use. Offboarding disables, never deletes — such keys stay listed in
`GET /api/apikeys` (`created_by`, `created_by_name`, `last_used_at`,
`creator_offboarded`: `deleted` | `disabled` | `expired` | `no_workspace_grant` |
`null`); only the console's explicit `DELETE /api/apikeys/{id}` removes a row.
`POST /api/apikeys/{id}/enable` on an offboarded creator's key is refused with
`409 apikey.creator_offboarded` (`detail.reason`). **Member rule (CN):** a member
lists, toggles and deletes only the keys they minted — another member's key answers
`404 apikey.not_found` — while an administrator reaches every key of the workspace,
including legacy NULL-creator keys. Keys minted by the config-driven admin or the
auth-disabled local operator (no `users` row) and legacy keys are never offboarded.
A posted `created_by` / `created_by_name` is refused (422). Expiry, scopes and agent
allowlists are not implemented.
密钥仅创建时展示一次,后端只保存哈希。

**Times (contract note, N17-obs463, 2026-10-26).** Every date-time the API returns — `/v1`
and the console `/api` alike, JSON bodies and stream events — is ISO 8601 in the deployment's
display zone (`display_timezone`, default `Asia/Shanghai`), e.g.
`"2026-10-24T18:29:54.079198+08:00"`. It is the **same instant** as before: earlier responses
carried naive UTC (`"2026-10-24T10:29:54.079198"`) or `+00:00` for the same value. Parse the
offset (any ISO 8601 parser does) and convert if you need UTC; never strip it. Only the
platform's own timestamp fields are converted (`*_at`, `*At`, `at`, `ts`, `time`, `timestamp`,
`start` / `end`, `since` / `until`, `first` / `last`, `bucket` / `bin`, …). Content you or an
agent wrote comes back exactly as stored, even when it is a date-time string: dataset items
and scenarios, proposal content, chat message text and stream text deltas, tool arguments and
results, span attributes, memory / KB metadata, evaluator configs, the agent spec. Storage,
logs and AWS stay UTC. `GET /api/capabilities` names the zone (`display_timezone`). Times you send
may carry any offset (`Z`, `+00:00`, `+08:00`) and are converted, not relabelled.

## Sync invoke / 同步调用

```bash
curl -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke \
  -H "X-Api-Key: $LP_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "What is 2+2?", "session_id": null}'
# → {"agent":"…","text":"4","session_id":"…","latency_ms":1234}
# optional "truncated": true when the turn ended at the model's token limit (N-SCN-bug4) — absent on a complete answer
```

Every `/v1` turn (sync and streaming) is ledgered like a console turn, so the
session appears in the console's Chat session list, history and Observability
with `source: "public_api"` (N-SCN-obs1); the row keeps the request's
`actor_id` (default `api`).

A caller-supplied `session_id` must be **33–100 characters** (the AgentCore Runtime
`runtimeSessionId` bounds). Anything else is refused up front with
`422 chat.session_id_invalid` (`detail {length, min_length: 33, max_length: 100}`) — before
the policy gate records a decision and before any AWS call — on `/v1/agents/{id}/invoke`,
`/v1/agents/{id}/invoke-stream`, `POST /api/chat/{id}` and the console test invoke
(N-SCN-obs16). N16-obs72: the id must also match `[a-zA-Z0-9][a-zA-Z0-9-_]*` (the botocore
session-id shape; a CJK or blank-bearing id used to pass the length check and fail at AWS as
a SigV4 signature mismatch) — the same 422 with `detail.pattern` added; the by-id chat
routes (history / memory / stop / reattach) apply the character-class check too, before
reading the ledger. Omit it (or send `null`) to start a new session: generated ids are 64
characters and never subject to the check. Likewise `AgentSpec.env` accepts at most 22
variables: the Runtime's `environmentVariables` cap is 50 and the platform reserves 28 of them
for its own keys (`deployer/environment.PLATFORM_ENV_KEYS`; 27 → 28 with N16-obs90's
`LAUNCHPAD_A2A_ACTOR_KEY`, so the member's budget went 23 → 22); the `422 validation.invalid_request`
names that budget, instead of a ValidationException after the whole pipeline ran; the
`generate` stage additionally derives the MERGED map (spec + platform keys) before any AWS call
and fails the job with `agent.env_budget_exceeded {user, platform, limit}` should it exceed 50.

**N16-obs88:** a `session_id` whose chat session was ENDED (`POST /api/chat/{id}/sessions/{sid}/stop`)
is `409 chat.session_ended {session_id, ended_at}` on `/v1/agents/{id}/invoke` and
`/v1/agents/{id}/invoke-stream` exactly as on `POST /api/chat/{id}` (N16-obs79) — before the
policy gate and any runtime call, as a plain response before the stream opens, and the ledger
row is left untouched (the marker stays; the `/v1` entrance used to re-open the session). The
gate lives in the shared invoke chain (`services.invoke.require_session_open`), so the two
entrances cannot disagree; send a new `session_id` (or none) to continue.

## Streaming invoke (SSE) / 流式调用

```bash
curl -N -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke-stream \
  -H "X-Api-Key: $LP_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "Tell me a two-sentence story."}'
# event: meta   → {"session_id": "…", "mode": "stream"}
# event: delta  → {"text": "Once"} … (incremental chunks)
# event: done   → {"latency_ms": 2100}
```

Pass the returned `session_id` on the next call to continue the conversation
(session context + AgentCore Memory ride on it).

The stream may also carry `tool` (`{name, id}`, a tool call started), the additive
`tool_input` / `tool_result` summaries that follow it (N16-obs85 — see *Console Chat
API*; `{id, name, input ≤ 512 chars}` / `{id, status, summary ≤ 256 chars}`) and, for a
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
offboarded-creator key), `agent.not_active` (409), `agent.not_found` (404),
`agent.runtime_error` (502 — **N17-obs95**: the Runtime's stream ended on the AgentCore SDK's
terminal error frame `{"error", "error_type", "message"}` — a model timeout, throttle or
validation error inside the agent, an expired bearer, the one-body `{"error": …}` refusal,
a malformed frame or a structured `REJECTED` body — on the **sync** entrances
`/v1/agents/{id}/invoke` and the console `POST /api/agents/{id}/invoke`, where there is no
stream to carry Chat's `error` event; `message` is the frame's bounded, secret-redacted text
`runtime returned error: <error_type>: <error>` and `detail` the structured
`{error_type, error, message}`; nothing is ledgered, so the session's turn count is unchanged),
`agent.empty_answer` (502 — **N17-obs114**: the runtime completed without producing anything to
show — an empty `complete` / `{"result": ""}` body, the recorded shape being a BYOC model that
spent its whole token budget on reasoning and returned no text block, or a stream that ended
with no frame at all. On the sync entrances this is an error envelope rather than a `200` with
`text: ""`: the runtime ran and gave no answer, and a caller must be able to tell that apart
from a successful turn — on this protocol a runtime with nothing to say streams an `error` or a
notice, never silence. In Chat the same condition ends the turn on the `error` event with that
code, so the console shows an error row where it used to show nothing at all; a turn that
streamed a tool card, a consent ask, a policy card or the `truncated: true` notice is not empty).
Every 401 from the key check counts against the caller's source IP: after
`LAUNCHPAD_AUTH_API_KEY_IP_LIMIT` (default 20) of them within
`LAUNCHPAD_AUTH_API_KEY_IP_WINDOW_SECONDS` (default 300) the answer is
`auth.rate_limited` (429, `Retry-After` header, `detail.retry_after` seconds) for every
*further invalid* attempt until a slot frees; a valid key is never counted and never
refused by the window (behind nginx or the NLB every caller shares one peer address, so
the window is a coarse secondary control). `0` disables the window.

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

**Transport failures (N17-obs94).** A call AWS never answered is not a `ClientError`, so
the table above does not see it; since `cn/r17-o` the botocore transport family has its
own handler and is **never a bare 500** either: a timeout (`ReadTimeoutError`,
`ConnectTimeoutError` — the request was sent, nothing came back in time) is
**504 `aws.timeout`**, and a connection that could not be made or was cut
(`EndpointConnectionError`, `ConnectionClosedError`, `SSLError`, `ProxyConnectionError`,
`ResponseStreamingError`, `IncompleteReadError`, …) is **502 `aws.unreachable`**. The
`message` is a static sentence (`AWS did not answer in time` / `AWS could not be reached`)
on the console and on `/v1` alike — botocore's text embeds the endpoint URL, i.e. the
runtime ARN — and `detail` is `{"error_type": "<botocore class name>"}`; the raw text is in
the backend log. On an invoke the caller cannot tell whether the runtime ran the turn (a
504 after a long turn usually means it did): retry on a fresh session or read the history.
Other `BotoCoreError`s (`ParamValidationError`, `NoCredentialsError`) are programming or
configuration faults and remain the unhandled 500 described above.

## Health

| Method | Path | Auth | Response |
|---|---|---|---|
| `GET` | `/api/health` | public (no session) | `{status: "ok", version, region, build{sha, short, built_at, source, dist_sha}}` — `build` (N13-obs10, additive) is the deployed commit: `sha` / `short` (8 chars) / `built_at` (ISO-8601 UTC) / `source` = `env` (`LAUNCHPAD_BUILD_SHA` [+ `LAUNCHPAD_BUILD_AT`]) \| `file` (`data/build-info.json`, written by `scripts/host-update.sh` step 7 on every update) \| `dist_stamp` (`frontend/dist/.launchpad-build`, `built_at` = its mtime); every key null when the host carries no stamp. The V2 sidebar shows `Build <short>` from it, so a demo video's `recorded_version` (E01-obs1) can be checked against the running console. `dist_sha` (N17-obs466, additive) is the commit the SERVED console bundle was built from — the dist stamp, else `dist_built_from` of `data/build-info.json`, else null (never the env / HEAD sha: host-update keeps `dist/` when `frontend/` did not change, so `sha` moves on a backend-only update while the bundle stays). An open V2 tab compares it with the sha its bundle was compiled with and shows a non-blocking new-version banner with a Reload button on a mismatch (never an automatic reload). Since the obs466 follow-up the sidebar `Build <short>` names the TAB's bundle sha (the deployed short sha only on a dev-server tab), its tooltip `This tab … · server <dist_sha> · deployed commit <sha> · <built_at>`, and it is marked `new version available` on the banner's verdict. Polled by the console (30 s), nginx / the NLB target group and host-update step 14 |

## Console Agents API — BYOC uploads

The `byoc` creation method deploys member-written code. The two zip artifact
kinds (`code_zip`, `container_source`) stage their archive here first; the
returned `upload_id` goes into the create body's `spec.byoc`.

| Method | Path | Result |
|---|---|---|
| `POST` | `/api/agents/uploads?python_version=PYTHON_3_13` | `perm:agents.deploy` — `multipart/form-data`, single part `file`, `.zip` only, ≤250 MiB (≤750 MiB uncompressed, ≤20k entries; zip-slip/absolute paths/symlinks refused). Stores `byoc/{workspace_id}/{upload_id}/source.zip` + `manifest.json` in the artifacts bucket → `201` `{upload_id, sha256, size_bytes, original_filename, uploaded_by, uploaded_at, entries_count, uncompressed_bytes, detected: {entrypoint_candidates[], has_requirements, has_dockerfile, agentcore_sdk_detected, requirements: {status: ok\|failed\|skipped, package_count, error}}}` — `requirements` is an upload-time dry resolve of the zip's requirements.txt against the deploy target (linux/aarch64 + the optional `python_version`, default PYTHON_3_13); `failed` means the deploy's package stage would fail the same way, `skipped` (no requirements.txt, resolver timeout ~90 s, `uv` unavailable) says nothing either way |
| `GET` | `/api/agents/uploads/{upload_id}` | member — the stored manifest (same shape, plus `uploaded_by_id`: the uploader's `users.id`, `null` for the config-driven administrator); another workspace's upload_id answers 404 `byoc.upload_not_found`. **N16-obs77:** the workspace-visibility rule (users-and-ownership §7) applies — under `isolated` a member reads only an upload they staged, an administrator staged, or one with no recorded uploader (staged before the field existed); a hidden upload is the same 404. `shared` keeps by-id reads open |

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

### TypeScript (Node) container agents in aws-cn (N17-obs465)

The upload's `detected.telemetry` also reads a Node zip: `{"adot": "node", "runtime": "node",
"adot_node": "<ADOT Node range>", "cn_exporter": "will_apply" | "not_applicable"}` — a
`package.json` that depends on `@aws/aws-distro-opentelemetry-node-autoinstrumentation` used to
read `adot: "absent"`. In aws-cn a Node `container_source` / `container_image` agent is deployed
with the platform's aws-cn trace exporter layer on top of its image (ADOT Node cannot sign the
China export itself — evaluation-engine.md §33.7); `byoc.cn_trace_exporter: false` (default
`true`) opts out. The row stamps `spec.byoc.telemetry.cn_exporter` (`applied` | `not_aws_cn` |
`opted_out` | `not_node` | `unknown_runtime`), `member_image` and `layer_image`.

`GET /api/agents/{id}/trace-status` (member, read-only) answers `{cn_exporter, member_image,
layer_image, hook: {state, reason, at} | null, traces: {session_id, arriving} | null, badge}`:
`hook` is the layer's start-up line in the runtime log (`attached` | `native` | `skipped` |
`unavailable` | `not_started`), `traces` whether the newest session (7 days) left spans in
`aws/spans`; `badge` = `ok` | `unavailable` | `not_arriving` | `unknown`. 404 `agent.not_found`.

## Console Agents API — versions and endpoints

`GET /api/agents/{agent_id}/versions` is the read-only AWS view behind the agent
detail's VERSIONS & ENDPOINTS panel. It follows every `nextToken` page of the two
list operations for the agent's resource family and returns an allow-listed
projection — no environment values, artifact locations, execution roles or
authorizer configuration.

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/agents/{agent_id}/versions` | `{kind: runtime\|harness, resource_id, versions[{version, status, description, last_updated_at}], endpoints[{name, live_version, target_version, status, description, created_at, last_updated_at, failure_reason}], latest_version, ledger_version, canary_endpoints[]}` — `versions` newest first; `endpoints` with `DEFAULT` first then by name; `latest_version` is the highest version AWS reports and `ledger_version` the one the last Launchpad deploy recorded (`Agent.version`) — they may differ after an out-of-band update or a canary candidate mint; `canary_endpoints` lists the `stable`/`treatment` names still present. Resource family: `zip_runtime`/`studio`/`container` and imported rows whose `spec.discovery.resource_type` is absent or `runtime` → `ListAgentRuntimeVersions` + `ListAgentRuntimeEndpoints`; `harness` and imported rows with `resource_type == "harness"` → `ListHarnessVersions` + `ListHarnessEndpoints` (harness versions carry no description). Never mutates anything |
| `GET` | `/api/agents/{agent_id}/conversions` | member — `{source: {id, name, method, status}, conversions: [agent…]}`: the **Runtime twins** converted from this agent (`POST …/convert` stamps `spec.source_harness.agent_id` on the new `-rt` agent), newest first, each in the ordinary agent projection plus its latest `deployment`. Pure ledger read (no AWS call); deleted twins are omitted; unknown or deleted source → 404 `agent.not_found`. The assistant's NEXT STEPS reads it to switch to the twin once it is `active` and to find it again after a reload |

**Spec snapshots (upstream #277, reads only in CN).** Every publish leaves a numbered
snapshot of the agent's spec in the ledger (`spec_snapshots`, workspace-scoped): create,
redeploy, recommendation accept, architect approve and preset releases write one through
`create_deployment`; the Runtime-canary promote records its own. An agent that predates the
table gets snapshot #1 (`note: "baseline"`) at startup. On success a snapshot is stamped with
the AWS version it left live and its spec is replaced by the spec **as deployed**; a failed
publish keeps `aws_version: null`. The evaluation scorecards read `seq` as the agent version
a run evaluated. A deleted agent's history stays readable (its runs keep their versions);
another workspace's agent, or one the caller cannot see, is `404 agent.not_found`. Upstream's
`POST …/snapshots/{seq}/rollback` is **not** ported — a redeploy publishes any spec.

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/agents/{agent_id}/snapshots` | member — `{snapshots[{seq, agent_id, aws_version, deployed, state, deployment_id, note, created_by, created_by_name, created_at}]}`, newest first and without `spec`. `state` is `deployed` \| `deploying` \| `failed` \| `not_deployed` |
| `GET` | `/api/agents/{agent_id}/snapshots/diff` | member — `?from_seq=&to_seq=`. An omitted side is the agent's **current** spec; both omitted → `422 snapshot.diff_side_required`. Result `{from, to, changes[{path, group, kind, before, after, added?, removed?, elided?}]}`, where `from`/`to` are a snapshot or `null` (current). `group` is `prompt` \| `model` \| `tools` \| `skills` \| `knowledge_bases` \| `memory` \| `guardrail` \| `other`, `kind` is `added` \| `removed` \| `changed`. Nested fields are compared by dotted path, lists by membership (`added`/`removed`), and `code`, `code_bundle`, `studio_flow` and `system_prompt` whole; a body over 2 KB comes back as `{length}` with `elided: true` |
| `GET` | `/api/agents/{agent_id}/snapshots/{seq}` | member — one snapshot, including `spec`. `404 snapshot.not_found` |

Error codes: `agent.not_found` (404, unknown id, another workspace's agent, **or a
deleted agent** — since N11 r3-A every `/api/agents/{id}*` and `/api/chat/{id}*` route
answers a deleted row exactly like `/v1` does, instead of `200 status: deleted` or
`409 agent.not_active`; N10v2-bug3/bug7), `agent.no_resource` (409, the row has no AWS
resource to ask about — deploy still running, failed first deploy, or a shape that is
neither Runtime nor Harness; `message` is the human reason the panel shows). AWS
`ClientError`s map to the standard 4xx envelope.

### Deploy-job ownership of the Runtime canary's publishes (N17-obs13)

`POST /api/runtime-canaries/{id}/action` with `action: complete` (promote) or `rollback` is
`409 agent.deploy_in_progress {job_id, stage, job_status}` — the envelope `DELETE
/api/agents/{id}` answers — while the champion agent has a `queued` / `running` deploy job:
both publish the agent's live version, so the job owns it until it is terminal
(`docs/cn/interfaces/agents.md` §1.2). Nothing is written (agent row, canary row, snapshots,
the A/B split all unchanged); the check comes after `canary.stage_not_ready` and before the
verdict gate. `setup`, `traffic`, `verdict`, `advance` and `cleanup` act on the split only and
proceed. Once the job is terminal the same `complete` / `rollback` is the usual 202, and the
redeploy's snapshot holds the spec that job packaged — never a spec another writer put on the
row mid-job. **N17-obs16:** `complete` / `rollback` are `409 canary.stale_base {setup_version, current_version, action}` once the champion's version moved since the canary's `setup` (the candidate was built on a base that is no longer production); the canary stays `running` — `cleanup` it and start a new one.

### Ownership (N10v2-bug4; generalised by r13-g)

Since r13-g the rule below is `services/ownership.require_creator_or_admin` and gates every
user-managed kind's mutations, not only agents: runtime-canary actions other than `verdict`
(`POST /api/runtime-canaries/{id}/action` → 403 `canary.forbidden` `{canary_id, action,
created_by, created_by_name}`), Skill Lab job cancel / resume / publish / delete and task set
PUT / DELETE / apply-expansion (`skill_lab.forbidden`), dataset PUT / DELETE
(`dataset.forbidden`) and custom evaluator PUT / DELETE (`evaluator.forbidden`). A row with no
recorded creator (`created_by` NULL) is an administrator's. `GET /api/auth/status` and the
login response carry the caller's `user_id` so the console can mirror the rule. Contract:
`docs/cn/interfaces/users-and-ownership.md` §8.

`agent.owner` is the **login identity** that created the agent (`POST /api/agents`,
an approved assistant proposal) — never a configured operator name (N10v2-obs9).
Within a workspace the agent list, detail, versions, identity, invoke and every
`/api/chat/{id}*` route stay shared between members (route policy: Chat and invoke
are deliberately `MEMBER`). The two mutations that destroy or re-publish an agent are
**owner-or-admin**:

| Method | Path | Rule |
|---|---|---|
| `DELETE` | `/api/agents/{agent_id}` | `perm:agents.delete` **and** (owner or administrator) — otherwise `403 agent.forbidden` `{agent_id, owner}` before any AWS call. `409 agent.deploy_in_progress {job_id, stage, job_status}` while the agent's deploy job is `queued` / `running` (N16-obs83 — members and administrators alike, no force; wait for the job to succeed or fail, then delete). `409 agent.in_use {canaries: [{id, name, status, stage}]}` while a Runtime canary whose status is not terminal (`running`) names the agent as champion or challenger (N16-obs111 — members and administrators alike, no force; `completed` / `rolled_back` / `cleaned` canaries never block; settle the canary with `cleanup` / `rollback` / `complete`, then delete; the architect conversation purge keeps its own order — canaries first, then the agent). Both guards run before any AWS call and leave the row, the name claim and the canary untouched (`docs/cn/interfaces/agents.md` §1, §1.1). A 200 marks the row `deleted` and releases its name claim in the same commit, so `POST /api/agents` with that name answers 202 again (new id; `409 agent.name_exists` is only for a row that is not deleted — agents.md §4.4). Belt and braces: the pipeline re-reads the row before and after every stage and ends a job whose agent was deleted anyway as `cancelled` (Job / Deployment / stage status `cancelled`, `payload.cancelled {stage, reason, teardown[]}` naming what the job tore down of its own creation). Contract: `docs/cn/interfaces/agents.md` Result `{deleted: true, agent_id, aws_resource_deleted, removed[], skipped[], failed[], failed_detail{}, steps[{step, status: deleted\|skipped\|failed, detail, job_id?, reason?}]}` — **N16-obs76:** only a resource that existed counts as `removed`; an agent whose deploy never provisioned (failed at generate / package) reports `aws_resource_deleted: false` with `runtime` and `execution_role` under `skipped` (reason in `steps[].detail`), never "deleted". **N16-obs87:** the Runtime is read back before `DeleteAgentRuntime` — already gone → `runtime` is `skipped` ("already gone") and the row still goes; the control plane's `AccessDeniedException` for a delete of an id that no longer exists is re-read and, when the runtime is gone, counted as deleted — only an AccessDenied on a runtime that is still there is the `403 aws.access_denied` it was before **N17-obs18:** the same `409 agent.in_use` while an evaluation run on the agent is ACTIVE (`queued` / `invoking` / `waiting` / `evaluating`): `detail.runs: [{id, name, status}]` lists them (`detail.canaries` and `detail.runs` are both present, either may be empty); stop the run or wait for it, then delete (`docs/cn/interfaces/agents.md` §1.3). The run worker re-reads the agent at dispatch and fails a run whose agent is gone with `agent.not_found` (no version recorded). A key S3 refuses inside a 200 `DeleteObjects` is a `failed` `artifacts` / `memory_snapshots` step whose `failed_detail` reason carries the count + code (N17-obs190); the row is still deleted. **N17-obs151:** the local `data/builds/<name>` scratch of the package stage is one more residue step, `build_dir`, between `registry_record` and `log_groups` (`deleted` with the byte count, `skipped` when none exists, `failed` with the host's reason — never blocking the delete). |
| `POST` | `/api/agents/{agent_id}/redeploy` | `perm:agents.deploy` **and** (owner or administrator) — same 403. Body: the edited `AgentSpecRequest` (name and method immutable → `400 agent.redeploy_immutable`). Answers `202 {agent, job_id, deployment_id}` (the `POST /api/agents` shape) and flips the row to `deploying` while an update-mode job runs — chat answers `409 agent.not_active`, redeploy / inbound-auth / delete `409 agent.deploy_in_progress` meanwhile; the previous version keeps serving. **N17-obs16:** while a Runtime canary on the agent is `running` the redeploy is `409 agent.in_use {canaries: [{id, name, status, stage}]}` — the delete's envelope, members and administrators alike, nothing written; settle the canary (`cleanup` / `rollback` / `complete`) first (`docs/cn/interfaces/agents.md` §1.1). A `failed` agent (no live version) is re-published the same way (cold re-publish: the deploy stage falls back to Create; a failure marks it `failed` again). A failed update of an agent that already serves a version keeps the agent `active` on the previous version with `last_deploy_failed: true` / `last_deploy_error` on its projection (N16-obs110; `docs/cn/interfaces/agents.md` §4) |

`POST /api/agents` and `POST /api/agents/{id}/redeploy` take the spec as `AgentSpecRequest`
(N16-obs78): an unknown top-level key (`frobnicate`, `system_key`, `owner`, …) is
`422 validation.invalid_request` with the key in `detail[].loc` (`extra_forbidden`), like
`OnlineConfigPatch` / `UserPatch`; `created_by` / `created_by_name` keep their own
explanation — still `422 validation.invalid_request`, with `created_by: the creator is recorded
by the server and cannot be set by the client` in `detail[].msg` (N17-obs116: this route never
answers `validation.creator_not_settable`, which is the code of the dict-body routers that call
`creators.reject_client_creator`). Specs read back from the ledger stay tolerant of retired keys.

Every spec update goes through `routers/agents.py::republish_agent`, which takes the
caller's identity and applies the gate itself — so an AI-recommendation accept or a
future `PUT`/`PATCH /api/agents/{id}` cannot re-publish another member's agent either
(`tests/test_agent_ownership.py` guards the route table against an ungated write path).
A revoked permission still wins (`403 auth.permission_required` from the route policy
runs first). Discovered imports (`owner: "aws-discovery"`, externally owned — delete only
detaches the row) and system presets (`owner: "system"`, refused by their own guard) are
not member-owned. **Migration note:** rows created before this fix carry the ledger
default `owner = "river"`; an administrator can still mutate them, a member cannot. To
hand them to the administrator account: `UPDATE agents SET owner = '<admin username>'
WHERE owner = 'river';` on `data/launchpad.db` (no schema change).

## Refusal precedence — the gate order per route group

One table for the DOC family N17-obs12 / 15 / 17 / 30 / 50 / 51 (round 17): when several gates
could refuse one request, this is the order the code answers in. **Docs follow code** — where a
route group deviates from the generic order the deviation is recorded here and is the contract;
the matrix (`backend/tests/graph/refusal_matrix.py`, `KNOWN_ORDER` / `REGISTERED_PAIRS`) cites this
section. **Ruling (lead, 2026-10-20): ownership-first everywhere — a caller the ownership gate does
not admit learns no state.** The generic order, left to right: schema 422 (framework; the envelope
is the same for any id) → visibility 404 (users-and-ownership.md §7: a hidden id answers the kind's
not-found; a deleted agent's `404 agent.not_found` sits here too) → ownership 403 (§8
creator-or-admin) or the personal session's 404 → system / preset refusal → `agent.deploy_in_progress` 409
(agents.md §1) → `*.in_use` 409 (agents.md §1.1 / §1.3, evaluation-engine.md §22) → the subject's
own lookups (404 / 422) → agent status 400 → semantic body validation 422 → scope lookups 404.

| route group | ordered gates (code → contract) | deviation from the generic order |
|---|---|---|
| `POST /api/chat/{id}` and every `/api/chat/{id}/sessions/{sid}/*` send | schema 422 → hidden / deleted agent `404 agent.not_found` (§7, api.md Chat) → **session ownership `404 chat.session_not_found`** (N16-obs70/71) → agent status `409 agent.not_active` (N16-obs74) → the session's own state (`409 chat.turn_in_progress`, `409 chat.session_ended`) | none since `cn/r17-j` — **N17-obs12 was the deviation (status before ownership) and is fixed as PRODUCT**: another member's post into a deploying agent's session reads the session's 404, never the status 409; the owner's send reads 409 as before |
| `/v1/agents/{id}/invoke[-stream]` | key auth 401 → key scope / hidden agent `404 agent.not_found` → agent status `409 agent.not_active` → `409 chat.session_ended` (N16-obs88: before any AWS call) | as the chat route (`services/invoke.py`: `require_session_open` runs in both `invoke_agent_text` and `invoke_agent_events`). Global parity (2026-10-27): invoking an agent is not a policy decision — the former `403 policy.denied` / `503 policy.undecidable` invoke gate is gone; policies decide Gateway tool calls at the Gateway |
| `DELETE /api/agents/{id}` | hidden `404 agent.not_found` (§7) → **creator-or-admin `403 agent.forbidden`** (§8) → preset `403 agent.system_managed` → `409 agent.deploy_in_progress` (agents.md §1) → `409 agent.in_use {canaries, runs}` (§1.1 / §1.3) → teardown | **ownership before the deploy-job gate** (N17-obs51): another member's delete of a redeploying agent reads 403, not 409 — the opposite of the chat family's status-first order |
| `POST /api/agents/{id}/redeploy` (every `republish_agent` caller) | hidden 404 → creator-or-admin 403 → preset 403 → `409 agent.deploy_in_progress` → `409 agent.in_use {canaries}` (§1.1; runs never block a re-publish, §22) → method / pin / tool-auth validation 422 / 409 | as the delete (N17-obs51) |
| `POST /api/runtime-canaries` | schema 422 → **agent status `400 canary.agent_not_active`** (hidden agents alike, E13 §3) → `400 canary.agent_unsupported` (capability, presets included) → `409 canary.already_running` | **status before the already-running gate** (N17-obs15) |
| `POST /api/runtime-canaries/{id}/action` | hidden canary `404 canary.not_found` → hidden arm agent `404 agent.not_found` → `403 canary.forbidden` (§8, all but `verdict`) → system subject 403 → purge `409 assistant.purge_*` → `409 canary.action_in_flight` → `409 canary.stage_not_ready` → `cleanup` only: `409 canary.cleanup_requires_decision` (N17-obs452 — a running canary past setup is completed or rolled back first) → `409 agent.deploy_in_progress` (agents.md §1.2) → `409 canary.stale_base` → the action's gates (`verdict_blocked` / `override_required`, `422 dataset_required`) | — (written in agents.md §1.2) |
| `POST /api/eval/runs` | schema 422 → `422 run.target_required` → **agent status `400 agent.not_active`** (hidden / deploying / deleted alike, §23) → semantic body (`422 run.no_evaluators`, scope rules) → scope lookups (`404 dataset.not_found`) | **status before semantic body validation and the dataset lookup** (N17-obs17) |
| `POST /api/eval/runs/{id}/rerun` | hidden / missing run `404 run.not_found` → the run's own rule `422 run.rerun_unsupported` → agent status `400 agent.not_active` → stored-body validation `422 run.no_evaluators` → dataset `404 dataset.not_found` | **the run's own refusals before the agent-status gate** (N17-obs50, refines obs17 for the rerun) |
| `DELETE /api/eval/datasets/{id}` / `…/evaluators/{id}` (`?force=`) | hidden 404 → **creator-or-admin `403 dataset.forbidden`** (§8) → member force `403 auth.forbidden` (§22) → `409 *.in_use` (§22) → delete | **ownership before the force gate** (N17-obs30): a non-creator member reads `dataset.forbidden` with or without `force` |
| `DELETE /api/knowledge-bases/{kb}` (`?force=`) | hidden 404 → member force `403 auth.forbidden` (N17-obs4) → preset mount `409 kb.attached_to_system_agent` → `409 kb.has_attached_agents` (unless force) → mounting agent mid-deploy `409 agent.deploy_in_progress` (N17-obs31) → strip + delete | — |

Rules of thumb the table encodes: (1) a hidden or deleted id always answers the kind's not-found
first (§7); (2) ownership answers next, on every route — the creator-or-admin 403 before the
deploy-job 409 and the in-use 409 on the **agent mutation** routes (N17-obs51, DOC: "members and
administrators alike" in agents.md §1 means every caller the ownership gate admits), the personal
session's 404 before the status 409 on the **chat** routes (N17-obs12, fixed); (3) for a caller the
ownership gate admits, the agent-status gate answers before a subject's own session / canary / run
gates on the **create and send** routes (chat, `/v1`, canary create, run create) but after the
run's own lookups on `rerun`; (4) a member's `force` is refused after ownership on datasets /
evaluators and first of all on knowledge bases.

## Console Identity API — Connections and bound Gateway targets

The console labels Connections **Outbound credentials** (出站凭证) and the caller's grants **My authorizations** (我的授权, the `?view=mine` tab of that page since R13); paths, fields and error codes keep `connection`.

A **Connection** is an AgentCore Identity credential provider (OAuth2 or API key) in
the workspace's token vault; see [identity.md](identity.md) for the service-model
findings and the design. Reads are open to members; every mutation requires the
`identity.manage` agent permission (`perm:identity.manage` in `ROUTE_POLICY`).
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
| `DELETE` | `/api/identity/connections/{kind}/{name}` | `{deleted: true}`; refused with `409 identity.connection_referenced` while an agent spec's tool auth or a gateway target references it, and with `409 identity.in_use {referenced_by[≤20]{type: agent\|model_provider, id, name}, total, truncated}` while the provider is some agent's `model_credential` pin or a model registry provider's `credential_provider` (N13-obs22 — `launchpad-global-bedrock` answers this, not `identity.external_connection`; isolation-hidden agents are dropped from the list and `total` / `truncated` follow the masked list, so a member never learns how many hidden agents pin it; the delete is refused even when nothing visible remains — `referenced_by: []`, `total: 0`). Online evaluation configs hold no credential reference. The external rule (`409 identity.external_connection`) stays for vault-only providers nobody pins |
| `GET` | `/api/identity/gateway-targets` | `{gateway_id, targets[{target_id, name, description, status, status_reasons, source, system, auth, connection, mode, scopes}]}` for the workspace gateway |
| `POST` | `/api/identity/gateway-targets` | `201` target + `warnings[{code, message, detail}]` (non-blocking). Body `{name, description?, source: openapi\|mcp, openapi_schema \| mcp_endpoint, connection, kind, mode: as_agent\|as_user\|obo, scopes[], api_key?{location: HEADER\|QUERY_PARAMETER, parameter_name, prefix?}}`; an MCP server target takes an OAuth2 Connection only. `mode: obo` sets `grantType: TOKEN_EXCHANGE` and needs a CUSTOM_JWT gateway (`409 identity.obo_needs_jwt_gateway`) and an OBO-configured Connection (`422 identity.obo_unsupported`). When the Connection's issuer differs from the gateway's JWT authorizer issuer, the target is still created and `warnings` carries `identity.obo_issuer_mismatch` `{connection, connection_issuer, gateway_issuer}`: the IdP must trust the gateway's inbound issuer |
| `DELETE` | `/api/identity/gateway-targets/{target_id}` | `{deleted: true}`; only Connection-bound, non-system targets |
| `POST` | `/api/identity/oauth/complete` | `identity.grant` — as_user (3LO) binding leg, called by the `/auth/return` page. Body `{session_id}` (the `session_id` AgentCore Identity appended to the return URL; an opaque one-time capability) → `{completed: true, provider, agent_id, agent_name, tool}` after `CompleteResourceTokenAuth` for the user recorded with the session. The caller must be that user. `404 identity.session_unknown` (never issued, already used, or dropped by a revoke), `409 identity.session_expired` (older than 15 minutes), `403 identity.session_user_mismatch`, `502 identity.session_completion_failed`. See [identity.md §7.4](identity.md#74-routes-permission-and-errors) |
| `GET` | `/api/identity/grants` | member — the caller's **own** as_user grants, never another member's: `{grants[{connection, agent_id, agent_name, tool, scopes[], status: pending\|authorized\|revoked, force_reauth, created_at, updated_at, authorized_at, revoked_at}]}`, newest first (My authorizations / 我的授权) |
| `GET` | `/api/identity/grants/{connection}/status?agent_id=` | member — `{connection, agent_id, status: none\|pending\|authorized\|revoked, force_reauth, authorized_at}`; what the Chat auth card polls until the consent completes (`none` until an ask was recorded). A grant is usable only when `authorized` with `force_reauth: false` |
| `DELETE` | `/api/identity/grants/{connection}` | `identity.grant` — revoke (force re-auth): `{revoked: true, provider, agents}`; the next call through this Connection carries `forceAuthentication=true` and asks for consent again. The revocation clears only when a fresh consent completes |
| `GET` | `/api/identity/consent-portal` | member — `{gateway_id, portal: {id, name, status, status_reason, portal_url, execution_role_arn, connection, scopes[], audience, created_at, …} \| null}`, the workspace gateway's Consent Portal read back from AWS every time (`gateway_id: null` without a gateway) |
| `POST` | `/api/identity/consent-portal` | **admin** — `202 {gateway_id, portal}`. Body `{name, description?, connection, scopes[] (default [openid]), audience?, execution_role_arn}`: creates the portal for as_user Gateway targets (`connection` = the inbound IdP, the same issuer as the gateway's JWT authorizer; the execution role is operator-supplied). Not member-grantable: `identity.manage` does not reach it. `409 identity.gateway_missing` / `identity.consent_portal_exists`, `422 identity.invalid_portal_name` / `identity.invalid_role_arn` / `identity.role_account_mismatch` / `identity.portal_scopes`; see [identity.md §7.6](identity.md#76-consent-portal-as_user-gateway-targets) |
| `DELETE` | `/api/identity/consent-portal` | **admin** — `{deleted: true}`; `404 identity.consent_portal_not_found` |
| `GET` | `/api/agents/{agent_id}/identity` | member — `{agent_id, name, method, workload_identity{status, name, arn, allowed_return_urls}, inbound{mode: iam\|jwt, source}, downstreams[{type, name, tool_type, via: agent\|gateway, mode, connection, kind, scopes, connection_status: ready\|missing\|unbound}]}` — the read-only 身份 page |
| `POST` | `/api/agents/{agent_id}/inbound-auth` | `agent.deploy` — `202 {agent, job_id, deployment_id}`. Body `{inbound_auth: {mode: iam\|jwt, jwt?} \| null}` (`null` unpins → workspace default; the key is required and unknown keys are refused with `422 validation.invalid_request`, so a mistyped body never starts a redeploy). Re-publishes the stored spec with the pin swapped: UpdateAgentRuntime on the same runtime, new version. `422 agent.inbound_auth_unsupported` (JWT on harness / A2A), `422 agent.inbound_auth_invalid`, `409 agent.deploy_in_progress` |
| `GET` | `/api/identity/inbound-auth/default` | member — `{workspace_id, default: {mode: iam\|jwt, jwt?}, configured, cognito, cognito_issuer}`; `configured=false` = implicit IAM; `cognito` is a ready-to-use JWT config for the workspace pool, or null before bootstrap; `cognito_issuer` is the pool's issuer (the issuer of every token the console, `/v1` and evaluation present), or null |
| `PUT` | `/api/identity/inbound-auth/default` | `identity.manage` — body `{mode, jwt?: {discovery_url, allowed_clients[], allowed_audience[], allowed_scopes[], custom_claims[], source_connection?}}` → the GET shape. `source_connection` is display-only and never reaches the authorizer. The discovery document is probed first (`422 identity.discovery_unreachable` / `identity.discovery_invalid`). Deployed agents keep their authorizer until redeployed |

Error codes (`identity.*`): 409 — `connection_exists`, `target_exists`,
`connection_referenced`, `system_connection`, `system_target`, `target_unbound`,
`no_gateway`; 404 — `connection_not_found`, `target_not_found`; 422 —
`unsupported_vendor`, `missing_endpoints`, `endpoints_custom_only`,
`invalid_target_name`, `invalid_openapi`, `invalid_mcp_endpoint`,
`target_auth_unsupported`, `obo_invalid`, `obo_vendor_unsupported`, `obo_unsupported`,
`discovery_unreachable`, `discovery_invalid`; 409 — `obo_needs_jwt_gateway`. The as_user
consent and Consent Portal codes (`session_*`,
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
| `GET` | `/api/system-agents` | member | `{workspace_id, presets[{key, name, label, description, method, skill_version, installed_skill_version, update_available, status, requirements[{code, message}], name_collision, agent_id, agent_status, error, job_id, deployment_id, deployment_status, model_id, model_source, model_origin, default_changed, knowledge_bases[], allowed_tools[], memory, settings{model_id, model_source, max_tokens, reasoning_effort, system_prompt, max_iterations, timeout_seconds, knowledge_bases[]} | {}, defaults{same members}, editable_fields[], operation, catalog_version{installed, current}, snapshot_behind, can_install, can_repair, can_configure, can_uninstall, updated_at}]}` — `status ∈ configuration_required | not_installed | deploying | uninstalling | active | failed`; `model_origin ∈ default | override | null` (N17-obs202: `default` = the stored `model_id` is the `agent_default` model role as it stood at the last install / repair — the preset FOLLOWS the model-defaults page; `override` = an administrator pinned it through the settings edit; `null` when not installed) and `default_changed` is true for a following, installed preset whose stored model is not what the role resolves to now (never for an override) — `defaults.model_id` is always the role's current value; `catalog_version.installed` is the latest deployment's pinned model-catalog version of the newest **non-failed** deployment — the snapshot the Runtime serves (`null` before one; a failed re-publish's pin is ignored, N17-obs259) and `snapshot_behind` (N17-obs234) is true when that pin is older than the ledger's catalog stamp — the same verdict as the model-providers `preset_snapshot_behind` banner; `operation` is `{kind: uninstall, job_id, job_status, attempt, error, retryable} | null` while a teardown job owns the row; requirement codes `bootstrap_not_ready | missing_artifacts_bucket | missing_execution_role | per_agent_roles_disabled | missing_oauth_provider`; `memory` is `disabled`; ledger-only |
| `POST` | `/api/system-agents/{key}/install` | admin | required JSON body, a partial edit: `{model_id?, model_source?, max_tokens? (1–131072, per model call = bedrockModelConfig.maxTokens), reasoning_effort? (low|medium|high; OpenAI model on model_source=bedrock only), system_prompt?, max_iterations? (1–100), timeout_seconds? (10–3600), knowledge_bases?[{kb_id, name?, description?}], reset?[editable field…], clear?["max_tokens"|"reasoning_effort"], force?}` (`{}` = preset defaults on install, stored choices on repair; omitted = keep, `reset` = build default, `clear` = send nothing; unknown members → `422`) → `202 {agent, job_id, deployment_id, created, changed, preset}` when a job is in flight (a bodiless repair of a deploying preset returns its existing job, `changed: false`; an explicit edit while deploying → `409 system_agent.deploy_in_progress {job_id}`; a racing install returns the winner's job unless it asked for different settings → `409 system_agent.deploy_in_progress`; a partial edit is resolved against the row inside the claiming transaction with a version-conditioned claim, re-resolved on a changed row and `409 system_agent.conflict` after three attempts), `200` with the last job's id when the active preset already matches — an active preset whose `snapshot_behind` is true never "already matches": a bodiless `{}` re-publishes it like `force` (`202`, job payload `repair_reason: "catalog snapshot behind v<installed> < v<current>"`, N17-obs234); likewise a following preset whose `default_changed` is true: the bodiless `{}` re-resolves `model_id` from the `agent_default` role and re-publishes (`202`, `repair_reason: "model default changed <old> -> <new>"`, N17-obs202) — an explicit `model_id` in the body pins the model (`model_origin: override`, kept on every later repair until `reset: ["model_id"]` returns it to following the role); `422 system_agent.invalid_options` for an unsupported pairing (e.g. a reasoning effort on a Claude model). Knowledge bases are verified in the target workspace during the provision stage |
| `POST` | `/api/system-agents/{key}/skill-registration` | admin | no body (the resource is server-selected: the release the stored spec pins, proven against this build and read back from S3) → `200 {preset_key, record{system, record_id, name, type: AGENT_SKILLS, status, version, descriptors, …}, created, changed, submitted, note, skill{name, version, digest, path, files[]}, preset}` — registers the **active** preset's published Skill release as its own Registry record pointing at the immutable `system-skills/<name>/<version>-<digest12>/` prefix (no S3 write, no Harness re-publish, the agent's A2A record untouched). `created` ⇒ a new record submitted for review (never approved here); `changed` without `created` ⇒ the descriptor rolled forward to a newer release (DRAFT, review again, `recordVersion` bumped); neither ⇒ identical release, no-op (approval kept). Idempotent and race-safe (durable `clientToken`, per-preset lock); the deploy pipeline's register stage performs the same registration on install/repair |
| `DELETE` | `/api/system-agents/{key}` | admin | `202 {agent, job_id, operation: uninstall, attempt, started, preset}` — claims the row (`uninstalling`, optimistic CAS) and queues the teardown job in one commit; simultaneous requests share one job (`started: false` for the loser); the row keeps its identity until the exclusive (per-agent advisory lock, single host), fenced worker has verified every KB target, the Harness and the dedicated role are gone (per-step `progress` with exact resource ids on the job, carried into the next attempt only when verified); a failed teardown gets attempt N+1; `409 agent.deploy_in_progress` while a deploy runs. `409 agent.in_use {canaries}` while a Runtime canary whose status is not terminal still names the preset as an arm (N16-obs111a; the create route refuses presets as arms, so only a legacy row reaches this) — settle the canary, then uninstall |

`GET /api/system-agents` additionally reports `release` (this build's release of the
preset, `{version, digest, tag}`, or `null`) and `release_error` (N-ARCH-obs19: why
`release` is `null` — this build cannot validate or render the preset's bundle against the
published model catalog; `null` when it can). `POST /api/system-agents/{key}/install` on such
a build answers `409 system_agent.bundle_invalid {preset_key, skill_version, catalog_version,
reason}` with the same message, before any row, job or AWS call — never a 500.

`GET /api/system-agents[].last_job` additionally carries `trigger` (`auto` = the host's
auto-repair sweep, `null` = a card / API action) and `repair_reason` (N17-obs206): the host
re-publishes every installed, active preset that `_repair` finds stale in EVERY workspace by
itself — 60 s after startup and, debounced, 180 s after the last catalog-shaping or model-role
registry write, claimed and deployed one at a time, a preset whose last re-publish failed
left to the administrator (`system_presets_auto_repair`, model-registry.md §9); the presets
panel renders a `deploying` card whose running job has `trigger: auto` as 自动修复中 /
AUTO-REPAIRING.

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
register/import under the reserved name answers `409 registry.name_reserved`; the platform
sample skill's name (`expense-report-writer`) answers `409 registry.sample_name_reserved` for
every caller (the defaults sync registers it), and a same-name record created between the name
check and the claim answers `409 registry.name_exists` with nothing uploaded — the record is
claimed before any file lands in `skills/<name>/` (N17-obs265). Every record
also carries its creator — `created_by` (`users.id`; `null` for deploy-created A2A records,
gateway imports, `sync-defaults`, the config admin and legacy rows) + `created_by_name` —
stamped on `POST /api/registry/records` / `POST …/skills/import`; `PUT`, `POST …/reimport` and
`DELETE` are creator-or-admin (`403 registry.forbidden {created_by, created_by_name, record_id,
action}`, a `null` creator is an administrator's; N15-obs7), reads stay open to every member
(users-and-ownership.md §8). `POST /api/registry/records/{id}/action` (`{action: submit |
approve | publish | reject | disable}`): any member who can see the record may `submit` it;
`approve`, `publish`, `reject` and `disable` are an administrator's decision — a member answers
`403 registry.approval_admin_only {action}` before the visibility check and any backend call
(a hidden, a visible and a missing record answer alike), so the status never moves
(N17-obs248, 2026-10-22). Platform-owned records (shared-gateway
targets, `sync-defaults`, the sample skill) are still approved by the `system` actor inside
those paths; records a member already approved before this rule stay as they are. A platform target
record is one whose server URL is the workspace's gateway **and** that no account created;
the `system` reconcile never adopts a member-authored record, which an import reports as
`registry.name_held` (N17-obs256). The same provenance holds for the sample skill: only a
creator-less record is the platform's (N17-obs262). `POST /api/registry/records` and `PUT …/{id}` refuse an MCP
`url` equal to the workspace's platform gateway URL (scheme/host case, trailing slash and
query ignored) with `422 registry.platform_gateway_url_reserved` for every caller — those
records come from the import path only.
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
canary | promote | …`, `detail.maintenance_route`) before any AWS call;
the runtime-canary action routes answer the same for rows referencing a preset; `DELETE /api/knowledge-bases/{kb_id}` answers `kb.attached_to_system_agent`
(409, `detail.agents`) when the KB is mounted on a preset, with or without `force`;
and `POST /api/agents` (and the architect approval) with a reserved name answers `agent.name_reserved` (422,
`detail {name, reserved_for}` — the preset's name, its base name and every `<base>-<suffix>`, e.g.
`launchpad-eval-worker`; N16-obs82, `docs/cn/interfaces/agents.md` §3).
**Creation-time validation (round 16, N15-obs16).** `POST /api/agents`, every re-publish
(`POST /api/agents/{id}/redeploy`, the recommendation accept) and the architect approval
validate the spec against its method's declared domain — what the method's generate stage can
render (`deployer.pipeline.MethodCapabilities`, published as `GET /api/capabilities.methods`)
— **before any agent row, job or AWS call**: `422 agent.method_unsupported {method, field,
value, supported}` for a builtin (`field: tools.builtin`), a tool type rendered by no channel
of the method (`tools.type` — e.g. an unauthenticated `mcp` on `zip_runtime`), an acting mode
(`tools.auth.mode`), a native tool, a protocol, memory, a knowledge base, a byoc artifact
kind or a container image outside the workspace account / region (`byoc.image_uri`); `409
workspace.resource_missing {resource, how_to_fix}` when the workspace lacks a resource the
deploy needs from its first stage (`artifacts_bucket`; `gateway_id` for a Gateway tool).
`422 agent.reference_missing {field, value, kind, how_to_fix}` when the spec names a row this
workspace does not have (`knowledge_bases[].kb_id` → `kind: knowledge_base`; N16-obs73 — a
bogus id used to deploy ACTIVE and fail at the first search; a KB hidden by isolation still
answers 404 `kb.not_found` first).
`422 agent.gateway_tool_unbound {field: tools.gateway, name, reason: absent|malformed|foreign, value, workspace_gateway_id, how_to_fix}` (N17-obs487) when a Gateway tool is not bound to THIS workspace's Gateway: `config.gateway_id` must equal `resources.gateway_id`; a record-only ref (`config.record_id`, no id) is resolved against the workspace's MCP registry record and the stored spec is normalised with the id; the architect approval, which has no ledger at hand, refuses record-only refs. `agent.reference_missing` gains two kinds (N17-obs487 / N17-obs496): `kind: gateway_record` (`tools.gateway.record_id` is not an MCP record of this workspace) and `kind: a2a_peer` (`tools.a2a` — `config.record_id` must be an APPROVED A2A record of this workspace owned by an ACTIVE `protocol: a2a` agent, and `config.runtime_arn` exactly that agent's Runtime ARN in this account and partition; otherwise the consumer role would be granted `InvokeAgentRuntime` on an arbitrary runtime).
`agent.builtin_unsupported` (the N15-obs16 code) is kept for one round as an alias on the wire:
a `tools.builtin` refusal carries it as `detail.legacy_code`. A `__LAUNCHPAD_` substring in any
member-supplied text the templates embed (prompt, descriptions, names) is a schema 422 naming
the field; a `container_source` artifact needs the workspace `codebuild_project` (409
`workspace.resource_missing`, like `gateway_id`). Both builtins (`code-interpreter`, `browser`) render on the Harness
today; the authoritative list per method is `GET /api/capabilities.methods[m].builtin_tools`
(mirrored as the `methods` field of each `GET /api/tools` builtin row). Contract:
`docs/cn/interfaces/testing.md` §6.
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

| Method | Path | Role | Result |
|---|---|---|---|
| `GET` | `/api/assistant/architect` | member | `{workspace_id, account_id, region, available, reasons[], preset{key, label, status, agent_id, requirements[], can_install}, can_deploy, deploy_requirements[{code, message}], capabilities{shared_memory, memory_kind: selfhosted|agentcore|null, knowledge_bases}, is_admin, owner, principal}` — `available` ⇔ the preset is `active`; `capabilities` says which prerequisites a proposal may bind to (never created here); ledger-only |
| `GET` | `/api/assistant/architect/conversations` | member | `{conversations[{id, title, owner, mine, read_only, shared, shared_by, shared_at, turns, turn_in_progress, live_turn, status, proposal_status, proposal_revision, created_at, updated_at}]}` — `live_turn` (N-ARCH-obs15) is `{turn, seq, started_at, state: running\|finishing, author}` while a turn runs server-side, else `null` — the caller's own plus every conversation its owner or an admin shared in this workspace (`mine: false`), newest first (≤ 50); an **admin** lists every member's conversation of the workspace (≤ 200), another member's unshared one with `read_only: true` (every write answers it 404 until shared); `owner` is the creator's display username; each `messages[]` row of the detail carries `author` (the sender of a `user` row) |
| `POST` | `/api/assistant/architect/conversations` | member | body `{title?}` → `201` conversation detail (below); snapshots the workspace **catalog** (registry attachables, live gateway ARN + outbound-auth identity per gateway record, S3 content digest per skill, ACTIVE managed KBs, workspace prerequisites — the only AWS reads); `409 assistant.unavailable` (`detail.preset_status`) while the preset is not active |
| `GET` | `/api/assistant/architect/conversations/{id}` | member (owner, or anyone while shared); admin (any, `read_only` while unshared) | `{…summary, catalog{fetched_at, tools[{key, kind, name, description, attachable, reason, gateway_arn?, auth_type?, outbound_auth?, url?, record_id}], skills[{key, name, description, path, record_id, content_digest, object_count}], knowledge_bases[{kb_id, name, description}], warnings[], resources{memory_arn, memory_id, memory_kind, kb_gateway_id, kb_gateway_arn, oauth_provider_arn, execution_role_arn}, target{workspace_id, account_id, region}}, messages[{id, turn, role: user|assistant|tool|error, text, name, at}], proposals[…]}` |
| `PUT` | `/api/assistant/architect/conversations/{id}/sharing` | member (owner) or admin | body `{shared: bool}` → summary. Opens (or closes) the conversation to every member of the workspace, stamping `shared_by` / `shared_at`; does not touch `updated_at`. Only a conversation the caller can already reach (its own, or one already shared; an admin reaches every conversation, so it may share any — that is how a read-only one becomes a collaboration) — another member's private one is a 404 for a non-admin; a collaborator on a shared conversation who is neither its owner nor an admin is a 403 `assistant.share_forbidden`. While shared, every conversation route below — turns, catalog, preparation, proposal edit/reject/approve, evaluation plan and assets — accepts any member under that route's usual permission (re-checked inside each write, so unsharing takes effect immediately); only `footprint` and `DELETE` (CLEAR) stay with the owner (404 for a collaborator). An admin's read of an unshared conversation (`GET` detail, `evaluation-plan`, `evaluation-plan/operations/{id}`, a `turns/{turn}/stream` tail) never widens a write: every write route answers it 404 until the conversation is shared |
| `POST` | `/api/assistant/architect/conversations/{id}/catalog` | member | re-reads the catalog → `{catalog, conversation}`; consume the returned conversation because refreshed bindings can advance preparation and proposal revisions |
| `PUT` | `/api/assistant/architect/conversations/{id}/preparation` | member | `{expected_revision, knowledge_bases: [kb_id], skills: [catalog_key], tools?: [catalog_key]}` → full conversation detail, including `preparation`. Missing `tools` preserves existing MCP choices for older clients; `[]` explicitly removes them. Every list is unique. Validates live resource bindings and creates a new reviewable proposal revision when applicable, retaining evaluation rules for fresh validation rather than broadening them. Refuses a stale preparation revision or an in-flight turn. |
| `POST` | `/api/assistant/architect/conversations/{id}/preparation/skills` | `perm:agents.deploy` | `{expected_revision, staging_id, selections: [{index}]}` → `{conversation, results: [{name, ok, key?, error?}]}`. Compatibility endpoint for earlier assistant imports; the current console creates new Skills in Registry. Imports server-validated staged bundles into conversation-owned sources and selects successful imports; never accepts a client S3 path. |
| `POST` | `/api/assistant/architect/conversations/{id}/turns` | member | body `{prompt}` (≤ 100k chars / 300k bytes, and it must fit the request budget with the preamble) → SSE `meta{conversation_id, turn, session_id, agent, omitted_turns} → (tool|delta)* → proposal? → done` or `error{code?, message}`; a transient upstream failure first emits `retry{attempt, reason}` and replays the turn once in a fresh session (discard what was streamed so far) (errors are kept on the transcript). **N-ARCH-obs15:** the turn runs server-side to its terminal row whatever happens to this connection — each frame carries `id: <seq>`, closing the response only drops the subscriber, and `GET …/turns/{turn}/stream` reattaches; only a producer failure, a lost claim or a dead process leave the partial answer + a `turn_interrupted` error row (a proposal block that had streamed completely is still recorded). One `InvokeHarness` on the preset with the bounded replayed transcript (paired by turn, ≤ 12 turns / 160k chars incl. preamble); a `launchpad-proposal` block (≤ 64 000 bytes) becomes a new revision (`draft` or `invalid`); **no other write**. **Automatic correction (N17-obs295):** when a model proposal fails ONLY the structural evaluation-plan routing rules (a reference-driven evaluator whose reference a scenario lacks, a TRACE reference judge with a turn lacking `expected_response`, an evaluator mapped to a subset of golden tests), the turn emits `autocorrect{revision, errors[], validation_error_details[]}` and makes ONE more model call with those errors as an internal follow-up (never stored as a member message), then `(tool|delta|retry{…, phase: "autocorrect"})*` and the `proposal` of the corrected revision; the first revision is `superseded`. The transcript keeps a notice row `proposal_autocorrected` (or `proposal_autocorrect_failed` + the usual `proposal_rejected` row when the correction did not validate); `LAUNCHPAD_ASSISTANT_PROPOSAL_AUTOCORRECT=false` turns it off. Contract: [assistant-turns.md](cn/interfaces/assistant-turns.md) §11. **No block (N17-obs468):** a reply with no proposal block that imitates the replay placeholder or claims a submitted revision gets a `proposal_missing_pending` notice, `reask{revision, reason}` and ONE corrective internal call (`retry{…, phase: "reask"}`); the notice ends `proposal_missing_recovered` (a revision was stored) or `proposal_missing` (not updated); `LAUNCHPAD_ASSISTANT_PROPOSAL_REASK=false` keeps only the notice; the detail adds `turn_revisions` (§12). Before the stream opens: `409 assistant.unavailable`, `409 assistant.turn_in_progress` (`detail.active_turn` — one in-flight turn per conversation), `409 assistant.conversation_full` (200 turns), `413 assistant.prompt_too_large` |
| `GET` | `/api/assistant/architect/conversations/{id}/turns/{turn}/stream?from=<seq>` | member | Reattach to a turn (N-ARCH-obs15): replays the persisted events with `seq > from` (default 0), tails the turn while it runs, then its terminal `done`/`error`. `404 assistant.turn_not_found`; `409 assistant.turn_not_yours {author}` for another member's turn; `422 assistant.invalid_seq`. A **read-only viewer** (an admin on another member's unshared conversation, `read_only: true`) tails any turn of the conversation — the stream is a subscription and writes nothing, and that viewer has no turn of its own; the sender rule (409) applies to collaborators. Contract: [docs/cn/interfaces/assistant-turns.md](cn/interfaces/assistant-turns.md) |
| `PUT` | `/api/assistant/architect/conversations/{id}/proposal` | member | body `{content}` (the proposal allowlist; unknown outer members → 422) → `{proposal}` — a **new** revision (`source: member`, unique monotonic number), never a mutation; invalid content is stored as `invalid` with `validation_errors`, never corrected; `413 assistant.proposal_too_large` above 64 000 serialized bytes (nothing stored; the normalized stored content is re-checked against the same cap); `409 assistant.conversation_full` at 50 revisions. Every assistant write is also bounded at ingress: `413 assistant.request_too_large` above 512 000 received bytes. **`evaluation_plan` (N15-obs13):** when the body **omits** the key and the latest revision carries a seed, the seed is carried forward verbatim and the revision records `notes: ["proposal_repaired: evaluation_plan carried from revision N — the editor did not submit it"]`; an explicit `evaluation_plan: null` removes the seed (no note); a submitted seed is stored as given. Every proposal payload (here, the detail's `proposals[]`, approve / reject) carries `notes[]` (`[]` when nothing was repaired) |
| `POST` | `/api/assistant/architect/conversations/{id}/proposal/reject` | member | body `{revision}` → `{proposal}` with `status: rejected` (non-executable); a conditional transition — `409 assistant.proposal_stale` for a non-current revision, `409 assistant.proposal_already_approved` (`detail.approval`) when the revision was executed meanwhile |
| `POST` | `/api/assistant/architect/conversations/{id}/proposal/approve` | `perm:agents.deploy` | body `{revision, content_hash}` → `202 {proposal, agent, job_id, deployment_id, started: true}` when this call claimed the revision, claimed the agent name (shared with `POST /api/agents`) and created the ordinary agent + deployment + `deploy_agent` job with their ids on the proposal in one commit; `200 … started: false` with the recorded outcome for a repeated, concurrent or historical (already approved, even if newer revisions exist) request — and a still-`queued` job with no live worker is re-woken. Refusals, all before any write: `401 auth.required` / `403 auth.permission_required` / `403 workspace.forbidden` (re-resolved from the database at the claim, after the live catalog read), `409 assistant.proposal_stale` (unknown revision / hash differs / changed while approving), `409 assistant.proposal_not_approvable` (invalid, rejected, superseded), `409 assistant.already_approved` (`detail.agent_id`, `revision`, `job_id`, `agent_name`, `agent_status` — the conversation already minted its ONE Agent from an earlier revision; even a deleted Agent keeps the conversation closed to a second creation — N16-obs40), `409 assistant.workspace_not_ready`, `409 assistant.proposal_invalid` (live catalog no longer has a referenced resource or prerequisite — e.g. the KB gateway), `422 agent.builtin_unsupported` (a builtin tool the method does not render — N15-obs16), `422 agent.name_reserved`, `409 agent.name_exists` (atomic — one of two racing creators; N17-obs469: every draft / invalid proposal payload already carries `name_conflict: {name} | null` by the same live-holder rule, the console shows it as a persistent warning and disables approval, and the architect's preamble lists the agent names in use — the 409 stays the backstop), `409 assistant.bindings_changed` (`detail.changed[]` — a key resolves to a different URL, gateway auth identity, skill content, memory or KB gateway than reviewed), `502 assistant.catalog_unavailable` (live catalog unreadable and no winner exists); every already-approved answer re-validates the caller first |

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
validation_error_details[], created_by, created_at, approval, rejected_by, rejected_at}`.
`validation_error_details[]` (N17-obs296) carries each `validation_errors[]` sentence as
`{code, params, message}` — `message` verbatim; `code` is
`evaluation_plan.reference_missing {evaluator, scenario, fields[]}`,
`evaluation_plan.trace_turn_missing_response {evaluator, scenario}`,
`evaluation_plan.evaluator_partial_routing {evaluator, golden_tests[]}` or the generic
`proposal.validation_error {}`; an evaluation-plan revision carries the same member, and a
transcript notice row (`proposal_rejected`, `proposal_autocorrect*`) carries `errors[]` of
that shape. `content` is the
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
> **CN (N12 W-E5, CA-5).** The plan contract and these routes are the Global ones; the materialization substrate is Launchpad's own: evaluators become `eval_evaluators` rows (`evaluators_local`), the Dataset stays a ledger row, and a **code** evaluator's Lambda (own role + log group + one published version) is built only when `Settings.eval_code_evaluators_enabled` is on — otherwise the code chain and the code evaluator are recorded `skipped` and the operation still succeeds. No AgentCore evaluator, no Lambda resource policy, no additive execution-role grant; the SE-049 `lambda-revision-review` route does not exist (`ALLOWED_DELTA`, CA-5). Judge / derived models must be in the CN catalog (`422 model.not_allowed` at materialize).

| `GET` | `/api/assistant/architect/conversations/{id}/evaluation-plan` | member | `{plans[…], operations[…], disclosure}` |
| `POST` | `…/evaluation-plan/prepare` | member | body `{revision}` (a shape-valid proposal revision, approved or not) → `201 {plan, plans, operations, disclosure}` — the platform draft as a new plan revision; `409 assistant.proposal_stale` / `409 assistant.evaluation_plan_source_invalid`; no side effects |
| `PUT` | `…/evaluation-plan` | member | body `{content}` (the plan contract; `content.source_revision` names the proposal revision) → `{plan, …}` — a **new** revision, `draft` or `invalid` with `validation_errors` (never corrected); `413 assistant.evaluation_plan_too_large` above 160 000 bytes |
| `POST` | `…/evaluation-plan/materialize` | `perm:agents.deploy` (owner) | body `{plan_revision, plan_hash, acknowledge_disclosure: true}` → `202 {operation, started: true}` when this call atomically claimed the plan (still draft, this hash, newest revision; worker launched), `200 … started: false` for a repeated / concurrent request (same operation). `422 assistant.disclosure_required`, `409 assistant.evaluation_plan_stale` (unknown revision / hash differs / edited, superseded or claimed meanwhile), `409 assistant.evaluation_plan_not_approvable` (invalid / superseded), `409 assistant.evaluation_plan_invalid` (no longer validates against its proposal), `409 assistant.workspace_not_ready`, `409 assistant.evaluation_plan_name_taken` (an evaluator name the workspace already holds), `422 model.not_allowed` (a judge / derived model outside the CN catalog); the caller (admin + owner) and the workspace identity are re-resolved from the database inside the claim and pinned on the operation |
| `GET` | `…/evaluation-plan/operations/{operation_id}` | member | `{operation}` — ledger only, no AWS call |
| `POST` | `…/evaluation-plan/operations/{operation_id}/retry` | `perm:agents.deploy` (owner) | `{operation, started}` — resumes the persisted intents of a `partial` / `failed` operation (same requests; a read-only `existing` conflict is re-evaluated, owned conflicts stay for review); `409 assistant.evaluation_assets_exhausted` after 5 attempts, `409 assistant.evaluation_assets_running` while a worker is live |
| `DELETE` | `…/evaluation-plan/operations/{operation_id}/assets` | `perm:agents.deploy` (owner) | `{operation}` — deletes exactly the owned artifacts whose identity still matches (evaluator rows first; function / log group / role only once no owned evaluator of their chain remains), one checkpoint per effect; the local Dataset stays; `409 assistant.evaluation_assets_running` while a worker is live, `409 assistant.evaluation_assets_stopped` when the approver or workspace identity changed |
| `GET` | `/api/assistant/architect/conversations/{conversation_id}/footprint` | member (owner) | What CLEAR would remove: `{agents[{id,name,status,method}], canaries[{id,name,kind: runtime,status,agent_id,running_action,pending,resources{ab_test,endpoints[],online_evaluations}}], operations[{id,status,plan_revision,dataset_id,cloud_resources}], datasets[{id,name,item_count,cloud}], blockers[{kind: turn\|preparation\|operation\|job\|canary, id, reason}], required_permissions[], can_clear, purge{job_id,status,error,blocker,step,updated_at}\|null, turns, proposals}` — `canaries` are the Runtime canaries run on the conversation's Agents (`pending` = cleanup not run or left resources; `ab_test` is the local A/B row, there is no dedicated gateway in CN); `agents` also keeps one a failed clear already marked deleted but has not read back gone; `required_permissions` lists `agents.deploy` when cloud assets are involved and `agents.delete` when an Agent is; `can_clear` = the caller holds them all; `purge` = the newest clear job. Ledger read only |
| `DELETE` | `/api/assistant/architect/conversations/{conversation_id}` | member (owner) + every `required_permissions` key + creator-or-admin of every canary and Agent | A transcript-only conversation is deleted inline: `200 {deleted: true, conversation_id, operations_cleaned: [], datasets: [], agents: [], canaries: []}`. Anything else is a durable clear job: `202 {deleted: false, job_id, status, …}` — poll `GET /api/jobs/{job_id}` (`payload.step`, `payload.result`, `payload.blocker` on failure); `resume_pending_jobs` resumes it after a restart. In dependency order, each step read back before the next: canaries on the Agents (stop + `act_cleanup` under the canary's own `running_action` claim, then wait until the per-arm online-evaluation configs and both named endpoints are gone) → the fenced cleanup of every cleanable evaluation-assets operation → the local Datasets (a copy synced to AWS by hand stays) → every Agent (Runtime delete, wait until `GetAgentRuntime` is not found, then the residue sweep — execution role, artifacts, memory snapshots / records, its own A2A Registry record, log groups — and the ledger) → canary candidate zips → the ledger rows. Refusals before anything is deleted: `409 assistant.conversation_busy` (a turn, import, operation, deployment job or canary action is live), `403 auth.permission_required`, `403 canary.forbidden` / `403 agent.forbidden` (a canary or Agent the clear would tear down was created by another member and the caller is not an administrator — `users-and-ownership.md` §8). A live job is returned again by a repeated DELETE; while it runs conversation writes answer `409 assistant.conversation_clearing` and the Agents' canary / redeploy / delete routes `409 assistant.agent_clearing`. A job that cannot finish ends `failed` with `payload.blocker.code` one of `assistant.purge_canary_incomplete`, `conversation_assets_remain`, `purge_still_deleting`, `purge_delete_failed`, `purge_agent_dependency`, `purge_agent_conflict`, `purge_agent_busy`, `purge_role_remains`, `purge_dependency_busy`, `purge_unowned`, `purge_step_failed` (a failed Registry-record step included; or a refusal's own code); the conversation is kept and the next DELETE resumes from the verified checkpoints |

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
subset of golden tests is refused at plan validation (selection is global). `link` is the legacy
`/evaluation?view=datasets&ds=…` / `?view=evaluators&ev=…` deep link (redirected to the V2 pages
since r13-a); the V2 architect builds
its own link from the same ids (`/v2/eval/data?tab=datasets&view=dataset&id=…` /
`/v2/eval/evaluators?view=detail&id=…`). The ordinary
`DELETE /api/eval/evaluators/{id}` answers `409 evaluator.managed_by_operation`
(`detail.operation_id`) for an evaluator an operation owns. `GET /api/assistant/architect`
gained `can_materialize_evaluation_assets` (= the caller holds `agents.deploy`).

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
| `GET` | `/api/registry/records/counts` | `{pending, approved, draft, rejected, deprecated, total}` — per-status counts of the publisher list (N17-obs267): the Registry page's status tabs and the sidebar's pending-approval badge on 注册中心 / Registry (administrators only, hidden at 0, re-read on a route change and after an approve / reject / submit / delete — no polling). MEMBER, workspace-scoped, the same visibility as `GET /api/registry/records`: under isolation a member's counts leave out the A2A record of an agent hidden from them. One `list_records` read counted server-side (no per-record projection, no payload); `total` also counts any status outside the five buckets |
| `GET` | `/api/registry/records/discoverable?type=` | `{records[{record_id, name, display_name, description, type, descriptor_types[], status, status_reason, version, created_at, updated_at}], count}` — data-plane `ListDiscoverableRegistryRecords(registryId=<workspace registry>, maxResults=100)` paginated to completion with `nextToken`; `type` (optional) narrows with `filters=[{name: "recordType", values: [<GA type>]}]` and accepts the platform (`A2A`/`MCP`/`AGENT_SKILLS`) or GA (`agent`/`mcp`/`skill`) name; `type` in the rows is always the platform name. Summaries never carry `descriptors` — read `GET /api/registry/records/{record_id}` for the payload. `count` = number of rows |

Error codes: `registry.bad_type` (422, unknown `type`), `registry.unavailable` (503,
the workspace has no registry). AWS `ClientError`s map to the standard 4xx envelope
(`aws.access_denied`, `aws.throttled`, …), never a bare 500. Route policy: MEMBER,
like the other registry reads.

**Deleting a skill record removes its files (N16-obs35, `cn/r17-j`).** `DELETE
/api/registry/records/{record_id}` on an AGENT_SKILLS record deletes the row and then the current
objects under its `skills/<name>/` prefix in the artifacts bucket — unless another record or a
live agent's `spec.skills` still references the prefix, in which case the files stay and
`objects.kept_for` names the holders; `system-skills/<name>/<version>/` (presets) is never a
target. The sweep is best-effort (`objects: {objects_deleted, prefix, kept_for?, error?}`); on a
versioned bucket noncurrent versions follow the bucket's lifecycle rules. A refused object is an
S3 failure too (N17-obs118, `cn/r17-t`): the sweep deletes through the one reporting deleter
(`services/s3_sweep.py` — `DeleteObjects` in batches, its `Errors[]` read), so a raised
`DeleteObjects` or a key S3 refused inside a 200 answers `objects_deleted: false` with a redacted
`error` (`ClientError: AccessDenied (DeleteObjects)` / `1 of 1 objects refused (AccessDenied)` —
type, code, operation, never an S3 message); `objects_deleted: true` means every listed object is
gone. Before `cn/r17-t` a refused per-key delete was swallowed and reported as deleted.

## Console Registry API — Import Gateway targets

Registry → **Import Gateway targets** (both consoles) opens a dialog over these two
routes: pick an onboarded Gateway (the Governance inventory, platform gateway first),
tick targets (all by default), import, read one result per target. The platform
gateway keeps the `sync-defaults` semantics (live `tools/list`, APPROVED by `system`,
stuck DRAFT/PENDING records reconciled) — **for an administrator's request** (N17-obs257,
user ruling 2026-10-22); a member's import of the platform gateway creates new records and
only submits them (PENDING_APPROVAL, decided later by an administrator) and reconciles
nothing; the response's `auto_approved` says which. Any other Gateway is a member import (created +
submitted, PENDING_APPROVAL, an administrator approves). Details in
[docs/cn/interfaces/registry-backend.md](cn/interfaces/registry-backend.md#import-gateway-targets--selective-gateway--mcp-record-import-wb-005).

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/registry/import-gateway/preview` | `{gateways[{gateway_id, name, gateway_arn, gateway_url, auth, auth_warning, platform, live_error, will_approve, member_submissions_to_approve, targets[{target_id, target, target_status, tools, tool_source, record_id, status, imported, held, held_by, endpoint, endpoint_kind, origin, will_approve}]}]}` — read-only; `auth` ∈ `aws_iam \| none \| oauth \| unmapped`, `auth_warning` set when the authorizer has no Launchpad-managed credential; `tool_source` ∈ `live \| schema \| none`; `record_id`/`status` = the record already holding the target's name, `imported` when it is this gateway's, `held` when another server owns the name (the import refuses it); `held_by` (N17-obs250) names that holder when `held`, else `null` — `{record_id, name, status, origin, gateway_id?, gateway_name?}`, `origin: gateway` when the holder's server URL is a gateway of this same inventory (its id + name, no extra AWS call), `member` for an external MCP server a member registered. N17-obs261 (additive): `endpoint` is what the target points at, read from the inventory already listed with no extra AWS call. It is an MCP-server URL, a Lambda function ARN, an API Gateway `restApiId/stage`, or `null` for schema-only targets. `endpoint_kind` is the `targetConfiguration` member. `origin` applies to this gateway's own record: `member` when it waits in PENDING_APPROVAL because a member's sync / import only submitted it, `platform` otherwise, and `null` without such a record. `will_approve` is true when the caller's import approves that DRAFT / PENDING_APPROVAL record, which happens only for an administrator on the platform gateway. The gateway-level `will_approve` / `member_submissions_to_approve` count those rows across every target |
| `POST` | `/api/registry/import-gateway` | body `{gateway_id?, target_ids?}` (`gateway_id` omitted = the platform gateway, `target_ids` omitted = every target) → `{gateway_id, gateway_name, gateway_arn, auth, platform, live_error, results[{name, target, target_id, gateway_id, record_id, created, reconciled, status, tool_source, auth_warning?, error?, error_code?, held_by?}]}` — one item per target: created / already imported (`created: false`) / reconciled / failed (`error`, `error_code: registry.name_held`, with the same `held_by` — here only the platform gateway and the requested one are known by URL, the platform gateway named by its id). Idempotent; never a duplicate or an overwrite. Records carry `descriptors.mcp.gateway = {gateway_id, gateway_arn, auth, runtime_tools[, auth_note]}` |
| `POST` | `/api/registry/sync-defaults` | `{results[{name, type, record_id, created, reconciled, status}], auto_approved}` — the whole-platform import (every target of the platform gateway + the sample skill). N17-obs257: only host bootstrap (`make bootstrap`, the workspace bootstrap job) and an administrator's call auto-approve — new target records APPROVED by `system`, stuck DRAFT/PENDING platform rows reconciled, A member's call creates new records and **submits** them for administrator approval (PENDING_APPROVAL) and reconciles nothing; `auto_approved: false`. The sample skill follows its own rule on every trigger (N17-obs262, user ruling 2026-10-22): a new record is submitted for an administrator, and an APPROVED platform record whose bundle version moved is re-approved by `system`, because its files are the repo's own. A refreshed record still in review is re-submitted. A member-authored skill under the sample's name is never refreshed or approved; its result row carries `name_held: true` (skill rows also carry `updated`, `removed_duplicates`). A member can add targets to the platform gateway (Identity → gateway targets), so a member-run sync must not publish them. A later administrator sync / import or bootstrap promotes the platform rows a member's run left PENDING; the administrator can also approve or reject each one from the 待审批 tab first |

Error codes: `registry.gateway_target_not_found` (404, `detail.target_ids`, before any
write), `governance.gateway_not_found` (404), `gateway.not_bootstrapped` (503, no
platform gateway and no `gateway_id` given), `feature.unavailable` (503, no registry
backend).

## Console Governance API

These `/api` routes back the authenticated console. They are not part of the
public `/v1` agent invocation contract.

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/tools` | Tool catalog → `{gateway_url, gateway_error, tools[]}`: the shared Gateway's live MCP tools (`source: gateway`, `target`) followed by the builtin rows (`source: builtin`: `code-interpreter`, `browser`), each builtin with `methods: string[]` — the deploy methods that render it (`["harness"]`; a method not listed answers `422 agent.builtin_unsupported` on create, N15-obs16). `?refresh=true` bypasses the 60 s gateway cache |
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

## Console Knowledge Bases API

`/api/knowledge-bases/*` backs the Knowledge Bases console (console 04) over
Bedrock *managed* knowledge bases — `bedrock-agent` for the control plane,
`bedrock-agent-runtime` for retrieval. Only `type == "MANAGED"` KBs are
addressable: a VECTOR KB in the same account answers `kb.not_found`. Nothing is
stored locally, so every route is a live AWS call. See
[architecture.md](architecture.md#managed-knowledge-bases-console-04).

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/knowledge-bases?status=` | Every MANAGED KB with `{kb_id, name, description, status, updated_at, data_source_count, attached_agents}`; `status` is an optional exact-match filter applied after the read (e.g. `ACTIVE`) |
| `POST` | `/api/knowledge-bases` | `202` — `CreateKnowledgeBase` (`{name, description?, source: {mode: "upload"\|"existing", bucket?, prefix?}}`) returns the detail while it is still `CREATING`, plus `source_pending`; the data source is created off-request by a backend thread once the KB is `ACTIVE` (1.5–3 min), so the client polls `GET /{kb_id}` |
| `GET` | `/api/knowledge-bases/{kb_id}` | Detail: status, ARN, timestamps, `failure_reasons`, `attached_agents`, and each data source with bucket/prefix, status, its most recent ingestion jobs, `jobs_degraded` and `has_objects` (`true` when any object other than a `_text/` sidecar sits under the prefix, `false` when none does, `null` when the listing failed — the console's automatic first sync runs only on `true`; N17-obs204) |
| `PATCH` | `/api/knowledge-bases/{kb_id}` | `{description}` (≤1000 chars) → `UpdateKnowledgeBase` with the name, role and configuration read back unchanged; answers the fresh detail |
| `DELETE` | `/api/knowledge-bases/{kb_id}?force=` | Deletes the KB row, then the index and the S3 prefix (best-effort, `cleanup` / `cleanup_job_id`). `409 kb.has_attached_agents` while agents mount it; `force=true` strips it from every mounted agent's spec first — in the shared router layer before any backend, hidden-by-visibility agents included, in one transaction (N17-obs31 = N16-obs34): each stripped agent is marked `kb_stale` (`last_strip {kb_ids, at, by}` on its projection) because the deployed Runtime keeps its retrieval tool until the next re-publish, which clears the mark; a strip is not a publish (no spec snapshot — the agent's latest deploy job log records it); the response adds `stripped_agents: [{id, name}]`. **`force` is an administrator's override (N17-obs4):** a member's `?force=true` is `403 auth.forbidden` whether or not anything mounts the KB (it would strip other members' agents' specs); a member's plain delete keeps the route policy (`409 kb.has_attached_agents` while mounted). A mounting agent whose deploy job is `queued` / `running` refuses the force as a whole with `409 agent.deploy_in_progress {kb_id, agents: [{id, name, job_id, stage, job_status}]}` and nothing is written (`docs/cn/interfaces/agents.md` §1). CN has no per-agent gateway `Retrieve` targets to re-sync (retrieval is a tool inside the Runtime bundle). |
| `POST` | `/api/knowledge-bases/{kb_id}/files` | `multipart/form-data`, one or more parts named `files` (or `file`) → `{keys, job}`: the stored keys in the artifacts bucket under `kb/{kb_id}/`, and the ingestion sync the upload started once every put landed — `job` is `{job_id, status, coalesced, covers_upload}`; a job of that source still running is returned (`coalesced: true`) instead of a second one being enqueued, with `covers_upload: false` when it started before this upload (a sync started before this upload may not cover it; the next upload or a manual sync does); `job: null` when the files are stored but the sync could not start (sync by hand). Always 200 once the files are stored (N17-obs204). A KB with no `upload`-mode data source (its last one removed, or all its sources `existing`) gets one created by the upload — a fresh `kb/{kb_id}/{ds_id}/` prefix record, the same thing `POST …/data-sources {mode: "upload"}` makes — and the response then carries `data_source: {ds_id, name, mode, bucket, prefix, status, created_at}` (absent otherwise; `keys` are under that prefix; two concurrent first uploads create one source) (N17-obs242) |
| `POST` | `/api/knowledge-bases/{kb_id}/data-sources` | `201` — creates a `MANAGED_KNOWLEDGE_BASE_CONNECTOR` source from the same `{mode, bucket?, prefix?}` body and answers the fresh detail. Idempotent per S3 location: an existing connector on the same bucket/prefix is returned instead of a second one. This is also the manual repair for a KB left with no data source. Self-hosted (China) backend: the body is the data source plus `created`; mode `upload` is idempotent per KB — a KB that already has an `upload`-mode source answers **200** with that source and `created: false`, a new one **201** with `created: true` (concurrent calls create one source); mode `existing` always appends and answers 201 `created: true` (N17-obs243) |
| `DELETE` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}` | `DeleteDataSource` → `{deleted, ds_id}` (deletion is asynchronous on the AWS side). Removing the KB's last `upload`-mode source is allowed; the next upload creates a fresh one (N17-obs242) |
| `POST` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync` | Optional JSON body `{full: bool}` (default `false`; unknown fields → 422). Seeds a `STARTING` ingestion job and enqueues the prefix for the extractor → `{job_id, status}`. The default sync is incremental: documents unchanged since their last indexing (same S3 ETag + size) are skipped (N17-obs246); `{"full": true}` forces a full rebuild, re-chunking and re-embedding every document (N17-obs272, the console's FULL REBUILD). Who may sync may rebuild: any viewer of the KB (a hidden KB is 404 `kb.not_found` for both). A full rebuild while a non-stale job of the data source is running (or when that check timed out) → 409 `kb.sync_in_progress {job_id}` with nothing enqueued (N17-obs275); the incremental sync has no such check. A finished job's `statistics` carry `numberOfDocumentsScanned` / `numberOfNewDocumentsIndexed` / `numberOfDocumentsUnchanged` / `numberOfDocumentsFailed` (+ `numberOfDocumentsDeleted` when > 0), plus `prefix_empty: true` when the listing held no original (N17-obs204). A job PUT whose ack timed out is confirmed with one GET before the hub gives up (the sync still answers normally when the write was applied); if the queue hand-off fails after the job document exists, the job is `FAILED` with `failure_reasons: ["enqueue_failed: <exception class>"]` and the route answers 502 `kb.unavailable` (N17-obs236) |
| `GET` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/ingestion-jobs` | The 50 most recent ingestion jobs, newest first, in the same projection. A job still `STARTING` with empty `statistics` 30 min after it started is presented and persisted as `FAILED` with `failure_reasons: ["never_dispatched: no worker picked the job up within 30 min"]` (N17-obs236; the detail's job list applies the same rule) |
| `GET` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/documents?page_size=&token=` | One page of `ListKnowledgeBaseDocuments` (`page_size` 1–100, default 50) as `{documents, next_token, page_size}`; each document carries the KB-side `status`/`status_reason`/`indexed_at` plus S3-side `size_bytes`/`uploaded_at` joined by object key (absent when the backend cannot list the bucket) |
| `POST` | `/api/knowledge-bases/{kb_id}/query` | Retrieval playground — `{text, number_of_results?}` (1–100, default 8) → `Retrieve` with a `managedSearchConfiguration`, answering `{results}` of `{text, score, location_uri, metadata}` |

Error codes: `kb.not_found` (404 — unknown id, or a KB that is not MANAGED),
`kb.ds_not_found` (404), `kb.has_attached_agents` (409, with the blocking names
in `detail.agents`), `kb.delete_conflict` (409 — the KB is still `CREATING`),
`kb.no_files` (400 — no upload part in the form),
`kb.sync_not_ready` (409 — `StartIngestionJob` hit `ValidationException` or
`ConflictException`: the data source is still provisioning, or a sync is already
running), `kb.bucket_required` / `kb.invalid_bucket` / `kb.invalid_prefix` /
`kb.invalid_source` (400 — source validation), `kb.query_failed` (502 —
retrieval failed on the KB side, e.g. the index is still building). Any other
AWS `ClientError` goes through the global mapping above (`aws.validation`,
`aws.conflict`, …). A workspace whose resource map has no `kb_role_arn` (create)
or no `artifacts_bucket` (uploads) has not been bootstrapped and raises a `500`
naming the missing key.

## Console Memory API

`/api/memory/*` backs the read-only Memory console (console 05) over the shared
`launchpad_memory` singleton. Every console route but one is a read: no endpoint
writes events or triggers extraction, and the one record mutation is
`DELETE /api/memory/records/{record_id}?actor_id=` below (N10v2-bug6). The other
mutating surface — the `/api/memory/resources*` routes below, which manage the memory
*resources* themselves — lives in a separate router (`routers/memory_resources.py`).
Record routes answer their own codes, not the AWS mapping: `404 memory.not_found` for an
unknown, another actor's or another store's record, `403 memory.record_forbidden` on the
delete (N16-obs113; `docs/cn/interfaces/memory-backend.md` "Error codes").
See [architecture.md](architecture.md#the-memory-console-console-05).

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/memory/overview` | Resource config, long-term strategies, bounded actor count, sibling memories |
| `GET` | `/api/memory/actors` | Actors with the compound `<agent_id>__<human>` id decoded and the agent name resolved |
| `GET` | `/api/memory/sessions?actor_id=` | Sessions for one actor, joined to the ChatSession ledger when the console wrote them |
| `GET` | `/api/memory/events?actor_id=&session_id=` | Short-term events; each payload entry is `kind` `conversational` (role + full text), `json` (the JSON value serialized losslessly into `text` — `null`, `false`, `0` and `""` included) or `blob` (byte count only); unknown kinds are omitted |
| `GET` | `/api/memory/namespaces?actor_id=` | Strategy namespace templates with `{actorId}` substituted; trailing `{sessionId}` segments collapse into an actor-level prefix (`prefix: true`), a placeholder elsewhere yields `resolvable: false` |
| `GET` | `/api/memory/records?actor_id=&strategy_id=` or `?namespace=` | Long-term records for the resolved namespace. r14-a: records superseded, soft-deleted or expired by the merge strategy are hidden unless `include_superseded=true`; every item carries `updated_at`, `confidence`, `superseded_by`, `superseded_at`, `supersedes[]`, `deleted_at`, `expired_at`, `last_op`, `active` |
| `POST` | `/api/memory/records/search` | Semantic retrieval (`{query, actor_id, strategy_id?, namespace?, top_k}`) over ACTIVE records; r14-a: `score` = similarity × recency decay × confidence floor, `similarity` = raw cosine |
| `DELETE` | `/api/memory/records/{record_id}?actor_id=` | `perm:memory.manage`. One long-term record; the backend confirms the record belongs to that actor and this workspace's store before deleting (N10v2-bug6) — an unknown id, another actor's or another store's record answers `404 memory.not_found` and nothing is removed. The handler additionally requires an administrator or the creator of the agent the actor is scoped to (`decode_actor`): `403 memory.record_forbidden {actor_id, agent_id}` otherwise (an unscoped actor is admin-only); a hidden actor is the 404 first. Unlinks the record's merge-v2 history neighbours in the same operation (N16-obs114). `200 {record_id, actor_id, namespace, strategy_id, deleted: true}` |
| `GET` | `/api/memory/extraction-jobs` | Failed (retry-eligible) extraction jobs, filterable by `actor_id`/`session_id`/`strategy_id`/`status` — **not surfaced in the console**; AWS's `status` enum is `FAILED` only, so a healthy resource returns an empty list |

Memory resource management (`?view=resources`):

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/memory/resources` | Every memory in the workspace's account/region, default first, each with the live agents whose spec pins it and `managed` (see below) |
| `POST` | `/api/memory/resources` | `perm:memory.manage`. `CreateMemory` (`{name, description?, event_expiry_days?, strategies?, namespace_keys?}`) → `201` with the detail projection in `CREATING` state; the new id is registered as managed |
| `GET` | `/api/memory/resources/{memory_id}` | Detail projection: description, status, event expiry, execution role, strategies, namespace keys |
| `POST` | `/api/memory/resources/{memory_id}/adopt` | **admin**. Registers an existing account memory as managed by the workspace (after `GetMemory` resolves it — unknown id → `404 aws.not_found`); idempotent; replies with the detail projection |
| `GET`/`POST`/`PUT` | `/api/memory/resources[/{memory_id}]` — `merge` (r14-a) | The detail carries `merge {recall_half_life_days 90, max_facts_per_actor 200, max_preferences_per_actor 100, long_term_ttl_days null, actor_summary_interval 1}` with defaults filled (`actor_summary_interval` was 5 before 2026-10-29: the rolling actor summary is now rewritten after every extraction, and recall also reads the actor's recent per-session summaries directly, so a new conversation sees the previous one at once). `POST` and `PUT` accept a partial `merge` object; on `PUT`, `null` resets one knob. Unknown keys or out-of-bounds values (1–3650 / 10–10000 / 10–10000 / 7–3650 / 1–100) answer `422`. A `merge` patch alone satisfies the at-least-one-field rule |
| `PUT` | `/api/memory/resources/{memory_id}` | `perm:memory.manage`. `UpdateMemory` limited to `{description?, event_expiry_days?}` — at least one required (422 otherwise), `description` 1–4096 chars (it can be replaced, not cleared), `event_expiry_days` 7–365 (422 outside). Sends exactly `memoryId` + the given fields and never `namespaceKeys` (the API replaces that set wholesale); the reply is the detail projection read back with `GetMemory`. Not blocked by referencing agents or the platform default; unknown id → `404 aws.not_found` |
| `DELETE` | `/api/memory/resources/{memory_id}` | `perm:memory.manage`. `DeleteMemory` (irreversible); `409 memory.default_protected` for the workspace default (N16-obs113 — the code name; `memory.platform_protected` was never a code), `409 memory.in_use` (with the agents) while a live agent's spec pins it; the managed registration is dropped. Reply `{id, deleted: true, cleanup: {s3, index: ok\|skipped}, cleanup_warnings: [...]}` — the row is gone once the call answers 200; the two storage purges are best-effort and a failed one is `skipped` with a redacted warning (type + status / error code + operation, never a body; N14-obs4, N17-obs93) |

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
derived), `memory.unavailable` (502, the underlying AWS / OpenSearch call failed in a way
the console cannot translate; the `message` is the static `memory lookup failed` /
`memory operation failed` and `detail.error_type` names the exception type — never the
backend's own text, which for an OpenSearch 403 / 400 is the raw body, N17-obs92).

## Console Chat API

`/api/chat/*` backs the Chat playground over the same invoke chain as `/v1`
(`app.services.invoke`). Sessions are AgentCore Runtime sessions: the id the
console sends as `runtimeSessionId` is the one the ledger tracks.

| Method | Path | Result |
|---|---|---|
| `POST` | `/api/chat/{agent_id}` | One turn as SSE (`meta` → `delta`/`tool`/`auth_required`/`policy_denied`/`error` → `done`); **N-ARCH-obs15 v2:** the turn runs server-side to its terminal event whatever happens to this connection — each frame carries `id: <seq>`, `meta` carries `turn`, closing the response only drops the subscriber, and `GET …/sessions/{session_id}/turns/{turn}/stream` reattaches; `409 chat.turn_in_progress {active_turn}` before the stream while the session's previous turn still runs (one turn per session). `{prompt?, attachments?, session_id?}`, a missing id starts a new session (a supplied one must be 33–100 characters of `[a-zA-Z0-9][a-zA-Z0-9-_]*` — `422 chat.session_id_invalid` otherwise, before the policy gate and any AWS call; N-SCN-obs16 / N16-obs72 — and, N16-obs71, must name the caller's own session or, for an administrator, any: another member's idle session is `404 chat.session_not_found` in both visibility modes, so no turn is ever appended into it); message text or at least one attachment is required. **N16-obs78:** the body accepts exactly these keys — an unknown one (`as_user`, retired with the Cognito inbound path; `actor_id`; a typo) is `422 validation.invalid_request` with the key in `detail[].loc` (`extra_forbidden`), never silently dropped. **N16-obs79:** a `session_id` whose session was ended (`…/stop`) is `409 chat.session_ended {session_id, ended_at}` before any runtime call — the marker stays; start a new session instead. `meta.inbound = {mode: jwt, caller: user_jwt\|m2m}`; the Memory actor is `scoped_actor` in both cases. `auth_required` `{provider, tool, scopes[], url, agent_id}` is an as_user (3LO) consent ask from a tool: `url` is the single-use authorization URL (the console links it only when it is `https:`), the `sessionUri` stays server-side, and the answer keeps streaming around it. History keeps the ask as a `role: "auth"` row (`text` = the Connection, `name` = the tool) **without** the URL, so a restored ask can only be retried; poll `GET /api/identity/grants/{connection}/status` to see when consent completes. `policy_denied` (Harness; payload under *Streaming invoke*) is kept as a `role: "policy"` row (`text` = the reason, `name` = the tool) and restored as the V2 Chat policy-deny card, which then links to the Governance landing page rather than a specific gateway. **N16-obs85 (additive):** the generated runtimes also stream `tool_input {id, name, input}` (the call's arguments, ≤ 512 characters, whitespace-collapsed, cut with an explicit `… [+N chars]` marker — never parsed) and `tool_result {id, status, summary}` (≤ 256 characters; for the `browser` builtin `title: … | url: … | text: N chars` — the final URL, the title and the text length, never the page text; otherwise the first characters of the result; **C4:** credential-shaped `name=value` / `name: value` pairs and bare `Bearer <value>` are reduced to `<name>=[redacted]` in both fields, and the native `shell` / `file_operations` tools stream `summary: ""` — `status` only) after the `tool` event they belong to (matched on `id`; by `name`, then the newest tool row, when a runtime streams no id). Neither closes the answer bubble; a hub or console that does not know them ignores them |
| `GET` | `/api/chat/{agent_id}/sessions` | Replayable sessions for the agent: `{session_id, actor_id, turns, last_at, ended_at, preview, source, live_turn}` — `ended_at` is set once the console explicitly ended the runtime session, `null` while it is live or merely idle; `source` is `console` or `public_api` (a session opened through `POST /v1/agents/{id}/invoke[-stream]`, N-SCN-obs1 — such rows carry the caller's `actor_id`, default `api`) |
| `GET` | `/api/chat/{agent_id}/history?session_id=` | The rendered thread items of one session, in replay order; `live_turn` (N-ARCH-obs15 v2, additive) is `{turn, seq, started_at, state: running\|finishing, author}` while a console turn still runs server-side, else `null` — the page reattaches to it on load. Own session or administrator, in both visibility modes: another member's session is `404 chat.session_not_found` (N16-obs71); an id with no row yet is an empty thread. **N16-obs85 (additive, `role: "tool"` rows only):** `tool_input` (≤ 512 chars), `tool_result_summary` (≤ 256 chars) and `tool_result_status` (`success` \| `error`) — each present only when the runtime streamed the matching frame (`chat_messages.tool_call`); rows older than the column and runtimes without the summaries carry none of the three |
| `GET` | `/api/chat/{agent_id}/memory?session_id=` | **Chat Inspector → MEMORY rail**: the session's short-term events and long-term records from the registered memory backend, plus `actor_id` = the compound `<agent_id>__<session owner>` the console deep-links with. Own session or administrator, in both visibility modes (N16-obs71 — it used to echo another member's actor under `shared`): `404 chat.session_not_found`; `503 feature.unavailable` without a backend; `409 agent.memory_not_managed` for a foreign pinned memory |
| `GET` | `/api/chat/{agent_id}/sessions/{session_id}/turns/{turn}/stream?from=<seq>` | Reattach to a console turn (N-ARCH-obs15 v2): replays the persisted events with `seq > from` (default 0), tails the turn while it runs, then its terminal `done`/`error`. `404 chat.session_not_found` for an unknown id, another agent's / workspace's session and — N16-obs71, both visibility modes — another member's session (resolved before any turn is looked at, so neither its existence nor its author leaks); `404 chat.turn_not_found`; `409 chat.turn_not_yours {author}` only on a session the caller may read when THAT turn was sent by a different account (an administrator's turn in a member's session, or the reverse — ownership is per turn; `meta.author` names the sender); `422 chat.invalid_seq` for a `from` that is not an integer in `0..2^63-1` and `422 validation.invalid_request` for a `turn` outside that range (N16-obs86: an unbounded value used to reach SQLite as a Python int and raise `OverflowError`). A turn made through `POST /v1/agents/{id}/invoke` on the same session (sync, no event log) counts as a session turn and reattaches as `200` with a single bare `event: done {"turn": n}` — no `meta`, no `delta` (N16-obs112; the answer is in `…/history`). Contract: [docs/cn/interfaces/assistant-turns.md §8](cn/interfaces/assistant-turns.md) |
| `GET` | `/api/traces/{session_id}?lookback_hours=` | **Chat Inspector → TRACE rail** (N-CHAT-obs2, upstream parity): this session's spans read through the same multi-group Logs Insights source as the Observability views (`services/traces.py`) → `{session_id, span_count, spans[{name, category: model\|tool\|memory\|policy\|runtime\|other, start_ms, duration_ms, trace_id}], log_groups[], log_group, lookback_hours, cloudwatch_url, log_group_url, unavailable_reason}`. `lookback_hours` 1–336, default 3. Spans are sorted by start, times relative to the earliest span. The id must be a chat session of the caller's workspace (any of its agents — the chat-history ownership rule) and not another principal's private assistant session, else 404 `chat.session_not_found` before any query; the id shape is the Observability session-id alphabet (422 otherwise). Fail-soft: a Logs Insights failure answers 200 with an empty rail and `unavailable_reason` (e.g. `observability.query_failed`). **Console links are partition-aware** (C2): `cloudwatch_url` is a CloudWatch **Logs Insights** deep link on the workspace's console host (`cn-northwest-1.console.amazonaws.cn` in aws-cn, `<region>.console.aws.amazon.com` in aws) pre-filled with the rail's own query (`filter @message like "<session_id>" \| filter ispresent(name) or ispresent(spanName)`), the contributing log groups as its source (the shared `aws/spans` when none answered) and `lookback_hours` as a relative window; `log_group_url` opens the primary group (the one that contributed most spans) |
| `POST` | `/api/chat/{agent_id}/sessions/{session_id}/stop` | **END SESSION** — data-plane `StopRuntimeSession(agentRuntimeArn, runtimeSessionId)` → `{session_id, ended: true, already_ended, ended_at}`. `already_ended: true` when AWS answered `ResourceNotFoundException` (the session had already ended or idle-expired) — a success, not an error. The ledger row is kept (history stays replayable) and stamped `ended_at`; a later turn posted under the same id is refused (`409 chat.session_ended`, N16-obs79) — the console's next prompt opens a new session. Only runtime-backed agents qualify (`zip_runtime`, `studio`, `container`, discovered runtimes); a managed Harness — deployed or imported — has no session-stop operation and answers 409 `chat.session_stop_unsupported` with `detail.reason_code` (`harness`). A session of another agent or workspace — and, N16-obs70, another member's session in BOTH visibility modes (a member ends only the sessions they opened, an administrator any) — is 404 `chat.session_not_found`. A `RetryableConflictException` that outlives botocore's retries is 409 `aws.conflict` |

**Malformed and refusing runtime answers (N17-obs96 / N17-obs97, `cn/r17-o`).** The
Runtime's answer is parsed by one normaliser for every entrance
(`services/agentcore/runtime._runtime_payload_events`). Two shapes that used to be rendered
as the assistant's reply are protocol errors and end the turn on the terminal `error` event
(Chat; `detail` carries `error_type`) or the 502 `agent.runtime_error` envelope (the sync
entrances), never as text and never with a `done`: (1) an SSE `data:` line that is **not
JSON** — the AgentCore SDK serialises every frame with `json.dumps`, so a line that does not
parse is a connection dropped mid-frame (the recorded live shape is the agent template's own
error frame cut at 300 characters) or a runtime writing outside the protocol; `error_type:
MalformedFrame`, `detail.error` = the cut text (bounded), whether the line is the whole
response or follows healthy deltas (which are kept, assistant-turns.md §8.2); (2) a JSON body
carrying a **`code` plus a `state` or an `error` key** — a structured refusal such as the
evaluation worker's `{"state": "REJECTED", "code": "evaluation.kind_unsupported", "message":
…}` ack (a Runtime invoked with a payload it refuses) — `error_type: RuntimeRejected`,
`detail {code, state, error, message}`; the free-form `message` key no longer wins. A plain
`text/plain` body, the devguide's `{"answer": …}` / BYOC `{"result": …}` bodies and the
SDK's JSON-string frames render as before.

Ending is explicit: NEW SESSION in the console only forgets the id locally, so the
runtime session it leaves behind idles out on its own. END SESSION is what to press
after a re-publish — AgentCore pins a live session to the version that first
served it, so validation of the new version needs a fresh session.

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

## Console Tools API — builtin demos

`/api/demos/*` drives the two AgentCore builtin tools from the Tools page, under the hub's
own credentials (a cross-account workspace is refused up front: `409
workspace.cross_account_tool_unavailable`).

| Method | Path | Result |
|---|---|---|
| `POST` | `/api/demos/browser` | `{url, web_bot_auth?, browser_identifier?, profile_identifier?, save_profile?}` → starts an `aws.browser.v1` (or the selected Web Bot Auth browser) session, opens `url` once over the session's CDP automation stream and answers `{url, title, session_id, latency_ms, live_view_url, live_view_expires_in, viewport, browser_identifier, web_bot_auth, profile_identifier, save_profile}`; the session is kept alive for the DCV live view until `DELETE` or the 300 s expiry. **N15-obs17 (C2):** the stream and live-view URLs are signed by the hub against the data-plane endpoint boto3 resolved for the workspace region (`services/agentcore/browser_stream.py` — the recipe the Harness `browser` builtin carries; SDK 1.17.0's own builders dial a Global-only host, `ENOTFOUND` in aws-cn). `422 tools.url_blocked` (private / link-local / literal-IP targets), `422 tools.browser_profile_required`, **`409 browser.signing_unsupported_in_partition {partition}`** (N17-obs258: `web_bot_auth: true` in a partition without a Web Bot Auth signer — every aws-cn workspace; checked before any AgentCore call; `GET /api/capabilities` carries `tools: {browser_signing}` so the console hides the toggle there), `409 tools.browser_required` / `tools.browser_web_bot_auth_unavailable` / `tools.browser_profile_unavailable`, `502 tools.browser_configuration_failed`; **`502 demo.browser_failed {reason}`** for any failure after the session started (session start, stream dial, navigation — `reason` is one bounded `Type: message` line, ≤ 300 chars, never a traceback), the SDK session always stopped first |
| `DELETE` | `/api/demos/browser/{session_id}` | Stops a retained demo session → `{session_id, stopped, profile_saved}`; an unknown, expired or another workspace's id is `stopped: false` (never a 404). `502 tools.browser_stop_failed` |
| `GET` | `/api/demos/browser/options` | The workspace's Web Bot Auth browsers and browser profiles for the demo form. `502 tools.browser_options_failed` |
| `POST` | `/api/demos/code-interpreter` | `{code}` → runs it in an `aws.codeinterpreter.v1` session of the workspace and answers its output |

## Console Evaluation Datasets API

`/api/eval/datasets` holds the local scenario datasets (SQLite, the editable source
of truth) and their one AWS Dataset each. AWS datasets have a **DRAFT** plus
immutable numbered **versions**: SYNC TO AWS creates the dataset once and afterwards
replaces the draft's examples in place; PUBLISH VERSION snapshots the draft.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/eval/datasets` | `{datasets[]}` — local rows with `items`, `kind`, `has_ground_truth` and the `cloud` blob |
| `POST` | `/api/eval/datasets` | Create from items (devguide scenarios, simulated personas or legacy prompts; kind is inferred) → 201. Items may carry `split: tuning | held_out` (omitted = the deterministic default; the row reports `split_counts` / `item_splits`, §32.2). Optional `agent_id` links the agent whose runtime tool names gate every `expected_trajectory`: the other Gateway spelling is stored canonically (`tool_name_rewrites` on the response), any other unknown name is 422 `dataset.unknown_tool`, a missing agent 422 `dataset.agent_not_found` (N17-obs407, evaluation-engine.md §32.1). `/datasets/upload` takes the same field; `PUT` links with `"<id>"`, unlinks with `""` |
| `PUT` · `DELETE` | `/api/eval/datasets/{dataset_id}` | **Creator-or-admin** (403 `dataset.forbidden` `{dataset_id, action, created_by, created_by_name}`; a NULL-creator row is an administrator's). Edit (kind is immutable → 400 `dataset.kind_immutable`) / delete the local row; a synced AWS copy stays. Delete answers **409 `dataset.in_use`** `{id, referrers[{kind, id, name, status}], total, truncated, force_allowed: "admin"}` while an ACTIVE run replays it or a pipeline outputs into it (finished runs keep their snapshot and never block); an administrator may add `?force=true` → `{deleted, forced: true}`. **Two 403s, in this order (N17-obs30):** the creator-or-admin gate answers first — a member who is not the creator gets `403 dataset.forbidden` with or without `force` — then the force gate: the creator's own `?force=true` is `403 auth.forbidden` (the flag is an administrator override). Contract: `docs/cn/interfaces/evaluation-engine.md` §22 |
| `POST` | `/api/eval/datasets/{dataset_id}/sync-to-aws` | **Creator-or-admin** (403 `dataset.forbidden {dataset_id, action: "sync"}` — it rewrites the row's `cloud` blob and the shared draft). Without a live cloud copy: `CreateDataset` (inline examples) polled to `ACTIVE`. With one: edit its **draft in place** — `ListDatasetExamples` → `DeleteDatasetExamples` (skipped when the draft is empty) → `AddDatasetExamples` with the normalized scenarios, each polled through `UPDATING` to `ACTIVE`; the dataset id and published versions survive and the draft reads `MODIFIED`. A copy AWS no longer knows (`ResourceNotFoundException` on `GetDataset`) or marked `deleted` is re-created. Returns the row; `CREATE_FAILED` / `UPDATE_FAILED` / timeout → 502 `dataset.sync_failed` with the AWS `failureReason`, also recorded on the blob |
| `POST` | `/api/eval/datasets/{dataset_id}/publish-version` | **Creator-or-admin** (403 `dataset.forbidden {dataset_id, action: "publish"}`). `CreateDatasetVersion` on the row's cloud copy, polled through `UPDATING` to `ACTIVE` → the row with the new version first in `cloud.versions` and `cloud.draft_status == "UNMODIFIED"`. No live copy → 409 `dataset.not_synced`; `UPDATE_FAILED` / timeout → 502 `dataset.publish_failed` (reason recorded on the blob, versions kept) |
| `GET` | `/api/eval/datasets/cloud` | Every AWS dataset in the workspace region: `{datasets[{datasetId, name, status, schemaType, exampleCount, draftStatus, updatedAt}]}` |
| `GET` | `/api/eval/datasets/cloud/{cloud_id}` | Draft detail: `{datasetId, name, status, schemaType, exampleCount, draft_status, failure_reason, versions[{version, example_count, created_at}], runnable, has_ground_truth}` — versions newest first |
| `POST` | `/api/eval/datasets/cloud/{cloud_id}/publish-version` | PUBLISH VERSION for a cloud-only dataset → the refreshed detail above; failures as for the local route. **Derived creator-or-admin**: the caller must be allowed to mutate every local row pointing at the store (403 `dataset.forbidden {cloud_id, dataset_id, action}` names the first they may not); a store with no pointing row stays open |
| `DELETE` | `/api/eval/datasets/cloud/{cloud_id}` | **Derived creator-or-admin** on every pointing local row (403 `dataset.forbidden {cloud_id, dataset_id, action}`, before the 409). `DeleteDataset` — the draft and every version; local rows pointing at it are marked `cloud.status = "deleted"` and re-create on the next sync. **409 `dataset.in_use`** while an ACTIVE run replays it or any run pinned one of its published versions (`referrers[].dataset_version`; the `dataset.version_pinned` rule applied to the whole store); administrators may `?force=true` |
| `DELETE` | `/api/eval/datasets/cloud/{cloud_id}/versions/{version}` | **Derived creator-or-admin** on every pointing local row (403 `dataset.forbidden`, before the `dataset.version_pinned` 409). `DeleteDataset` with `datasetVersion` — one published version; the draft and the other versions stay, cached lists are refreshed |

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

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/api/eval/datasets/from-sessions` | `{session_ids[1..50], range, dataset_id XOR name, description?, first_turn_only?, dedupe?}`; optional `?sync=true|false`. **≤ 10 sessions (or `?sync=true`)** → 201 `{dataset, added, skipped[{session_id, reason}], timing{elapsed_ms, sessions, read, workers}}` (the per-session span reads run on a 4-worker pool, N-SCN-obs10; items keep the request order). **> 10 sessions (or `?sync=false`)** → 202 `{job_id, status: "queued", sessions, poll: "/api/jobs/{job_id}"}` — the dataset is written by a job (`type: eval_dataset_from_sessions`); poll `GET /api/jobs/{job_id}`: `payload.progress{read, total}`, then `payload.result{dataset{id, name, kind, item_count}, added, skipped, timing}` on `succeeded` or `payload.error{code, message, detail}` on `failed`. `?sync=true` above 10 sessions → 400 `from_sessions.sync_too_large`. Reasons: `not_found`, `no_transcript`, `no_exchange`, and (with an empty `session_id`) `duplicate` / `dataset_full`. A new dataset with nothing usable → 422 `dataset.nothing_extracted` with `detail.skipped` (on the job: `payload.error`) |
| `GET` · `POST` | `/api/eval/pipelines` | List / create a saved processing task: `{name, description, source{agent, range, status: all\|ok\|error, max_sessions ≤ 50}, processing{first_turn_only, dedupe, min_input_chars}, output{dataset_id XOR dataset_name}}` (an unknown or simulated output dataset → 404 / 400) |
| `GET` · `PUT` · `DELETE` | `/api/eval/pipelines/{pipeline_id}` | Read / replace (409 `pipeline.running` while running) / delete — the output dataset is kept |
| `POST` | `/api/eval/pipelines/{pipeline_id}/run` | `202` with the row as `running` (409 `pipeline.running` if it already is); the bounded run proceeds in the background: list the window's sessions, filter by agent/status, read at most `max_sessions` newest, extract, merge. The outcome lands on the row as `last_run{at, scanned, matched, added, skipped, dataset_id, error}` with `status` `succeeded` / `failed` (a run interrupted by a server restart reads back as `failed`); a `dataset_name` output is created on the first run and the pipeline then targets its id |
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
| `GET` | `/api/eval/evaluators` | `{evaluators[], builtin_count}` — local builtin catalog first (`source: builtin`, trajectory matchers flagged `requires_ground_truth`), then this workspace's custom evaluator rows from the ledger (`eval_evaluators`; `source: custom`, `name`, `evaluator_type`, `provider`, `status`, `definition: judge \| derived \| code`, creator fields; filtered by workspace isolation). CN has no account `ListEvaluators` call, so there is no partial-failure marker: upstream's `custom_unavailable` (#257) does not exist here — a ledger read failure is a 500 the console shows as a load error, never as an empty custom section |
| `POST` | `/api/eval/evaluators` | Create → 201 `{evaluator_id, arn}`. Common: `name` (`^[a-zA-Z][a-zA-Z0-9_]{0,47}$`), `description`. **Judge**: `instructions` (10–4000 chars, ≥1 `{placeholder}` else 422 `evaluator.missing_placeholder`), `rating_scale[≥2]` (default pass/fail), `model_id`, `level` (TOOL_CALL \| TRACE \| SESSION, default TRACE). **Derived**: `base_evaluator_id` (`Builtin.*` \| `ThirdParty.*`; unknown → 400 `evaluator.base_not_found`), `model_id`; the level is the base's. **Code-based**: `lambda_arn` (`arn:aws[-partition]:lambda:<region>:<account>:function:<name>[:qualifier]`), `lambda_timeout_s` 1–300 (default 60), `level`; the Lambda must be in the workspace Region, else 422 `evaluator.lambda_region_mismatch` (`detail: {lambda_region, workspace_region}`). `rating_scale` with a derived or code-based body → 400 `evaluator.rating_scale_not_allowed`. `CreateEvaluator` requires `level` for every definition |
| `GET` | `/api/eval/evaluators/{evaluator_id}` | `{id, name, level, description, definition, instructions, rating_scale, model_id, base_evaluator_id, lambda_arn, lambda_timeout_s, evaluator_type, provider, status}` — the other definitions' fields are empty/null (a code-based evaluator has `instructions: ""`, `rating_scale: []`, `model_id: null`) |
| `PUT` | `/api/eval/evaluators/{evaluator_id}` | **Creator-or-admin** (403 `evaluator.forbidden` `{evaluator_id, action, created_by, created_by_name}`; NULL creator → administrator-only). Full-config replace (`UpdateEvaluator` takes the complete config, so every field is sent back) with the same bodies as create minus `name` → the refreshed detail. The payload must be of the evaluator's **current** definition: a judge/derived payload against a code-based evaluator, or a code payload against a judge/derived one → 400 `evaluator.definition_mismatch` (`detail: {current, payload}`) — the evaluator is never converted. Managed ids → 400 `evaluator.builtin_immutable`. **Scoring lock (audit 7.6):** an edit that changes a SCORING field (`level`, `instructions`, `rating_scale`, `model_id`, `base_evaluator_id`, `lambda_arn`, `lambda_timeout_s` — the P7.1 definition hash) while an ENABLED online config or a running canary scores with the evaluator → **409 `evaluator.in_use`** with the referrers (the delete's detail shape); a description-only edit passes; an administrator may `?force=true` (the version moves), a member's `force` is 403 `auth.forbidden`. Contract: evaluation-engine.md §32.5 |
| `DELETE` | `/api/eval/evaluators/{evaluator_id}` | **Creator-or-admin** (403 `evaluator.forbidden`). → `{deleted: true}` (`forced: true` added on a forced delete); managed ids → 400 `evaluator.builtin_immutable`; operation-managed → 409 `evaluator.managed_by_operation` (first, forced or not). **409 `evaluator.in_use`** `{id, referrers[{kind: run\|online_config\|canary, id, name, status}], total, truncated, force_allowed: "admin"}` while an ACTIVE run, an online config (any status) or a running canary names it — finished runs never block; an administrator may `?force=true` (the referrers keep the dangling id), a member's `force` is 403 `auth.forbidden`. Contract: `docs/cn/interfaces/evaluation-engine.md` §22 |

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
run queue (`eval_max_concurrent_runs`, default 3, capped at 20 — each run is one job on
the self-hosted evaluation worker, so the Global active-batch-evaluations account quota
of 5 does not bind on the CN build). Run status: `queued → invoking → waiting → evaluating → completed |
failed | stopped`. Every row carries `stop_requested` (an operator stop is pending
on a run whose batch is still STOPPING).

`POST /api/eval/runs` also takes an optional operator-facing `name` (1–64) and
`description` (≤ 1000) — console V2 lists runs as named evaluation tasks — and every
run row carries them (null on unnamed runs) plus `updated_at`. Every run row also carries
`hidden: {agent, dataset}` (N15-obs8): when workspace isolation hides the run's dataset
(local row or cloud store) or agent from the caller, the reference keeps its id,
`dataset_name` / `agent_name` is `null` and the flag is `true` — the console renders a
placeholder; administrators, the run's creator and shared workspaces always read the names.

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
| `GET` | `/api/eval/runs/{run_id}` | One run (scores / insight trees / `batch_eval_id` / `budget_stops` — dataset scenarios that ended on the agent's own budget and are scored as they stand, `[{scenario_id, session_id, code, stop_reason}]` / `scenario_failures` — simulated persona scenarios skipped after their replay attempts while the rest of the run went on, `[{scenario_id, retries, code, error}]` (`[]` when none; the run fails only when every scenario failed — a predefined scenario that failed to replay is listed under `session_failures` instead) / `errors` — evaluator records that errored on the worker, `{count, total, first_type, first_message}` / `session_failures` — the sessions a partially failed evaluators run left unscored and why, `{total, failed, kind telemetry_incomplete|replay_failed|evaluator_error|mixed, sessions: [{session_id, index, kind, error_type, message}]}`, null on a clean run / `error` / `stop_requested`). |
| `GET` | `/api/eval/scorecard?agent_id=` | **Agent scorecards**, ledger-only (no AWS call) → `{runs, agents, truncated}`. `runs` = the workspace's evaluator-mode runs the viewer may see (insights runs excluded; under workspace isolation the run's creator rule applies, and runs of an agent hidden from the viewer are dropped), the newest 1000, oldest first (`truncated: true` when older ones were left out): `{id, name, agent_key, status, error, created_at, updated_at, evaluators, scores, sessions, scope, dataset_name, dataset_version, benchmark, snapshot, created_by_name}`. `agent_key` = the agent id, or `logs:<service_name>` for a log-source run. `scope`: `dataset` \| `cloud` \| `window` \| `sessions` \| `logs`. `benchmark` = `{kind, key, label, dataset_id, revision, size}`: a local dataset is `dataset` / `ds:<id>`, labelled with its current name (the name stored on the run when the dataset is gone), with `revision` = the fingerprint of the items the run replayed (16 hex of sha256 over the normalized scenarios; null on older rows); a cloud dataset is `cloud` / `cloud:<id>` with `revision` `v<N>` (pinned) or `draft:<fingerprint>`; every passive run (window, sessions, logs) is the agent's `live` benchmark (`key: "live"`, null label/revision). `size` = the replayed scenario count. `agents` (newest evaluation first) = `{key, agent_id, name, method, log_source, deleted, current_seq, versions: [{seq, aws_version, created_at, created_by_name, note}]}` — `current_seq` is the newest deployed snapshot; a deleted agent keeps its runs under the stored name with an empty timeline. With `agent_id` the read narrows to that agent (listed even before its first run when visible) and each version adds `changes`: the diff groups (`prompt`, `model`, `tools`, …) against the newest deployed snapshot before it. **N15-obs15 (isolation):** a run whose dataset the viewer cannot see keeps `dataset_id` / `benchmark.key` but answers `dataset_name: null`, `benchmark.label: null`, `benchmark.hidden: true` and `hidden: {agent: false, dataset: true}` (`benchmark.hidden` / `hidden` are additive and always present); administrators, the creator and shared workspaces see the names |
| `GET` | `/api/eval/runs/compare?a=&b=` | **Two runs side by side** → `{runs: {a, b}, alignment, alignment_reason, cases, results, config}`. Both must be terminal evaluator runs (else 422 `run.compare_unsupported`); a run that is missing, of another workspace or hidden → 404 `run.not_found`. `runs.*` = the run payload plus its `benchmark`. `alignment`: `scenario` (both runs recorded which scenario each session replayed — `EvalRun.scenario_map`), `order` (an older run without that map, paired by position only when it skipped no scenario and has exactly one session per scenario of its dataset's current items, or of its pinned cloud version) or `none` with `alignment_reason` (`live_traffic`, `different_benchmark`, `scenarios_skipped`, `session_count_mismatch`, `dataset_unavailable` — gone, unreadable or hidden from the viewer): overall scores only, and no results are read. When aligned, `cases` = `[{scenario_id, input, a: {session_id, results} \| null, b: …}]` in B's scenario order, then A-only ones (`input` ≤ 200 chars), with each side's judge records from its batch results stream (one read per side, as `GET /runs/{id}/results`); `results` = `{a: {available, reason?, detail?, truncated?}, b: …}` — a side that cannot be read degrades alone (its cases carry empty `results`). `config` = `{agent_id, from_seq, to_seq, between: [{seq, aws_version, note, created_at, created_by_name}]}` when both versions are known and differ (`between` = the snapshots after the lower seq up to the higher), `{same: true, agent_id, seq}` on one version, `null` when a version is unknown, the agents differ or the agent is hidden from the viewer. The diff itself is `GET /api/agents/{id}/snapshots/diff?from_seq=&to_seq=`. CN: a run's judge records come from the ledger `eval_results` rows (`service.run_results`), not a CloudWatch results stream. Each side is the run payload (`dataset_name` / `agent_name` masked under isolation as `GET /api/eval/runs` masks them) plus `benchmark`, whose `label` is null and `hidden` true when the dataset is hidden from the viewer (N15-obs15) |
| `GET` | `/api/eval/runs/{run_id}/results` | **Per-session judgements** of a terminal evaluators run — `{available, sessions: [{session_id, results: [{evaluator_id, level, score, label, explanation, error_type, error_message}]}], count, truncated}`. The row only stores per-evaluator averages; the judge's explanation of every score lives in the batch's own results log stream (`GetBatchEvaluation.outputConfig.cloudWatchConfig`, the same `gen_ai.evaluation.result` records the online-evaluation views read), which this route reads on demand and never persists. Sessions follow the run's `session_ids` order. `available=false` + `reason` (`insights_run` \| `no_batch` \| `run_active` \| `stream_missing` \| `unreadable` + `detail`) instead of an error when there is nothing to read. Unknown → 404 `run.not_found` |
| `POST` | `/api/eval/runs` | Start a run (exactly one scope: `dataset_id` \| `cloud_dataset_id` \| `session_ids` \| `lookback_hours`) → 201. `session_source: "logs"` (display only, `session_ids` scope only — else 422 `run.session_source_scope`) records the scope as `dataset_name` `logs:<n>`, like `window:<N>h`. **Target**: exactly one of `agent_id` or `log_source {service_name, log_group_names[1..10]}` (else 422 `run.target_required`). A `log_source` run evaluates telemetry already in CloudWatch with no platform agent behind it (e.g. an agent not hosted on AgentCore Runtime): the batch's `cloudWatchLogs` source is `serviceNames: [service_name]` + `logGroupNames` as given, only passive scopes apply (`session_ids` / `lookback_hours`; a dataset scope → 422 `run.log_source_scope`), and every log group must exist (422 `run.log_group_missing`, `detail.missing`). The row echoes `log_source` with `agent_id: ""` and `agent_name` = the service name. A `cloud_dataset_id` scope may add `dataset_version` (a published version number such as `"2"`, never `DRAFT`; omitted = the draft): the version must exist in `ListDatasetVersions` (else 422 `run.dataset_version_unknown`, no run row) and `GetDataset` / `ListDatasetExamples` read that snapshot. `dataset_version` with any other scope → 422 `run.dataset_version_scope`. Every run row echoes `dataset_version` (`null` for draft, local, session and window runs). **Evaluators** (mode `evaluators`, the default): omitted → the built-in set; an explicit `evaluators: []` → 422 `run.no_evaluators` before any row (N13-obs18 — a run that scores nothing is refused; insights runs ignore the field). **Insights windows** (`mode: insights` + `lookback_hours`): the window is resolved to session ids at request time (one Logs Insights query); zero sessions → 409 `run.no_sessions` (`detail.lookback_hours`) before any row or job (N13-obs19), otherwise the row carries the resolved `session_ids` from creation. Evaluators-mode windows are still resolved on the queue worker. **Refusal precedence (N17-obs17):** the target's status gate answers first — a `deploying` / `failed` / `deleted` / hidden `agent_id` is `400 agent.not_active` — then the body (`422 run.no_evaluators` for an explicit `evaluators: []`, `422 run.target_required`, …), then the scope lookups (`404 dataset.not_found`); so `evaluators: []` on a non-active agent reads 400, not 422. **Trajectory tool names (N17-obs407):** a dataset scope's `expected_trajectory` names are checked against the run's agent (evaluation-engine.md §32.1) — the other Gateway spelling is replayed canonically and recorded as `tool_name_rewrites` on the row, any other unknown name is 422 `run.unknown_tool` `{agent_id, unknown, agent_tools, suggestions}` before any row. **Split (D3-d / R3-c):** `split: all | tuning | held_out` (default `all`; dataset scopes only, else 422 `run.split_scope`) replays only that split's scenarios — an empty one is 422 `run.split_empty`; the row echoes `dataset_split` and each `session_scenarios[]` entry its `split` (evaluation-engine.md §32.2). |
| `POST` | `/api/eval/runs/{run_id}/stop` | **Stop an active run** → 202 with the run. A run whose batch exists on AWS (`batch_eval_id` set) is stopped with `StopBatchEvaluation`: the batch goes `STOPPING → STOPPED`, the sessions already judged keep their results, and the poller records the run as `stopped` with those partial scores / insight trees and `error = "stopped by operator"`. A run still `queued` is cancelled locally (the worker skips it, AWS is never called) and returns `stopped` at once. A run replaying its dataset or waiting for telemetry (no batch yet) stops between prompts and never calls `StartBatchEvaluation`. Terminal runs (`completed` / `failed` / `stopped`) → 409 `run.not_active`; unknown → 404 `run.not_found`. `DeleteBatchEvaluation` is deliberately not exposed — the ledger keeps the partial results AWS would drop |
| `POST` | `/api/eval/runs/{run_id}/recheck` | `eval.run` — **Re-read a failed run's batch from AWS** → 202 with the run. For a `failed` run that started a batch (`batch_eval_id` set): one `GetBatchEvaluation`; a terminal batch settles the row exactly as the poller would have (e.g. `completed` with scores, or `failed` with the batch's own reason), a batch still running puts the row back to `evaluating` with a fresh poller. Reads only — nothing is re-run or billed. Other runs → 409 `run.not_recheckable`; unknown → 404 `run.not_found`. Every batch wait is bounded by `eval_batch_wait_s` (default 7200 s, 30 s polls); a run that outlives it fails with "batch evaluation still <STATUS> after the N-minute wait" and can be re-checked from the V2 task detail |
| `POST` | `/api/eval/runs/{run_id}/rerun` | `eval.run` — **Evaluate the run's target again** on the same scope → 201 with the new run, started exactly as `POST /api/eval/runs` would start it: the same validation and visibility refusals (incl. 422 `run.no_evaluators` for a source whose stored evaluator list is empty); the caller is the new run's creator (CN: no per-person budget gate — budgets are not ported). Body `{name?}` (1–64; default `<source name> · 再评`). The same target (agent or `log_source`), evaluators and description, and the same dataset (a cloud run's pinned `dataset_version` stays pinned, the draft stays the draft — the examples are read again) or the same window length (`window:<N>h` → `lookback_hours: N`). A simulated-persona dataset plays the user with the platform's actor model (`eval_actor_model_id`). A chosen-session or log-stream run (it would only re-score the same sessions) and an insights run → 422 `run.rerun_unsupported`; unknown → 404 `run.not_found` **Refusal precedence (N17-obs17):** as on `POST /api/eval/runs` — the source agent's status gate (`400 agent.not_active`) before the stored body's validation (`422 run.no_evaluators`) before the dataset lookup (`404 dataset.not_found` for a deleted dataset). |
| `DELETE` | `/api/eval/runs/{run_id}` | `eval.run` — **Remove a run that produced no result** (`failed` / `stopped`) from the ledger → `{deleted, run_id, status, aws_batch_left_in_place}`. Only the row goes: a batch evaluation that reached AWS is left as it is (AWS stays the source of truth; the row was the console's pointer and the answer names it). `completed` runs are the evaluation history and active runs must be stopped first → 409 `run.not_deletable`; unknown → 404 `run.not_found`. The Evaluation runs table and the assistant's NEXT STEPS history show ✕ on such rows |
| `GET` | `/api/eval/recommendation-providers` | **The recommendation generators the console can offer** → `{providers: [{id, label, requires_source, supports, models: [{model_id, label, available}], default_model_id, enabled}]}` (`gepa_lite` default; `harness_optimizer` `enabled` only when the workspace opted in). No AWS call. Lived at `GET /api/experiments/providers` until the configuration experiment retired (N13-obs2) |
| `GET` | `/api/eval/runs/{run_id}/recommendation-inputs` | **What a recommendation from this run would revise** → `{source, system_prompt, tools: [{name, description, origin}], notes, evaluators, default_evaluator, eligible, reason_code}`. `source: "harness"` = a Managed Harness read live (`GetHarness`: the system prompt, each `inline_function` description, and every attached `agentcore_gateway`'s target tool schemas named `<target>___<tool>`, all narrowed by the Harness `allowedTools`); `"spec"` = the Launchpad agent spec (prompt + discoverable tools); `"manual"` = nothing readable (BYOC without a prompt, CloudWatch-sourced runs, deleted agents) — the console then requires both inputs. `notes` names what could not be read (`remote_mcp_runtime_only`, `gateway_unreadable`, and `harness_unreadable` when the run outlived its Harness or GetHarness is denied — the inputs then fall back to the spec / manual instead of failing). `evaluators` = every evaluator the job can optimize toward, as `[{id, name, level, group: run|builtin|third_party|custom, recommended}]`: the run's own first, then the devguide's two recommended targets (`Builtin.GoalSuccessRate` / `Builtin.Helpfulness`, `recommended: true`), then AWS built-ins, third-party managed evaluators and this account's ACTIVE custom ones (each custom judge read back with `GetEvaluator`). `excluded_evaluators` = `[{id, reason}]` for what cannot be a target — `lower_is_better` (penalty scores), `ground_truth` (trajectory matchers, judges reading `{expected_response}` & friends, managed reference-reading code evaluators), `categorical` (non-numeric judge scale), `unavailable` (the run's evaluator no longer exists). `POST` applies the same rules (422 `recommendation.evaluator_<reason>`). `tools_eligible` is false for a run without session ids (a time-window run) |
| `GET` | `/api/eval/runs/{run_id}/recommendations` | Recommendations started from this run, newest first → `{recommendations: [{id, kind, recommendation_id, status, input_source, system_prompt, evaluator, tools, skipped_tools, result, error, …}]}`. Each non-terminal row is refreshed with one `GetRecommendation` on read (no background poller); a job that ends without text reads as `FAILED` with AWS's own error. A tool job refused because some tools never appear in the traces is restarted **once** without them (`skipped_tools`) |
| `POST` | `/api/eval/runs/{run_id}/recommendations` | `eval.run` — **Start recommendations** `{kinds: ["system_prompt" \| "tool_descriptions"], input_source, system_prompt?, evaluator?, tools?: [{name, description}]}` → 201 `{recommendations}`. One `StartRecommendation` per kind, scoped to the run's sessions: the system-prompt job pins `agentTraces.batchEvaluation` to the run's batch; the tool-description job refuses that source (live `ValidationException`), so it gets the same sessions' spans inline (`sessionSpans`, read with the on-demand evaluation query, ≤ 20 000 spans) and tools whose name appears nowhere in them are skipped up front (`skipped_tools`). Only a `completed` run with a batch → else 409 `recommendation.run_not_completed` / `run_no_batch`; a tool job on a run without session ids → 409 `run_no_sessions`, with expired telemetry → 409 `no_spans`, with no traced tool → 422 `tools_not_traced`; empty prompt / no tools / a tool without description → 422. Every refusal happens before the first Start, so a request never leaves one kind running and the other unstarted; a custom judge with a categorical rating scale → 422 `recommendation.evaluator_categorical`. Results are shown for review/copy; only the accept route below applies one |
| `POST` | `/api/eval/runs/{run_id}/recommendations/{rec_id}/accept` | `agents.deploy` — **Accept** a `COMPLETED` system-prompt recommendation into a new Harness version → 202 `{agent, job_id, deployment_id, recommendation}`. Re-publishes the run's platform-deployed Harness through the same guards and update-mode deploy job as `POST /api/agents/{id}/redeploy` (UpdateHarness → a new immutable version; `DEFAULT` follows it). The platform-appended `## Knowledge bases` section is stripped from the recommended text first — the recommendation revised the *live* prompt, which already carries it. Records `accepted {by, at, agent_id, previous_version, job_id, deployment_id}` on the row (then exposed by the list route); accepted once → 409 `recommendation.already_accepted`; a tool-description recommendation → 400 `recommendation.accept_kind`; not completed / no text → 409 `recommendation.not_completed`; a non-Harness agent → 400 `recommendation.accept_not_harness` |
| `POST` | `/api/eval/runs/{run_id}/recommendations/{rec_id}/revise` | `agents.deploy` — **Publish a manual edit** of an ACCEPTED system-prompt recommendation (N17-obs464), body `{system_prompt, expected_version}` → 202 `{agent, job_id, deployment_id, recommendation}`: a new Harness version on top of the live one through the accept path (same eligibility, owner-or-admin, preset / converted refusals; snapshot note `recommendation edited`), recorded as `accepted.edits[] {by, at, version_before, job_id, deployment_id, system_prompt}`. Pinned: the agent must still be `active` on `expected_version` — checked, then claimed by a compare-and-set in the publish transaction, so a concurrent publish answers 409 `recommendation.version_moved {expected_version, current_version}`; not accepted yet → 409 `recommendation.not_accepted` (accept takes the edit); empty → 422 `recommendation.accept_prompt_empty`. The row payload adds `edits` and `final_prompt` (the text an edit starts from) |
| `GET` | `/api/eval/queue` | `{running, queued, locked, max_concurrency}` — cancelled runs leave the queue immediately, so the count covers active runs only |

## Console Online Evaluation API

`/api/eval/online/*` manages AgentCore **online evaluation configs** — continuous,
sampled scoring of live sessions. AWS is the source of truth; the ledger keeps
identifiers only. Every config in the workspace account is listed and classified
by `owner`: `agent` (created here for an agent), `experiment` (`can_*` arms owned by
runtime canaries, plus legacy `exp_*` arms of the retired configuration experiment —
read-only), `external` (anything else).

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/eval/online` | `{configs, total}` — all configs, newest first, with `owner`, both statuses, `failure_reason`, evaluators, sampling, timeout, `matched_agent` (external rows whose log group matches a workspace agent), `duplicate_enabled` (two ENABLED agent configs on one agent), `results_log_group` |
| `POST` | `/api/eval/online` | Create for an active agent: `{agent_id, mode: scores\|insights (scores), evaluators[1..10] (scores mode), insights[1..3] ⊆ Builtin.Insight.FailureAnalysis\|UserIntent\|ExecutionSummary + clustering_frequencies[0..3] ⊆ DAILY\|WEEKLY\|MONTHLY (insights mode), sampling_percentage 0.01–100 (omit → 10 scores / 100 insights), session_timeout_minutes 1–1440 (15), filters[0..5], description?, enable_on_create (true)}` → 201 row (`status` starts `CREATING`). Mixing kinds → 422 `online_eval.mode_conflict`; rows carry `mode` (derived: `insights` non-empty) |
| `GET` | `/api/eval/online/{config_id}` | Full detail incl. `filters`, `data_source`, `execution_role_arn` |
| `PATCH` | `/api/eval/online/{config_id}` | `owner=agent` only: any of `description, sampling_percentage, session_timeout_minutes, filters` plus the mode's own analysis field — `evaluators` (scores) or `insights` / `clustering_frequencies` (insights; complete lists, `[]` frequencies clears clustering); the other kind → 422 `online_eval.mode_conflict`, mode is immutable. The body is **closed** (N13-obs21): any other key — protected ones such as `agent_id` / `mode` / `config_id` or a typo — is 422 `validation.invalid_request` (`detail[].type: extra_forbidden`, naming the key) and the row is untouched; nothing is silently ignored. The backend re-sends the complete `rule` (AWS replaces it as a unit) |
| `POST` | `/api/eval/online/{config_id}/pause` · `/resume` | Flip `executionStatus` (`agent` + `external`) |
| `DELETE` | `/api/eval/online/{config_id}` | Delete on AWS + drop the ledger row (`agent` + `external`); the results log group is left in place and named in the response |
| `GET` | `/api/eval/online/{config_id}/results?range=1h\|6h\|24h\|7d` | Logs Insights over the results log group: `evaluators[{evaluator_id, level, mean, count, sessions, labels}]`, `series{evaluator: [{bucket, mean, count}]}`, `recent[≤50]` with judge `explanation`, `errors{count, first_message}`; empty collections while nothing has been evaluated yet |
| `GET` | `/api/eval/online/{config_id}/reports` | Insights **reports** = batch evaluations sourced from the config: `{config_id, mode, reports[{batch_id, name, status, run_status, created_at, updated_at, insights, sessions{completed, failed, in_progress, total}, origin: aws_scheduled\|console, run_id, error}], aws_unavailable}` newest first (`aws_unavailable: true` when ListBatchEvaluations failed — console rows only) — console runs from the ledger (`EvalRun.dataset_name == "online:<config_id>"`) merged with AWS-scheduled batches attributed by `GetBatchEvaluation.dataSourceConfig.onlineEvaluationConfigSource.onlineEvaluationConfigArn` (only source-less summaries are candidates; one Get each, cached per batch id). Any owner may read |
| `POST` | `/api/eval/online/{config_id}/reports` | RUN REPORT NOW `{range: 1h\|6h\|24h\|7d (24h)}` → 202 `{run_id, status, queue_position}`: agent-owned insights configs only (403 / 422 otherwise); an `EvalRun(mode=insights, dataset_name="online:<config_id>")` through the bounded run queue whose batch uses `onlineEvaluationConfigSource` — it covers only the sessions the config **sampled** in the window and inherits the config's insights (AWS rejects explicit evaluators/insights on that source) |
| `GET` | `/api/eval/online/{config_id}/reports/{batch_id}` | `{batch_id, name, status, created_at, updated_at, time_range, sessions, insights{failures, userIntents, executionSummaries}, error_details}` (`parse_insights` trees, same as a Runs-page insights run); 404 `online_eval.report_not_found` when the batch is not sourced from this config |

Filter shape: `{key: "[a-zA-Z0-9._-]+", operator: Equals|NotEquals|GreaterThan|LessThan|
GreaterThanOrEqual|LessThanOrEqual|Contains|NotContains, value: {stringValue|doubleValue|booleanValue}}`
(exactly one typed value).

Error codes: `online_eval.no_telemetry` (400, the agent has no telemetry log group
yet — run one session first), `online_eval.evaluator_unsupported` (400, trajectory
matcher / unknown built-in / custom judge that needs ground truth),
`online_eval.read_only` (403, action not allowed for that owner: an experiment / canary arm
config — except pause / resume once its owning canary or experiment is terminal, N17-obs445;
experiment-owned rows carry the additive `owner_run {kind, id, status, terminal, created_by,
created_by_name}`, null on agent-owned rows),
`online_eval.not_found` (404), `online_eval.conflict` (409, name collision after one
retry), `online_eval.workspace_not_bootstrapped` (400), `online_eval.invalid_filter`
/ `online_eval.bad_range` (422).

Results appear only after a session is idle for `session_timeout_minutes`; custom
evaluators referenced by an ENABLED config are locked by AWS (no edit/delete).

Online scores also surface where sessions are looked at:

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/observability/sessions/{session_id}` | The session detail carries `online_scores: {configs[{config_id, config_name, owner, agent{id,name}?, records[{time, evaluator_id, level, score, label, explanation, trace_id}]}], total, unavailable, configs_exist}` — every config's result records for that session (agent-owned blocks first), read with one prefix `SOURCE logGroups(namePrefix: ['/aws/bedrock-agentcore/evaluations/results/'])` query. Fail-soft: a results-query failure sets `unavailable: true` and never removes traces or transcript; `configs_exist` is whether the workspace has an agent-owned config (the UI hides the block when neither results nor configs exist) |
| `GET` | `/api/observability/sessions/{session_id}/transcript` | Conversation only — `{session_id, transcript}` with the same `transcript` shape as the session detail, and **no** Logs Insights query (the V2 evaluation result drawer's read). Optional `agent_id` attributes a session no ledger row claims (an id from another workspace is ignored); optional `since` (ISO time of its first span) then lets a session with nothing in Memory rebuild its turns from that agent's runtime content logs — the session detail passes its own first-span time the same way. Eval-run sessions with no memory events rebuild turns from the runtime's otel-rt-logs content records — for a harness, from its backing runtime's log group (`harness_<name>-<id>-DEFAULT`), which is the only copy when the harness has memory disabled. A session an evaluation run replayed (a key of its `scenario_map`) needs neither parameter: the run supplies its agent and start, and the transcript carries `source: "eval"` + `run_id` (N17-obs432). A member who cannot see that run gets `404 observability.session_not_found` |
| `GET` | `/api/observability/traces/{trace_id}?range=` | The trace waterfall `{trace_id, range, meta{root_operation, service, agent, session_id, session_ids[], start, duration_ms, span_count, llm_count, status: ok\|error, tokens{input,output,cache_read,cache_write,total}, malformed{rows, attrs, status, resource}}, tree[], spans[]}`. **`meta.malformed` (additive, N16-obs53 / rev-r16):** a Logs Insights row that is not an OTEL span object is never a 500 — a non-JSON or non-object `@message` row is skipped (`rows`), a span whose `attributes` / `status` / `resource` is not an object is rendered with empty ones (`attrs` / `status` / `resource`) — and every skip / coercion is counted here, with ONE `WARNING` per trace in the backend log naming the shapes (never row content). All four are `0` for a clean export; a non-zero count means the waterfall shows fewer or barer spans than the trace had. |
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

## Console Skill Lab API — ownership of mutations (r13-g)

Every Skill Lab route is `MEMBER`; the mutations are **creator-or-admin** (403
`skill_lab.forbidden` `{job_id | taskset_id, action, created_by, created_by_name}`; a row with
no recorded creator — a seeded sample, a legacy row — is an administrator's): `POST
…/jobs/{id}/cancel` · `…/resume` · `…/publish` · `DELETE …/jobs/{id}` (the job's creator);
`PUT` · `DELETE …/tasksets/{id}` and `POST …/jobs/{id}/apply-expansion` (the **target task
set's** creator — the write lands there). `POST …/jobs/{id}/import-taskset` stays open to every
member who sees the job (it creates a new set owned by the importer). The gate runs after the
visibility 404 and before any status check (`skill_lab.job_not_cancellable`,
`skill_lab.taskset_in_use`, `skill_lab.sample_readonly` …). Contract:
`docs/cn/interfaces/users-and-ownership.md` §8.

## Console Skill Lab API — evaluation results

The Skill Lab evaluation detail (`/skill-lab?view=eval&job=<id>`) reads one
finished job's judged rows from the CLI's `out/results.json`; the file, not the
ledger, is authoritative and is re-read per request. The route is unchanged;
its response gained a validated token-usage projection. The same table carries
the two routes that save a reviewed taskgen result.

| Method | Path | Result |
|---|---|---|
| `POST` | `/api/skill-lab/jobs`, `/api/skill-lab/jobs/{job_id}/resume` | Submit an `eval` / `train` / `taskgen` job (resume: an interrupted or failed train job) → 201 job row, queued. **Before any row** the hub is probed: the exec worker + artifacts bucket (503 `skill_lab.not_provisioned`), an assumed-role workspace (400 `skill_lab.workspace_unsupported`) and the Global Bedrock bearer the host-side CLIs and judge need — a hub without `AWS_BEARER_TOKEN_BEDROCK` answers 503 `skill_lab.model_credential_missing` at submit (N13-obs20; formerly 201 followed by `failed "failed to start: the hub cannot reach Global Bedrock: …"`). |
| `GET` | `/api/skill-lab/jobs/{job_id}/results` | `{summary, rows[]}` for an eval job (a taskgen job returns `{type: "taskgen", count, tasks, summary}` instead). `summary` = `{tasks, passed, invalid, pass_rate, soft_mean, duration_s, judge_prerequisite_missing[], token_usage}`; each row = `{id, task_type, hard, soft, score_valid, duration_s, judge_status, judge_reason, judge_error, error, judge_prerequisite, response (excerpt), artifacts[{path,size}], usage, judge_usage, token_usage}`. The job's status is not checked: the file is served as soon as the CLI has written it (before the process exits), and `404 skill_lab.results_pending` is the answer until then (also the final answer for a job that ended before scoring). |
| `POST` | `/api/skill-lab/jobs/{job_id}/import-taskset` | Save a **succeeded taskgen** job's generated tasks as a NEW single-mode task set. Body `{name, tasks?}`; `tasks` is the reviewed selection — `[{index, id?, question?, rubric?, task_type?}]` (max `MAX_TASKS_PER_SPLIT` entries) where `index` is the row's position in the job's `generated_tasks.json` (strict int, no bool/string/float coercion) and the four optional fields are the only author edits accepted; unknown keys (e.g. `files`, `attachments`) are `422 validation.invalid_request`. Rows absent from `tasks` are excluded, an omitted field keeps the generated value, `task_type: ""` clears it; the server rebuilds `files`/attachments from the job snapshot. `tasks` omitted or `null` → every generated row verbatim (legacy). `201 {job, taskset}`. Errors, all before any write: `400 skill_lab.not_a_taskgen_job`, `409 skill_lab.job_not_finished` / `skill_lab.already_imported` / `skill_lab.results_missing`, `422 skill_lab.taskgen_empty_selection` (empty `tasks`), `422 skill_lab.taskgen_bad_selection` (index out of range or repeated), `422 skill_lab.taskgen_duplicate_id` (edited ids not unique), `422 skill_lab.taskset_invalid` (the validator subprocess, e.g. an unsafe id), `404 skill_lab.job_not_found` outside the caller's workspace. |
| `POST` | `/api/skill-lab/jobs/{job_id}/apply-expansion` | Append a **succeeded expansion** job's generated tasks to its target task set/split. Body optional: `{tasks?}` with the same selection shape and rules as `import-taskset`; no body / no `tasks` appends every generated row. The **edited** ids are re-checked against every current split of the target (`409 skill_lab.expansion_conflict` names the colliding ids), other splits are preserved, and the write is a validated full-replace. `200 {job, taskset}`; `400 skill_lab.not_an_expansion_job`, plus the same `409`/`422`/`404` family as import. Both routes leave the job's `generated_tasks.json` and attachment snapshot untouched. |
| `GET` | `/api/skill-lab/jobs/{job_id}/artifacts?path=` | The job's `out/` tree, any status. A directory → `{kind: "dir", path, dirs[], files[{name, size}]}`; a file → `{kind: "text", path, size, truncated, content}` (UTF-8, `content` capped at 512 KB with `truncated: true` beyond it) or `{kind: "binary", path, size}` (NUL byte / undecodable). A job that has not created `out/` yet (queued, or running before the CLI wrote anything) answers an **empty root listing**, not an error; a missing or vanished sub-path is `404 skill_lab.artifact_not_found`. Absolute, `~`, backslash or NUL paths and anything resolving (symlinks included) outside `out/` are `400 skill_lab.bad_path`. |
| `GET` | `/api/skill-lab/jobs/{job_id}/artifacts/raw?path=` | The file's exact bytes as a download (`Content-Disposition` filename), never capped; `404 skill_lab.artifact_not_found` for a directory or missing file, same `400 skill_lab.bad_path` guard. Both routes 404 `skill_lab.job_not_found` for a job outside the caller's workspace. |
| `POST` | `/api/skill-lab/jobs/{job_id}/publish` | Body `{reapprove?: bool}` → `200 {record_id, name, new_version, status_before, status_after, reapproved, pending_approval, unchanged}` — pushes a **succeeded train** job's `best_skill.md` onto its source Registry record as a bumped minor version (`update_record`; the record settles into DRAFT; `reapprove` re-approves a previously-APPROVED record through the lifecycle — for an administrator; a member's `reapprove` only re-submits it, `status_after: PENDING_APPROVAL` and `pending_approval: true`, N17-obs248). **Idempotent on content (N16-obs44):** when the record's latest version already carries exactly this best_skill.md (its SKILL.md descriptor, or this job's last publish while the record is still at that version) the call writes nothing and answers the existing `new_version` with `unchanged: true`, `status_after == status_before`. Creator-or-admin on the job (`403 skill_lab.forbidden`) **and on the source record** (`403 registry.forbidden {created_by, created_by_name, record_id, action: "publish"}` — a member may train on any readable record but publish only onto their own; a `null`-creator record such as the platform sample is an administrator's; checked before any split or write, so the record's bytes and status stay as they were — N17-obs263); `400 skill_lab.publish_unsupported` (not a succeeded train job / upload source), `400 skill_lab.publish_no_change` (training accepted no edits), `404 skill_lab.results_pending` |

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

## Console API keys

`/api/apikeys` mints and manages the `X-Api-Key` credentials the public `/v1` surface
accepts. Keys are workspace-scoped (a key reaches its own environment's agents only) and
stored as sha256 hashes — the plaintext is returned exactly once by `POST`.

| Method | Path | Auth | Result |
|---|---|---|---|
| `GET` | `/api/apikeys` | member | `{keys: [{id, name, prefix, enabled, created_by, label, created_at}]}` — this workspace's keys; `created_by` is the login identity that minted the key (`null` on rows minted before N11 r3-A); `label` is the optional display label given at creation (`null` when none — the Chat → API panel records the agent it was minted from, N-SCN-obs4; keys stay workspace-wide and the label never scopes `/v1`) |
| `POST` | `/api/apikeys` | member | `{name, label?}` (label ≤ 120 chars, display only) → `201` — the row plus `key` (full plaintext, shown once); `created_by` = the caller |
| `POST` | `/api/apikeys/{id}/disable` / `…/enable` | member | Toggle `enabled`; a disabled key is `401 auth.invalid_api_key` on `/v1` and stays listed |
| `DELETE` | `/api/apikeys/{id}` | member (**creator**) or admin | Removes the row for good → `{deleted: true, key_id}`; a client still holding the plaintext is 401 on `/v1` from the next request. `403 apikey.forbidden` `{key_id, created_by}` for anyone else — including every member on a legacy row whose `created_by` is `null` (administrator-only). Another workspace's id is `404 apikey.not_found` (N10v2-obs11) |

## Console Workspaces API — bootstrap

Provisioning a registered workspace (`architecture.md` "Workspaces — multi-account/multi-region
environments"). Hub-global, administrator-only; the target is the path parameter, never the
`X-Workspace` header (the environment is not usable yet). Progress is polled through
`GET /api/jobs/{job_id}` with `X-Workspace: <workspace_id>`; `payload.stages` carries the
per-stage records `{name, status, detail}`, `payload.mode` the run's mode.

| Method | Path | Auth | Result |
|---|---|---|---|
| `POST` | `/api/workspaces/{workspace_id}/bootstrap` | admin | `202 {job_id, workspace_id, bootstrap_status, mode: "full", stages[]}` — queues the FULL staged job (`validate-access → iam → model-credential → storage → codebuild → gateway → registry → memory → skill-lab → observability → policy → finalize`); a retry after a failure is a resume (the stages the previous full job completed are carried forward as `succeeded` with the detail `already provisioned by an earlier run`). `400 workspace.default_not_bootstrappable` for `default`; `409 workspace.bootstrap_conflict {workspace_id, bootstrap_status}` while the workspace is `bootstrapping` or `ready`; `404 workspace.not_found` |
| `POST` | `/api/workspaces/{workspace_id}/bootstrap` with body `{"validate_only": true}` (or `{"stages": ["validate-access"]}`) | admin | `202 {…, mode: "validate-only", stages: [validate-access]}` — runs the `validate-access` stage ALONE: the hub's credentials are probed for every service the bootstrap needs in the workspace's region, nothing is provisioned, and when the job ends the workspace row is handed back the `bootstrap_status` it had (`registered` or `failed` — never `bootstrapping`, `ready` or a new `failed`). The job's terminal status is the verdict: `succeeded` with the stage detail `<region> usable · 5 services probed`, or `failed` with the stage's own sentence (`<service> <Operation> failed in <region>: <Code> — the hub's credentials must be able to provision this workspace`; `<service> is not usable in <region> (…) — region not supported`; `<service> <Operation> failed in <region> after 2 attempts (<redacted cause>) — transient — retry the bootstrap; …` after the one retry of a transport timeout / throttle / 5xx — N17-obs132; `<service> endpoint (<service> · <region>) unreachable from this host after 2 attempts (<Operation>: EndpointConnectionError) — network/DNS, not the region: …` after the one retry of a resolved-but-unreachable endpoint — N17-obs200, the host redacted to service + region; `cannot assume this workspace's role (…)`). A validate-only job is never carried forward by a later full run. A probe a backend restart interrupts is not re-run: startup marks the job `failed` (`interrupted by a backend restart before the probe finished …`) and hands the row back its status, so the next probe or full run is accepted (a full job interrupted the same way is resumed instead). Any other `stages` subset is `422 workspace.bootstrap_stages_unsupported {stages, supported}`; the same `400` / `409` / `404` as the full run apply. The release smoke `backend/scripts/e2e_workspace_region_smoke.py` drives this mode (deploy.md §8.1) |
| `GET` | `/api/workspaces/{workspace_id}/bootstrap` | admin | `{workspace_id, bootstrap_status, job: {id, status, mode, stages[]} \| null}` — the latest run of either mode, so a browser that did not start it can find and watch it (live polling stays on `GET /api/jobs/{job_id}`). `404 workspace.not_found` |

## Console Workspaces API — member visibility

Who sees the resources members create in a workspace (Family B step 2; upstream #262 /
#263). Hub-global, administrator-only; the target is the path parameter, like grants.

| Method | Path | Auth | Result |
|---|---|---|---|
| `GET` | `/api/workspaces/{workspace_id}/settings/visibility` | admin | `{resource_visibility: "shared" \| "isolated"}` — absent or unknown stored values read as `shared`. `404 workspace.not_found` for an unknown id. Every row of `GET /api/workspaces` carries the same `resource_visibility` |
| `PUT` | `/api/workspaces/{workspace_id}/settings/visibility` | admin | body `{resource_visibility: "shared" \| "isolated"}` → the stored value; any other value is `422`. Changes no resource and applies from each member's next request (the workspace row is re-read per request). Works on a workspace that is not bootstrapped yet |

**What `isolated` changes.** A member sees a resource only when its `created_by` is their
`users.id`, is `null`, or names a *current* administrator; administrators see everything.
A hidden resource answers the kind's **existing** not-found — `dataset.not_found`,
`run.not_found`, `pipeline.not_found`, `online_eval.not_found`, `evaluator.not_found`,
`canary.not_found`, `skill_lab.job_not_found` /
`skill_lab.taskset_not_found`, `kb.not_found`, `memory.not_managed`, `agent.not_found`
(every `/api/agents/{id}*` and `/api/chat/{id}*` route, `/v1`), `job.not_found`,
`registry.record_not_found` (a hidden agent's A2A record), `memory.actor_not_found` — so
a hidden id is indistinguishable from a missing one, and it lands before any AWS call and
before the D20 policy gate. Lists filter in SQL before `total` / paging (`GET
/api/eval/runs`, `/api/runtime-canaries` cap after filtering);
create-time references are checked (`POST /api/eval/runs`, `/api/eval/pipelines`,
`/api/eval/datasets/from-sessions`, canary actions: a hidden dataset or run is a missing
one; a hidden custom evaluator is an unknown one — `run.evaluator_not_found`,
`online_eval.evaluator_unsupported`, `canary.evaluator_unsupported`). Agents: `POST /api/agents` and `…/redeploy` refuse a
pinned KB / memory the caller cannot see (`kb.not_found` / `agent.memory_not_managed`;
redeploy checks only pins **added** relative to the stored spec); the `agent.name_exists`
409 carries `agent_id: null` when the holder is hidden; `GET /api/agents/discovery` and
`…/discovery/import` return `managed: true` with `managed_agent_id` / `agent_id` and names
`null` for a resource a hidden agent manages; KB summaries / details carry
`attached_agent_count` (every mounting agent) next to `attached_agents` (visible names
only); `memory.in_use` / `kb.has_attached_agents` / `identity.connection_referenced` 409s
keep the true count but name only visible agents. `/v1`: a key follows its **creator's
current** visibility — a hidden agent is `404 agent.not_found` on invoke and absent from
`GET /v1/agents`; admin, config-admin and legacy (NULL-creator) keys are unrestricted.
Observability rows (traces, sessions, the Chat trace rail `GET /api/traces/{session_id}`)
attributable to a hidden agent are dropped and their details `404`; memory-console actors
`<hidden agent>__<human>` and the sessions / namespaces carrying them are dropped or `404`.
Overview: `eval_runs` / `eval_pass_rate`, `active_sessions` and `registry_assets.agents`
are the viewer's; `GET /api/overview/online-quality` is cached per viewer. Internal paths
(deploy, resume, workers, purge, presets) are never filtered. CN: the step-1 member rules
(own API keys, own chat sessions) hold in both modes; D20 policies stay shared.
**Chat sessions by id.** Chat sessions are personal in BOTH modes (N16-obs70 / N16-obs71):
`GET …/history`, `GET …/memory?session_id=`, `POST …/sessions/{sid}/stop`, the reattach
stream and a `POST /api/chat/{agent}` naming an existing `session_id` answer another member's
session `404 chat.session_not_found` under `shared` as under `isolated` (own session, or any
as an administrator — one resolver, `routers/chat._session_for`). The observability by-id
surfaces (`GET /api/traces/{sid}`, observability session / trace details) stay open under
`shared`; under `isolated` the same own-sessions predicate gates them and the observability
`traces` / `sessions` lists: another member's session is `observability.session_not_found` /
`observability.trace_not_found` and its rows are dropped. A session with no ledger row stays
visible. `GET /api/policies/entities` stays
member-readable with key ids / names / creators (authoring needs them; ids are not
secrets).

## Console Model Providers API — owner-bound providers (N17-obs203, r17-mp)

User decision 2026-10-21: model providers are owner-bound resources. Every signed-in member
registers their OWN providers (`scope: personal` — visible to them and to administrators only),
reads / changes / deletes them, adds models, probes and registers keys; an administrator does
everything, shares a provider with the workspace (`scope: workspace`) and may change or delete
members' rows. Hub-global (`/api/settings` prefix). Contract:
`docs/cn/interfaces/model-registry.md` §4 / §4.1; ownership rules
`docs/cn/interfaces/users-and-ownership.md` §10.

| Method | Path | Auth | Result |
|---|---|---|---|
| `GET` | `/api/settings/model-providers` | member | `{catalog_version, providers[], models[], workspaces[], warnings[]}` narrowed to what the caller may see: workspace-scope providers + the caller's personal ones (and only their models / probe warnings); `workspaces` is `[]` for a member. Every provider carries `created_by` (users.id), `created_by_name`, `scope`, `owned` |
| `POST` | `/api/settings/model-providers` | member | body `ProviderCreate` + `scope?` + `api_key?` (write-only — probed against the endpoint FIRST, `422 model_provider.probe_failed` stores nothing; then written to the row's SSM SecureString by the console host and registered with Identity; the response carries the key's last four characters only (`ProviderOut.key_last4` — N17-obs283: `ProviderOut` has one flat credential shape, the nested `credential` mirror was removed); `502 model_provider.ssm_write_failed` names the IAM statement; a Bedrock row → `422 model_provider.api_key_unsupported`). A member's row is `personal` (omitted or explicit; `workspace` → `403 model_provider.forbidden {scope, action: scope}`); an administrator's defaults to `workspace`. A personal provider's SSM parameter / Identity provider are owner-derived (`launchpad-user-<uid>-<name>-api-key` / `launchpad-user-<uid>-<name>`, no host-env variable) — a different `credential_ssm_parameter` is `422 model_provider.invalid_ssm_parameter {expected}` |
| `GET` | `/api/settings/model-providers/{provider_id}` | member | the row, or `404 model_provider.not_found` when it does not exist OR is another member's personal provider (indistinguishable) |
| `PUT` / `DELETE` | `/api/settings/model-providers/{provider_id}` | member (creator-or-admin) | `DELETE`: `409 model_provider.builtin` for the hub Global Bedrock row only (the legacy `siliconflow` seed is deletable — N17-obs207); `409 model_provider.in_use {referrers, agents[{id, name, workspace_id, system_key}], roles[{role, field, model_ids}]}` while a non-deleted agent spec (system presets included), a platform-role override or an evaluator names one of its models. After the visibility 404: `403 model_provider.forbidden {provider_id, action, created_by, created_by_name}` for anyone but the creator or an administrator — raised before the body is validated; a NULL creator (built-in / legacy rows) is an administrator's. `scope` on `PUT` is administrators only; **sharing (`personal → workspace`) transfers the row to the workspace — `created_by` becomes NULL, the former creator loses every mutation right (N17-obs225)**; on a workspace-scope row `base_url`, `credential_ssm_parameter` and `api_key` are administrators only too — a non-admin creator (legacy re-pointed row) changing one of them is `403 model_provider.forbidden {action: base_url \| credential_ssm_parameter \| api_key, scope: workspace}` (an unchanged echo of the same value is not a change); a personal row's `credential_ssm_parameter` cannot be changed (422). **N17-obs228:** a `base_url` change on a row holding a registered key needs `api_key` in the same body, otherwise the stored key is cleared (console-written SSM parameter, Identity provider, hub record, cache — best-effort) and the row is `configured: false` until a key is entered again; the journal note names the old and new host |
| `POST` | `…/{provider_id}/credential`, `…/probe`, `…/models`; `PUT` / `DELETE` / `POST …/probe` on `…/models/{model_ref}` | member (creator-or-admin on the provider) | same two gates; `is_default` on a model stays an administrator's (`403 … action: is_default`); `POST …/credential` (with or without a body) on a workspace-scope row is an administrator's (`403 … action: credential`, N17-obs225 — a body-less call registers the OPERATOR's parameter for the row's Identity provider, a typed key overwrites it). `POST …/credential {api_key}` (or `PUT /{id} {api_key}`) = "replace key": probe → SSM write → Identity rotation, `CredentialOut.last4`; an empty body keeps the D21 SSM-read registration. `DELETE /{id}` deletes a console-written parameter best-effort (residue in the journal note), drops the hub's cached key for the name and marks the hub / spoke `model_credentials[<name>]` record `configured: false` — a same-named successor provider starts unconfigured (N17-obs226 / obs229) |
| `GET` | `/api/settings/model-providers/changes?target_id=` | admin | the journal; `target_id` narrows it to one provider's audit trail (N17-obs203 §4: one row per create / update / scope flip / key entry / delete — console user, provider, action, time, host and scope in `before` / `after`, a key's last four characters at most) |
| `PUT` | `/api/settings/model-providers/workspaces/{workspace_id}/models` | admin | the per-workspace flags (unchanged) |

**What a user may USE (N17-obs203 §6).** `GET /api/capabilities` lists, for the caller, the
models of workspace-scope providers plus the caller's own personal providers (administrators:
everything) — every picker reads it. Naming another member's personal provider's model is refused
where it is named: `POST /api/agents` / `POST /api/agents/{id}/redeploy` → `422
agent.model_not_visible {model_id}` (its owner or an administrator may deploy it); an evaluation
run's `actor_model_id` → `422 run.model_not_visible`; a platform role → `422
models.model_not_allowed` reason `personal_scope`; a judge / derived evaluator's `model_id`
(`POST` / `PUT /api/eval/evaluators`), the judges of an assistant evaluation plan at approval,
a recommendation's `model_id` (`POST /api/eval/runs/{id}/recommendations`) → `422
models.model_not_visible {model_id}` (N17-obs227 — the hub would spend the other member's key).

`base_url` needs no operator allowlist any more (N17-obs203 §2): any public https host passes;
the console host resolves it before every request and refuses loopback / private / link-local /
IMDS / internal names (`422 model_provider.invalid_base_url`). `model_provider_allowed_hosts`
(non-empty = lock mode) and `model_provider_denied_hosts` are operator yaml / env settings; a
refusal is `422 model_provider.host_not_allowed {host, setting, mode: lock | deny}`.

## Console Model Roles API — platform model per job (r15-e)

Which catalog model the platform itself uses or pre-selects for each job (upstream #275
subset: roles only — no prices, Claude SDK, Studio or codex roles). Hub-global; the tab lives on
Settings → Model Providers (`?view=roles`). Contract: `docs/cn/interfaces/model-registry.md` §12.

| Method | Path | Auth | Result |
|---|---|---|---|
| `GET` | `/api/model-defaults` | member | `{roles: [{role, field, model_id, default, overridden_by, updated_at, needs_tools, allow_empty, default_disabled, kind, feature}]}` — roles `agent_default`, `judge`, `judge_fallback`, `actor`, `prompt_opt`, `skill_lab_judge`, `skill_lab_target`, `memory_extract`, `codegen` (Settings-backed, `kind: chat`) and `embedding` (N17-obs252: the registry's active embedding row, `field: ""`, `kind: embedding`; `feature ∈ skill_lab | studio | null` names the console feature a role is hidden with); a member reads the same rows with `overridden_by` redacted to `""` when overridden (N17-obs251 — the Model roles tab is read-only for members; writes stay admin); `model_id` = the administrator's override, else env / `launchpad.yaml` / the built-in default (`default`); `overridden_by` is null when not overridden. The V2 pickers pre-select from here |
| `PUT` | `/api/settings/model-roles/{role}` | admin | body `{model_id}` → the role row (`embedding`: the registry's guarded default flip — `409 embedding.reindex_required` while any vector index holds documents, N17-obs252). `404 models.unknown_role`; `422 models.model_not_allowed {role, model_id, reason}` (`empty \| not_in_catalog \| not_chat \| not_embedding \| disabled \| personal_scope \| no_tool_use` — a role accepts only an enabled chat row of the registry, tool-capable where the role runs tools; `""` only for `judge_fallback`). Persisted in `platform_settings`, applied at once (no restart), journaled as `model_catalog_changes` `model_role.set` (audit-only: the catalog version does not move) |
| `DELETE` | `/api/settings/model-roles/{role}` | admin | → the role row at its default again; journaled `model_role.reset` (a no-op without an override writes nothing) |

A model (or provider) a role points at can neither be deleted nor **disabled** —
`409 model.in_use` / `model_provider.in_use` with the setting referrers (`{kind: setting, id:
<field>, role, model_ids}`, `suggest: "repoint"`) — until the role is repointed. Probing a role's
model is the registry's own `POST /api/settings/model-providers/{provider_id}/models/{model_ref}/probe`.

## Console Accounts API

`/api/auth/*` gates the console and `/api/users/*` manages the accounts behind
it. Neither surface touches AWS. See
[architecture.md](architecture.md#console-authentication-and-accounts).

| Method | Path | Auth | Result |
|---|---|---|---|
| `GET` | `/api/auth/status` | open | `{auth_required, authenticated, registration_enabled, registration_requires_approval, password_policy, username, role, email, account_expires_at, permissions}` — identity fields are null (`permissions`: `[]`) until authenticated; `password_policy: {mode: strict \| relaxed, min_length}` is the effective password policy the registration form hints from (additive, N14-obs3; the login response carries it too) |
| `POST` | `/api/auth/login` | open | Sets the `launchpad_session` cookie (`HttpOnly`, `SameSite=Lax`, `Secure` in prod; absolute lifetime `LAUNCHPAD_AUTH_SESSION_TTL_SECONDS`, default 12h, clamped to the account validity — not refreshed on activity) and echoes the identity. Protected by the per-account lockout and the per-IP window (see *Login protection* below) |
| `POST` | `/api/auth/register` | open | `201` — creates a `member` account; by default `status=pending` with `expires_at=null` until an admin approves it, then valid for `auth_registration_valid_days` (default 7). Per-IP window on *failed* attempts (`LAUNCHPAD_AUTH_REGISTER_IP_LIMIT` 10 / `…_WINDOW_SECONDS` 3600 → further failures `429 auth.rate_limited`; a valid registration is never refused by it), pending-queue cap (`LAUNCHPAD_AUTH_REGISTRATION_PENDING_MAX` 50 → `409 auth.registration_backlog`), and no enumeration: under approval mode a taken username / email (or the reserved admin name) answers **exactly like a pending registration** and creates nothing; under instant activation both collisions answer one `409 auth.registration_rejected` |
| `POST` | `/api/auth/logout` | session | Clears the cookie |
| `GET` | `/api/users?q=&status=all\|pending\|active\|expired\|disabled&limit=&offset=` | admin | Paged account list with derived `state` / `days_remaining`, plus the live lockout state `failed_logins` / `locked_until` (ISO or `null`) |
| `GET` | `/api/users/stats` | admin | Totals including the `pending` approval queue, `expiring_soon` (≤3 days), 7-day registration/sign-in counts, a 14-day registration series, top email domains |
| `PATCH` | `/api/users/{id}` | admin | Any of `status` (`pending`\|`active`\|`disabled`; `active` on a pending account approves it and starts its window), `role`, `extend_days`, `expires_at` (`null` = never expires), `password` (`null` = generate and return once), `permissions` (`{permission_key: bool}`, `null` = all granted), `workspaces` (full replacement of the account's workspace grants, `null` clears them), `unlock` (`true` clears the sign-in lockout and failure counter) |
| `DELETE` | `/api/users/{id}` | admin | Removes the account. `409 users.self_delete` for the caller's own row; `409 users.last_admin` when the row is the last active, non-expired administrator (the config admin does not count) |

Registration error codes: `auth.registration_disabled` (400, gate off or
registration disabled), `auth.invalid_username` / `auth.invalid_email` /
`auth.email_domain_blocked` / `auth.weak_password` (400),
`auth.registration_rejected` (409 — taken username **or** email, instant-activation mode
only; under approval mode a collision is a `201` pending-shaped answer with no row, so the
public form never discloses which names exist — the journal records the collision),
`auth.registration_backlog` (409 — `auth_registration_pending_max` pending accounts are
waiting for review; `detail {pending, max}`; journaled `WARNING`), `auth.rate_limited`
(429 — `auth_register_ip_limit` *failed* attempts from one address in
`auth_register_ip_window_seconds`; further failures only, `Retry-After`). `auth.username_taken` / `auth.email_taken` (409) remain the *service-level*
codes an administrator-created account reports.

**Last-admin / self guards** (`PATCH` and `DELETE /api/users/{id}`, before any write):
`409 users.last_admin` when the change would leave zero active, non-expired `role=admin`
rows — demoting (`role=member`), disabling, re-pending, setting a past `expires_at`, or
deleting the only one, including an administrator doing it to themselves. The
config-driven built-in admin does **not** count: it is a break-glass credential, so at
least one real administrator row must remain (promote another account first).
`409 users.self_delete` when an administrator deletes their own row (the config admin
has none). `detail {user_id, username}`.

**Password policy** (registration and the admin `password` patch; stored hashes are
never re-checked, so existing accounts are unaffected until they change their
password). `LAUNCHPAD_AUTH_PASSWORD_POLICY` selects the mode: `strict` (default) = at
least `LAUNCHPAD_AUTH_PASSWORD_MIN_LENGTH` characters (default 12, floor 6), not equal to
the username, the email or its local part (case-insensitive), and not on the embedded
top-common list; `relaxed` = the minimum length only. `auth.weak_password` carries
`detail: {reason: too_short | matches_identity | common, min_length}` (relaxed mode only
ever reports `too_short`); a `null` `password` patch generates one that satisfies the
policy. `GET /api/auth/status` reports the effective policy as `password_policy`.

Sign-in error codes: `auth.invalid_credentials` (401 — one code and one message for
an unknown account and a wrong password, with the same hashing work spent on both),
plus `auth.account_pending` / `auth.account_disabled` / `auth.account_expired` (401)
once the submitted credentials themselves are correct.

**Login protection** (`POST /api/auth/login`; in-process state, cleared by a backend
restart): `auth.locked` (429) — the submitted account name, whether or not it exists,
has had `LAUNCHPAD_AUTH_LOGIN_LOCKOUT_THRESHOLD` (default 5) consecutive wrong
passwords and is locked for `LAUNCHPAD_AUTH_LOGIN_LOCKOUT_BASE_SECONDS` (default 60),
doubling with every further failure up to `LAUNCHPAD_AUTH_LOGIN_LOCKOUT_MAX_SECONDS`
(default 1800); attempts during the lock are refused without counting; a successful
sign-in or an admin `PATCH /api/users/{id} {"unlock": true}` clears it. This is the
primary control. The built-in config admin (`LAUNCHPAD_AUTH_USERNAME`) is **exempt** from
it — the break-glass credential with the strong configured password must never be
lockable from the internet; it is covered by the peer window only. `auth.rate_limited`
(429) — the caller's source IP has made `LAUNCHPAD_AUTH_LOGIN_IP_LIMIT` (default 20)
failed sign-ins within `LAUNCHPAD_AUTH_LOGIN_IP_WINDOW_SECONDS` (default 300); the
window is consulted only after a *failed* check, so it turns further wrong passwords
from that peer into 429 but never refuses a correct one — a coarse secondary control:
the peer is the real client on the EIP path (nginx `X-Forwarded-For`, honoured by
uvicorn's `--proxy-headers --forwarded-allow-ips=127.0.0.1`) but the NLB's private IP
for everyone behind the NLB. Both 429s carry a `Retry-After` header and
`detail: {retry_after}` (seconds). Each lock and each window trip is a `WARNING` line
on the `launchpad.auth` logger (journald) naming the account key, counters and peer —
never the password.

**Session secret.** The cookie is signed with a random 32-byte secret generated on first
start and persisted at `data/session_secret` (0600; `LAUNCHPAD_SESSION_SECRET_FILE`), or
with `LAUNCHPAD_SESSION_SECRET` when set — no longer derived from the admin password.
Rotating the secret (new env value, or delete the file and restart) signs every session
out at once, which also happens once at the deploy that introduces it. **A password
change still ends that account's other sessions:** the cookie carries a session epoch —
`users.session_epoch`, bumped by every `password` patch, or a digest of the configured
password for the built-in admin — and a stale epoch is `401 auth.required`.

Session and role errors: `auth.required` (401 — missing, tampered, or expired
cookie, and also an account that has since been disabled, expired, or deleted),
`auth.forbidden` (403 — member session on `/api/users*`), `users.not_found`
(404).
