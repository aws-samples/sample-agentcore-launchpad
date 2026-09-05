## Summary

Self-evolution direction **SE-011 — Chat can end the AgentCore Runtime session (StopRuntimeSession)** (branch `evo/se-011-chat-can-end-the-agentcore-runtime-sessi`).

### Reviewer notes (host acceptance)

- **IAM widening to decide on merge:** `bedrock-agentcore:StopRuntimeSession` was added to the shared execution role's `ABTestOrchestration` statement (`infra/stacks/base_stack.py` + the derived policy in `workspace_iam.py`, docs count 18 → 19), next to the existing `InvokeAgentRuntime`. The console itself does not need it (hub console runs on the instance role; the spoke role already grants `bedrock-agentcore:*`), so this hunk can be dropped without breaking the feature — see the note in the docs' "Two grants were removed" paragraph on why platform actions on that role are already a known compromise.
- `RetryableConflictException` added to the closed `AWS_ERROR_MAP` (409 `aws.conflict`); the closed-list test and api docs updated.
- A new turn posted under an ended session id revives the row (`ended_at` cleared) because AgentCore starts a fresh session with the same id.
- Host rerun: `make verify` PASS; `pytest -k "session and stop"` 17 passed; route_policy + client_funnel pass. Live `StopRuntimeSession` **not exercised** (declared not required by the gate).

### Requirement

Chat can end the live AgentCore Runtime session behind a conversation, instead of only forgetting it locally.

- New console route `POST /api/chat/{agent_id}/sessions/{session_id}/stop` calls `StopRuntimeSession(agentRuntimeArn=<agent's runtime ARN>, runtimeSessionId=<session_id>)` through a new wrapper in `backend/app/services/agentcore/runtime.py` (data-plane `bedrock-agentcore` client from `aws_clients.py`), only for runtime-backed agents (`zip_runtime`, `studio`, `container`, `discovered_runtime` with `resource_type` runtime). Harness agents and A2A/discovered-harness rows get 409 `chat.session_stop_unsupported` with a reason (there is no harness session-stop operation in the model).
- Semantics: AWS `ResourceNotFoundException` (session already gone / idle-expired) is reported as success with `already_ended: true`, not an error; `RetryableConflictException` is left to the SDK's default retries and, if it still surfaces, maps to 409 via the existing ClientError → envelope mapping. The ledger `ChatSession` row is kept (history stays replayable) and gets an `ended_at` timestamp (new nullable column, additive migration consistent with how the ledger evolves today) so the history rail can show "ended".
- UI (`frontend/src/pages/Chat.tsx`): an **END SESSION** button next to **NEW SESSION** for the current session, and a per-row action in the history rail; both use the shared `Btn` with `disabledReason` (no session yet / harness agent / already ended) and a toast on success. **NEW SESSION** itself keeps its current behaviour (does not auto-stop) — ending is explicit.
- Optional, if cheap: the Observability session detail shows the same action when the session resolves to a runtime-backed Launchpad agent.
- `docs/api.md` (+ zh-CN) documents the route; `docs/architecture.md` (Chat / invoke chain or Memory console section that talks about sessions) mentions explicit session end and the version-pinning motivation.

### Evidence

- `frontend/src/pages/Chat.tsx:310-318` — `newSession` only resets local state; `:413-421` current-session chip + NEW SESSION button; `:495-520` history rail rows (`restoreSession`).
- `backend/app/routers/chat.py:201-253` — `list_sessions` (ledger `ChatSession` rows); no mutation route for a session.
- `backend/app/services/agentcore/runtime.py` — control-plane wrappers only; `backend/app/services/invoke.py` / `chat.py` own the data-plane `InvokeAgentRuntime` call — put `stop_runtime_session(client, *, runtime_arn, session_id, qualifier=None)` next to the other runtime wrappers and pass the data-plane client explicitly.
- `backend/app/models/ledger.py` — `ChatSession` (session_id, actor_id, turns, last_at, workspace_id).
- botocore `bedrock-agentcore/2024-02-28`: `StopRuntimeSession` input `{runtimeSessionId*, agentRuntimeArn*, qualifier, clientToken}` → `{runtimeSessionId, statusCode}`, `POST /runtimes/{agentRuntimeArn}/stopruntimesession`; no `*Harness*Session*` operation exists in either model.
- Docs (accessed 2026-09-05) https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-stop-session.html — "instantly terminates the specified session and stops any ongoing streaming responses"; scenarios: user-initiated end, quota management, stalled sessions; 404 = "Session not found or already terminated"; 409 `RetryableConflictException` is transient and auto-retried by SDKs. Availability: `Bedrock AgentCore+StopRuntimeSession` isAvailableIn us-west-2 and us-east-1.
- `docs/architecture.md` §The invoke chain — sessions are pinned to the version that first served them; explicit end + new session is the documented validation recipe after a re-publish.
- IAM: the console's caller role needs `bedrock-agentcore:StopRuntimeSession`; check `backend/app/services/workspace_iam.py` (derived workspace policies list allowed actions, e.g. `:197` `UpdateGateway`) and `infra/` for where console-side AgentCore actions are granted, and add the action there.

### Acceptance checks

- [ ] `cd backend && uv run pytest tests/ -q -k "session and stop"` — hermetic tests with a stubbed data-plane client: runtime agent → `stop_runtime_session` called with the agent's runtime ARN + session id, 200 `{ended: true, already_ended: false}`, `ChatSession.ended_at` set; stub raising `ResourceNotFoundException` → 200 `already_ended: true`; harness agent → 409 `chat.session_stop_unsupported`; session from another workspace/agent → 404.
- [ ] `tests/test_client_funnel.py` passes (no new `boto3.client`).
- [ ] Frontend: END SESSION disabled with a reason when no session / harness agent; enabled after a turn; history rows show ended state; `npx tsc --noEmit && npm run lint`; i18n parity.
- [ ] Docs: `docs/api.md` + zh-CN route entry; `docs/architecture.md` sentence on explicit session end; if IAM changed, the grants table / `workspace_iam.py` note.
- [ ] `make verify` passes.
- [ ] Live AWS check: **declared, not required by the gate** — a real stop on a dev-account runtime session (invoke once, stop, invoke again with the same session id and observe a fresh session) is left for the host. Record as not run.

### Notes

- Do not stop sessions from `/v1` (public API) in this direction; console only.
- Register the new POST in `route_policy.py` if that file gates routes.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
