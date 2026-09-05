# Direction SE-011 — Chat can end the AgentCore Runtime session (StopRuntimeSession)

You are the implementation session for one direction of the AgentCore Launchpad
self-evolution loop. You are in a dedicated git worktree on a branch named
`evo/se-011-…` (check `git branch --show-current`), created from `main`. A host session
wrote this brief, will independently re-run the acceptance checks on your branch, and
owns push, PR and merge. Work only inside this worktree.

## Requirement

Chat can end the live AgentCore Runtime session behind a conversation, instead of only forgetting it locally.

- New console route `POST /api/chat/{agent_id}/sessions/{session_id}/stop` calls `StopRuntimeSession(agentRuntimeArn=<agent's runtime ARN>, runtimeSessionId=<session_id>)` through a new wrapper `stop_runtime_session(client, *, runtime_arn, session_id, qualifier=None)` in `backend/app/services/agentcore/runtime.py`, using the **data-plane** `bedrock-agentcore` client obtained from `app/services/aws_clients.py` the same way the invoke chain obtains it. Only runtime-backed agents qualify: `method in {zip_runtime, studio, container}` and `discovered_runtime` rows whose `spec.discovery.resource_type` is absent or `runtime`. Harness agents (`method == harness`) and discovered harness rows get 409 `chat.session_stop_unsupported` with a reason — there is no harness session-stop operation in the model (verify: no operation name containing both `Harness` and `Session` in `bedrock-agentcore` or `bedrock-agentcore-control`).
- Semantics: AWS `ResourceNotFoundException` (session already gone / idle-expired) is reported as success with `{"ended": true, "already_ended": true}`, not an error; `RetryableConflictException` is left to the SDK's default retries and, if it still surfaces, maps to 409 via the existing ClientError → envelope mapping (`app/core/errors.py`, `AWS_ERROR_MAP`). The ledger `ChatSession` row is kept (history stays replayable) and gets an `ended_at` timestamp (new nullable column, additive — follow how other nullable ledger columns were added, e.g. a startup `ALTER TABLE … ADD COLUMN` guard if the repo does that; check `app/core/db.py` / `app/models/ledger.py` for the existing pattern) so the history rail can show "ended".
- UI (`frontend/src/pages/Chat.tsx`): an **END SESSION** button next to **NEW SESSION** for the current session, and a per-row action in the history rail; both use the shared `Btn` with `disabledReason` (no session yet / harness agent / already ended / request in flight) and a toast on success. **NEW SESSION** keeps its current behaviour (does not auto-stop) — ending is explicit. After a successful end, the current session id is cleared the same way NEW SESSION does it, so the next prompt starts a fresh AgentCore session.
- `GET /api/chat/{agent_id}/sessions` includes `ended_at` per row.
- Docs: `docs/api.md` **and** `docs/api.zh-CN.md` document the route; `docs/architecture.md` **and** `docs/architecture.zh-CN.md` (the §The invoke chain paragraph about version pinning, or the chat/session section) mention explicit session end and the version-pinning motivation.
- IAM: the console's caller needs `bedrock-agentcore:StopRuntimeSession`. Find where console-side AgentCore actions are granted for the workspace/console role (`backend/app/services/workspace_iam.py` derived policies — see the action lists around `:197`; and `infra/` for the base stack role) and add the action alongside `InvokeAgentRuntime`; update any grants table in docs that enumerates actions. Do not deploy.

Out of scope: `/v1` public API, Observability session detail action, stopping harness sessions.

## Repository evidence and extension points

- `frontend/src/pages/Chat.tsx:310-318` — `newSession` only resets local state; `:413-421` current-session chip + NEW SESSION button; `:495-520` history rail rows (`restoreSession`). `frontend/src/lib/api.ts` is the single typed client (the sessions call is currently a raw `fetch` at `Chat.tsx:187` — you may route the new call through `api.ts`).
- `backend/app/routers/chat.py:201-253` — `list_sessions` (ledger `ChatSession` rows, workspace-scoped via `require_workspace` + `_agent_in`); no mutation route for a session. Add the stop route here.
- `backend/app/services/agentcore/runtime.py` — control-plane wrappers (`get_runtime`, endpoints…); `backend/app/services/invoke.py` / `app/services/chat.py` own the data-plane `InvokeAgentRuntime` call and show how the data-plane client and the agent's runtime ARN are obtained — mirror that.
- `backend/app/models/ledger.py` — `ChatSession` (session_id, actor_id, turns, last_at, workspace_id, agent_id).
- `backend/app/core/route_policy.py` enumerates every live route; `tests/test_route_policy.py` fails on drift — register the new POST with the same posture as the chat POST routes.
- `backend/app/core/errors.py` — `AWS_ERROR_MAP` closed list of mapped ClientError codes; `ResourceNotFoundException` is already mapped to 404 there — the route must catch it **before** the generic mapping to return the `already_ended` success shape.
- botocore `bedrock-agentcore/2024-02-28`: `StopRuntimeSession` input `{runtimeSessionId*, agentRuntimeArn*, qualifier, clientToken}` → `{runtimeSessionId, statusCode}`, `POST /runtimes/{agentRuntimeArn}/stopruntimesession`. Verify offline with `uv run python -c "import boto3; print(boto3.client('bedrock-agentcore', region_name='us-west-2').meta.service_model.operation_model('StopRuntimeSession').input_shape.members)"`.
- AWS docs (runtime-stop-session): "instantly terminates the specified session and stops any ongoing streaming responses"; scenarios: user-initiated end, quota management, stalled sessions; 404 = "Session not found or already terminated"; 409 `RetryableConflictException` is transient and auto-retried by SDKs. Available in us-west-2 and us-east-1.
- `docs/architecture.md` §The invoke chain — sessions are pinned to the version that first served them; explicit end + new session is the documented validation recipe after a re-publish.

Load-bearing patterns from `CLAUDE.md`: all boto3 clients come from `app/services/aws_clients.py` (`tests/test_client_funnel.py` guards it); AgentCore client names and preview drift stay in `app/services/agentcore/`; one invoke chain — do not touch `invoke_agent_text`/`chat_stream` behaviour; errors go through `app/core/errors` (`AppError`); every user-facing string is an i18n key with en ↔ zh-CN parity and full-width zh-CN punctuation (`python3 scripts/i18n_check.py`, `python3 scripts/i18n_zh_punct.py --check`).

Read `CLAUDE.md`, then `docs/architecture.md` §The invoke chain and §The SQLite ledger and job/event model, then the files above, before editing.

## Acceptance checks (the host re-runs these — make each one pass)

- [ ] `cd backend && uv run pytest tests/ -q -k "session and stop"` (name your tests so this selects them) — hermetic tests with a stubbed data-plane client: runtime agent → `stop_runtime_session` called with the agent's runtime ARN + session id, 200 `{ended: true, already_ended: false}`, `ChatSession.ended_at` set; stub raising `ResourceNotFoundException` → 200 `already_ended: true`; harness agent → 409 `chat.session_stop_unsupported`; session belonging to another agent or workspace → 404; `list_sessions` returns `ended_at`.
- [ ] `cd backend && uv run pytest tests/test_route_policy.py tests/test_client_funnel.py -q` pass.
- [ ] Frontend: END SESSION disabled with a reason when no session / harness agent; enabled after a turn; history rows show ended state; `cd frontend && npx tsc --noEmit && npm run lint`; `python3 scripts/i18n_check.py` and `python3 scripts/i18n_zh_punct.py --check` clean.
- [ ] Docs: `docs/api.md` + `docs/api.zh-CN.md` route entry; `docs/architecture.md` + `docs/architecture.zh-CN.md` sentence on explicit session end; IAM action added where console actions are granted (and the docs grants table if one enumerates them).
- [ ] `make verify` passes in this worktree (includes infra ruff+pytest if you touched `infra/`).
- [ ] Live AWS check: **declared, not required by the gate** — a real stop on a dev-account runtime session is left for the host. Do not call AWS; say in the report that it was not exercised live.

## Boundaries

- Run EVERY command in the FOREGROUND — never `run_in_background`, never wait on a background task. In this non-interactive session your turn ends the moment you stop issuing foreground tool calls; an unfinished background `make verify` means the run ends with nothing committed.
- **Never** `git push`, open PRs, merge, rebase or force anything. Commit on the current branch with clear conventional messages; leave the tree clean (`git status --short` empty at the end).
- **Never** run `make bootstrap`, teardown scripts, `cdk deploy`, `make dev`, or anything against AWS or the production box. No AWS calls are needed; tests stub the client.
- Do not edit `apps/studio/`, `vendor/`, `vendor-src/`, `backend/samples/frontdesk_agent`.
- Do not widen scope. If the requirement turns out to be wrong or already covered, stop and say so in the report instead of building something adjacent.
- Commit only files you changed (`git add <paths>`), never `git add .` or `git add docs/`.
- Save any probe output to the ABSOLUTE host path `/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-011/`. Nothing under `.claude/` is committed.
- Stay within the budget cap the host set; if you are running out, commit what is verified and report what remains.

## Final report (the host reads only this)

End with exactly these sections:

1. **Changed** — files and what changed, one line each.
2. **Verified** — the commands you ran with their pass/fail outcome (paste the `make verify` tail).
3. **Acceptance checks** — the list above, each ✅/❌ with the evidence.
4. **Not done / deviations** — anything left, anything you interpreted differently, and why.
5. **Commits** — `git log --oneline main..HEAD`.
