# E11 Evaluation tasks 「评估任务」 (section `eval-tasks`, `/v2/eval/tasks`) — acceptance standard

**Risk score:** 9.5 (card: 3 / 3 / 3 / 0.5; R2 cross-partition judge + replay 5xx, R6 worker/backend restart, R7 billable `perm:eval.run` default-granted) → negative cases ≥ 6 · **Recorded build:** to fill · **Status:** draft

## 1. Scope and roles
- **Routes (`route_policy.py` 347–360):** `GET /api/eval/queue`, `GET /api/eval/runs`, `GET …/runs/{id}`, `GET …/runs/{id}/results`, `GET …/recommendation-inputs`, `GET …/recommendations` = `MEMBER`; `POST /api/eval/runs`, `POST …/stop`, `POST …/recheck`, `POST …/recommendations`, `DELETE …/runs/{id}` = `perm:eval.run`; `POST …/recommendations/{rec}/accept` = `perm:agents.deploy`. Provider discovery: `GET /api/experiments/providers` at tip → `GET /api/eval/recommendation-providers` (`MEMBER`) after `cn/r13-b`.
- **Roles:** `demo-admin` submits runs on `hr-helpdesk-wb006`; `hr-intern` holds `perm:eval.run` by default (revocable) and may submit billable runs — narrate as R7; `created_by` = submitter. Accepting a recommendation republishes the agent → creator-or-admin (`403 agent.forbidden`).
- **Out of scope:** `mode=insights` (E14), online configs (E12), canaries (E13), configuration experiments (retired, N12-obs49 / r13-b), the eval-worker preset install itself (E03 `29`/`49`).

## 2. Capabilities and pass criteria (positive)
| id | action | observable pass criteria | evidence |
|---|---|---|---|
| AC-E11-01 | Read existing run `f80fc768712f` | `GET …/runs/{id}` 200 `status completed`, `scores[] 3 × {evaluatorId, score, count}`, `errors {count, total}`, `session_failures`, `dataset_version`; `GET …/results` `available true`, `sessions[].results[] {evaluator_id, score, label, explanation}` | response.json ×2 |
| AC-E11-02 | Wizard: wb006 × `hr-helpdesk-golden` v1 × {GoalSuccessRate, Helpfulness, `hr_guardrails_judge`} | `POST /api/eval/runs {agent_id, cloud_dataset_id, dataset_version "1", evaluators[3], mode evaluators}` 201 `status queued`, `queue_position`, `dataset_version "1"`, `dataset_name "cloud:<name>"`, `created_by` | request/response.json |
| AC-E11-03 | Progress | status mirrors `queued → invoking → waiting → evaluating → completed` (≤ 30 min, cut in editing); `GET /api/eval/queue {running[], queued[], locked, max_concurrency 3}` names the run while active | status polls, queue.json |
| AC-E11-04 | Completed card | `scores[].count` = scored sessions; target `errors.count 0`; if an upstream transient slipped through, `completed` with `error "k of N evaluator records errored…"` + `session_failures.kind evaluator_error` is still a PASS with an observation (N-SCN-obs21 ruling); RunCoverage tag "Completed · n/m sessions" (N12-obs47) | run.json, shot |
| AC-E11-05 | Results / explanation / trace | per-session rows with `label`, `explanation`; session link opens the observability session (`session_id` matches `run.session_ids`) | shot, results.json |
| AC-E11-06 | Red-team run (`hr-helpdesk-redteam`, judge only) | `completed`; every record of `hr_guardrails_judge` `score 1.0` (6 / 6), `scores[0].count 6`, `errors.count 0` | run.json, results.json |
| AC-E11-07 | Stop | `POST …/stop` 202 `stop_requested true` → `stopped` ≤ 60 s grace (`StopRuntimeSession` after `STOP_GRACE_S`); partial `scores` kept; queued run → `stopped` at once with `error "stopped by operator"` | run.json before/after |
| AC-E11-08 | Delete | `DELETE` on a `failed`/`stopped` run 200 `{deleted true, run_id, status, aws_batch_left_in_place}`; second `GET` → 404 | response.json |
| AC-E11-09 | Next step: recommendation (s08a) | `POST …/recommendations {kinds [system_prompt], provider gepa_lite}` 201 `IN_PROGRESS` → `COMPLETED` with `result.recommended_prompt`; providers list contains `gepa_lite enabled true`; `POST …/accept` 202 `{job_id, deployment_id}` → agent version +1, `accepted.by` = approver | rec.json, job.json |
| AC-E11-10 | Member view | `hr-intern` lists all runs (shared) and the New task button is enabled (perm default); after admin revokes `perm:eval.run` the button is disabled with the missing-permission hint | two-identity shot |

## 3. Negative / boundary cases (≥ 6)
| id | input | expected | source | risk |
|---|---|---|---|---|
| NC-E11-01 | no dataset / sessions / lookback scope; or `agent_id` + `log_source` together | `422 run.scope_required` / `422 run.target_required` | `routers.py` 1225–1253 | — |
| NC-E11-02 | `agent_id` of a deleted / `deploying` / hidden agent | `400 agent.not_active` | `routers.py` 1233–1236 | — |
| NC-E11-03 | `dataset_version "2"` (never published); `"DRAFT"`; version with a local `dataset_id` | `422 run.dataset_version_unknown` / `422 run.dataset_version_scope` ×2 | `routers.py` 497–504, 1260–1271 | — |
| NC-E11-04 | 11 distinct evaluators | `422 run.too_many_evaluators` (`MAX_BATCH_EVALUATORS 10`) | `routers.py` 1302–1311 | — |
| NC-E11-05 | `Builtin.TrajectoryExactOrderMatch` on a dataset without ground truth | `422 run.trajectory_needs_ground_truth {evaluators:{id:[gaps]}}`; judge with `{expected_response}` → `422 run.judge_needs_ground_truth` | `routers.py` 158–212; `coverage.py` 81–96 | — |
| NC-E11-06 | unknown `cust_…` or `ThirdParty.*` id | `422 run.evaluator_not_found` before any replay | `routers.py` 177–183, 870–888 | — |
| NC-E11-07 | eval-worker preset uninstalled or still `deploying` | `503 evaluation.worker_missing {worker_status}` before any row; evaluators / datasets / SCORE NOW keep working | `executor.py` 190–198; `runtime_executor.py` 95–111 | — |
| NC-E11-08 | `stop` a `completed` run; `DELETE` a `completed`/`queued` run | `409 run.not_active` / `409 run.not_deletable {status}` (no delete for completed runs, N12-obs38; cleanup counts these as `refused`, N-SCN-obs20) | `service.py` 734–739; `routers.py` 1561–1588 | — |
| NC-E11-09 | `recheck` a `completed` run / a `failed` run without `batch_eval_id` | `409 run.not_recheckable`; failed run whose job left no artefacts stays `failed` with "lost before it started — submit the run again" | `service.py` 779–795 | — |
| NC-E11-10 | `evaluators: []` explicitly | **Today: 201** (no `min_length`; defaults apply only when omitted) → a run that scores nothing. **Open ruling:** expected `422 run.evaluators_required` | `routers.py` `RunCreate` 1079–1104 | — |
| NC-E11-11 | member with `perm:eval.run` revoked: `POST /runs`, `/stop`, `DELETE`; member without `perm:agents.deploy`: `accept` | `403 auth.permission_required`; UI entry disabled | `route_policy.py` 349–360; `routers/auth.py` 318–328 | — |
| NC-E11-12 | **Fault injection:** judge ConverseStream 5xx ×2 then OK | record scored, run `completed`, `errors.count 0`; worker waited 5 s / 15 s (`eval_transient_retry_backoff_seconds "5,15"`) | N12-obs53; `engine_core.py` 125–166 | — |
| NC-E11-13 | **Fault injection:** judge 5xx ×3 | error record `error_type EventStreamError`, message `judge <id> errored on every one of 3 attempts (transient upstream; waited 5 s, 15 s): …`; `session_failures.kind evaluator_error`; run `completed` with `error` (not `failed`) | N12-obs53 | — |
| NC-E11-14 | **Fault injection:** agent replay `internalServerException` ×2 / ×3 | ×2: same pre-assigned session retried, `invocations.jsonl.retried[]`; ×3: `failed after 3/3 attempts: … (retried after: #1 …, waited 5 s; #2 …)`, `session_failures.kind replay_failed`, one `replay_failures[]` entry, error rows carry `arm` (r13-d) | N12-obs55; r13-d `test_failure_arm_attribution.py` | — |
| NC-E11-15 | **Fault injection:** `systemctl restart launchpad-backend` mid-run | dispatched run re-attached by `resume_interrupted_runs` and completes; a run interrupted before dispatch → `failed "interrupted by a backend restart before the evaluation job was dispatched — submit the run again"`; nothing stays `invoking` | `service.py` 877–903 | — |
| NC-E11-16 | `accept` twice; accept on a non-harness agent; prompt > 20 000 chars | `409 recommendation.already_accepted` / `400 recommendation.accept_not_harness {method, reason}` / `400 recommendation.prompt_too_long` | `recommendations.py` 888–924 | — |

## 4. Evidence
Per case: `request.json`, `response.json`, `run.json` at terminal state, `results.json`, `shot.png`; replay runs add `cw/<session_id>.jsonl` for one scored session and one errored session (N12-obs47 reason visible); fault cases add the worker `invocations.jsonl` lines and `done.json` (from S3 `evaluations/<job>/`). Judge prompts are content; model credentials never appear.

## 5. Automation mapping and GAPs
| case | Existing automation | Status |
|---|---|---|
| AC-01/02/03/04 | `backend/tests/evaluation/test_runs_flow.py::test_active_run_completes_with_scores`, `::test_statuses_mirror_the_workers_phases`; `test_run_dataset_version.py::*`; `test_run_session_failures.py::*`; e2e `43-batch-run.spec.ts::case 26`, `scenario/s07-evaluate.spec.ts::s07c` | covered (HR fixtures needed) |
| AC-05/06 | `test_runs_flow.py::test_run_results_groups_judge_records_per_session`; red-team dataset | partial: red-team 6/6 run **GAP** (new e2e chapter) |
| AC-07/08, NC-08/09 | `test_run_stop.py::*`, `test_run_recheck.py::*`, `test_runs_flow.py::test_run_failure_recorded` | covered |
| AC-09, NC-16 | `test_run_recommendations.py::*`; `optimization/test_recommend_providers.py::test_providers_endpoint_shape_follows_settings`; e2e `47-recommendation.spec.ts::case 30`, `s08-improve.spec.ts::s08a` | covered (provider path moves in r13-b) |
| AC-10, NC-11 | `test_route_policy.py::TestPermissionRoutesAreEnforced`, `::test_fully_revoked_member_gets_403` | covered generically; UI disabled-state **GAP** |
| NC-01/03/04/05/06 | `test_run_scopes.py::*`, `test_run_dataset_version.py::*`, `test_datasets_v2.py` (ground truth / too many), `test_runs_flow.py::test_skill_builtins_pass_through_and_thirdparty_ids_are_refused`, `test_visibility.py::test_hidden_evaluators_cannot_be_referenced` | covered |
| NC-02/10 | none | **GAP** → Q6a schemathesis + ruling on NC-10 |
| NC-07 | `test_worker_gate.py::*`; e2e `49-worker-preset.spec.ts::case 32` | covered |
| NC-12/13/14 | `test_upstream_transient_retries.py` (19), `test_eval_worker_template.py::test_a_judge_5xx_is_retried_and_the_session_is_still_scored`, `::test_a_judge_that_stays_5xx_is_an_error_record_typed_as_the_cause_naming_the_attempts`, `::test_a_replay_5xx_error_payload_is_retried_in_the_same_session`; r13-d `test_failure_arm_attribution.py` | covered hermetic; live re-test pending (N12-obs53/55) → Q4a `FaultyBedrock` |
| NC-15 | `test_resume.py::*` | covered hermetic; process drill **GAP** → Q4a `kill_worker.sh` / restart script |

## 6. Failure classification
ENV: Global Bedrock `internalServerException` / throttling that survives the 3-attempt retry (`errors.count ≥ 1` on an otherwise completed run — N-SCN-obs21), telemetry readiness timeout on a cold agent (`telemetry_incomplete`), run > 30 min; TEST: hard asserting `errors.length === 0`, dataset-version fixtures; PRODUCT: `failed` with `all N … failed`, stuck `invoking` after restart, any 5xx from `/api/eval/*`.

## 7. Open rulings
- Rulings are recorded inline in the rows marked "ruling": AC-E11-04, NC-E11-10.
