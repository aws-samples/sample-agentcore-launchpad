# Interface: policy engines — the Global AgentCore Policy model (2026-10-28)

Contract of the policy module after the user's ruling of 2026-10-28 (`designs/policy-global-model-20261028/REQUIREMENTS.zh.md`): **the model, the naming, the console entry points and the decision semantics are those of Global AgentCore Policy.** A policy engine is an independent resource, policies belong to an engine, an engine is attached to one or more gateways, and the interceptor on a gateway decides the tool calls that pass through it. The China partition has no managed Policy service, so the engine stays self-hosted (Dogwood, `docs/cn/interfaces/policy-engine.md`) — but it is presented exactly the Global way. The lead's draft (`designs/policy-global-model-20261028/API-CONTRACT-DRAFT.md`) is the origin of this file; the deviations the backend owner made are listed at the end.

Owners: backend `routers/policy_engines.py`, `services/policy_engines.py`, `policy_store.py`, `policy_engine_sync.py`, `policy_gateway_publish.py`, `policy_bundle.py`, the interceptor `app/policy/interceptor/`; console `frontend/src/v2/pages/policy/*` (engines, policies, attachments, decisions, audit, simulation; the gateway page under `pages/governance/*` shows the attachment card) (the console agent codes against THIS file); operator kit `designs/policy-default-20261027` + `designs/policy-m2-20261027` (updated for engines by r17-bk).

## Model (Global parity)

| Global | Launchpad CN (this contract) |
|---|---|
| `PolicyEngine` (name, description), many per account | `policy_engines` row, many per workspace; the Dogwood set key IS the engine id |
| `Policy` belongs to one engine (`policyEngineId`), `enforcementMode: ACTIVE \| LOG_ONLY` | `policies.engine_id`; `enforcement_mode: active \| log_only` (lowercase stored states, as every other state in the API) |
| `Gateway.policyEngineConfiguration = {engine arn, mode LOG_ONLY \| ENFORCE}` — one engine per gateway, one engine on many gateways | `policy_engine_attachments(gateway_id PK, engine_id, mode LOG_ONLY \| ENFORCE)` — Global's spelling kept for the attachment mode |
| Decisions on `tools/call` (+ `tools/list`, `initialize`) through the gateway | unchanged (contract amendment 9) |
| Principals `AgentCore::OAuthUser` / `AgentCore::IamEntity`, resource `AgentCore::Gateway::"arn"`, action `AgentCore::Action::"<target>___<tool>"` | unchanged on the engine (the hub's user token maps to the OAuthUser-style principal; per-user decisions kept) |
| No workspace policy / posture / switches / confirmation name | all REMOVED (see §Removed) |
| No agent scope / D7 closure / hub extensions | agent scope removed on the hub (statements compile as written); the D7 synthetic permit is removed from the Rust compiler |

Decision semantics, unchanged: only gateway tool calls are decided; system agents (architect, evaluation, presets, canaries) are exempt (amendment 10); **an attached engine with no matching permit denies** (`empty_set_decision: deny`); an unreachable engine fails closed in `ENFORCE` (`error` row + pass in `LOG_ONLY`); **a gateway without an attachment is unguarded** — its interceptor is not attached, nothing is decided, nothing is recorded.

## Ledger (`app/models/policy.py`)

- `policy_engines(id "pe_<12hex>", workspace_id, name ^[A-Za-z][A-Za-z0-9_]{0,47}$ unique per workspace, description, status active|deleting|deleted, created_by, created_by_name, created_at, updated_at, policy_set_seq, temporal_set_version, temporal_digest, engine_state)` — the last four are the Dogwood install state that used to live on the per-workspace settings row: `ps-<policy_set_seq>` is the installed set version, `engine_state` the last successful install's facts (`policy_set_version`, counts, `governed_actions` / `temporal_referenced_actions` in entity form, `set_digest`, `bundle_pending`, `bundle_version`, and `migrated_from_workspace` on a state the migration carried over).
- `policies.engine_id` (indexed; NULL only on a row the startup migration has not visited). Policy names are unique per engine. `scope` / `agent_ids` are **kept but inert after migration** (never read or written by the API, omitted from every view) — dropping them would mean rebuilding the table on SQLite; `enforcement_mode` and `status draft|active|retired` unchanged.
- `policy_engine_attachments(gateway_id PK, engine_id, mode LOG_ONLY|ENFORCE, attached_by, attached_at, updated_at)` — hub-global, keyed by gateway (the engine carries the workspace). Distinct from `policy_gateway_attachments` (whether the interceptor Lambda is verified attached on AWS — written only by the attach / detach read-back).
- `policy_decisions.gateway_id` + `engine_id` (nullable; set on every gateway row the ingest writes since bundle /3 and on every simulator row; the migration back-fills older gateway rows from the workspace gateway). `policy_changes.engine_id` likewise.
- `policy_settings` is **inert**: read ONCE by the migration, never written again; a fresh ledger gets no row. The former `mode`, `gateway_interceptor`, `engine_publish`, `evidence_window`, confirmation and `stale_agents` have no successor.

## Routes (console `/api`; `ROUTE_POLICY`: reads MEMBER, engine / policy writes `perm:policy.manage`, gateway attachments ADMIN; all 503 `feature.unavailable` until the Cedar engine is registered, 404 without the `policy` feature)

| method path | body → response |
|---|---|
| GET `/api/policy-engines` | `{engines: [engine]}` |
| POST `/api/policy-engines` 201 | `{name, description?}` → `engine`; 409 `policy_engine.name_taken`; 422 (pydantic pattern) / `policy_engine.invalid_name` |
| GET `/api/policy-engines/{id}/entities` | pickers + templates: `{users, api_keys, agents, tools, roles, system, gateways[{gateway_id, gateway_arn, attachment}], templates}` (the workspace gateway and its attachment view) |
| GET / PUT / DELETE `/api/policy-engines/{id}` | PUT `{description}`; DELETE → `{deleted, id}`; 409 `policy_engine.attached {gateway_ids}` while any gateway is attached; 409 `policy_engine.has_policies {policy_ids}` unless `?force=true` (retires its policies and archives the engine; policy/version, decision and journal reads remain available by ID, live names are released, mutations return 404) |
| GET `/api/policy-engines/{id}/status` | `{available, healthy, shards[{shard, ready, store_epoch, policy_set_version, in_sync, error?}], engine}` — this engine's set on each Dogwood shard; `available: false` while the platform switch is off |
| GET `/api/policy-engines/{id}/changes?limit` | `{changes: [change]}` (journal rows of the engine) |
| GET `/api/policy-engines/{id}/decisions?gateway_id&source&decision&agent_id&principal&since&limit` | `{decisions: [decision], runtime_channel}` — rows whose `engine_id` is this engine (its gateways' decisions + its simulations); same row shape as before plus `gateway_id`, `engine_id`, `log_only_would` |
| POST `/api/policy-engines/{id}/validate` | `{statement, policy_id?}` → validation report. With engine routing enabled, both dialects use the install rewrite/compiler against Gateway `tools/list` and the other ACTIVE policies; `policy_id` replaces that policy in preflight. Without routing, D20 checks only the Launchpad schema; Dogwood returns 409 `policy.engine_not_routed`. |
| POST `/api/policy-engines/{id}/format` | `{statement}` → `{statement}` |
| POST `/api/policy-engines/{id}/simulate` | `{gateway_id, principal{kind user\|api_key\|agent\|system, id, role?}, tool, agent_id?, input?, at?, history[]?, record?}` → the real engine simulator answer (both stages, replay trace and `gateway_id`). The selected gateway supplies the resource ARN and reported attachment mode (`log_only` when unattached); `record: true` needs `perm:policy.manage`. |
| GET `/api/policy-engines/{id}/policies?status` | `{policies: [policy], engine}` |
| POST `/api/policy-engines/{id}/policies` 201 | `{name, statement, description?, enforcement_mode?, prefill?}` → `policy` (draft v1; `enforcement_mode` defaults to `log_only` — Global creates every policy LOG_ONLY) |
| GET `…/policies/{pid}` / GET `…/policies/{pid}/versions` | `policy` + `versions[]` / `{versions[]}` — a policy is addressed under ITS engine (404 `policy.not_found` elsewhere) |
| PUT `…/policies/{pid}` | `{statement?, description?}` → `policy` (a new statement = a new draft version) |
| POST `…/policies/{pid}/publish` | `{version?, reason?}` → `policy` — NO confirmation / apply fields; the engine's set is installed before the commit (409 `policy.engine_install_failed` rolls back, 422 `policy.invalid_statement` when the set does not compile) and every attached gateway's bundle is written after it |
| PUT `…/policies/{pid}/enforcement-mode` | `{enforcement_mode: active \| log_only}` → `policy` (an ACTIVE policy is re-installed at once) |
| POST `…/policies/{pid}/retire` | `{reason?}` → `policy` (Global: no guard on the last ACTIVE policy — an ENFORCE gateway whose engine holds none then denies every call) |
| DELETE `…/policies/{pid}` | → `{deleted, id}` (drafts / retired only, 409 `policy.active`) |
| GET `/api/governance/gateways/{gid}/policy-engine` | `{gateway_id, attached, engine_id, engine_name, mode, attached_by, attached_at, interceptor{attached, interceptor_arn, attached_at, bundle_version, bundle_pending}}` |
| PUT `/api/governance/gateways/{gid}/policy-engine` (ADMIN) | `{engine_id, mode LOG_ONLY \| ENFORCE}` → the GET view. Idempotent. A change of engine or mode rewrites that gateway's bundle; the FIRST attach also attaches the interceptor Lambda to that gateway (`interceptor_hook` → `gateway_bootstrap.set_interceptor`, the verified read-back lands in `policy_gateway_attachments`). 404 `policy_engine.not_found` (another workspace's engine included), 409 `gateway.policy_engine_foreign` — TWO meanings, both rendered 「由其他环境管理」 by the console: (a) the gateway's interceptor names another host's Lambda (ruling 24; `GET` shows `foreign: true` + `foreign_lambda`), (b) the gateway is already held by another workspace's engine on this host (N17-obs518 asks for a distinct code), 409 `gateway.policy_engine_busy` while an attach / detach of that gateway is in flight, 422 `gateway.policy_engine_mode_invalid` |
| DELETE `/api/governance/gateways/{gid}/policy-engine` (ADMIN) | → `{gateway_id, detached, attached: false, …}` — the bundle is rewritten with `attachment: null`, the interceptor detached from that gateway; decisions history kept; idempotent (`detached: false` when nothing was attached); 409 `gateway.policy_engine_foreign` with the same two meanings as PUT (foreign Lambda; held by another workspace's engine) |
| `/api/policies/**` | **410 `policy.moved {engines_url: "/api/policy-engines", path}`** on every method and path for one release (the console routes to the engines) |

`engine` = `{id, name, description, status, workspace_id, policy_count, active_policies, attachments[{gateway_id, gateway_name, engine_id, engine_name, mode, attached_by, attached_at, updated_at}], policy_set_version ("ps-<n>" | null), temporal_set_version, temporal_count, has_user_permit, warnings, bundle_pending, engine_decides, last_install{status, trigger, policy_set_version, error, operator, at} | null, created_by, created_by_name, created_at, updated_at}`.

Validation reports carry `scope: launchpad_schema | gateway_active_set`. The editor labels
basic validation separately from publish preflight. Preflight is a read-only snapshot:
publishing recompiles against the then-current tools and ACTIVE set before any shard install.
Compiler diagnostics are returned in `errors`, rendered in the editor, and retained in a
failed install's audit message. A compile failure leaves the active version, set counters and
all shard sets unchanged.

D20 rewriting uses Cedar's JSON EST through `CedarEngine`, not source-text substitution.
Tool equality, exact-name and target-prefix tests become action tests **at the same expression
position**; `&&`, `||`, nesting, `when` and `unless` retain their structure and polarity.
A Tool constraint in the resource scope becomes an additional `when` condition (implicit AND).
Unsupported patterns fail schema compilation rather than widening authorization.
`policy` = `{id, engine_id, name, description, status, enforcement_mode, kind, active_version, draft_version, statement, validation, policy_ids, published_at, s3_key, created_by, created_at, updated_at}` (+ `versions[]` on GET by id; `kind` / `enforcement_mode` are always present now).
`decision` = the previous row shape + `gateway_id` + `engine_id` (+ the gateway-row extras `stage`, `session_psid`, `event_seq`, `store_epoch`, `input_keys`, `ingested_at`, `ingest_lag_ms`, `log_only_would`). `change` = the previous shape + `engine_id`. Exact key sets are pinned by `backend/tests/test_policies_contract.py`.

The flat simulation body is the console contract. `input` defaults to `{}` and is
limited to 16 KiB of JSON; `history` defaults to `[]` and allows at most 50 events
(`{tool, kind?: request|response|error, input?, output?, at}`), each with bounded JSON.
It always uses the engine replay path and its per-user rate limit; a disabled engine
returns 409 `policy.engine_not_routed`. Existing operator clients may still send
`{principal, action, resource, gateway_id?, context?, include_drafts?, policy_version?,
record?, engine?{tool?, agent_id?, input?, at?, history[]?}}`. That legacy shape keeps
its prior behavior: an omitted gateway defaults to the workspace gateway and an
omitted `engine` selects the Cedar-only simulator.
Mixing flat fields with legacy fields returns 422 before either format is selected;
invalid replay input cannot silently fall back to the Cedar-only path.

Capabilities: `GET /api/capabilities.policy = {engines: <n>, attached_gateways: <n>}` (replaces `{mode, active_policies}`); `GET /api/overview.services.policy` is true iff the Cedar engine is registered AND one of the workspace's engines is attached to a gateway with ≥ 1 ACTIVE policy; `service_detail.policy = "<n> engine(s) · <m> gateway(s)"`.

The attachment table includes live gateways from the engine's region and from other ready
workspaces visible to the caller in the same account. Each read pins its workspace header;
the console selection and the regional governance API remain unchanged. Cross-region rows
show the disabled attachment indicator and do not issue attachment/detail requests in the
engine's region. A failed regional read is surfaced instead of silently omitting that region.

## Engine install (`services/policy_engine_sync.py`)

One Dogwood set per ENGINE: the install / decide / respond / list-filter messages' `ws` field carries the **engine id** (`pe_<hex>`); set versions stay `ps-<n>` per engine (the Rust CAS parses `<prefix>-<n>`). Every publish / retire / enforcement-mode change of an engine compiles its ACTIVE set — statements as written, no agent-scope clause — against the union of `tools/list` of the gateways the engine is attached to (the engine's workspace gateway when none is attached: an engine installs and simulates before it is attached) and installs it on every shard BEFORE the ledger commits. The temporal-set version advances only when the temporal items change; new principal tokens carry compact `engine_versions` for declared gateway targets (including the A2A audience) and a session `generation`; the interceptor selects its trusted attachment's engine ID and derives a workspace-bound psid with that engine's tsv. Scalar tsv/psid remain for legacy /2 rollout. `engine_decides(db, engine)` = the platform switch is on, a set was accepted, its digest is the current set's and the state is not legacy / migrated. Inert while `Settings.policy_gateway_interceptor` (the PLATFORM kill switch, kept) is off.

Schema reads for validation and installation use the live Gateway `tools/list`, without a
cached-schema fallback. Each Gateway read has at most three attempts (20-second HTTP inactivity
timeout per attempt, 0.25/0.5-second backoff); only HTTP 429/5xx and timeout/network failures
are retried. Authorization, RPC and malformed-schema failures are not retried. This is an
inactivity timeout, not a total publish deadline. A failed read returns JSON 409
`policy.compiler_unavailable` (validation) or `policy.engine_install_failed` (installation),
with `source: tools_list`, an error category, attempt count and upstream HTTP status when
available. Response bodies, URLs and credentials are excluded. Installation fails before
changing any shard; the prior active versions and set versions remain intact and a failed
publish is journalled. Publishing, shard installation and `tools/call` are never retried
by this read policy. The console displays the safe HTTP status and attempt count in either locale.

## Bundle (per gateway, schema `launchpad.policy.gateway/3`, `app/policy/contracts/gateway_bundle.schema.json`)

```
{schema, gateway_id, gateway_arn, version, issued_at, env, hub_role, pubkeys, role_map[*].exempt,
 empty_set_decision: "deny",
 attachment: {engine_id, mode LOG_ONLY|ENFORCE, active_count, temporal_count,
              policy_set_version "ps-<n>"|"unpublished", temporal_set_version,
              governed_actions[entity form], temporal_referenced_actions[entity form]} | null,
 engine: {endpoints, shards, key_s3_key}}
```

The `workspaces` block of /1 and /2 is gone; the Lambda reads `attachment.mode` (`LOG_ONLY` → records, `ENFORCE` → denies) and the installed set version (a missing engine set fails closed). `attachment: null` ⇒ pass everything (the gateway is unguarded; its interceptor is detached anyway). Written after every successful install (every gateway attached to the engine), after an attach / mode change / detach (that gateway), and by the reconciler (startup + every 300 s) when a live bundle no longer says what the hub would compose — the candidates are the attached gateways, every gateway whose interceptor was ever verified attached and every workspace gateway. A write failure keeps the install and the engine's `bundle_pending` until the reconciler succeeds. The interceptor Lambda serves several gateways: it picks its bundle by the invocation's gateway (`GATEWAY_ARN` in the Lambda client context; `LAUNCHPAD_POLICY_GATEWAY_ID` stays the default), its role reads `policy/gateway/*` of the bucket, and the attach of a second gateway copies the engine HMAC key under that gateway's prefix. Every I7 decision line carries `gateway_id` and `engine_id`.

## Migration (startup, idempotent, per workspace — `policy_engines.migrate`, journal `policy_engine.migrated`)

1. A workspace whose policies name no engine gets `default_policy_engine` (「默认策略引擎」) and every such policy joins it (a workspace with no policies gets neither engine nor migration journal).
2. The old settings row's engine-set state (`policy_set_seq`, `temporal_set_version`, `temporal_digest`, `engine_state`) moves onto the engine as a **migrated** state (`engine_state.migrated_from_workspace`): the Dogwood store still holds the set under the WORKSPACE key, so the reconciler re-installs it under the engine key (`force`) and rewrites the bundle before anything trusts it.
3. If the old posture was live — `gateway_interceptor && engine_publish`, the interceptor verified attached, `mode != off` — the workspace gateway is attached to the engine with `mode = ENFORCE` (old `enforce`) or `LOG_ONLY` (old `log_only`); otherwise no attachment; a formerly off workspace has its interceptor detached.
4. Every `policy_decisions` gateway row of the workspace gets its `gateway_id` (the workspace gateway) and `engine_id`; `policy_changes` rows get the engine id.
5. Legacy agent-scoped active/draft statements gain explicit agent conditions in new immutable versions. Validate the transformed text, copy active statements to S3, retain original versions, clear scope metadata, and journal `policy.scope_baked`. A failed copy/validation leaves the original version selected.
6. The old settings keys are left in place, inert (SQLite).

## Removed vocabulary / behaviour

Agent scope (`scope`, `agent_ids`, `AgentSpec.policy_ids` selects nothing — kept only so stored specs validate), the hub's scope-clause injection and the pins reinstall; workspace mode / `gateway_interceptor` / `engine_publish` / confirmation name / override reason / evidence window / `stale_agents` + `POST /api/policies/apply` (the principal carrier is part of every deploy while the platform switch is on — `policy_stale` flips only with that switch and the agent page redeploys); the "retire the last policy under enforce" guard; `policy_default_mode`. Kept: system-principal exemption (`role_map[*].exempt` + System token pass), fail-closed, SWR settle-on-RESPONSE, replay window, the KMS circuit, `empty_set_decision`.

## Deviations from the lead's draft (backend owner, 2026-10-28)

1. **Set version** stays `ps-<n>` per engine (the draft said `pe_<id>:v<n>`): the Rust install CAS parses `<prefix>-<n>` and would treat the draft's form as always-newer. The set KEY is the engine id, which is what distinguishes engines.
2. **D7 closure** and the retired invoke action are removed from both hub and Rust compiler (R1 rebuild).
3. **Multi-gateway Lambda**: the Lambda picks the bundle per invocation gateway, the role reads `policy/gateway/*`, the engine key is copied per gateway prefix — R2 (Lambda code) + the `policy-interceptor` bootstrap stage re-run (IAM) on both hosts.
4. **Compile schema source** = the union of the attached gateways' `tools/list`, falling back to the engine's workspace gateway.
5. **Every** `/api/policies/*` path is 410 (not only the collection and `/apply`).
6. Additive routes kept from the old surface: `GET /api/policy-engines/{id}/entities`, `GET …/{id}/changes`, `GET …/{id}/status`; `GET …/{id}/policies/{pid}/versions` as the draft asked.
7. `scope` / `agent_ids` columns and the `policy_settings` table stay physically (inert, documented above); `policy_decisions.gateway_id` is nullable at the DDL (NOT NULL is enforced by the writers).
8. `Settings.policy_gateway_interceptor` stays as the platform kill switch; only the workspace-level switches are gone.
9. Attachment writes are ADMIN; engine / policy writes `perm:policy.manage`.
10. Pre-engine call forms stay available to operator scripts and the older tests: `policy_store.settings_row(db, ws)` (= the workspace's default engine row), `policy_engine_sync.install_workspace / reconcile_workspace / publish_enabled`, and a workspace id anywhere a `PolicyEngine` is expected (→ the default engine, created on first use; orphan policy rows of the workspace join it).

## Attachment recovery and multi-gateway sessions

Every mutation reads the real Gateway first. Cross-region targets return 422; a foreign Lambda returns 409 before writes. GET exposes `foreign`, `foreign_lambda`, `bundle_bucket` and `bundle_prefix`. Attach grants the target IAM role invocation of this host’s Lambda, verifies/copies this host’s engine key, publishes the per-gateway bundle and attaches the interceptor. Failures are journalled and returned as errors; the same PUT/DELETE retries incomplete work. A fresh AWS observation repairs stale verified ledger state. Bundle versions and pending flags are tracked per gateway. Membership changes recompile all attached gateway schemas and publish the resulting set to all attachments.

Generated and Harness runtimes group declared tools by their validated gateway. Caller-supplied URLs are overwritten by authoritative gateway facts. New tokens include only referenced engine epochs; changing an unrelated engine leaves the current engine’s session unchanged. The claims schema is v2, while the compatible token wire version remains 1. Both codec copies are identical.

Engine detail/status expose `has_user_permit` and `warnings: ["no_user_permit"]` when appropriate. The console displays this diagnostic before operators enable enforcement. It does not synthesize a permit.

Migration failures retain the legacy gateway bundle. Every publish path refuses to overwrite
it while that workspace has policies without an engine; periodic reconciliation retries
workspaces independently. Attachment operation history retains the workspace/bucket owner
after a failed detach removes an additional gateway's relationship.

The engine set key (`ws = pe_<id>`) is separate from the calling agent's workspace.
`agent.workspace_id` comes from the verified bundle caller or the hub's Agent row and is
shared by REQUEST, RESPONSE, list-filter and simulation. The engine uses it for the
Agent → Workspace entity parent; legacy requests omit it and fall back to `ws`.
The simulator uses the selected gateway resource for both current and historical events.

Release order preserves compatibility with the old engine's strict AgentRef parser:
R2 dual-schema Lambda first (it omits `agent.workspace_id` on legacy /2 bundles), then the
t1 user baseline through the OLD hub API, then R1's rolling binary upgrade, and only then
the hub migration and /3 publication. Both R1 shards must retain the existing workspace set
before migration. Restarting preserves its compiled policy text, including old D7 closure;
after migration explicitly force-install every live engine and publish all of its bundles.
Verify that these sets contain no synthetic closure. After /3 is live, rollback must restore
the old hub and /2 bundles before downgrading R1; the old binary cannot accept the new
AgentRef field. This order supersedes the earlier hub-before-R1 operator draft.
