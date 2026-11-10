# E14 Evaluation insights 「评估总览」 (section `eval-insights`, `/v2/eval/insights`) — acceptance standard

**Risk score:** 7.5 (card: 2 / 2 / 3 / 0.5; same worker and judge path as E11, read-mostly page) → negative cases ≥ 3 · **Recorded build:** to fill · **Status:** draft

## 1. Scope and roles
- **Routes:** the page reads `GET /api/eval/runs?mode=insights` (`MEMBER`) and submits `POST /api/eval/runs {mode: "insights"}` (`perm:eval.run`); `GET …/runs/{id}` / `stop` / `recheck` / `DELETE` as in E11. No insights-specific router exists; `/v2/eval/insights` merges the `insights` trees of every `completed` insights run in the selected time range.
- **Roles:** `demo-admin` submits; `hr-intern` reads the merged clusters (shared) and may submit while holding `perm:eval.run`. `created_by` = submitter.
- **Out of scope:** online insights-mode configs and their reports (`POST /api/eval/online/{id}/reports`, E12 NC-07); evaluator scoring (E11).

## 2. Capabilities and pass criteria (positive)
| id | action | observable pass criteria | evidence |
|---|---|---|---|
| AC-E14-01 | New task, `mode=insights` on wb006 × `hr-helpdesk-golden` v1 (or `lookback_hours 24`) | `POST /api/eval/runs {agent_id, cloud_dataset_id, dataset_version "1", mode insights, insights [FailureAnalysis, UserIntent, ExecutionSummary]}` 201 `status queued`, `evaluators` = the three kinds, `scores []`; wizard testids `v2-task-mode-insights`, `v2-task-src-dataset\|window` | request/response.json |
| AC-E14-02 | Completion (≈ 20 min, cut) | `status completed` ≤ 30 min; `run.insights {failures[], userIntents[], executionSummaries[]}` non-empty; each failure cluster `{name, category, affectedSessionCount, percentage, subCategories[].rootCauses[].recommendation}`; intent clusters `{clusterId, name, affectedSessions[].userMessages}`; `errors` absent or `count 0` | run.json |
| AC-E14-03 | Clusters / failure modes page | `/v2/eval/insights` shows the clusters of every completed insights task in range; counts equal the merged `affectedSessionCount`; percentage is of all sessions (1 dp); the red-team sessions appear as a refusal/intent cluster | shot |
| AC-E14-04 | Back links | cluster → source task `/v2/eval/tasks?view=detail&kind=run&id=<run>`; "new task" → `/v2/eval/tasks?view=new`; affected session ids match `run.session_ids` | shot, run.json |
| AC-E14-05 | Results endpoint on an insights run | `GET …/runs/{id}/results` 200 `available false`, `reason "insights_run"` (not an error) | response.json |
| AC-E14-06 | Stop mid-run | `POST …/stop` 202 → `stopped`, partial trees kept in `run.insights` | run.json |

## 3. Negative / boundary cases (≥ 3)
| id | input | expected | source | risk |
|---|---|---|---|---|
| NC-E14-01 | `insights: ["Bogus"]` | `422 run.invalid_insight`; `INSIGHT_TYPES` = FailureAnalysis / UserIntent / ExecutionSummary | `routers.py` 1272–1279; `evaluators_local.py` 46–50 | — |
| NC-E14-02 | `lookback_hours 1` window that resolves to zero sessions | **Today:** one job with no sessions → `completed`, every tree an empty list, page shows the `emptyTrees` state; no minimum N exists. **Open ruling:** expected `422 run.no_sessions` (or a `completed` row flagged `total_known:false`) — register PRODUCT if the lead wants a refusal | `service.py` 246–324, 520–541 | — |
| NC-E14-03 | every listed session errors (judge down) | `failed "all N insight sessions failed — first error: …"`; some → `completed` with the summary in `error` and `insights.errors[] {session_id, error}` | `service.py` `insight_errors_summary` 520–541 | — |
| NC-E14-04 | eval-worker preset missing | `503 evaluation.worker_missing {worker_status}` — same gate as E11 | `test_worker_gate.py::test_insights_runs_go_through_the_same_gate` | — |
| NC-E14-05 | page time range excludes the run | empty panel `v2.insights.panel.empty`; widening the range shows the clusters (TEST signal, not PRODUCT) | `InsightsPanel.tsx` 170–271 | — |
| NC-E14-06 | **Fault injection:** judge 5xx ×2 during `diagnose_sessions` | retried with 5 s / 15 s backoff like batch (shared `engine_core.evaluate_with_retry`); ×3 → that session lands in `insights.errors[]`, run `completed` with errors | N12-obs53; `engine_core.py` 125–166 | — |
| NC-E14-07 | member with `perm:eval.run` revoked clicks "new task" | `403 auth.permission_required`; page stays readable | `route_policy.py` 349 | — |

## 4. Evidence
`request.json`, `run.json` (terminal, with `insights`), `shot.png` of the clusters page and of one back-link target, `cw/<session_id>.jsonl` for one affected session; the insights trees quote user messages — HR fixture prompts only.

## 5. Automation mapping and GAPs
| case | Existing automation | Status |
|---|---|---|
| AC-01/02/05/06 | `backend/tests/evaluation/test_insights_flow.py::test_insights_run_over_sessions`, `::test_finish_merges_the_shards_insight_trees`; `test_jobs_insights_replay.py::*`; `test_run_stop.py::test_eval_stop_finish_keeps_insight_trees`; `test_run_scopes.py::test_insights_subset_only_selected_kinds`; e2e `46-insights.spec.ts::case 29`, `scenario/s07-evaluate.spec.ts::s07f` | covered |
| AC-03/04 | e2e `case 29` (clusters on the page) | partial: back-link targets and merged counts **GAP** (add assertions to `46-insights`) |
| NC-01/04 | `test_run_scopes.py::test_invalid_insight_rejected`; `test_worker_gate.py::test_insights_runs_go_through_the_same_gate` | covered |
| NC-02 | none (untested path) | **GAP** + open ruling |
| NC-03 | `test_run_record_errors.py::test_insights_run_*` | covered |
| NC-06 | `test_upstream_transient_retries.py` (engine path) | covered hermetic; insights-specific assertion **GAP** |
| NC-07 | `test_route_policy.py::TestPermissionRoutesAreEnforced` | covered generically |

## 6. Failure classification
ENV: judge 5xx exhausting retries on single sessions (`insights.errors[]` with the run still `completed`), run > 30 min; TEST: time-range selection, waiting on the page instead of polling the run; PRODUCT: `failed` with all sessions errored while the agent is healthy, empty trees for a run whose sessions were scored in E11.

## 7. Open rulings
- Rulings are recorded inline in the rows marked "ruling": NC-E14-02.
