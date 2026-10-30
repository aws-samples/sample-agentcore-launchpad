# Users and ownership — who created what (Family B step 1)

The contract between the console accounts (`users`) and the resources they create. Step 1
(this document, branch `cn/r12-c`) ports upstream #259 (API keys bound to their creator with
offboarding) and #261 (a creator on every user-managed resource) in the #263 column shape,
plus four CN extras the user decided on 2026-10-16. Step 2 (r12-d) adds workspace-level
visibility (`resource_visibility` = `shared` | `isolated`) on top of exactly these columns —
nothing here filters by creator except the two member rules listed under "CN extras".

## 1. Columns

Every stamped table carries the same two nullable columns:

| column | type | meaning |
|---|---|---|
| `created_by` | `VARCHAR(32)`, indexed, **not a FK** | the creating console principal's `users.id` — the ownership key. NULL for the config admin and the auth-disabled operator (no `users` row), for system presets, discovered runtimes, bootstrap / seeded resources, and for legacy rows whose creator could not be resolved. A deleted account leaves its id behind on purpose: the dangling id is how a key knows its creator is gone. |
| `created_by_name` | `VARCHAR(64)` | the username at creation, display only. NULL only when nothing is known (legacy rows, system paths). |

Stamped tables (`core/db._CREATOR_COLUMN_DDL` is the authoritative list):

| table | stamping point | NULL creator when |
|---|---|---|
| `agents` | `POST /api/agents` (caller), architect proposal approve (the approver) | system presets (`system_key`), discovered runtimes (`method=discovered_runtime`), unresolved legacy owners. No Harness convert route exists in the CN build. |
| `chat_sessions` (CN extra) | the first turn of a session: Chat console → the signed-in account; `/v1` invoke / invoke-stream → the key's creator | rows older than the column; a `/v1` session whose key has no creator |
| `api_keys` | `POST /api/apikeys` | config admin / operator (name only), legacy keys |
| `managed_memories`, `memory_stores` | `POST /api/memory/resources` (both rows); `POST …/{id}/adopt` records the adopter's **name only** (`created_by` NULL — the memory pre-existed) | bootstrap / default store (no row), seeded |
| `knowledge_bases` | `POST /api/knowledge-bases` (`KnowledgeBackend.create_kb(..., creator=)`) | preset-installed KBs, legacy rows |
| `identity_providers` | `POST /api/identity/connections/{oauth2,api-key}` | system providers (no row), the `""` legacy default |
| `eval_datasets` | `POST /api/eval/datasets`, `…/upload`, `…/from-sessions` (sync and 202 job — the job payload carries the caller), a pipeline run's output dataset (the run starter, not the pipeline's creator), the architect materializer (the plan's approver) | appends leave the target's creator alone |
| `eval_runs` | `POST /api/eval/runs`, `POST /api/eval/online/{id}/reports/run` | scheduler-started runs |
| `eval_pipelines` | `POST /api/eval/pipelines` | — |
| `online_eval_configs` | `POST /api/eval/online` | experiment / canary-owned configs |
| `eval_evaluators` | `POST /api/eval/evaluators` (judge / derived / code), the architect materializer (approver) | builtins are not rows |
| `experiments`, `runtime_canaries` | `POST /api/experiments`, `POST /api/runtime-canaries` | — |
| `skill_lab_tasksets`, `skill_lab_jobs` | `POST /api/skill-lab/tasksets`, `POST /api/skill-lab/jobs` (eval / train / taskgen), `POST …/jobs/{id}/import-taskset` (the importer) | seeded samples |
| `resource_creators` (side table) | `creators.record / lookup / forget` for kinds without a ledger row | **no kind registered in the CN build** (KBs and evaluators are ledger-backed here); kept with upstream's API so step 2 and a future AWS-only kind plug in unchanged. Workspace-scoped; purged with the workspace. |

`services/creators.py` is the one helper: `Creator.of(identity)` / `Creator.of_request(request)`
→ `(user_id, name)`; `stamp(row, creator)`; `fields(row)` → `{created_by, created_by_name}` for
every response; `Creator.owns(row)` (a `users.id` match — a principal without a row owns nothing
by id); `reject_client_creator(payload)` → 422 `validation.creator_not_settable` (dict-body
routes only — a pydantic request model such as `AgentSpecRequest` refuses the key itself as
422 `validation.invalid_request` with the same sentence in `detail[].msg`; N17-obs116).

**Never client-settable.** `AgentSpec`, `CreateKeyRequest` and `CreateKBRequest` refuse a posted
`created_by` / `created_by_name` with 422; open request models (datasets, pipelines, …) ignore
the keys. Either way the server-derived creator wins.

## 2. Migration (`core/db._migrate_creator_columns`, additive, idempotent)

* Adds `created_by` + `created_by_name` (+ `ix_<table>_created_by`) to every table above where
  missing; `api_keys.last_used_at` and the `resource_creators` unique index come with it.
* **Atomic per table** (rev-r11 must-fix): the ADD COLUMN(s), the re-point and — for `agents` —
  the owner copy + `DROP COLUMN owner` run in one transaction, and the re-point is data-driven
  (rows still carrying a legacy value with no `created_by_name`), so a half-migrated ledger is
  finished on the next start, never skipped; the agents copy is gated on the `owner` column
  still existing, and a row already stamped by this build is never overwritten.
* **One-time re-point** of the five tables whose `created_by` held a username (api_keys,
  managed_memories, memory_stores, knowledge_bases, identity_providers), guarded on
  `created_by_name` being absent: username → `created_by_name` (`""` → NULL); `created_by` ←
  the `users.id` whose `username_key` matches case-insensitively, NULL when no account has that
  name (config admin, deleted account); adopted managed memories get `created_by = NULL`.
* **`agents.owner` retired (C5):** copied the same way — `river` (the pre-obs9 default),
  `system` and `aws-discovery` record no creator — then `ALTER TABLE agents DROP COLUMN owner`.
* A second startup, or a ledger that already has `created_by_name`, changes nothing; a ledger
  without a `users` table records names only. Rehearsed by `tests/test_creator_migration.py`
  and the generic round trip in `tests/test_ledger_migration.py`.

## 3. API keys bound to their creator (#259)

* `GET /api/apikeys` rows: `created_by`, `created_by_name`, `last_used_at` (stamped by
  `/v1` auth on success, at most once per minute), `creator_offboarded`: `deleted` |
  `disabled` | `expired` | `no_workspace_grant` | `null`.
* `services.api_keys.creator_offboarded(db, key)` is the ONE definition (deleted / non-active /
  expired account, or a member without a grant to the key's workspace; admins bypass grants).
  Used by `/v1 require_api_key` (401 `auth.invalid_api_key` + key disabled in the same request),
  `POST /api/apikeys/{id}/enable` (409 `apikey.creator_offboarded`, `detail.reason`) and the
  list. `users_service.apply_patch` / `delete_user` and the grant writers (per-user PATCH,
  bulk `PUT /api/workspaces/{id}/grants`) call `disable_offboarded_keys` eagerly; expiry has no
  write event and lands on the key's next use. Offboarding never deletes a key; `delete_user`
  disables the account's keys and keeps them listed for audit.
* NULL-creator keys (config admin, operator, legacy) are never offboarded.

## 4. CN extras (user decision 2026-10-16)

| extra | rule | code |
|---|---|---|
| API keys member rule | a member lists / toggles / deletes only the keys they minted; an administrator every key (incl. legacy NULL-creator keys) | hidden key → 404 `apikey.not_found` (never 403) |
| Chat sessions (list and every by-id route) | `GET /api/chat/{id}/sessions` shows a member their own sessions: `created_by` = their `users.id`, or — rows older than the column — `created_by IS NULL AND actor_id = username`; administrators every session. Chat sessions are personal **in both visibility modes** (N16-obs70 / N16-obs71, 2026-10-19): `…/history`, `…/memory?session_id=`, `…/sessions/{sid}/stop`, `…/sessions/{sid}/turns/{n}/stream` and a `POST /api/chat/{id}` naming an existing `session_id` all resolve the row through the one helper `routers/chat._session_for` → `session_visibility.require_own`; a session with no row yet is open | hidden session → 404 `chat.session_not_found` (the same envelope an unknown id gets; the reattach 409 `turn_not_yours` is reachable only on a readable session) |
| Memory record delete | `DELETE /api/memory/records/{id}` is admin, or the member whose account **created the agent** the actor is scoped to (replaces the retired `Agent.owner` check) | 403 `memory.record_forbidden` |
| Agent redeploy / delete | creator-or-admin (`require_agent_creator`); discovered runtimes detach for any `agents.delete` holder; presets keep `refuse_system_mutation`. **NULL creator → administrator-only** (rev-r11 ruling): an agent whose creator is unknown (config-admin-created, legacy owner that resolved to no account) can be re-published or deleted by administrators only — a **CN deviation** from upstream #263, where a NULL creator means "platform resource, open to everyone"; chosen for consistency with the API-key rule above (a NULL-creator key is an administrator's) | 403 `agent.forbidden` `{agent_id, created_by, created_by_name}` |
| Approval grants | `PATCH /api/users/{id}` accepts `workspace_ids` (the same full replacement as `workspaces`; both at once → 422) so `{status: "active", workspace_ids}` approves and grants in one call; both consoles' approve dialogs send it. The body is `extra="forbid"` (N13-obs9, cn/r13-d): an unknown / mistyped key (`expire_at`) is 422 `validation.invalid_request` for the whole patch instead of being silently dropped; the 4xx matrix (422 schema · 400 `users.invalid_status` / `invalid_role` / `invalid_permissions` / `invalid_workspaces`, `auth.weak_password` · 404 `users.not_found`) is pinned by `tests/test_users_patch_validation.py` | 400 `users.invalid_workspaces` for unknown ids |

Policy contract unchanged: the Cedar `Agent.owner` and `ApiKey.created_by` attributes
(`policy-backend.md`) carry `created_by_name` (the username), so existing policies keep matching.

## 5. UI

V2 console: a **Creator** column where a list has room (agents, KBs, memory resources,
pipelines, experiments, Skill Lab lists) or a sub-line under the created/updated cell
(datasets, canaries, online configs, tasks), plus a row in every detail summary
(`v2.common.creator`, `creatorLabel()` in `v2/format.ts` — "—" when unknown). Chat → API
panel: "Created by … · last used …", a **Creator offboarded** tag with the reason as tooltip,
and the 409 translated (`apiErrors.apikey.creator_offboarded`). The agents list's former
"Owner" column is the Creator column.

## 6. What step 2 (r12-d) consumes

* The `created_by` column + index on every table in §1 and the `resource_creators` side table.
* `Creator.of(identity)` / `Creator.owns(row)` for the viewer; `creators.fields()` on every
  response (the UI already renders it).
* The two member rules in §4 are step 1's only creator filters — step 2's `shared` default must
  keep them, and `isolated` extends the same predicate shape (`own | NULL creator | current
  admin's`) to the other kinds.
* Tests: `tests/test_resource_creators.py`, `tests/test_api_key_lifecycle.py`,
  `tests/test_apikeys_api.py`, `tests/test_chat_session_visibility.py`,
  `tests/test_creator_migration.py`, `tests/test_users_approval_grants.py`,
  `tests/test_agent_ownership.py`, `tests/test_memory_record_delete.py`.

## 7. Step 2 — workspace visibility (r12-d, branch `cn/r12-d`; upstream #262 / #263)

**The setting.** `Workspace.settings["resource_visibility"]` = `shared` (default; absent or
unknown reads as shared) | `isolated`. `GET`/`PUT /api/workspaces/{id}/settings/visibility`
(ADMIN, `WORKSPACE_EXEMPT`); `set_mode` reassigns the JSON so D20's keys in the same column
survive. Re-read with the workspace row on every request — no cache, no data migration.

**The rule (verbatim from upstream).** A member in an isolated workspace sees a row whose
`created_by` is NULL, their own `users.id`, or the id of a user whose *current* role is
`admin`. Admins (and the config admin, `user_id` None) see everything. A dangling creator id
(deleted account) matches none of the three → admin-only. A hidden id answers the kind's
existing not-found on every route; the one refusal that addresses a NAME, not an id — the
agent create's duplicate-name `409 agent.name_exists` — is unavoidable for a hidden holder
(the name invariant is workspace-wide, agents.md §4.4) and masks the id instead:
`detail.agent_id: null`.

**The helper.** `services/visibility.py`: `Viewer(user_id, filters)` with `Viewer.ALL`,
`Viewer.of_request` (cached on `request.state.viewer`; `filters` is True only for a non-admin
in an isolated workspace), `require_viewer` (FastAPI dependency), `viewer_for_principal`
(assistant conversation owner: `user:<id>` / `config-admin` / `local-operator`),
`viewer_for_key` (a `/v1` key = its creator's CURRENT visibility; NULL-creator and
admin-creator keys see everything), `sees(viewer, db, created_by)` ≡ `clause(viewer, column)`,
`require_seen`, `hidden_aws_ids` (the `resource_creators` side table, empty in CN) and the CN
`hidden_ledger_ids` (a kind's own `created_by` column — KBs, managed memories).
`services/agent_access.py`: `agent_in` (workspace + visibility; deleted reads missing unless
`include_deleted`), `agent_clause`, `agent_visible` / `require_agent_visible`,
`hidden_agent_ids` / `hidden_agent_names`, `HiddenAgents` (actors `<agent>__<human>`,
namespaces, span `service` via the observability resolver, ledger session ids, registry record
ids, `mentions(payload)`), `hidden_kb_ids` (ledger), `check_pins` (KB + memory pins; redeploy
checks only pins ADDED vs the stored spec).

**Choke points (each takes `viewer: Viewer = Viewer.ALL`; only routers pass the request's).**
`evaluation/routers._dataset_in` / `_run_in` / `_visible_evaluator` / `_require_cloud_visible`
(a cloud copy follows its local rows: hidden only when every row is hidden),
`pipeline_routers._pipeline_in` / `_check_output`, `online.get_config` / `list_configs` /
`_visible_row` (an experiment- / canary-owned arm follows its OWNER's creator) / reports /
results / `online_quality`, `optimization.routers._experiment_in` / `_eligible_agent` /
`_visible_dataset`, `canary_routers._canary_in` / `_eligible_agent`, `skill_lab.jobs.get_job` /
`list_jobs` / `_resolve_tasks_file` and `tasksets.get_row` / `list_tasksets`,
`memory_ownership.require_managed` / `hidden_ids`, `routers/knowledge.visible_kbs` /
`_require_visible`, `agent_names.claim_agent_name` (the 409 names the holder only when
visible). Internal callers (deploy pipeline, resume, workers, purge, presets, the pipeline run
thread, the deploy-job catalog re-validation) keep the default.

**Agents + keys (#263).** Every `/api/agents/{id}*`, `/api/chat/{id}*`, `/api/jobs/{id}`
(deploy job payload `agent_id`), `/v1` route, `live-agent-card`,
`/api/eval/agents/{id}/log-streams`, eval run / online config / experiment / canary create,
discovery (`_mask_managed`), observability (`HiddenAgents` after `PrivateSessions`, incl. the
CN trace rail `GET /api/traces/{session_id}`), memory console actors, local-registry A2A
records (`HiddenAgents.record_ids`), Connection `referenced_by`, KB / memory used-by, the
policy editor's agent picker, the overview tiles. Cross-module mutations (recommendation
accept / inputs, experiment and canary actions other than `verdict` / `cleanup`) require the
agent — for a canary BOTH arms — visible.

**CN decisions.** The step-1 member rules (§4: own API keys, own chat sessions) hold in both
modes and are not widened; `isolated` adds the agent layer on top. D20 policies and
`policy_entities` stay shared (governance; `enforce` stays explainable). The local registry's
MCP / skill records, gateways and Connections stay shared for READ (a record's content
mutations are creator-or-admin since N15-obs7 — §8). The visibility 404 is evaluated
before `policy.denied` (403). No convert / inbound-auth routes exist in CN (upstream hunks
dropped).

**UI.** V2 workspace detail: `VisibilityCard` (Segmented shared / isolated, discard / save,
hint per mode) + a summary row and list tag (`VisibilityTag`); agents discovery
`already-managed` reason; KB `attached_agent_count` + "+n agent(s) not visible to you" tag.

**Tests.** `tests/test_visibility.py` (ported #262, CN fakes for the KB / memory / cloud-store
backends), `tests/test_agent_visibility.py` (ported #263 minus convert / inbound-auth),
`tests/test_visibility_cn.py` (D20 ordering, canary arms, route policy, overview assets).

**By-id surfaces (lead ruling 2026-10-16, amended 2026-10-19 for chat — N16-obs70 / N16-obs71,
branch `cn/r16-j`).** Chat sessions are personal in BOTH modes: every by-id chat route (`GET
…/history`, `GET …/memory?session_id=`, `POST …/sessions/{sid}/stop`, `GET
…/sessions/{sid}/turns/{n}/stream`, and `POST /api/chat/{agent}` with an existing `session_id`)
resolves the row through `routers/chat._session_for` → `session_visibility.require_own` (own
session, or any as an administrator) and answers another member's session `404
chat.session_not_found` under `shared` as under `isolated` — the 2026-10-16 "stays open under
shared" reading let a member end an administrator's live session, read its memory rail and
append turns into it. The reattach `409 chat.turn_not_yours {author}` is reachable only on a
session the caller may read, when that turn was sent by a different account (an administrator's
turn in a member's session, or the reverse); another member's session is 404 before any turn is
looked at. The observability surfaces keep the 2026-10-16 ruling: under `shared` `GET
/api/traces/{sid}` and the observability session / trace details stay open to every member
(accepted design, E04 §7); under `isolated` the same predicate (`own_clause` / `may_list` /
`hidden_session_ids`, the one definition the chat list uses too) gates them and the
observability `traces` / `sessions` lists drop the rows (`observability.session_not_found` /
`observability.trace_not_found`). A session with no ledger row has an unknown owner and stays
open to whoever holds the id — the row-less residual, precisely: sessions the hub never saw (direct AWS callers, eval replays) have no row, so stop / reattach answer 404, history is empty, the memory rail reads only the caller's OWN actor partition, and a `POST` continues the AWS runtime state only for a holder of a ≥33-character, class-checked id. Regression matrix: `tests/test_chat_session_ownership.py` (route ×
role × mode), `tests/test_permission_matrix.py` (`chat_sessions.update` = 404 for another
member in both modes).
`GET /api/policies/entities` stays MEMBER and keeps listing key ids / names / creators and the
visible agents: policy authoring needs them and ids are not secrets.

**Masked references in run payloads (r15-d, N15-obs8; isolation UI V8).** A visible run may
still NAME rows the viewer cannot see — an administrator's run on member A's dataset or agent
is visible to member B (admin creator) and used to carry `dataset_name` / `agent_name` while
B's own reads of those rows answer 404. Every run payload (`GET /api/eval/runs`, `GET
/api/eval/runs/{id}`, create / stop / recheck responses) now goes through
`evaluation/routers._runs_out(db, ws, viewer, runs)` — the one choke point, never a per-route
mask: a referenced local dataset hidden by `hidden_ledger_ids`, a cloud store hidden by
`_hidden_cloud_ids` (a `cloud:` run) or an agent in `agent_access.hidden_agent_ids` keeps its
**id**, has its **name nulled** and is flagged in the additive `hidden: {agent: bool, dataset:
bool}` (every key always present; all false for admins, the creator, a shared workspace and a
viewer who sees the rows). The V2 task list / detail render `v2.common.hiddenRef`
(已隐藏（对你不可见）) for a flagged reference (`tasks.ts: sourceLabel / agentLabel`) and never
the literal null. Evaluator ids are opaque (`cust_…`) and carry no name. The run response is
`additionalProperties: true` in the frozen eval OpenAPI slice, so the field needs no
`ALLOWED_DELTA` entry (noted there); the key set is pinned in
`tests/contract/test_eval_response_shapes.RUN_OUT`. Tests: `tests/test_visibility.py::
test_isolated_run_masks_the_names_of_rows_the_viewer_cannot_see`,
`frontend/src/v2/pages/tasks/TaskDetail.test.tsx`.

Observability boundary clarification (2026-10-28): in both workspace visibility modes,
members must first establish current-workspace provenance for trace/session records.
An unknown external runtime is not a creator-NULL local resource. Lists and all by-id
readers (including transcript, preview and scoring) reject unassignable records; the
existing owner/private-session rules still apply to attributable records. Exact runtime
service aliases are used for access, not the looser display-name resolver. Administrators
retain account/region-wide diagnostics. This is an application access boundary, not
physical log isolation by environment; dashboard aggregates remain account/region-wide.

## 8. Step 3 — mutation ownership (r13-g, branch `cn/r13-g`; N13-obs15)

Visibility (§7) decides who may *see* a row; this step decides who may *change or destroy*
one. Until round 13 only agent redeploy / delete and memory record delete were
creator-or-admin; every other mutation was plain `MEMBER`, so an intern could `complete` the
administrator's canary (E13 NC-16), publish another member's Skill Lab job to the Registry
(E15 NC-15) or rename / delete any dataset or evaluator. Acceptance rulings on `cn/q-acc-2`
asked for creator-or-admin on all of them.

**The rule (one helper).** `services/ownership.py`: `may_mutate(row, identity)` /
`require_creator_or_admin(row, identity, code, *, message, detail)` — an administrator may act
on every row; the member whose account created it (`created_by` == their `users.id`) may act
on it; anyone else answers **403 `<kind>.forbidden`** with `{created_by, created_by_name,
<kind>_id, action}` before any side effect. **NULL creator → administrator-only** (the CN rule
of §4, extended verbatim: a row nobody is recorded to have created is an administrator's).
`routers/agents.require_agent_creator` now wraps the same helper and only adds its two
exemptions (discovered runtimes detach for any `agents.delete` holder; presets keep
`refuse_system_mutation`). Reads, invoke and chat stay shared. The gate runs **after the
kind's visibility lookup** (a hidden row still answers the kind's 404, so it stays
indistinguishable from a missing one) and **before any stage / status / in-use check** (a
forbidden caller learns nothing about the row's state).

| route | gated actions | not gated (why) | code |
|---|---|---|---|
| `POST /api/runtime-canaries/{id}/action` | `setup`, `traffic`, `advance`, `complete`, `rollback`, `cleanup` — every action that mints a version, drives traffic, changes the production routing or destroys resources | `verdict` (scores sessions both arms already collected; changes no version, route, weight or resource) | `canary.forbidden {canary_id, action}` |
| Skill Lab jobs | `POST …/jobs/{id}/cancel`, `…/resume`, `…/publish` (mutates the Registry record), `DELETE …/jobs/{id}` | `POST …/import-taskset` (creates a NEW set owned by the importer); every read | `skill_lab.forbidden {job_id, action}`; publish also needs the **source record's** creator-or-admin — `registry.forbidden {record_id, action: "publish"}` (N17-obs263) |
| Skill Lab task sets | `PUT` / `DELETE …/tasksets/{id}`; `POST …/jobs/{id}/apply-expansion` is gated by the **target task set's** creator (the write lands there — the job's creator alone is not enough; a target that no longer exists is left to the service's own 404) | sample sets have no creator: members hit this 403, administrators fall through to the service's `skill_lab.sample_readonly` | `skill_lab.forbidden {taskset_id, action}` |
| `PUT` / `DELETE /api/eval/datasets/{id}`, `POST …/sync-to-aws`, `POST …/publish-version` | edit, delete, sync (rewrites the row's `cloud` blob and the shared cloud draft), publish (adds a version, rewrites the blob) — rev-r11 finding N13-obs15a | every read | `dataset.forbidden {dataset_id, action}` |
| Cloud store: `DELETE /api/eval/datasets/cloud/{id}`, `DELETE …/versions/{v}`, `POST …/cloud/{id}/publish-version` | the store records no creator; its owner is **derived from the local rows that point at it** (several creators may sync into one store) and each mutation rewrites every pointing row's `cloud` blob — so the caller must be allowed to mutate **every** pointing row (the first they may not names itself); 403 before the in-use / pinned 409 | a store no local row points at (made outside the console) has no derivable owner and stays open to every member who sees it — unchanged; the NULL-creator rule is about stamped rows and the store is not one | `dataset.forbidden {cloud_id, dataset_id, action}` |
| `PUT` / `DELETE /api/eval/evaluators/{id}` | edit, delete (custom rows only — managed ids keep `evaluator.builtin_immutable`) | every read | `evaluator.forbidden {evaluator_id, action}` |
| `PATCH` / `DELETE /api/eval/online/{id}`, `POST …/pause`, `POST …/resume` (N17-obs426) | edit, delete, pause, resume of an online evaluation config. Before this, the routes checked only that the config was agent-owned (`online_eval.read_only` for an experiment arm, still checked first — except pause / resume of an arm whose owning canary or experiment is terminal, N17-obs445: canary `completed` / `rolled_back` / `cleaned`, legacy experiment `promoted` / `cleaned` / `failed`; that gate is creator-or-admin against the OWNING RUN's creator, since an arm config records none, 403 `online_config.forbidden {config_id, action, owner_run}`; an arm whose run is running or cannot be resolved stays read-only), so under `shared` a member could pause or delete an administrator's config. The gate runs after the visibility 404 and before any change; a config created before creators were stamped (NULL) is an administrator's | every read; `POST …/reports` (an insights run the caller owns, over the sessions the config sampled); create | `online_config.forbidden {config_id, action}` |
| `PATCH` / `DELETE /api/knowledge-bases/{kb}`, `POST …/files`, `POST` / `DELETE …/data-sources[/{ds}]`, `POST …/data-sources/{ds}/sync {"full": true}` (N17-obs530, `cn/wk-authz`) | edit, delete, upload, add / remove a data source, full rebuild — `routers/knowledge._require_owner`, the ledger row's `created_by`; a KB with no creator or no ledger row is an administrator's. Before this only `force` was gated, so a member could delete or rebuild an administrator's KB they were shown under `isolated` (workshop dry run 2026-10-29) | the incremental sync, `query`, every read (the service desk syncs and searches the trainer's KB) | `auth.forbidden {kb_id, action, created_by, created_by_name}` (the KB routes keep the `auth.*` code `force` already used; see knowledge-backend.md "Mutations are the creator's") |
| `PUT` / `DELETE /api/registry/records/{id}`, `POST …/reimport` (r15-d, N15-obs7 — isolation UI S1) | edit (`edit` / `replace`), reimport, delete of a local MCP / skill record — the gate runs after the visibility 404 and the system-skill refusal (`registry.system_skill_protected` stays first) and before every type / status check. Records are stamped on `POST /api/registry/records` and `POST /api/registry/skills/import` through `registry_backend.current_creator` (a `ContextVar`, like `current_actor`; the ledger backend stamps the new row); deploy-created A2A records, gateway imports, `sync-defaults` and legacy rows keep NULL = an administrator's | every read in BOTH modes (§7: records stay shared, so the other member reads 200 and is refused 403, never 404); `POST …/action` — the review workflow (the N4 state machine), not creator-gated: any member submits; approve / publish / reject / disable are an administrator's (`403 registry.approval_admin_only {action}`, N17-obs248) | `registry.forbidden {record_id, action}` |

**Order against the other 403s (N17-obs30).** The creator-or-admin gate answers BEFORE a
route's own capability gates: on `DELETE /api/eval/datasets/{id}?force=true` a member who is not
the creator gets `403 dataset.forbidden` (this gate), and only the creator's own `force` reaches
evaluation-engine.md §22's `403 auth.forbidden` (the flag is an administrator override). Both
refuse and write nothing; the order is ownership first, capability second.

**Knowledge-base `force` is the same override (N17-obs4, `cn/r17-h`).** `DELETE
/api/knowledge-bases/{kb}?force=true` strips the KB from EVERY mounting agent's spec — other
members' agents included — so the flag is an administrator's, exactly as on datasets and
evaluators: a member's `force` is `403 auth.forbidden` whether or not anything mounts the KB; a
member's plain delete keeps the MEMBER route policy (`409 kb.has_attached_agents` while mounted).
Since N17-obs530 the creator-or-admin gate runs before it: only the KB's creator reaches the
`force` refusal or the attached 409 at all.

**Pins this ruling changed** (`tests/test_visibility.py`): under `shared`, another member's
`PUT /api/eval/datasets/{id}` used to answer 200 and now answers 403 `dataset.forbidden`; a
member's edit of a NULL-creator dataset used to answer 200 and is now an administrator's.
`tests/test_auth.py` exact-payload pins gained `user_id`.

**The console learns the key.** `GET /api/auth/status` and `POST /api/auth/login` carry
`user_id` (`users.id`; `null` for the row-less config admin and before authentication) so the
V2 console can mirror the rule (`frontend/src/v2/ownership.ts`: `mayMutate(row, isAdmin,
userId)` / `useMayMutate()`): the canary detail disables every non-`verdict` action with a
creator-only hint and an `Alert` naming the creator; Skill Lab lists / details hide cancel /
resume / delete / publish / edit for rows the member cannot change; dataset and evaluator
lists / details hide edit / delete the same way; the Registry list / detail hide 编辑 / 删除 /
重新导入 and the detail shows the creator row + the same `Alert` (N15-obs7); the Agent list hides
编辑 / 删除 and the Agent detail disables them with the creator-only hint + the `Alert`
(`useAgentPermissions.creatorOnly`, N15-obs9 — a discovered runtime stays detachable for any
`agents.delete` holder, as the server allows). An open console (auth disabled) is the
operator's and shows everything. The server decides; the UI only stops members from running
into the 403. Copy: `apiErrors.{canary,skill_lab,dataset,evaluator}.forbidden`,
`v2.common.creatorOnly`, `v2.common.creatorOnlyActions` (en / zh-CN).

**Tests.** `tests/test_mutation_ownership.py` (every gated path: creator ok, other member
403, admin ok, NULL creator → member 403 / admin ok, hidden row 404 first, ownership before
stage checks, `verdict` and `import-taskset` stay open, the sample-set fall-through, the
`user_id` payload, sync / publish-version, the derived cloud-store rule, the registry paths:
PUT / DELETE / reimport creator ok · other member 403 · admin ok · NULL creator → member 403 /
admin ok · reads open in both modes · register stamps the caller · lifecycle stays open),
`frontend/src/v2/ownership.test.ts`, `frontend/src/v2/pages/registry/RecordList.test.tsx`. Referential delete guards (the
companion ruling N13-obs16) are specified in `evaluation-engine.md` §22 and tested by
`tests/test_referential_delete_guards.py`.

**Referential guards on the agent itself (N16-obs111, `cn/r16-q`).** The same `*.in_use`
family protects the Agent row: `DELETE /api/agents/{id}` while a `running` Runtime canary names
the agent → `409 agent.in_use {canaries}` (no force for anyone; settle the canary — `cleanup`,
`rollback` or `complete` — then delete). `completed` / `rolled_back` / `cleaned` canaries never
block. The guard runs after the creator-or-admin gate and the `agent.deploy_in_progress` check,
before any AWS call; the architect purge keeps its own order (canaries first, then agents).
Contract and tests: `agents.md` §1.1.

## 9. Login protection and account-management guards (r12-g, N12-obs57; branch `cn/r12-g`)

**Why.** The console NLB (N12-obs52) is open to the internet by user decision, so
`POST /api/auth/login` is the only gate. The sign-in controls below are in-process state in
`backend/app/services/login_guard.py` — one uvicorn worker over SQLite, no Redis — and a
backend restart clears them (acceptable: the lock cap is 30 min anyway; documented in
`docs/api.md` and the troubleshooting guide).

**Per-account lockout — PRIMARY.** Keyed by the *submitted* normalized username
(`users.username_key` for an account, the lower-cased input otherwise), so a lock never
discloses whether an account exists. Only `auth.invalid_credentials` counts: a correct
password on a pending / disabled / expired account proved ownership and is not a
brute-force failure. After `auth_login_lockout_threshold` (5) consecutive failures the
name is locked for `auth_login_lockout_base_seconds` (60) seconds; every further failure
after the lock lapsed doubles it (60 → 120 → 240 → 480 → 960 → 1800) up to
`auth_login_lockout_max_seconds` (1800). Attempts during the lock answer 429 `auth.locked`
with `Retry-After` BEFORE any credential work and do not count. A successful sign-in
forgets the name; an administrator forgets it with `PATCH /api/users/{id} {"unlock": true}`
(`UserPatch.unlock`, StrictBool; `false` is a no-op; the route stays ADMIN — no new route,
so `route_policy` is unchanged). `serialize()` adds `failed_logins` / `locked_until` (ISO
or `null`) for the Users consoles, which show an *Unlock* action while locked. The
built-in admin is covered too: its lock is capped the same way and a restart clears it —
an attacker who keeps one failure per 30 min can keep the admin name locked, which is the
documented cost of a per-account control; the admin's recourse is the restart (or the
`auth_login_lockout_enabled=false` switch). Consecutive failures keep accumulating while
they land within `2 × max` (1 h) of each other; the maps are capped at 10 000 keys.

**Built-in admin exemption (rev-r11 ruling).** `settings.auth_username` is never entered
into the per-name lockout (`lockable = account_key != auth_username.lower()`): it is the
break-glass credential with the strong configured password, and a per-name lock would let
anyone on the internet keep the operator out. Its wrong passwords still count against the
peer window, so a spray from one address is throttled; the lockout tests pin the exemption.

**Per-source-IP windows — SECONDARY, coarse (rev-r11 FIX-FIRST).** Failed sign-ins per
transport peer in a sliding window: `auth_login_ip_limit` (20) per
`auth_login_ip_window_seconds` (300); `0` disables it. The window is consulted **only after a
failed credential check**: once tripped, further wrong passwords from that peer answer 429
`auth.rate_limited` + `Retry-After` instead of 401, but a correct password is never refused
by it and successful sign-ins never count (`LoginGuard.failed()` returns the peer's
`retry_after` as it stood before the failure; `check()` is per-name only). Same shape for
`/v1` (a valid key is never refused; every 401 counts) and registration (failed attempts —
validation errors, collisions, backlog — count; a valid registration is never refused; a
collision under approval mode counts but keeps the pending-shaped 201, since a 429 would
disclose it). Why: **the transport peer is nginx (EIP path) or the NLB**. uvicorn binds
`127.0.0.1:8000` behind nginx; the systemd unit now runs it with `--proxy-headers
--forwarded-allow-ips=127.0.0.1` and nginx sends `X-Forwarded-For`
(`infra/ec2/nginx-site.sh`), so on the EIP path `request.client.host` is the real client
(`infra/tests/test_nginx_site.py` pins both). Behind the NLB (client-IP preservation OFF)
it is the NLB's private IP shared by every user — a pre-check window there would let twenty
wrong passwords from anyone sign the whole console out, which is exactly what rev-r11 found.
Hosts bootstrapped before r12-g keep their original unit (`host-update.sh` does not rewrite
it): uvicorn's CLI default is already `--proxy-headers` with `forwarded_allow_ips=127.0.0.1`,
so they behave the same; a systemd drop-in with the explicit `ExecStart` makes it pinned. The
per-account lockout is the control to tune; the windows are a backstop against sprays.

**Aged-out failures are forgotten permanently (N17-obs160, `cn/r17-o`).** A failed attempt
counts until `auth_login_ip_window_seconds` after its recorded wall-clock timestamp, and the
first read of the window after that moment drops it for good (`SlidingWindow._trim` is a
destructive head trim of an arrival-ordered deque). A backwards clock step — an NTP
correction — therefore does **not** revive failures that had already aged out; it only
lengthens a running per-name lock by the step, because a lock is a wall-clock instant. The
same holds for the `/v1` and registration windows (one `SlidingWindow` class). The E18 model
(`tests/property/test_login_guard_model.py::PeerModel`) encodes exactly this; its earlier
"keep every failure, filter by timestamp" reading predicted a 429 the product rightly does not
give after `advance → step back`.

**`/v1` keys.** `require_api_key` counts every 401 it raises (missing header, unknown /
disabled / offboarded key) per source IP: `auth_api_key_ip_limit` (20) per
`auth_api_key_ip_window_seconds` (300) → further invalid attempts answer 429
`auth.rate_limited`; the window is read only after a failed lookup, so a valid key is never
refused by it, never counts, and `last_used_at` is stamped as before (#259).

**Envelope.** Every 429: `{code: auth.locked | auth.rate_limited, message, detail:
{retry_after}}` plus the `Retry-After` response header; `AppError` gained an optional
`headers` argument for this. Unknown name and wrong password stay one 401 code, one message
and the same PBKDF2 burn (`services/users._burn_verify`).

**Journal.** Logger `launchpad.auth` (→ journald under systemd): one `WARNING` per lock
(`login lockout: account=<key> consecutive_failures=<n> locked_for=<s>s peer=<ip>`), one
per window trip (`login rate limit` / `registration rate limit` / `/v1 api-key rate limit`),
one per registration-backlog refusal, one when a session secret is generated; one `INFO`
per registration collision. Never any password material.

**Password policy** (`services/users.validate_password(password, username=, email=,
settings=)`). `auth_password_policy` selects the rule set (r14-c, N14-obs3, user decision
2026-10-19): **`strict`** (default, the r12-g behaviour) = `auth_password_min_length`
(default 12), not equal to the username / email / email local part (case-insensitive), not
in `COMMON_PASSWORDS` (embedded, matched after lower-casing and stripping separators);
**`relaxed`** = the minimum length only (no identity check, no common-password check). The
floor of `auth_password_min_length` is **6** (was 8); pydantic refuses a lower value at
settings load, as before. `auth.weak_password` carries
`detail.reason ∈ {too_short, matches_identity, common}` + `min_length` — in relaxed mode only
`too_short` can occur; the consoles word each reason (`auth.errWeakPassword` /
`errPasswordMatchesIdentity` / `errPasswordCommon`, en + zh). Applied at registration and at
the admin `password` patch (`generate_password` never undercuts the minimum: it is
`max(14, min_length)` long); stored hashes are never re-checked. **The consoles learn the
effective policy from the server:** `GET /api/auth/status` (and the login response) carry
the additive `password_policy: {mode, min_length}` (`users.password_policy(settings)`); the
registration form's hint (`auth.passwordHint` with the configured `{{min}}` in strict mode,
`auth.passwordHintRelaxed` = length only in relaxed mode) and its client-side length
pre-check read it, so no console copy hard-codes "12" or a rule the backend will not
enforce. The admin Users page only ever requests a *generated* password (`password: null`),
so it has no rule text to adjust. **Deployment note:** both CN hosts run `relaxed` with
`auth_password_min_length: 8` by user decision 2026-10-19 (`config/launchpad.yaml`:
`auth_password_policy: relaxed` + `auth_password_min_length: 8`); lower to 6 with the one
config line `auth_password_min_length: 6`. Tests: `tests/test_password_policy.py` (strict
and relaxed), `tests/test_config.py::test_password_policy_defaults_and_floor`,
`frontend/src/auth/AuthGate.test.tsx`.

**Self-registration (A-2).** `POST /api/auth/register`, in order: `auth.registration_disabled`
→ per-peer window (`auth_register_ip_limit` 10 per `auth_register_ip_window_seconds` 3600;
every attempt that passed body validation counts, successful or not; 429 `auth.rate_limited`;
the NLB caveat above applies — onboarding a team through the NLB counts against one address)
→ pending cap (approval mode only: `pending_count(db) >= auth_registration_pending_max` (50;
`0` = none) → 409 `auth.registration_backlog {pending, max}` + `WARNING` naming the setting;
admin-created accounts are neither counted nor refused) → `register_user`. **No enumeration:**
the service keeps raising the precise `auth.username_taken` / `auth.email_taken` (the reserved
admin name is `username_taken`), and the PUBLIC route maps both: under approval mode (the
default) it answers `201` with the exact pending-registration body (`_pending_shaped`) and
creates nothing — the caller could not sign in before an approval anyway, and the `INFO`
collision line tells the administrator; under instant activation a fake "ready" would strand
a legitimate user, so both collisions share one `409 auth.registration_rejected` that names
neither field (the residual: a probe learns that *one of* the pair exists). Adapted pins:
`test_users_api.py::test_duplicate_username_and_email_are_case_insensitive` (+ the
`OPERATOR` parametrization) now assert the uniform answers and the unchanged row count.

**Last-admin / self-delete guards.** `services/users.active_admin_ids(db)` = `role=admin`,
`status=active`, not expired. `apply_patch` refuses, before any mutation, a patch that would
take an active admin row out of that set — `role=member`, `status` ∈ {pending, disabled}
(invalid values keep their 400), a past `expires_at` — when no OTHER active admin row remains
(409 `users.last_admin {user_id, username}`); self-demotion is the same rule (allowed while
another active admin exists, resolved per request so the demoted session loses `/api/users`
at once). `delete_user(..., actor_user_id=)` refuses the caller's own row (409
`users.self_delete`; the config admin has `user_id=None`, so it never fires for it) and the
last active admin row (409 `users.last_admin`). **The config-driven admin does not count**
(decision): it is a break-glass credential, and account management must never depend on it —
at least one real administrator row must remain; zero rows is a legal *starting* state, and
once one exists it is pinned until a second one is promoted. Adapted pin:
`test_api_key_lifecycle.py::test_an_admin_role_creator_needs_no_grant` promotes a second
admin before demoting.

**Session epoch (rev-r11 ruling).** A password change MUST sign that account's other
sessions out. `users.session_epoch` (INTEGER, default 0; additive migration in `core/db.py`)
is bumped by every `password` patch in `apply_patch` (reset or chosen); the v3 cookie payload
is `3:subject:user_id:epoch:expiry` and `resolve_identity` refuses a cookie whose epoch is not
the row's current one (→ `401 auth.required`, the console's logged-out state). The built-in
admin has no row, so its epoch is `admin_session_epoch()` = the first 16 hex chars of
`sha256("agentcore-launchpad-admin-epoch:" + password)` — rotating `LAUNCHPAD_AUTH_PASSWORD`
still ends every admin session (`test_auth.py::test_password_rotation_invalidates_session`
restored in that form). v2 cookies are refused (one re-login at deploy, which the secret
rotation causes anyway). `Identity.session_epoch` carries the value at issue time.

**Session secret (A-3).** `routers/auth.session_secret()`: `LAUNCHPAD_SESSION_SECRET` when set;
otherwise `session_secret_file` (default `data/session_secret`, created with `O_EXCL` 0600 and
64 hex chars on first use; an empty file is regenerated; cached per path,
`reset_session_secret_cache()` for tests / rotation). `_signing_key =
sha256("agentcore-launchpad-session:" + secret)` — the admin username / password are no
longer part of it, so a leaked password cannot forge a cookie offline; a password rotation
still ends that account's sessions through the session epoch above. Rotation on demand: set a
new env value, or delete the file and restart → every session is signed out once; the deploy
that introduces r12-g does the same once. `data/session_secret` is listed in `.gitignore`
explicitly (the whole `data/` tree already is). The file lives under the gitignored `data/` beside the
ledger (ubuntu-owned under systemd); the hermetic suite points `LAUNCHPAD_SESSION_SECRET_FILE`
at its temp dir. Pins: `test_auth.py::test_password_rotation_invalidates_session` (kept, now
via the session epoch) and `test_session_secret_rotation_invalidates_session`.

**Session cookie.** `HttpOnly`, `SameSite=Lax`, `Secure` when `run_mode=prod` or
`auth_cookie_secure` (unchanged); lifetime `auth_session_ttl_seconds` (default 43200). It is
an **absolute** lifetime — the signed expiry is never refreshed on activity, so there is no
idle timeout: a session ends at issue + TTL, at the account's `expires_at`, or when the
account is disabled / deleted (resolved per request).

**Settings** (`LAUNCHPAD_` env / `launchpad.yaml`): `auth_session_ttl_seconds`,
`auth_login_lockout_enabled`, `auth_login_lockout_threshold`,
`auth_login_lockout_base_seconds`, `auth_login_lockout_max_seconds`, `auth_login_ip_limit`,
`auth_login_ip_window_seconds`, `auth_api_key_ip_limit`, `auth_api_key_ip_window_seconds`,
`auth_password_policy`, `auth_password_min_length`, `auth_register_ip_limit`,
`auth_register_ip_window_seconds`,
`auth_registration_pending_max`, `session_secret`, `session_secret_file`.

**Tests.** `tests/test_login_protection.py`, `tests/test_v1_key_rate_limit.py`,
`tests/test_password_policy.py`, `tests/test_registration_abuse.py`,
`tests/test_admin_guards.py`, `tests/test_session_secret.py`; the hermetic suite resets the
guard around every test (`conftest.reset_login_guard`) because every TestClient shares the
`testclient` peer.

## 10. Model providers are owner-bound resources (N17-obs203, r17-mp, branch `cn/r17-z`)

**Model providers are owner-bound resources.** User decision 2026-10-21 (verbatim intent:
"无论管理员还是普通成员都可以增加、删除、修改自己加的模型提供方（管理员可以修改或删除普通成员的）"):
`model_providers` joins the stamped kinds — `created_by` (`users.id`) / `created_by_name` through
`services/creators.py`, plus a `scope` (`personal` | `workspace`). The two rules of §7 and §8 apply
in their order, hub-globally (identical under `shared` and `isolated`): a member SEES every
`workspace`-scope provider and their own `personal` ones — another member's personal provider is
`404 model_provider.not_found` on every route — and may CHANGE only the rows their account created
(`403 model_provider.forbidden {provider_id, action, created_by, created_by_name}`, raised by a
dependency before the body is validated); a NULL creator (the built-in Bedrock row, legacy rows,
the config admin's) is an administrator's. Administrators see and change everything, and alone
set or flip `scope: workspace`. Sharing TRANSFERS the row to the workspace (N17-obs225, rev-r20
B1): `created_by` becomes NULL, the former creator loses every mutation right and the journal
note names them; on a workspace-scope row `base_url`, `credential_ssm_parameter` and the key are
an administrator's whoever the creator is (403 `model_provider.forbidden {action}`) — the
creator keeps label / enabled / models (`model-registry.md` §4.1). The pre-obs203 username in
`created_by` is re-pointed once by `_migrate_creator_columns` (`model_providers` joined
`_LEGACY_USERNAME_CREATOR_TABLES`; `system` / `bootstrap` record no creator) and every existing
row defaults to `workspace`. What a member may USE follows the same line: the catalog offered
(`GET /api/capabilities`, the architect prose) is the shared providers' models plus their own,
and naming another member's personal model is `422 agent.model_not_visible` on deploy /
redeploy (`run.model_not_visible` for an evaluation actor; `models.model_not_visible` where the
hub itself would spend the key — a judge / derived evaluator's model, a plan approval's judges,
a recommendation's reflection model, the hub judge builder — N17-obs227) — the owner or an
administrator may still deploy it. Contract and the owner-derived credential references: `model-registry.md`
§4.1; tests `tests/test_model_provider_ownership.py`, `tests/test_model_provider_visibility.py`.
