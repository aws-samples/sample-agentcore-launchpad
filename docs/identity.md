# AgentCore Identity in Launchpad

Launchpad productizes AgentCore Identity around three objects:

- **Connection**: the product shell over one AgentCore Identity *credential
  provider* (OAuth2 or API key) in a workspace's token vault. Secrets flow one way,
  from the request to AWS. The ledger keeps identifiers only.
- **Acting mode**: how one downstream is called. `as_agent` (M2M / API key, the
  agent acts as a service account), `as_user` (3LO, P2), `obo` (token exchange, P3).
- **Identity view**: the read-only identity page of one agent: workload identity,
  inbound mode, and every downstream with its acting mode and Connection.

This document covers **Phase P1** (Connections, Gateway targets bound to a
Connection, tool-level `as_agent` auth, the agent identity page) and **Phase P2**
(`as_user` 3LO: consent sessions, grants, revocation, `/auth/return`, My
connections, and the Consent Portal for Gateway targets; see §7), and **Phase P3**
(inbound JWT, Chat "invoke as me", `obo` token exchange and a tool-level Cedar
policy; see §8). It starts with
the service-model facts the implementation relies on, verified before any code was
written.

## 1. Service-model findings (verified 2026-09-28)

Source: the `bedrock-agentcore-control` and `bedrock-agentcore` service models
shipped with **botocore 1.43.83** (the backend venv, `uv run python -c "import
botocore; print(botocore.__version__)"`), dumped operation by operation. Gateway
target/auth compatibility is from the AWS devguide pages
[gateway-outbound-auth](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-outbound-auth.html)
and
[gateway-building-adding-targets-authorization](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-building-adding-targets-authorization.html),
cross-checked against [aws/agentcore-cli docs/gateway.md](https://github.com/aws/agentcore-cli/blob/main/docs/gateway.md).

### 1.1 Credential provider CRUD

| Operation | Notes |
|---|---|
| `CreateOauth2CredentialProvider` | `name`, `credentialProviderVendor`, `oauth2ProviderConfigInput`, `tags`. Output: `credentialProviderArn`, **`callbackUrl`**, `clientSecretArn`, `status`. |
| `GetOauth2CredentialProvider` | Output also carries `callbackUrl`, so the callback URL can be re-read. The ledger still snapshots it at create time. |
| `ListOauth2CredentialProviders` | `maxResults` **1–20** (declared in the model). A larger value fails with a `ValidationException`, so pages are fixed at 20. |
| `UpdateOauth2CredentialProvider` | **Exists.** Rotating a client secret is possible without delete/recreate. P1 does not expose it (see §6). |
| `DeleteOauth2CredentialProvider` | Error list includes `ConflictException`. |
| `CreateApiKeyCredentialProvider` | `name`, `apiKey` (≤ 65536), optional `apiKeySecretConfig`, `tags`. Output: `credentialProviderArn`, `apiKeySecretArn`. |
| `ListApiKeyCredentialProviders` | `maxResults` 1–100. Launchpad pages at 20 for both kinds. |
| `UpdateApiKeyCredentialProvider` | **Exists** (key rotation). Not exposed in P1. |
| `DeleteApiKeyCredentialProvider` | No `ConflictException` in its error list. |

- `CredentialProviderName` pattern: `[a-zA-Z0-9\-_]+`, length 1–128.
- Vendors (`credentialProviderVendor`): `GoogleOauth2`, `GithubOauth2`,
  `SlackOauth2`, `SalesforceOauth2`, `MicrosoftOauth2`, `AtlassianOauth2`,
  `LinkedinOauth2`, `CustomOauth2`, and newer entries: Okta, OneLogin, PingOne,
  Facebook, Yandex, Reddit, Zoom, Twitch, Spotify, Dropbox, Notion, Hubspot, CyberArk,
  FusionAuth, Auth0, Cognito, X. The newer vendors take an
  `includedOauth2ProviderConfig` (`clientId`\*, `clientSecret`, `issuer`,
  `authorizationEndpoint`, `tokenEndpoint`).
- `customOauth2ProviderConfig`: `oauthDiscovery` is **either** `discoveryUrl`
  (pattern `.+/\.well-known/(openid-configuration|oauth-authorization-server)`)
  **or** `authorizationServerMetadata` {`issuer`\*, `authorizationEndpoint`\*,
  `tokenEndpoint`\*}. It also takes `clientId`, `clientSecret` (≤ 2048),
  `clientAuthenticationMethod` (`CLIENT_SECRET_BASIC` / `CLIENT_SECRET_POST` /
  `PRIVATE_KEY_JWT` / `AWS_IAM_ID_TOKEN_JWT`), `privateKeyJwtConfig`,
  `onBehalfOfTokenExchangeConfig`, and `privateEndpoint`.
- Launchpad P1 templates cover the built-in vendors whose config is `clientId` +
  `clientSecret` (Google, GitHub, Slack, Salesforce, Microsoft, Atlassian, LinkedIn),
  `CustomOauth2` (discovery URL or explicit endpoints), and API key. Other vendors
  are refused with `identity.unsupported_vendor` rather than guessed.

### 1.2 Token exchange (data plane, `bedrock-agentcore`)

- `GetResourceOauth2Token`: `workloadIdentityToken`\*,
  `resourceCredentialProviderName`\*, `scopes`\* (required list, may be empty),
  `oauth2Flow`\* ∈ {`M2M`, `USER_FEDERATION`, `ON_BEHALF_OF_TOKEN_EXCHANGE`}, plus
  optional `audiences`, `customParameters`, `resources`, and the 3LO fields.
  Output: `accessToken` | `authorizationUrl` + `sessionUri`.
- `GetResourceApiKey`: `workloadIdentityToken`\*, `resourceCredentialProviderName`\*
  → `apiKey`.
- IAM resources for both: the token vault (`token-vault/default`), the provider ARN
  (`token-vault/default/oauth2credentialprovider/<name>` or
  `.../apikeycredentialprovider/<name>`), the workload identity directory plus the
  agent's **own** workload identity (`workload-identity/<runtime base>_??????-*`,
  never `workload-identity/*`: a wildcard would let one agent's role mint tokens
  for another agent's identity and read its users' 3LO tokens), and
  `secretsmanager:GetSecretValue` on
  `bedrock-agentcore-identity!default/{oauth2|apikey}/<name>-*`.

### 1.3 Workload identity

- `GetWorkloadIdentity(name)` returns `workloadIdentityArn` and
  `allowedResourceOauth2ReturnUrls`. Runtime auto-creates one per agent runtime
  and reports it as `workloadIdentityDetails.workloadIdentityArn` on
  `GetAgentRuntime`. The identity page reads it from there.

### 1.4 Gateway target credential configuration

`CreateGatewayTarget` takes `credentialProviderConfigurations`, a list of:

```
{ credentialProviderType: GATEWAY_IAM_ROLE | OAUTH | API_KEY | CALLER_IAM_CREDENTIALS | JWT_PASSTHROUGH,
  credentialProvider: {
    oauthCredentialProvider:  { providerArn*, scopes* (list), customParameters,
                                grantType CLIENT_CREDENTIALS | AUTHORIZATION_CODE | TOKEN_EXCHANGE,
                                defaultReturnUrl },
    apiKeyCredentialProvider: { providerArn*, credentialParameterName,
                                credentialPrefix, credentialLocation HEADER | QUERY_PARAMETER },
    iamCredentialProvider:    { service*, region } } }
```

- Target `name` pattern: `([0-9a-zA-Z][-]?){1,100}`. `description` is 1–200.
- Target status adds `CREATE_PENDING_AUTH` / `UPDATE_PENDING_AUTH` /
  `SYNCHRONIZE_PENDING_AUTH` (3LO targets waiting for consent).
- Target type ↔ outbound auth (devguide + agentcore-cli):

  | Target type | OAuth client credentials | API key | IAM only |
  |---|---|---|---|
  | OpenAPI schema | ✓ | ✓ | — |
  | MCP server | ✓ | — | — |
  | Lambda / Smithy | — | — | ✓ |

  Launchpad therefore binds a Connection to **OpenAPI** targets (OAuth or API key)
  and **MCP server** targets (OAuth only). Lambda and Smithy targets never take a
  Connection.

## 2. Connections (credential providers)

Backend: `app/services/identity_providers.py` + `app/routers/identity.py`.

| Route | Policy | Behavior |
|---|---|---|
| `GET /api/identity/connections` | member | Reconciles the vault (both kinds, paged at 20) with the ledger. Ledger rows AWS no longer has read `status: "missing"`. Providers created outside Launchpad read `source: "external"`. Platform-owned providers (`launchpad-gw-m2m`, `launchpad-office-facts-key`) read `system: true`. Each row lists `referenced_by` (agents and Gateway targets). |
| `GET /api/identity/connections/templates` | member | The creation templates (vendor, kind, required fields). |
| `GET /api/identity/connections/{kind}/{name}` | member | One Connection with its callback URL re-read from AWS (OAuth2). |
| `POST /api/identity/connections/oauth2` | `perm:identity.manage` | Creates an OAuth2 provider. The response carries `callback_url`: the redirect URI to register at the IdP. |
| `POST /api/identity/connections/api-key` | `perm:identity.manage` | Creates an API-key provider. |
| `DELETE /api/identity/connections/{kind}/{name}` | `perm:identity.manage` | Refuses a system provider, and any provider still referenced by an agent spec or a Gateway target. |

Error codes (all under `apiErrors.<code>` in the console):

- `identity.connection_exists` (409): the name is taken in this vault. This is
  checked against AWS **before the secret leaves the request**, and AWS's own
  `ConflictException` maps to the same code.
- `identity.system_connection` (409) and `identity.connection_referenced` (409,
  `detail.referenced_by`).
- `identity.connection_not_found` (404).
- `identity.unsupported_vendor`, `identity.missing_endpoints`,
  `identity.endpoints_custom_only` (422).
- `validation.invalid_request` (422): a malformed body. Its `detail` rows carry
  `loc`, `msg` and `type` but **never `input`**, because Pydantic's `input` is the
  whole body for a missing field and the value itself for a too-long one, so it
  would echo `client_secret` or `api_key`. `ctx` is also dropped when `loc` names
  a credential-like field (`*secret*`, `*token*`, `api_key` / `apiKey`,
  `password`, `private_key`, `authorization`, `credential`; case-insensitive,
  `core/errors.py`).

### Ledger

`identity_providers` is the table the earlier fork shipped (the same name, the
same columns, and the unique index `(workspace_id, kind, name)`), extended with
`client_id`, `scopes`, `template` and `description` (additive `_migrate` entries).
It is workspace-scoped: a token vault belongs to one (account, region).

The P2/P3 tables and columns that the earlier fork already created on deployed
ledgers are claimed by the models now, isomorphically, so none is orphaned:
`user_token_revocations`, `oauth_pending_sessions`, `agents.inbound_auth_mode` /
`agents.inbound_auth_config`, and `workspaces.settings`. P1 reads
`agents.inbound_auth_mode` only: NULL means IAM, which every P1 agent is. The other
tables and columns are used by the later phases: `oauth_pending_sessions` and
`user_token_revocations` by as_user (§7), `agents.inbound_auth_config` and
`workspaces.settings` by inbound JWT (§8.1).

### Permission

`identity.manage` is a member-grantable permission (default granted, revocable
per user in Users). It gates Connection create/delete and Gateway target
create/delete. Reads are plain member. The v2 page (`/v2/connections`, shown in the
console as **Outbound credentials** / 出站凭证 — each entry a credential / 凭证; the
API and this document keep the name Connection) lives in the admin
navigation group, but a member who holds the permission can use it by URL.

## 3. Gateway targets bound to a Connection

`POST /api/identity/gateway-targets` (`perm:identity.manage`) creates a target on
the **workspace gateway** (`resources.gateway_id`) with
`credentialProviderConfigurations` built from the Connection:

- OAuth2 Connection → `OAUTH` + `grantType: CLIENT_CREDENTIALS` + scopes
  (`as_agent`, M2M).
- API-key Connection → `API_KEY` + `credentialLocation` / `credentialParameterName` /
  `credentialPrefix` (OpenAPI targets only).

Supported target sources: OpenAPI (inline JSON/YAML schema) and MCP server
(https endpoint). The acting mode is `as_agent`, or `as_user` on an OAuth2
Connection (P2; it needs the workspace gateway's Consent Portal, §7.6), or `obo`
on an OAuth2 Connection with an on-behalf-of config, on a CUSTOM_JWT gateway (P3,
§8.3; refused with `identity.obo_unsupported` 422 / `identity.obo_needs_jwt_gateway`
409). `GET /api/identity/gateway-targets`
lists the gateway's targets with their bound Connection (read back from
`GetGatewayTarget`, so AWS stays the source of truth), and
`DELETE /api/identity/gateway-targets/{target_id}` removes one. Platform-owned
targets are refused.

Error codes:

- `identity.no_gateway` (409): the workspace has no `gateway_id` (bootstrap first).
- `identity.invalid_target_name`, `identity.invalid_openapi`,
  `identity.invalid_mcp_endpoint`, `identity.target_auth_unsupported` (API key on
  an MCP server target), `identity.mode_unsupported` (422). All are raised before
  any AWS write.
- `identity.target_exists` (409): the name is taken on the gateway, checked
  up front and mapped from `ConflictException`.
- `identity.target_not_found` (404).
- `identity.system_target` (409): a bootstrap target (`hr-database`,
  `office-facts`). `identity.target_unbound` (409): a target with no Connection
  binding belongs to the surface that created it (Registry, KB mounts), not this
  one.

Deleting a Connection re-reads the gateway's targets with the non-swallowing
list, so a gateway read failure refuses the delete instead of skipping the
reference check. The read paths tolerate the failure.

Any agent that attaches the gateway, of any method, gets the target's outbound
auth with no code change. The gateway performs the exchange.

## 4. Tool-level auth (`ToolRef.auth`)

```json
{"type": "rest", "name": "crm", "config": {"url": "https://api.example.com/v1"},
 "auth": {"connection": "crm-oauth", "kind": "oauth2", "mode": "as_agent",
          "scopes": ["crm/read"]}}
```

- `connection` names the credential provider. The legacy key `provider` is
  accepted. The legacy `flow` is mapped: `M2M` → oauth2 + `as_agent`, `API_KEY` →
  api_key + `as_agent`, `USER_FEDERATION` → oauth2 + `as_user`.
- `kind` is `oauth2` | `api_key`. `api_key` takes an optional placement
  `{"in": "header"|"query", "name": "..."}`, with a default of
  `Authorization: Bearer <key>`. Scopes apply to oauth2 only.
- Supported on `zip_runtime` (HTTP) and `byoc`. On zip_runtime the generated agent
  performs the exchange (`templates/identity_tools.py.tmpl`: workload token →
  `GetResourceOauth2Token(M2M)` / `GetResourceApiKey`). On byoc the auth is a
  declaration: exact IAM grants plus the `LAUNCHPAD_OUTBOUND_AUTH` env JSON, and
  the member's code performs the exchange. The execution role gets exact
  per-provider grants, never the family-wide identity wildcard.
- **Request-time validation** (`POST /api/agents`, redeploy): every referenced
  Connection must exist in the live vault, with a matching kind
  (`identity.connection_unknown` / `identity.kind_mismatch`, 422). `as_user` is
  accepted on oauth2 Connections only (an api_key Connection can only act as the
  agent, a 422 from the schema), and `obo` is refused on a tool with
  `identity.mode_unsupported` (422): obo runs on a Gateway target bound to the
  Connection instead (§3, §8.3). A stored spec
  keeps validating after its Connection is deleted, and the next save fails loudly.
- **No silent stripping (P0-1 fix)**: `auth` is declared in the backend
  `AgentSpec` (which ignores unknown fields, so an undeclared key would be dropped
  on every save). In the console it round-trips `formFromStoredSpec` ↔
  `buildAgentSpec` (`lib/agent-spec.ts`), shared by classic and v2. When editing,
  a Connection missing from the catalog **blocks submission** with an error instead
  of dropping the block. `authToolIssues` is unit-tested in
  `frontend/src/lib/agent-spec.test.ts`.

## 5. Agent identity page

`GET /api/agents/{agent_id}/identity` (member) returns:

- `workload_identity`: `{name, arn, allowed_return_urls}`, from `GetAgentRuntime`
  → `workloadIdentityDetails` and then `GetWorkloadIdentity`. The harness has none
  (it reports `managed`).
- `inbound`: `{mode: "iam"|"jwt", source}`, the deployed snapshot (§8.1).
- `downstreams[]`: each tool with auth (mode + Connection + status of that
  Connection), each attached gateway (`as_agent` via the platform M2M Connection
  `launchpad-gw-m2m`), and each Gateway target bound to a Connection.

The v2 page is `/v2/agents?view=identity&id=<agent>`.

## 6. Not in P1

- Secret rotation through `Update*CredentialProvider`. The API exists (§1.1,
  re-confirmed in §8.4); a console action is deferred.
- `as_user` (3LO), `My authorizations`, revocation (all shipped in P2, §7), inbound
  JWT, and `obo` (shipped in P3, §8).
- Per-Connection workspace visibility. A Connection is scoped to its workspace's
  token vault by construction.

## 7. P2: `as_user` (3LO)

An `as_user` downstream is called with **the end user's own token**. That token
comes from a one-time consent at the IdP and is kept in the AgentCore Identity
token vault under (workload identity, user). Launchpad never sees it. The ledger
holds only derived progress: pending sessions, grants and revocations.

### 7.1 The four legs

1. **Ask.** On zip_runtime the generated tool
   (`templates/identity_tools.py.tmpl`) calls
   `GetResourceOauth2Token(oauth2Flow=USER_FEDERATION)` with the workload token of
   the invoking user (the platform always sends `runtimeUserId` when a tool carries
   auth), `resourceOauth2ReturnUrl` and a `customState` of `{agent_id, tool,
   session_id}`. The call is **non-blocking**. With no vaulted token, the response
   carries `authorizationUrl` + `sessionUri`. The tool then queues an auth notice
   and answers the model with a structured auth-required result. It never polls. On
   byoc the declaration (`mode: as_user` in `LAUNCHPAD_OUTBOUND_AUTH`, plus
   `LAUNCHPAD_OAUTH_RETURN_URL`) is handed to the member's code, which performs the
   exchange.
2. **Surface.** The invoke chain (`services/agentcore/runtime.py`) turns the notice
   into an `auth_required` SSE event `{agent_id, provider, tool, scopes, url}`. The
   `sessionUri` is stored server-side in `oauth_pending_sessions` (with the
   invoking user, the agent, the Connection and a `SESSION_TTL_S` = 900 s expiry)
   and **never reaches the browser**. A `user_grants` row goes to `pending`.
3. **Consent.** The user opens the single-use authorization URL. After consent,
   AgentCore redirects the browser to the return URL with `session_id` (the
   `sessionUri`) and `state` (the `customState`).
4. **Bind.** `/auth/return` posts `session_id` to `POST /api/identity/oauth/complete`,
   which calls `CompleteResourceTokenAuth` with the user **recorded on the pending
   session**. The caller must be that user. This leg is required: until it runs,
   the vault does not release the token. The grant goes to `authorized` and the
   session row is deleted, because it is single-use.

### 7.2 Return URL

The return URL is `oauth_return_url` (env `LAUNCHPAD_OAUTH_RETURN_URL`), or
`{public_base_url}/auth/return` when that is unset. For every spec with an as_user
tool, the deployer does two things:

- It bakes the URL into the runtime environment.
- It reconciles it onto the runtime's workload identity
  (`allowedResourceOauth2ReturnUrls`, `deployer/return_url.py`, zip_runtime and
  byoc).

A reconcile failure **fails the deploy stage** visibly. Without the URL on the
allow-list, every consent redirect would end at the provider's callback page.

### 7.3 Grants and revocation

AgentCore Identity has no API that revokes a vaulted user token. Launchpad
revokes by **forcing re-authentication**:

- `DELETE /api/identity/grants/{connection}` records a `user_token_revocations`
  row for (workspace, user, Connection). It marks each of the caller's grants on
  that Connection `revoked` and drops in-flight sessions for it, so a consent
  already under way cannot bind after the revoke.
- On every invoke of an agent with as_user tools, `services/invoke.py` computes
  the Connections whose revocation is still in force *for this agent*. The
  generated tool then sends `forceAuthentication=true`, so the IdP asks again.
- A revocation stops being in force for an agent once that agent's grant is
  re-authorized after it (`authorized_at >= requested_at`).

`effectiveStatus` in the console treats `authorized` with `force_reauth: true` as
still pending.

### 7.4 Routes, permission and errors

| Route | Policy | Behavior |
|---|---|---|
| `POST /api/identity/oauth/complete` | `perm:identity.grant` | Leg 4. Returns `{provider, agent_id, agent_name, tool, …}` |
| `GET /api/identity/grants` | member | The caller's **own** grants, never another member's |
| `GET /api/identity/grants/{connection}/status?agent_id=` | member | `{status, force_reauth, …}`, polled by the Chat auth card |
| `DELETE /api/identity/grants/{connection}` | `perm:identity.grant` | Revoke (§7.3). Returns `{revoked, provider, agents}` |
| `GET /api/identity/consent-portal` | member | The workspace gateway's portal, read back from AWS |
| `POST` / `DELETE /api/identity/consent-portal` | **admin** | Create / delete the portal (§7.6). Not member-grantable: `identity.manage` does not reach it |

`identity.grant` is a member-grantable permission, like `identity.manage`
(default granted, revocable per user in Users). Without it, a member can still see
their grants but cannot bind or revoke.

Error codes (all under `apiErrors.<code>` in the console):

- `identity.session_unknown` (404): no pending session with that id. It was never
  issued, has already been used, or was dropped by a revoke.
- `identity.session_expired` (409): older than 15 minutes.
- `identity.session_user_mismatch` (403): the caller is not the user the session
  was issued for.
- `identity.session_completion_failed` (502): `CompleteResourceTokenAuth` refused.
- `identity.session_token_unavailable` (409): a session asked over *Invoke as me*
  cannot be completed because no user JWT of the caller could be minted.
- `identity.as_user_requires_user_jwt` (409): an as_user consent on a JWT agent
  was asked over the shared workspace M2M token, or such a session was presented
  for completion. See §8.2.
- `identity.consent_portal_required` (409), `identity.gateway_missing` (409),
  `identity.consent_portal_exists` (409), `identity.consent_portal_not_found`
  (404), `identity.invalid_portal_name` / `identity.invalid_role_arn` /
  `identity.role_account_mismatch` / `identity.portal_scopes` (422): see §7.6.

### 7.5 Console surfaces (v2)

- **Chat auth card** (`v2/pages/chat/AuthCard.tsx`, logic in `lib/user-grants.ts`):
  - Shows the Connection, the tool, the scopes and an "Open authorization" link.
  - While live, it polls the grant status every 2.5 s, up to the 15-minute
    session limit. It flips to authorized as soon as consent completes in another
    tab, and the **Retry** then re-sends the user turn that triggered the ask.
  - A card restored from history has no URL, because the URL is single-use and
    never persisted. It checks the status once and offers the retry.
- **`/auth/return`** (`v2/pages/AuthReturn.tsx`):
  - Runs leg 4 and removes the spent `session_id` from the address bar.
  - Shows binding / done / failed, with the mapped error code.
  - Offers a way back to the chat named in the `state`.
- **My authorizations** / 我的授权 (`/v2/my-connections`): the caller's grants per (Connection,
  agent), with status. Revoke goes through a ConfirmDialog, is busy while the call
  runs, and is disabled while a revocation is in force or without
  `identity.grant`.
  Deleting an agent drops every user's grants on it (its workload identity, and
  every vaulted token keyed by it, goes with the runtime); deleting a Connection
  drops its grants, revocations and in-flight consents. Grants of an agent
  deleted before that cleanup existed are hidden from the list.
- **Agent wizard**: an OAuth2 tool can pick the acting mode `as_user`. An api_key
  tool cannot.

**`/v1` never starts a consent.** Leg 4 needs a signed-in console user matching
the recorded runtime user, and a `/v1` caller is an API key, not a console user.
So when an `as_user` tool asks for consent on a `/v1` call, Launchpad records no
pending session and the call fails with `identity.as_user_requires_console` (409;
an `error` event on the stream). Authorize the tool once from console Chat.

Real-AWS evidence: [identity-e2e-evidence-p2.md](identity-e2e-evidence-p2.md).

### 7.6 Consent Portal (as_user Gateway targets)

An as_user **Gateway target** (`grantType=AUTHORIZATION_CODE`) differs from an
as_user tool. The *gateway* performs the exchange, so the consent has to happen
somewhere the gateway knows about. AgentCore provides this as a managed **Consent
Portal**, one per gateway. The portal signs the user in with the gateway's inbound
IdP and lists the gateway's AUTHORIZATION_CODE targets for the user to connect.

**Verdict (verified 2026-09-28): available, integrated in the backend, not
exercised live.**

- **Available.** The consent-portal operations are in the
  `bedrock-agentcore-control` model of the pinned botocore (≥ 1.43.103). Live
  `ListConsentPortals` in `123456789012` / us-west-2 returned 200
  `{"consentPortals": []}`.
- **Integrated.** The wrappers are `services/agentcore/consent_portal.py`, the
  service is `services/consent_portals.py`, the routes are listed in §7.4, and
  hermetic tests are in `tests/test_consent_portal.py`.
  - Creating an as_user target reads the gateway's portal. With no `ACTIVE`
    portal, the create is refused with `identity.consent_portal_required` (409)
    before any AWS write.
  - Otherwise the target is created with `grantType: AUTHORIZATION_CODE` and
    `defaultReturnUrl: <portalUrl>/connect/callback`.
  - The portal view returns both URLs the operator must wire (`callbacks`).
- **Not live-e2e'd.** A portal needs an execution role, and the P2 rules forbid
  creating IAM roles for it. It also needs a gateway whose JWT authorizer shares
  the IdP's issuer, and `launchpad-gw` (IAM inbound) must not be modified. The
  as_user *tool* path, which needs no portal, was proven live instead. There is
  no console surface for the portal yet; operators use the API.

**What an operator provides** (from the devguide):

1. **A JWT-inbound workspace gateway.** Its authorizer must reference an OIDC IdP
   that issues JWT access tokens. OAuth2-only vendors (GitHub, Slack, Salesforce,
   Atlassian, LinkedIn) cannot be the portal's IdP, although they remain valid as
   target providers.
2. **An OAuth2 Connection for that IdP**, passed as `connection`.
   - It must have the **same issuer** as the gateway authorizer, which AWS
     validates at create time.
   - The IdP app must be an OIDC web app using the authorization-code grant, with
     a client secret.
   - Its scopes must include `openid`; otherwise the create fails with
     `identity.portal_scopes`.
3. **An execution role** (`execution_role_arn`), which Launchpad deliberately does
   not create. The backend principal passes this role to AgentCore, so:
   - Only an **administrator** can create or delete the portal (route policy
     `ADMIN`; a member gets `403 auth.forbidden` even with `identity.manage`).
   - The ARN must be `arn:<partition>:iam::<workspace account>:role/…`, where the
     partition is the one of the workspace region. A role in any other account or
     partition is refused with `identity.role_account_mismatch` (422) before any
     AWS call, as is any role while the workspace account is unknown.
   - The role needs:
     - Trust: `bedrock-agentcore.amazonaws.com`, with `aws:SourceAccount` and
       `aws:SourceArn` = the portal ARN. The ARN can only be added after the portal
       exists.
     - `bedrock-agentcore:GetGateway`, `GetGatewayTarget` and `ListGatewayTargets` on
       the gateway.
     - `GetOauth2CredentialProvider` and `ListOauth2CredentialProviders` on
       `token-vault/default` and its `oauth2credentialprovider/*`.
     - `CompleteResourceTokenAuth`, `GetResourceOauth2Token` and
       `GetWorkloadAccessTokenForJWT` on `*`.
     - `secretsmanager:GetSecretValue` on
       `bedrock-agentcore-identity!default/oauth2/*`, conditioned on the
       `aws:secretsmanager:owningService` tag.
4. **Two URLs to wire once the portal is `ACTIVE`:**
   - `<portalUrl>/callback` goes on the IdP app's allowed redirect URIs.
   - `<portalUrl>/connect/callback` is every as_user target's `defaultReturnUrl`,
     which Launchpad sets itself.

Sources (AWS devguide, `https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/`):
[identity-consent-portal-prerequisites](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-consent-portal-prerequisites.html),
[identity-consent-portal-execution-role](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-consent-portal-execution-role.html),
[identity-create-consent-portal](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-create-consent-portal.html),
[identity-configure-consent-portal-target](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-configure-consent-portal-target.html),
[identity-update-consent-portal](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-update-consent-portal.html),
[identity-create-consent-portal-console](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-create-consent-portal-console.html),
and the AWS ML blog post
[Manage end-user OAuth consent for AI agents with Amazon Bedrock AgentCore](https://aws.amazon.com/blogs/machine-learning/manage-end-user-oauth-consent-for-ai-agents-with-amazon-bedrock-agentcore/).

## 8. P3: identity passthrough

P3 lets a caller's own identity reach the agent (inbound JWT, Chat "invoke as me")
and the agent's downstreams (`obo` token exchange). It also adds a Cedar policy
per Gateway tool. The real-AWS evidence is in
[identity-e2e-evidence-p3.md](identity-e2e-evidence-p3.md).

### 8.1 Inbound auth: IAM (SigV4) vs JWT bearer

An AgentCore Runtime accepts **one inbound mode at a time**:

- IAM SigV4, the default.
- A `customJWTAuthorizer`. Callers send `Authorization: Bearer <jwt>` from an OIDC IdP.

**Where the choice lives**

- **Workspace default.** Edited under *Workspaces → detail → Inbound auth default*
  (`GET/PUT /api/identity/inbound-auth/default`; PUT needs `identity.manage`).
  - It is stored in `Workspace.settings["inbound_auth_default"]`.
  - Agents whose spec pins nothing inherit it **on their next deploy**. A deployed
    runtime keeps its authorizer until it is redeployed.
  - The "Use workspace Cognito pool" preset uses the bootstrap pool's discovery
    URL and pre-lists the console client and the M2M client.
  - Nothing is sent until *Save*; *Discard changes* (enabled once the editor
    differs from the saved default) puts the editor back.
- **Per agent.** The wizard's *Inbound auth* section (inherit / IAM / JWT) writes
  `spec.inbound_auth`. An explicit value pins the agent.
- **Resolution** is `spec.inbound_auth` → workspace default → IAM.
  - The deploy pipeline resolves it once per deploy and snapshots it on the agent
    row (`inbound_auth_mode` / `inbound_auth_config`).
  - Chat, `/v1` and the console read that snapshot.
  - Only Runtime-backed HTTP methods can carry JWT: `zip_runtime`, `studio`,
    `container` and `byoc` (`JWT_CAPABLE_METHODS`). The managed harness and A2A
    servers always resolve to IAM.

**The JWT config** is `{discovery_url, allowed_clients[], allowed_audience[],
allowed_scopes[], custom_claims[]}`, the service-model
`CustomJWTAuthorizerConfiguration`, plus an optional display-only
`source_connection` (below) that the authorizer build ignores. It is checked at
save time:

- The discovery URL must end in `/.well-known/openid-configuration`.
- The document is fetched once and must expose `jwks_uri`
  (`identity.discovery_unreachable` / `identity.discovery_invalid`).
- At least one restriction is required. A discovery URL alone would admit every
  token the issuer ever minted.

**Where the discovery URL comes from.** Every JWT editor (the wizard's *Inbound
auth* section, the workspace default card and the agent page's switch dialog)
shares one field set with an *Identity provider* row:

- **Use the workspace Cognito pool** fills the whole form with the bootstrap
  pool preset.
- **Choose from a Connection** lists the workspace's OAuth2 Connections whose
  OIDC discovery URL is derivable
  (`GET /api/identity/connections/oidc-sources`, member-readable). What is
  derivable was checked against the live `GetOauth2CredentialProvider` shape:
  every vendor output carries `oauthDiscovery`, holding either
  - `discoveryUrl`, used as-is (a CustomOauth2 Connection created from a
    discovery URL); or
  - `authorizationServerMetadata.issuer`, turned into
    `<issuer>/.well-known/openid-configuration` (OIDC Discovery 1.0 §4). This
    covers custom metadata Connections and the built-in vendors whose stored
    config names a concrete issuer (for example an Entra tenant).

  A templated issuer (`…/{tenantid}/v2.0`) names no single IdP and is left out.
  So is **GitHub**: its stored issuer is the Actions OIDC issuer
  (`https://token.actions.githubusercontent.com`), which does not sign OAuth-app
  user tokens. The system `launchpad-gw-m2m` provider is never offered, and a
  provider that cannot be read is skipped rather than failing the list.
- **A pick fills `discovery_url` only.** It never fills `allowed_clients`: the
  Connection's client id is the agent's own *outbound* client (how the agent
  calls tools), not an inbound caller. The console says so next to the picker.
  The Connection name is kept as `source_connection` for display ("From
  Connection X" on the identity card, read from the deploy-time snapshot while it
  still names the live discovery URL); typing a different URL drops it.

**Using your own IdP** (Okta, Auth0, Keycloak, Entra ID or any OIDC provider):

1. Enter the IdP's discovery URL, or create an OAuth2 Connection for it first
   (§2) and choose it.
2. Register the **caller** applications at the IdP and list their client ids in
   *Allowed clients*, or restrict by audience, scopes or a claim. At least one
   restriction is required.
3. Save. The discovery document is probed as above.
4. Hand callers the runtime's bearer URL and a token recipe
   (`samples/inbound-jwt/`). They are the only callers such an agent admits (see
   the next paragraph).

**Console reachability.** Every platform invoke presents a **workspace-Cognito**
token:

- console Chat with *Invoke as me* on (the user's pool JWT) and off (the M2M
  token);
- `/v1`, `POST /api/agents/{id}/invoke` and evaluation (the M2M token).

An agent whose authorizer trusts another issuer refuses all of them. Every JWT
editor therefore compares the issuer of the discovery URL (the URL minus
`/.well-known/openid-configuration`, scheme and host lowercased, no trailing
slash) with the workspace pool issuer
`https://cognito-idp.{region}.amazonaws.com/{pool id}` (`cognito_issuer` on
`GET /api/identity/inbound-auth/default`). When they differ, it warns that only
external callers holding tokens from that IdP can invoke the agent (on the
workspace default card: every agent that inherits the default). The wizard's
review step repeats it with the discovery URL the agent deploys with (pinned, or
an inherited JWT default), and the agent detail page shows it for a deployed JWT
authorizer on another issuer. Server-side,
the invoke chain makes the same comparison before it mints or sends any token and
fails with `409 agent.inbound_issuer_mismatch` (detail `{agent_issuer,
workspace_issuer, caller: "user_jwt" | "m2m"}`) instead of the authorizer's bare
403. Evaluation checks at submit, before a run row exists. Without a workspace
pool there is nothing to compare, and the M2M path keeps its own
`identity.m2m_unavailable`.

**Switch in place.** The agent detail page shows the deployed mode and the
authorizer summary, plus a **Switch** action.

- **To IAM** is a confirm dialog.
- **To JWT** opens an editable dialog holding the full JWT field set. It is
  prefilled from the JWT workspace default, else the Cognito preset, else empty,
  and every field can be changed. The wizard's validation gates the submit, and
  the backend discovery probe's errors (`identity.discovery_unreachable` /
  `identity.discovery_invalid`) appear in the dialog.

Both dialogs name the caller-side change: SigV4 callers get 403 after a switch to
JWT, and bearer callers are refused after a switch back to IAM. The action calls
`POST /api/agents/{id}/inbound-auth`, which:

- re-publishes the stored spec with only the pin changed;
- runs `UpdateAgentRuntime` on the **same runtime ARN**, so the version increments
  and earlier versions are kept;
- rolls the DEFAULT endpoint.

Switching back is the rollback. The e2e proves both directions on one runtime.

**Redeploys keep the authorizer.** `UpdateAgentRuntime` **resets an omitted
`authorizerConfiguration`**, so every deployer echoes the resolved authorizer on
every Create/Update. Omitting it (IAM) is exactly the JWT→IAM switch. Two things
assert this: unit tests over each deployer, and the e2e step 4 (a `/redeploy` of a
JWT agent leaves the live authorizer byte-identical).

**Invoking a JWT agent.** boto3 cannot send a bearer. JWT agents are therefore
invoked over the data-plane HTTPS endpoint:

- URL: `POST https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{url-encoded ARN}/invocations?qualifier=DEFAULT`
- Header: `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`

| Caller | Token |
|---|---|
| Console Chat, "invoke as me" on | the signed-in user's Cognito JWT |
| Console Chat, toggle off; `/v1`; evaluation | the workspace M2M `client_credentials` token (minted server-side, cached until expiry, never persisted) |
| External callers | their own token. See `samples/inbound-jwt/` for a client_credentials curl and a Python caller |

All three platform rows need the agent to trust the workspace pool issuer. An
agent on another IdP is reachable only by external callers
(`agent.inbound_issuer_mismatch`, above).

A non-interactive path fails with a named error when it cannot mint a token. It
never falls back to SigV4:

- `identity.m2m_unavailable` (503)
- `identity.m2m_token_failed` (502)

An authorizer rejection surfaces as `agent.inbound_auth_rejected`.

The execution role of a JWT agent is granted `GetWorkloadAccessTokenForJWT`: the
runtime exchanges the caller's bearer for a workload token.

### 8.2 Chat "invoke as me" and the memory actor

For a JWT agent, the Chat composer shows an *Invoke as me* toggle (`as_user` on
`POST /api/chat/{id}`):

- **On:** the turn sends the user's Cognito JWT. This is the same token the Gateway
  policy path mints, and it is reused within the turn.
- **Off:** the turn sends the workspace M2M token.
- **`true` with no pool sign-in** (auth gate off): `409 chat.as_user_unavailable`.
  An explicit "as me" never quietly becomes the machine identity.
- **Every turn** reports `meta.inbound = {mode: "jwt", caller: "user_jwt" | "m2m"}`.

**as_user tools on a JWT agent need the user's JWT.** Under a JWT authorizer
the token vault keys an `as_user` consent on the inbound JWT subject. The
workspace M2M token has **one subject that every M2M caller shares**: Chat with
the toggle off, every `/v1` key holder, `POST /api/agents/{id}/invoke` and
evaluation runs. A consent bound to it would hand one member's downstream token
to all of them. So:

- **Ask.** When an `as_user` tool of a JWT agent asks for consent on a call that
  presented the M2M token, Launchpad records **no** pending session and no grant,
  and sends **no** authorization URL. The call fails with
  `identity.as_user_requires_user_jwt` (409, detail `{provider, tool, agent_id,
  caller: "m2m"}`). In console Chat the message says to turn on *Invoke as me*.
  On `/v1` and direct invoke it says that as_user tools on JWT agents need a user
  JWT; those surfaces present only the M2M token, so external callers use the
  runtime directly with the user's own token (`samples/inbound-jwt/`).
- **Bind.** `POST /api/identity/oauth/complete` refuses a pending session whose
  `caller_kind` is `m2m` (a row written before this rule) with the same 409. It
  drops the row and never calls `CompleteResourceTokenAuth`.
- **Unchanged.** IAM-inbound agents key the vault on `runtimeUserId` and complete
  with `userId`. A consent asked over *Invoke as me* is recorded as `user_jwt`
  and completed with a fresh Cognito JWT of the caller, who must be the recorded
  user (`identity.session_user_mismatch` otherwise).

**Actor-mapping verdict: the Memory actor does not depend on the inbound token.**

- **Launchpad's rule.** Launchpad keeps the actor at
  `scoped_actor(agent_id, human)` = `<agent>__<human>` in both toggle states. It
  carries the actor in the payload (`actor_id`), with `human` derived server-side
  from the signed console session. Toggling mid-session keeps one conversation and
  one memory partition.
- **Model source.** In the `bedrock-agentcore` model of botocore 1.43.103, the only
  user-identity input on `InvokeAgentRuntime` is `runtimeUserId`
  (`X-Amzn-Bedrock-AgentCore-Runtime-User-Id`). It is an IAM-caller header for
  `GetWorkloadAccessTokenForUserId`. The JWT authorizer only validates the bearer
  (`GetWorkloadAccessTokenForJWT` derives the workload token from it).
  - Nothing in the model rewrites the payload or binds the Memory `actorId` to the
    token `sub`. Memory namespaces are keyed on the `actorId` the runtime code
    passes.
- **Live source.** In e2e step 3 the backend invoke chain calls a JWT runtime with
  a user bearer and `actor_id=scoped_actor(...)`, and the call succeeds.
- **External callers.** They choose the actor themselves (`--actor-id` in
  `samples/inbound-jwt/`). To share a console conversation's memory they must pass
  the same `<agent>__<human>` string.

### 8.3 `obo`: on-behalf-of token exchange

An `obo` downstream exchanges the **caller's inbound JWT** for a downstream token
at the IdP, so the downstream sees the user rather than the agent. Unlike
`as_user`, there is no consent click.

**What AWS offers** (botocore 1.43.103 `bedrock-agentcore-control`):

- `CustomOauth2ProviderConfigInput.onBehalfOfTokenExchangeConfig` takes a
  `grantType`:
  - `TOKEN_EXCHANGE`, RFC 8693 (`urn:ietf:params:oauth:grant-type:token-exchange`)
  - `JWT_AUTHORIZATION_GRANT`, RFC 7523 (`urn:ietf:params:oauth:grant-type:jwt-bearer`)
- `TOKEN_EXCHANGE` also takes a `tokenExchangeGrantTypeConfig`:
  - `actorTokenContent` is one of `NONE`, `M2M` or `AWS_IAM_ID_TOKEN_JWT`.
  - `actorTokenScopes` applies to M2M only.
  - Launchpad offers `NONE` and `M2M`. `AWS_IAM_ID_TOKEN_JWT` needs
    `iam:EnableOutboundWebIdentityFederation` on the account, which is an account
    decision.
- A Gateway target's `oauthCredentialProvider.grantType` accepts `TOKEN_EXCHANGE`.
  The runtime-side call is `GetResourceOauth2Token(oauth2Flow=ON_BEHALF_OF_TOKEN_EXCHANGE)`.
- Doc sources:
  - [on-behalf-of-token-exchange](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/on-behalf-of-token-exchange.html)
    has the grant mapping. It says MicrosoftOauth2 exchanges via jwt-bearer.
  - [gateway-outbound-auth](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-outbound-auth.html)
    says token exchange is supported on MCP-server and OpenAPI targets.
  - The AWS ML blog *Implement on-behalf-of token exchange for multi-tenant agents
    with AgentCore Gateway* covers Auth0, Keycloak and Okta over RFC 8693, and
    Entra ID over jwt-bearer.

**Requirements Launchpad enforces:**

1. **A CUSTOM_JWT gateway.** The gateway exchanges the inbound token, so an
   IAM-authorized gateway has nothing to exchange (`409 identity.obo_needs_jwt_gateway`).
2. **A CustomOauth2 Connection created with `obo`.** Other vendors get
   `422 identity.obo_vendor_unsupported`. A target on a Connection without an OBO
   config gets `422 identity.obo_unsupported`. Malformed fields get
   `422 identity.obo_invalid`.
3. **An IdP that implements the chosen grant.**
   - A discovery document whose `grant_types_supported` omits the grant URN gets
     `422 identity.obo_unsupported`.
   - An IdP that declares nothing is let through. The exchange then fails at call
     time.

**Issuer consistency hint (non-blocking).** The token the gateway exchanges is the
caller's inbound JWT, which the gateway's `customJWTAuthorizer` accepted. The
Connection's IdP has to trust that issuer as a subject-token issuer. When an
`obo` target is created, Launchpad compares the Connection's issuer (derived as
in §8.1) with the gateway authorizer's issuer (its discovery URL minus the
well-known suffix). When they differ, the `201` response carries

```json
"warnings": [{"code": "identity.obo_issuer_mismatch", "message": "...",
              "detail": {"connection": "...", "connection_issuer": "...",
                         "gateway_issuer": "..."}}]
```

The console shows it above the targets table. It is not a refusal: a
cross-issuer trust (for example Okta configured to accept Cognito-issued subject
tokens) is a legitimate setup. An issuer that cannot be derived on either side
yields no warning. `warnings` is always present, and empty when nothing applies.

**Cognito verdict: not supported, refused up front.**

- Amazon Cognito user pools' `/oauth2/token` endpoint accepts
  `authorization_code`, `refresh_token` and `client_credentials` only. Its
  discovery document declares no `grant_types_supported`, so Launchpad refuses it
  by host (`cognito-idp.*` / `*.amazoncognito.com`) with a 422 that names the IdPs
  that do support it.
- AWS's own [sample-cognito-oauth2-token-exchange](https://github.com/aws-samples/sample-cognito-oauth2-token-exchange)
  emulates RFC 8693 with a proxy in front of Cognito, which confirms Cognito has
  no native support.
- **Live check** (e2e step 6): a `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`
  POST to the throwaway pool's token endpoint is answered `400 unsupported_grant_type`.
- The console states the limit on the Cognito template instead of offering the
  checkbox.

**Tested vs untested.**

- **Tested live, at the AWS-shape level:**
  - creating an OBO Connection against a placeholder IdP, and reading its config
    back via `GetOauth2CredentialProvider`;
  - creating an `obo` target on a CUSTOM_JWT gateway (live `grantType: TOKEN_EXCHANGE`);
  - both 422 refusals.
- **Untested:** the successful exchange itself. It needs a real RFC 8693 / 7523
  IdP (Okta, Auth0, Keycloak, Entra ID), and none was available.

### 8.4 `Update*CredentialProvider`

**Verdict: the operations exist.** botocore 1.43.103 has
`UpdateOauth2CredentialProvider`, `UpdateApiKeyCredentialProvider` and
`UpdatePaymentCredentialProvider`. The OAuth2 update requires `name`,
`credentialProviderVendor` and a full `oauth2ProviderConfigInput`: it is a
replace, not a patch. P3 does not expose rotation or editing OBO on an existing
Connection. To change the OBO config, recreate the Connection, and keep the
target's binding in mind.

### 8.5 Tool-level Cedar policy

Every Connection-bound row in the Gateway targets list has a **Policy** action. It
opens the upstream governance editor
(`/v2/governance?view=policy&gateway=<id>&target=<name>`), prefilled once:

- name `allow_<target>`;
- the tool actions of that target (the gateway's discovered actions filtered by
  target name);
- an allowlist `permit` statement.

Everything after that is the normal governance flow: a new policy starts
`LOG_ONLY`, and promotion and enforcement are unchanged. When the gateway has not
yet discovered the target's tools, the editor says so instead of producing an empty
policy.

In Chat, a Managed Harness call that an enforced policy denies for the signed-in
user shows a **policy-deny card** next to the as_user consent card (tool, reason,
determining policy when named, the console identity, a link to the gateway's
policies — or to the Governance landing page for a card restored from history).
Only a rule with an input condition (`context.input.*`) is denied at call
time; a rule without one hides the tool from `tools/list`. Number inputs are Cedar
`decimal`, so compare them as `context.input.amount.lessThanOrEqual(decimal("500.0"))`
— `<= 500` fails validation.
