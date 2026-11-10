# E03 Agent management 「Agent 管理」 (section `agents`, `/v2/agents`) — acceptance standard

**Risk score:** 12 (R2 CodeBuild / Runtime upstream + R7 billable deployments + R9 creator permissions) → negatives ≥ 10 · **Recorded build:** to fill · **Status:** draft

Chinese working copy: `plans/acceptance/E03-agents.md` (outside the repo) — ported 2026-10-18; case ids, codes and tests identical, test paths repo-relative.

## 1. Scope and roles
- **Routes:** `GET /api/agents`, `GET …/{id}`, `…/identity`, `…/versions`, `GET /api/jobs/{id}`, `GET …/uploads/{id}`, `POST …/{id}/invoke` = `MEMBER`; `POST /api/agents`, `POST …/uploads`, `POST …/{id}/redeploy` = `perm:agents.deploy`; `DELETE …/{id}` = `perm:agents.delete`; `POST …/discovery/import` = `perm:agents.import`; presets `POST/DELETE /api/system-agents/*` = `ADMIN`.
- **Roles:** `demo-admin` creates / corrects / deletes; `hr-intern` is read-only plus redeploy / delete on agents they created; redeploy / delete are creator-or-admin (`require_agent_creator`), a NULL creator → admin-only (CN deviation, `users-and-ownership.md` §4).
- **Out of scope:** experiments / canary (E13), A2A (`16-a2a`), `isolated` visibility (E19).

## 2. Capabilities and pass criteria (positive)
| id | action | observable pass criteria | evidence |
|---|---|---|---|
| AC-E03-01 | List and wb006 detail | 200; Creator column, `policy_coverage`, `versions` non-empty, five-stage log readable; `created_by_name` shown | response.json, shot |
| AC-E03-02 | Edit wb006 → redeploy | `POST …/redeploy` 202 → job `succeeded`; `version` +1; `status` stays `active` throughout; only prompt / model changed | job.json, before/after agent.json |
| AC-E03-03 | Method C Studio `hr-faq-studio` | `POST /api/agents` 202 → five stages → `active` ≤ 15 min; Chat returns model text | job.json |
| AC-E03-04 | BYOC zip `hr-notice-byoc` | `POST …/uploads` 201 → `POST /api/agents` 202 → `active` ≤ 15 min; runtime log has no `EndpointConnectionError` | upload.json, cw/ |
| AC-E03-05 | Delete `hr-faq-studio` | `DELETE` 200, `removed` ends with `log_groups`, `failed []`; read-only AWS: runtime / role / log group gone (≤ 80 s backfill then delete again, N-SCN-obs8). `removed` names only resources that existed (N16-obs76): deleting an agent whose deploy failed before provision reports `aws_resource_deleted: false`, `runtime` + `execution_role` under `skipped` with the reason in `steps[].detail`. The V2 detail's Refresh after a delete shows the not-found notice, never the stale ACTIVE view | response.json, aws-readonly.json |
| AC-E03-06 | Member view | `hr-intern` lists everything (shared), no redeploy / delete buttons on the admin's agents | shot |
| AC-E03-07 | Failed redeploy | redeploy of a bad package → agent still `active`, `last_deploy_failed true` + `last_deploy_error`, amber tag `v2-agent-redeploy-failed-<name>` | N10v3-bug7 |

## 3. Negative / boundary cases (≥ 10)
| id | input | expected | source | risk |
|---|---|---|---|---|
| NC-E03-01 | `POST /api/agents` with an existing name | `409 agent.name_exists` | `backend/app/routers/agents.py` | — |
| NC-E03-02 | Reserved name (preset / `eval-worker` family: name, base, `<base>-<suffix>`) | `422 agent.name_reserved {name, reserved_for}` | `test_deploy_cancel::test_reserved_preset_name_families_are_422_before_any_row` | — |
| NC-E03-03 | `method` not in the CN build | `422 agent.method_not_available` | `backend/app/routers/agents.py` | — |
| NC-E03-04 | Redeploy while deploying | `409 agent.deploy_in_progress` | `test_agents_api::test_redeploy_conflicts_while_deploying` | — |
| NC-E03-04b | Delete while a deploy job is `queued` / `running` (N16-obs83) | `409 agent.deploy_in_progress {job_id, stage, job_status}`; nothing torn down | `test_deploy_cancel::test_delete_during_a_deploy_stage_is_409_and_touches_nothing` | — |
| NC-E03-05 | Redeploy changing `name` / `method` | `400 agent.redeploy_immutable` | `test_agents_api::test_redeploy_rejects_name_or_method_change` | — |
| NC-E03-06 | `hr-intern` DELETE / redeploy the admin's agent | `403 agent.forbidden {agent_id, created_by, created_by_name}` | `test_agent_ownership::test_member_cannot_delete_the_admins_agent` | — |
| NC-E03-07 | Redeploy / delete a preset (`system_key`) | `refuse_system_mutation` 4xx, UI button disabled with explanation | `route_policy.py` comment | — |
| NC-E03-08 | Request body carrying `created_by` / `created_by_name` | `422 validation.invalid_request` — the `AgentSpecRequest` model refuses the key; `detail[].msg` carries the creator sentence (`created_by: the creator is recorded by the server and cannot be set by the client`); no row created. `validation.creator_not_settable` is the dict-body routers' code (`creators.reject_client_creator`), not this route's (N17-obs116) | `test_agent_ownership::test_the_creator_is_never_client_settable`; api.md "`POST /api/agents` … `AgentSpecRequest`" | — |
| NC-E03-09 | BYOC non-zip / empty file / over the cap | `400 byoc.invalid_upload` ×2; `413 byoc.upload_too_large` | `backend/app/routers/agents.py` | — |
| NC-E03-10 | BYOC zip-slip / symlink / zip bomb | refused, per `test_byoc` | `test_byoc::test_validate_rejects_*` | — |
| NC-E03-11 | Deploy with an invalid `model_id` | job `failed` + readable UI error, no half-built runtime left | e2e `18-error-paths::case 18a` | — |
| NC-E03-12 | `GET/POST /api/chat/{id}` on a deleted agent | `404 agent.not_found` + UI notice (no blank screen) | e2e `18::case 18b` | — |
| NC-E03-13 | **Fault injection**: `CreateAgentRuntime` returns `ThrottlingException` ×3 | the stage retries and succeeds; or the job `failed` with the reason, the agent never stuck in `deploying` | Q4a `FaultyAgentCoreClient` | — |
| NC-E03-14 | **Fault injection**: `DeleteLogGroup` times out during delete | `failed ["log_groups"]` with the reason; a retried delete succeeds (N-SCN-obs9 rule: the API must give the reason) | `test_agent_teardown::test_one_failing_step_does_not_stop_the_rest` | — |
| NC-E03-15 | `POST /api/agents` after `perm:agents.deploy` is revoked | `403`, the UI create entry disabled | `test_route_policy::test_fully_revoked_member_gets_403` | — |

## 4. Evidence
Per case: `response.json`, the five-stage job log, `shot.png`; delete cases add `aws-readonly.json` (`list-agent-runtimes`, `describe-log-groups`); BYOC adds the first 50 lines of the `cw/` runtime log.

## 5. Automation mapping and GAPs
| case | Existing automation | Status |
|---|---|---|
| AC-02/05 | `tests/e2e/cn/17-lifecycle.spec.ts::case 17`; `backend/tests/test_agents_api.py::test_redeploy_updates_in_place`, `backend/tests/test_agent_teardown.py::test_log_groups_are_deleted_last_*` | covered |
| AC-03/04 | `tests/e2e/cn/03-studio-create-deploy-chat.spec.ts::case 3`, `tests/e2e/cn/04-byoc-create-deploy-chat.spec.ts::case 4` | covered (HR naming needs fixtures) |
| AC-06, NC-06/15 | `tests/e2e/cn/12-users.spec.ts::case 12`; `backend/tests/test_agent_ownership.py` (11 tests); `backend/tests/test_route_policy.py` | covered |
| NC-01/03/04/05 | `backend/tests/test_agents_api.py::test_duplicate_name_conflict`, `::test_unsupported_method_rejected`, `::test_redeploy_*` | covered |
| NC-02/07/08 | `backend/tests/test_agent_ownership.py::test_the_creator_is_never_client_settable`; `backend/tests/test_deploy_cancel.py::test_reserved_preset_name_families_are_422_before_any_row` (NC-02, r16-h); `test_system_agents::test_reserved_name_is_refused…`; preset refusals | covered (N17-obs116 closed the NC-02 gap note; the sheet follows the code — 422, not 400 / 409) |
| NC-09/10 | `backend/tests/test_byoc.py` (9 zip validation tests) | covered; HTTP-level 413 **GAP** |
| NC-13/14 | `backend/tests/test_agent_teardown.py` in part | **GAP** → Q4a stubs |
| creation form × method domain (round 16, N15-obs16 class) | `backend/tests/test_creation_matrix.py` (79 pairwise + single-factor cells: create 2xx ⇒ generate dry-run clean, else 4xx with no row), `backend/tests/test_fail_early_guard.py` (moved conditions as data + AST reverse guard), `backend/tests/test_method_capabilities.py`, `frontend/src/lib/method-capabilities.test.ts` | covered (`docs/cn/interfaces/testing.md` §6) |

## 6. Failure classification
ENV: CodeBuild failing to pull images from outside China, `ThrottlingException` still failing after the retry, N-SCN-obs8 log-group backfill; TEST: `N10_REUSE` state-file drift; everything else PRODUCT.

## 7. Open rulings
- None recorded by the writer; add rows here when a reading of the code needs a lead decision.
