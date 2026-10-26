# Resource Visibility (workspace member isolation)

> Per-workspace `shared` (default) | `isolated` visibility of user-managed resources.
> Task `10-11-workspace-visibility-isolation`; keys on the creator recorded per
> [Resource Creators](./resource-creators.md).

## 1. Scope / Trigger

- Trigger: an authorization rule spanning ~67 by-id routes, 13 lists, ~11 create-time
  references, overview aggregates and the architect catalog; a new admin API.
- Kinds: knowledge bases, managed memories, eval datasets (+ cloud datasets), pipelines,
  eval runs, online eval configs, custom evaluators, experiments, runtime canaries, Skill
  Lab jobs + tasksets, and **agents** (task `10-11-agent-visibility-isolation`, §8) with
  their derived surfaces (chat, `/v1`, registry A2A records, observability rows, memory
  console actors). Registry MCP/skill records and gateways stay shared.

## 2. Signatures

`backend/app/services/visibility.py`:

```python
SETTINGS_KEY = "resource_visibility"; SHARED = "shared"; ISOLATED = "isolated"
def mode(row: Workspace | None) -> str            # absent/unknown → SHARED
def set_mode(db, row, value) -> None               # reassigns the JSON blob, commits

@dataclass(frozen=True)
class Viewer:
    user_id: str | None
    filters: bool                                  # isolated and not identity.is_admin
    ALL: ClassVar[Viewer]                          # internal callers
    @classmethod
    def of_request(cls, request) -> Viewer         # cached on request.state.viewer
    def principal_key(self) -> str                 # "all" | "u:<id>" (cache keys)

def require_viewer(request) -> Viewer               # FastAPI dependency
def viewer_for_principal(db, workspace, principal) -> Viewer   # assistant conversation owner
def sees(viewer, db, created_by) -> bool            # row check
def clause(viewer, column)                          # the same rule as SQL
def require_seen(viewer, db, created_by, error) -> None
def hidden_aws_ids(viewer, db, workspace_id, kind, ids) -> set[str]   # KB / evaluator
def aws_id_visible(viewer, db, workspace_id, kind, resource_id) -> bool
```

API: `GET` / `PUT /api/workspaces/{workspace_id}/settings/visibility`,
body/response `{"resource_visibility": "shared" | "isolated"}`; ADMIN in `ROUTE_POLICY`,
listed in `WORKSPACE_EXEMPT`; needs only the ledger row (works before bootstrap).
Workspace `_out` carries `resource_visibility`.

## 3. Contracts

**Rule.** Visible iff `not viewer.filters` OR `created_by IS NULL` OR
`created_by == viewer.user_id` OR `created_by` is a user whose **current** role is admin.
`filters` is False for every admin (config admin, admin users, auth-disabled operator) and
in shared workspaces — those viewers pay no extra query. A dangling id (deleted user) →
admins only. A demoted admin's resources become private.

**Placement.** Each kind's existing choke point (`_dataset_in`, `_pipeline_in`, `_run_in`,
`online.get_config`, `_experiment_in`, `_canary_in`, `jobs.get_job`, `tasksets.get_row`,
`memory_ownership.require_managed`, router helpers for KB / evaluator / cloud dataset)
takes keyword-only `viewer: Viewer = Viewer.ALL`. **Only routers pass
`require_viewer`.** Workers, the pipeline background thread, the materializer, purge, the
taskset expansion target, memory pin/deploy/chat and the deploy-time catalog recheck keep
`Viewer.ALL`.

**Eval scorecards** ([Evaluation Scorecards](./eval-scorecard.md)): `GET /api/eval/scorecard` applies the run `clause` and drops `hidden_agent_ids` in SQL before its cap. A dataset label or legacy-alignment items come from a dataset only when the viewer `sees` it; otherwise the run's stored name is used and alignment gets `dataset_unavailable`. Compare withholds a hidden agent's publish history (`config: null`).

**Derived ownership.** Online config with a ledger row → row creator; name `exp_<id8>` /
`can_<id8>` → the owning experiment / canary's creator; external → shared. Cloud dataset:
no local row → shared; linked → visible if any linked row is visible.

**Aggregates.** `/api/overview` counts with `clause`; online-quality cache key is
`(workspace_id, viewer.principal_key())`. Queue depth / concurrency stay global; only the
run ids it names are filtered. The one-running-experiment mutex stays workspace-wide (its
error no longer names a hidden experiment).

**Catalog.** `restrict_catalog` on create / refresh / selection / import / approval, and
`owner_catalog(db, conversation)` on read, both with the conversation **owner's** viewer —
a snapshot taken while shared never leaks after the switch.

## 4. Validation & Error Matrix

| Condition | Result |
|---|---|
| Hidden id on a by-id route | the kind's existing not-found (`dataset.not_found`, `run.not_found`, `pipeline.not_found`, `online_eval.not_found`, `experiment.not_found`, `canary.not_found`, `skill_lab.*_not_found`, `memory.not_managed`, `kb.not_found`; evaluators `aws.not_found`) |
| Hidden reference at create time | that route's existing missing-id answer (`dataset.not_found` 404, `run.evaluator_not_found` 422, `*.evaluator_unsupported` 400, `canary.source_experiment_invalid` 400) |
| Member calls the setting route | 403 `auth.forbidden` |
| Unknown mode value | 422 |
| Unknown workspace | 404 |
| Built-in / third-party evaluator, bootstrap memory | always visible |

## 5. Good / Base / Bad Cases

- Good: isolated; member bob lists datasets → own + config-admin + admin-user rows; GET/PUT/DELETE alice's id → 404 `dataset.not_found`; pipeline with `output.dataset_id` = alice's → 404.
- Base: shared (default) → identical to pre-feature behavior; admin in isolated sees all.
- Bad: filtering a list in Python after `.limit(20)` / offset — members see short pages and wrong `total`. Filter in SQL first.

## 6. Tests Required

`backend/tests/test_visibility.py`: `sees`/`clause` parity; per-kind matrix (shared vs
isolated × own / other member / admin-created / NULL-creator / demoted admin);
create-time references; runs `total` and experiments/canaries `limit(20)` with 25 hidden
newer rows; online experiment-owned config + `/results` gate; overview per-viewer cache;
catalog owner view incl. a snapshot taken while shared; setting route (admin, member 403,
non-bootstrapped workspace, bad value 422); `tests/test_route_policy.py`.

## 7. Wrong vs Correct

#### Wrong — passing the request viewer into an internal path

```python
# pipeline background thread
ds = _dataset_in(db, ws, out.dataset_id, viewer=viewer)   # a later run by an admin of a
                                                          # member's pipeline breaks
```

#### Correct

```python
ds = _dataset_in(db, ws, out.dataset_id)   # Viewer.ALL — the route already checked access
```

#### Wrong — filtering a stored snapshot only when it is written

The assistant catalog is persisted on the conversation; restricting it only at create /
refresh leaks KBs captured while the workspace was shared. Restrict on read too
(`owner_catalog`).

## 8. Agents, /v1 keys, API keys (task `10-11-agent-visibility-isolation`)

- **Creator.** `agents.created_by` / `created_by_name` (literal DDL in
  `_migrate_creator_columns`). Stamped by `POST /api/agents` (caller), convert (the
  converter owns the new twin), architect approval (`Creator.of(approver)`). Discovered
  runtimes and presets record nothing → shared. `owner` stays a provenance label.
- **One choke point.** `app/services/agent_access.py`: `agent_in(db, ws_id, id, *,
  viewer=Viewer.ALL)`, `agent_clause(viewer)`, `hidden_agent_ids`, a request-scoped
  hidden-agent snapshot (names, `harness_<name>` service names, registry record ids),
  `require_agent_visible`, `check_pins`. Every former helper (`agents._agent_in`,
  `chat._agent_in`, `online_routers._agent_in`, both `_eligible_agent`s, `/v1
  _active_agent`, inline loads) delegates and keeps **its own miss error**.
- **/v1.** `visibility.viewer_for_key(db, key)`: shared workspace / NULL-creator key /
  admin creator → `Viewer.ALL`; else filtered by the key creator. Re-evaluated per call.
- **API keys.** Under isolation a member lists / disables / enables only keys they minted
  (404 `apikey.not_found` otherwise). NULL-creator keys are **admin-only** — a key is a
  credential, so the "unknown creator = shared" rule does not apply.
- **Pins.** Create / convert check every pinned KB + memory against the request viewer;
  redeploy / inbound-auth check only KB ids added vs the stored spec (or a changed memory
  id), so pins made while shared survive a switch to isolated. Hidden memory →
  422 `agent.memory_not_managed` (the route's existing unmanaged answer).
- **Cross-module.** Recommendation accept, recommendation-inputs (no hidden prompt/tools
  disclosure), experiment and canary actions (all but `verdict` / `cleanup`) also require
  the agent visible.
- **Observability / memory console.** Hidden agents' trace / session rows dropped; details,
  transcript, evaluate → 404; memory actors of hidden agents dropped, their events /
  records / namespaces → 404 `memory.actor_not_found`. Dashboard + memory overview totals
  stay workspace-wide (no names).
- **Leaks closed.** `agent.name_exists` detail carries `agent_id` only when visible (name
  uniqueness stays workspace-wide); discovery masks hidden owners (`managed: true`,
  no id/name/method, `reason_code: "already-managed"`); KB / memory / Connections lists
  drop hidden names but keep true counts (`attached_agent_count`); online `matched_agent`
  null when hidden; registry A2A records of hidden agents → dropped / 404
  `aws.not_found`; `GET /api/jobs/{id}` of a hidden agent → 404; conversion-in-flight 409
  no longer names a hidden twin.
- **Known residuals.** `/observability/sessions/{id}/evaluate` refuses only ledger-linked
  hidden sessions; a visible run / experiment / canary still shows its own `agent_name`;
  KB force-delete strips the KB from every mounting agent; canary rollback of a hidden
  agent is admin-only.
- **Tests.** `backend/tests/test_agent_visibility.py`.
