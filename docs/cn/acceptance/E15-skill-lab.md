# E15 Skill Lab 「技能实验室」 (section `skill-lab`, `/v2/skill-lab`) — acceptance standard

**Risk score:** 14.5 (card: 4 / 6 / 4 / 0.5; three ≤ 30-min worker jobs on a microVM Runtime, Global Bedrock through OpenCode, publish mutates the Registry, R6 restart, R9 no creator rule) → negative cases ≥ 10 · **Recorded build:** to fill · **Status:** draft

## 1. Scope and roles
- **Routes (`route_policy.py` 283–305):** `GET /api/skill-lab/status`, `POST …/task-assets`, `GET/POST …/tasksets`, `GET/PUT/DELETE …/tasksets/{id}`, `GET/POST …/jobs`, `GET …/jobs/{id}`, `POST …/{id}/cancel|resume|publish|import-taskset|apply-expansion`, `DELETE …/jobs/{id}`, `GET …/{id}/train-summary|diff|log|results|artifacts|artifacts/raw` — all `MEMBER`.
- **Roles:** `demo-admin` runs the chain on the seed skill `expense-report-writer`; `hr-intern` can read and — with no creator-or-admin rule coded — cancel, delete, publish any visible job or edit any taskset (narrate as R9; open ruling NC-15). `created_by` stamped on tasksets, jobs and imported tasksets.
- **Out of scope:** Registry approval flow and "use in new agent" deployment (E16 / E03 — this video only verifies the record is attachable), agentic judge mode (`opencode` + `bwrap`, "wired but unproven", n13 §6.2), assumed-role workspaces.
- **Expectation correction:** the card's risk "trainer refuses a single-mode task set" does not match the code — training accepts single-mode sets (ratio split 4:3:3, `runner.py` 641–652). The s03 seed fallback is triggered by `diff.changed == false` (no accepted edit → `publish_no_change`), not by a refusal.

## 2. Capabilities and pass criteria (positive)
| id | action | observable pass criteria | evidence |
|---|---|---|---|
| AC-E15-01 | Status | `GET /api/skill-lab/status` 200 `provisioned true`, `missing []`, `venv_ready true`, `target_backends ["opencode_exec"]`, `default_target_model global.moonshotai.kimi-k3`, `default_judge_model global.zai.glm-5.3`, `judge_modes ∋ chat` | response.json |
| AC-E15-02 | gen-new (taskgen) from `expense-report-writer` → import `hr-expense-tasks` | `POST /api/skill-lab/jobs {type taskgen, skill_source {kind registry, record_id}, params {count 5}}` 201 `queued` → `succeeded` ≤ 30 min; `GET …/results {type taskgen, count 5}`; `POST …/import-taskset {name hr-expense-tasks}` 201 `{job, taskset {mode single, counts.tasks 5}}`, `params.imported_taskset_id` set | job.json, results.json, taskset.json |
| AC-E15-03 | train (1 epoch) | `POST {type train, skill_source, taskset_id, params {epochs 1}}` 201 → `succeeded` ≤ 30 min; `GET …/train-summary {steps[] ≥ 1, best_step, best_score, totals}`; `GET …/diff {changed}` — narrate honestly when `changed false` | job.json, train-summary.json, shot (curve) |
| AC-E15-04 | eval (split omitted → `tasks`) | 201 → `succeeded`; `GET …/results {summary {tasks 5, passed, pass_rate, errored, invalid, token_usage}, rows[]}`; `GET …/log?offset=0 {content, next_offset, eof}` streams during the run; `GET …/artifacts` lists `out/` | results.json, log.txt head |
| AC-E15-05 | publish → version +1 | `POST …/jobs/{train}/publish {reapprove true}` 200 `{record_id, new_version 1.1.0-skill (minor bump), status_before, status_after APPROVED when it was APPROVED, reapproved}`; Registry record shows the new version | response.json, registry.json |
| AC-E15-06 | "Use in new agent" check (no deploy) | Registry record detail offers "Evaluate in Skill Lab" (`?tab=eval&view=new&record=`), the agent wizard lists the skill as attachable at the new version; no agent is created | shot ×2 |
| AC-E15-07 | Cancel a queued job | `POST …/cancel` 200 `status cancelled` immediately; a running job → `cancel_requested` then `cancelled` (process group killed; the microVM idles out ≤ 5 min) | job.json |
| AC-E15-08 | Member view | `hr-intern` lists tasksets and jobs with `created_by_name demo-admin`; sample `sample-*` tasksets are read-only for everyone | shot |

## 3. Negative / boundary cases (≥ 10)
| id | input | expected | source | risk |
|---|---|---|---|---|
| NC-E15-01 | split-mode taskset with `train` but no `val`; unknown split key | `422 skill_lab.taskset_invalid detail [{split, message}]` | `tasksets.py` 92–103 | — |
| NC-E15-02 | taskset `name` 97 chars / blank; a split with 0 or 2001 tasks | `422 validation.invalid_request` / `422 skill_lab.taskset_invalid` ("at least one task is required") | `tasksets.py` 108–116, 466–468 | — |
| NC-E15-03 | `PUT` / `DELETE` a `sample-*` taskset; taskgen expansion targeting a sample | `409 skill_lab.sample_readonly` | `tasksets.py` 410–420; `jobs.py` 450 | — |
| NC-E15-04 | `DELETE` a taskset referenced by any job (any status) | `409 skill_lab.taskset_in_use` | `tasksets.py` 626–631 | — |
| NC-E15-05 | train with `split`; `workers 9`; `timeout 30`; eval with `split` on a single-mode set | `422 skill_lab.bad_params` ("training uses the whole task set — omit `split`") / `422 bad_params` ×2 / `422 skill_lab.taskset_invalid` | `routers.py` 221–226; `runner.py` 73–121; `jobs.py` 107–126 | — |
| NC-E15-06 | publish an eval job, a `failed` train, or a train whose `diff.changed` is false | `400 skill_lab.publish_unsupported` / `400 skill_lab.publish_no_change`; registry refusal (DEPRECATED record) passes through as `400 registry.not_editable` | `jobs.py` 941–1025 | — |
| NC-E15-07 | `import-taskset` twice; on a `running` taskgen; `apply-expansion` when a generated id now exists | `409 skill_lab.already_imported` / `409 skill_lab.job_not_finished` / `409 skill_lab.expansion_conflict {ids}` | `jobs.py` 750–868 | — |
| NC-E15-08 | `cancel` a `succeeded` job; `DELETE` a `running` job; `resume` an eval job or a `succeeded` train | `400 skill_lab.job_not_cancellable` / `400 skill_lab.job_not_deletable` / `400 skill_lab.job_not_resumable` | `jobs.py` 871–908, 1120–1158 | — |
| NC-E15-09 | task-assets: 33 files; one 26 MiB file; `.exe`; `Content-Length` > 101 MiB | `422 skill_lab.asset_limit_exceeded` / `413 skill_lab.asset_too_large` / `422 skill_lab.asset_format_invalid` / `413 skill_lab.asset_request_too_large` (before multipart parsing) | `task_assets.py` 85–111, 170–213 | — |
| NC-E15-10 | `GET …/artifacts?path=../../etc/passwd` | `400 skill_lab.bad_path`; unknown file → `404 skill_lab.artifact_not_found` | `artifacts.py` 365–446 | — |
| NC-E15-11 | worker resources missing from the workspace; assumed-role workspace | `503 skill_lab.not_provisioned` / `400 skill_lab.workspace_unsupported` at `POST /jobs` (status still answers 200) | `runner.py` 124–153 | — |
| NC-E15-12 | `GET …/results` before `out/results.json` exists | `404 skill_lab.results_pending` naming the job status | `routers.py` 317–322 | — |
| NC-E15-13 | **Fault injection:** hub without `AWS_BEARER_TOKEN_BEDROCK` | **Today:** `POST /jobs` 201, then `failed` with `error "failed to start: the hub cannot reach Global Bedrock: …"`. **Open ruling:** expected `503 skill_lab.model_credential_missing` at submit (`/status.provisioned` also ignores `artifacts_bucket` and the bearer) | `jobs.py` 1068–1089; `runner.py` 486–493 | — |
| NC-E15-14 | **Fault injection:** `systemctl restart launchpad-backend` during train / during eval | both → `interrupted` with the type hint (train: "resume it…", eval: "submit the job again"); `POST …/resume` on the train → 200 re-queued, continues from `out/runtime_state.json`, `train-summary.steps` not reset | `jobs.py` 1173–1200, 871–908 | — |
| NC-E15-15 | `hr-intern` cancels / deletes / publishes the admin's job | **Today: 200** (MEMBER, no creator rule; publish mutates the Registry). **Open ruling (R9):** creator-or-admin on `publish`, `DELETE`, `cancel`, taskset `PUT/DELETE` | `routers.py` 139–168, 269–289 | — |
| NC-E15-16 | every rollout errors (worker image unreachable) | job `failed` with `all N rollouts failed (I infrastructure · E exec errors) — nothing was scored; first error: …`; a partially errored eval stays `succeeded` with `summary.errored > 0` | `verdict.py` 91–104 | — |
| NC-E15-17 | taskgen `count 31`; `guidance` 4001 chars; 9 attachments | `422 skill_lab.bad_params` / `422` / `422 skill_lab.asset_limit_exceeded` (aggregate > 25 MiB → `413`) | `runner.py` 322–382; `jobs.py` 342–412 | — |

## 4. Evidence
Per case `request.json`, `response.json`, `job.json` at terminal state, `results.json` / `train-summary.json` / `diff.json`, `log.txt` head + tail (worker log lines `[skilleval] rollout finished: X ok, Y errored, Z invalid`), `shot.png` (curve, results, Registry version), `registry.json` before/after publish, `cw/` runtime log of the worker (`/aws/bedrock-agentcore/runtimes/<skill-lab-worker>-DEFAULT`) first 50 lines on failure. The hub bearer and Identity provider names never appear in shots.

## 5. Automation mapping and GAPs
| case | Existing automation | Status |
|---|---|---|
| AC-01, NC-11 | `backend/tests/test_skill_lab_tasksets.py::test_status_reflects_worker_resource_keys`; `test_skill_lab_jobs.py::test_submit_refused_without_worker_resources`, `::test_submit_refused_on_assumed_role_workspace`; e2e `23-skill-lab-nav-status.spec.ts::case 23` | covered |
| AC-02, NC-07/17 | `test_skill_lab_taskgen.py::test_generate_review_import`, `::test_import_guards`, `::test_expansion_conflict_when_taskset_changed`, `::test_clamp_taskgen_params`, `::test_attachment_count_and_aggregate_bytes_are_bounded`; e2e `25-skill-lab-taskgen.spec.ts::case 25`, `scenario/s03-skill-lab.spec.ts::s03a` | covered (HR fixtures) |
| AC-03/05, NC-05/06 | `test_skill_lab_train.py::test_train_lifecycle_summary_and_diff`, `::test_train_param_bounds_and_split_refusal`, `::test_publish_happy_path`, `::test_publish_reapproves_only_previously_approved`, `::test_publish_refusals`, `::test_publish_no_change_refused`; e2e `26::case 26`, `28::case 28`, `s03b`, `s03c` | covered |
| AC-04, NC-12/16 | `test_skill_lab_jobs.py::test_lifecycle_success_with_results_and_log`, `::test_all_rollouts_errored_fails_the_job_with_a_readable_reason`, `::test_partially_errored_run_stays_succeeded_but_counts_the_errors`; e2e `27::case 27` | covered (`log` offset/`eof` **GAP**) |
| AC-06 | e2e `28::case 28` mounts via the agent wizard | partial: TrainDetail "open registry" button and the Registry → Skill Lab link **GAP** |
| AC-07, NC-08 | `test_skill_lab_jobs.py::test_cancel_while_queued`, `::test_cancel_running_kills_the_process_group`, `::test_terminal_jobs_cannot_cancel_but_can_delete`; `test_skill_lab_train.py::test_resume_refusals` | partial: `DELETE` on a running job **GAP** |
| AC-08, NC-03/04 | `test_skill_lab_samples.py::test_sample_tasksets_seed_idempotently_and_are_read_only`; `test_skill_lab_jobs.py::test_taskset_delete_blocked_while_referenced`; e2e `24::case 24` | covered |
| NC-01/02 | `test_skill_lab_tasksets.py::test_create_split_requires_train_and_val`, `::test_name_longer_than_the_column_is_refused`, `::test_blank_rename_is_refused_not_silently_ignored` | covered |
| NC-09/10 | `test_skill_lab_task_assets.py::test_limits_are_centralized_and_enforced`, `::test_content_length_limit_rejects_before_multipart_parsing`; `test_skill_lab_jobs.py::test_artifact_traversal_guard_and_reads` | covered |
| NC-13 | `test_skill_lab_jobs.py::test_a_hub_without_the_bearer_refuses_the_job_at_submit` (calls the function, not HTTP) | **GAP** at HTTP level + open ruling |
| NC-14 | `test_skill_lab_jobs.py::test_interrupted_sweep`, `test_skill_lab_train.py::test_interrupted_train_is_told_to_resume`, `::test_resume_continues_from_disk_state` | covered hermetic; process drill **GAP** → Q4a |
| NC-15 | none (no rule) | **GAP** + open ruling |

## 6. Failure classification
ENV: worker Runtime cold start / microVM idle-out, Global Bedrock 5xx inside OpenCode rollouts (`summary.errored` with a still-`succeeded` job), CodeBuild image pull for the worker image, job > 30 min; TEST: polling the page instead of the job, HR task-set fixtures with `diff.changed false` (take the seed fallback and mark `deviation`); PRODUCT: `failed` with `process exited N` on a healthy worker, publish changing the wrong record or not bumping the version, `interrupted` jobs that cannot be resumed.

## 7. Open rulings
- Rulings are recorded inline in the rows marked "ruling": NC-E15-13, NC-E15-15.
