# E09 Data Center 「数据中心」 (section `eval-data`, `/v2/eval/data`) — acceptance standard

**Risk score:** 9.5 (VIDEO-SERIES-PLAN card: 3 / 3 / 3 / 0.5; R2 CloudWatch + S3 upstream, R9 shared edit rights) → negative cases ≥ 6 · **Recorded build:** to fill · **Status:** draft

## 1. Scope and roles
- **Routes (`route_policy.py` 318–341):** `GET/POST /api/eval/datasets`, `POST …/datasets/upload`, `POST …/datasets/from-sessions`, `PUT/DELETE …/datasets/{id}`, `POST …/{id}/sync-to-aws`, `POST …/{id}/publish-version`, `GET …/datasets/cloud`, `GET/DELETE …/cloud/{cloud_id}`, `POST …/cloud/{cloud_id}/publish-version`, `DELETE …/cloud/{cloud_id}/versions/{v}`, `GET/POST /api/eval/pipelines`, `GET/PUT/DELETE …/pipelines/{id}`, `POST …/pipelines/{id}/run`, `GET /api/eval/agents/{id}/log-streams`, `GET /api/eval/log-sessions|log-groups|log-services` — all `MEMBER`.
- **Roles:** `demo-admin` builds `hr-helpdesk-golden` from `hr-helpdesk-wb006` sessions and uploads `hr-helpdesk-redteam.jsonl`; `hr-intern` reads the same rows. `created_by` is stamped on datasets and pipelines (`users-and-ownership.md`); **no creator-or-admin rule on PUT/DELETE** — in `shared` mode any member edits any dataset (`test_visibility.py::test_datasets_shared_by_default`). Isolation (`isolated` mode) is E19.
- **Out of scope:** starting runs on the dataset (E11), evaluators (E10), SCORE NOW (E12), the `legacy`/`simulated` persona kinds (not in the HR storyline).

## 2. Capabilities and pass criteria (positive)
| id | action (UI / API) | observable pass criteria | evidence |
|---|---|---|---|
| AC-E09-01 | Traces tab: pick wb006 sessions | `GET /api/eval/agents/<wb006>/log-streams?hours=24` 200, `streams[].session_id` present, `kind ∈ session\|shared\|otel_session`; UI lists the E04 sessions | response.json, shot |
| AC-E09-02 | FROM SESSIONS (console) | `POST …/from-sessions?sync=false` 202 `{job_id, status queued, poll}`; `GET /api/jobs/{id}` → `succeeded`, `payload.result {dataset{id,name,kind predefined,item_count}, added ≥ 8, skipped[]}`; `?sync=true` with ≤ 10 sessions → 201 `{dataset, added, skipped, timing}` (s07b path) | job.json / response.json |
| AC-E09-03 | Pipeline → `hr-helpdesk-golden` | `POST /api/eval/pipelines` 201 `status idle`; `POST …/run` 200 `status succeeded`, `last_run {scanned, matched, added ≥ 1, dataset_id}`; `config.output` rewritten from `dataset_name` to `{dataset_id}`; a second run appends once (dedupe) | pipeline.json before/after |
| AC-E09-04 | Rows: write assertions / expected answers | `PUT /api/eval/datasets/{id}` 200, `has_ground_truth true`, `kind` unchanged, `item_count` unchanged; UI row editor saves without full reload | response.json, shot |
| AC-E09-05 | Upload `hr-helpdesk-redteam.jsonl` (6 third-party-salary prompts) | `POST …/datasets/upload {name, locale zh-CN, jsonl}` 201, `item_count 6`, `kind predefined`, `created_by` = uploader; blank lines skipped | response.json |
| AC-E09-06 | SYNC to S3 | `POST …/sync-to-aws` 200; `cloud {dataset_id cds_<12hex>, status ACTIVE, draft_status MODIFIED, versions [], arn arn:aws-cn:s3:::<bucket>/eval-datasets/<cds>}`; read-only AWS: `draft.json` + `meta.json` under that prefix | response.json, aws-readonly.json |
| AC-E09-07 | PUBLISH VERSION | `POST …/publish-version` 200 → `versions [{version "1", example_count == item_count}]`, `draft_status UNMODIFIED`; `GET …/datasets/cloud` row `exampleCount == items`; `GET …/cloud/{id}.versions == ["1"]`; a second publish → `"2"`, `v1.json` unchanged (immutable) | response.json, cloud.json |
| AC-E09-08 | Member view | `hr-intern` `GET /api/eval/datasets` 200 lists both HR datasets with `created_by_name demo-admin`; the UI shows the Creator column | two-identity response, shot |

## 3. Negative / boundary cases (≥ 6)
| id | input | expected | source | risk |
|---|---|---|---|---|
| NC-E09-01 | `POST /datasets` with `items: []` or 201 items; `name` 65 chars; `description` 1001 chars | `422 validation.invalid_request` (pydantic `items 1..200`, `name ≤ 64`, `description ≤ 1000`) | `routers.py` `DatasetCreate` 34–48 | — |
| NC-E09-02 | plain item without `prompt`; scenario item without `scenario_id` / duplicate `scenario_id` | `422 dataset.invalid_item`, message names the item index | `routers.py` `_validate_items` 68–115 | — |
| NC-E09-03 | upload with one malformed JSON line / only blank lines / a non-object line | `422 dataset.invalid_jsonl` / `422 dataset.empty` / `422 dataset.invalid_item` | `routers.py` 306–316 | — |
| NC-E09-04 | `PUT` replacing predefined items with scenario items | `400 dataset.kind_immutable` | `routers.py` 352 | — |
| NC-E09-05 | `publish-version` before any sync | `409 dataset.not_synced` | `routers.py` 468 | — |
| NC-E09-06 | `DELETE …/cloud/{id}/versions/1` while a run pins version 1 | `409 dataset.version_pinned {runs: n}`; unknown version → `404 dataset.version_not_found` | `routers.py` 651, `datasets_local.py` 215 | — |
| NC-E09-07 | `from-sessions?sync=true` with 11 sessions; target dataset of kind `simulated` | `400 from_sessions.sync_too_large {sessions, max 10}`; `400 dataset.kind_unsupported` | `pipeline_routers.py` 574, 217 | — |
| NC-E09-08 | `from-sessions` into a NEW dataset where every session has no exchange | `422 dataset.nothing_extracted {skipped[]}`; appending to an existing dataset with 0 added is still 201 | `pipeline_routers.py` 251 | — |
| NC-E09-09 | `PUT` or `run` a pipeline whose `status == running` | `409 pipeline.running` | `pipeline_routers.py` 730, 763 | — |
| NC-E09-10 | **Fault injection:** workspace without `artifacts_bucket`; S3 `PutObject` 5xx during sync / publish | `503 dataset.store_unavailable`; `502 dataset.sync_failed` / `502 dataset.publish_failed` with `cloud.status` persisted as `CREATE_FAILED` (first sync) or `UPDATE_FAILED`; retry after recovery succeeds | `datasets_local.py` 231; `routers.py` 432–483 | — |
| NC-E09-11 | `DELETE /datasets/{id}` for a dataset referenced by a run or a pipeline output; `DELETE …/cloud/{id}` while a run pins one of its versions | **Today: 200, no guard** (run keeps `dataset_name` text; next pipeline run `failed` with `last_run.error "dataset.not_found: …"`). **Open ruling (PRODUCT candidate):** expected `409 dataset.in_use` | `pipeline_routers.py` 806–808 | — |
| NC-E09-12 | log-streams for a deleted / inactive agent | `400 agent.not_active` | `online_routers.py` 106 | — |
| NC-E09-13 | **Fault injection:** `systemctl restart launchpad-backend` during a from-sessions job | job `failed`, `payload.error.code from_sessions.interrupted`; dataset partial rows are NOT created; re-submit works | `pipeline_routers.py` 495–520 (startup sweep) | — |

## 4. Evidence
Per TEMPLATE §4 under `s3://launchpad-cn-evidence-…/<round>/E09/<case-id>/`: `request.json`, `response.json`, `shot.png`; AC-06/07 add `aws-readonly.json` (`aws s3 ls s3://<bucket>/eval-datasets/<cds>/`); AC-02 adds `job.json`; `ledger.redacted.json` for `eval_datasets` / `eval_pipelines` rows (creator columns). Session transcripts in evidence must not include the fixture employee data beyond the prompt text.

## 5. Automation mapping and GAPs
| case | Existing automation | Status |
|---|---|---|
| AC-01, NC-12 | `backend/tests/evaluation/test_log_streams.py::test_route_resolves_the_agent_log_group`, `::test_discovery_routes` | covered (UI Traces tab **GAP** — no e2e) |
| AC-02 | `evaluation/test_pipelines.py::test_from_sessions_creates_predefined_dataset`; `evaluation/test_from_sessions_job.py::test_more_than_the_cap_runs_as_a_job_with_progress_and_result`, `::test_sync_false_forces_the_job_for_a_small_input`; e2e `scenario/s07-evaluate.spec.ts::s07b` (sync path) | covered (202 job path in e2e **GAP**) |
| AC-03, NC-09 | `test_pipelines.py::test_pipeline_crud_and_runs_append_once`, `::test_pipeline_run_failure_is_recorded` | partial: `409 pipeline.running` **GAP** |
| AC-04/05, NC-02/03/04 | `evaluation/test_datasets_crud.py::test_jsonl_upload_en_and_zh_samples`, `::test_jsonl_upload_rejects_bad_lines`, `::test_dataset_rejects_items_without_prompt`; `test_datasets_v2.py::test_put_edits_name_description_items`, `::test_put_kind_immutable`, `::test_scenario_item_validation` | covered |
| AC-06/07, NC-05/06/10 | `evaluation/test_datasets_draft_sync.py::*` (first sync, resync in place, publish snapshots, immutable versions, never-reuse number, 502 recorded, 503 without bucket, 409 not_synced, 409 version_pinned); e2e `42-dataset.spec.ts::case 25`, `s07b` | covered (live S3 fault **GAP** → Q4a `FakeS3` is hermetic only) |
| AC-08 | `test_visibility.py::test_datasets_shared_by_default`; `test_resource_creators.py::test_a_member_dataset_records_the_member`; e2e `12-users` | covered |
| NC-01 | none (no test for pydantic limits on datasets) | **GAP** → Q6a schemathesis on `DatasetCreate`/`DatasetUpload` |
| NC-07/08 | `test_pipelines.py::test_from_sessions_rejects_simulated_and_empty`; `test_from_sessions_job.py::test_sync_true_above_the_cap_is_refused_and_the_default_small_input_stays_201` | covered |
| NC-11 | none (no guard, no test) | **GAP** + open ruling |
| NC-13 | `test_from_sessions_job.py::test_restart_sweep_fails_an_interrupted_job_instead_of_rerunning_it` | covered hermetic; process-level drill **GAP** → Q4a |

## 6. Failure classification
ENV: CloudWatch Logs Insights throttling / `from-sessions` latency > 30 s with a large trace window (N-SCN-obs10 — the result is still correct, use the 202 job path), S3 5xx on sync/publish (re-sync once before classifying); TEST: session picking before E04 sessions are indexed (`skipped[].reason no_transcript`), `cloud.status` casing; everything else PRODUCT (register in `bugs-round-N.md`).

## 7. Open rulings
- Rulings are recorded inline in the rows marked "ruling": NC-E09-11.
