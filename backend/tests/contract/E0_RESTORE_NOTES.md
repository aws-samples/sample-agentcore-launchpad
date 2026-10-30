# N12 E0-2 — verbatim restore of the evaluation / optimization backend: what changed

Baseline: `4e27b1f4` (Global fork). Restored verbatim with `git archive 4e27b1f4 -- …`:
`backend/app/evaluation/**`, `backend/app/optimization/**`, `backend/tests/evaluation/**`,
`backend/tests/optimization/**`, `backend/tests/test_invoke_canary_route.py`, and the five
`backend/scripts/e2e_{eval_run,eval_extended,online_eval,experiment,runtime_canary}.py`
(the e2e scripts are not run by the gate; E1/E2/E3 adapt them).

Every AWS wrapper (`agentcore_eval.py`, Gateway/ABTest/ConfigurationBundle paths) is still
present so the restored suites run against the same stubs they did on the baseline; the
engine replacement is W-E1 … W-E3's work. The only edits made here are the ones needed for
`import app.main` to succeed in the CN tree and for the gates (ruff, pytest, cn_lint) to pass.

## Source edits (delete, not flag)

| file | edit | why |
|---|---|---|
| `evaluation/recommendations.py` | dropped `services.agentcore.{harness,policy}` imports, `_gateway_actions`, `_is_harness`, `_selected`, `_harness_inputs`; `resolve_inputs` reads every agent from its spec; `managed_reference_gap` → `evaluation.managed`; builtin evaluator ARN via `ac.builtin_evaluator_arn(…, workspace.partition)` | managed-Harness / Policy control-plane APIs absent in CN; C2 |
| `evaluation/service.py` | dropped `_harness_telemetry` + the `method == "harness"` telemetry branch (a 方式B agent is a runtime in CN), the `InvokeHarness` replay branch, the JWT-bearer replay branch (`rt.invoke_runtime_text_bearer`, `inbound_auth.m2m_bearer_token`, `require_platform_reachable`) | managed Harness and JWT inbound do not exist in CN |
| `evaluation/simulation.py` | dropped the `InvokeHarness` branch | same |
| `evaluation/routers.py`, `evaluation/online_evaluators.py` | `app.assistant.evaluation_assets` / `evaluation_plan` lazy imports → `evaluation/managed.py` shims (always "not managed"); the code-rule conflict / legacy-package checks that only run for managed evaluators are gone with them; `strip_generated_prompt` → `evaluation/generated_prompt.py` (CN twin on `templates.kb_support`) | W-E5 restores the assistant's evaluation assets and replaces the shim bodies |
| `evaluation/agentcore_eval.py` | `_GSR_EVALUATOR_ARN` literal → `builtin_evaluator_arn(evaluator, partition)` (+ `_client_partition` fallback for the default kwarg); `to_log_group_arn` partition-aware; docstrings partition-neutral | C2 (`cn_lint`) |
| `optimization/canary_service.py` | dropped `canary_harness` / `services.agentcore.harness` / `services.memory` imports, `is_harness`, `start_harness_canary`, `_setup_harness`, `_paired_harness_traffic`, `_paired_verdict` (+ `PAIRED_*`), `_rollback_harness`, every `is_harness(row)` branch (setup / traffic / verdict / rollback / cleanup); `early_complete_allowed` → always `False` (it was Harness-only) | C5: the managed-Harness canary is deleted (`canary_harness.py` removed); W-E3 finishes the service-side rewrite |
| `optimization/canary_routers.py` | dropped `_validate_harness_versions` and the `method == "harness"` create branch; `HarnessVersions` / `harness_versions` / `start_stage` **stay in the schema until E0-3** (the contract snapshot is first proven equal with an empty delta, then CA-1a removes them) | sequencing of E0-3 |
| `optimization/service.py` | `services.agentcore.gateway.sigv4_post` (absent in CN) inlined as a private twin (`SESSION_HEADER`, `USER_ID_HEADER`, `sigv4_post`); the system-prompt recommendation passes the workspace-partition evaluator ARN | import-clean; W-E3 deletes `send_gateway_traffic` with it |
| `optimization/providers/registry.py` | `_MODEL_LABELS` → the two CN catalog ids | C3 |
| `core/config.py` | `traffic_concurrency`, `eval_max_concurrent_runs` (le=20), `eval_batch_wait_s` (le=28800), `prompt_opt_*` (CN model defaults) | the restored modules read them at import |
| `core/features.py` | `evaluation`, `optimization` added to `Feature` / `FEATURES` | routers mount conditionally |
| `core/db.py` | the seven baseline tables in `WORKSPACE_SCOPED_TABLES` + `_migrate_workspace_columns`; `_migrate_evaluation_columns` (baseline ALTERs) | `test_ledger_migration`, `test_datasets_v2::test_migrate_*` |
| `core/route_policy.py` | the baseline `/api/eval/**`, `/api/experiments/**`, `/api/runtime-canaries/**` rows (+ `PERM_EVAL_RUN`); `/api/traces/{session_id}` and the W-E5 evaluation-plan rows are **not** restored | default-deny table |
| `services/users.py` | `eval.run` back in `AGENT_PERMISSIONS` (baseline position) | `PERM_EVAL_RUN` must be a known role |
| `main.py` | eval / online / pipelines routers + `resume_interrupted_runs` under `"evaluation" in features`; experiments / canaries routers + the two `clear_stale_running_actions` under `"optimization"` | plan E0-2 |

## Tests removed (cannot pass in CN — the subject is deleted)

- `tests/optimization/test_harness_canaries.py`, `tests/optimization/test_canary_paired.py` (managed-Harness canary).
- `tests/evaluation/test_eval_transient_retry.py` (drives the replay through `invoke_harness` streams; W-E1-4 re-creates the transient-retry coverage against the executor).
- `tests/test_invoke_canary_route.py` (asserts the canary **Gateway** route in `services/invoke.py`; W-E3-2 replaces it with `test_invoke_routing.py`).
- `tests/evaluation/test_run_recommendations.py`: `test_allowed_tools_selection`, `test_harness_inputs_come_from_get_harness`, `test_unreadable_gateway_is_a_note_not_a_failure`, `test_a_deleted_harness_falls_back_instead_of_failing`, `test_catalogs_are_cached_but_the_harness_is_read_live`, `test_gateway_targets_are_read_in_parallel`.
- `tests/evaluation/test_runs_flow.py`: `test_harness_run_completes`, `test_harness_without_telemetry_group_rejected`.
- `tests/evaluation/test_simulation.py`: `test_adapter_dispatches_harness`.
- `tests/optimization/test_canary_infra.py`: `test_mint_candidate_container_is_follow_up` (方式A `container` is not an `AgentSpec.method` in CN).

## Tests marked `xfail(strict=True)` — pending another group

- `tests/evaluation/test_online_surfaces.py` (3): `ObsSessionDetail.online_scores` and `GET /api/overview/online-quality` — **W-E2-3**.
- `tests/optimization/test_tool_evidence.py` (3): `observability.eval_tool_turns_from_content_logs` / `eval_turns_from_content_logs` were deleted from CN `services/observability.py` by N1 — **W-E2** restores them with the evidence collector (coordinate the `observability.py` hunk through the lead).

## Tests adapted (CN values, same assertions)

- `test_evaluator_crud.py`: `LAMBDA_ARN` is an `arn:aws-cn:lambda:cn-northwest-1:…` function; the region-mismatch case uses `cn-north-1` vs the default workspace `cn-northwest-1`.
- `test_run_recommendations.py`: `test_accept_drops_the_kb_section_the_deployer_appends` / `test_strip_generated_prompt_tolerates_a_reflowed_kb_section` use `templates.kb_support.kb_prompt_section` and `evaluation.generated_prompt`; `test_start_pins_the_batch_and_the_chosen_evaluator` expects the `aws-cn` evaluator ARN.
- `test_rec_adversarial_exclusion.py`: `test_the_golden_test_snapshot_carries_the_flag_only_when_set` removed (`assistant.evaluation_plan`, W-E5).
- `test_gateway_traffic_concurrency.py`: `USER_ID_HEADER` imported from `optimization.service`.
- `tests/test_capabilities.py`: twelve features.

Result on this branch: `uv run pytest tests/evaluation tests/optimization -q -n auto` → 575 passed, 6 xfailed; full backend suite green.
