# Identity P3 — real-AWS e2e evidence (inbound JWT, invoke as user, OBO)

> Real identifiers (account id, Cognito pool, client and domain ids, callback ids, gateway ids, local paths) are redacted to placeholders such as `123456789012` and `us-west-2_EXAMPLE`.

- **When and where:** 2026-09-28 (UTC), account `123456789012`, region `us-west-2`.
- **Driver:** [`backend/scripts/e2e_identity_inbound.py`](../backend/scripts/e2e_identity_inbound.py),
  subcommands `up` / `run` / `down` / `trail`.
- **Stack:** a throwaway backend on `127.0.0.1:8793` with a temporary SQLite ledger
  (`/tmp/lpid-e2e-p3.db`).
  - `LAUNCHPAD_RESOURCES` held `execution_role_arn`, `artifacts_bucket` and the
    throwaway `gateway_id` only.
  - No registry and no Memory, so the `register` stage was skipped.
- **Scope kept:**
  - No production host or DB was touched.
  - `launchpad-users`, `launchpad-gw` and every other `launchpad-*` resource were
    left alone.
  - Every AWS resource created is named `lpid-e2e-p3*`, apart from the per-agent
    role noted in §1.
  - Everything was deleted at the end (§5).

## 1. Scope decisions

| Decision | Why |
|---|---|
| The IdP is a **new throwaway Cognito pool** `lpid-e2e-p3-pool`, with a domain, a resource server `lpid-e2e-p3` (scope `invoke`), an M2M client (client_credentials), a user client (`USER_PASSWORD_AUTH`) and one user | `launchpad-users` must not be modified. A pool we own mints both a user JWT and an M2M JWT headlessly |
| A **throwaway CUSTOM_JWT gateway** `lpid-e2e-p3-gw` holds the `obo` target | `obo` needs a JWT-authorized gateway, and `launchpad-gw` must not be modified |
| "Invoke as me" was exercised **in-process**: `invoke_agent_text(row, bearer_token=<throwaway user JWT>, actor_id=scoped_actor(...))` | Console Chat mints its user JWT from the workspace's own pool (`launchpad-users`), which cannot be repointed at the throwaway pool. The chat route's token choice (`as_user` on / off / unavailable) is covered by hermetic tests (`tests/test_inbound_auth_p3.py::test_chat_toggle_picks_the_bearer`, `::test_chat_explicit_as_user_without_a_pool_user_is_409`) |
| The positive OBO path is proven at the **AWS-shape level**: a CustomOauth2 Connection with `onBehalfOfTokenExchangeConfig` against a placeholder IdP (`lpid-e2e-p3-idp.example.com`), and an `obo` target | No IdP that implements RFC 8693 / RFC 7523 was available. **The exchange itself is untested** |
| **Deviation:** the product's `provision` stage created the per-agent IAM role `launchpad-agent-lpid-e2e-p3-agent-<id8>` | Every zip Runtime deploy works this way (`app/services/agent_iam.py`). The rules allow it provided the role is deleted. The agent delete removed it (CloudTrail in §5) |

A first `run` attempt failed in step 1. The throwaway backend had been started
without `LAUNCHPAD_ACCOUNT_ID`, so the minted role's trust policy carried an empty
`aws:SourceAccount`, and `CreateAgentRuntime` answered "Role validation failed".
That agent (`0c6436af…`) was deleted through `DELETE /api/agents/{id}`, and its role
went with it (`GetRole` → `NoSuchEntity`; CloudTrail `DeleteRole` at 06:54:05Z). The
backend was then restarted with the account id, and the second attempt passed.

## 2. Resources created

| Resource | Identifier |
|---|---|
| Cognito user pool | `us-west-2_EXAMPLE` (`lpid-e2e-p3-pool`) |
| Cognito domain | `lpid-e2e-p3-xxxxxx` |
| Cognito M2M client | `<m2m-client-id>` (scope `lpid-e2e-p3/invoke`) |
| Cognito user client / user | `<user-client-id>` / `lpid-e2e-p3-user` (password only in the `0600` state file, deleted at teardown) |
| Gateway (CUSTOM_JWT, the throwaway pool's discovery URL) | `lpid-e2e-p3-gw-xxxxxxxxxx` |
| Agent (zip Runtime) | ledger `a1532421699e44249c79388baaf5da6b`, runtime `lpid_e2e_p3_agent_bdcd9c-0RevAO6bjl` |
| Workload identity | `lpid_e2e_p3_agent_bdcd9c-0RevAO6bjl` (created by the Runtime) |
| Per-agent IAM roles | `launchpad-agent-lpid-e2e-p3-agent-0c6436af` (first attempt), `launchpad-agent-lpid-e2e-p3-agent-a1532421` |
| Connections | `lpid-e2e-p3-cognito` (CustomOauth2 → the throwaway pool, no OBO), `lpid-e2e-p3-obo` (CustomOauth2, explicit endpoints, `obo: TOKEN_EXCHANGE / actor NONE`) |
| Gateway target | `lpid-e2e-p3-obo-target` (`QQDGLP4RFM`, OpenAPI, `grantType: TOKEN_EXCHANGE`) |
| Zip artifact | `s3://launchpad-artifacts-123456789012-us-west-2/agents/lpid-e2e-p3-agent/deployment_package.zip` |

## 3. The flow: 30 assertions passed, 0 failed (06:54:47Z–06:57:28Z)

| Step | What was asserted | Result |
|---|---|---|
| 1 IAM deploy | `POST /api/agents` → 202 → active; runtime v1 READY with **no authorizer**; SigV4 `InvokeAgentRuntime` answers | ✓ |
| 2 Switch IAM → JWT | `POST /api/agents/{id}/inbound-auth {mode: jwt, …}` → 202; **same runtime ARN**; version **1 → 2**; live `customJWTAuthorizer` equals the pinned config (discovery URL, allowed clients = both throwaway clients) | ✓ |
| 3 Callers | user access-token bearer → **200** (`data: {"event": "delta", "text": "Ok."}`); M2M bearer → **200**; SigV4 → **403** `AccessDeniedException: Authorization method mismatch…`; garbage bearer → **403** `OAuth authorization failed: Failed to parse token`; backend invoke chain with the user JWT and `scoped_actor(agent, human)` answers | ✓ |
| 4 Redeploy a JWT agent | `POST /api/agents/{id}/redeploy` with the JWT spec → version **2 → 3**; live authorizer **identical** after the re-publish (the omitted-authorizer reset trap does not fire) | ✓ |
| 5 Switch JWT → IAM | `inbound_auth {mode: iam}` → same runtime, version **3 → 4**; authorizer removed; SigV4 answers again; bearer → **403** "Authorization method mismatch" | ✓ |
| 6 OBO | Cognito `/oauth2/token` with `grant_type=urn:ietf:params:oauth:grant-type:token-exchange` → **400 `{"error":"unsupported_grant_type"}`**; Cognito Connection with `obo` → **422 `identity.obo_unsupported`**; plain Cognito Connection → 201; CustomOauth2 + `obo` → 201, and `GetOauth2CredentialProvider` echoes `onBehalfOfTokenExchangeConfig {grantType: TOKEN_EXCHANGE, tokenExchangeGrantTypeConfig {actorTokenContent: NONE}}`; `obo` target on the non-OBO Connection → **422**; `obo` target on the OBO Connection → 201, live `grantType: TOKEN_EXCHANGE`, and the console lists it with `mode: obo` | ✓ |

Acceptance mapping:

- **Criterion 1** (in-place switch with versions kept): steps 2 and 5.
- **Criterion 2** (redeploy keeps the authorizer): step 4, plus the hermetic
  tests `test_pinned_jwt_redeploy_echoes_the_same_authorizer` and
  `test_update_wrappers_forward_the_authorizer`.
- **Criterion 3** (user JWT OK, SigV4 403): step 3.
- **Criterion 4** (OBO where supported, clear 422 otherwise): step 6, and
  [identity.md §8.3](identity.md#83-obo-on-behalf-of-token-exchange).

## 4. Verdicts recorded from this run

- **Cognito does not implement RFC 8693.** Its token endpoint answers
  `unsupported_grant_type`, which matches the docs-based verdict in identity.md §8.3.
- **The authorizer does not bind the Memory actor.** The runtime accepted the user
  bearer with a payload `actor_id` of Launchpad's choosing (identity.md §8.2).

## 5. Teardown (`down`, 06:57:59Z–06:58:24Z) and deletion evidence

| Resource | Deletion evidence |
|---|---|
| Agent + runtime | `DELETE /api/agents/a1532421…` → `agent.deleted`; `GetAgentRuntime` → not found (`runtime.gone`) |
| Workload identity | `GetWorkloadIdentity` → not found (`workload_identity.gone`) |
| Gateway target `lpid-e2e-p3-obo-target` | `DELETE /api/identity/gateway-targets/QQDGLP4RFM` → 200 |
| Connection `lpid-e2e-p3-cognito` | console `DELETE` → 200; `GetOauth2CredentialProvider` → not found |
| Connection `lpid-e2e-p3-obo` | console `DELETE` → 409 (the target was still being deleted), then `DeleteOauth2CredentialProvider` directly; `Get…` → not found |
| Gateway `lpid-e2e-p3-gw-xxxxxxxxxx` | `DeleteGateway` → `GetGateway` not found (`gateway.gone`) |
| S3 prefix `agents/lpid-e2e-p3-agent/` | object deleted, 0 keys left |
| Cognito domain + pool | `DeleteUserPoolDomain`, `DeleteUserPool`; no `lpid` pool listed |
| IAM role `…-0c6436af` | CloudTrail `DeleteRolePolicy` `d02a27df-1491-4357-b867-14e07166ebdc` and **`DeleteRole` `d0144e57-3bef-4cc1-b4cd-da4835cfcf8f`** at 06:54:05Z (created by `CreateRole` `71e5ec0f-dec9-4db5-949a-de8e90aa664e`, 06:52:10Z) |
| IAM role `…-a1532421` | CloudTrail `DeleteRolePolicy` `f5d675f6-adfc-4840-b7fe-7db93760b4fd` and **`DeleteRole` `27b5a790-af05-4359-b539-9b297f206876`** at 06:57:59Z (first `CreateRole` `0b6f87a2-881c-4435-bf38-a40e5018b502` at 06:54:57Z; each later deploy re-ran the idempotent `CreateRole`); `GetRole` → `NoSuchEntity` |

An independent sweep after teardown returned empty lists for every `lpid`-named
resource of each kind:

- `list-gateways`
- `list-agent-runtimes`
- `list-oauth2-credential-providers`
- `list-workload-identities`
- `iam list-roles`
- `cognito-idp list-user-pools`

**No P3 e2e resource remains.**
