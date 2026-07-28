# Experiment Stepwise Actions (backend + frontend)

> Configuration A/B and Runtime Canary are separate, user-triggered workflows
> under `/evaluation?view=experiment`. Both use server-side orchestration,
> per-record progress, and artifact-driven UI, but they have different tables,
> APIs, state machines, and cleanup ownership.

## Scenario: driving one configuration A/B stage

### 1. Scope / Trigger

- Cross-layer API contract: `POST /api/experiments/{id}/action` owns only
  configuration recommendation, bundle A/B, promotion, and cleanup.
- DB schema: `experiments.running_action` (VARCHAR 24, nullable),
  `experiments.progress` (TEXT, nullable) — added via the additive `_migrate`
  in `app/core/db.py` (no Alembic).
- Anything editing `backend/app/optimization/service.py`,
  `backend/app/optimization/routers.py`, or
  `frontend/src/pages/EvaluationExperiment.tsx` must respect this contract.

### 2. Signatures

```text
POST /api/experiments                     {agent_id}            → 201 experiment
GET  /api/experiments                                            → {experiments: [...]}
GET  /api/experiments/{id}                                       → experiment
POST /api/experiments/{id}/action         ActionRequest          → 200 | 202 {"experiment": ...}
```

```python
class ActionRequest(BaseModel):
    action: str  # recommend|accept|bundles|gateway|abtest|traffic|verdict
                 # |promote|cleanup
    accepted_prompt: str | None                        # accept
    accepted_tool_descriptions: dict[str, str] | None  # accept
    dataset_id: str | None                             # traffic
    online_evaluators: list[str] | None                # gateway, 1..ONLINE_EVAL_MAX
```

Service layer (`app/optimization/service.py`):

```python
run_action(exp_id, action, fn: Callable[[Progress], Any])  # daemon thread runner
act_recommend/act_gateway/act_abtest/act_traffic/act_verdict/act_promote/act_cleanup(exp_id, ..., progress)
action_accept(exp, prompt, tool_descriptions)  # sync
action_bundles(exp)  # sync
stage_not_ready_reason(exp, action) -> str | None
resolve_traffic_prompts(dataset) -> list[str]
normalize_online_evaluators(ids) -> list[str]   # None/empty → ONLINE_EVAL_DEFAULT
create_bundle_idempotent(control, **kwargs)    # conflict-adopt via ListConfigurationBundles
clear_stale_running_actions() -> list[str]     # startup sweep (main.py resume block)
assert_shared_gateway_available(own_test_name=None) -> None
```

### 3. Contracts

- **Async actions** `{recommend, gateway, abtest, traffic, verdict, promote,
  cleanup}` → `202`, work runs on a daemon thread; the thread persists its
  artifact + stage on success. **Sync actions** `{accept, bundles}` → `200`
  inline. Both response bodies are `{"experiment": <row>}`
  (NOT the pre-refactor `{"result": ...}`).
- Experiment serialization adds `running_action: str|null` and
  `progress: str|null`. The UI polls `GET /api/experiments` every 8s, and
  2.5s while `running_action` is set.
- `stage` is a **furthest-completed marker** in
  `recommend|bundles|gateway|abtest|traffic|verdict|promote|cleanup`;
  card visibility in the UI is **artifact-driven**, not stage-driven, so old
  auto-pipeline rows render unchanged (A8 backward compat).
- New configuration records own `recommend/bundles/gateway/abtest/traffic/
  verdict/promote/cleanup`. A stored legacy `canary` artifact remains readable
  and cleanable, but is never created or ramped by this API.
- Other artifact keys include:
  - `agent_meta` — runtime snapshot written at create
    (`{id,name,arn,resource_id,runtime_name,system_prompt}`); rebuilt lazily
    by `_agent_meta()` for old rows.
  - `recommend.accepted_prompt` / `recommend.accepted_tool_descriptions` —
    written by `accept`; `bundles` uses accepted → recommended fallback.
  - `traffic.dataset_id` / `traffic.dataset_name` — when a dataset was
    replayed instead of the built-in `TRAFFIC_PROMPTS*2`.
  - `gateway.online_evaluators` — the evaluator ids the online evaluation
    config was created with (records the *request*: a name-conflict adoption
    keeps the pre-existing config's own set, which is never rewritten).
- **Online evaluators are operator-chosen at `gateway`.**
  `ONLINE_EVAL_DEFAULT = ("Builtin.GoalSuccessRate", "Builtin.Helpfulness")`
  when the field is absent, so pre-existing flows are byte-identical.
  `normalize_online_evaluators` dedupes (first occurrence wins, order kept),
  drops blanks, and is called **in the router before dispatch** so a bad id
  costs no AWS round-trip. Accepts the 13 `ac.ALL_BUILTIN_EVALUATORS` plus any
  custom evaluator id; rejects `ac.TRAJECTORY_EVALUATORS` (they score against
  dataset ground truth, absent from live traces) and unknown `Builtin.*`.
  `ONLINE_EVAL_MAX = 10` mirrors the `CreateOnlineEvaluationConfig` cap.
  Custom ids are NOT verified against `ListEvaluators` — the UI only offers ids
  from `/api/eval/evaluators`, and AWS rejects an unknown one at create time.
- `POST /api/experiments` performs **no AWS-mutating work** (A1) — only the
  row + `agent_meta` (one `get_agent_runtime` read).
- The Gateway is the shared `EXP_GATEWAY_NAME`. `gateway` and `abtest` run a
  read-only active-test preflight before mutation. Any foreign test on the same
  Gateway whose `executionStatus != "STOPPED"` returns
  `409 experiment.gateway_busy`; an exact record-owned test name is adoptable
  on idempotent retry.
- Cleanup deletes only configuration-owned bundles, targets, online evaluator
  configs, and A/B tests. It passes `delete_gateway=False`; the shared Gateway
  is never deleted.
- `canary` and `ramp` are compatibility-only request values. They return
  `410 experiment.action_moved` and point callers to `/api/runtime-canaries`;
  they never dispatch asynchronous work.
- Failure: the runner stores `error = "<action>: <Type>: <msg>"` (action
  prefix is a UI contract — the page pins the retry button by
  `error.startsWith(action + ": ")`), clears `running_action`, KEEPS the
  stage. Retry = re-POST the same action (AWS creates are idempotent,
  conflict-adopt by name — bundles via `create_bundle_idempotent`).
- `status="failed"` is LEGACY-ONLY (old auto-pipeline rows): stepwise
  failures keep `status="running"` with inline retry. A permanently stuck
  experiment therefore holds the single-running-experiment slot — the
  escape hatch is `cleanup` (→ `cleaned`). Sync-action failures
  (accept/bundles) surface as the HTTP error → UI toast; they
  do NOT write `exp.error` (only the async runner does).

### 4. Validation & Error Matrix

| Condition | Error |
|---|---|
| unknown action | 422 (pydantic pattern) |
| `running_action` non-null | 409 `experiment.action_in_flight` |
| prerequisite artifact missing (see table below) | 409 `experiment.stage_not_ready` |
| accept with no prompt anywhere | 400 `experiment.accept_invalid` |
| traffic dataset not found | 404 `dataset.not_found` |
| traffic dataset kind `simulated` / no usable prompts | 422 `experiment.dataset_unsupported` |
| gateway `online_evaluators` empty list or > `ONLINE_EVAL_MAX` items | 422 (pydantic bounds) |
| gateway `online_evaluators` holds a trajectory matcher or unknown `Builtin.*` | 400 `experiment.evaluator_unsupported`, with `evaluator` |
| foreign active A/B test on shared Gateway | 409 `experiment.gateway_busy`, with `gateway_arn` and `active_tests` |
| legacy `canary` or `ramp` action | 410 `experiment.action_moved`, with `runtime_canaries_path` |
| second concurrent experiment (create) | 409 `experiment.already_running` |

Prerequisites (`stage_not_ready_reason`): accept←recommend artifact;
bundles←`recommend.accepted_prompt` OR existing bundles artifact (old-row
retry); gateway←bundles; abtest←gateway; traffic←abtest; verdict←traffic;
promote←verdict; recommend/cleanup←none.

### 5. Good/Base/Bad Cases

- **Good**: create → recommend(202, poll) → accept(edited prompt) → bundles →
  gateway → abtest → traffic(dataset_id) → verdict → promote → cleanup. The
  promoted Agent can be handed off into a separate Runtime Canary record.
- **Base**: old auto-pipeline row (all artifacts, no `accepted_*`, NULL new
  columns, possible `canary` artifact) — GET serializes, the legacy Canary
  renders read-only, cleanup still owns its old resources, and re-running
  `bundles` is allowed.
- **Bad**: POST `gateway` before `bundles` → 409 stage_not_ready; POST while
  recommend runs → 409 action_in_flight; active Canary test on the shared
  Gateway → 409 gateway_busy before configuration resources are mutated.

### 6. Tests Required

`backend/tests/optimization/test_stepwise_actions.py`:
- guard matrix (every action w/o prereq → 409; in-flight → 409)
- accept persists edit + keeps `recommended_prompt`; empty accept → 400
- `act_recommend` re-run preserves prior `accepted_*`
- bundles consumes accepted prompt/tool-descs (capture via monkeypatched
  `stage_bundles`)
- `resolve_traffic_prompts`: legacy prompt, predefined first-turn (incl.
  dict `input` via `scenario_prompts` reuse), simulated → ValueError
- runner lifecycle: failure keeps stage + `"<action>: "` error prefix;
  success clears error/progress (monkeypatch `svc._spawn` to run inline)
- `clear_stale_running_actions` clears only stuck rows, writes retryable error
- create defers all stage work (`artifacts == {agent_meta}`)
- old `canary`/`ramp` actions return `experiment.action_moved`
- Gateway conflict is detected before config mutation; exact own test is
  adoptable; cleanup never calls shared-Gateway deletion
- online evaluators: absent → `ONLINE_EVAL_DEFAULT`; explicit list reaches
  `act_gateway` deduped/in order (custom ids pass through); trajectory matcher
  and unknown `Builtin.*` → 400 before dispatch; `[]` / 11 ids → 422;
  `stage_gateway` records `online_evaluators` on the artifact and forwards
  `evaluators=[{"evaluatorId": …}]` to `create_online_evaluation_config`

Frontend has NO test runner — verify via the fetch-stub browser evidence
flow with synthetic experiment states plus a real handoff URL.

### 7. Wrong vs Correct

#### Wrong
```python
# Orchestrate in the frontend / auto-run stages at create
threading.Thread(target=run_experiment_loop, ...).start()   # removed
# Continue target canary work on the configuration record
POST /api/experiments/{id}/action {"action": "canary"}
```

#### Correct
```python
# Configuration A/B ends at promotion/cleanup. Canary gets a separate row that
# rolls a candidate VERSION of the (one) promoted agent — see the canary scenario.
POST /api/runtime-canaries {
    "agent_id": promoted_agent_id,
    "candidate": {"system_prompt": "...", "tool_description_overrides": {}},
    "source_experiment_id": experiment_id,
}
```

## Design Decisions

### Server-side orchestration (vs agentxray's frontend chaining)

agentxray's Live console chains AWS calls in the browser and PUTs artifacts
back — closing the tab strands a stage. Launchpad keeps stage logic in
`service.py`; the browser only POSTs actions and polls. Progress lives on
the experiment ROW (`running_action`/`progress`), not in a button closure,
so a reload resumes the same spinner mid-action.

### Startup sweep for stale actions

Action threads are daemons — a backend restart (incl. `--reload`) kills them
without clearing `running_action`, which would 409 every retry forever.
`clear_stale_running_actions()` runs in the `resume_jobs` startup block of
`create_app` and converts the stuck flag into a retryable
`"<action>: interrupted by a backend restart — retry the action"` error.

### Traffic dataset replay

The traffic stage accepts any legacy/predefined `EvalDataset`; prompt
extraction reuses `app.evaluation.scenarios.scenario_prompts` so dict turn
inputs unwrap exactly as in eval replay. Simulated datasets are rejected —
they need an actor loop, not a fire-and-forget prompt replay.

> **Warning**: AgentCore permits only one active A/B test per Gateway.
> **Configuration A/B** uses the single shared `EXP_GATEWAY_NAME`, so its
> `gateway` / `abtest` stages run the `experiment.gateway_busy` preflight.
> **Runtime Canary** does NOT share it — each canary owns a dedicated
> `lp-canary-{id}` Gateway (conflict-adopt on retry), so canaries run
> concurrently across agents (bounded only by `canary.already_running`, one per
> agent). A Configuration A/B test and a Runtime Canary no longer contend.

## Scenario: production target-based canary (Runtime Canary)

> **Model 1**: a canary rolls out a CANDIDATE VERSION of ONE agent against its
> current version, splitting REAL production traffic on a DEDICATED per-canary
> Gateway, then either promotes (production serves the candidate) or rolls back.
> This SUPERSEDES the former experiment-only design (no more `experimental_only`,
> no shared-Gateway mutex, no two-agent compare). Its sibling `CONFIGURATION-BUNDLE
> A/B` still uses the shared `EXP_GATEWAY_NAME`.

### 1. Scope / Trigger

- Cross-layer: `POST /api/runtime-canaries` + `/{id}/action`, **the invoke hot
  path** (`app/services/invoke.py`), and the AgentCore Runtime named-endpoint +
  Gateway target-based A/B primitives.
- Files: `canary_service.py`, `canary_infra.py`, `canary_routers.py`,
  `app/services/invoke.py`, `agentcore/runtime.py` (endpoint wrappers +
  `invoke_runtime_text(qualifier=)`), `agentcore/gateway.py` (`sigv4_post`),
  `EvaluationRuntimeCanary.tsx`.
- DB: `RuntimeCanary` — `champion_agent_id`/`challenger_agent_id` BOTH hold the
  single agent id (Model 1); the candidate is `artifacts.edited_spec`.

### 2. Signatures

```text
POST /api/runtime-canaries
     {agent_id, candidate:{system_prompt?,tool_description_overrides?,code?},
      source_experiment_id?}                              -> 201 canary
POST /api/runtime-canaries/{id}/action
     {action, dataset_id?, allow_non_significant?}        -> 202 {"canary": canary}
```

```python
canary_capability(agent) -> {"eligible": bool, "reason": str|None, "reason_code": str|None}
active_canary_route(agent_id) -> None | dict   # invoke hot-path lookup (see §3)

# canary_infra (AWS building blocks; injected-client, no ledger writes)
current_version(control, runtime_id) -> str
mint_candidate_version(*, agent, edited_spec, control_client, ...) -> (v_current, v_candidate)
create_canary_gateway(*, control_client, canary_id, ...) -> {gateway_id,arn,url}  # conflict-adopt
ensure_endpoint_ready(control, *, runtime_id, endpoint_name, version, ...)
promote_stable_endpoint(*, control_client, runtime_id, stable_name, version, ...)
delete_endpoint_quiet / delete_canary_gateway
endpoint_log_group(resource_id, endpoint) / endpoint_service_name(runtime_name, endpoint)

# agentcore/runtime.py
create/update/get/delete_runtime_endpoint + wait_endpoint_ready
invoke_runtime_text(client, arn, prompt, ..., qualifier: str|None = None)
```

### 3. Contracts

- **Model 1 / candidate mint.** Create takes ONE `agent_id` + a `candidate`
  edit (≥1 of `system_prompt`/`tool_description_overrides`/`code`), resolves it
  onto the agent's current spec (mirrors `act_promote`) and stores it as
  `artifacts.edited_spec`. `act_setup` mints the candidate as a new immutable
  version via `mint_candidate_version` (reuses the deploy build blocks +
  `UpdateAgentRuntime`; reads `v_candidate` from the response; **never writes the
  ledger Agent row**). `container` is not yet supported (mint raises; capability
  gates it, `reason_code="container-followup"`).
- **DEFAULT is the single source of production truth.** `UpdateAgentRuntime`
  auto-rolls DEFAULT to the candidate, so `act_setup` MUST, in order:
  read `v_current` → create the per-canary Gateway → create the STABLE named
  endpoint pinned to `v_current` → **persist a PARTIAL `setup`
  `{runtime_id, stable_endpoint, v_current, gateway_*}` BEFORE the mint** →
  mint the candidate → create the TREATMENT endpoint (candidate) → create the
  two `http-runtime` targets (`agentcoreRuntime.qualifier=<endpoint>`) + per-
  variant online-eval → create the 90/10 A/B test → finalize `setup`
  (`ab_test_id`, `champion`/`challenger` TARGETS, `v_candidate`,
  `treatment_endpoint`, `ramp_stage`, `weights`). Persisting the stable endpoint
  before the mint is load-bearing: it keeps production on `v_current` the instant
  DEFAULT rolls.
- **invoke hot path** (`invoke_agent_text` → `active_canary_route(agent.id)`):
  one indexed `SELECT` (no AWS). Returns `None` (→ unchanged direct-ARN/DEFAULT
  path), or a **provisioning** route `{runtime_id,arn,stable_endpoint,v_current}`
  (stable endpoint stood up, gateway A/B not live → invoke serves `v_current`
  via `invoke_runtime_text(qualifier=stable_endpoint)`), or a **live-gateway**
  route (adds `gateway_url`+`control_target` once `ab_test_id` exists → SigV4
  POST `{gateway_url}/{control_target}/invocations` with a sticky ≥33-char
  `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`). Every setup key is read with
  `.get()`. The gateway fail-safe is **CONTROL-SAFE**: any error/non-200 falls
  back to the stable endpoint (`v_current`), NEVER DEFAULT (the untested
  candidate). The harness / a2a / no-canary paths are byte-identical.
- **promote / rollback.** `act_complete` (promote): DEFAULT already = candidate,
  so just `_stop_ab_test` + update the ledger (`agent.spec=edited_spec`,
  `agent.version=v_candidate`) — no endpoint repoint. `act_rollback`
  (roll-forward): `_stop_ab_test` (only if `ab_test_id` present) + re-deploy the
  agent's CURRENT unchanged spec via `mint_candidate_version` so DEFAULT rolls
  back to `v_current` behavior. `rollback` is allowed for ANY running canary
  (safety valve) and tolerates a partial setup.
- **Dedicated per-canary Gateway.** `create_canary_gateway` (name
  `lp-canary-{id}`, `AWS_IAM`) is conflict-adopt on retry. There is NO shared-
  Gateway mutex for canaries (only Configuration A/B keeps `EXP_GATEWAY_NAME`),
  so canaries run concurrently across agents — but only ONE running canary per
  agent (`409 canary.already_running`). `act_cleanup` deletes the dedicated
  gateway + BOTH named endpoints + targets + online-eval + A/B test
  (`delete_gateway=False` passed to `ac.cleanup_resources`; the gateway is
  deleted explicitly via `canary_infra.delete_canary_gateway`). It tolerates a
  partial setup. **Teardown gotcha (verified live):** AgentCore rejects
  `delete_gateway` / `delete_online_evaluation_config` while the async-deleting
  A/B test is still propagating — for MINUTES *after* `list_ab_tests` /
  `list_gateway_targets` already report it gone, so draining the lists is not a
  reliable signal. Both `cleanup_resources` and `delete_canary_gateway` therefore
  **retry the delete until it is accepted** (≤300s) and treat NotFound as success;
  a single-shot delete leaks the dedicated gateway.
- **Verified AWS shapes.** Named-endpoint content-log group =
  `/aws/bedrock-agentcore/runtimes/{resource_id}-{endpoint}` (created at
  endpoint-create). `create_agent_runtime_endpoint` pins via `agentRuntimeVersion`;
  get-detail exposes `liveVersion`; invoke selects an endpoint via `qualifier`
  (session id min length 33). One RUNNING A/B test per gateway.
- Verdict/ramp policy is UNCHANGED from the prior design: `baseline_n` fresh-
  evidence gate; `treatment-wins` advances; `tie`/non-significant needs
  `allow_non_significant`; `control-wins`/`insufficient-*` cannot be overridden.

### 4. Validation & Error Matrix

| Condition | Error |
|---|---|
| Canary not found | 404 `canary.not_found` |
| Agent missing or inactive | 400 `canary.agent_not_active` |
| Harness, A2A, container, missing Runtime ARN | 400 `canary.agent_unsupported`, with `canary_capability` (container → `reason_code=container-followup`) |
| candidate has no non-empty edit | 400 `canary.candidate_empty` |
| a running canary already exists for this agent | 409 `canary.already_running` |
| invalid/incomplete source experiment | 400 `canary.source_experiment_invalid` |
| source experiment Agent is not this agent | 400 `canary.source_champion_mismatch` |
| action already running | 409 `canary.action_in_flight` |
| setup/traffic/verdict/advance prerequisite missing | 409 `canary.stage_not_ready` (NOT rollback — always allowed) |
| simulated or unusable traffic dataset | 422 `canary.dataset_unsupported` |
| tie/non-significant without explicit override | 409 `canary.override_required` |
| control win or insufficient evidence | 409 `canary.verdict_blocked` |

### 5. Good/Base/Bad Cases

- **Good**: create `{agent_id, candidate:{system_prompt}}` → setup mints the
  candidate + stands up the dedicated gateway/endpoints/A-B (real traffic splits
  90/10) → fresh traffic/verdict at 90/10, 50/50, 1/99 → treatment wins →
  **promote** (DEFAULT already serves the candidate; ledger updated to it) →
  cleanup (dedicated gateway + both endpoints torn down; production on DEFAULT =
  candidate).
- **Base — partial-setup safety**: setup fails after the mint (e.g. gateway
  timeout); the partial `setup` (stable endpoint, no `ab_test_id`) makes
  `active_canary_route` return the **provisioning** form, so invoke serves
  `v_current` via the stable endpoint (NOT the untested candidate), and
  `rollback` is allowed and roll-forwards DEFAULT back to `v_current`.
- **Bad — rollback**: `rollback` stops the test (if any) and re-deploys the
  current spec; DEFAULT returns to `v_current` behavior; production never lands
  on the rejected candidate.

### 6. Tests Required

- API create: single-agent record (both columns = agent id; `edited_spec`
  stored); empty candidate → 400; inactive/container/harness/a2a ineligible;
  duplicate running canary → 409; source-experiment linkage; no ledger mutation
  at create.
- Setup: reorder — stable endpoint + partial setup persisted BEFORE the mint;
  two targets pin `qualifier=stable`/`treatment`; per-variant online-eval on the
  named-endpoint log groups; gateway conflict-adopt.
- invoke routing: no-canary → direct invoke, no qualifier (byte-identical);
  provisioning route → `invoke_runtime_text(qualifier=stable_endpoint)`; live
  route → gateway POST; gateway error/non-200 → fail-safe to stable endpoint.
- promote → ledger `spec==edited_spec`, `version==v_candidate`, no endpoint
  repoint. rollback → re-mints the CURRENT spec, `version` updated, allowed on
  partial setup. cleanup → both endpoints + dedicated gateway deleted.
- Ramp/verdict policy unchanged (treatment/tie/non-sig/control/insufficient +
  override). Browser: single-agent + candidate-edit form, live-traffic copy,
  disabled/ineligible reasons, EN/ZH.

### 7. Wrong vs Correct

#### Wrong

```python
# Mint the candidate first, write setup last, and route invoke off DEFAULT —
# a setup failure then strands production on the untested candidate.
mint_candidate_version(...)          # DEFAULT now = candidate
gw = create_canary_gateway(...)      # if this fails, no setup artifact exists
# ...and complete just flips A/B weights / records experimental_only=True
```

#### Correct

```python
v_current = current_version(control, runtime_id)
gw = create_canary_gateway(...)
ensure_endpoint_ready(stable → v_current)
_update(canary, artifact={"setup": partial})   # BEFORE the mint → invoke safe
_, v_candidate = mint_candidate_version(...)    # DEFAULT rolls; invoke → stable
ensure_endpoint_ready(treatment → v_candidate)
# targets(qualifier=stable/treatment) + per-variant eval + 90/10 A/B, then finalize
# promote: _stop_ab_test + ledger(spec=edited_spec, version=v_candidate)
# rollback: _stop_ab_test + re-mint the current spec (roll DEFAULT forward)
```

## Scenario: official AgentCore production promotion

### 1. Scope / Trigger

- `promote` means completing the AgentCore experiment lifecycle, not changing
  variant weights to `C=1/T1=99`.
- This contract covers experiment eligibility, configuration-bundle payloads,
  production defaults, A/B test shutdown, in-place runtime deployment, legacy
  1/99 records, and optional Runtime Canary handoff validation.

### 2. Signatures

```python
experiment_capability(agent: Agent) -> {
    "eligible": bool,
    "system_prompt": bool,
    "tool_descriptions": bool,
    "reason": str | None,
}
promotion_complete(artifacts: dict[str, Any]) -> bool
legacy_promotion(artifacts: dict[str, Any]) -> bool
act_promote(exp_id: str, progress: Progress) -> dict[str, Any]

create_deployment(
    db: Session,
    agent: Agent,
    mode: str = "create",
    *,
    skip_register: bool = False,
) -> tuple[Deployment, Job]
```

```python
class AgentSpec(BaseModel):
    tool_description_overrides: dict[str, str]  # max 100 entries
```

`GET /api/agents` and agent create/read responses expose
`experiment_capability`. `POST /api/experiments` enforces that same
backend-owned projection and returns `400` when `eligible=false`.

### 3. Contracts

- Eligible agents are active Launchpad-generated HTTP `zip_runtime` agents and
  converted harness runtimes with a recognized config-bundle graft. Harness,
  container, Studio, A2A, and arbitrary custom source are not assumed to
  consume bundles. A legacy converted graft supports system prompts but reports
  `tool_descriptions=false`; the v2 graft supports both.
- New AgentCore configuration bundles write tool descriptions as
  `configuration.tools.<name>.description`. Generated and converted runtimes
  continue reading legacy `configuration.tool_descriptions`, then let the
  documented `tools` shape win. Without a routed bundle, promoted
  `Agent.spec.system_prompt` and `tool_description_overrides` are defaults.
- Promotion is asynchronous (`202`). It stops the bundle A/B test and waits
  until `executionStatus == "STOPPED"`, writes `promotion_attempt`, applies the
  accepted prompt and tool descriptions to `Agent.spec`, upgrades a managed
  harness graft when needed, then creates an in-place deployment with
  `mode="update", skip_register=True`.
- `skip_register` skips only the `register` stage. Generate, package,
  provision, and deploy still run, preserving runtime identity through the
  normal update path.
- A successful `artifacts.promote` contains `ab_test_id`,
  `ab_test_status="STOPPED"`, `agent_id`, `deployment_id`, `job_id`,
  `agent_version`, `applied_system_prompt`, `applied_tool_descriptions`, and
  `completed_at`. Only after both deployment and job succeed may the service
  set `status="promoted"` and `stage="promote"`.
- A legacy artifact with `after_weights` but no completed deployment is
  projected as `status="ready"` without mutating its stored row. The UI labels
  it `LEGACY TRAFFIC SHIFT` and requires explicit `COMPLETE PROMOTION`.
  Completion preserves its former weights as `promote.prior_shift`.
- A Runtime Canary handoff with `source_experiment_id` requires
  `promote.deployment_id` and `promote.ab_test_status == "STOPPED"`; a legacy
  1/99 artifact is insufficient. Direct Runtime Canary creation has no
  configuration-experiment prerequisite.

### 4. Validation & Error Matrix

| Condition | Behavior |
|---|---|
| capability `eligible=false` | `POST /api/experiments` returns `400` with capability details |
| tool override name empty or over 200 chars | `AgentSpec` validation error |
| tool override description over 4000 chars | `AgentSpec` validation error |
| more than 100 tool overrides | `AgentSpec` validation error |
| A/B stop API fails or never reaches `STOPPED` | no deployment and no success artifact; async error is retryable |
| managed converted source lacks graft anchors | promotion fails before deployment |
| production agent is missing/deleted | promotion fails before deployment |
| deployment or job does not succeed | keep `status="ready"`; retain `promotion_attempt`; no success artifact |
| Canary source references legacy 1/99 shift | `400 canary.source_experiment_invalid` |

### 5. Good/Base/Bad Cases

- **Good**: treatment accepted → promote returns `202` → A/B test reaches
  `STOPPED` → accepted defaults are persisted → update deployment succeeds →
  `promote` records the deployed version and status becomes `promoted`.
- **Base**: legacy `{"after_weights":{"C":1,"T1":99}}` row renders as ready;
  explicit completion stops the existing test, deploys treatment, and retains
  the old weights in `prior_shift`.
- **Bad**: runtime update fails after the test stopped; the row stays ready,
  `promotion_attempt` proves where it failed, and retry does not try to stop an
  already-stopped test.

### 6. Tests Required

- Capability matrix: generated runtime, v2/legacy converted runtime, harness,
  container, Studio, A2A, and arbitrary source; assert API projection and create
  enforcement.
- Bundle shape: assert new writes use `tools.<name>.description`; runtime tests
  assert documented shape, legacy fallback, and promoted defaults.
- Promotion: assert stop-before-deploy ordering, already-stopped retry,
  stop failure, deployment failure, legacy completion, success artifact shape,
  and success-only status transition.
- Deployment pipeline: assert `skip_register=True` skips only registration and
  preserves `mode="update"`.
- Frontend/browser: assert legacy label and confirmation, running/failed/success
  states, deployed version, disabled unsupported agents, and mobile layout.

### 7. Wrong vs Correct

#### Wrong

```python
# A weight floor is an A/B test constraint, not production deployment.
update_ab_test(variants={"C": 1, "T1": 99})
_update(exp.id, status="promoted")
```

#### Correct

```python
stopped = _stop_ab_test(data, ab_test_id, progress)
deployment, job = create_deployment(
    db, agent, mode="update", skip_register=True
)
execute_deploy_job(job.id)
# Write status="promoted" only after deployment and job are both succeeded.
```
