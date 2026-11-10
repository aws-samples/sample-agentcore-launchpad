# E02 Architect assistant 「架构助手」 (section `assistant`, `/v2/assistant`) — acceptance standard

**Risk score:** 12 (R2 upstream failures + R6 long-turn interruption + R7 billing) → negatives ≥ 10 · **Recorded build:** to fill · **Status:** draft

Chinese working copy: `plans/acceptance/E02-assistant.md` (outside the repo) — ported 2026-10-18 after qa-planner's amendments; case ids, codes and tests identical, test paths repo-relative.

## 1. Scope and roles
- **Routes (`backend/app/core/route_policy.py`):** `GET /api/assistant/architect`, `GET/POST …/conversations`, `GET …/conversations/{id}`, `POST …/{id}/turns`, `GET …/{id}/turns/{turn}/stream`, `PUT …/{id}/proposal`, `POST …/proposal/reject` = `MEMBER`; `POST …/proposal/approve`, `…/preparation/skills`, `…/evaluation-plan/materialize|retry|assets` = `perm:agents.deploy`; `PUT …/{id}/sharing` = `MEMBER` (handler: owner or admin).
- **Roles:** `demo-admin` opens the conversation and approves the deployment of `hr-policy-reader`; `hr-intern` is the shared read-only collaborator; an admin has read-only access to every conversation (N-ARCH-obs17). Conversations are owner-bound; the approver becomes the new agent's `created_by`.
- **Out of scope:** preset install / repair (E03 / `29-preset-uninstall`); SiliconFlow models (not configured).

## 2. Capabilities and pass criteria (positive)
| id | action | observable pass criteria | evidence |
|---|---|---|---|
| AC-E02-01 | `GET /api/assistant/architect` | 200, `available true`, `installed_release` = 1.7.3, `can_deploy true` (admin) | response.json |
| AC-E02-02 | New Conversation + HR policy reader brief (KB `hr-policy-kb`, no tools, refuses other people's salary) | `POST …/conversations` 201; a `launchpad-proposal` within ≤ 4 turns, `validation_errors []` (N-ARCH-obs16 / 1.7.x) | `events.jsonl` per turn |
| AC-E02-03 | Refresh the page mid-generation | after reload the UI shows "generating in the background…" and reconnects automatically; `GET …/turns/{n}/stream?from=<seq>` resumes, `seq` strictly monotonic with no gaps, terminal event `done` exactly once (contract `assistant-turns.md` §2–3) | events before and after the reconnect |
| AC-E02-04 | Proposal review | `model_id = global.zai.glm-5.3`; KB binding `hr-policy-kb`; the evaluation plan contains `hr-guardrails-judge` (`blocking true`, threshold 1.0, judge `global.moonshotai.kimi-k3`) and `Builtin.Helpfulness` 0.7; no tools | proposal.json |
| AC-E02-05 | Approve and deploy | `POST …/proposal/approve` 202 → job with five stages `succeeded`; agent `hr-policy-reader` `active` ≤ 15 min; `created_by` = approver id; approving again → `assistant.proposal_already_approved` | job.json, agent.json |
| AC-E02-06 | Materialize evaluation assets | `POST …/evaluation-plan/materialize` 202 → operation `completed`; every result row carries a V2 deep link (#283); dataset / evaluator `created_by` = approver | operation.json |
| AC-E02-07 | Share with `hr-intern` | owner `PUT …/sharing` 200; member `GET …/conversations/{id}` 200 and no `POST turns` entry (read-only) | responses for both identities |
| AC-E02-08 | Revisit the existing `octank-hr-self-service` | list and detail readable; the UI copy explains its relation to wb006 as an independent case | shot.png |

## 3. Negative / boundary cases (≥ 10)
| id | input | expected | source | risk |
|---|---|---|---|---|
| NC-E02-01 | `POST …/turns` while a turn is running | `409 assistant.turn_in_progress` | `assistant-turns.md` §2 | — |
| NC-E02-02a | prompt > 100 000 characters | `422 validation.invalid_request` (`TurnRequest` schema `max_length`, before the service) | r13-d hermetic | — |
| NC-E02-02b | prompt ≤ 100 000 characters but > 300 000 bytes (multibyte) / over the preamble budget | `413 assistant.prompt_too_large`, message names the limit | `assistant/service.py` `MAX_PROMPT_BYTES`; r13-d | — |
| NC-E02-03 | brief names `anthropic.claude-sonnet-4-5` | proposal `invalid`, message lists the selectable `global.zai.glm-5.3 / global.moonshotai.kimi-k3`, no `siliconflow.*`, no raw pydantic text | N-ARCH-obs2/3/9 → `test_assistant_validation_errors` | — |
| NC-E02-04 | brief names the unconfigured `siliconflow.deepseek-ai/DeepSeek-V4-Pro` | refused, naming the SSM parameter `launchpad-siliconflow-api-key` | N-ARCH-bug3 | — |
| NC-E02-05 | the 201st turn | `409 assistant.conversation_full` (`max_turns 200`) | `backend/app/assistant/service.py:1301` | — |
| NC-E02-06 | member opens another user's private conversation `GET …/conversations/{id}` | `404 assistant.conversation_not_found` (**never 403**) | e2e `11b::arch-4c` | — |
| NC-E02-07 | non-owner member `PUT …/sharing` | `403 assistant.share_forbidden` | `route_policy.py` comment | — |
| NC-E02-08 | approve with an old revision after the proposal was edited | `409 assistant.proposal_stale` | `backend/app/assistant/service.py` | — |
| NC-E02-09 | member with `perm:agents.deploy` revoked approves | `403`; the UI approve button is disabled with the missing permission explained | `test_route_policy::test_fully_revoked_member_gets_403` | — |
| NC-E02-10 | judge `description` of 201 characters | a single-field, readable error; does not make the whole proposal INVALID for more than one round | N-ARCH-obs14 | — |
| NC-E02-11 | **Fault injection**: `systemctl restart launchpad-backend` mid-turn | the startup sweep writes a `turn_interrupted` row + terminal event `error {"message":"interrupted"}`; a reconnecting client sees the end; proposal blocks already streamed are kept | contract §2 "Startup sweep"; Q4a `restart_mid_turn.sh` | — |
| NC-E02-12 | **Fault injection**: Harness upstream 503 ×2 then recovery | the event stream shows `retry`, ends with `done`; past the retry budget → an `error` row with the real cause, not `harness.incomplete_response` | N-ARCH-obs18; Q4a `FaultyBedrock` | — |
| NC-E02-13 | workspace `bootstrap_status != ready` | opening a conversation and `POST turns` are **not refused** (200/201); `GET /api/assistant/architect` `deploy_requirements` lists what is missing; only `proposal/approve` (with the deploy claim) refuses `assistant.workspace_not_ready`. **Gap (lead ruling pending):** whether opening a conversation should also be refused | r13-d hermetic | — |

## 4. Evidence
Per TEMPLATE §4; this module must attach: `events.jsonl` per turn (with seq), `proposal.json`, the deploy job log, `cw/<runtime session>.jsonl` (first Chat turn after approve). Keys and SSM values never appear.

## 5. Automation mapping and GAPs
| case | Existing automation | Status |
|---|---|---|
| AC-01/02/05 | `tests/e2e/cn/11-assistant.spec.ts::case 11`, `tests/e2e/cn/11a-assistant-helpdesk.spec.ts::arch-2/3` | covered (HR brief needs fixtures) |
| AC-03, NC-01/05/11 | `backend/tests/test_assistant_async_turns.py::test_reattach_from_seq_replays_*`, `::test_second_post_while_a_detached_turn_runs_is_refused`, `::test_startup_sweep_finalizes_a_dead_turn_from_its_event_log` | covered (process-level drill **GAP** → Q4a) |
| AC-07, NC-06/07 | `tests/e2e/cn/11b-assistant-edges.spec.ts::arch-4c` (404); `backend/tests/test_assistant_async_turns.py::test_read_only_admin_viewer_*` | partial: owner sharing + member read-only UI **GAP** |
| NC-03/04/10 | `backend/tests/test_assistant_validation_errors.py::*` (11 tests) | covered |
| NC-02a/02b/13 | r13-d hermetic tests (`cn/r13-d`) | covered |
| NC-08 | none | **GAP** → Q6a schemathesis + new `test_assistant_limits.py` |
| NC-12 | none | **GAP** → Q4a `FaultyBedrock` |

## 6. Failure classification
ENV: Bedrock cross-partition 5xx / throttle (counts only when the N-SCN-obs18 single retry still fails), a turn exceeding the 15 min timeout (obs13 latency); TEST: `aside[aria-label]` selectors, the seq assertion rule; everything else PRODUCT.

## 7. Open rulings
- Rulings are recorded inline in the rows marked "ruling": NC-E02-13.
