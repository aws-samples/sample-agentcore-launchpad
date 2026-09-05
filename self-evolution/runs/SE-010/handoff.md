# Direction SE-010 — Agent detail lists AWS versions and endpoints (Runtime + Harness)

You are the implementation session for one direction of the AgentCore Launchpad
self-evolution loop. You are in a dedicated git worktree on a branch named
`evo/se-010-…` (check `git branch --show-current`), created from `main`. A host session
wrote this brief, will independently re-run the acceptance checks on your branch, and
owns push, PR and merge. Work only inside this worktree.

## Requirement

The agent detail on `/create` (the agent list's details mode) gains a read-only **VERSIONS & ENDPOINTS** panel backed by AWS, for every runtime-backed or harness-backed agent.

- New console route `GET /api/agents/{agent_id}/versions` returns
  `{kind: "runtime"|"harness", versions: [{version, status, description, last_updated_at}], endpoints: [{name, live_version, target_version, status, description, created_at, last_updated_at, failure_reason?}], latest_version, ledger_version}` — following **every** AWS page internally (`nextToken`), so the console gets the complete set.
  - `method in {zip_runtime, studio, container}` and `discovered_runtime` rows whose `spec.discovery.resource_type` is absent or `runtime` → `ListAgentRuntimeVersions` + `ListAgentRuntimeEndpoints` on the agent's runtime id.
  - `method == harness` and `discovered_runtime` rows with `spec.discovery.resource_type == "harness"` → `ListHarnessVersions` + `ListHarnessEndpoints` on the harness id.
  - Any other shape (an agent with no AWS resource yet — status `deploying`/`failed` without a resource id — or an A2A/other row that cannot be resolved) → 409 `agent.no_resource` in the standard error envelope with a human reason the UI shows.
- Wrappers live in `backend/app/services/agentcore/runtime.py` (`list_runtime_versions`, `list_runtime_endpoints`) and `backend/app/services/agentcore/harness.py` (`list_harness_versions`, `list_harness_endpoints`), taking the control client explicitly (tests inject stubs); the projection is allow-listed — no environment values, artifact locations, execution roles or authorizer configuration leave the backend (same rule as `runtime_discovery.py`).
- The panel marks the `DEFAULT` endpoint, highlights the version the ledger recorded (`Agent.version`) vs AWS latest (a mismatch is visible, not an error), and flags the canary `stable`/`treatment` endpoint names when present so leftovers are visible. Loading / empty / error states use the shared components (`Panel`, `LoadError`); both locales; no new nested route (details mode already exists on `/create`).
- Docs: `docs/architecture.md` **and** `docs/architecture.zh-CN.md` (Runtime/Harness rows or the discovery section) plus `docs/api.md` **and** `docs/api.zh-CN.md` document the route.

Keep it strictly read-only: no re-pointing of `DEFAULT`, no endpoint create/update/delete from this panel (the canary owns those).

## Repository evidence and extension points

- `backend/app/services/agentcore/runtime.py:220-275` — `create_runtime_endpoint`, `update_runtime_endpoint`, `get_runtime_endpoint`, `delete_runtime_endpoint`, `wait_endpoint_ready`: endpoints are already a first-class wrapper concept; no list operation exists. Add the two list wrappers beside them.
- `backend/app/services/agentcore/harness.py:19-89` — `create_harness`, `update_harness`, `get_harness`, `list_harnesses`, `delete_harness`, `wait_harness_ready`; add the two list wrappers here. `HarnessSummary` has no `description` and uses `updatedAt` (not `lastUpdatedAt`) — read the model, do not assume the runtime shape.
- `backend/app/optimization/canary_infra.py:294-340` — `ensure_endpoint_ready` mints `stable`/`treatment` named endpoints; nothing lists them afterwards.
- `backend/app/deployer/zip_runtime.py:550-577`, `backend/app/deployer/container.py:249-268` — ledger `row.version` is set from `agentRuntimeVersion` at deploy time only.
- `backend/app/routers/agents.py:224` — `GET /agents/{agent_id}` exists (workspace-scoped lookup helper) — add the sibling route there and reuse its lookup.
- `backend/app/services/runtime_discovery.py:300-340` — the allow-listed projection style for runtime detail, and how discovered rows carry `spec.discovery.resource_type`.
- `backend/app/core/route_policy.py` enumerates every live route; `tests/test_route_policy.py` fails on drift — register the new GET with the same posture as `GET /agents/{agent_id}`.
- `frontend/src/pages/CreateAgent.tsx:2273-2380` — details mode panels (`create.launchPanel.*`, conversion panel `data-testid="conversion-panel"`, KB mounted panel) — insertion point; `frontend/src/lib/api.ts` is the single typed client.
- botocore `bedrock-agentcore-control/2023-06-05` (verify offline with `uv run python -c "import boto3; m=boto3.client('bedrock-agentcore-control', region_name='us-west-2').meta.service_model; print(m.operation_model('ListAgentRuntimeEndpoints').output_shape.members)"`):
  - `ListAgentRuntimeVersions(agentRuntimeId, maxResults, nextToken)` → `agentRuntimes[] {agentRuntimeArn, agentRuntimeId, agentRuntimeVersion, agentRuntimeName, description, lastUpdatedAt, status}`
  - `ListAgentRuntimeEndpoints(agentRuntimeId, …)` → `runtimeEndpoints[] {name, liveVersion, targetVersion, agentRuntimeEndpointArn, agentRuntimeArn, status, id, description, createdAt, lastUpdatedAt}`
  - `ListHarnessVersions(harnessId, …)` → `harnessVersions[]` (HarnessVersionSummaries — read the member names from the model)
  - `ListHarnessEndpoints(harnessId, …)` → `endpoints[] {harnessId, harnessName, endpointName, arn, status, createdAt, updatedAt, liveVersion, targetVersion, description, failureReason}`
- AWS docs (agent-runtime-versioning): every update creates an immutable version, `DEFAULT` auto-follows latest, named endpoints pin; endpoint states `CREATING, CREATE_FAILED, READY, UPDATING, UPDATE_FAILED`. Both list operations are available in us-west-2 and us-east-1.
- `docs/architecture.md` §The invoke chain: "AgentCore pins an existing runtime session to the version that first served it, so a post-republish validation must start a new Chat session" — the operator currently has no way to see the versions involved; that sentence is the natural docs anchor.

Load-bearing patterns from `CLAUDE.md`: all boto3 clients come from `app/services/aws_clients.py` (`tests/test_client_funnel.py` guards it); AgentCore client names and preview drift stay in `app/services/agentcore/`; errors go through `app/core/errors` (`AppError`); every user-facing string is an i18n key with en ↔ zh-CN parity and full-width zh-CN punctuation (`python3 scripts/i18n_check.py`, `python3 scripts/i18n_zh_punct.py --check`).

Read `CLAUDE.md`, then `docs/architecture.md` §Runtime/Harness rows, §Existing Runtime and Harness discovery and §The invoke chain, then the files above, before editing.

## Acceptance checks (the host re-runs these — make each one pass)

- [ ] `cd backend && uv run pytest tests/ -q -k versions` — hermetic tests with a stub control client: runtime agent → both runtime list ops called with the runtime id, two pages followed via `nextToken`, projection contains only the allow-listed keys (assert no `environmentVariables`/`roleArn`/`artifact`/`authorizerConfiguration` in the response); harness agent → harness ops called; discovered harness row → harness ops; agent without a resource → 409 `agent.no_resource`; agent from another workspace → 404.
- [ ] `cd backend && uv run pytest tests/test_route_policy.py tests/test_client_funnel.py -q` pass.
- [ ] Frontend: panel renders loading/empty/error; `DEFAULT` marked; ledger-vs-latest mismatch visible; `cd frontend && npx tsc --noEmit && npm run lint`; `python3 scripts/i18n_check.py` and `python3 scripts/i18n_zh_punct.py --check` clean.
- [ ] `docs/api.md` + `docs/api.zh-CN.md` document `GET /api/agents/{agent_id}/versions`; `docs/architecture.md` + `docs/architecture.zh-CN.md` updated.
- [ ] `make verify` passes in this worktree.
- [ ] Live AWS check: **not required by the gate** (read-only ops). Do not call AWS; say in the report that it was not exercised live.

## Boundaries

- Run EVERY command in the FOREGROUND — never `run_in_background`, never wait on a background task. In this non-interactive session your turn ends the moment you stop issuing foreground tool calls; an unfinished background `make verify` means the run ends with nothing committed.
- **Never** `git push`, open PRs, merge, rebase or force anything. Commit on the current branch with clear conventional messages; leave the tree clean (`git status --short` empty at the end).
- **Never** run `make bootstrap`, teardown scripts, `cdk deploy`, `make dev`, or anything against AWS or the production box. No AWS calls are needed; tests stub the client.
- Do not edit `apps/studio/`, `vendor/`, `vendor-src/`, `backend/samples/frontdesk_agent`.
- Do not widen scope. If the requirement turns out to be wrong or already covered, stop and say so in the report instead of building something adjacent.
- Commit only files you changed (`git add <paths>`), never `git add .` or `git add docs/`.
- Save any probe output to the ABSOLUTE host path `/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-010/`. Nothing under `.claude/` is committed.
- Stay within the budget cap the host set; if you are running out, commit what is verified and report what remains.

## Final report (the host reads only this)

End with exactly these sections:

1. **Changed** — files and what changed, one line each.
2. **Verified** — the commands you ran with their pass/fail outcome (paste the `make verify` tail).
3. **Acceptance checks** — the list above, each ✅/❌ with the evidence.
4. **Not done / deviations** — anything left, anything you interpreted differently, and why.
5. **Commits** — `git log --oneline main..HEAD`.
