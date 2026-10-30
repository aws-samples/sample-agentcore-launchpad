# E08 Connections 「出站凭证」 + My authorizations 「我的授权」 (section `connections`, `/v2/connections`, `?view=mine`) — acceptance standard

**Risk score:** 9.5 (card: 3 / 3 / 3 / 0.5; DoD §4.2 lists E08 high-risk → R4 secret leakage, R9 vault mutations) → negatives ≥ 10 · **Recorded build:** to fill · **Status:** draft

## 1. Scope and roles
- **Routes (`route_policy.py`):** `GET /api/identity/connections`, `…/templates`, `…/oidc-sources`, `…/{kind}/{name}`, `GET /api/identity/gateway-targets`, `GET /api/identity/grants`, `…/grants/{connection}/status`, `GET /api/identity/consent-portal` = `MEMBER`; `POST …/connections/oauth2|api-key`, `DELETE …/connections/{kind}/{name}`, `POST/DELETE …/gateway-targets[/{id}]` = `perm:identity.manage`; `POST …/oauth/complete`, `DELETE …/grants/{connection}` = `perm:identity.grant`; `POST/DELETE …/consent-portal` = `ADMIN`.
- **Post r13-c (N13-obs5):** `/v2/my-connections` is deleted; the per-user 3LO grants list lives in the Connections page as the `?view=mine` tab (`connections/MyGrants.tsx`, test ids `my-grants`, `grant-status-*`, `grant-revoke-*`); `/auth/return` links there. Names match `^[a-zA-Z0-9\-_]{1,128}$`; `kind ∈ {oauth2, api_key}`.
- **List semantics (`services/identity_providers.py`):** every vault provider of both kinds, `source = system | launchpad | external` (`system` = this environment's gateway M2M and API-key providers; `launchpad` = has a ledger audit row; `external` = vault-only), `status = ready | missing` (ledger row without a vault counterpart), `references` (agents' tool auth + bound gateway targets), 20 per page. Secrets are passed through to `Create*CredentialProvider` and **never persisted, logged or echoed**.
- **Roles:** `demo-admin` creates / binds / deletes; `hr-intern` lists and (default-granted perm) may create; grants are always the caller's own. Out of scope: inbound gateway auth, consent-portal creation (admin-only infra, narrate), model-provider registration (E21).

## 2. Capabilities and pass criteria (positive)
| id | action | observable pass criteria | evidence |
|---|---|---|---|
| AC-E08-01 | List | `launchpad-global-bedrock` row: `kind api_key`, `status ready`, `arn` matching `^arn:aws-cn:bedrock-agentcore:cn-northwest-1:\d+:token-vault/default/apikeycredentialprovider/launchpad-global-bedrock$`, explained as the shared GLM 5.3 / Kimi K3 credential; `GET …/templates` offers no China-unavailable OAuth vendor | connections.json, shot.png |
| AC-E08-02 | Create `hr-saas-api-key` | `POST …/connections/api-key` 201; row `source launchpad`, `status ready`, creator = `demo-admin`; the request's `api_key` value appears in **no** response, log or screenshot | request.json (`api_key: "***"`), response.json |
| AC-E08-03 | Bind a gateway target | `POST …/gateway-targets {source openapi, openapi_schema <mock>, connection hr-saas-api-key, kind api_key, mode as_agent, api_key {location HEADER, parameter_name Authorization}}` 201; `GET …/gateway-targets` reads the binding back; after READY `GET /api/tools` lists the target's tools; the connection detail lists the target under `references` | targets.json, tools.json |
| AC-E08-04 | 「我的授权」 tab (`?view=mine`) | `GET /api/identity/grants` 200 `{grants: []}`; the tab explains 3LO and that no external OAuth2 server is configured in CN; `/v2/my-connections` → the nav alias is gone (404 / redirect per r13-c) | grants.json, shot.png |
| AC-E08-05 | Delete | `DELETE …/gateway-targets/{id}` 200 → `DELETE …/connections/api_key/hr-saas-api-key` 200 `{deleted: true}`; both gone from the lists; read-only AWS check shows the provider removed from the vault | responses, aws-readonly.json |
| AC-E08-06 | Member view | `hr-intern` lists the same connections; `references` of agents hidden by isolation are masked (E19); create allowed while `perm:identity.manage` is granted | two-identity connections.json |

## 3. Negative / boundary cases (≥ 10)
| id | input | expected | source | risk |
|---|---|---|---|---|
| NC-E08-01 | name `bad name!` or 129 chars; empty `api_key`; `client_secret` 2049 chars | `422 validation.invalid_request` **with the secret fields redacted** in the error body (nested too); plain limits stay readable | `test_identity_connections::test_api_key_validation_422_does_not_echo_the_key`, `::test_validation_422_redacts_nested_secrets_and_keeps_plain_limits` | R4 |
| NC-E08-02 | duplicate name (ledger or vault) | `409 identity.connection_exists` before the secret leaves the process; AWS `ConflictException` maps to the same code | `test_identity_connections::test_duplicate_name_is_409_before_the_secret_leaves`, `::test_aws_conflict_maps_to_connection_exists` | R4 |
| NC-E08-03 | delete a connection referenced by an agent tool or a bound target | `409 identity.connection_referenced {referenced_by}` (hidden agents masked); deleted agents do not hold references | `services/identity_providers.py::delete_connection`; `test_identity_connections::test_delete_refuses_referenced_by_agent_or_target` | R9 |
| NC-E08-04 | delete the environment's gateway M2M or API-key system provider | `409 identity.system_connection` | `test_identity_connections::test_delete_refuses_system_connections` | R9 |
| NC-E08-05 | delete `launchpad-global-bedrock` | `409 identity.external_connection` "created outside Launchpad — delete it in AgentCore Identity" (bootstrap writes no ledger row and `model_credential` pins are not counted as references; see §7) | `identity_providers.py::delete_connection`; `test_identity_connections::test_delete_refuses_an_external_provider_and_leaves_the_vault_alone` | R9 / R2 (every agent's model key) |
| NC-E08-06 | unknown name; `kind` other than `oauth2\|api_key`; a provider recorded only by another workspace | `404 identity.connection_not_found`; `422 validation.invalid_request` (path `Literal`); delete refused (`test_delete_refuses_a_provider_recorded_only_by_another_workspace`) | `routers/identity.py`, `test_identity_connections.py` | R9 |
| NC-E08-07 | target: `source openapi` without schema / unparsable schema; `mcp` with a bad endpoint; `mcp` + `api_key`; duplicate target name; unknown connection; workspace without `gateway_id` | `422 identity.invalid_openapi`; `422 identity.invalid_mcp_endpoint`; `422 identity.target_auth_unsupported`; `409 identity.target_exists`; `404 identity.connection_not_found`; `409 identity.no_gateway` — all before any AWS call | `services/identity_gateway_targets.py`; `test_identity_gateway_targets.py` (11) | R2, input hygiene |
| NC-E08-08 | delete the bootstrap `hr-database` target; a target not bound to a connection; target id not ten alphanumerics | `409 identity.system_target`; `409 identity.target_unbound`; refused (`test_governance_target_sync::test_target_id_must_be_ten_alphanumerics`) | `identity_gateway_targets.py`; `test_identity_gateway_targets::test_delete_guards` | R9 |
| NC-E08-09 | OAuth2 vendor outside the table; `custom` without endpoints; explicit endpoints on a non-custom vendor; `mode obo` on a gateway without a JWT authorizer; `JWT_AUTHORIZATION_GRANT` with actor scopes | `400 identity.unsupported_vendor`; `400 identity.missing_endpoints`; `400 identity.endpoints_custom_only`; `400 identity.obo_needs_jwt_gateway`; `422 identity.obo_invalid` (default `AppError` status 400 where the code sets none) | `services/identity_providers.py`, `test_obo.py` (13), `test_identity_connections::test_router_oauth2_request_validation` | input hygiene |
| NC-E08-10 | `POST …/oauth/complete` with an unknown session; another user's session; an expired one | `404 identity.session_unknown`; `403 identity.session_user_mismatch`; `409 identity.session_expired` | `services/oauth_sessions.py::complete` | R9 |
| NC-E08-11 | `hr-intern` `POST …/consent-portal`; admin with an execution role from another account | `403` (ADMIN route); refused (`test_consent_portal::test_route_refuses_a_cross_account_role`) | `test_member_permissions::test_consent_portal_writes_are_admin_only` | R9 |
| NC-E08-12 | `hr-intern` with `perm:identity.manage` revoked `POST …/connections/api-key` | `403`; the CREATE button is disabled with the missing-permission note | `test_route_policy::test_fully_revoked_member_gets_403` | R9 |
| NC-E08-13 | **fault injection**: gateway target read fails during list / during delete | list still 200 (targets' references omitted); `DELETE` refuses rather than skipping the reference check (mapped `aws.*` or 502) | `test_identity_connections::test_router_list_survives_a_gateway_read_failure`; `routers/identity.py::delete_connection` comment | R2 |

## 4. Evidence
Per TEMPLATE §4. Mandatory here: every `request.json` with `api_key` / `client_secret` replaced by `***` **before** upload; `connections.json` before/after; `aws-readonly.json` (`list-api-key-credential-providers`, `list-gateway-targets`) for AC-05; a grep of the evidence folder for `sk-`, `lp_live_`, `AKIA` must return nothing (Q2b checklist).

## 5. Automation mapping and GAPs
| case | existing automation | status |
|---|---|---|
| AC-01/02/05 | `tests/e2e/cn/09-identity-provider.spec.ts::case 9` (list, OAuth2 create + delete); `test_identity_connections.py` (30) | covered (API-key create path **GAP** in e2e → extend case 9 with `hr-saas-api-key`) |
| AC-03, NC-07/08 | `19-gateway-admin.spec.ts::case 19` (target → `/api/tools` → delete), `test_identity_gateway_targets.py` (11), `test_tool_auth.py` (24) | covered |
| AC-04, NC-10 | `test_identity_connections` grants paths, `routers/identity.py`; e2e `01-login-overview-nav` allowed-nav set (no `/v2/my-connections`, r13-c) | partial: **GAP** no e2e opens `?view=mine` and asserts `my-grants` empty state → extend case 9 |
| NC-01/02/03/04/06 | `test_identity_connections.py` | covered |
| NC-05 | `test_identity_connections::test_delete_refuses_an_external_provider_and_leaves_the_vault_alone` (generic external) | partial: **GAP** no test names `launchpad-global-bedrock` specifically |
| NC-09 | `test_obo.py`, `test_identity_connections::test_vendor_table_has_no_china_unavailable_oauth_vendors` | covered |
| NC-11/12 | `test_consent_portal.py` (18), `test_member_permissions.py`, `test_route_policy.py` | covered |
| NC-13 | `test_identity_connections::test_router_list_survives_a_gateway_read_failure` | covered (unit); delete-path injection **GAP** → Q4a |

## 6. Failure classification
`ENV`: Identity control-plane throttling (`429 aws.throttled`), target `CREATING` longer than the recording window (tools appear only after READY), gateway list read failure. `TEST`: table text matching on names. Else `PRODUCT`. Any secret visible in a response or screenshot is a **blocking PRODUCT/PROCESS** finding regardless of status code.

## 7. Open rulings
- `launchpad-global-bedrock` is protected only by the "external" rule: it is not in `system_provider_names()` and agents' `model_credential` pins are not counted as references. Decide whether bootstrap should register it as a system connection (then NC-05 becomes `409 identity.system_connection`).
- Members may mint vault credentials by default (`perm:identity.manage` default-granted) — keep or restrict, same question as E06/E07.
