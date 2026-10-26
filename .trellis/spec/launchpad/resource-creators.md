# Resource Creators

> Who created a user-managed resource: one helper, one response shape, every kind.
> Introduced by task `10-11-resource-creator-field`; the ownership key it records is
> what workspace visibility isolation (`10-11-workspace-visibility-isolation`) filters on.

## 1. Scope / Trigger

- Trigger: DB schema change (8 tables + `managed_memories` + new `resource_creators`),
  cross-layer response contract (`created_by` / `created_by_name` on list + detail of
  every in-scope kind), and every create/delete path of those kinds.
- In scope: knowledge bases, managed memories, eval datasets, eval pipelines, eval runs,
  online eval configs, custom evaluators, experiments, runtime canaries, Skill Lab jobs,
  Skill Lab tasksets. **Not yet**: agents (`agents.owner` is a provenance label, not a
  principal — see `10-11-agent-visibility-isolation`).

## 2. Signatures

`backend/app/services/creators.py` — the only place creator logic lives:

```python
@dataclass(frozen=True)
class Creator:
    user_id: str | None   # users.id — the ownership key
    name: str | None      # username at creation — display only
    @classmethod
    def of(cls, identity: Identity | None) -> Creator | None
    @classmethod
    def of_request(cls, request: Request) -> Creator     # require_identity(request)

def empty() -> dict[str, str | None]
def stamp(row, creator: Creator | None) -> None          # None → leaves NULLs
def fields(row | Creator | None) -> dict[str, str | None]

KB = "knowledge_base"; EVALUATOR = "evaluator"           # resource_creators.kind
def record(db, workspace_id, kind, resource_id, creator) -> None   # idempotent, commits
def lookup(db, workspace_id, kind, resource_ids) -> dict[str, dict] # one query
def lookup_one(db, workspace_id, kind, resource_id) -> dict
def forget(db, workspace_id, kind, resource_id) -> None             # commits
```

Service constructors take keyword-only `creator: Creator | None = None` (default keeps
direct-call tests working): `submit_run`, `online.create_config`, `start_experiment`,
`start_canary`, `start_harness_canary`, `jobs.submit_eval_job|submit_train_job|
submit_taskgen_job`, `tasksets.create_taskset`, `jobs.import_taskgen_taskset`,
`memory_ownership.register`.

DB:

```
<table>.created_by       VARCHAR(32) NULL, indexed (ix_<table>_created_by)   -- not a FK
<table>.created_by_name  VARCHAR(64) NULL
  on eval_datasets, eval_pipelines, eval_runs, online_eval_configs, experiments,
     runtime_canaries, skill_lab_jobs, skill_lab_tasksets, managed_memories

resource_creators(id VARCHAR(32) PK, workspace_id VARCHAR(32) NOT NULL indexed,
                  kind VARCHAR(32), resource_id VARCHAR(128),
                  created_by VARCHAR(32) indexed, created_by_name VARCHAR(64), created_at)
  UNIQUE uq_resource_creators_workspace_kind_resource (workspace_id, kind, resource_id)
```

## 3. Contracts

- Response: every list + detail item of an in-scope kind carries `created_by: str|null`
  and `created_by_name: str|null`. Builtin / third-party evaluators, the bootstrap
  memory, unmanaged memories, experiment/canary-owned online configs and resources made
  outside the console return both null.
- Request: **no** request model carries either field; the server derives them.
- Creator source per path:

| Path | Creator |
|---|---|
| HTTP create route | `Creator.of_request(request)` |
| Auth disabled / config admin | `(None, auth_username)` — no `users` row |
| Architect evaluation-asset materializer (datasets, evaluators) | `Creator(op.approver_user_id, op.approved_by)` |
| Pipeline run creating its output dataset (thread) | whoever hit `/pipelines/{id}/run`, passed into the thread |
| Online insight report → eval run | caller of `/online/{id}/reports` |
| Memory adopt | `(None, adopter name)` — pre-existed in AWS, no owner |
| Skill Lab samples, bootstrap memory, `scripts/e2e_*` | nothing (NULL) |

- AWS-only kinds: `record` right after the AWS create returns (before any background
  thread); routers merge `lookup` into list/get; `forget` after a successful AWS delete
  (KB `knowledge.delete_kb`; evaluator DELETE route; materializer cleanup, both the
  deleted and already-gone branches).
- Mutations of an existing row (resume, recheck, stop, append, apply-expansion, PUT) never
  touch the creator.

## 4. Validation & Error Matrix

| Condition | Behavior |
|---|---|
| Client posts `created_by` / `created_by_name` | ignored (Pydantic drops unknown keys); response shows the real caller |
| `record` called twice for one resource / concurrent race | first row kept; `IntegrityError` rolled back silently |
| `record` with `creator=None` or empty id | no-op |
| Resource deleted outside the console | orphan `resource_creators` row; harmless, purged with the workspace |
| Creator user later deleted | id + name stay (no FK); display still shows the name |
| Legacy row (pre-migration) | both NULL → UI "—" |

## 5. Good / Base / Bad Cases

- Good: member `alice` creates a dataset → `{"created_by": "<alice users.id>", "created_by_name": "alice"}`.
- Base: dev stack with auth off creates a pipeline → `{"created_by": null, "created_by_name": "admin"}`.
- Bad: a KB created in the AWS console → both null; it must not be attributed to whoever
  lists it.

## 6. Tests Required

- `tests/test_resource_creators.py`: helper units; migration (legacy NULL, memory
  username → `users.id` for `origin='created'`, config admin → `(NULL, name)`,
  adopted → NULL id, second `_migrate` run is a no-op, `assert_no_schema_drift`);
  per kind: member → `(users.id, name)`, config admin / auth-disabled → `(None, name)`,
  forged body ignored; AWS-only: outside-console → nulls, delete removes the row.
- `tests/test_evaluation_assets.py`: materialized datasets + evaluators carry the
  approver; cleanup deletes evaluator creator rows; a resumed operation still records it.
- Member fixtures: copy the `app` / `admin` / `_member` pattern in
  `tests/test_api_key_lifecycle.py` (hermetic tests run auth-disabled by default).
- Frontend: `src/v2/creator.test.ts` — `creatorLabel` null → "—", `taskFromRun` /
  `taskFromOnline` map `createdByName`, `v2.common.creator` in en + zh-CN.

## 7. Wrong vs Correct

#### Wrong — f-string DDL, or a new workspace table registered in one place only

```python
conn.execute(text(f"ALTER TABLE {t} ADD COLUMN created_by VARCHAR(32)"))
# and resource_creators added to the model but not WORKSPACE_SCOPED_TABLES
```

`tests/test_ledger_migration.py` regex-scans `_migrate*` source for the **literal**
`ALTER TABLE <t> ADD COLUMN <c>`; an f-string column is never re-tested and the drift
guard fails startup on a ledger that lacks it. A `workspace_id` table missing from
`WORKSPACE_SCOPED_TABLES` / `_migrate_workspace_columns.additions` fails
`tests/test_workspaces.py:306/317`, and a unique index spanning `workspace_id` must also
be in the DROP list of `tests/test_workspaces.py::_pre_p2_database`.

#### Correct

```python
"eval_runs": {
    "created_by": "ALTER TABLE eval_runs ADD COLUMN created_by VARCHAR(32)",
    "created_by_name": "ALTER TABLE eval_runs ADD COLUMN created_by_name VARCHAR(64)",
},
```

plus `"resource_creators"` in `WORKSPACE_SCOPED_TABLES`, the literal
`ALTER TABLE resource_creators ADD COLUMN workspace_id VARCHAR(32)` in
`_migrate_workspace_columns`, and `_migrate_resource_creators_index`
(`CREATE UNIQUE INDEX IF NOT EXISTS`).

#### Wrong — recording the AWS-only creator inside a "first attempt" branch

Writing the `resource_creators` row only in the branch that runs when no id is stored yet
loses it if the worker dies between the AWS create checkpoint and the write. Record after
the branch, keyed on the stored id — `record` is idempotent.

### Design Decision: id + name, not name only

`created_by` (`users.id`) is the ownership key — stable across a deleted/recreated
username and what visibility filtering compares; `created_by_name` is display. Same as
API keys (#259). `managed_memories.created_by` previously held a username; the one-time
migration (guarded on `created_by_name` being absent) moves it to `created_by_name` and
re-points `created_by` via `users.username`.
