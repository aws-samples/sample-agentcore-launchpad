# E07 Governance 「治理」 (section `governance`, `/v2/governance`, policies at `?tab=policies`) — acceptance standard

**Risk score:** 14.5 (card: 4 / 6 / 4 / 0.5; R9 governance posture, R7 member-grantable publish, R2 redeploy upstream) → negatives ≥ 10 · **Recorded build:** to fill · **Status:** draft

## 1. Scope and roles
- **Routes (`route_policy.py`):** gateways — `GET /api/governance/gateways[/{id}]`, `POST/DELETE …/{id}/manage`, `GET/POST …/{id}/rate-limits`, `PUT/DELETE …/rate-limits/{rid}`, `POST …/targets/{tid}/synchronize` = `MEMBER`; tools — `GET /api/tools`, `POST /api/tools/call`, `/api/demos/*` = `MEMBER`; policies (mounted only with the `policy` feature) — `GET /api/policies[/{id}]`, `POST …/validate|format|simulate`, `GET …/audit|changes|settings|entities` = `MEMBER`; `POST /api/policies`, `PUT …/{id}`, `POST …/{id}/publish|retire`, `DELETE …/{id}`, `PUT …/settings`, `POST …/apply` = `perm:policy.manage`; `mode: enforce` and `simulate {record: true}` add checks **in the handler** (admin; permission).
- **Roles:** `demo-admin` authors, publishes, switches to `enforce`, applies, restores; `hr-intern` is the denied principal (Cedar `Launchpad::User`, role member) and must not be able to switch to enforce. Policies are workspace-shared (not creator-bound). Test ids: `v2-policy-*`, `policy-deny-card`, `rate-limit-*`, `gateway-target-kind-*`, `enforce-blockers`.
- **Scenario fixtures:** `hr_permit_declared_tools`, `hr_forbid_member_get_employee`, `hr_permit_members_invoke` (log_only → enforce → retire/delete). Mutations on a Gateway require the Launchpad managed tag.
- **Out of scope:** Gateway creation / inbound auth (bootstrap), Registry import of a gateway (E16), runtime guard (P2-a, default off — narrate only).

## 2. Capabilities and pass criteria (positive, s11 cases 32–37)
| id | action | observable pass criteria | evidence |
|---|---|---|---|
| AC-E07-01 | Gateways · targets · tools | `launchpad-gw` listed (inbound AWS_IAM, managed tag); detail shows `hr-database` target with kind and `lastSynchronizedAt`; `GET /api/tools` lists `hr-database___get_employee`; TRY IT `POST /api/tools/call` returns the employee row | gateways.json, tools.json |
| AC-E07-02 | Rate limit | `POST …/rate-limits` 201 (`requests` per `minute`, keys `["toolName"]`, 1 entry); `PUT` replaces entries; `DELETE` 200; audit rows `rate_limit.*` | rate-limits.json |
| AC-E07-03 | Editor: three `hr_*` policies | `POST /api/policies` 201 each (`status draft`, `draft_version 1`); `POST …/validate` `{ok: true, errors: []}`; `POST …/format` round-trips; `v2-policy-validation` shows ok | policies.json |
| AC-E07-04 | Simulator (drafts) | member `invoke` → `ALLOW`; member `call_tool hr-database___get_employee` → `DENY` naming `hr_forbid_member_get_employee` in `determining_policy_ids`; admin same tool → `ALLOW`; `record: false` writes no audit row | simulate.json ×3 |
| AC-E07-05 | Publish in `log_only` (apply off) | 3 `active`; `GET /api/overview` `services.policy true`, detail `log_only · 3 active`; `hr-intern` chat turn allowed; `GET …/audit` row `decision allow`, `runtime_channel.status` reported | settings.json, audit.json |
| AC-E07-06 | Switch to `enforce` | admin `PUT …/settings {mode: enforce, confirmation_name: <workspace id>}` 200; `hr-intern` tool turn → `policy-deny-card` with `decision_id`, API `403 policy.denied`; the admin's identical turn shows no card; audit `deny` row with that id | deny.json, shot.png |
| AC-E07-07 | Apply the narrowing | after publish the HR agent reads `policy_stale true`; `POST …/apply` 202 one job; redeploy ≤ 10 min → `policy_stale false`, `coverage` valid; admin tool call still answers | apply.json, agent.json |
| AC-E07-08 | Restore | `PUT …/settings {mode: log_only}` → `POST …/retire` ×3 → `DELETE` ×3 → `active_policies 0`, `services.policy false`; audit rows remain | settings.json, audit.json |

## 3. Negative / boundary cases (≥ 10)
| id | input | expected | source | risk |
|---|---|---|---|---|
| NC-E07-01 | `hr-intern` `PUT …/settings {mode: enforce}` | `403 auth.forbidden` "Switching a workspace to enforce requires an administrator account" | `services/policy_store.py::set_settings`; `test_policies_api::test_enforce_is_admin_only_and_record_needs_the_permission`; e2e `s11d` | R9 |
| NC-E07-02 | admin enforce with 0 ACTIVE; with ACTIVE but no `would_deny` evidence and no / wrong confirmation | `409 policy.no_active_policies`; `422 policy.confirmation_required {evidence, expected_confirmation: <workspace **id**>}` (D20-obs3: the id, not the name) | `policy_store.py`; `test_policies_api::test_enforce_confirmation_is_the_workspace_id_not_its_name` | R10 (self-lockout) |
| NC-E07-03 | statement that does not parse; statement > `MAX_STATEMENT_CHARS` | `422 policy.invalid_statement` with the validation report (line/col) | `test_policy_store::test_invalid_statement_is_422_with_the_report` | input hygiene |
| NC-E07-04 | name `1abc`; duplicate name | `400 policy.invalid_name` (`^[A-Za-z][A-Za-z0-9_]{0,47}$`); `409 policy.name_taken` | `policy_store.py` | — |
| NC-E07-05 | `scope agents` with an unknown agent id; `agent_ids` on a workspace-scoped policy | `422 policy.unknown_agent {agent_ids}`; `422 policy.scope_mismatch` | `test_policy_store::test_agent_scope_validates_the_agent_ids` | R9 |
| NC-E07-06 | publish with no draft; retire a draft; delete an ACTIVE; retire the last ACTIVE while in enforce | `409 policy.no_draft`; `409 policy.not_active`; `409 policy.active`; `409 policy.no_active_policies` | `test_policy_store::test_publish_guards`, `::test_retire_and_delete_guards` | R10 |
| NC-E07-07 | unknown policy id / version pin | `404 policy.not_found`; `404 policy.version_not_found` | `policy_store.py` | — |
| NC-E07-08 | simulate `agent` resource with `call_tool`; `record: true` by a member whose `policy.manage` is revoked; `GET …/audit?since=garbage` | `422 policy.invalid_simulation`; `403 auth.permission_required`; `422 policy.invalid_since` | `routers/policies.py` | R9 |
| NC-E07-09 | **fault injection**: an ACTIVE version that no longer parses (engine `NO_DECISION`) under `enforce`; under `log_only` | `503 policy.undecidable` (fail closed); recorded `error` and let through (`policy_invoke_fail_mode` default) | `test_policy_invoke_gate::test_undecidable_in_enforce_is_503_policy_undecidable`, `::test_undecidable_in_log_only_is_recorded_and_let_through_by_default` | R2 |
| NC-E07-10 | publish when the workspace has no `artifacts_bucket`; S3 copy fails | `409 workspace.not_ready`; `502 policy.s3_copy_failed` (fail closed, nothing published) | `test_policy_store::test_publish_fails_closed_when_s3_fails` | R2 |
| NC-E07-11 | rate limit with 11 keys / unknown key / duplicate key / `rate 10000001` / `tokens` per `second`; any mutation on an unmanaged gateway; AWS `ConflictException` | `422 governance.rate_limit_invalid {reason: dimension_keys_count\|dimension_key_unknown\|dimension_key_duplicate\|rate_out_of_range\|period_not_allowed}` before any AWS call; `409 governance.gateway_not_managed`; `409 aws.conflict` | `services/governance.py`; `test_governance_rate_limits.py` (14) | R2, input hygiene |
| NC-E07-12 | SYNC on a static OpenAPI target; on a non-MCP gateway; unknown gateway id | `409 governance.target_not_synchronizable {reason: static_tool_schema\|not_mcp_server, status}`; `409 governance.gateway_unsupported`; `404 governance.gateway_not_found` | `test_governance_target_sync.py` (13) | — |
| NC-E07-13 | `hr-intern` with `perm:policy.manage` revoked `POST /api/policies` | `403`; editor actions disabled with the missing-permission explanation (console rule "disabled primary actions explain what is missing") | `test_route_policy::test_fully_revoked_member_gets_403` | R9 |
| NC-E07-14 | `policy` feature off / engine not registered | every `/api/policies*` → `503 feature.unavailable {feature: "policy"}`; Home `policy` row waits for capabilities | `test_policies_api::test_routes_answer_503_before_an_engine_is_registered` | — |
| NC-E07-15 | `PUT …/settings` mode change alone | `policy_stale` flips `true` on the gateway agents without a policy edit (D20-obs5, Low — report as-is, narrate as expected noise) | `bugs-round-5.md` D20-obs5; e2e `s11f` soft check | expectation setting |
| NC-E07-16 | publish dialog: typed policy name | client-side check only; the server ignores `PolicyPublish.confirmation_name` (D20-obs4) — not a safety control; do not present it as one | `bugs-round-5.md` D20-obs4 | expectation setting |

## 4. Evidence
Per TEMPLATE §4. Mandatory here: `settings.json` before/after every mode change, `audit.json` filtered by `principal=hr-intern`, the `403 policy.denied` body with `decision_id`, the apply job log, `ledger.redacted.json` of `policies` / `policy_versions` / `policy_decisions`. The decisions log group is named only by its pattern (`/launchpad/policy/decisions[-env]`).

## 5. Automation mapping and GAPs
| case | existing automation | status |
|---|---|---|
| AC-01/02 | `tests/e2e/cn/05-gateway.spec.ts::case 5`, `19-gateway-admin::case 19`, `scenario/s02-tools::s02`, `15-builtin-tools::case 15`; `test_governance_gateways.py`, `test_governance_rate_limits.py`, `test_governance_target_sync.py` | covered |
| AC-03…08, NC-01/02 | `scenario/s11-govern.spec.ts::s11a…s11f` (cases 32–37), `test_policies_api.py` (14), `test_policy_store.py` (22), `test_policy_deploy_narrowing.py` (27) | covered |
| AC-06 deny card | `s11d`, `test_policy_invoke_gate.py` (26), `frontend …/chat/PolicyDenyCard.test.tsx` | covered |
| NC-03…08 | `test_policy_store.py`, `test_policies_contract.py`, `routers/policies.py` | covered (unit); HTTP-level matrix **GAP** → Q6a schemathesis |
| NC-09/10 | `test_policy_invoke_gate`, `test_policy_store::test_publish_fails_closed_when_s3_fails` | covered (unit); live injection **GAP** → Q4a |
| NC-11/12 | `test_governance_rate_limits::test_create_validation_answers_422_before_any_aws_call`, `test_governance_target_sync::test_static_targets_are_not_synchronizable` | covered (unit); UI blockers (`rate-limit-blockers`) **GAP** |
| NC-13/14 | `test_route_policy`, `test_policies_api::test_routes_answer_503_before_an_engine_is_registered` | covered |

## 6. Failure classification
`ENV`: apply redeploy > 10 min (CodeBuild / Runtime, N10v3-obs3 baseline ≈ 4.5 min), `runtime_channel.status missing` on a fresh environment (expected until the sink ships), Gateway `ConflictException` while busy. `TEST`: `e2es_` fixture names, 120 s deny-card wait. Else `PRODUCT`; D20-obs4/obs5 are already registered.

## 7. Open rulings
- `perm:policy.manage` is default-granted: a member can author and publish policies (only `enforce` is admin). Keep (R7/R9) or make policy writes admin-only — lead decision before recording the member chapter.
- Runtime guard (`runtime_guard`) is off by default and has no scenario step; narrate as a setting, do not demonstrate.
