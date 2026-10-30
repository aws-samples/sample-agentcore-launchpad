# E10 Evaluators 「评估器」 (section `eval-evaluators`, `/v2/eval/evaluators`) — acceptance standard

**Risk score:** 7.5 (card: 2 / 2 / 3 / 0.5; pure ledger CRUD, judge model only exercised by E11/E12) → negative cases ≥ 3 · **Recorded build:** to fill · **Status:** draft

## 1. Scope and roles
- **Routes (`route_policy.py` 342–346):** `GET/POST /api/eval/evaluators`, `GET/PUT/DELETE /api/eval/evaluators/{id}` — all `MEMBER`. The judge-model list comes from `GET /api/capabilities.models[]` (admin model registry, E21).
- **Roles:** `demo-admin` creates `hr_guardrails_judge` (Kimi K3) and reads the 19 builtins; `hr-intern` reads and — in `shared` mode — may edit/delete any custom evaluator (**no creator-or-admin rule**, `created_by` is display + isolation only). Builtins have no creator.
- **Out of scope:** blocking / threshold semantics (they live on the assistant's evaluation plan, E02 — an evaluator row has **no threshold or blocking field**); code (Lambda) evaluators (`eval_code_evaluators_enabled` false by default); model registry edits (E21).
- **Fixture note:** the API name pattern is `^[a-zA-Z][a-zA-Z0-9_]{0,47}$`, so the storyline name must be `hr_guardrails_judge` (the card's `hr-guardrails-judge` is refused — NC-01). The judge prompt must not reference `{expected_response}` / `{assertions}` if the same evaluator is reused online or as a canary primary (E12 / E13 refuse ground-truth judges).

## 2. Capabilities and pass criteria (positive)
| id | action | observable pass criteria | evidence |
|---|---|---|---|
| AC-E10-01 | Catalog | `GET /api/eval/evaluators` 200, `builtin_count 19`; `Builtin.Helpfulness level TRACE`, `Builtin.GoalSuccessRate level SESSION`, the three `Builtin.Trajectory*` carry `requires_ground_truth true`; UI groups Builtin / Custom | response.json, shot |
| AC-E10-02 | Create `hr_guardrails_judge` (V2 form) | `POST` 201 `{evaluator_id cust_<12hex>, arn arn:aws-cn:bedrock-agentcore:<region>:<acct>:evaluator/<id>, model_fallback null, created_by, created_by_name}`; body `model_id global.moonshotai.kimi-k3`, `level TRACE`, `description ≤ 200`, default `rating_scale` pass 1.0 / fail 0.0, instructions containing `{…}` placeholder | request.json (prompt), response.json |
| AC-E10-03 | Model picker | exactly the registry catalog: `global.zai.glm-5.3` (default, selected) and `global.moonshotai.kimi-k3`; providers whose SSM key is missing render `aria-disabled` with the parameter name as tooltip; no `siliconflow.*` selectable | shot (picker open) |
| AC-E10-04 | Detail and edit | `GET …/{id}` returns the frozen key set (`id, name, level, description, definition, instructions, rating_scale, model_id, …, created_by`); `PUT` with new instructions 200, `name` unchanged (immutable); omitted `level`/`description` reset to defaults (full replace) | response before/after |
| AC-E10-05 | Derived evaluator (Helpfulness on Kimi) | `POST {name, base_evaluator_id Builtin.Helpfulness, model_id kimi-k3}` 201, `evaluator_type CustomDerived`, level = base level | response.json |
| AC-E10-06 | Where it is referenced | the new id is selectable in the E11 wizard and the E13 canary primary select; run detail shows `evaluator_id` for the custom row | shot (E11 wizard) |
| AC-E10-07 | Delete | `DELETE` 200 `{deleted true}`; repeat → `404 evaluator.not_found`; builtins list unchanged | response.json ×2 |

## 3. Negative / boundary cases (≥ 3)
| id | input | expected | source | risk |
|---|---|---|---|---|
| NC-E10-01 | `name: "hr-guardrails-judge"` (hyphen) / starts with a digit | `422 validation.invalid_request` (pattern), readable field error | `routers.py` `JudgeCreate` 749–765 | — |
| NC-E10-02 | same `name` twice in the workspace | `409 evaluator.name_taken` | `evaluators_local.py` 120 | — |
| NC-E10-03 | instructions without any `{placeholder}` | `422 evaluator.missing_placeholder` | `routers.py` 839 | — |
| NC-E10-04 | `model_id anthropic.claude-sonnet-4-5` / `siliconflow.deepseek-ai/DeepSeek-V4-Pro` | `422 model.not_allowed` / `422 model.provider_unconfigured` naming `launchpad-siliconflow-api-key` | `evaluators_local.py` 94; `schemas/agent.py` 494–500 | — |
| NC-E10-05 | `PUT` / `DELETE /api/eval/evaluators/Builtin.Helpfulness` | `400 evaluator.builtin_immutable` | `routers.py` 973, 1043 | — |
| NC-E10-06 | `instructions` + `base_evaluator_id` together; `rating_scale` on a derived evaluator; `PUT` switching definition type | `400 evaluator.definition_ambiguous` / `400 evaluator.rating_scale_not_allowed` / `400 evaluator.definition_mismatch {current, payload}` | `routers.py` 785, 792, 997 | — |
| NC-E10-07 | `description` 201 chars; `instructions` 9 chars; `rating_scale` with 1 entry | `422 validation.invalid_request`, single-field error (cf. N-ARCH-obs14 for the assistant path) | `JudgeCreate` limits | — |
| NC-E10-08 | `DELETE` an evaluator referenced by a run, an online config or a canary | **Today: 200, no referential guard**; next `POST /api/eval/runs` with the id → `422 run.evaluator_not_found`; what a running online config does is undeterminable from code. **Open ruling:** expected `409 evaluator.in_use` | `evaluators_local.py` 345 | — |
| NC-E10-09 | `DELETE` an evaluator materialised by the assistant evaluation plan (E02) | `409 evaluator.managed_by_operation` with the owning operation in `detail` | `routers.py` 1052 | — |
| NC-E10-10 | code evaluator (`lambda_arn`) while the flag is off | `403 evaluation.code_evaluators_disabled {setting}` | `routers.py` 806 | — |
| NC-E10-11 | E21 admin deletes the Kimi model row while this evaluator references it | `409 model.in_use` (cross-module, `routers/model_providers.py`) | s12c note; `test_delete_model_rules_and_referrers` | — |

## 4. Evidence
`request.json` (judge prompt is content, not a secret), `response.json`, `shot.png` (picker with the disabled SiliconFlow rows), `ledger.redacted.json` (`eval_evaluators` row). SSM parameter *names* may appear; values never.

## 5. Automation mapping and GAPs
| case | Existing automation | Status |
|---|---|---|
| AC-01 | `backend/tests/evaluation/test_evaluator_catalog.py::test_catalog_builtins_only`; `test_evaluator_mapping.py::test_catalog_mirrors_the_contract_ids_and_levels` | covered |
| AC-02/04/05/07, NC-02/03/05/06/09 | `evaluation/test_evaluator_crud.py::test_create_full_params_writes_the_row`, `::test_create_duplicate_name_is_409`, `::test_create_missing_placeholder_rejected`, `::test_update_builtin_rejected`, `::test_update_definition_mismatch_rejected`, `::test_create_both_definitions_rejected`, `::test_derived_create_rating_scale_rejected`, `::test_delete_managed_rejected`, `::test_delete_removes_the_row`; e2e `41-evaluators.spec.ts::case 24`, `scenario/s07-evaluate.spec.ts::s07a` | covered |
| AC-03, NC-04 | `test_evaluator_crud.py::test_create_refuses_a_model_outside_the_catalog`, `::test_create_refuses_a_judge_model_whose_provider_is_not_configured`, `::test_create_accepts_the_alternative_catalog_model`; `test_judge_model_factory.py::test_kimi_k3_gets_no_temperature_and_glm_keeps_it`; e2e `case 24` (disabled rows) | covered |
| AC-06 | e2e `43-batch-run.spec.ts::case 26` (custom id in a run), `48-experiment-canary.spec.ts::case 31b` (primary select) | partial: deep link from evaluator detail to its runs **GAP** |
| NC-01/07 | none | **GAP** → Q6a schemathesis on `JudgeCreate` |
| NC-08 | none (no guard) | **GAP** + open ruling |
| NC-10 | `test_evaluator_crud.py::test_code_definition_refused_while_the_flag_is_off` | covered |
| NC-11 | `backend/tests/test_model_registry.py::test_delete_model_rules_and_referrers` | covered hermetic; live skipped (s12c note) |

## 6. Failure classification
No AWS call on this module (ARNs are computed locally); failures are TEST (picker selector, catalog count after an E21 change) or PRODUCT. The only ENV signal is `GET /api/capabilities` reporting a provider `available:false` because the SSM parameter was not readable at startup.

## 7. Open rulings
- Rulings are recorded inline in the rows marked "ruling": NC-E10-08.
