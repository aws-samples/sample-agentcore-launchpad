# E13 Canary experiments 「金丝雀实验」 (section `eval-experiments`, `/v2/eval/experiments`) — acceptance standard

**Risk score:** 9.5 proposed (card carries no score; 3 / 3 / 3 / 0.5: R2 replay + judge upstream, R7 billable replay rounds, real Runtime versions + endpoints minted and deleted) → negative cases ≥ 6 (written to ≥ 10 because the module mutates production serving) · **Recorded build:** to fill · **Status:** draft — **pending lead ruling on the score**

## 1. Scope and roles
- **Routes (`route_policy.py` 381–384):** `GET/POST /api/runtime-canaries`, `GET /api/runtime-canaries/{id}`, `POST …/{id}/action` — all `MEMBER`; no creator-or-admin rule in the handlers (only workspace scoping, isolation visibility and the preset refusal). After `cn/r13-b` every `/api/experiments*` route answers 404 and `GET /api/eval/recommendation-providers` (`MEMBER`) is the provider list; at the recorded tip (b1c9307b) `/api/experiments*` still exists (`MEMBER`) but is unmounted from every console surface (N12-obs49).
- **Roles:** `demo-admin` runs one canary on `hr-helpdesk-wb006`; `hr-intern` chats during the canary (sticky arm routing) and can read the row. Any member may run `complete` / `rollback` on any canary — narrate as a known R9 gap (open ruling below).
- **Out of scope:** configuration experiments (retired — the retired `ea43a27f129a` is shown as history only), the eval worker itself (E11), online configs the canary owns (`can_*`, read-only — E12 NC-06), policy narrowing content (E07).

## 2. Capabilities and pass criteria (positive)
| id | action | observable pass criteria | evidence |
|---|---|---|---|
| AC-E13-01 | Look back at `ea43a27f129a` (`INSUFFICIENT-N`, not a win) | the row is no longer reachable from any console surface; `/v2/eval/experiments?mode=configuration` and `?view=detail&id=…` redirect to the canary list (1-second shot); after r13-b `GET /api/experiments` → 404 | shot, response.json |
| AC-E13-02 | Entry from task detail / assistant next step | `/v2/eval/experiments?canary=new&champion=<wb006>` opens CanaryStart; `GET /api/agents` row carries `canary_capability.eligible true` for wb006 (harness, `active`, `protocol http`) | shot, agents.json |
| AC-E13-03 | CanaryStart: champion → candidate prompt → evaluators → primary | `POST /api/runtime-canaries {agent_id, candidate {system_prompt}, online_evaluators [Builtin.GoalSuccessRate, Builtin.Helpfulness, cust_<hr_guardrails_judge>], primary_evaluator cust_<…>}` 201 `status running`, `stage setup`, `name CANARY-hr-helpdesk-wb006`, `artifacts.online_evaluators` + `primary_evaluator`, `stages` = 7 names, `created_by` | request/response.json |
| AC-E13-04 | `setup` | `POST …/action {action setup}` 202 → `artifacts.setup {stable_endpoint stable<id6>, treatment_endpoint treat<id6>, v_current, v_candidate, ramp_stage 0, weights [90,10], policy_narrowing, online_evaluators, primary_evaluator}`; read-only AWS `list-agent-runtime-endpoints` shows both endpoints READY; two local configs `can_<id8>_oec/_oet` (read-only) | canary.json, aws-readonly.json |
| AC-E13-05 | 50/50 traffic (card ④): `traffic {dataset_id golden, sessions_per_arm 10}` | 202 → `artifacts.rounds[].traffic_attempts[] {sent, failed, arms {C, T1}, status_counts, primary_baseline_n}`; `eval_results` rows stamped with `arm`; chats during the round route stickily by endpoint qualifier | attempt.json, cw/ |
| AC-E13-06 | `verdict` | 202 → card `{verdict, primary cust_<…>, arms_n {control, treatment}, min_arm_n 10, significant, metrics[]}`; UI renders "Decided by the primary evaluator <id>" from `verdict.primary` (no `decided_by` key exists — the console derives it); `insufficient-n` reason names the primary and both arm counts | verdict.json, shot |
| AC-E13-07 | `advance` to 50/50 then 1/99 (`allow_non_significant` on tie / insufficient-n) | `setup.ramp_stage 1 → 2`, `weights [50,50] → [1,99]`; a new round pops the previous `verdict` | canary.json |
| AC-E13-08 | `complete` at 1/99 **or** `rollback` | complete: `status completed`, `agent.version == v_candidate`, prompt = candidate, `policy_stale` cleared; rollback: `status rolled_back`, `agent.version == v_restored` (new version, prompt unchanged). Both are PASS when narrated honestly (N-SCN-obs22 ruling: 50/50 reached + verdict names the primary + cleaned) | agent.json before/after |
| AC-E13-09 | `cleanup` | 202 → `status cleaned`, `artifacts.cleanup[] {category, status deleted\|skipped}` covering `abtest`, `online-eval ×2`, `endpoint ×2`, `endpoints-gone`, `assignments`, `s3:<superseded zip>`; endpoints gone ≤ 5 min (eventual, N-SCN-obs3); ledger row + `eval_results` remain | cleanup.json, aws-readonly.json |

## 3. Negative / boundary cases (≥ 6)
| id | input | expected | source | risk |
|---|---|---|---|---|
| NC-E13-01 | BYOC agent / preset (`system_key`) / A2A agent as champion | `400 canary.agent_unsupported {agent_id, canary_capability {reason, reason_code}}`; inactive or hidden agent → `400 canary.agent_not_active` | `canary_routers.py` 157, 165 | — |
| NC-E13-02 | `candidate: {}` or blank prompt | `400 canary.candidate_empty` | `canary_routers.py` 243, 253 | — |
| NC-E13-03 | `primary_evaluator` not in `online_evaluators`; categorical custom judge; trajectory evaluator | `400 canary.primary_not_selected {primary_evaluator, online_evaluators}` / `400 canary.evaluator_categorical` / `400 canary.evaluator_unsupported` — before any row (N12-obs46) | `canary_routers.py` 128–143; `online_evaluators.py` 103–163 | — |
| NC-E13-04 | second canary on wb006 while one is `running` | `409 canary.already_running` | `canary_routers.py` 184 | — |
| NC-E13-05 | `traffic` before `setup`; `verdict` before traffic; `complete` at stage 0 / 1; `advance` at 1/99 | `409 canary.stage_not_ready` with "run canary setup first" / "send traffic at the current weights first" / "reach the final 1/99 stage first" / "the final 1/99 stage must be completed, not advanced" | `canary_service.py` `stage_not_ready_reason` 305 | — |
| NC-E13-06 | `traffic` without `dataset_id`; with a `simulated` dataset | `422 canary.dataset_required` / `422 canary.dataset_unsupported` | `canary_routers.py` 341; `traffic.py` `resolve_traffic_items` | — |
| NC-E13-07 | `advance` / `complete` on `control-wins`; `complete` on `insufficient-n` even with `allow_non_significant` | `409 canary.verdict_blocked` (message names `sessions_per_arm ≥ 10` and both arm counts); a significant `control-wins` is terminal (N-SCN-obs15) | `canary_service.py` `assert_verdict_allows` 338 | — |
| NC-E13-08 | `advance` on `tie` / `insufficient-n` without `allow_non_significant` | `409 canary.override_required` | `canary_service.py` | — |
| NC-E13-09 | any action while `running_action` is set | `409 canary.action_in_flight` | `canary_routers.py` 328 | — |
| NC-E13-10 | action on a canary whose champion became hidden (isolation) / is a preset | `404 agent.not_found` / `agent.system_managed` refusal | `canary_routers.py` 317 | — |
| NC-E13-11 | `cleanup` twice; `cleanup` on a `cleaned` row | 202 both times, not-found deletes swallowed, categories `skipped` | `canary_service.py` `act_cleanup` 1100 | — |
| NC-E13-12 | **Fault injection:** runtime guard on but the live role lacks `PolicyBundleRead` | `setup` 202 then row `error "setup: AppError: canary.policy_guard_unprovisioned …"` (raised on the thread, not as HTTP 409); apply/redeploy first, then Retry (N12-obs45, live re-test pending) | `canary_infra.py` 168–342 | — |
| NC-E13-13 | **Fault injection:** replay `internalServerException` ×2 on one session | worker retries in the same session with 5 s / 15 s backoff (r12-f); attempt `failed` count unchanged; **known gap N12-obs54:** the attempt record carries counts only, no per-session reason — PASS requires the `invocations.jsonl` line (`retried[]`, `error`) in evidence | N12-obs54/55 | — |
| NC-E13-14 | **Fault injection:** `systemctl restart launchpad-backend` during `traffic` | row `error "<action>: interrupted by a backend restart — retry the action"`, `status running` unchanged, `running_action` cleared; Retry succeeds | `canary_service.py` `clear_stale_running_actions` 116 | — |
| NC-E13-15 | small replay at 90/10 without `sessions_per_arm` | `insufficient-data` / `insufficient-n` (N-SCN-obs2/obs14 — expected behaviour, not a defect); use `sessions_per_arm ≥ 10` | `verdict_min_arm_n` default 10 (`config.py` 436) | — |
| NC-E13-16 | `hr-intern` runs `complete` / `rollback` on the admin's canary | **Today: 202** (MEMBER, no creator rule) although it changes the production version. **Open ruling (R9):** expected creator-or-admin like agent redeploy (`403 agent.forbidden`) | `route_policy.py` 384 | — |

## 4. Evidence
Per case `request.json`, `response.json`, `canary.json` after each action, `verdict.json`, `shot.png` (verdict card with "Decided by"), `aws-readonly.json` before setup / after setup / after cleanup (`list-agent-runtime-endpoints`, `get-agent-runtime` version), `cw/<session_id>.jsonl` for one control and one treatment session (log group `/aws/bedrock-agentcore/runtimes/<rid>-<endpoint>`), worker `invocations.jsonl` for the fault cases.

## 5. Automation mapping and GAPs
| case | Existing automation | Status |
|---|---|---|
| AC-01 | `frontend/src/lib/evaluation-url.test.ts` (7); e2e `48-experiment-canary.spec.ts`, `s08-improve.spec.ts::s08b` (r13-b version asserts the 404s + providers path) | covered after r13-b |
| AC-02/03, NC-01/02/03/04 | `backend/tests/optimization/test_runtime_canaries.py::test_create_*`, `test_capability_matrix.py::test_canary_capability_matrix`, `test_canary_evaluators.py` (30), `test_visibility.py::test_experiment_and_canary_references_respect_isolation`; e2e `canary-ui.ts::createCanaryViaUi` | covered |
| AC-04/05/06/07, NC-05/07/08/15 | `test_runtime_canaries.py::test_setup_mints_candidate_pins_endpoints_and_opens_the_local_ab_row`, `::test_traffic_*`, `::test_verdict_gate`, `::test_complete_requires_final_stage`, `::test_final_stage_must_complete_instead_of_advance`; `test_canary_routing.py`, `test_traffic.py` (12), `test_verdict_polarity.py` (21); e2e `targeted/t01-canary-ramp.spec.ts::t01b`, `s08c`, `case 31b` | covered |
| AC-08/09, NC-11/12 | `::test_complete_records_candidate_as_production`, `::test_rollback_rolls_forward_current_spec`, `::test_cleanup_*` (9), `test_canary_policy_parity.py` (11) | covered hermetic; N12-obs45 live re-test **pending** |
| NC-06 | `test_runtime_canaries.py::test_traffic_action_requires_dataset`; `dataset_unsupported` on the canary route | partial: 422 `dataset_unsupported` **GAP** (moves to `test_traffic.py` in r13-b — verify) |
| NC-09/10 | none | **GAP** → new `test_canary_action_guards.py` |
| NC-13 | `test_eval_worker_template.py::test_a_replay_5xx_error_payload_is_retried_in_the_same_session` (worker side) | partial; N12-obs54 **pending ruling** |
| NC-14 | `test_weights_and_cleanup.py::test_clear_stale_canary_actions_is_retryable` | covered hermetic; process drill **GAP** → Q4a |
| NC-16 | none (no rule) | **GAP** + open ruling |

## 6. Failure classification
ENV: Runtime version wait / endpoint READY wait beyond 15 min, `DeleteAgentRuntimeEndpoint` eventual consistency (endpoints listed ≤ 5 min after `cleaned` — N-SCN-obs3), judge/replay 5xx that exhausts 3 attempts; TEST: expecting a decidable verdict at 90/10 without `sessions_per_arm` (N-SCN-obs17), forcing `complete` instead of accepting rollback (N-SCN-obs22); PRODUCT: a row `error` other than the restart message, endpoints left after cleanup with `status deleted`, production version changed without `complete`.

## 7. Open rulings
- Rulings are recorded inline in the rows marked "ruling": AC-E13-08, NC-E13-16.
