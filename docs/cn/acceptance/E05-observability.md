# E05 Observability 「可观测」 (section `observability`, `/v2/observability`) — acceptance standard

**Risk score:** 8.5 (card: 2 / 3 / 3 / 0.5; R2 Logs Insights / judge upstream) → negatives ≥ 6 · **Recorded build:** to fill · **Status:** draft

## 1. Scope and roles
- **Routes (`route_policy.py`, post r13-c):** `GET /api/observability/dashboard`, `…/traces`, `…/traces/{trace_id}`, `…/sessions`, `…/sessions/{sid}`, `…/sessions/{sid}/transcript`, `POST …/sessions/{sid}/evaluate` (SCORE NOW), `GET /api/traces/{session_id}` (Chat rail) = `MEMBER`. `GET /api/observability/prices` and `POST …/prices/refresh` **no longer exist** (N13-obs3); no response carries `est_cost_usd` or `prices_meta` — token counts only.
- **Parameters:** `range ∈ {1h, 6h, 24h, 7d}`; `trace_id` `^[0-9a-f]{32}$`; `session_id` `^[A-Za-z0-9_\-#:.@]{8,256}$` (router path pattern and `SESSION_ID_RE` in the service); 60 s TTL cache keyed by (view, range), `force=true` bypasses it. SCORE NOW takes 1–5 `evaluator_ids`.
- **Roles:** `demo-admin` and `hr-intern` both read; other principals' **assistant** sessions are hidden (`PrivateSessions`); under `isolated` visibility a member's view drops sessions of hidden agents (E19). Views: DASHBOARD · SESSIONS → detail · TRACES → waterfall; deep links `?tab=sessions&view=session&id=<sid>`, `?trace=<id>`.
- **Out of scope:** online evaluation configs (E12), batch runs (E11), cost reporting (deleted; returns with the budgets family).

## 2. Capabilities and pass criteria (positive)
| id | action | observable pass criteria | evidence |
|---|---|---|---|
| AC-E05-01 | TRACES after the E04 turn | the turn's trace appears ≤ 120 s after `done` (lag recorded); row has `agent` = `hr-helpdesk-wb006` (ledger-mapped), `status ok`, `tokens.{input,output,total}`; filters `agent`, `status`, `session` narrow `count`; no cost column, no UPDATE PRICES button | traces.json, shot.png, lag.txt |
| AC-E05-02 | DASHBOARD | 5 stat tiles + traffic / latency / tokens / tools charts; `tokens_by_model` rows have token counts only; a second load within 60 s is a cache hit (`force=true` bypasses) | dashboard.json ×2 |
| AC-E05-03 | SESSIONS → tool session detail | transcript reconciled from Memory + ledger in turn order; traces-in-session cards; the `hr-database___get_employee` turn shows a `TOOL` span in its trace waterfall; ONLINE EVALUATION block present or `unavailable` (degraded, not an error) | session.json, shot.png |
| AC-E05-04 | SCORE NOW with 2 evaluators (`hr-guardrails-judge`, `Builtin.Helpfulness`) | `POST …/evaluate` 200 with exactly 2 result rows, each scored or an `error_code` row; results **not persisted**: `GET /api/eval/runs` has no `ondemand*` dataset row; the card shows scores (`v2-obs-score-now`) | evaluate.json, runs.json |
| AC-E05-05 | Cross links | session detail OPEN IN CHAT ↗ opens the session in `/v2/chat`; Chat OPEN IN OBSERVABILITY ↗ lands on `?tab=sessions&view=session&id=`; `?trace=<id>` opens the waterfall | shot.png |
| AC-E05-06 | eval-worker preset row (card chapter ④) | `GET /api/system-agents` entry carries `version`, `release`, `installed_release`, `last_job`; `GET /api/system-agents/{key}/jobs/{job}` admin 200 / member 403 (N12-obs31) | system-agents.json |
| AC-E05-07 | Member view | `hr-intern` sees the same traces and sessions of the shared workspace; another user's assistant session is absent from lists and 404 by id | two-identity sessions.json |

## 3. Negative / boundary cases (≥ 6)
| id | input | expected | source | risk |
|---|---|---|---|---|
| NC-E05-01 | `range=2h` | `422 validation.invalid_request` (`RangeParam` pattern) | `routers/observability.py` | input hygiene |
| NC-E05-02 | trace id of 31 hex chars; session id of 7 chars or containing `\|` | `422 validation.invalid_request` (path patterns); a bypassing caller hits `422 observability.bad_id` in the service (defense in depth before any query literal) | `routers/observability.py`, `services/observability.py::_require`; `test_observability::test_validation_rejects_bad_inputs`, `::test_query_builders_reject_unvalidated_ids` | R4 (Logs Insights literal injection) |
| NC-E05-03 | unknown trace; unknown session detail / transcript | `404 observability.trace_not_found`; `404 observability.session_not_found` (UI `v2-obs-trace-notfound` / `v2-obs-session-notfound`, no blank page) | `routers/observability.py` | — |
| NC-E05-04 | SCORE NOW with 6 evaluator ids | `422 validation.invalid_request` (`SessionEvaluateBody.evaluator_ids max_length 5` wins over the service's `observability.too_many_evaluators`) | `test_observability_evaluate::test_more_than_five_evaluators_is_422`, `::test_service_refuses_more_than_five_distinct_ids` | R7 (judge cost) |
| NC-E05-05 | SCORE NOW with a judge whose prompt reads ground truth | `422 observability.evaluator_needs_ground_truth {evaluators}` before any span read or judge call | `services/observability.py::evaluate_session`; `test_observability_evaluate::test_a_ground_truth_judge_cannot_score_a_live_session` | R7 |
| NC-E05-06 | SCORE NOW right after a turn, spans not landed | `409 observability.session_spans_missing {session_id, range, hint}`; card shows `v2-obs-score-error` with the readiness hint | `test_observability_evaluate::test_unknown_session_id_shape_is_422_and_no_spans_is_409` | R2 |
| NC-E05-07 | SCORE NOW with an unknown evaluator id; one judge call fails upstream | refused before any span read (code pinned by `test_unknown_evaluator_is_refused_before_any_span_read`); a failed judgement is an **error row** (`error_code`, `error_message`), never a 5xx | `test_observability_evaluate::test_a_failed_judgement_is_an_error_row_not_a_5xx` | R2 |
| NC-E05-08 | another principal's private assistant session by id (detail, transcript, evaluate, `/api/traces/{sid}`) | `404` on every surface (`observability.session_not_found` / `chat.session_not_found`) | `test_observability_evaluate::test_another_principals_private_session_is_404`, `test_chat_trace_route::test_another_workspaces_session_is_404` | R9 |
| NC-E05-09 | **fault injection**: Logs Insights query failure | lists / detail → `502 observability.query_failed` with the reason; the Chat rail `GET /api/traces/{sid}` stays `200` with `spans []` and `unavailable_reason` | `test_observability` cache/query tests, `test_chat_trace_route::test_route_degrades_when_the_query_fails` | R2 |
| NC-E05-10 | `GET /api/observability/prices` on the recorded build | `404` (route removed, r13-c); any body still carrying `est_cost_usd` is PRODUCT | `bugs-round-13.md` N13-obs3; `test_observability.py` asserts the keys are absent | deletion regression |
| NC-E05-11 | evaluation engine not registered (feature off) | SCORE NOW `503 feature.unavailable {feature: "evaluation"}` | `test_observability_evaluate::test_without_an_engine_the_feature_is_unavailable` | — |

## 4. Evidence
Per TEMPLATE §4. Mandatory here: `lag.txt` (turn `done` timestamp vs first list hit), `traces.json` / `sessions.json` / `dashboard.json` per range used, `evaluate.json` + `runs.json` for AC-04, `cw/<session_id>.jsonl` extracted from `aws/spans` for the recorded session.

## 5. Automation mapping and GAPs
| case | existing automation | status |
|---|---|---|
| AC-01/03 | `tests/e2e/cn/10-observability.spec.ts::case 10` (lag ≤ 120 s, sessions ≥ agents), `scenario/s06-observe::s06` | covered |
| AC-02 | `backend/tests/test_observability.py::test_dashboard_endpoint_shape`, `::test_dashboard_tokens_prefer_span_aggregation`, `::test_cache_second_call_hits_no_aws`, `::test_cache_force_bypasses_and_ttl_expires` | covered (API); UI tiles **GAP** |
| AC-04, NC-04…07, NC-11 | `44-score-now.spec.ts::case 27`, `test_observability_evaluate.py` (11) | covered |
| AC-06 | `31-preset-observability.spec.ts::obs31` | covered |
| AC-07, NC-08 | `test_observability_evaluate::test_another_principals_private_session_is_404`, `test_visibility_cn` | covered (unit); member e2e **GAP** → extend `s05e` |
| NC-01/02/03 | `test_observability::test_validation_rejects_bad_inputs`, `::test_session_routes_accept_composite_external_ids` | covered; 404 UI states **GAP** → Q6a schemathesis + spec 10 step |
| NC-09 | `test_chat_trace_route::test_route_degrades_when_the_query_fails`, `test_traces::test_query_failure_leaves_an_empty_rail_without_raising` | covered (unit); live injection **GAP** → Q4a |
| NC-10 | `test_observability.py` (adapted in r13-c) | covered once r13-c merges |

## 6. Failure classification
`ENV`: first-trace lag > 120 s (history N10v2-bug2: 103 s / 86 s, then 47 s), empty `aws/spans` (narrow `Settings.observability_spans_source` to `runtime_logs`), Logs Insights throttling, judge-model 5xx (N12-obs53/55 retries). `TEST`: table test ids, 60 s waits. Else `PRODUCT`.

## 7. Open rulings
- Members see every workspace session here while Chat lists only their own (see E04 §7); keep or align — lead decision.
- The r13-c stat/architecture text still mentions "est cost" in `docs/architecture.md` §Observability until that branch merges; standards above describe the post-deletion behaviour.
