# E12 Online evaluation 「在线评估」 (section `eval-online`, `/v2/eval/online`) — acceptance standard

**Risk score:** 9.5 (card: 3 / 3 / 3 / 0.5; R2 judge upstream on live traffic, R6 scheduler across restarts, R7 continuous billable judging) → negative cases ≥ 6 · **Recorded build:** to fill · **Status:** draft

## 1. Scope and roles
- **Routes (`route_policy.py` 363–373, 453):** `GET /api/eval/online`, `GET …/{id}`, `PATCH …/{id}`, `POST …/{id}/pause`, `DELETE …/{id}`, `GET …/results`, `GET …/reports`, `GET …/reports/{batch}` = `MEMBER`; `POST /api/eval/online`, `POST …/{id}/resume`, `POST …/{id}/reports` = `perm:eval.run`; SCORE NOW `POST /api/observability/sessions/{session_id}/evaluate` = `MEMBER`.
- **Roles:** `demo-admin` creates the config on `hr-helpdesk-wb006`; `hr-intern` chats (E04) to generate samples and may **pause / patch / delete** any visible config (no creator rule) but needs `perm:eval.run` to create or resume. Canary-owned configs (`can_*`, E13) are read-only for everyone.
- **Out of scope:** insights-mode online configs and reports (E14 territory, kept to NC-07 here); workspace isolation (E19). "Workbench sync" in the card = the session detail `online_scores` block + overview `online-quality` card.

## 2. Capabilities and pass criteria (positive)
| id | action | observable pass criteria | evidence |
|---|---|---|---|
| AC-E12-01 | Create 100 % / 1 min / two evaluators | `POST /api/eval/online {agent_id wb006, mode scores, evaluators [Builtin.Helpfulness, cust_<hr_guardrails_judge>], sampling_percentage 100, session_timeout_minutes 1}` 201 `status ACTIVE`, `execution_status ENABLED`, `name oe_<slug>_<6hex>`, `results_log_group /launchpad/evaluations/results/<id>`, `created_by` | request/response.json |
| AC-E12-02 | Two chats on wb006, then wait | after the sessions idle ≥ 1 min: `GET …/results?range=1h` → `evaluators[] count ≥ 2` for both evaluators within ≤ 15 min (soft target ≤ 3 min, s07e); `recent[]` rows carry `session_id`, `score`, `explanation`; `errors.count 0` | results.json (timestamps), shot |
| AC-E12-03 | PAUSE | `POST …/pause` 200 `execution_status DISABLED`; a third chat during the pause produces **no** new `recent[]` row (no scan, no ingest) | results before/after |
| AC-E12-04 | RESUME | `POST …/resume` 200 `execution_status ENABLED`, `status ACTIVE`, `failure_reason null`; the paused chat is picked up on the next tick (watermark overlap) | response.json |
| AC-E12-05 | Workbench sync | `GET /api/observability/sessions/{sid}.online_scores.configs[]` names the config with its records (V2 session detail card); `GET /api/overview/online-quality` `{mean, sessions ≥ 2, evaluators[]}` (120 s cache, `?force=true` refreshes) | two response.json, shot |
| AC-E12-06 | SCORE NOW (s07d) | `POST /api/observability/sessions/{sid}/evaluate {evaluator_ids [Builtin.Helpfulness, Builtin.Coherence]}` 200 `results[] {value, label, explanation}` for both; no run row created (`GET /api/eval/runs` total unchanged) | response.json |
| AC-E12-07 | Delete | `DELETE` 200 `{config_id, deleted true, results_log_group}`; `GET` → `404 online_eval.not_found`; `eval_results` kept — overview 24 h quality still shows the scores | response.json, overview.json |
| AC-E12-08 | Duplicate flag | a second ENABLED config on wb006 → both rows `duplicate_enabled true` (allowed, flagged) | list.json |

## 3. Negative / boundary cases (≥ 6)
| id | input | expected | source | risk |
|---|---|---|---|---|
| NC-E12-01 | `sampling_percentage 0` / `101`; `session_timeout_minutes 0` / `1441`; 11 evaluators; `scores` mode without evaluators | `422 validation.invalid_request` | `online_routers.py` 34–82 | — |
| NC-E12-02 | `Builtin.TrajectoryExactOrderMatch`; a judge referencing `{expected_response}`; unknown `cust_…` | `400 online_eval.evaluator_unsupported`, row not written | `online_evaluators.py` 100–170 | — |
| NC-E12-03 | agent deleted / `deploying` / hidden | `400 agent.not_active` | `online_routers.py` 99–107 | — |
| NC-E12-04 | agent whose runtime log group does not exist yet (never invoked) | `400 online_eval.no_telemetry` (**doc drift:** `evaluation-engine.md` §17 says 409 — expectation follows the router; register DOC) | `online.py` 379–418 | — |
| NC-E12-05 | `PATCH {insights:[…]}` on a scores config; `PATCH {agent_id: other}` | `422 online_eval.mode_conflict`; `agent_id` silently ignored (200, unchanged) — **open ruling:** should unknown keys be `422`? | `online.py` 474–488 | — |
| NC-E12-06 | `pause` / `PATCH` / `DELETE` a canary-owned `can_*` config (a running canary's; N17-obs445: pause / resume of a terminal canary's arm is allowed, creator-or-admin of the canary; edit / delete stay refused) | `403 online_eval.read_only` | `online.py` 323–331 | — |
| NC-E12-07 | `POST …/reports` on a scores config; on an insights config with no sampled sessions in range | `422 online_eval.mode_conflict` / `409 online_eval.no_sampled_sessions` | `online.py` 858–892 | — |
| NC-E12-08 | `GET …/results?range=2h` | `422 online_eval.bad_range` (1h / 6h / 24h / 7d) | `online.py` 696 | — |
| NC-E12-09 | SCORE NOW with 6 ids; on a session with no spans in range; on another member's private assistant session | `422 observability.too_many_evaluators` / `409 observability.session_spans_missing {session_id, range, hint}` / `404 observability.session_not_found` | `services/observability.py` 1925–1962; `routers/observability.py` 282 | — |
| NC-E12-10 | `pause` an already DISABLED config; `resume` an ENABLED one | 200 idempotent (no 409) | `online.py` 510–523 | — |
| NC-E12-11 | **Fault injection:** eval-worker preset uninstalled while ENABLED | config `status FAILED`, `failure_reason "evaluation.worker_missing: …"`, samples stay `queued`; after reinstall `resume` → `ACTIVE` and the backlog is dispatched | `online_scheduler.py` 501–511 | — |
| NC-E12-12 | **Fault injection:** judge 5xx (non-throttle) inside the online job | **Today: no retry** — immediate error record, `results.errors.count ≥ 1`, job still COMPLETED (only throttling is retried 2 / 4 / 8 s). **Open ruling (PRODUCT candidate):** extend the r12-f transient retry (`is_upstream_transient`, backoff 5 s / 15 s) to `jobs_online` | `jobs_online.py` 62–73, 129–146 | — |
| NC-E12-13 | **Fault injection:** `systemctl restart launchpad-backend` with samples `dispatched` | next tick re-derives the job id from `sampled_at`, ingests `results.jsonl` from S3; no sample stuck `dispatched` beyond `eval_job_stale_s` (600 s) | `online_scheduler.py` 1–11, 156–158 | — |
| NC-E12-14 | worker job lost (`status.json` older than 600 s, no `done.json`) | samples `failed "worker job … lost"`, no automatic re-dispatch; config stays ACTIVE | `online_scheduler.py` 367–377 | — |
| NC-E12-15 | member with `perm:eval.run` revoked | `POST /api/eval/online` 403, `resume` 403, `pause` 200 (asymmetry by design: pausing stops spend) | `route_policy.py` 364–368 | — |
| NC-E12-16 | `DELETE` while samples are `dispatched` | 200; the worker job finishes but its output is never ingested — **GAP** (no test, no guard; acceptable, document) | `online.py` 526–539 | — |

## 4. Evidence
`request.json`, `response.json`, `results.json` with wall-clock stamps (chat end → first `recent[]` row), `shot.png` (results chart, session detail card, overview card), `cw/<session_id>.jsonl` for one sampled session, `ledger.redacted.json` (`online_eval_configs`, `online_samples`). Judge explanations may quote HR prompts; employee fixture values are synthetic.

## 5. Automation mapping and GAPs
| case | Existing automation | Status |
|---|---|---|
| AC-01/03/04/07/08 | `backend/tests/evaluation/test_online_configs.py::test_create_writes_the_row_as_local_truth`, `::test_pause_resume_owner_matrix_and_status_reset`, `::test_delete_owner_matrix_removes_row_and_samples_keeps_results`, `::test_list_classifies_and_flags_duplicates`; e2e `45-online.spec.ts::case 28`, `scenario/s07-evaluate.spec.ts::s07e` | covered |
| AC-02 | `test_online_scheduler.py::test_tick_samples_idle_sessions_and_dispatches_one_job_per_config`, `::test_tick_ingests_results_marks_samples_and_is_idempotent`; e2e `case 28` (≤ 15 min poll), `s07e` (soft ≤ 180 s) | covered; the 15-min bound is e2e-only |
| AC-05 | `test_online_surfaces.py::test_session_online_scores_groups_by_config_with_agent_first`, `::test_overview_online_quality_endpoint_caches_per_workspace`; e2e `case 28` (`online_scores` truthy) | covered |
| AC-06, NC-09 | `backend/tests/test_observability_evaluate.py::*` (11); e2e `44-score-now.spec.ts::case 27`, `s07d` | covered |
| NC-01/02/03/04/06/07/08 | `test_online_configs.py::test_create_bounds_are_422`, `::test_create_rejects_unsupported_evaluators`, `::test_create_requires_an_active_agent`, `::test_create_cold_agent_without_a_log_group_is_no_telemetry`, `::test_patch_rejects_experiment_owned_configs`, `::test_results_empty_and_bad_range`; `test_online_insights_mode.py::test_start_report_without_sampled_sessions_is_409` | covered |
| NC-05/10/16 | none | **GAP** → Q6a schemathesis (PATCH extra keys) + idempotency test |
| NC-11/14 | `test_online_scheduler.py::test_worker_missing_and_bad_filter_fail_the_config_transient_errors_do_not`, `::test_tick_marks_a_lost_job_failed_after_the_stale_window` | covered hermetic |
| NC-12 | `test_online_scheduler.py::test_handler_retries_throttled_judges_with_backoff_and_records_other_errors` (pins today's behaviour) | **GAP** for 5xx retry + open ruling |
| NC-13 | `test_online_scheduler.py::test_job_id_is_derived_from_config_and_sample_stamp` (implied) | process drill **GAP** → Q4a restart script |
| NC-15 | `test_route_policy.py::TestPermissionRoutesAreEnforced` | covered generically |

## 6. Failure classification
ENV: first result later than 15 min because of CloudWatch Logs Insights latency or a cold worker, judge `internalServerException` recorded as an error row (NC-12 until ruled), `online_eval.no_telemetry` on an agent that was never invoked; TEST: chatting before the config is ENABLED, the 180 s soft bound; PRODUCT: config `FAILED` for a reason other than worker missing / malformed filter, samples stuck `dispatched` > 600 s, any 5xx.

## 7. Open rulings
- Rulings are recorded inline in the rows marked "ruling": NC-E12-05, NC-E12-12.
