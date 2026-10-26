# Spec Snapshots and Rollback

> Introduced by task `10-14-v2-env-snapshots` (step 4d of `10-13-v3-to-v2-port`). Every publish
> leaves a numbered snapshot of the agent's spec. A user can diff two snapshots, and anyone who
> can redeploy the agent can roll it back to an earlier one. Ported from `origin/console-v3`
> without tiers, the prod lock, promotions or DLC.
>
> **Environment compare / drift was removed (user decision, 2026-10-15).** #277 shipped a
> `/v2/environments` page (cross-workspace compare + an AWS version drift check), #280 hid its
> drift tab, and the follow-up removed the page, its nav entry, `services/environments.py`,
> `routers/environments.py`, `/api/environments/*` and their tests entirely. Restore from git
> history if it is ever wanted again.

## 1. Scope / Trigger

This spec applies when you:
- add a code path that changes `Agent.spec`, especially one that bypasses `create_deployment`;
- touch `deployer/pipeline.create_deployment` or `_finish`;
- change `republish_agent` or a redeploy guard. Rollback inherits every one of them;
- add a spec field that is a deploy-time stamp, or a field that should get a diff group;
- touch `services/snapshots.py` or the V2 `SnapshotsCard`.

## 2. Contracts

### Snapshots are written in one place

- **`create_deployment(..., actor: Creator | None, note: str | None)`** calls
  `snapshots.record_snapshot` after `deployment.job_id = job.id`. The row is flushed in the
  **caller's transaction** and never committed on its own, so `commit=False` callers (architect
  approve, preset release) commit it together with their rows.
  - The Job payload carries `actor_name` and `note`.
  - A resumed job never re-enters `create_deployment`, so it never records a second snapshot.
- **`_finish` on success** calls `stamp_deployed(db, deployment_id, agent)`. It sets
  `aws_version = agent.version` **and** `spec = deepcopy(agent.spec)`, which is the spec as
  deployed. The architect's harness stage rewrites `spec.skills` to immutable copy URIs
  mid-job, and a rollback must re-deploy what actually ran.
  - A failed deploy leaves `aws_version` null.
  - A deployment with no snapshot (a job that predates the table) is ignored.
- **Spec writers outside `create_deployment` record their own snapshot,** stamped with the
  version they leave live:
  - `canary_service` Runtime promote: note `canary promoted`;
  - Harness canary rollback: note `canary rolled back`.

  **A new writer of `Agent.spec` that bypasses the pipeline must do the same,** or the history
  has a hole.
- **`backfill_baselines`** runs at startup in `create_app`'s `resume_jobs` block, listed in
  `inflight.STARTUP_HOOKS`.
  - Every active agent with a `resource_id`, `method != discovered_runtime` and no snapshot
    gets seq 1 with `note="baseline"`, its current spec, `aws_version = agent.version` and its
    creator.
  - It is ledger-only and idempotent. It never raises: a failure is rolled back, logged, and
    retried next startup.
- **Table:** `spec_snapshots` is workspace-scoped, with `UNIQUE(agent_id, seq)` and the creator
  convention (`created_by` = `users.id`, `created_by_name`). A console-v3 ledger (prod2) has the
  table with a username in `created_by`. `core/db._migrate_branch_creators`, shared with
  `alert_rules`, converts it once.

- **Evaluation runs consume the timeline** ([Evaluation Scorecards](./eval-scorecard.md)): a run stamps the newest snapshot with an `aws_version`, and legacy runs infer theirs from `aws_version` + `Deployment.ended_at`. A new snapshot writer that leaves `aws_version` null on success breaks that attribution.

### Routes

| Route | Policy | Notes |
|---|---|---|
| `GET /api/agents/{id}/snapshots` | MEMBER | newest first; the `spec` column is **not loaded** (`defer`) |
| `GET /api/agents/{id}/snapshots/diff` | MEMBER | declared **before** `/{seq}`; an omitted side = the current spec; both omitted → 422 `snapshot.diff_side_required` |
| `GET /api/agents/{id}/snapshots/{seq}` | MEMBER | with `spec`; 404 `snapshot.not_found` |
| `POST /api/agents/{id}/snapshots/{seq}/rollback` | `perm:agents.deploy` | 202, same as redeploy |

- **Visibility:** every route resolves the agent with `_agent_in(db, ws, id, viewer)`, so a
  hidden or other-workspace agent is 404 `agent.not_found`. The NULL-creator rule applies:
  legacy or auth-off agents stay visible in isolated mode.
- **Diff:** nested fields by dotted path, and lists by membership (`added`/`removed`).
  - `code`, `code_bundle`, `studio_flow` and `system_prompt` are compared whole. A body over
    2 KB comes back as `{length}` with `elided: true`.
  - Groups, in display order: prompt, model, tools, skills, knowledge_bases, memory,
    guardrail, other. A new top-level spec field that belongs to one goes into `_GROUPS`.

### Rollback = an ordinary redeploy of the stored spec

**Order of checks:**
1. `refuse_system_mutation`;
2. `_refuse_redeploy`: discovered, deploy in flight, purge;
3. `get_snapshot` → 404;
4. `aws_version` null → 409 `snapshot.never_deployed`;
5. `AgentSpec.model_validate` fails → 409 `snapshot.spec_invalid` `{errors}`;
6. `stale_kb_ids` → 409 `snapshot.stale_reference` `{kb_ids}`;
7. `republish_agent(actor=Creator.of_request, note="rollback to #k")`.

- **`stale_kb_ids`** reads only the KB ids the target adds relative to the current spec, through
  `agent_client(workspace)`. It is usually zero calls.
- **`republish_agent`** applies every redeploy guard: name or method immutability,
  `converted_locked`, added-pin visibility, tool auth and memory ownership.
- **The result is an update-mode job:** a **new** AWS version and a new snapshot. Nothing is
  reverted in place on AWS.
- **Shared AWS:** on prod and prod2 a rollback changes the agent for both consoles. That is why
  the UI confirm says so.

### Frontend

- **`v2/pages/agents/SnapshotsCard.tsx`** mounts after `VersionsCard` for every agent that is
  not a discovered runtime.
  - Compare takes exactly two rows.
  - **Roll back** shows only on `deployed` rows other than the newest, and only when
    `perms.canEdit(agent) && !agent.system && method !== discovered_runtime`.
  - The confirm loads `snapshotDiff(id, undefined /* current */, seq)` and shows the changes
    from current to target, plus the shared-AWS warning.
  - On success it bumps the page nonce, so the deploy line and log appear.
  - The pure logic lives in `snapshots.ts`, tested.

## 3. Tests

- `tests/test_spec_snapshots.py` covers:
  - a snapshot on create and on every redeploy; the stamp and the as-deployed refresh; nothing
    on failure; none on resume;
  - the diff: field-level, list membership, against the current spec, and elision;
  - rollback as a new snapshot (update mode, actor, note);
  - each refusal: unknown seq, never deployed, invalid spec, stale KB, in flight, system preset,
    discovered, converted locked;
  - a member without `agents.deploy` gets 403, and a hidden or other-workspace agent gets 404;
  - the backfill (once, with skips), and the console-v3 table migration.
- `tests/optimization/test_runtime_canaries.py` and `test_harness_canaries.py` assert the
  canary snapshots.
- Vitest: `v2/pages/agents/snapshots.test.ts`.

## 4. Pitfalls

- **Don't add a `record_snapshot` call next to a `create_deployment` call.** The pipeline
  already records one, and a second gives two rows per publish.
- **Don't read the pre-job spec as "what ran".** A stage may rewrite `agent.spec`. The success
  stamp is the source of truth.
- **Don't add a rollback-only bypass of a redeploy guard.** If rollback needs an exception,
  the guard is wrong for redeploy too.
- **Isolated visibility has a NULL-creator escape hatch.** On an auth-off dev box every agent
  stays visible to members. To exercise isolation live, the agent must be owned by another
  `users.id`.
- **A Harness prompt read back from AWS includes the platform KB section.** Strip it with
  `deployer.harness.strip_generated_prompt` before storing it in `Agent.spec` or a snapshot,
  as the canary rollback (`canary_service._ledger_prompt`) and recommendation accept do.
  Otherwise the next publish, or a rollback to that snapshot, appends the section twice.
- **Times go on the wire with a UTC offset.** SQLite drops the zone, so `_iso` adds `UTC`.
- **A BYOC `container_image` given by tag redeploys whatever the tag points at now.** The old
  `byoc.provenance` digest in a snapshot is a record only. It is re-stamped by the generate
  stage and does not pin the image.
