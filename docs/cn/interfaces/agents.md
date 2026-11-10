# Agents — lifecycle contract (delete vs. deploy job; reserved names)

The rules the ordinary-agent routes (`routers/agents.py`) and the deploy pipeline
(`deployer/pipeline.py`) hold between them. Written for round 16 (branch `cn/r16-h`,
2026-10-19) from two live findings on t1 (build aaacb0a6): N16-obs83 (a delete during a
deploy job orphaned a Runtime, a role, an S3 package, a log group and a registry record)
and N16-obs82 (`launchpad-eval-worker`, the system eval worker's base name, was not
reserved). Ownership / visibility of agents is in `users-and-ownership.md`; the deploy
stages themselves in `docs/architecture.md`.

## 1. A deploy job owns its agent until it is terminal

**Rule (ruling, lead 2026-10-19).** `DELETE /api/agents/{id}` is refused while the agent
has a deploy job that is not terminal:

| job status | `DELETE /api/agents/{id}` |
|---|---|
| `queued`, `running` | `409 agent.deploy_in_progress {job_id, stage, job_status}` — nothing is touched (no AWS call, the row stays `deploying`, the name claim stays) |
| `succeeded`, `failed`, `cancelled` | proceeds (the existing teardown) |

* `stage` is the stage the job is at: the `running` stage, else the first `pending` one
  (`generate` for a job that has not started).
* **Members and administrators alike; no `force`.** The member waits for the job to end —
  succeeded, or failed — and deletes then. The same code the redeploy / inbound-auth routes
  already answer for a `deploying` agent; consistent with the `409 aws.conflict` AWS itself
  returns while the Runtime is `CREATING`. This closes the window **before** any AWS
  resource exists (`generate` / `package` / `provision`) and after it (`register`).
* **"Alike" means every caller the creator-or-admin gate admits** (users-and-ownership.md
  §4 / §8): another member's delete or redeploy of a redeploying agent reads `403
  agent.forbidden`, not this 409 — ownership answers first on every route (N17-obs51, DOC;
  the order lives in api.md "Refusal precedence").
* Shared by every caller of `routers/agents.teardown_agent_row` — today the route alone:
  the architect purge does not go through it (its `_step_agents` deletes the cloud resource,
  sweeps the residue and calls `mark_agent_deleted` itself) and has its own, earlier
  `assistant.purge_agent_busy` blocker for the same situation; the `delete_agent_row`
  wrapper is kept for callers that record only the boolean (none in tree, r16-q).
* Lookup: `pipeline.in_flight_deploy_job(db, agent_id)` — the newest `Job` joined through
  `Deployment.job_id` with `status ∈ LIVE_JOB_STATUSES`.

* **The knowledge-base force delete asks the same guard for every mounting agent (N17-obs31).**
  `DELETE /api/knowledge-bases/{kb}?force=true` strips the pin from every mounting agent's spec;
  while any of them has a `queued` / `running` deploy job (or is `deploying`) the whole force is
  `409 agent.deploy_in_progress {kb_id, agents: [{id, name, job_id, stage, job_status}]}` and
  nothing is written — a strip racing a `package` stage would ship a spec the ledger no longer
  has. Rule: `knowledge_backend.strip_kb_from_agents` (knowledge-backend.md contract notes).

### 1.1 A Runtime canary owns its agents until it is settled (N16-obs111)

**Rule (ruling, lead 2026-10-19; branch `cn/r16-q`).** `DELETE /api/agents/{id}` is refused
with `409 agent.in_use {canaries: [{id, name, status, stage}]}` while a Runtime canary whose
status is not terminal (`running`) names the agent as champion or challenger — members and
administrators alike, no `force`; `completed`, `rolled_back` and `cleaned` canaries never
block. The operator settles the canary first (`cleanup`, `rollback` or `complete` on
`POST /api/runtime-canaries/{id}/action`), then deletes the agent.

* Why: before the rule the delete answered 200 and the canary row kept `running` with
  `champion_agent_id` / `challenger_agent_id` naming a `deleted` Agent — its A/B row and
  per-arm online configs stayed live, and every later action but `verdict` re-resolved the
  agent to `404 agent.not_found`, so only `cleanup` could ever settle it (r16-o reference
  graph, edge `agent_delete_with_running_canary`). The ledger invariant "no canary naming a
  missing Agent" (testing.md §6.1) did not fire because the row was marked, not removed.
* Order inside `teardown_agent_row`: preset refusal → `agent.deploy_in_progress` →
  `agent.in_use` → the AWS teardown. A refused delete touches nothing (no AWS call, the row
  stays `active`, the name claim stays). `canaries` lists every blocking canary oldest first;
  the match is on the agent id alone (a legacy row with a NULL `workspace_id` still routes
  to the agent and still blocks). `CANARY_BLOCKING_STATUSES` (`optimization/models.py`) is
  the one definition of "not terminal" — the canary create route's `already_running` check
  keys on it too. The rule lives once, in `services/canary_references.refuse_agent_in_use`.
* **The system-preset uninstall shares the guard (N16-obs111a).** `DELETE
  /api/system-agents/{key}` (`system_agents.service.uninstall_preset`) answers the same
  `409 agent.in_use {canaries}` after its own `agent.deploy_in_progress` check and before the
  `uninstalling` claim, so a canary still naming the preset as an arm never loses its
  Runtime. The create route already refuses presets as canary arms (`canary_capability` →
  `400 canary.agent_unsupported`, reason `system-managed`), so only a legacy row can reach
  the guard; tests `test_system_agents.py::test_uninstall_is_409_agent_in_use_while_a_canary_names_the_preset`,
  `optimization/test_runtime_canaries.py` (preset refused at create; `completed` /
  `rolled_back` / `cleaned` never trip `already_running`).
* **The architect purge is not this path.** A conversation CLEAR cleans the conversation's
  canaries (`purge._STEPS`: `canaries` before `agents`, `act_cleanup` under the canary's own
  `running_action` claim) and then marks each agent through `mark_agent_deleted`, never
  through `teardown_agent_row` — so a clear still removes both, in that order
  (`tests/test_assistant_purge_dependencies.py::
  test_clear_stops_the_canary_waits_for_its_endpoints_then_deletes_the_agent`).
* Console: the delete dialog stays open on the 409, shows
  `v2.agents.deleteBlockedByCanaries` and one link per canary to its detail page
  (`/v2/eval/experiments?mode=canary&canary=<id>`, where cleanup / rollback / complete
  live); `apiErrors.agent.in_use` (en / zh-CN) is the toast copy.
* Tests: `tests/test_agent_delete_canary_guard.py` (409 for champion and challenger with the
  row and the canary untouched and the name still taken; every running canary listed
  oldest first; `completed` / `rolled_back` / `cleaned` never block; 409 → cleanup → 200
  → 404; the deploy-job guard answers first; the purge keeps its own order),
  `tests/graph/test_graph_reference.py` edge `agent_delete_with_running_canary` (refusal
  self-loop: creator and administrator both 409, nothing written),
  `frontend/src/v2/pages/Agents.test.tsx`.
* **A `cleaned` row that still lists blockers never blocks either (N17-obs121 ruling, `cn/r17-t`).**
  `cleaned` is the ledger's terminal verdict that the cleanup RAN and every delete was issued
  — not that every resource is verifiably gone. A refused `DeleteAgentRuntimeEndpoint`
  (AccessDenied, a throttle, a 5xx, a timeout) is recorded `skipped` under
  `artifacts.cleanup` (`category: endpoint:<name>`), the row flips to `cleaned` all the same
  (evaluation-engine.md N-SCN-obs3), and `canary_service.cleanup_blockers(artifacts.cleanup)`
  is the one definition of what such a row left behind (`skipped` entries other than the
  kept `s3:` candidate zip and the `endpoints-gone` wait marker). Three consequences, all
  deliberate: (1) the agent delete proceeds — the guard keys on `CANARY_BLOCKING_STATUSES`
  alone, so members and administrators delete the agent while its stable / treatment
  endpoints may still exist; the hub's own teardown deletes the runtime and reports what it
  could not remove in its summary, and whether AWS removes or refuses a runtime that still
  owns named endpoints is a deploy-ops live re-test item, not something this rule depends
  on; (2) the ONE retry path is `cleanup` again on the `cleaned` row (`POST
  /api/runtime-canaries/{id}/action`, idempotent, 202 — it re-issues the delete for every
  owned endpoint name (`_owned_resources(row)`), re-waits for them to disappear and
  rewrites `artifacts.cleanup`; the round-7 fault walks' recovery oracle proves a second
  `cleanup` clears the blockers once the control plane answers again); nothing retries on
  the operator's behalf — no later action, no agent delete sweep; (3) the architect
  conversation purge is the one caller that WAITS on blockers (`assistant/purge.py` re-runs
  `act_cleanup` and reads `cleanup_blockers` before marking the agent). Today the blockers
  are visible only on the canary row's `artifacts.cleanup`; the console has no banner for
  them (a copy follow-up, not part of this ruling).

* **Every re-publish shares the guard (N17-obs16, fixed in `cn/r17-d`).** `POST
  /api/agents/{id}/redeploy` — and every caller of `routers/agents.republish_agent`, the ONE
  spec-update entry point — answers the same `409 agent.in_use {canaries}` while a canary
  names the agent, right after its `agent.deploy_in_progress` check and before any
  validation; nothing is written (no job, no snapshot, the row keeps its spec / version /
  status). Why: a redeploy that finished under a running canary moved production to a new
  version, and the canary's later `complete` (no job live by then → 202) set the agent's
  spec and version to a candidate built on the PRE-redeploy base — production silently
  reverted, every call legal, invisible to the digest oracle (rev-r17 on `cn/r17-c`). The
  policy bulk re-publish (`policy_enforcement.apply`) lists such an agent as `skipped`
  ("a Runtime canary is running on this agent") instead of answering 409 — a bulk operation
  reports, it does not refuse. The one re-publisher that does NOT ask the guard is the
  system-preset repair (`system_agents.service`, an update-mode job on a preset row): harmless,
  because a preset can never be a canary subject — the canary CREATE route refuses it up front
  (`400 canary.agent_unsupported`, `reason_code: system-managed`, via `canary_capability`; test
  `test_create_refuses_a_system_managed_preset`) and `setup` / `complete` / `rollback` refuse a
  system subject again (`_refuse_system_subject`), so no `running` canary row can name a preset.
  Tests: `tests/test_agent_republish_canary_guard.py`,
  `tests/graph/test_graph_crossgraph.py` (`AgentCanary.cross_redeploy`: every `agent × canary`
  walk treats a redeploy under a running canary as a refusal self-loop for the creator and the
  administrator; `AgentWorld.FIXED` keeps the edge's neighbourhood).

### 1.2 A deploy job blocks the Runtime canary's publishes (N17-obs13)

**Rule (ruling, lead 2026-10-19; fixed in `cn/r17-c`).** The canary actions that publish or
change the champion agent's LIVE version are refused with the same envelope while a deploy
job owns the agent:

| canary action | agent has a `queued` / `running` deploy job |
|---|---|
| `complete` (promote — production = candidate, the agent row's spec / version change, a snapshot row) | `409 agent.deploy_in_progress {job_id, stage, job_status}` — nothing written: the agent row keeps its spec and version, the canary stays `running` at its stage, no snapshot, the A/B split keeps routing |
| `rollback` (a roll-forward mint of the current spec; the row's version changes) | the same 409, nothing written |
| `setup`, `traffic`, `verdict`, `advance`, `cleanup` | proceed — they act on the split (the candidate endpoint, the A/B row, the replay, the verdict), not on the agent's live version |

* Order on `POST /api/runtime-canaries/{id}/action`: visibility 404 → `canary.forbidden` →
  system-subject → `assistant.purge_*` → `canary.action_in_flight` → `canary.stage_not_ready`
  → `cleanup` only: `canary.cleanup_requires_decision` (N17-obs452, evaluation-engine.md §30.9)
  → **`agent.deploy_in_progress`** → `canary.stale_base` (§1.2 belt) → the action's own gates
  (`canary.verdict_blocked` / `override_required`, `dataset_required`). Because the stage gate
  answers first, `complete` reaches the deploy guard only at the final 1/99 stage with a verdict
  recorded, while `rollback` (allowed at any stage) reaches it from any running canary —
  confirmed live (deploy-ops-13, t1). The route answers the 409 as a plain response
  (never a 202 whose thread fails), and `act_complete` / `act_rollback` re-check under their
  own session before the A/B stop — the belt for a job that landed in between.
* Why: the promote wrote the candidate spec and version onto a `deploying` row and recorded a
  "canary promoted" snapshot; the update job then finished and `_finish` stamped the row's
  spec — the canary's — into the REDEPLOY's snapshot (seq N read the candidate prompt instead
  of the job's spec): a lost update and a version ledger that lied about what seq N deployed
  (found by the r17-b cross-graph walk `agent_x_canary`, register row N17-obs13).
* Both publishes record a snapshot row (N17-obs2, A-live deploy-ops-13 T77): the promote's
  `canary promoted` row existed; the rollback's roll-forward mint changed the live version
  (`agent.version = v_restored`) WITHOUT a row, so the version ledger had no entry for the
  restored version — `act_rollback` now records `note: "canary rolled back"` with
  `aws_version = v_restored` holding the spec it re-published (the current one), in the same
  commit as the version. Test: `tests/optimization/test_runtime_canaries.py::
  test_rollback_rolls_forward_current_spec`.
* The second belt (architecture.md "Spec snapshots"): a job carries **the spec its own stages
  packaged** (`Job.payload.packaged_spec` — the row's spec when the job started, plus every
  rewrite a stage of THIS job made: the row is compared right before and right after each
  stage call and the changed keys are applied onto the carried spec, so BYOC `generate`'s
  provenance and a harness deploy's skill pins are carried, while a write that lands between
  two stages is not — even when a later stage rewrites other keys), and `_finish` stamps THAT onto the
  job's snapshot, never the agent row's current spec; a job resumed across the upgrade seeds
  the key from the row as it resumes. So even a writer the guard does not know cannot make a
  snapshot record a spec it did not deploy.
* The rule lives once: `deployer.pipeline.refuse_deploy_in_progress(db, agent_id, then=…)`
  (the lookup `in_flight_deploy_job` of §1) — called by `routers/agents.teardown_agent_row`
  (§1) and by `optimization/canary_service.refuse_publish_during_deploy` (`PUBLISH_ACTIONS` =
  `complete`, `rollback`) from the action route and from the two actions.
* Console: the canary detail page's action toast shows `apiErrors.agent.deploy_in_progress`
  (en / zh-CN), which names the canary promote / rollback as a blocked publish.
* Tests: `tests/test_canary_deploy_guard.py` (409 + detail for both publishes with the ledger
  digest unchanged; the five split actions and every terminal job status pass; `act_complete`
  / `act_rollback` refuse before `_stop_ab_test`), `tests/test_pipeline.py::
  test_finish_stamps_the_spec_the_job_packaged_not_a_concurrent_writers` (+ the legacy-job
  fallback), `tests/graph/test_graph_crossgraph.py::test_N17_obs13_*` (the route: 409 while
  the job is live, 202 once it is terminal, for `complete` and `rollback`; every `agent ×
  canary` walk treats the two publishes under a live job as refusal self-loops).

* **The stale-base belt (N17-obs16, fixed in `cn/r17-d`).** `setup` records the champion's
  ledger version (`artifacts.setup.agent_version`) next to the runtime's `v_current`; `complete`
  and `rollback` answer `409 canary.stale_base {setup_version, current_version, action}` once
  the agent's version is no longer that one — a re-publish finished in between (refused on
  the route since this fix, but a job already live at setup, an out-of-band publish or a
  pre-guard canary can still leave it). Checked on the action route right after
  `agent.deploy_in_progress` (a plain 409) and inside `act_complete` / `act_rollback` before the
  A/B stop; nothing written, the canary stays `running` so `cleanup` settles it and a new
  canary starts on the current version. A canary whose setup predates the key is never stale
  by this rule. The rule lives in `canary_service.stale_base` / `refuse_stale_base`. Tests:
  `tests/test_canary_stale_base.py`, the cross-graph `publish_on_stale_base` steps of
  `test_N17_obs13_*`.

### 1.3 An ACTIVE evaluation run owns its agent until it is terminal (N17-obs18)

**Rule (ruling, lead 2026-10-20; fixed in `cn/r17-f`).** `DELETE /api/agents/{id}` is refused
with the canary guard's envelope — `409 agent.in_use {canaries: [...], runs: [{id, name,
status}]}` — while an evaluation run naming the agent is ACTIVE (`evaluation.service
.ACTIVE_STATUSES`: `queued` / `invoking` / `waiting` / `evaluating`, the same definition that
blocks dataset deletes in §22 of evaluation-engine.md); members and administrators alike, no
`force`; `completed` / `failed` / `stopped` runs never block. The operator stops the run
(`POST /api/eval/runs/{id}/stop`) or waits for it, then deletes. `runs` lists every blocking run
oldest first; `canaries` and `runs` are both present on every refusal (either may be empty).

* Why: the delete answered 200 (Runtime torn down) while a run on the agent was `queued`; the
  run kept naming the deleted agent, `stop` still answered 202, and at dispatch the worker —
  which never re-read the agent — completed it against the torn-down Runtime, recording a
  scorecard version for a deleted agent (r17-e cross walk `agent_x_run`, the dangling oracle
  "ACTIVE run naming a deleted agent").
* Order inside `teardown_agent_row`: preset refusal → `agent.deploy_in_progress` →
  `agent.in_use` (canaries and runs in one answer) → the AWS teardown. The rule lives once, in
  `services/canary_references.refuse_agent_in_use(kinds=…)` (`blocking_runs` next to
  `blocking_canaries`); the system-preset uninstall shares it. **A re-publish asks for the
  canaries only** (`kinds=("canaries",)`): a queued run is designed to evaluate the version live
  at dispatch (evaluation-engine.md §22, "a deploy that finished while the run waited in the
  queue is what runs"), so a redeploy under an active run is a 202 as before.
* **The worker belt.** `evaluation.service.execute_run` re-reads the agent at dispatch
  (`_check_agent_present`): a row that is `deleted` or gone fails the run with error
  `agent.not_found: …` BEFORE the status flips to `invoking` and before any `agent_snapshot_seq`
  stamp — no job is submitted, no scorecard / version row is ever recorded for a deleted agent.
  Log-source runs have no agent and pass.
* Console: the agent delete dialog lists the blocking runs under the canaries
  (`v2.agents.deleteBlockedByRuns`, each linked to `/v2/eval/tasks?view=detail&id=<run>` where
  stop lives); `apiErrors.agent.in_use` stays the toast copy.
* Tests: `tests/test_agent_delete_run_guard.py` (409 + `runs` detail for every ACTIVE status
  with the ledger digest unchanged; both referrer kinds listed together; terminal runs 200;
  409 → stop → 200; a redeploy is not blocked by a run; the worker fails a run whose agent was
  deleted / whose row is gone before dispatch, no `invoking`, no version stamp),
  `tests/graph/test_graph_crossgraph.py` (`AgentRun.cross_delete`: every `agent × run` walk
  treats the delete under an ACTIVE run as a refusal self-loop for creator and administrator;
  `test_N17_obs18_agent_delete_under_a_queued_run`: 409 → `stop_queued` → 200 with the dangling
  oracle clean; `AgentWorld.FIXED` keeps the delete edges in the neighbourhood set),
  `frontend/src/v2/pages/Agents.test.tsx`.

## 2. The pipeline re-reads the agent row before and after every stage

Belt and braces for a client that never saw the 409 (older build, a race between the guard
and the stage boundary, a row marked deleted by any other path):

* Before each stage **and** after each stage (and once more before `_finish`), the job
  reads the row's `status` with a fresh column `SELECT` (`pipeline.agent_gone` — never the
  session's identity map). Missing row or `status = deleted` → the job stops.
* The stop is honest: job `status = cancelled`, `error` = `pipeline.CANCEL_REASON`
  ("cancelled: the agent was deleted while this deploy job was running — …"), one
  `warning` log line on the stage that did not run (or `job` when every stage had
  finished), the Deployment `status = cancelled` with `ended_at`, and that stage marked
  `cancelled` (`detail: "stopped — the agent was deleted"`). The stages that finished keep
  `succeeded` / `skipped`.
* If a stage **raised** because the row went away under it (its refresh / re-read failed,
  a write landed on a vanished row), the exception is logged at `warning`, the stage is
  marked `cancelled` and the job ends `cancelled`, not `failed` — the delete is the truth,
  the exception its symptom.
* The agent row, when it still exists, is left exactly as the delete put it (`deleted`).
  **The finalizers refuse too** (live fact, t1 13:30Z: the row the member deleted at
  13:29Z was back at 13:30:57Z as `active` with `registry_record_id` set and one deployed
  snapshot — the finalizer had written it back; the orphan is a *resurrected row* pointing
  at resources the console later fails to delete, the T46 shape). `_finish` (success and
  failure paths) and `_fail_job` re-read the row in their own transaction and, when it is
  gone / deleted, write **nothing** onto it — no `active`, no `failed`, no `error`, no
  `stamp_deployed` on the spec snapshot — and close the Job / Deployment `cancelled`
  (`payload.cancelled.finalizer_refused: true`, the would-be error kept as a `warning`
  line). Nothing in the pipeline re-INSERTs a physically missing row either: every write
  is an UPDATE through `db.get(...)` on an existing row, so a row gone by DELETE stays gone.
  (`_fail_job` alone keeps its historical `failed` for a row that is physically *missing*
  — the half-deleted-ledger contract; there is no row to resurrect in that case.)
* The `register` stage guards itself as well (`registration.register_stage`): it re-reads
  the row in its own session and returns `skipped` ("agent deleted during the deploy — no
  registry record created") for a deleted or missing row — no `register_agent_record`
  call, no record id written; the loop then cancels the job.
* `cancelled` is terminal: `resume_pending_jobs` does not adopt it, the route no longer
  sees a live job, the console wizards show `✕` for the stage. Additive to the API: the
  Job / Deployment / stage `status` enums gain `cancelled` (`frontend/src/lib/api.ts`).

### 2.1 What the cancelled job tears down

Recorded on the job: `payload.cancelled = {stage, reason: "agent_deleted", teardown:
[TeardownStep…]}` plus `teardown`-stage log lines.

| job mode | stages that started | torn down by the job |
|---|---|---|
| `create` | none past `generate` | nothing — no AWS resource exists; log says so |
| `create` | `package` / `provision` started | `teardown.cleanup_residue` (execution role + inline policies, S3 artifact prefix, memory snapshots / records, registry record, runtime log groups — best-effort, idempotent, each step reported; the two S3 prefix sweeps go through the shared reporting deleter `services/s3_sweep.py` since N17-obs190 `cn/r17-u`: a key S3 refuses inside a 200 `DeleteObjects` answer makes the step `failed` with `reason` = the sweep summary — `1 of 4 objects refused (AccessDenied)`, counts and codes only — instead of being counted as deleted) |
| `create` | `deploy` started and the row carries `resource_id` | the Runtime first (`routers/agents.delete_agent_cloud_resource`, idempotent on NotFound), then the residue |
| `update` | any but `register` | nothing — the new version rode the agent's existing Runtime; role / artifacts / record belong to the agent's own delete (which already ran) — log says so |
| `update` | `register` started | only `teardown._step_registry_record` — the A2A record a re-register may have recreated after the delete route removed the agent's |

Resources are named from a transient snapshot of the row taken after every finished stage
(`pipeline._agent_snapshot`), so the teardown still knows the names when the row itself is
gone. A snapshot that predates a Runtime the vanished stage created cannot name it — the
operator's `scripts/teardown.py` tagged-orphan sweep remains the backstop for that corner
(the product's delete marks the row, it never removes it, so in practice the row is there).

### 2.2 Tests

`backend/tests/test_deploy_cancel.py`: 409 at every stage with the row untouched (route
shaped, and end-to-end with the DELETE issued from inside the running stage — the job
runs on to `succeeded`); a `queued` job is in flight until terminal; the row marked
`deleted` between stages → `cancelled`, deploy / register never run, residue swept, row
stays `deleted`; the row vanishing after the deploy stage → Runtime + residue; the row
vanishing inside a stage → `cancelled` not `failed`; a delete during `register` never
resurrects the row; an update-mode job tears down nothing of its own (but a recreated
registry record); nothing to tear down before `package`; `cancelled` is terminal for the
route and the resume. The finalizer scenario: delete during `deploy`, job let run →
register never runs, no record, snapshot unstamped, row 404 / `deleted`, Runtime torn
down; `_finish` (both paths) and `_fail_job` called directly on a deleted row write
nothing onto it; a physically missing row never comes back by INSERT. The
restart-sweep scenarios (`tests/faults/test_restart_sweeps.py`) are unchanged.

## 3. Reserved names: a preset's whole family

**Rule.** For every system preset in `system_agents/presets.PRESETS`, these names are
reserved for ordinary agents (`presets.reserved_for(name)`):

| | example (eval worker, env `t1`) |
|---|---|
| the preset's reserved name (`SystemPreset.name`, env-suffixed where the preset is) | `launchpad-eval-worker-t1` |
| its base name (`SystemPreset.base_name`, or `name` when empty) | `launchpad-eval-worker` |
| every `<base>-<suffix>` | `launchpad-eval-worker-prod`, `launchpad-eval-worker-2` |

The architect's base is its name (`aws-agent-solution-architect`), so
`aws-agent-solution-architect-v2` is reserved too. A near-miss is ordinary
(`launchpad-eval-workers`, `my-eval-worker`). Derived from the presets table — never a
literal list; a new preset is covered by declaring `base_name`.

**Refusal.** `422 agent.name_reserved {name, reserved_for: <preset key>}` from
`presets.refuse_reserved_name`, at every ordinary-agent creation boundary before any row,
claim or AWS call: `POST /api/agents` and the architect proposal approval (its proposal
validation lists the same as an error). Previously `409` with `{name}` only, and only the
suffixed name — a member could create `launchpad-eval-worker` whose Runtime / role / S3
names collided with the preset family's. Discovery import (`runtime_discovery`) keeps
skipping the whole family (it suffixes the imported name with the resource id).

Why 422 and not 409: the name is invalid for an ordinary agent in every state of the
ledger — a validation failure of the request, not a conflict with an existing row (`409
agent.name_exists` stays for the latter). The check is at the request boundary, not inside
`AgentSpec`, because the presets build their own `AgentSpec` with exactly these names.

Locale: `apiErrors.agent.name_reserved` (en / zh-CN) names the family rule;
`apiErrors.agent.deploy_in_progress` now covers delete as well as re-publish;
`apiErrors.agent.in_use` is the canary guard of §1.1.

## 4. Redeploy edges — the 202, the `deploying` flip, re-publish from `failed`, keep-live, name release (N16-obs110)

The transitions the r16-o lifecycle graph (`tests/graph/graphs/agent.yaml`) walks and no
contract sentence named until round 16 (DOC ruling: the docs follow the code). The pipeline
itself is `docs/architecture.md` "Job/event model".

* **4.1 Redeploy answers 202 and flips the row to `deploying`.** `POST /api/agents/{id}/redeploy`
  with an edited spec (name and method immutable — `400 agent.redeploy_immutable`) answers
  `202 {agent, job_id, deployment_id}` — the same shape as `POST /api/agents` — after the
  creator-or-admin gate, the preset / discovered-runtime / purge refusals and the method,
  pin, tool-auth, memory and policy validations, all before any AWS call. The row's `spec`
  is replaced, `status` becomes `deploying`, `error` is cleared, and an update-mode job is
  queued (`create_deployment(mode="update")`). While the job is live, chat answers
  `409 agent.not_active` (N16-obs74 ruling), redeploy and the inbound-auth pin answer
  `409 agent.deploy_in_progress`, and delete answers the same code (§1) — the previous
  version keeps serving on the Runtime's DEFAULT endpoint throughout.
* **4.2 A `failed` agent can be re-published.** The same route on an agent whose status is
  `failed` (a first deploy that never produced a live version, or a later failure inside the
  `deploy` stage) answers the same 202 and runs the update-mode job; its `deploy` stage falls
  back to Create when the row has no Runtime yet (`redeploying_cold` in the graph). A cold
  re-publish that fails marks the agent `failed` again — nothing was serving, so nothing is
  kept. The same holds for a re-publish of an agent whose `failed` came from a failure inside
  the `deploy` stage (§4.3): that agent has **no verified live version** — the older
  succeeded deployment proves nothing any more — so a re-publish that fails before its own
  `deploy` stage leaves the agent `failed`; only a succeeded publish makes it `active`
  again (N17-obs100).
* **4.3 A failed update keeps the live version.** When the agent already serves a version and
  the failing stage left it untouched (the `deploy` stage is still `pending` / `skipped`, or
  already `succeeded` — a `register` failure), the `Job` and the `Deployment` are `failed`
  but the `Agent` returns to `active`, keeps serving the previous version, and
  `GET /api/agents/{id}` / the list carry `last_deploy_failed: true` and `last_deploy_error`
  (the job's error; both `false` / `null` once a later publish succeeds). A failure inside
  the `deploy` stage itself (UpdateAgentRuntime may have been accepted) marks the agent
  `failed` — and, until a later publish succeeds, the agent has no verified live version:
  the keep-live rule reads the deployment history newest-first and stops at such a
  deploy-stage failure, so the next re-publish failing before `deploy` keeps `failed`
  (N17-obs100). Code: `pipeline._keeps_live_version`, `_has_verified_live_version`,
  `_fail_job`, `_finish`, the `LIVE_VERSION_KEPT` job-log line. Console: the detail shows the REDEPLOY FAILED tag with
  the reason and Chat stays available (`v2.agents.lastDeployFailed*`).
* **4.4 A delete releases the name.** `DELETE /api/agents/{id}` (200) marks the row `deleted`
  and releases its name claim (`agent_names.release_agent_name`) in the same commit, so
  `POST /api/agents` with the same name answers 202 and creates a new row (new id); the old
  row stays readable as history where the contract says so (snapshots, scorecards).
  `409 agent.name_exists` is answered only for a conflict with a row that is not `deleted`
  (§3 keeps `422 agent.name_reserved` for a preset family's names). Under `isolated`
  (users-and-ownership.md §7) a duplicate-name 409 whose holder is HIDDEN from the caller
  carries `detail.agent_id: null`: the name invariant is workspace-wide, so the 409 itself is
  unavoidable — only the holder's id is masked (`agent_names.name_exists_error`; the round-4
  refusal matrix's `name_leak` cell, rev-r17 on round 5).

Tests: `tests/graph/test_graph_agent.py` edges `redeploy`, `redeploy_from_failed`,
`cold_redeploy_fails`, `redeploy_fails_keeps_live`, `create_after_delete_same_name`;
`tests/graph/test_graph_fault_walks_agent.py::test_N17_obs100_*` (deploy-stage failure →
re-publish fails before `deploy` → still `failed`); `tests/test_pipeline.py`
(`test_republish_failure_after_a_deploy_stage_failure_keeps_the_agent_failed`,
`test_a_succeeded_publish_after_a_deploy_stage_failure_re_verifies_the_live_version`);
`tests/test_agents_api.py` (`test_redeploy_conflicts_while_deploying`, the immutable fields);
`tests/test_deploy_cancel.py` / `tests/test_agent_teardown.py` for the delete side.
