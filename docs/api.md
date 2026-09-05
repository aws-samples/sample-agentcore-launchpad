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

## Sync invoke / 同步调用

```bash
curl -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke \
  -H "X-Api-Key: $LP_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "What is 2+2?", "session_id": null}'
# → {"agent":"…","text":"4","session_id":"…","latency_ms":1234}
```

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
`auth.missing_api_key` (401), `agent.not_active` (409), `agent.not_found` (404).

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

## Console Agents API — versions and endpoints

`GET /api/agents/{agent_id}/versions` is the read-only AWS view behind the agent
detail's VERSIONS & ENDPOINTS panel. It follows every `nextToken` page of the two
list operations for the agent's resource family and returns an allow-listed
projection — no environment values, artifact locations, execution roles or
authorizer configuration.

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/agents/{agent_id}/versions` | `{kind: runtime\|harness, resource_id, versions[{version, status, description, last_updated_at}], endpoints[{name, live_version, target_version, status, description, created_at, last_updated_at, failure_reason}], latest_version, ledger_version, canary_endpoints[]}` — `versions` newest first; `endpoints` with `DEFAULT` first then by name; `latest_version` is the highest version AWS reports and `ledger_version` the one the last Launchpad deploy recorded (`Agent.version`) — they may differ after an out-of-band update or a canary candidate mint; `canary_endpoints` lists the `stable`/`treatment` names still present. Resource family: `zip_runtime`/`studio`/`container` and imported rows whose `spec.discovery.resource_type` is absent or `runtime` → `ListAgentRuntimeVersions` + `ListAgentRuntimeEndpoints`; `harness` and imported rows with `resource_type == "harness"` → `ListHarnessVersions` + `ListHarnessEndpoints` (harness versions carry no description). Never mutates anything |

Error codes: `agent.not_found` (404, unknown id or another workspace's agent),
`agent.no_resource` (409, the row has no AWS resource to ask about — deploy still
running, failed first deploy, deleted, or a shape that is neither Runtime nor
Harness; `message` is the human reason the panel shows). AWS `ClientError`s map to
the standard 4xx envelope.

## Console Governance API

These `/api` routes back the authenticated console. They are not part of the
public `/v1` agent invocation contract.

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/governance/gateways` | Live MCP Gateway inventory |
| `GET` | `/api/governance/gateways/{id}` | Targets, actions, Registry, Engine, IAM, and attachability detail |
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
| `GET` | `/api/governance/gateways/{id}/audit` | Immutable local change journal |
| `GET` | `/api/governance/operations/{operation_id}` | Async operation status |

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
| `GET` | `/api/memory/actors` | Actors with the compound `<agent_id>__<human>` id decoded and the agent name resolved |
| `GET` | `/api/memory/sessions?actor_id=` | Sessions for one actor, joined to the ChatSession ledger when the console wrote them |
| `GET` | `/api/memory/events?actor_id=&session_id=` | Short-term events; conversational payloads carry role + full text, blobs only a byte count |
| `GET` | `/api/memory/namespaces?actor_id=` | Strategy namespace templates with `{actorId}` substituted, plus a `resolvable` flag |
| `GET` | `/api/memory/records?actor_id=&strategy_id=` or `?namespace=` | Long-term records for the resolved namespace |
| `POST` | `/api/memory/records/search` | Semantic retrieval (`{query, actor_id, strategy_id?, namespace?, top_k}`) with relevance scores |
| `GET` | `/api/memory/extraction-jobs` | Failed (retry-eligible) extraction jobs, filterable by `actor_id`/`session_id`/`strategy_id`/`status` — **not surfaced in the console**; AWS's `status` enum is `FAILED` only, so a healthy resource returns an empty list |

Memory resource management (`?view=resources`):

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/memory/resources` | Every memory in the workspace's account/region, default first, each with the live agents whose spec pins it |
| `POST` | `/api/memory/resources` | `CreateMemory` (`{name, description?, event_expiry_days?, strategies?, namespace_keys?}`) → `201` with the detail projection in `CREATING` state |
| `GET` | `/api/memory/resources/{memory_id}` | Detail projection: description, status, event expiry, execution role, strategies, namespace keys |
| `PUT` | `/api/memory/resources/{memory_id}` | `UpdateMemory` limited to `{description?, event_expiry_days?}` — at least one required (422 otherwise), `description` 1–4096 chars (it can be replaced, not cleared), `event_expiry_days` 7–365 (422 outside). Sends exactly `memoryId` + the given fields and never `namespaceKeys` (the API replaces that set wholesale); the reply is the detail projection read back with `GetMemory`. Not blocked by referencing agents or the platform default; unknown id → `404 aws.not_found` |
| `DELETE` | `/api/memory/resources/{memory_id}` | `DeleteMemory`; `409 memory.platform_protected` for the workspace default, `409 memory.in_use` (with the agents) while a live agent's spec pins it |

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
| `POST` | `/api/chat/{agent_id}` | One turn as SSE (`meta` → `delta`/`tool`/`error` → `done`); `{prompt, session_id?}`, a missing id starts a new session |
| `GET` | `/api/chat/{agent_id}/sessions` | Replayable sessions for the agent: `{session_id, actor_id, turns, last_at, ended_at, preview}` — `ended_at` is set once the console explicitly ended the runtime session, `null` while it is live or merely idle |
| `GET` | `/api/chat/{agent_id}/history?session_id=` | The rendered thread items of one session, in replay order |
| `POST` | `/api/chat/{agent_id}/sessions/{session_id}/stop` | **END SESSION** — data-plane `StopRuntimeSession(agentRuntimeArn, runtimeSessionId)` → `{session_id, ended: true, already_ended, ended_at}`. `already_ended: true` when AWS answered `ResourceNotFoundException` (the session had already ended or idle-expired) — a success, not an error. The ledger row is kept (history stays replayable) and stamped `ended_at`; a later turn posted under the same id starts a fresh runtime session and clears it. Only runtime-backed agents qualify (`zip_runtime`, `studio`, `container`, discovered runtimes); a managed Harness — deployed or imported — has no session-stop operation and answers 409 `chat.session_stop_unsupported` with `detail.reason_code` (`harness`). A session of another agent or workspace is 404 `chat.session_not_found`. A `RetryableConflictException` that outlives botocore's retries is 409 `aws.conflict` |

Ending is explicit: NEW SESSION in the console only forgets the id locally, so the
runtime session it leaves behind idles out on its own. END SESSION is what to press
after a re-publish — AgentCore pins a live session to the version that first
served it, so validation of the new version needs a fresh session.

## Console Evaluation Datasets API

`/api/eval/datasets` holds the local scenario datasets (SQLite, the editable source
of truth) and their one AWS Dataset each. AWS datasets have a **DRAFT** plus
immutable numbered **versions**: SYNC TO AWS creates the dataset once and afterwards
replaces the draft's examples in place; PUBLISH VERSION snapshots the draft.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/eval/datasets` | `{datasets[]}` — local rows with `items`, `kind`, `has_ground_truth` and the `cloud` blob |
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

## Console Evaluation Runs API

`/api/eval/runs` drives batch evaluations / insights analyses through the bounded
run queue (`eval_max_concurrent_runs`, capped at the 5 active-batch-evaluations
account quota). Run status: `queued → invoking → waiting → evaluating → completed |
failed | stopped`. Every row carries `stop_requested` (an operator stop is pending
on a run whose batch is still STOPPING).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/eval/runs?limit&offset&mode&agent_id` | Newest-first page `{runs, total, limit, offset}` |
| `GET` | `/api/eval/runs/{run_id}` | One run (scores / insight trees / `batch_eval_id` / `error` / `stop_requested`) |
| `POST` | `/api/eval/runs` | Start a run (exactly one scope: `dataset_id` \| `cloud_dataset_id` \| `session_ids` \| `lookback_hours`) → 201. A `cloud_dataset_id` scope may add `dataset_version` (a published version number such as `"2"`, never `DRAFT`; omitted = the draft): the version must exist in `ListDatasetVersions` (else 422 `run.dataset_version_unknown`, no run row) and `GetDataset` / `ListDatasetExamples` read that snapshot. `dataset_version` with any other scope → 422 `run.dataset_version_scope`. Every run row echoes `dataset_version` (`null` for draft, local, session and window runs) |
| `POST` | `/api/eval/runs/{run_id}/stop` | **Stop an active run** → 202 with the run. A run whose batch exists on AWS (`batch_eval_id` set) is stopped with `StopBatchEvaluation`: the batch goes `STOPPING → STOPPED`, the sessions already judged keep their results, and the poller records the run as `stopped` with those partial scores / insight trees and `error = "stopped by operator"`. A run still `queued` is cancelled locally (the worker skips it, AWS is never called) and returns `stopped` at once. A run replaying its dataset or waiting for telemetry (no batch yet) stops between prompts and never calls `StartBatchEvaluation`. Terminal runs (`completed` / `failed` / `stopped`) → 409 `run.not_active`; unknown → 404 `run.not_found`. `DeleteBatchEvaluation` is deliberately not exposed — the ledger keeps the partial results AWS would drop |
| `GET` | `/api/eval/queue` | `{running, queued, locked, max_concurrency}` — cancelled runs leave the queue immediately, so the count covers active runs only |

## Console Online Evaluation API

`/api/eval/online/*` manages AgentCore **online evaluation configs** — continuous,
sampled scoring of live sessions. AWS is the source of truth; the ledger keeps
identifiers only. Every config in the workspace account is listed and classified
by `owner`: `agent` (created here for an agent), `experiment` (`exp_*`/`can_*`
arms owned by experiments — read-only), `external` (anything else).

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/eval/online` | `{configs, total}` — all configs, newest first, with `owner`, both statuses, `failure_reason`, evaluators, sampling, timeout, `matched_agent` (external rows whose log group matches a workspace agent), `duplicate_enabled` (two ENABLED agent configs on one agent), `results_log_group` |
| `POST` | `/api/eval/online` | Create for an active agent: `{agent_id, mode: scores\|insights (scores), evaluators[1..10] (scores mode), insights[1..3] ⊆ Builtin.Insight.FailureAnalysis\|UserIntent\|ExecutionSummary + clustering_frequencies[0..3] ⊆ DAILY\|WEEKLY\|MONTHLY (insights mode), sampling_percentage 0.01–100 (omit → 10 scores / 100 insights), session_timeout_minutes 1–1440 (15), filters[0..5], description?, enable_on_create (true)}` → 201 row (`status` starts `CREATING`). Mixing kinds → 422 `online_eval.mode_conflict`; rows carry `mode` (derived: `insights` non-empty) |
| `GET` | `/api/eval/online/{config_id}` | Full detail incl. `filters`, `data_source`, `execution_role_arn` |
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
| `GET` | `/api/overview/online-quality` | ONLINE QUALITY · 24h tile: `{range: "24h", mean, scores, sessions, agents, configs, evaluators[{evaluator_id, mean, count, polarity}], cached}` — count-weighted mean over every (evaluator, agent-owned config) pair with lower-is-better evaluators inverted (`1 − mean`), so the tile always reads higher-is-better; `evaluators[].mean` stays raw; `configs` counts the workspace's agent-owned configs (ledger) and `agents` the agents that scored, so "configured, nothing judged yet" is distinguishable from "no config". 120 s per-workspace cache with single-flight, `force=true` bypasses; a workspace without agent-owned configs answers the empty payload without any AWS call |

## Console Accounts API

`/api/auth/*` gates the console and `/api/users/*` manages the accounts behind
it. Neither surface touches AWS. See
[architecture.md](architecture.md#console-authentication-and-accounts).

| Method | Path | Auth | Result |
|---|---|---|---|
| `GET` | `/api/auth/status` | open | `{auth_required, authenticated, registration_enabled, registration_requires_approval, username, role, email, account_expires_at}` — identity fields are null until authenticated |
| `POST` | `/api/auth/login` | open | Sets the `launchpad_session` cookie (12h, clamped to the account validity) and echoes the identity |
| `POST` | `/api/auth/register` | open | `201` — creates a `member` account; by default `status=pending` with `expires_at=null` until an admin approves it, then valid for `auth_registration_valid_days` (default 7) |
| `POST` | `/api/auth/logout` | session | Clears the cookie |
| `GET` | `/api/users?q=&status=all\|pending\|active\|expired\|disabled&limit=&offset=` | admin | Paged account list with derived `state` / `days_remaining` |
| `GET` | `/api/users/stats` | admin | Totals including the `pending` approval queue, `expiring_soon` (≤3 days), 7-day registration/sign-in counts, a 14-day registration series, top email domains |
| `PATCH` | `/api/users/{id}` | admin | Any of `status` (`pending`\|`active`\|`disabled`; `active` on a pending account approves it and starts its window), `role`, `extend_days`, `expires_at` (`null` = never expires), `password` (`null` = generate and return once) |
| `DELETE` | `/api/users/{id}` | admin | Removes the account |

Registration error codes: `auth.registration_disabled` (400, gate off or
registration disabled), `auth.invalid_username` / `auth.invalid_email` /
`auth.email_domain_blocked` / `auth.weak_password` (400),
`auth.username_taken` / `auth.email_taken` (409).

Sign-in error codes: `auth.invalid_credentials` (401), plus
`auth.account_pending` / `auth.account_disabled` / `auth.account_expired` (401)
once the submitted credentials themselves are correct.

Session and role errors: `auth.required` (401 — missing, tampered, or expired
cookie, and also an account that has since been disabled, expired, or deleted),
`auth.forbidden` (403 — member session on `/api/users*`), `users.not_found`
(404).
