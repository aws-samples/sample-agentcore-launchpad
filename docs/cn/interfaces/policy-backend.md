# Interface: policy backend (D20 → P1-a — implemented by `CedarEngine` on cedarpy)

> **Global model (2026-10-28).** The policy module follows Global AgentCore Policy now: policy
> engines are resources, policies belong to an engine, an engine is attached to gateways with a
> gateway-level `LOG_ONLY | ENFORCE` mode, and the `/api/policies` surface is 410. The contract is
> **`docs/cn/interfaces/policy-engines.md`** — it supersedes §"Store" (the `policy_settings` row,
> `scope` / `agent_ids`), §"HTTP surface" and the workspace mode / switches / evidence /
> confirmation / `stale_agents` / `apply` paragraphs below, which stay as the D20 history. The
> engine Protocol, the Cedar schema, the compiled-set cache (keyed by engine id now), the decision
> audit, the S3 layout and the interceptor operations remain as written.

Frozen 2026-10-14 (node P1-a of `docs/superpowers/plans/2026-10-14-d20-policy.md`). This
file is the contract the other D20 nodes code against: the `PolicyEngine` Protocol, the
store (ledger + S3), the compiled-set cache, the decision semantics and the HTTP surface
of `/api/policies`. Enforcement at invoke and deploy-time tool narrowing (P1-b, §"Enforcement"),
the CloudWatch audit sink and the capabilities / overview blocks (P1-d), the console (P1-c)
and the runtime guard (P2-a) build on the hooks named in §"Hooks for the other nodes".

**Implemented 2026-10-14 (P1-a)** by `backend/app/services/policy_cedar.py::CedarEngine`
on **`cedarpy==4.12.1`** (Apache-2.0; embeds Cedar 4.12; pinned in
`backend/pyproject.toml`, `uv.lock` carries the `cp313-manylinux_2_17_{aarch64,x86_64}`
wheels), registered in `app/main.py::create_app` when `"policy"` is in
`Settings.features`. Storage: SQLite tables `policies`, `policy_versions`,
`policy_settings`, `policy_changes`, `policy_decisions` (`app/models/policy.py`) plus an
immutable S3 copy of every published version under the artifacts bucket prefix
`policy/`.

## Where it lives

| piece | module |
|---|---|
| Protocol + slot (`set_backend` / `backend_or_none` / `get_backend` → 503 `feature.unavailable {"feature":"policy"}`) and the shared types (`AuthzRequest`, `AuthzDecision`, `EntityRef`, `ValidationReport`) | `backend/app/services/policy_backend.py` |
| cedarpy engine + the per-policy-engine compiled-set cache | `backend/app/services/policy_cedar.py` |
| ledger CRUD, versions, publish (S3 copy), journal, decision rows | `backend/app/services/policy_store.py` |
| policy engines, gateway attachments, the startup migration, capability / overview helpers | `backend/app/services/policy_engines.py` |
| Cedar entities, `Principal`, request contexts | `backend/app/services/policy_entities.py` |
| one compiler/evaluator for gate, simulator and bundle | `backend/app/services/policy_bundle.py` |
| schema + baseline templates (data files) | `backend/app/policy/` |
| request/response shapes | `backend/app/schemas/policy.py` |
| HTTP surface | `backend/app/routers/policy_engines.py` (`/api/policy-engines`, `/api/governance/gateways/{gid}/policy-engine`); `backend/app/routers/policies.py` answers 410 `policy.moved` on the retired `/api/policies` |
| tests | `backend/tests/test_policy_engine.py`, `test_policy_store.py`, `test_policies_api.py` (+ `tests/policy_fakes.py`: `FakeEngine`, `FakeS3`) |

## `PolicyEngine` Protocol (`app/services/policy_backend.py`)

```python
class PolicyEngine(Protocol):
    """Cedar evaluation only. The store (ledger + S3), enforcement and audit are plain
    modules; this is the one seam a test replaces with a FakeEngine."""

    def validate(self, statement: str, schema: str) -> ValidationReport:
        """Parse + schema-validate one statement set → {ok, errors[{line,col,msg}], warnings[]}."""

    def format(self, statement: str) -> str:
        """Canonical Cedar formatting (editor FORMAT button). Raises ValueError on a
        statement that does not parse."""

    def compile(self, statement: str) -> Any:
        """Parse once into an opaque handle authorize / authorize_batch accept in place of
        `policies`. Raises ValueError on a parse error."""

    def authorize(self, request: AuthzRequest, policies: Any, entities: list[dict]) -> AuthzDecision:
        """One decision: {decision: allow|deny|no_decision, reasons[policy_ids], errors[]}."""

    def authorize_batch(self, requests: list[AuthzRequest], policies: Any, entities: list[dict]) -> list[AuthzDecision]:
        """Bundle compilation / simulator matrix — N decisions, same order as requests."""

    def policy_ids(self, statement: str) -> list[str]:
        """Stable ids for each policy in a statement set (`@id("…")` annotation if present,
        else `policy<n>`). Raises ValueError on a parse error."""
```

Additive to the plan's §2.4 body: **`compile()`** — cedarpy re-parses policy text on every
call (≈ 0.9 ms for 50 policies as text vs ≈ 0.1 ms with a parsed `PolicySet` handle,
probe 2026-10-14), so the cache keeps handles and the hot path never passes text.
`CedarEngine` also offers a non-Protocol `annotate(statement, stamp)` that re-renders
the statement with `@id("<stamp>")` (one policy) / `@id("<stamp>#<k>")` (several) on
every policy, replacing author-supplied ids; `policy_cedar.annotate()` uses it when
present and leaves the text alone for a `FakeEngine`.

Decision values: `allow` | `deny` | `no_decision`. **`no_decision` is an engine failure,
never a permit**: a parse error, an entity attribute the schema does not declare, a
missing required context attribute or an undeclared principal type all yield it, with
the engine's message in `errors`. Callers fail closed on it (simulator: outcome
`NO_DECISION`; the former hub invoke gate's 503 is gone with the gate — Global parity,
§"Enforcement").

`reasons` are the determining policies as *our* ids: the compiled workspace set stamps
every policy `@id("<policy_id>:v<n>[#k]")`, and cedarpy's `id_annotations_by_reason`
maps its positional `policy<n>` back to those stamps. Unannotated text (the `/validate`
of an unsaved statement) reports `policy<n>`.

## Cedar schema (`backend/app/policy/schema.cedarschema.json`, namespace `Launchpad`)

```text
entity Role;                                   // Role::"admin" | Role::"member"
entity User in [Role]        { username: String, role: String, user_id?: String };
entity ApiKey                { name: String, created_by?: String };
entity System;                                 // System::"hub" — internal callers (plan §4.3)
entity Workspace;
entity Agent in [Workspace]  { name: String, method: String, owner?: String, system: Bool };
entity Gateway in [Workspace]{ gateway_id: String };
entity Tool in [Gateway, Agent] { name: String, tool: String, target: String };
action invoke    appliesTo { principal: [User, ApiKey, System], resource: [Agent],
                             context: { channel: String, session_id?: String, env: String } };
action call_tool appliesTo { principal: [User, ApiKey, System, Agent], resource: [Tool],
                             context: { channel: String, agent_id: String } };
action list_tool appliesTo { principal: [Agent], resource: [Tool], context: { agent_id: String } };
```

Two deliberate differences from the plan §2.1 sketch:

- **`Tool.name`** (additive) is the full `<target>___<tool>` id, so `resource.name like
  "hr-database___*"` mirrors the `LAUNCHPAD_GATEWAY_TOOLS` fnmatch patterns; `tool` /
  `target` are the two halves (`create_payout` / `hr-database`).
- **`call_tool.context.input` is absent.** An open record (`Record<Any>` /
  `additionalAttributes`) is rejected by this wheel (`unsupported feature used in schema`
  — cedarpy 4.12.1 is built without partial validation). A rule reading `context.input`
  therefore fails validation (422 `policy.invalid_statement`, "attribute `input` in
  context … not found") instead of silently evaluating to an error. P2-a's
  `context.input` rules need either per-target attribute declarations generated from the
  Gateway tool schemas or a wheel built with the feature — recorded for that node.

Entity ids operators write in rules: `User::"<username>"`, `ApiKey::"<key id>"` (rules may
also match `.name`), `Agent::"<agent id>"`, `Tool::"<target>___<tool>"`, `System::"hub"`.
`AgentSpec.policy_ids: list[str] = []` (P1-b adds the field) is read by
`policy_bundle.spec_policy_ids`; the spec is a full-document replacement, so an ABSENT
field on write means CLEAR, never "keep". `request_context(action, **values)` filters a context to the declared attributes (an
undeclared one would be `NoDecision`) and defaults required strings to `""`.

Baseline templates (`backend/app/policy/templates/*.cedar`, served by `GET
/api/policies/entities.templates`; amendment 9 — policies decide Gateway tool calls only):
`permit_workspace_agents_call_tools` (Stage A: every agent of the workspace,
`__WORKSPACE_ID__` — publish this first), `permit_users_call_tools` (Stage BT: members,
admins, `principal is Launchpad::System` and `principal is Launchpad::ApiKey` — the API key
stands where Global's M2M client does, lead option D 2026-10-27), `permit_agent_declared_tools`
(`__AGENT_ID__` / `__TOOL_PATTERN__` placeholders), `forbid_tool_for_members`
(`__TOOL_ID__`, `unless { principal in Role::"admin" }`). All four validate as shipped; the
console builders emit the first two byte-equal to the files' canonical form
(`tests/test_policy_templates.py` pins). A new policy starts `enforcement_mode = log_only`
(item 5): publish → promote → `enforce`.

## Store (`app/services/policy_store.py`, tables in `app/models/policy.py`)

**Global model (2026-10-28, `policy-engines.md` §Ledger):** `policy_engines` (the PolicyEngine
resource — many per workspace — carrying the Dogwood install state), `policies.engine_id` (a policy
belongs to ONE engine; `scope` / `agent_ids` are inert), `policy_engine_attachments` (one engine +
`LOG_ONLY | ENFORCE` per gateway), `policy_decisions.gateway_id` / `engine_id`,
`policy_changes.engine_id`; `policy_settings` is inert (read once by the startup migration). The
rows below describe the D20 shapes that still hold.

| table | role |
|---|---|
| `policies` | identity: `id` `pol_<hex12>`, `workspace_id`, `engine_id`, `name` (unique per workspace, `^[A-Za-z][A-Za-z0-9_]{0,47}$`), `description`, `enforcement_mode` `active\|log_only`, `status` `draft\|active\|retired`, `active_version`, `draft_version`, `created_by`, timestamps (`scope` / `agent_ids` inert) |
| `policy_versions` | immutable statements: `(policy_id, version)` unique, `statement` (≤ 50 000 chars), `schema_digest`, `validation` (the `ValidationReport.as_dict()` at save), `authored_by`, `published_at` / `published_by` / `s3_key` set once |
| `policy_settings` | **inert since the Global model** — the retired per-workspace mode / switches / evidence window / engine-set state; read ONCE by `policy_engines.migrate`, never written (a fresh ledger has no row) |
| `policy_changes` | journal: `policy.create\|update\|publish\|retire\|delete`, `mode.set`, `bundle.apply` (P1-b: one row per deploy that carried a narrowing — `requested.dry_run` under `log_only` — and one per `POST /apply` call) with `before` / `requested` / `after` / `override_reason` / `error`, `status` `succeeded\|failed` |
| `policy_decisions` | audit rows: `source` `hub\|runtime\|simulate`, `channel`, `principal_{kind,id,label}`, `action`, `resource_{kind,id}`, `agent_id`, `session_id`, `request_id`, `decision` `allow\|deny\|would_deny\|error`, `mode`, `policy_ids`, `policy_versions`, `bundle_version`, `reason`, `latency_ms`; `gateway` rows (per-user policy stream B5) add `stage`, `session_psid`, `event_seq`, `store_epoch`, `input_keys` and `ingested_at` (N17-obs482: the hub's ingest time — `created_at` is the interceptor's decision time; the view adds `ingest_lag_ms`) |

All five are in `core.db.WORKSPACE_SCOPED_TABLES`; `app.models.policy` is in
`MODEL_MODULES`; `_migrate_workspace_columns` / `_migrate_policy_indexes` carry the
upgrade path (`test_ledger_migration.py`).

Lifecycle (`policy_store`):

- `create_policy` → validated draft **v1** (`policy.invalid_statement` 422 with the report
  in `detail`; `policy.name_taken` 409; `policy.unknown_agent` 422 for `scope=agents`).
  **Global parity (GLOBAL-DEVIATIONS item 5, 2026-10-27): a new policy starts
  `enforcement_mode = log_only`** — its first publish installs it as a shadow (the engine
  records what it would decide, the console shows the would-be denials); the operator
  promotes it with `PUT /{policy_id}/enforcement-mode {active}`. Global creates every policy
  LOG_ONLY and promotes it the same way. Rows created before this rule keep their stored mode.
  Under `enforce`, an installed set whose permits are all `log_only` denies every call
  (Cedar default-deny): promote the baselines before switching the workspace to `enforce`.
  A statement naming the retired `Launchpad::Action::"invoke"` (contract amendment 9) is
  422 `policy.invalid_statement` with `detail.reason = "invoke_retired"` and a message that
  names the two baselines to publish instead — before any schema report. A pre-parity
  ACTIVE row made only of `invoke` statements rewrites to `""` for the engine and
  `policy_engine_sync.active_set` leaves it out (one WARN naming the row); the operator
  retires it.
- `update_policy(statement=…)` → a new **draft** version; an ACTIVE version stays live
  until the draft is published (Global: "ACTIVE edits create a candidate"). Description /
  `agent_ids` edits create no version (`policy.scope_mismatch` 422 on a workspace-scoped
  row).
- `publish_policy(version=None)` publishes the draft (or re-activates the last published
  version of a retired row): the S3 copy `policy/policies/<policy_id>/v<n>.cedar` is
  written **before** the ledger commits via `workspace.client("s3").put_object(…,
  IfNoneMatch="*")`; `PreconditionFailed` (key already there) is treated as done, any
  other S3 error is 502 `policy.s3_copy_failed` with a `failed` journal row and the policy
  stays as it was; a workspace without `artifacts_bucket` is 409 `workspace.not_ready`.
  `policy.no_draft` 409 when nothing is publishable, `policy.version_not_found` 404.
- `retire_policy` (`policy.not_active` 409) — refused with 409 `policy.no_active_policies`
  when it would leave an `enforce` workspace with zero ACTIVE policies.
- `delete_policy` — drafts / retired only (`policy.active` 409); removes the versions.
  The S3 copies stay (immutable record).
- `set_settings` — `mode: enforce` requires an administrator (`auth.forbidden` 403,
  checked in the handler because `ROUTE_POLICY` carries one role per route), ≥ 1 ACTIVE
  policy (`policy.no_active_policies` 409) and either ≥ 1 non-simulated `would_deny` row
  in the evidence window or (`confirmation_name == <workspace id>` AND a non-empty
  `override_reason` — GLOBAL-DEVIATIONS item 7(i): Global's `governance.evidence_required`
  needs both; the 422 `policy.confirmation_required` detail carries
  `override_reason_required: true`)
  (`policy.confirmation_required` 422 with `detail.evidence` and
  `detail.expected_confirmation`). Every change is a `mode.set` journal row.

Deviation from plan §2.3(a): publishing *while* in `enforce` is **not** refused — it only
widens what is permitted, and the retire guard above keeps the "enforce with zero ACTIVE
policies" state from arising in the first place.

## Compiled-set cache (`app/services/policy_cedar.py`, plan §2.3(d))

`compiled_set(db, workspace_id, mode) -> CompiledSet` — one per workspace:
`version_stamp = sha256(sorted (policy_id, active_version))[:32] + "+" + mode`, built
lazily from the ACTIVE rows (each statement stamped `@id("<policy_id>:v<n>")`).
`CompiledSet.for_agent(agent_id, extra_policy_ids)` returns the `CompiledSubset`
(`text`, parsed `handle`, `policy_versions`) over the policies that apply to that agent —
workspace-scoped ones plus agent-scoped ones that list it or that its
`AgentSpec.policy_ids` pins (plan §4.2) — and caches the subset per distinct stamp tuple.

Invalidation: `policy_store` calls `policy_cedar.invalidate(workspace_id)` after
`publish` / `retire` / `delete` / `mode.set` (and after an `agent_ids` edit of an ACTIVE
policy); a **30 s TTL** (`STAMP_TTL_SECONDS`) re-stamps from the ledger with one cheap
query so a second hub process converges. A set that no longer parses is kept as raw text
and every decision over it is `no_decision` (fail-closed, logged at ERROR). Drafts and
pinned versions (`include_drafts` / `policy_version` in the simulator) compile **uncached**.

## Decision flow (`app/services/policy_bundle.py`)

```text
caller ─► Principal (user | api_key | agent | system::"hub") + Agent row (+ tool ids)
          ─► effective_subset(db, ws, mode, agent, include_drafts?, pinned?)   cache / ad hoc
          ─► build_entities(principal, agent, tools, gateway)                   per request
          ─► engine.authorize(AuthzRequest, subset.handle, entities)
          ─► Evaluation{outcome ALLOW|DENY|NO_DECISION, determining ["pol_x:v3#0"],
                        determining_policy_ids ["pol_x"], policy_versions {pol_x: 3},
                        mode, source_set active|draft|pinned, latency_ms,
                        reason no_active_policies|null}   ← D20-obs1: ALLOW without the
                                                            engine when the workspace has
                                                            ZERO active policies (knob)
```

Helpers: `evaluate(...)` (one decision), `invoke_request(principal, agent, channel, env,
session_id)`, `call_tool_request(principal, tool_id, agent_id, channel)`,
`decide_tools(db, ws, agent, tool_ids, principal=None)` (`{tool_id: allow|deny|no_decision}`,
one batch; default principal = `Agent::<id>` — the deploy-time projection P1-b intersects
with the declared patterns), `build(db, ws, agent, tool_ids, version)` — the Phase-2
bundle (`launchpad.policy.bundle/1`: `tools` table, `default: "deny"`, `policy_versions`,
`conditional: []` in this build, `principal_rules` when human-principal `call_tool` rules
exist). Per-mode behaviour is the Gateway interceptor's (the bundle's `mode`; Global parity
2026-10-27 — the hub's own invoke gate, narrowing and guard are gone, §"Enforcement"); this
module only decides, for the simulator and the engine-parity checks.

## Enforcement — Global parity (user ruling 2026-10-27, `app/services/policy_enforcement.py`)

**What a policy governs.** Exactly what Global AgentCore Policy governs: **Gateway tool calls**,
decided per `tools/call` (and `tools/list` visibility) **at the Gateway** by the interceptor + the
policy engine (per-user policy streams A/B; contract `policy-principal-contracts.md`,
amendment 9). Three hub extensions were retired on 2026-10-27 — nothing of them is left in the
code, the API or the runtime:

| retired extension | what the hub does instead |
|---|---|
| **invoke gate** (`gate_invoke` on every chat / console / `/v1` / A2A turn; Cedar `invoke` action; `policy_invoke_fail_mode`; 403 `policy.denied` / 503 `policy.undecidable`) | invoking an agent is NOT a policy decision: the invoke chain runs its own gates (status, session, visibility) and no `policy_decisions` row with `source=hub action=invoke` is written any more. A stored statement that names `Launchpad::Action::"invoke"` is dropped by the engine compiler with a warning; a new one is 422 at validate. |
| **deploy-time tool narrowing** (`narrowing_for`: declared patterns ∩ the ACTIVE set's `call_tool` decisions for `Agent::<id>`, `LAUNCHPAD_GATEWAY_TOOLS` narrowed, `LAUNCHPAD_GATEWAY_URL` / `AgentCoreGatewayInvoke` dropped, `policy_applied.patterns / removed / unenforceable / gateway_dropped / coverage`, `AgentInfo.policy_coverage`) | every declared Gateway tool pattern reaches `LAUNCHPAD_GATEWAY_TOOLS`, the execution role always carries `AgentCoreGatewayInvoke`, no `LAUNCHPAD_POLICY_*` key exists; the interceptor decides each call with the caller's principal. |
| **runtime guard** (P2-a: the `BeforeToolCallEvent` hook rendered into the Strands / Harness templates, the per-agent `policy/bundles/<agent>/v<n>.json`, `LAUNCHPAD_POLICY_BUNDLE` / `_FAIL_MODE`, `PolicyBundleRead`, `policy_settings.runtime_guard`, `Settings.policy_runtime_guard[_fail_mode]`, `samples/byoc/launchpad_policy_guard.py`) | gone — Global has no runtime guard. The hub still forwards and audits the `policy_denied` / `policy_decision` frames a runtime stream may carry (a legacy guarded runtime, or the template's re-emit of an I8 Gateway denial), and the chat deny card reads them as before. **N17-obs507:** the Gateway interceptor's denial otherwise reaches the hub only as the agent's TOOL RESULT text (`Tool execution failed: Tool Execution Denied: Tool call not allowed due to policy enforcement [Policy evaluation denied due to pol_a, pol_b]`), so `services/invoke.py` derives the `policy_denied` frame from every `tool_result` frame that carries the signature (`policy_denials.gateway_denial_from_tool_result`, names resolved by `policy_enforcement.gateway_denial_from_tool_result`): the stream yields it right after the `tool_result` (`tool`, `reason` = the raw text, `policy_id` = the first id, `policy_ids[]`, `policy_name`, `policy_names{id: name}` — an unknown id names itself, `source: gateway`, `stage` / `decision_id` null — the ingested gateway row is the audit record, no hub row), plus `gateway_id` / `engine_id` = the DECIDING gateway and the engine attached to it (**N17-obs526:** the gateway is the tool's own — the agent's Gateway ToolRef whose `name` is the tool's `<target>` (`launchpad_gw_<target>___<tool>` / `<target>___<tool>`) or whose declared `config.tools` lists the name, else the agent's single Gateway ToolRef, else the workspace gateway; `engine_id` null when that gateway is unattached; the `policy` history row keeps both so a restored card links the same engine), the sync entrance adds the same objects to `result.policy_denied` (so `/v1` sees them; the runtime's `tool_results` list is consumed and never returned), the `policy` history row is persisted as for any `policy_denied` frame, and a runtime-emitted frame for the same tool suppresses the derived one. Console: the deny card lists every named policy; the paired failed tool card keeps its English result collapsed. **N17-obs508:** a gateway ALLOW a `log_only` policy would have denied carries only the interceptor's note `log_only policy would deny` in `reason`; `decision_view` adds `log_only_would: bool` to gateway rows (additive) and `GET /audit?decision=would_deny` returns those rows beside the real `would_deny` ones; the Decisions tab marks them 本会拒绝（仅记录策略）, counts them as evidence and opens their card as an observation. **N17-obs509:** the deny card opened from a Decisions row names the ROW's principal (`principal_label` / `principal_id` + kind) as 身份 — never the viewer — and lists every determining policy of the row in its headline; a live / restored chat frame keeps the turn's user (the viewer). |

**What remains at deploy time — the principal carrier.** `stage_policy(ctx, agent, spec) ->
DeployPolicy` (computed once per deploy job, logged) answers whether this version renders the
per-user **principal carrier** (`principal_carrier_for`: the interceptor switches on, Gateway
tools declared, not an exempt preset, a template the platform renders — the shared Strands
template or the self-built Harness) and whether the workspace Gateway's interceptor is verified
attached (`interceptor_decides`). `DeployPolicy = {active, mode, declared[], carrier,
interceptor_decides, reason}`; its `summary()` is the job-log line (`policy: enforce · 2 Gateway
tool pattern(s) rendered · principal carrier on · the Gateway interceptor decides every call`).
`mark_applied(db, agent, policy, job_id=, operator=, carrier=, interceptor=)` snapshots the live
version onto `Agent.policy_applied = {mode, applied_at, job_id, carrier?: true,
interceptor_decides?: true}` (a key absent = no) and journals one `bundle.apply` row
(`requested {agent_id, job_id, mode}`, `after {carrier, interceptor_decides}`); a caller without
a job decision (the Runtime-canary promote / rollback) gets the module rule (`_carrier_now`).

**Stale.** `AgentInfo.policy_stale` is **derived on read**, never stored, and has exactly ONE
meaning: the carrier a deploy would render NOW (`_carrier_now`: the interceptor switches + the
spec's Gateway tools) differs from the one the live version renders (`policy_applied.carrier`).
A publish, a retire or a mode change never makes an agent stale — policies apply at call time;
only the interceptor switch flipping does (either direction). Presets, discovered runtimes and
agents without a gateway ToolRef are never stale. `AgentInfo.policy_carrier` (`true | false |
null` until a deploy under the module stamped the row) shows what the live version renders.
`GET /api/policies/settings.stale_agents` lists the stale agents.

**Apply.** `POST /api/policies/apply {agent_ids?}` → 202 `{agents: [{agent_id, job_id, carrier,
skipped?}]}`: the stale agents (or exactly `agent_ids`; an unknown id is 404 `agent.not_found`)
are re-deployed with one `deploy_agent` **update** job each (`skip_register`,
`payload.policy_apply = {mode, carrier, operator}`) — the ordinary pipeline, so
`resume_pending_jobs` resumes an interrupted apply like any deploy. An agent that cannot be
re-deployed now (deploying, system preset, discovered runtime, a running Runtime canary, an
unparseable spec) is listed with `job_id: null` and a `skipped` reason. `publish {"apply":
true}` still answers its `apply` block — with nothing to re-deploy unless the switch flipped.

**The empty ACTIVE set.** `Settings.policy_empty_set_decision` (default **`deny`** since the
parity ruling — Cedar's default-deny, what an attached engine without a matching permit answers)
governs the simulator and, as the gateway bundle's `empty_set_decision`, the interceptor: publish
the two baseline `call_tool` permits (workspace agents, users) before attaching. The legacy value
`allow` (D20-obs1) treats zero ACTIVE policies as "policy not configured" (ALLOW with reason
`no_active_policies`) until the first ACTIVE policy. `GET /api/policies/settings` exposes the
effective value as `empty_set_decision`.

**Workspace mode.** `policy_settings.mode` (`off` | `log_only` | `enforce`) has ONE consumer:
the gateway bundle block (`policy_gateway_publish` → the interceptor's `w.mode`). `off` = the
interceptor passes the workspace's calls (Global's "engine detached"); `log_only` records what it
would deny; `enforce` denies.

**Simulator.** `POST /api/policies/simulate` decides a `tool` resource with `call_tool` |
`list_tool` for a principal, the calling agent named in `context.agent_id` (cedarpy on the
ACTIVE / draft / pinned set, or the engine's `replay` with history — stream A6). There is no
`invoke` action and no `agent` resource.

**`AgentSpec.policy_ids: list[str] = []`** (≤ 20): validated on `POST /api/agents` and
re-publish against this workspace's `policies` (422 `policy.unknown_policy
{policy_ids[]}`; a draft / retired id is accepted and ignored with a job-log warning).
The spec is a full-document replacement: an ABSENT field on write means CLEAR.

Settings knobs: `policy_empty_set_decision` (above) and the per-user switches
(`policy_gateway_interceptor`, the workspace's `gateway_interceptor` / `engine_publish`). System
presets (`Agent.system_key`) never render the carrier (ruling 12: system and evaluation agents
are exempt from policy — the exemption is structural, not a knob).

## HTTP surface — `/api/policies` (RETIRED 2026-10-28 → 410 `policy.moved`; the live surface is `/api/policy-engines`, see `policy-engines.md` §Routes)

The table below is the D20 surface as it stood before the Global model; every path answers
410 `policy.moved {engines_url}` for one release.

| method path | body → response | route policy |
|---|---|---|
| GET `""` `?status&agent_id` | → `{policies: [policy], settings}` | MEMBER |
| POST `""` 201 | `PolicyCreate{name, statement, description?, scope, agent_ids?, prefill?, dialect?}` → `policy` (draft v1). `dialect` (A6, additive, default `cedar`): `dogwood` = Global's vocabulary incl. temporal rules, accepted only while the workspace publishes to the policy engine (409 `policy.engine_not_routed`), validated on the engine's compiler against the workspace's Gateway `tools/list` together with the other ACTIVE policies (T8 quotas; 422 `policy.invalid_statement` with the compiler's errors; 409 `policy.compiler_unavailable` `{source: tools_list \| compiler, reason}` when `tools/list` or the compiler does not answer). `POST /validate` with `dialect: dogwood` draws on the per-user engine-simulation budget (429 `policy.simulate_rate_limited`, review D2) and its `policy_id` must be this workspace's (404); the version stores `dialect` and the compiler's `kind` | `perm:policy.manage` |
| GET `/{policy_id}` | → `policy` + `versions[{id, policy_id, version, statement, schema_digest, validation, authored_by, created_at, published_at (null ⇒ draft), published_by, s3_key}]` | MEMBER |
| PUT `/{policy_id}` | `PolicyUpdate{statement?, description?, agent_ids?, dialect?}` → `policy` (`dialect` absent = the current version's; a change of language alone is a new draft version) | `perm:policy.manage` |
| POST `/{policy_id}/publish` | `PolicyPublish{version?, reason?, confirmation_name?, apply}` → `policy` + `apply: {requested, agents[], job_ids[], unavailable_reason?}`. A Dogwood version (incl. a re-activation) is 409 `policy.engine_not_routed` `{policy_id, version}` unless the workspace publishes to the engine (review D1) | `perm:policy.manage` |
| POST `/{policy_id}/retire` | `{reason?}` → `policy` | `perm:policy.manage` |
| PUT `/{policy_id}/enforcement-mode` | `{enforcement_mode: active \| log_only}` → `policy` (A6, per-user policy). One policy's enforcement on the policy engine: `log_only` records what it would deny without denying (the interceptor's would-be denials show in Decisions). 409 `policy.interceptor_unavailable` while the platform switch is off, 403 for a non-administrator, 422 on another value; an ACTIVE policy is re-installed on the engine at once (`_engine_install`, then the gateway bundle) — a refusal is 409 `policy.engine_install_failed` and the mode is rolled back; journalled `policy.enforcement_mode` (`before` / `requested` / `after`); a no-op when unchanged | `perm:policy.manage`; ADMIN in the handler |
| DELETE `/{policy_id}` | → `{deleted: true, id}` | `perm:policy.manage` |
| POST `/validate` | `{statement, dialect?, policy_id?}` → `{ok, errors[{line,col,msg,policy_id}], warnings[], policy_ids[]}` (`dialect: dogwood` as on create; `policy_id` = the policy being edited, left out of the ACTIVE set it compiles with) | MEMBER |
| POST `/format` | `{statement}` → `{statement}` (422 `policy.invalid_statement`) | MEMBER |
| POST `/simulate` | `{principal{kind,id,role?}, action, resource{kind,id}, context?, include_drafts?, policy_version?{policy_id,version}, record?}` → `{outcome ALLOW\|DENY\|NO_DECISION, determining_policy_ids ["<policy_id>:v<n>"], policy_ids ["<policy_id>"], determining ["<policy_id>:v<n>#k"], errors[], mode, policy_versions, source_set, latency_ms, request, reason (D20-obs1, additive: `no_active_policies` when the workspace has zero ACTIVE policies and `Settings.policy_empty_set_decision = allow`, else `null`), decision_id?}`. Additive `engine?{tool?, agent_id?, input, at?, history[]}` (A6): decided on the policy engine (both stages, temporal over `history`) → `{engine: true, stage_a, stage_bt, final, log_only_would, determining, errors, trace, source_set, policy_versions, warnings, decision_id?}`; 409 `policy.engine_not_routed` unless `engine_decides` (policy-principal-contracts.md §Stream A6) | MEMBER; `record: true` → `perm:policy.manage` in the handler |
| GET `/audit` `?source&decision&agent_id&principal&since&limit` | → `{decisions: [decision], runtime_channel: {status ready\|missing\|unknown, reason, log_group}}` (`runtime_channel` = the CloudWatch decisions channel, §"Decision audit"; prunes rows older than `Settings.policy_decisions_keep_days`) | MEMBER |
| GET `/changes` `?limit` | → `{changes: [change]}` | MEMBER |
| GET `/settings` | → `{mode, evidence_window, evidence{would_deny, since, window}, active_policies, stale_agents[], empty_set_decision, updated_by, updated_at}` (Global parity 2026-10-27: `invoke_fail_mode`, `runtime_guard` and `coverage` are gone; `stale_agents` = carrier-stale; `empty_set_decision` = the effective knob). Additive while `Settings.policy_gateway_interceptor` is on (A6): `interceptor{gateway_interceptor, engine_publish, policy_set_version ("ps-<n>" \| null), temporal_set_version, last_install{status ok\|failed, trigger, policy_set_version, error, operator, at} \| null, temporal_count, bundle_pending, attached, enforcement_live, engine_decides}` (`attached` = the workspace gateway's verified interceptor attach state (`policy_gateway_state`), `enforcement_live` = `gateway_interceptor` AND `engine_publish` AND `attached` — the console's live posture that shows the Policies / Decisions / Audit tabs, N17-obs497 user ruling 2026-10-27; `last_install` = the newest `engine.install` journal row; `temporal_count` / `bundle_pending` from the last successful install's `engine_state` (A5a-2: how many temporal policies the set holds; whether the gateway bundle write after it has not succeeded yet); `engine_decides` = `policy_engine_decide.engine_decides`) | MEMBER |
| PUT `/settings` | `{mode?, evidence_window?, confirmation_name?, override_reason?, gateway_interceptor?, engine_publish?}` → settings. The two per-user policy flags (A6) are 409 `policy.interceptor_unavailable` while the platform switch is off and 403 `auth.forbidden` for a non-administrator; journalled in the `mode.set` row (`before` / `requested` / `after` carry them). Turning `engine_publish` on (so that the workspace now publishes to the engine) **reconciles** the engine at once (`policy_engine_sync.reconcile_workspace`: installs only when the engine is not provably on the hub's set — a set that missed a publish while a switch was off, or one installed by hand — and then writes the gateway bundle); an engine refusal is 409 `policy.engine_install_failed` and the switch stays off (rolled back). EVERY switch / mode change rewrites the gateway bundle (block `gateway_interceptor` / `published` / `mode` live; a failed write is retried by the reconciler) — contract amendment 7 (N17-obs491). A change that stops the workspace publishing to the engine while a policy's ACTIVE version or DRAFT is Dogwood is 409 `policy.dogwood_active` (`policy_ids`; only the engine reads Dogwood — retire / discard them first; review D1) | `perm:policy.manage`; `mode: enforce`, `gateway_interceptor`, `engine_publish` → ADMIN in the handler |
| GET `/engine` | → `{available, healthy, shards[{shard, ready, store_epoch, policy_set_version (this workspace's set on that shard), in_sync (= the hub's last installed set), error?}], workspace: interceptor block \| null}` (A6). `available: false` with no engine call while the platform switch is off; an unreachable engine is `ready: false` per shard with its error code, never a 5xx; other workspaces' sets are not shown | MEMBER |
| POST `/apply` | `{agent_ids?}` → 202 `{agents: [{agent_id, job_id, carrier, skipped?}]}` (re-deploys the carrier-stale agents, §"Enforcement") | `perm:policy.manage` |
| GET `/entities` | → `{users[{id, username, role}], api_keys[{id, name, label, created_by, enabled}], agents[{id, name, method, status, system, owner}], tools[{name "<target>___<tool>" (fnmatch pattern), target, tool, agent_ids[], source: "declared"}], roles[], system[], templates[{name, statement}]}` — ledger-derived (the agents' declared gateway patterns), no AWS call | MEMBER |

`policy` = `{id, name, description, scope, agent_ids, status (lowercase draft|active|retired),
active_version, draft_version, statement (draft if any, else active), validation, policy_ids,
published_at, s3_key, created_by, created_at, updated_at}`, plus, only while
`Settings.policy_gateway_interceptor` is on (A6), `kind` (`stateless` | `temporal`), `dialect`
(`cedar` | `dogwood`) of the live version (else the editable one) and `enforcement_mode`
(`active` | `log_only`). `decision` = the
`policy_decisions` columns (`principal_kind/_id/_label`, `resource_kind/_id`, lowercase
`decision`, `policy_ids` bare ids, `policy_versions {id: n}`, …) plus nested `principal` /
`resource` objects. `change` = the `policy_changes` columns. Stored states are lowercase;
only the simulator `outcome` is uppercase. **Exact key sets are pinned by
`backend/tests/test_policies_contract.py`** — extend that test and this file together. Simulator resource rules: `tool` ⇔ `call_tool | list_tool` only (Global parity — no `agent`
resource, no `invoke` action; 422 `policy.invalid_simulation` otherwise); a `user`
principal takes its role from the ledger row when one exists, else `role` (default
`member`); an `api_key` principal resolves by key id or by name within the workspace.

Errors (all through `register_error_handlers`): `feature.unavailable` 503,
`policy.invalid_statement` 422, `policy.invalid_name` 400, `policy.name_taken` 409,
`policy.unknown_agent` 422, `policy.scope_mismatch` 422, `policy.not_found` 404,
`policy.version_not_found` 404, `policy.no_draft` 409, `policy.not_active` 409,
`policy.active` 409, `policy.no_active_policies` 409, `policy.confirmation_required` 422,
`policy.invalid_mode` / `policy.invalid_window` 400, `policy.s3_copy_failed` 502,
`workspace.not_ready` 409, `policy.invalid_simulation` 422, `policy.invalid_since` 422,
`auth.forbidden` 403, `auth.permission_required` 403. P1-b's: `policy.denied` 403,
`policy.undecidable` 503, `policy.compile_failed` 409, `policy.unknown_policy` 422,
`agent.not_found` 404 on `/apply`.

New permission key `policy.manage` (`services/users.AGENT_PERMISSIONS`, default granted,
revocable per user); new `ROUTE_POLICY` constant `PERM_POLICY_MANAGE`.

## Decision audit (P1-d, plan §2.3(c)) — `app/services/policy_audit.py`

`policy_store.record_decision` writes the `policy_decisions` row (the **system of record**)
and then calls `policy_audit.publish_row(row)`, which mirrors the decision to CloudWatch
Logs group **`/launchpad/policy/decisions[-<env>]`** (`naming.resource_name`), stream
`hub-<hostname>-<yyyymmdd>`. Properties:

- **Never blocks a request.** `publish_row` enqueues onto a bounded queue
  (`Settings.policy_audit_queue_max`, default 5000); a daemon thread ships batches of
  ≤ `policy_audit_batch_size` (100) events or every `policy_audit_flush_s` (1.0 s), whichever
  first, with `put_log_events` through the **funnel** — the decision's workspace context
  (`context_for_workspace(ws).client("logs")`), so a spoke workspace ships into its own
  account's group (plan §4.1).
- **Fail-soft.** A full queue drops the event (counted, WARN); a failed put is a WARN
  rate-limited to one per code per 60 s; nothing raises into a decision. The ledger row
  exists either way.
- **No secrets (C4).** The event is exactly the fields below — principal id/label, resource
  id, decision, policy ids/versions, latency, request id. Prompts, tool arguments, keys and
  bodies never enter a decision row, so they cannot leak here (`policy_audit.EVENT_FIELDS`,
  pinned by `tests/test_policy_audit.py`).

```json
{"schema": "launchpad.policy.decision/1", "decision_id": "dec_…", "request_id": "…",
 "ts": "2026-10-14T02:30:00+00:00", "env": "", "workspace_id": "default",
 "source": "hub|runtime|simulate", "channel": "chat|v1|system|runtime",
 "principal": {"kind": "user|api_key|agent|system", "id": "…", "label": "…"},
 "action": "invoke|call_tool|list_tool", "resource": {"kind": "agent|tool", "id": "…"},
 "agent_id": "…", "session_id": "…", "decision": "allow|deny|would_deny|error",
 "mode": "off|log_only|enforce", "policy_ids": ["pol_…"], "policy_versions": {"pol_…": 3},
 "bundle_version": null, "reason": "… (≤ 2000 chars)", "latency_ms": 1.25}
```

The sink is installed by `app/main.py` with the `policy` feature when
`Settings.policy_audit_enabled` (default true); the hermetic suite replaces the client
factory (`tests/conftest.py::hermetic_policy_audit`). **`GET /audit.runtime_channel`** is the
channel's state: `unknown` before the first ship, `ready` once a batch landed (or the
bootstrap stage ensured the group in this process), `missing` when CloudWatch answered
`ResourceNotFoundException` (reason names `make bootstrap`), always with `log_group`.
Phase-2 runtime-guard rows (P2-a) print the same JSON shape to the runtime log group and
will be joined through this channel.

**Bootstrap `policy` stage** (hub `services/bootstrap.py::ensure_policy_decisions` and spoke
`workspace_bootstrap._stage_policy`, after `observability`): registers the Cedar engine in
the CLI process, `CreateLogGroup` (idempotent) + `PutRetentionPolicy` with
`Settings.log_group_retention_days` (90; `0` leaves it unbounded), records
`resources.policy_log_group` (+ `policy_log_group_unavailable_reason`). Degrades on any AWS
error — decisions stay in the ledger, the summary prints a `policy decisions log WARN` row.
`scripts/bootstrap.py` prints `policy  engine cedarpy · mode <mode> · <n> active` and
`policy decisions log  <group> · created|reused · retention <d> d`. IAM: the EC2 ops role has
`logs:*`; the spoke workspace role gained Sid `PolicyDecisionsLog` (`logs:CreateLogStream`,
`logs:PutLogEvents` on `log-group:/launchpad/policy/decisions*`) next to the existing
`CreateLogGroup` / `PutRetentionPolicy`. `scripts/teardown.py` lists the group as a
`policy-log-group` target (after the agent roles, before the stacks).

**Prune-on-record** (rev-r8 nit): every `Settings.policy_decisions_prune_every` (500; `0`
disables) committed `record_decision` calls also run `prune_decisions` (which now deletes
with `synchronize_session="fetch"`), so retention does not depend on someone opening the
Decisions tab.

## Settings (`core/config.py`)

- `features` gains `policy` (default on; `LAUNCHPAD_FEATURES` without it hides the
  router and skips `set_backend`).
- `policy_default_mode: off|log_only|enforce = "log_only"` — the mode a new
  `policy_settings` row starts in.
- `policy_decisions_keep_days: int = 30` — `prune_decisions` retention.
- `continuation_max_tokens: int = 0` (N-SCN-bug4 ⑤, user ruling 2026-10-14; `0..65536`, 0 =
  off) — the lower output cap a max_tokens *continuation* call uses in both generated
  templates; injected as `LAUNCHPAD_CONTINUATION_MAX_TOKENS` only when non-zero (a spec's
  own `env` wins), restored to the model's cap after the continuation.
- `policy_audit_enabled: bool = True`, `policy_audit_batch_size: int = 100`,
  `policy_audit_flush_s: float = 1.0`, `policy_audit_queue_max: int = 5000` — the CloudWatch
  decision sink (P1-d, §"Decision audit").
- `policy_decisions_prune_every: int = 500` — prune-on-record cadence (`0` = only on `GET /audit`).
- `policy_empty_set_decision: allow|deny = "deny"` — what an EMPTY applicable ACTIVE set
  means for the simulator and, through the gateway bundle, the interceptor (Global parity
  2026-10-27: Cedar default-deny; the legacy `allow` answers ALLOW with reason
  `no_active_policies`). Default-deny applies from the first ACTIVE policy on either way.

## S3 layout (artifacts bucket, prefix `policy/`)

`policy/policies/<policy_id>/v<n>.cedar` — published statements (P1-a);
`policy/bundles/<agent_id>/v<n>.json` — the runtime guard's decision tables, written by the
deployer per deploy job while the guard is on (P2-a, §"Runtime guard"); `policy/schema/
<digest>.cedarschema.json` (reserved); `policy/gateway/<gateway_id>/v<n>.json` + the mutable
`current.json` pointer + `engine-key` — the gateway interceptor's bundle (stream B,
§"Gateway interceptor"). Written through the funnel only (`workspace.client("s3")`); keys are never
rewritten (`IfNoneMatch="*"`); nothing reads the ledger from a runtime.

## Gateway interceptor (per-user policy stream B) — operations

Contract and decision table: `policy-principal-contracts.md` §"Stream B". Inert until
`Settings.policy_gateway_interceptor` (kill switch, default `false`) is on AND the interceptor
is attached; a workspace's `policy_settings.gateway_interceptor` + `mode` then govern it.

| Piece | Where |
|---|---|
| Lambda source (self-contained, zipped as is) | `backend/app/policy/interceptor/` (`handler.handler`, entry `main.handler`) |
| Bundle writer (I3 + `current.json` pointer) | `policy_bundle.build_gateway_bundle` / `write_gateway_bundle` / `gateway_role_map` |
| Install consistency (N17-obs492) | `policy_engine_control.install_all` installs under a process-wide lock and rolls the shards that accepted back (`force: true`) when a later shard refuses; `reconcile_workspace` re-syncs disagreeing shards with `force`; the gateway ingester schedules a reconcile on `temporal_set_mismatch` rows; the interceptor fails closed on set drift in `enforce` (contract amendment 8) |
| Action ids in the bundle (`governed_actions`, `temporal_referenced_actions`) | **entity form** `AgentCore::Action::"<tool>"` = `interceptor.core.action_entity(name)`; the hub converts the compiler's bare names ONCE in `policy_engine_sync.gateway_actions` when `engine_state` is written (contract amendment 6, N17-obs488 — a bare name governs nothing and the interceptor passes every call) |
| Create the Lambda + role + engine key + gateway invoke grant | bootstrap stage `policy-interceptor` (`services/policy_interceptor.py`; kill switch on only; degrades with `policy_interceptor_unavailable_reason`) |
| Network (two private subnets, S3 gateway endpoint, SGs) | EC2 stack parameter `PolicyInterceptorNetwork=true` (`infra/ec2/launchpad-cn-ec2.yaml`) |
| Attach / detach | `backend/scripts/attach_policy_interceptor.py` (dry run by default) |
| Decision rows | `policy_audit.GatewayDecisionIngester` (FilterLogEvents on `/aws/lambda/launchpad-policy-interceptor[-env]` every 20 s → `policy_decisions`, `source=gateway`) |

**Order to enable on a host** (each live step needs the user's go at that moment, §15.5):

1. EC2 stack update with `PolicyInterceptorNetwork=true` and `AmiId=<running AMI>` (an UPDATE
   without replacement — check the change set: no Instance replacement).
2. `LAUNCHPAD_POLICY_GATEWAY_INTERCEPTOR=true` in `/etc/launchpad/env`, then `make bootstrap`
   (the `policy-interceptor` stage creates; it never attaches). The resource map gains
   `policy_interceptor_function_arn`.
3. The engine units on the host (stream C2) read the same engine key object.
4. The gateway must be off `exceptionLevel: DEBUG` (`scripts/repair_gateway_exception_level.py`,
   N17-obs474) — attach refuses otherwise.
5. `uv run python scripts/attach_policy_interceptor.py` (dry run: prints the UpdateGateway
   payload) → `--apply`. The script reads the gateway back and exits 1 if any field other than
   `interceptorConfigurations` changed.
6. Workspace `mode` `log_only` first (G3: ≥ 1 day, zero `error` rows), then `enforce`.

**Break-glass.** Set the workspace mode to `log_only` (admin, journalled): the interceptor then
passes every call and records `would_deny` / `error` rows; the engine keeps receiving events
best-effort. Picked up within the bundle TTL (30 s). Full stop: `scripts/attach_policy_interceptor.py
--detach --apply` (the gateway then decides nothing; the runtime guard / narrowing of the old
path still apply where configured).

**Removal order.** Detach first, then (only on the user's explicit request) delete the Lambda
and its role; never the reverse — the gateway must never point at a missing function.

**Health.** `policy_decisions` rows with `source=gateway` arriving (≤ 60 s); interceptor log
diagnostics (`launchpad.policy.interceptor`: `bundle_unavailable`, `respond_failed`,
`list_filter_failed`, `list_filter_skipped`, `internal_error`) should be absent
(`list_filter_skipped` = more than 1024 governed actions: tools/list stays unfiltered, calls
are still decided); a deny with
`reason_code: engine_unavailable` means the engine (stream C) is down or over budget (fail
closed, T7); `bundle_unavailable` means the Lambda could not read
`policy/gateway/<gw>/current.json` since its cold start.

**Engine key readers.** The artifacts bucket policy (CDK `launchpad-base`, statement
`EngineKeyReadersOnly`) denies `s3:GetObject*` on `policy/gateway/*/engine-key` to every
principal except `role/launchpad-policy-interceptor[-env]` and this environment's hub role
`role/launchpad-cn-ops-<region>[-env]` — an explicit Deny, so the bucket-wide reads (BYOC
CodeBuild, workspace source objects) never reach it.

**Known limitations (lead review 2026-10-26, accepted).**
- `attach_policy_interceptor.py --probe --replace-probe` without `--apply` plans the detach of
  the P0 recorder, but the attach plan that follows still sees the recorder in the slot and
  reports "another interceptor"; run `--detach` for the recorder first, or accept that the dry
  run shows only the first step.
- `decision_id` is `codec.decision_id(jti, jsonrpc id, "final")`: one token reused across MCP
  reconnects with a repeated JSON-RPC id yields the same id, and the ingester keeps the first
  row. The gateway REQUEST_ID is not added because a gateway retry may carry a new one (the
  retry dedupe the id exists for).
- The interceptor security group's egress is TCP 443 to `0.0.0.0/0`, not the S3 prefix list:
  CloudFormation cannot read a gateway endpoint's prefix-list id. The private subnets have no
  route but local and the S3 gateway endpoint, so the effective reach is S3 only.

## Hooks for the other nodes

| node | hook |
|---|---|
| P1-b enforcement | `policy_bundle.evaluate` / `invoke_request` (one decision for `gate_invoke`), `policy_entities.Principal.from_identity / from_api_key / system()`, `policy_store.mode_of`, `policy_store.record_decision(…, source="hub")`, `policy_bundle.decide_tools` (deploy-time narrowing), `policy_store.register_change_listener(fn)` (events `publish\|retire\|delete\|mode.set` → mark agents `policy_stale`), `settings_view()["stale_agents"]` / `["coverage"]` to fill, `POST /apply` and `publish.apply` to wire |
| P1-d audit / capabilities / overview (**done 2026-10-14**) | `policy_store.record_decision` → `policy_audit.publish_row` (CloudWatch mirror, §"Decision audit"); `routers/capabilities.py` lists `policy` + the `policy_store.capability_summary` block only while the engine is registered; `routers/overview.py` → `policy_store.overview_status(…)` for `services.policy` / `service_detail.policy` (plan §2.6); `GET /audit.runtime_channel` = `policy_audit.runtime_channel()`; prune-on-record every `policy_decisions_prune_every` records |
| P1-b enforcement (**done** 2026-10-14; **retired 2026-10-27**, Global parity — history only) | `policy_bundle.evaluate` / `invoke_request` (the one decision in `gate_invoke`), `Principal.from_identity / from_api_key / system()`, `policy_store.mode_of` (pure read), `policy_store.record_decision(…, source="hub")`, `policy_bundle.decide_tools` (deploy-time narrowing); `settings_view()["stale_agents"]` is filled from `policy_enforcement.stale_agent_ids` (derived — the change listener was not needed); `POST /apply` and `publish.apply` wired |
| P1-d audit / capabilities / overview | `policy_store.record_decision` is the ledger row to mirror to CloudWatch; `policy_store.capability_summary(db, ws)` → the additive `policy: {mode, active_policies, runtime_guard}` block; `policy_store.overview_status(db, ws, engine_registered=policy_backend.backend_or_none() is not None)` → `services.policy` / `detail.policy` (plan §2.6); `GET /audit.runtime_channel` to replace; `policy_store.prune_decisions` for the janitor |
| P1-c console | `schemas/policy.py` + the table above; `GET /entities` feeds pickers and prefill |
| P2-a runtime guard (**done 2026-10-14**; **retired 2026-10-27**, Global parity — history only) | `policy_bundle.build(...)` / `write_bundle(...)` (bundle + S3), `policy_enforcement.stage_narrowing` (writes it per job; `Narrowing.guard` / `guard_rendered` / `bundle_key`), `templates/policy_support.render_policy_placeholders` (Strands) + `harness_convert.graft_policy_guard` (Harness), `record_runtime_decision` (hub audit row from the stream frame), `policy_denials` restored; gated by `Settings.policy_runtime_guard` + `policy_settings.runtime_guard` — §"Runtime guard" |

## Tests (hermetic, in `make verify`)

P1-d: `test_policy_audit.py` (event fields = contract, no secret keys; batching by size +
chronological order + stream name; background thread ships within `flush_s`; per-workspace
clients; `ResourceNotFoundException` → `missing` and recovery; every other failure →
`unknown`, never raised; rate-limited WARNs; bounded queue drops; `record_decision` →
sink incl. `commit=False`; prune-on-record cadence and `0` = off; `ensure_decision_log_group`
create / exists / retention 0 / denied raises; `probe_log_group`; the two role templates
carry the grants; settings defaults), `test_policy_templates.py` (every `templates/*.cedar`
validates against the schema **with placeholders substituted**, formats round-trip, and the
substituted set decides member-invoke / agent-tool / forbidden-payout as promised),
`test_capabilities.py` (`policy` block; hidden without an engine), `test_overview.py`
(ledger-derived row, mode `off`), `test_bootstrap.py::TestPolicyStage` + the script summary
rows, `test_workspace_bootstrap.py` (`policy` stage), `test_teardown_script.py` (target).
P1-b: `test_policy_invoke_gate.py` (modes on the sync entrance, principal kinds incl.
`System::"hub"`, preset exemption + knob, the fail-mode knob matrix, ledger-failure
tolerance, the native stream and the A2A branch gated once, Chat / `/v1` / console invoke
over HTTP with the 403 envelope and the SSE `error` event, the perf budget with 50 real
policies) and `test_policy_deploy_narrowing.py` (narrowing cases incl. wildcard /
unenforceable / gateway dropped / `compile_failed` / exempt / `ignored_policy_ids`, env +
IAM output, `log_only` dry run, coverage enum, derived stale + `mark_applied` journal,
`apply` jobs + resume + HTTP + `publish.apply`, `AgentSpec.policy_ids`, the zip
provision / deploy stages with IAM and Runtime stubs, byte-identical env without an
engine). N12-obs45: `tests/optimization/test_canary_policy_parity.py` (canary candidate
env / code / bundle == the deploy path's per method, one bundle at the next immutable key,
flag-off byte-identical mint, the agent's authorizer on the update, refusal /
disarmed restore on an unprovisioned role, `empty_set_allows`, `policy_stale` untouched by
the mint and snapshotted by `complete` / `rollback`).

`test_policy_engine.py` (real cedarpy: allow / deny / default-deny, parse error →
`no_decision`, schema errors, format, `@id` ids, `annotate`, batch = singles, context
filter, entity shapes, 50 policies × 100 requests < 1 s); `test_policy_store.py` (CRUD,
versioning, S3 copy with `IfNoneMatch` on a `FakeS3`, idempotent re-copy, S3 failure →
502 + failed journal, retire/delete guards, workspace isolation, scoped tables, cache
hit / invalidation / TTL re-stamp / per-agent subsets, drafts & pinned versions, broken
ACTIVE → `NO_DECISION`, bundle table, settings & enforce rules, listeners, decision rows,
pruning, capability/overview semantics); `test_policies_api.py` (503 before registration,
feature flag on/off, CRUD over HTTP, validate/format, simulator matrix incl. drafts,
pinned, record → audit, settings/apply 501, entity catalog, templates validate, gated
console: admin-only `enforce`, `record` needs `policy.manage`). Existing drift tests
(`test_route_policy.py`, `test_capabilities.py`, `test_ledger_migration.py`,
`test_ledger_model_modules.py`, `test_client_funnel.py`) cover the wiring.

## Global parity (contract amendment 9, 2026-10-27)

The invoke gate, the deploy-time narrowing and the runtime guard described above are RETIRED
(user ruling 「去掉」): policies decide Gateway tool calls only; see
`policy-principal-contracts.md` amendment 9 and `designs/policy-default-20261027/GLOBAL-PARITY-NOTE.md`.
The sections above remain as history of the D20 design until the hub removal batch lands.

## Console under Global parity (contract amendment 9, user ruling 2026-10-27)

Policies decide GATEWAY TOOL CALLS only; the console follows (`cn/r17-bp-global-parity-ui`, details
in `policy-principal-contracts.md` §"Stream A6 — console", "Global parity"): one deny card (the
Gateway tool denial — no invoke card, no `policy.denied` envelope handling in Chat); Decisions
offers the `gateway` and `simulate` sources, historic `hub` / `runtime` rows stay readable, greyed
and marked `historic`, and the N17-obs499 mirror banner keeps its source scoping while `GET
/audit.runtime_channel` exists; the template picker lists `permit_workspace_agents_call_tools`
(workspace id substituted console-side) and `permit_users_call_tools` instead of
`permit_members_invoke`, and the first-run hint reads publish the workspace Stage-A permit →
promote to `active` (a new policy publishes as `log_only`, GLOBAL-DEVIATIONS item 5) → Enforce;
the simulator is `call_tool` only (principals user / API key / system, the engine block's calling
agent always asked); the posture card shows the GATEWAY enforcement mode (`mode`) + the two
switches, `empty_set_decision` (deny) and the agents to redeploy so they carry the user token
(`stale_agents`, carrier-only), without the runtime-guard toggle, the fail-mode row (the dead
`invoke_fail_mode`, N17-obs500) or the coverage block; `POST /apply` rows show `carrier` only; the
agent detail shows "携带用户令牌：是/否" from `policy_carrier`; the Enforce dialog requires the typed
workspace id AND an override reason without evidence and names what a 422
`policy.confirmation_required` says is `missing` (item 7(i)).

