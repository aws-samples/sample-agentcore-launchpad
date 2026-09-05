## Summary

Self-evolution direction **SE-009 — Evaluation runs can be stopped (StopBatchEvaluation + queued cancel)** (branch `evo/se-009-evaluation-runs-can-be-stopped-stopbatch`).

### Requirement

An operator can stop an evaluation run from the Evaluation Runs page before AWS finishes it.

- A run in status `invoking`, `waiting` or `evaluating` that has a `batch_eval_id` shows a **STOP** action (row + run detail). Clicking it (with the shared ConfirmDialog) calls a new console route `POST /api/evaluation/runs/{run_id}/stop`, which calls `StopBatchEvaluation(batchEvaluationId=…)` through a wrapper in `backend/app/evaluation/agentcore_eval.py` (the only place AgentCore evaluation shapes live) using the data-plane client from `aws_clients.py`.
- A run still `queued` (never submitted to AWS, `batch_eval_id` is null) can be **cancelled** locally: the route marks it and the queue worker skips it when it dequeues (the `EvaluationQueue` in `queue.py` has no cancel today — add a cancelled-set check at the start of the submitted callable, or equivalent).
- The poller in `service.py` treats AWS `STOPPING` as still-running and `STOPPED` as a new terminal ledger status `stopped` (not `failed`): scores for already-judged sessions are parsed with the existing `parse_eval_scores` / `parse_insights` when present, and the run row shows a "stopped by operator" reason. `COMPLETED_WITH_ERRORS` handling is unchanged.
- The Runs list/detail render `stopped` as its own chip (en + zh-CN), the disabled STOP button explains why via `disabledReason` (already completed / failed / no batch id yet), and the queue-state banner keeps counting only active runs.
- `docs/api.md` (+ zh-CN twin) documents the route; `docs/architecture.md` Evaluation row mentions stop/cancel and the `stopped` status.

Out of scope: `DeleteBatchEvaluation` (ledger and AWS would disagree about results), stopping online evaluation configs (already has pause).

### Evidence

- `backend/app/evaluation/routers.py:784-931` — `list_runs`, `get_run`, `create_run`, `queue_state` — no stop/cancel route exists.
- `backend/app/evaluation/service.py:183-371` — status transitions `queued → invoking → waiting → evaluating`; `:308-311` any AWS status other than `COMPLETED`/`COMPLETED_WITH_ERRORS` becomes `failed` with "batch evaluation ended <status>", so a stop today would render as a failure.
- `backend/app/evaluation/queue.py:19-79` — `EvaluationQueue.submit/_drain/state/position`; no cancel.
- `backend/app/evaluation/agentcore_eval.py:216-319` — `start_batch_evaluation`, `get_batch_evaluation`, `parse_eval_scores` — add `stop_batch_evaluation` beside them.
- `backend/app/evaluation/models.py:41-52` — `EvalRun.status` is `String(16)` default `queued`; `stopped` fits.
- `frontend/src/pages/Evaluation.tsx:346-368` — status → chip/label mapping (`queued`, `completed`, `failed`); `:1068` run-detail error block.
- botocore model `bedrock-agentcore/2024-02-28` (apiVersion 2024-02-28): `StopBatchEvaluation` input `{batchEvaluationId}` → output `{batchEvaluationId, batchEvaluationArn, status, description}`, HTTP 202; `BatchEvaluationStatus` enum `PENDING, IN_PROGRESS, COMPLETED, COMPLETED_WITH_ERRORS, FAILED, STOPPING, STOPPED, DELETING`.
- Docs (accessed 2026-09-05): SDK reference for StopBatchEvaluation — "Stops a running batch evaluation. Sessions that have already been evaluated retain their results." Regional availability: `Bedrock AgentCore+StopBatchEvaluation` isAvailableIn us-west-2 and us-east-1.
- `docs/architecture.md` Evaluation row: runs execute through a bounded-concurrency queue capped at the 5 active-batch-evaluations account quota — a stuck run holds a slot.

### Acceptance checks

- [ ] `cd backend && uv run pytest tests/ -q -k "eval and stop"` — new hermetic tests (stubbed data-plane client) cover: stop of an `evaluating` run calls `stop_batch_evaluation` with the ledger's `batch_eval_id` and returns 202/200 with the run; stop of a `completed` run returns 409 in the error envelope; cancel of a `queued` run never calls AWS and the worker skips it; poller maps AWS `STOPPED` (with partial `evaluatorScores`) to ledger status `stopped` with parsed scores and `STOPPING` to still-running.
- [ ] `frontend`: `npx tsc --noEmit && npm run lint` pass; `src/lib/api.ts` has the stop call and the `stopped` status in the run type; en/zh-CN keys added with parity (`python3 scripts/i18n_check.py`).
- [ ] `docs/api.md` + `docs/api.zh-CN.md` list `POST /api/evaluation/runs/{run_id}/stop`; `docs/architecture.md` Evaluation row mentions stop/cancel + `stopped`.
- [ ] `make verify` passes.
- [ ] Live AWS check: **not required by the gate**; the host may verify on a dev run later (StopBatchEvaluation on a real IN_PROGRESS batch). Record in the report that it was not run.


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
