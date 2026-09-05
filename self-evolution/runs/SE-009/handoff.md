# Direction SE-009 — Evaluation runs can be stopped (StopBatchEvaluation + queued cancel)

You are the implementation session for one direction of the AgentCore Launchpad
self-evolution loop. You are in a dedicated git worktree on branch
`evo/se-009-evaluation-runs-can-be-stopped-stopbatchevaluat` (or similar — check `git branch --show-current`),
created from `main`. A host session wrote this brief, will independently re-run the
acceptance checks on your branch, and owns push, PR and merge. Work only inside this worktree.

## Requirement

An operator can stop an evaluation run from the Evaluation Runs page before AWS finishes it.

- A run in status `invoking`, `waiting` or `evaluating` that has a `batch_eval_id` shows a **STOP** action (row + run detail). Clicking it (with the shared `ConfirmDialog`) calls a new console route `POST /api/evaluation/runs/{run_id}/stop`, which calls `StopBatchEvaluation(batchEvaluationId=…)` through a wrapper in `backend/app/evaluation/agentcore_eval.py` (the only place AgentCore evaluation shapes live) using the data-plane client obtained the same way the existing run code obtains it (never `boto3.client` directly — `tests/test_client_funnel.py` fails otherwise).
- A run still `queued` (never submitted to AWS, `batch_eval_id` is null) can be **cancelled** locally: the route marks it and the queue worker skips it when it dequeues (the `EvaluationQueue` in `queue.py` has no cancel today — add a cancelled-set check at the start of the submitted callable, or equivalent). A run that is `invoking`/`waiting` without a `batch_eval_id` yet (dataset replay before StartBatchEvaluation) should also be cancellable: set a cancel flag that the replay loop checks between prompts and that prevents `StartBatchEvaluation` from being called; the run ends `stopped`.
- The poller in `service.py` treats AWS `STOPPING` as still-running and `STOPPED` as a new terminal ledger status `stopped` (not `failed`): scores for already-judged sessions are parsed with the existing `parse_eval_scores` / `parse_insights` when present, and the run row shows a "stopped by operator" reason (use the existing `error`/reason field or a dedicated one — keep the schema change additive). `COMPLETED_WITH_ERRORS` handling is unchanged.
- The Runs list/detail render `stopped` as its own chip (en + zh-CN), the disabled STOP button explains why via the shared `Btn` `disabledReason` (already completed / failed / stopped), and the queue-state banner keeps counting only active runs.
- `docs/api.md` **and** `docs/api.zh-CN.md` document the route; `docs/architecture.md` **and** `docs/architecture.zh-CN.md` Evaluation row mention stop/cancel and the `stopped` status.

Out of scope: `DeleteBatchEvaluation` (ledger and AWS would disagree about results), stopping online evaluation configs (already has pause), the public `/v1` API.

## Repository evidence and extension points

- `backend/app/evaluation/routers.py:784-931` — `list_runs`, `get_run`, `create_run`, `queue_state` — no stop/cancel route exists. Add the route here.
- `backend/app/evaluation/service.py:183-371` — status transitions `queued → invoking → waiting → evaluating`; `:308-311` any AWS status other than `COMPLETED`/`COMPLETED_WITH_ERRORS` becomes `failed` with "batch evaluation ended <status>", so a stop today would render as a failure. `:358-371` re-attaches to `evaluating` runs on startup — a `stopped` run must be terminal there too.
- `backend/app/evaluation/queue.py:19-79` — `EvaluationQueue.submit/_drain/state/position`; no cancel.
- `backend/app/evaluation/agentcore_eval.py:216-319` — `start_batch_evaluation`, `get_batch_evaluation`, `parse_eval_scores` — add `stop_batch_evaluation(client, *, batch_id)` beside them.
- `backend/app/evaluation/models.py:41-52` — `EvalRun.status` is `String(16)` default `queued`; `stopped` fits without a schema change.
- `frontend/src/pages/Evaluation.tsx:346-368` — status → chip/label mapping (`queued`, `completed`, `failed`); `:1068` run-detail error block. `frontend/src/lib/api.ts` is the single typed client — add the stop call and the `stopped` status there.
- `frontend/src/components/Btn.tsx` carries `disabledReason`; `ConfirmDialog.tsx` is the shared confirm.
- `backend/app/core/route_policy.py` enumerates every live route and `tests/test_route_policy.py` fails on drift — register the new POST there with the same posture as `create_run`.
- botocore model `bedrock-agentcore/2024-02-28`: `StopBatchEvaluation` input `{batchEvaluationId}` → output `{batchEvaluationId, batchEvaluationArn, status, description}`, HTTP 202; `BatchEvaluationStatus` enum `PENDING, IN_PROGRESS, COMPLETED, COMPLETED_WITH_ERRORS, FAILED, STOPPING, STOPPED, DELETING`. Verify with `cd backend && uv run python -c "import boto3; print(boto3.client('bedrock-agentcore', region_name='us-west-2').meta.service_model.operation_model('StopBatchEvaluation').input_shape.members)"` (no network needed).
- AWS docs: "Stops a running batch evaluation. Sessions that have already been evaluated retain their results." The operation is available in us-west-2 and us-east-1.
- `docs/architecture.md` Evaluation row: runs execute through a bounded-concurrency queue capped at the 5 active-batch-evaluations account quota — a stuck run holds a slot; this is the operator motivation.

Load-bearing patterns from `CLAUDE.md` that apply: all boto3 clients come from `app/services/aws_clients.py`; AgentCore evaluation shapes stay in `agentcore_eval.py`; errors go through `app/core/errors` (`AppError` envelope; a `completed` run being stopped → 409); every user-facing string is an i18n key with en ↔ zh-CN parity (`python3 scripts/i18n_check.py`) and zh-CN copy uses full-width punctuation (`python3 scripts/i18n_zh_punct.py --check`; `--fix` converts).

Read `CLAUDE.md`, then `docs/architecture.md` §Evaluation (service mapping row) and §The SQLite ledger and job/event model, then the files named above, before editing.

## Acceptance checks (the host re-runs these — make each one pass)

- [ ] `cd backend && uv run pytest tests/ -q -k "eval and stop"` (name your tests so this selects them) — hermetic tests with a stubbed data-plane client cover: stop of an `evaluating` run calls `stop_batch_evaluation` with the ledger's `batch_eval_id` and returns the run (200 or 202); stop of a `completed` run returns 409 in the error envelope; cancel of a `queued` run never calls AWS and the worker skips it when it dequeues; poller maps AWS `STOPPED` (with partial `evaluatorScores`) to ledger status `stopped` with parsed scores and `STOPPING` to still-running.
- [ ] `cd backend && uv run pytest tests/test_route_policy.py tests/test_client_funnel.py -q` pass.
- [ ] Frontend: `cd frontend && npx tsc --noEmit && npm run lint` pass; `src/lib/api.ts` has the stop call and the `stopped` status in the run type; en/zh-CN keys added with parity (`python3 scripts/i18n_check.py`) and `python3 scripts/i18n_zh_punct.py --check` clean.
- [ ] `docs/api.md` + `docs/api.zh-CN.md` list `POST /api/evaluation/runs/{run_id}/stop`; `docs/architecture.md` + `docs/architecture.zh-CN.md` Evaluation row mention stop/cancel + `stopped`.
- [ ] `make verify` passes in this worktree (backend ruff+pytest, infra ruff+pytest, frontend eslint+tsc+build, i18n parity, zh-CN punctuation).
- [ ] Live AWS check: **not required by the gate**. Do not run a real evaluation. State in the report that the live stop was not exercised.

## Boundaries

- Run EVERY command in the FOREGROUND — never `run_in_background`, never wait on a background task. In this non-interactive session your turn ends the moment you stop issuing foreground tool calls; an unfinished background `make verify` means the run ends with nothing committed.
- **Never** `git push`, open PRs, merge, rebase or force anything. Commit on the current branch with clear conventional messages; leave the tree clean (`git status --short` empty at the end).
- **Never** run `make bootstrap`, teardown scripts, `cdk deploy`, `make dev`, or anything against AWS or the production box. No AWS calls at all are needed for this direction; tests stub the client.
- Do not edit `apps/studio/`, `vendor/`, `vendor-src/`, `backend/samples/frontdesk_agent`.
- Do not widen scope. If the requirement turns out to be wrong or already covered, stop and say so in the report instead of building something adjacent.
- Commit only files you changed (`git add <paths>`), never `git add .` or `git add docs/`.
- Save any probe output to the ABSOLUTE host path `/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-009/` (not a relative path inside your worktree). Nothing under `.claude/` is committed.
- Stay within the budget cap the host set; if you are running out, commit what is verified and report what remains.

## Final report (the host reads only this)

End with exactly these sections:

1. **Changed** — files and what changed, one line each.
2. **Verified** — the commands you ran with their pass/fail outcome (paste the `make verify` tail).
3. **Acceptance checks** — the list above, each ✅/❌ with the evidence.
4. **Not done / deviations** — anything left, anything you interpreted differently, and why.
5. **Commits** — `git log --oneline main..HEAD`.
