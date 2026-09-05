## Summary

Self-evolution direction **SE-010 — Agent detail lists AWS versions and endpoints (Runtime + Harness)** (branch `evo/se-010-agent-detail-lists-aws-versions-and-endp`).

### Requirement

The agent detail on `/create` (the list's details mode) gains a read-only **VERSIONS & ENDPOINTS** panel backed by AWS, for every runtime-backed or harness-backed agent.

- New console route `GET /api/agents/{agent_id}/versions` returns `{kind: "runtime"|"harness", versions: [{version, status, description, last_updated_at}], endpoints: [{name, live_version, target_version, status, description, created_at, last_updated_at, failure_reason?}], latest_version, ledger_version, next_token?}` — following every page (AWS caps pages; round-trip `next_token` like the Memory console does).
  - `method in {zip_runtime, studio, container, discovered_runtime(resource_type runtime)}` → `ListAgentRuntimeVersions` + `ListAgentRuntimeEndpoints` on the agent's runtime id.
  - `method == harness` or a discovered harness → `ListHarnessVersions` + `ListHarnessEndpoints` on the harness id.
  - Any other shape (e.g. an agent with no AWS resource yet, status `deploying`/`failed` without an ARN) → 409 `agent.no_resource` in the standard error envelope with a reason the UI can show.
- Wrappers live in `backend/app/services/agentcore/runtime.py` (`list_runtime_versions`, `list_runtime_endpoints`) and `backend/app/services/agentcore/harness.py` (`list_harness_versions`, `list_harness_endpoints`), taking the control client explicitly; the projection is allow-listed (no environment values, artifact locations or authorizer config — same rule as discovery).
- The panel marks the `DEFAULT` endpoint, highlights the version the ledger recorded (`Agent.version`) vs AWS latest, and flags the canary `stable`/`treatment` endpoint names when present so leftovers are visible. Loading / empty / error states use the shared `Panel`/`LoadError`; both locales; no new nested route (details mode already exists).
- `docs/architecture.md` (Runtime + Harness rows or the discovery section) and `docs/api.md` (+ zh-CN twin) document the route.

### Evidence

- `backend/app/services/agentcore/runtime.py:220-275` — `create_runtime_endpoint`, `update_runtime_endpoint`, `get_runtime_endpoint`, `delete_runtime_endpoint`, `wait_endpoint_ready`: endpoints are already a first-class wrapper concept; no list operation exists.
- `backend/app/optimization/canary_infra.py:294-340` — `ensure_endpoint_ready` mints `stable`/`treatment` named endpoints; nothing lists them afterwards.
- `backend/app/deployer/zip_runtime.py:550-577`, `backend/app/deployer/container.py:249-268` — ledger `row.version` is set from `agentRuntimeVersion` at deploy time only.
- `backend/app/routers/agents.py:224` — `GET /agents/{agent_id}` exists; add the sibling route there.
- `backend/app/services/agentcore/harness.py:19-89` — harness wrappers (`get_harness`, `list_harnesses`, …); add the list-versions/list-endpoints siblings.
- `frontend/src/pages/CreateAgent.tsx:2273-2380` — details mode panels (`create.launchPanel.*`, conversion panel, KB mounted panel) — insertion point.
- botocore `bedrock-agentcore-control/2023-06-05`: `ListAgentRuntimeVersions(agentRuntimeId, maxResults, nextToken)` → `agentRuntimes[] {agentRuntimeArn, agentRuntimeId, agentRuntimeVersion, agentRuntimeName, description, lastUpdatedAt, status}`; `ListAgentRuntimeEndpoints(agentRuntimeId, …)` → `runtimeEndpoints[] {name, liveVersion, targetVersion, agentRuntimeEndpointArn, agentRuntimeArn, status, id, description, createdAt, lastUpdatedAt}`; `ListHarnessVersions(harnessId, …)` → `harnessVersions[]`; `ListHarnessEndpoints(harnessId, …)` → `endpoints[] {harnessId, harnessName, endpointName, arn, status, createdAt, updatedAt, liveVersion, targetVersion, description, failureReason}`.
- Docs (accessed 2026-09-05) https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agent-runtime-versioning.html — every update creates an immutable version, `DEFAULT` auto-follows latest, named endpoints pin; endpoint states `CREATING, CREATE_FAILED, READY, UPDATING, UPDATE_FAILED`. Regional availability: `ListAgentRuntimeVersions` isAvailableIn us-west-2 and us-east-1.
- `docs/architecture.md` §The invoke chain: "AgentCore pins an existing runtime session to the version that first served it, so a post-republish validation must start a new Chat session" — the operator currently has no way to see the versions involved.

### Acceptance checks

- [ ] `cd backend && uv run pytest tests/ -q -k versions` — hermetic tests with a stub control client: runtime agent → both list ops called with the runtime id, pages followed via `nextToken`, projection contains only the allow-listed keys; harness agent → harness ops called; agent without a resource → 409 `agent.no_resource`; workspace scoping honoured (agent from another workspace → 404).
- [ ] `tests/test_client_funnel.py` still passes (no new `boto3.client`).
- [ ] Frontend: panel renders loading/empty/error; `DEFAULT` marked; ledger-vs-latest mismatch visible; `npx tsc --noEmit && npm run lint`; i18n parity.
- [ ] `docs/api.md` + zh-CN twin document `GET /api/agents/{agent_id}/versions`; `docs/architecture.md` updated.
- [ ] `make verify` passes.
- [ ] Live AWS check: **not required by the gate** (read-only ops; host may spot-check against a dev runtime later).


## Verification

```
── ruff: OK
── pytest: OK
── infra ruff: OK
── infra pytest: OK
── local lifecycle: OK
── eslint: OK
── tsc: OK
── vite build: OK
── i18n_check: OK
── i18n_zh_punct: OK
════ verify: PASS ════
```

🤖 Generated with [Claude Code](https://claude.com/claude-code)
