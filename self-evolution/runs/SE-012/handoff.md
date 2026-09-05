# Direction SE-012 — Memory resources: edit description and event expiry (UpdateMemory)

You are the implementation session for one direction of the AgentCore Launchpad
self-evolution loop. You are in a dedicated git worktree on a branch named
`evo/se-012-…` (check `git branch --show-current`), created from `main`. A host session
wrote this brief, will independently re-run the acceptance checks on your branch, and
owns push, PR and merge. Work only inside this worktree.

## Requirement

The Memory console's `?view=resources` sub-page can edit an existing memory resource's **description** and **event expiry (days, 7–365)**.

- New route `PUT /api/memory/resources/{memory_id}` in `backend/app/routers/memory_resources.py` → `memory_admin.update_memory_resource(workspace, memory_id, *, description, event_expiry_days)` → `UpdateMemory` on the control client. Both fields optional; at least one required (422 otherwise); `event_expiry_days` outside 7–365 → 422. Strategies, namespace keys, execution role, indexed keys and stream delivery are **not** editable here. The response is the refreshed `_detail` projection (same shape as `GET /api/memory/resources/{memory_id}`), read back with `GetMemory` after the update.
- **Namespace-key trap (must be handled and tested):** the botocore model documents `UpdateMemory.namespaceKeys` as "This value fully replaces the existing set — any key you omit is removed." The update must therefore never send `namespaceKeys` (omit the member entirely) and the hermetic test asserts the `update_memory` call kwargs contain exactly `memoryId`, optional `description`, optional `eventExpiryDuration`, optional `clientToken` — nothing else. Leave a comment naming the fallback (re-send `GetMemory().namespaceKeys`) in case live verification later shows that omission also clears keys.
- Guard rails mirror delete: the workspace default memory is editable (description/expiry are harmless), but the UI says that an expiry change affects every agent using it; a memory referenced by live agents shows those agents in the confirm dialog (reuse `_agents_by_memory` from the router). No 409 on edit — only delete is blocked by in-use agents.
- The structural read-only guarantee stays: `tests/test_memory_console.py::test_console_exposes_no_memory_mutation` (or however it is named) must still pass — no `UpdateMemory`/`update_memory` token may appear in `app/services/memory_console.py` or `app/services/memory.py`; the admin pair (`memory_admin.py` + `routers/memory_resources.py`) is the only place it appears.
- UI (`frontend/src/pages/memory/ResourcesTab.tsx`): an EDIT action on each resource row opens an inline form (description, expiry days) reusing the create form's field components, with the shared `Btn` (`disabledReason` when nothing changed / invalid expiry / request in flight) and `ConfirmDialog` (listing referencing agents when any); toast on success; row refreshes from the response. Both locales.
- Docs: `docs/architecture.md` **and** `docs/architecture.zh-CN.md` Memory console table `resources` row mention update; `docs/api.md` **and** `docs/api.zh-CN.md` document `PUT /api/memory/resources/{memory_id}`.
- IAM: **no IAM change.** The console runs on the instance role (hub) or the spoke workspace role, which already grants `bedrock-agentcore:*`; do not touch `workspace_iam.py`, `infra/`, or the per-agent execution-role grants.

## Repository evidence and extension points

- `backend/app/routers/memory_resources.py:15-77` — pydantic request models (`NamespaceKeyInput`, `CreateMemoryResourceRequest` with `field_validator`s) and `_guard(fn, …)` that maps botocore failures to the memory error codes; `:109-160` — GET list, POST create, GET one, DELETE with the `memory.in_use` 409 guard built on `_agents_by_memory(db, ws)`. Add `UpdateMemoryResourceRequest` + the PUT here.
- `backend/app/services/memory_admin.py:93-103` — `_detail` exposes `description`, `event_expiry_days` (from `eventExpiryDuration`); `:132-156` `list_memory_resources`/`get_memory_resource`; `:156-201` `create_memory_resource` builds `eventExpiryDuration`, `description`, `memoryStrategies`, namespace keys — mirror its client acquisition and return shape; `:234` `delete_memory_resource`.
- `backend/app/core/route_policy.py:281-284` — the four `/api/memory/resources*` entries are `MEMBER`; add `("PUT", "/api/memory/resources/{memory_id}")` with the same posture; `tests/test_route_policy.py` fails on drift.
- `backend/tests/test_memory_resources.py` — existing hermetic tests with a stub control client (`configured` fixture, `monkeypatch`); add the update tests there. `backend/tests/test_memory_console.py` — the structural read-only assertion.
- `frontend/src/pages/memory/ResourcesTab.tsx:74-155` (create form state: `description`, `expiryDays`, `strategies`, `SHOW_NS_KEYS`), `:282-360` (inputs for name/description/expiry/strategies) — reuse for the edit form. `frontend/src/lib/api.ts` is the single typed client — add `updateMemoryResource`.
- `docs/architecture.md` §The Memory console (console 05) — "Read-only is structural … no wrapper or handler for … `UpdateMemory` … exists in either file, and `tests/test_memory_console.py` asserts that. The one mutating surface — the `resources` view — therefore lives in a separate pair (`services/memory_admin.py` + `routers/memory_resources.py`)". This direction extends that pair only; the table row for `resources` lists the AgentCore operations — add `UpdateMemory`.
- botocore `bedrock-agentcore-control/2023-06-05` `UpdateMemory` members: `clientToken, memoryId*, description, eventExpiryDuration (7–365), memoryExecutionRoleArn, memoryStrategies, addIndexedKeys, namespaceKeys ("fully replaces the existing set — any key you omit is removed"), streamDeliveryResources`. Verify offline: `cd backend && uv run python -c "import boto3; print(boto3.client('bedrock-agentcore-control', region_name='us-west-2').meta.service_model.operation_model('UpdateMemory').input_shape.members['namespaceKeys'].documentation)"`.
- `UpdateMemory` is available in us-west-2 and us-east-1 (checked 2026-09-05).

Load-bearing patterns from `CLAUDE.md`: all boto3 clients come from `app/services/aws_clients.py` (`tests/test_client_funnel.py` guards it); errors go through `app/core/errors` (`AppError`) and the router's `_guard`; every user-facing string is an i18n key with en ↔ zh-CN parity and full-width zh-CN punctuation (`python3 scripts/i18n_check.py`, `python3 scripts/i18n_zh_punct.py --check`).

Read `CLAUDE.md`, then `docs/architecture.md` §The Memory console (console 05), then the files above, before editing.

## Acceptance checks (the host re-runs these — make each one pass)

- [ ] `cd backend && uv run pytest tests/test_memory_resources.py tests/test_memory_console.py -q` — new tests: PUT with description only / expiry only / both → `update_memory` kwargs exactly `{memoryId, [description], [eventExpiryDuration], [clientToken]}` (assert the key set — no `namespaceKeys`, no `memoryStrategies`); expiry 6 or 366 → 422; neither field → 422; unknown memory (stub raises `ResourceNotFoundException`) → the existing not-found mapping; the response is the refreshed `_detail` projection read via `get_memory` after the update; the structural read-only test still passes.
- [ ] `cd backend && uv run pytest tests/test_route_policy.py tests/test_client_funnel.py -q` pass.
- [ ] Frontend: EDIT opens the inline form pre-filled, SAVE disabled with a reason until a field changes and is valid, confirm dialog lists referencing agents, row refreshes; `cd frontend && npx tsc --noEmit && npm run lint`; `python3 scripts/i18n_check.py` and `python3 scripts/i18n_zh_punct.py --check` clean.
- [ ] `docs/architecture.md` + `docs/architecture.zh-CN.md` Memory console `resources` row lists update (+ `UpdateMemory` in the operations column); `docs/api.md` + `docs/api.zh-CN.md` document `PUT /api/memory/resources/{memory_id}`.
- [ ] `make verify` passes in this worktree.
- [ ] Live AWS check: **declared, not required by the gate** — the host may later update a dev memory that has one namespace key and confirm `GetMemory().namespaceKeys` is unchanged. Do not call AWS; say in the report that it was not exercised live.

## Boundaries

- Run EVERY command in the FOREGROUND — never `run_in_background`, never wait on a background task. In this non-interactive session your turn ends the moment you stop issuing foreground tool calls; an unfinished background `make verify` means the run ends with nothing committed.
- **Never** `git push`, open PRs, merge, rebase or force anything. Commit on the current branch with clear conventional messages; leave the tree clean (`git status --short` empty at the end).
- **Never** run `make bootstrap`, teardown scripts, `cdk deploy`, `make dev`, or anything against AWS or the production box. No AWS calls are needed; tests stub the client.
- Do not edit `apps/studio/`, `vendor/`, `vendor-src/`, `backend/samples/frontdesk_agent`, `infra/`, or `workspace_iam.py`.
- Do not widen scope. If the requirement turns out to be wrong or already covered, stop and say so in the report instead of building something adjacent.
- Commit only files you changed (`git add <paths>`), never `git add .` or `git add docs/`.
- Save any probe output to the ABSOLUTE host path `/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-012/`. Nothing under `.claude/` is committed.
- Stay within the budget cap the host set; if you are running out, commit what is verified and report what remains.

## Final report (the host reads only this)

End with exactly these sections:

1. **Changed** — files and what changed, one line each.
2. **Verified** — the commands you ran with their pass/fail outcome (paste the `make verify` tail).
3. **Acceptance checks** — the list above, each ✅/❌ with the evidence.
4. **Not done / deviations** — anything left, anything you interpreted differently, and why.
5. **Commits** — `git log --oneline main..HEAD`.
