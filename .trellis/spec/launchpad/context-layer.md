# COA Context Layer

> One connected COA (Context Ontology Accelerator) deployment per workspace; agents and
> the V2 module read it **as the signed-in member**. Introduced by task
> `10-19-coa-agent-context-tool` (parent `10-19-coa-context-layer`); the module's
> browse pages, playground and attach action by `10-19-coa-console-module`; the shared
> COA demo account by `10-20-coa-demo-account`; the managed COA gateway (gateway mode)
> by `10-20-gw-infra`, its consent by `10-20-gw-consent`, the Harness wiring by
> `10-20-gw-harness` and the Strands wiring by `10-20-gw-strands` (parent
> `10-20-context-layer-gateway-mode`).

## 1. Scope / Trigger

- Touch this spec when you change `services/context_layer*.py`,
  `schemas/context_layer.py`, `templates/context_layer_support.py`,
  `routers/context_layer.py`, the Harness turn override or the Strands gateway gate in
  `services/invoke.py`, the zip deploy expansion, `templates/context_gateway_tools.py.tmpl`
  or the feature lines of `strands_agent/main.py.tmpl`, `services/context_layer_demo.py`
  (the demo account),
  `services/context_layer_gateway.py` (the managed COA gateway), or
  `/v2/context-layer` (`v2/pages/ContextLayer.tsx` + `v2/pages/contextLayer/`), the
  wizard's `agents/ContextLayerSection.tsx` (+ `lib/agent-spec.ts` context glue), or
  the Chat `AuthCard`'s demo-account offer.
- Cross-layer: a new ledger table, a new spec field, a token-custody exception
  (`docs/identity.md` §7.7), a new SSE ask shape reused from as_user.

## 2. Signatures

```
ledger context_layers(id, workspace_id UNIQUE via uq_context_layers_workspace, prefix, env,
  coa_region, resources JSON, connection, console_workload, created_by, created_by_name, …)
AgentSpec.context_layer: ContextLayerRef | None   # {namespace_id: UUID, namespace_name="",
                                                   #  via: "direct" | "gateway" = "direct"}
                                                   # exclude_if None → old specs keep their shape;
                                                   # via exclude_if "direct" → direct refs too
context_layer.discover(ws, prefix, env, region) -> resources
context_layer.connect(db, ws, *, prefix, env, region, connection, return_url, creator,
    demo_account_param=None)                       # None keeps, "" clears
context_layer.deploy_spec(stored_dict, workspace_id) -> AgentSpec   # zip stages only
context_layer_tokens.user_token(db, ws, username, *, console_user, agent_id, session_id,
    record_consent=True) -> Token | AuthNeeded   # never sends customParameters
context_layer_tokens.claims(token) -> dict          # unverified; cache bound + compare only
invoke.harness_turn_overrides(agent, ws, *, gateway_access_token, runtime_user_id,
    console_user, session_id, user_role=None, user_email=None)
    -> {} | {tools, allowedTools} | {auth_required}    # role/email: gateway mode's pool JWT
invoke.invoke_agent_text(..., user_role=None, user_email=None)
invoke.invoke_agent_events(..., user_role=None, user_email=None)
chat.chat_stream(..., user_role=None, user_email=None)   # the Chat route passes them in
                                                          # gateway mode only
invoke._runtime_gateway_gate(agent, ws, *, gateway_access_token, username, role, email,
    console_user, session_id) -> (notice, "") | (None, member_jwt)   # gateway-mode Strands

# module reads (context_layer_api; each returns an allowlisted projection)
context_layer_api.call_as_member(db, ws, username, call(layer, token)) -> T
context_layer_api.{get_namespace, list_sources, list_metrics, ontology}(layer, token, ns)
context_layer_api.system_health(layer, token); context_layer_api.health(db, ws, username)
context_layer_api.namespace_path(ns, *rest)   # refuses a non-UUID (invalid_namespace)
# playground (async generator of SSE text)
context_layer_playground.stream(*, layer, token, workspace_id, username, namespace_id,
    query, session_id, mode, strategy, is_disconnected, heartbeat_s) -> AsyncIterator[str]
context_layer_playground.normalize(frame) -> [{event, data}]   # pure
context_layer_playground.runtime_session_id(ws, user, ns) -> "lp-" + 40 hex
# the shared demo account (context_layer_demo)
DemoAccount(username, password=field(repr=False), sub="")
context_layer_demo.check_scope(prefix, name) -> name          # 422 demo_param_scope
context_layer_demo.read(coa_ctx, name) -> DemoAccount         # 409 demo_param_invalid
context_layer_demo.{require, load}(ws, layer) -> DemoAccount | (None + probe error)
context_layer_demo.public_view(account) -> {available, username}
context_layer_demo.identity_of(token, account) -> "demo" | "own" | None
context_layer_demo.sign_out_url(layer) -> str | None
context_layer_demo.status_fields(db, ws, layer, *, username, authorization, errors)
context_layer_demo.check_choice(ws, layer, account, *, username) -> None  # demo: 404 if none
# the managed COA gateway (context_layer_gateway); ledger key context_layer.GATEWAY_KEY
context_layer.gateway_ref(layer) -> {gateway_id, gateway_arn, gateway_url, target_id} | None
context_layer_support.uses_gateway(spec_dict) -> bool   # raw context_layer.via == "gateway"
context_layer_support.via_gateway(spec_or_dict) -> bool  # the same for an AgentSpec too
context_layer_support.harness_tool_prefix(spec) -> "coa_" | "launchpad_coa_gw_coa___"
context_layer_support.{GATEWAY_ALIAS "launchpad_coa_gw", GATEWAY_SELECTOR "@launchpad_coa_gw",
    GATEWAY_TOOL_PREFIX "launchpad_coa_gw_coa___", VIA_GATEWAY, VIA_DIRECT}
context_layer_support.strands_tool_prefix(spec) -> "" | "coa___"
context_layer_support.{STRANDS_GATEWAY_TOOL_PREFIX "coa___", STRANDS_GATEWAY_TOOLS "coa___*"}
context_layer_support.bind_gateway_url(ref, url) -> ref copy   # ContextLayerRef._gateway_url
context_layer_support.bound_gateway_url(spec) -> str           #   (PrivateAttr; '' = unbound)
context_layer_support.render_context_gateway_source(spec) -> str   # '' unless gateway mode;
                                                                   # ValueError when unbound
strands_agent._feature_line(source, marker, value) -> str   # '' drops the marker's line
context_layer_gateway.runtime_gateway_url(layer) -> str | None   # ledger, https only
context_layer_gateway.require_runtime_gateway_url(layer) -> str  # 409 gateway_unavailable
context_layer_gateway.harness_attachment(layer, resources) -> attachment | None  # no AWS
context_layer_gateway.require_harness_attachment(ws) -> attachment  # 409 gateway_unavailable
context_layer_gateway.gateway_unavailable(reason=None) -> AppError (409)
context_layer_gateway.enable(db, ws, *, username) -> status        # idempotent, resumable
context_layer_gateway.refresh_tools(db, ws, *, username) -> status
context_layer_gateway.disable(db, ws) -> {disabled, gateway_id, grants_removed}
context_layer_gateway.status(db, ws) -> {enabled, state, usable, gateway_name, gateway_id,
    gateway_url, gateway_status, target_status, status_reasons, tools, protocol_versions,
    return_url_ok, trusts_workspace_users, issues, agents, probe_errors}
context_layer_gateway.coa_tools(layer, token) -> [{name, description, inputSchema}]
context_layer_gateway.gateway_grant_id(gateway_id) -> "gw:" + gateway_id   # ≤ 32 chars
context_layer_gateway.gateway_agents(db, workspace_id) -> [{id, name, method}]
context_layer_gateway.preflight(ws, layer, *, username, user_jwt, agent_id, namespace_id,
    session_id="") -> None | {provider, tool, url, scopes, agent_id}   # auth_notice shape
context_layer_gateway.gateway_turn_auth(agent, ws, *, username, role, email=None,
    session_id="", console_user=True) -> None | {"auth_required": notice}
context_layer_gateway.forget_consents(gateway_id=None)   # the 10-min "consented" cache
oauth_sessions.GATEWAY_GRANT_PREFIX = "gw:"; oauth_sessions.is_gateway_grant(agent_id)
context_layer.grant_agent_id(db, ws_id, agent_id, provider) -> "" | "gw:<id>" | agent_id
agentcore.policy.gateway_replace_params(get_gateway_response, **changes) -> UpdateGateway kw
```

Routes (`/api/context-layer`, all in `route_policy`; every non-GET is `FREE`):

| Route | Policy | Note |
|---|---|---|
| `GET /` · `POST /authorize` · `POST /revoke` · `GET /namespaces` | MEMBER | child 2 |
| `PUT /` · `DELETE /` | ADMIN | child 2 |
| `GET /namespaces/{namespace_id}` · `/sources` · `/metrics` · `/ontology` | MEMBER | `namespace_id` = UUID path pattern |
| `GET /health` | MEMBER | runtimes (IAM) + COA `/system-health` (member) |
| `POST /playground` | MEMBER, FREE | SSE; COA spend is not metered |
| `GET /demo-account` | MEMBER | `{available, username, connection}`; ledger + cached SSM only |
| `POST /demo-account/reveal` | MEMBER, FREE | `{username, password}`, `Cache-Control: no-store` |
| `GET /gateway` | MEMBER | the managed gateway, read back from AWS (allow-listed) |
| `PUT /gateway` · `POST /gateway/refresh` · `DELETE /gateway` | ADMIN, FREE | enable / refresh tools / delete |

## 3. Contracts

- **Discovery reads the COA region** (`replace(ws, region=coa_region)`): SSM
  `/<prefix>/{mcp/runtime-arn, serve/runtime-arn, userpool-id, issuer,
  agent-oauth-client-id}`, CFN `<prefix>-<env>-{api,web,auth}` (`ApiEndpoint`,
  `*WebsiteURL*` — CDK prefixes construct outputs —, `CognitoDomainUrl`),
  `GetAgentRuntime` (status only; the response also carries env vars). Vault calls stay
  in the workspace region.
- **Connection**: create `coa-<prefix>` from `DescribeUserPoolClient` (secret used once)
  or adopt by name / an existing `coa-<prefix>`; adoption requires the live provider's
  `customOauth2ProviderConfig.clientId` (or the ledger's) = the agent client id. The
  layer's Connection counts as referenced (`identity_providers._references`).
- **Console workload** `launchpad-console-<workspace>`: created/allow-listed on connect;
  kept on disconnect; swept by `scripts/teardown.py`.
- **Status `callback_registered` is admin-only.** `DescribeUserPoolClient` returns the
  client secret, so a member's `GET /api/context-layer` never calls it; members see `null`
  and no callback row. An admin's answer is cached for 300 s per
  `(workspace, callback URL)` (`_CALLBACK_CACHE`), and connect clears that cache. A failed
  read is never cached.
- **Tokens never leave memory except toward COA** (identity.md §7.7). Cache to
  `exp − 120 s`; a hit is ignored while a revocation is in force; a COA 401 forces consent
  until `authorized_since(…)` the invalidation.
- **Harness**: deploy = prompt addendum (`coa_` tool names), no COA tool. Turn = re-read
  `GetHarness`, keep every deployed tool, append `remote_mcp coa` (MCP invocations URL +
  `Authorization: Bearer`), append `@coa` after any remap (bare `coa_<tool>` selects
  nothing — spike 2026-10-19). No consent ⇒ `{auth_required}` and no InvokeHarness. Never
  routed through a canary gateway.
- **Harness in gateway mode** (`via = "gateway"`, task `10-20-gw-harness`):
  - *Deploy* (`_build_live_params`): no COA tool. The managed gateway goes first in
    `gateway_attachments`, so it claims the alias `launchpad_coa_gw` and a clashing
    Registry attachment gets `_2`. A Registry attachment of the same gateway ARN is
    dropped, so it is deployed once. Its keys are `registry_console._gateway_attachment`'s.
    Outbound is the workspace M2M provider, `CLIENT_CREDENTIALS`, scope
    `launchpad-gw/invoke` (what `_gateway_auth` maps for a user-trusting gateway), read
    from the ledger ref + `resources.oauth_provider_arn` only, with no AWS read.
  - `allowedTools`: `@launchpad_coa_gw` is derived, and appended to an explicit list.
  - Addendum prefix: `launchpad_coa_gw_coa___`. `strip_generated_prompt` strips the
    addendum of either prefix, since the mode may have changed since the deploy.
  - The per-agent role gets `AgentCoreWorkloadIdentity`
    (`agent_iam._uses_gateway`, Harness only).
  - `_pinned_params` (architect approvals) is untouched.
  - *Refusals*: no gateway ref ⇒ 409 `context_layer.gateway_unavailable`, at create /
    redeploy (`routers/agents._validate_tool_auth`, ledger only) and again at the
    generate stage (`require_harness_attachment`, before any Registry read).
  - *Turn* (`invoke._gateway_turn_overrides`):
    1. `gateway_turn_auth` first. A missing role counts as no console user
       (`console_user and bool(role)`), so an unthreaded caller never reconciles a
       Cognito group with `""`.
    2. The token is the router's `gateway_access_token`, else
       `policy_identity.gateway_user_token(…, min_valid_s=CONSENT_JWT_MIN_VALID_S)`, a
       cache hit after the gate.
    3. `harness_user_overrides(agent, ws, token)`. A gateway-mode spec always reads
       `GetHarness` back. `_user_trusting_gateways` re-reads the gateway and swaps it
       for `remote_mcp launchpad_coa_gw` with `Authorization: Bearer <pool JWT>`.
       `@launchpad_coa_gw` is re-appended after an explicit list's remap.
    4. Fail closed with `context_layer.gateway_unavailable`, never M2M toward COA's
       gateway: the alias is missing from the deployed tools (re-publish), or it is
       not a `remote_mcp` carrying exactly this turn's `Bearer <pool JWT>` (gateway
       unreadable, gone or no longer trusting, or another server bearing the alias).
  - The Chat route (`gateway_support.runtime_user_id` names the member for any
    gateway-mode spec) mints with `min_valid_s=CONSENT_JWT_MIN_VALID_S`, so one JWT
    serves the preflight, the turn and the completion (F8). It passes `user_role` /
    `user_email`, and so does `POST /api/agents/{id}/invoke`. `/v1` and evaluation stay
    refused (`as_user_requires_console`, `unsupported_method`).
  - No COA token is fetched by the backend for these agents.
- **Strands in gateway mode** (`via = "gateway"` on HTTP `zip_runtime`, task
  `10-20-gw-strands`):
  - *Deploy* (`context_layer.expanded_spec`): no tool at all, so no `IDENTITY_AUTH_TOOLS`
    entry, no `gates_turn`, no IAM statement, no environment entry and no return URL
    (policy document and environment equal those of the same spec without a layer).
    `require_runtime_gateway_url(layer)` (ledger `gateway_url`, https only, else 409
    `context_layer.gateway_unavailable`) is bound to a copy of the ref
    (`bind_gateway_url`, a pydantic `PrivateAttr`: never serialized, never
    client-settable). Addendum prefix `coa___`. Every zip stage builds from this
    expansion, so each refuses without the gateway; create / redeploy refuse first
    (`_validate_tool_auth`, ledger only).
  - *Template*: `render_context_gateway_source` inlines `context_gateway_tools.py.tmpl`
    with `CONTEXT_GATEWAY = {'url': <gateway URL>, 'tools': 'coa___*'}` (repr) through two
    feature lines of `main.py.tmpl` (`__LAUNCHPAD_CONTEXT_GATEWAY_SOURCE__` after the
    launchpad-gw block, `+ __LAUNCHPAD_CONTEXT_GATEWAY_TOOLS_FN__(stack,
    gateway_access_token)` in `_turn_events`), processed before any spec text enters the
    source. Without gateway mode both lines are dropped, so every other spec renders
    byte-identically to the template before them. A gateway-mode spec that was not
    expanded raises `ValueError` (never deployed without COA or in direct mode).
  - *Runtime*: `context_gateway_tools(stack, user_token)` per turn: no token ⇒ `[]`, no MCP
    client (no M2M fallback); else `MCPClient(streamablehttp_client(url, headers=
    {Authorization: Bearer <token>}, httpx_client_factory=<follow_redirects=False,
    Timeout(30, read=300)>))`, teardown on the turn's stack, tools filtered by
    `fnmatchcase(name, "coa___*")`. Fail-soft: module scope is one literal + functions.
    It sits next to launchpad-gw: both receive the payload's `gateway_access_token`.
  - *Turn* (`invoke._runtime_gateway_gate`, at the top of `invoke_agent_text` and
    `invoke_agent_events`, before the JWT-inbound / SigV4 / canary branches):
    `gateway_turn_auth` with `console_user and bool(role)`; a notice ⇒ text path
    `{text: "", session_id, auth_required: [notice]}`, stream path one `auth_required`
    event (then Chat's `done`); the runtime is never invoked. Else the payload's
    `gateway_access_token` = the router's JWT or `gateway_user_token(…,
    min_valid_s=CONSENT_JWT_MIN_VALID_S)` (a cache hit); none ⇒ 409
    `gateway_unavailable`. `chat._chat_events` passes `user_role` / `user_email` to
    `invoke_agent_events` only when set; the buffered fallback passes them on.
  - `runtimeUserId` is still sent (`gateway_support.runtime_user_id`: the Chat route
    mints the JWT because it names the member). `as_user_connections_stored` does not
    count a gateway-mode ref, so no `force_reauth_providers` reach the runtime.
  - *Code view*: `main.py` from the deployed package shows `CONTEXT_GATEWAY`.
- **Strands**: every zip stage builds from `deploy_spec` (one `as_user` MCP tool `coa`
  + addendum without prefix); the stored spec keeps only the ref. The baked `coa` entry
  carries `gates_turn: true` (`identity_support.gates_turn`: layer attached + reserved name
  + mcp + as_user). Generated runtime: consent pending ⇒ `auth_required` + `complete ""`,
  the model and memory recall never run (`identity_turn_gated`, reset every turn). Before
  this (2026-10-20) the model ran without COA tools and the addendum made it answer "no
  access, ask an admin" next to the card. Other as_user MCP servers stay ungated.
  `gateway_support.runtime_user_id` and `as_user_connections_stored(spec, conn)` count a
  direct ref for non-Harness specs; `runtime_user_id` counts any gateway-mode ref (Harness
  or Strands), `as_user_connections_stored` none.
- **Who is the user**: `runtime_user_id` = the signed-in console username (Chat, console
  invoke). None or `console_user=False` ⇒ `identity.as_user_requires_console` before any
  vault call.
- **Chat card**: `auth_required {provider, tool: "context_layer", url, scopes, agent_id}`;
  `grants/{conn}/status?agent_id=<harness>` reads the console grant
  (`context_layer.grant_agent_id`); for a gateway-mode agent (any method) it reads
  `gw:<gateway_id>`, and without a managed gateway the agent's own id. `customState {kind: "context_layer"}` sends
  `/auth/return` back to `/v2/context-layer`. The playground's ask has `agent_id: ""`;
  `AuthCard` polls such a console grant (`isConsoleGrant`) instead of skipping it.
- **Module reads** are projections, never passthrough (`schemas/context_layer.py`):
  every field optional, unknown statuses verbatim, free text bounded, camelCase and
  snake_case both read (the ontology engine answers `ontology_id`), epoch-second
  times (the sources service) turned into RFC 3339. Lists follow `nextToken`
  (`MAX_PAGES` 20): namespaces under `namespaces`, sources under **`items`**, metrics
  under `metrics`. The ontology tab is `GET …/ontologies` + one
  `graph/ontology-overview?ontology_id=<IRI>` per ontology (the IRI carries `#`, so it
  rides the query): induced first, at most `MAX_OVERVIEWS` 8, four at a time, 200
  classes each; an overview that answers 5xx/404 goes to `failed`, a 401/403 fails
  the read. An error's `detail.coa_message` is at most 200 characters of COA's own
  `message`, never the body.
- **Health**: `GetAgentRuntime` per runtime (COA region, IAM) always; COA
  `/system-health` only when the ledger says the member is authorized — otherwise
  `authorization_required: true` and no vault or COA call. A COA refusal is
  `coa_error {code, status}` in a 200, not a failed request.
- **Playground SSE** (`services/context_layer_playground.py`):
  - The token is fetched in the handler, before the stream: not connected or a vault
    failure is an ordinary JSON error. No consent ⇒ the stream is one
    `auth_required` event.
  - Upstream: `httpx.AsyncClient` stream to `layer.serve_url`, headers `Authorization:
    Bearer`, `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` = `runtime_session_id`
    (stable per workspace + member + namespace), body `{query, namespace, stream: true,
    sessionId?, options: {mode?, strategy?}}` — never `options.includeDebugInfo`.
    Read timeout 180 s, total 185 s (COA's budget is 170 s).
  - Event map: COA `step` → `step {name (stepName), status, duration_ms, tool,
    detail ≤ 300}`; `token` → `token {text}`; `done` → `done {answer
    (synthesizedAnswer), tier, confidence (score), rationale, columns, rows ≤ 200,
    row_count, rows_truncated, partial, guardrail_blocked, data_sources,
    citations ≤ 10, total_ms, session_id (frame sessionId, only when it matches
    `SESSION_ID_RE`, the request's own pattern, so it round-trips)}` — never `queryUsed` /
    `sparqlGenerated` (COA's debug fields expose internal schema; the test asserts the
    SQL in a recorded frame does not survive); `error` → `error {code,
    message ≤ 200, status, coa_error}`. COA also answers a **bare** `{error, message,
    statusCode}` before streaming (a namespace grant denied) and a non-streamed
    `{result, sessionId}`; both are understood. A non-200 runtime answer is one
    `error` event naming the status. The stream ends after the first `done`/`error`;
    a stream that ends without either is `upstream`.
  - Error codes from a status + COA error type: 401 → `reauthorize` (and
    `tokens.invalidate`), 403 → `forbidden`, `NoResult` → `no_result`, 404 →
    `coa_not_found`, other 4xx except 429 → `coa_rejected`, else `upstream`.
  - Lifetime: `: keep-alive` every 15 s of COA silence (the pending read is never
    cancelled by a heartbeat); `request.is_disconnected()` is checked per frame and
    per heartbeat; `contextlib.aclosing` + the `async with` close COA's connection
    on disconnect or generator close.
- **Demo account** (`services/context_layer_demo.py`, task `10-20-coa-demo-account`):
  - **Storage.** An SSM SecureString `/<prefix>/launchpad/<name>` in the COA region,
    `{username, password, sub}`, written by the operator script
    `samples/coa-demo/demo_account.py` (the backend never creates or changes COA
    users). The row keeps only `resources.demo_account_param`. Connect checks the
    scope first (422, no AWS call), then `GetParameter(WithDecryption)` → SecureString
    with non-empty string `username` + `password` (409 `demo_param_invalid`,
    `detail.problem` ∈ `not_found | not_secure_string | bad_shape`, never the value,
    no chained exception), all before any write. `None` keeps the row's parameter
    (dropped when outside a new prefix), `""` clears it.
  - **Cache.** `(workspace, parameter)` for 300 s; a failure is never cached and is
    a `demo_account` probe error; connect and disconnect clear it.
  - **Status.** `demo_account {available, username}` and `sign_out_url` for everyone;
    `demo_account_param` for admins. For an authorized caller while an account
    exists: `authorization.identity` = `identity_of(token)` (`sub` vs the stored
    `sub`; with no stored `sub` only a username match is conclusive), from the
    broker's cache or one vault read with `record_consent=False` (never flips a grant
    to pending; while a re-consent is owed — revocation, COA 401 — no vault call at
    all, since that ask would carry `forceAuthentication`). A vault failure is an
    `identity` probe error and `identity: null`. No demo account ⇒ no vault call.
  - **Sign-out URL.** `{hosted_ui_domain}/logout?client_id=<web client>&logout_uri=
    <console_url without trailing slash + "/">` (urlencoded). The web client id is
    the optional SSM `/<prefix>/userpool-client-id`; without it the URL is null.
  - **Consent.** `POST /authorize {account}`: `demo` needs an account (404
    `demo_unavailable` before any vault call) and is logged; `own` and omitted are
    accepted as they are. **The consent request is identical for every value and
    never carries `customParameters`** (the vault keys tokens by them — see the
    operational notes). The username is NOT prefilled; the panel's copy buttons are
    the way. A COA browser session consents silently as its user, so switching is
    revoke → COA sign-out → authorize, guided by the console; the console keeps the
    choice to detect a consent that resolved to the other account.
  - **Custody.** The password is in the reveal response only (logged as "<member>
    revealed the COA demo account", never the value). `DemoAccount.password` is
    `repr=False`; the tests grep caplog, status, GET bodies and error details.
- **Managed COA gateway** (`services/context_layer_gateway.py`, task `10-20-gw-infra`;
  live facts F1–F11 in `10-20-context-layer-gateway-spike/research/findings.md`):
  - One per workspace, name `launchpad-coa-gw` (workspace = account × region, so the
    name is unique); `launchpad-gw` is never touched. Admin-only writes.
  - **Enable order** (each step reuses what exists; a second call writes nothing):
    prerequisites (`gateway_role_arn`, `user_pool_id`, `user_pool_client_id`,
    `m2m_client_id`, `oauth_provider_arn` — else 409 `gateway_prerequisites`, no AWS
    call) → COA `tools/list` via `call_as_member` (no consent ⇒ the existing 409
    `authorization_required`, nothing created) → the Connection's ARN → the gateway →
    **ledger write** (as soon as it exists: right after `CreateGateway` or the lookup,
    before any wait, so a gateway that then fails is still the card's to show and
    delete) → the gateway workload identity's return URL → the target → ledger write
    with `target_id`.
  - **Gateway shape** (create): `protocolType MCP`, `protocolConfiguration.mcp.
    supportedVersions ["2025-11-25","2025-03-26"]`, `CUSTOM_JWT {discoveryUrl:
    workspace pool, allowedClients: [console, m2m]}` (no audience, no interceptor),
    `roleArn = gateway_role_arn`, `exceptionLevel DEBUG`. Found by ledger id, then by
    name; drift (versions, clients, discovery, role, audience, types) is reconciled
    with `gateway_replace_params` (replace semantics: a policy engine, interceptors
    etc. are echoed); one AWS left `FAILED` / `UPDATE_UNSUCCESSFUL` gets the same
    update re-applied (the card's *Repair*). A `DELETING` one is waited out, then
    re-created.
  - **Target `coa`**: `mcp.mcpServer {endpoint: layer.mcp_url, mcpToolSchema.
    inlinePayload: json {"tools": [{name, description, inputSchema}]}}` (COA's own
    entries, nothing else), `OAUTH {providerArn, grantType AUTHORIZATION_CODE,
    defaultReturnUrl, scopes SCOPES}`, **never `customParameters`**. Updated only when
    endpoint, schema, provider, grant, return URL or scopes differ, or when it settled
    anywhere but READY (a re-enable re-applies it; one still settling is waited for).
    READY only;
    `*_PENDING_AUTH`, `FAILED`, `*_UNSUCCESSFUL` ⇒ 502 `gateway_failed {step: target,
    status, reasons}`. A create `ConflictException` adopts the winner's target.
  - **Workload identity** = the last segment of `workloadIdentityDetails.
    workloadIdentityArn` (the gateway id): union-add the return URL, never create it.
  - **COA `tools/list`**: `initialize` (2025-03-26) → `notifications/initialized` →
    `tools/list` pages (`nextCursor`, ≤ 10 pages, ≤ 100 tools), carrying `Mcp-Session-Id`
    and `MCP-Protocol-Version`, JSON or SSE answers, no redirects. 401 ⇒
    `CoaUnauthorized` (→ `reauthorize`), 403 ⇒ `forbidden`, other ≥ 300 / JSON-RPC error /
    no tools ⇒ `upstream`.
  - **Ledger**: `resources["gateway"]` identifiers only, written with a Core UPDATE that
    pins `updated_at` (the row's time is the connect time). `connect` keeps the key;
    `disconnect` refuses 409 `gateway_enabled` while it exists. `GET /api/context-layer`
    adds `gateway {enabled, gateway_id}` (ledger only, no AWS call).
  - **Status** (`GET /gateway`): `state` = `disabled | ready | creating | failed |
    missing | unknown` (gateway lifecycle, then the target's while the gateway is READY;
    `DELETING` and not-found read `missing`; an unreadable gateway `unknown` +
    `probe_errors`). `issues` ⊆ `protocol, authorizer, endpoint, connection,
    return_url, tools`; `usable` = `ready` and no issue. `connection` compares the
    target provider ARN's last segment with the layer's Connection (no AWS call).
    `agents` = live agents with `uses_gateway(spec)`, masked for hidden ones.
  - **Disable**: refused 409 `gateway_referenced {referenced_by}` before any AWS call
    while an agent uses gateway mode; else delete every target, wait for the list to
    drain, `DeleteGateway` (retried on `ConflictException`/`ValidationException`; a
    gateway AWS already lost is skipped), clear the key, delete `user_grants` and
    `oauth_pending_sessions` rows with `agent_id = gateway_grant_id(id)` (the console
    grant `""` stays) and `forget_consents(id)`. Without a ledger ref, a gateway found
    by name is deleted; none ⇒ 409 `gateway_not_enabled`.
  - One operation at a time per workspace (`_lock`). Teardown sweeps the gateway by
    name. Gateway consents are not revocable on the platform (F10) — the card says so.
  - **IAM**: the gateway role's `IdentityVault` statement carries
    `GetWorkloadAccessTokenForJWT` (CDK `base_stack.py` and `workspace_iam.
    gateway_role_policy`).
  - **Grant key** `gw:<gateway_id>`: the id is `launchpad-coa-gw-<10>`, so the key is 30
    characters of `user_grants.agent_id` / `oauth_pending_sessions.agent_id`'s
    `String(32)` (child 1's `gateway:` prefix made ~35; no row was ever written with it).
  - **Consent** (task `10-20-gw-consent`; called by the chat paths in children 3/4):
    - `gateway_turn_auth`: not gateway mode ⇒ None (nothing minted, no HTTP).
      `console_user=False` or no username ⇒ `identity.as_user_requires_console`
      (detail `{provider, tool, agent_id}`) before any call; no ledger ref ⇒ 409
      `gateway_not_enabled`; then `policy_identity.gateway_user_token(ws, username,
      role, email, min_valid_s=CONSENT_JWT_MIN_VALID_S)` (`SESSION_TTL_S` + 60: a cached
      token closer to expiry is re-minted, so the one the gateway sees is still cached
      when the consent completes), and None (console sign-in off) ⇒
      `as_user_requires_console` too.
      Then `preflight` on the spec's `namespace_id`. No COA token is fetched.
    - `preflight`: the ledger URL must be https (else `gateway_not_enabled`, the JWT
      never sent). A cached (gateway, user) ⇒ None, no HTTP. Else `_Mcp` with the
      member's bearer: `initialize {protocolVersion 2025-11-25, capabilities
      {elicitation: {url: {}}}}` (the `MCP-Protocol-Version: 2025-11-25` header rides
      every request, `Mcp-Session-Id` after initialize) → `notifications/initialized`
      → `tools/call coa___list_metrics {namespaceId}`, timeout 20 s, no redirects.
    - JSON-RPC `-32042`: the first `mode == "url"` elicitation (its `url` must be https);
      `session_uri` = its decoded `request_uri` query value. `record_pending(provider =
      layer.connection, user_id, agent_id = gw:<id>, tool "context_layer", SCOPES,
      caller_kind user_jwt)` in its own session, and the notice (`auth_notice` shape,
      `agent_id` = the asking agent). Never cached.
    - A `result` (data, or COA's `isError`, e.g. "no grants on namespace") ⇒ None +
      `(gateway_id, username)` cached `CONSENT_TTL_S` 600 s (monotonic `_clock`).
    - Fail open (None, nothing cached or recorded, one WARNING with gateway, user and a
      status / exception class): transport errors, timeouts, any HTTP ≥ 300 (401/403
      included), another JSON-RPC error, a negotiated version other than
      `2025-11-25`, a -32042 without a usable URL / `request_uri`.
    - Completion is the existing `POST /api/identity/oauth/complete`: a `user_jwt` row
      presents `{userToken: gateway_user_token(...)}`, the same per-process cached JWT
      (F8); a restart in between ⇒ `identity.session_token_unavailable` or a vault
      refusal, and the member retries.
    - `oauth_sessions.revoke` excludes `gw:` grants and pending rows (count included);
      `_grant_view` / `grant_status` never report `force_reauth` for a `gw:` key. The
      `UserTokenRevocation` row is still written (direct paths only).
- **Frontend**: `?view=` states in `contextLayer/views.ts` (`contextView` /
  `viewSearch`; a non-UUID id never routes). The status is loaded once by
  `ContextLayer.tsx` and passed to every view. `moduleGate` gates every sub-page:
  no COA call before the member is authorized. `useCoaLoad` keeps the error code
  (`coaErrorKind`: authorize / reauthorize → `AuthorizationControl`, forbidden → a
  COA console link, notFound, other → retry). Deep links (`coaConsoleUrl`, https
  only): `/namespaces/{id}`, `/sources`, `/metrics`, `/ontology/graph`,
  `/playground`. Attach: `attachTargets` (harness or HTTP zip without `code` /
  `code_bundle`, not system, not deploying, `agents.deploy`), links
  `/v2/agents?view=new&method=…&context_ns=&context_name=` or `?view=edit&id=…`;
  the wizard's `initialDraft` / `editDraft` read `contextPrefill(params)` once.
  The nav entry stays expert-only (`hintKey: glossary.contextLayer`).
  - **我的授权** (`MyAuthorization.tsx`, also the sub-page gate): `accountView`
    (`state.ts`) → `choose` (own + demo buttons), `demo` (tag + 切换为我的账号), `own`,
    or `guide` (revoke → `sign_out_url` → authorize toward a target). The explicit
    choice is kept in sessionStorage (`ACCOUNT_INTENT_KEY`); an `identity` other than
    the choice opens the guide with a warning; `choiceSettled` clears it. The guide's
    revoke pins the target in state so it survives the `revoked` read.
  - **Credentials** (`DemoAccount.tsx`): the username from the status, the password
    fetched by reveal only on show/copy, kept in component state only.
  - **Wizard 接入方式** (`agents/ContextLayerSection.tsx`, shown once a namespace is
    chosen): `Segmented` `v2-agent-context-via-direct` / `-gateway`. The gateway option is
    disabled with `viaGatewayDisabled` unless `status.gateway.enabled`, but a stored
    gateway choice stays selected so it can be switched back. Picking a namespace keeps
    the mode (`withContextVia`). The note is `noteGateway` in gateway mode (either
    method; child 3's `gatewayStrandsPending` warning is gone). `ContextLayerChoice.via?` (absent =
    direct); `contextLayerSpec` emits `via: "gateway"` only; `formFromStoredSpec` reads
    it back; an attach prefill on an edit keeps the stored mode. The review row has
    `v2-agent-review-context-via` and the detail card `v2-agent-context-via`.
  - **Gateway card** (`GatewayCard.tsx`, testid `context-layer-gateway`, after the
    deployment card): its own `useLoad` keyed on the ledger `gateway_id`;
    `gatewayActions(gw, isAdmin)` → enable (`-enable`, also *Repair* on a broken or
    drifted gateway), refresh (`-refresh`, ready only), delete (`-delete`, Confirm);
    members none. `gatewaySignal` maps the state (ready + issues ⇒ `act`). A failed
    action shows `CoaErrorNotice` (so a missing admin consent offers *Authorize*); a
    `gateway_referenced` refusal lists the agents. The revocation note is always shown.
  - **Chat** `AuthCard`: `DemoAccountOffer` when `demoOffered(ask, info)` (the ask's
    provider = the layer's Connection); `info` from `loadDemoInfo(workspace)`
    (`demoInfo.ts`, one read per workspace per 5 min, failures not kept). A gateway
    ask has the same shape, so the card, its poll and the offer work unchanged.
  - **我的授权 page** (`v2/pages/MyConnections.tsx`): `isGatewayGrant(g)` (`lib/
    user-grants.ts`, `agent_id` starts with `gw:`) rows read `myConnections.
    contextLayerGateway` linking to `/v2/context-layer` (testid
    `grant-gateway-context-layer`) and carry the tag `myConnections.notRevocable`
    (title = `notRevocableHint`, testid `grant-not-revocable-<conn>-<agent_id>`)
    instead of the revoke link; the revoke dialog's count leaves them out.

## 4. Validation & Error Matrix

| Condition | Result |
|---|---|
| method ∉ {harness, zip_runtime}, a2a, code/code_bundle, tool named `coa` | 422, error type `context_layer.unsupported_method` |
| create/redeploy with no layer connected | 409 `context_layer.not_connected` |
| `POST /api/agents/{id}/invoke` before the caller consented (no card there) | 409 `context_layer.authorization_required` |
| eval replay / conversion of a COA agent | 422 `context_layer.unsupported_method` |
| canary / experiment capability | `reason_code: "context-layer"` |
| disconnect while agents attach it | 409 `context_layer.referenced` |
| COA 401 on a module read | 409 `context_layer.reauthorize` (**not 401**: the console treats 401 as session end) |
| COA 403 / 404 / 429 or 5xx / timeout | 403 `forbidden` / 404 `coa_not_found` / 502 / 504 `upstream` |
| module read with a non-UUID namespace id | 422 (path pattern), COA never called |
| playground before consent | 200 SSE, one `auth_required` event |
| playground: COA 401 · 403 · NoResult · 404 · other 4xx · 5xx | `error` event `reauthorize` · `forbidden` · `no_result` · `coa_not_found` · `coa_rejected` · `upstream` |
| connect: demo parameter outside `/<prefix>/launchpad/` (or `..`, empty segment) | 422 `context_layer.demo_param_scope`, no AWS call |
| connect: demo parameter missing / not SecureString / bad JSON | 409 `context_layer.demo_param_invalid` (`detail.problem`), nothing written |
| connect: demo parameter unreadable (AccessDenied, …) | 502 `context_layer.discovery_failed` (`source: demo account parameter`) |
| reveal or `authorize {account: "demo"}` with no demo account | 404 `context_layer.demo_unavailable` |
| status: demo parameter unreadable | `demo_account.available: false` + `demo_account` probe error |
| gateway enable / refresh before the admin consented | 409 `context_layer.authorization_required`, nothing created |
| gateway enable on a workspace without the bootstrap resources | 409 `context_layer.gateway_prerequisites` (`detail.missing`), no AWS call |
| gateway AWS step failed / target pending-auth / timeout | 502 `context_layer.gateway_failed {step, code \| status + reasons}` (504 on a wait timeout) |
| gateway refresh / delete with no gateway | 409 `context_layer.gateway_not_enabled` |
| gateway delete while an agent uses gateway mode | 409 `context_layer.gateway_referenced` (`referenced_by`, masked) |
| disconnect while the managed gateway exists | 409 `context_layer.gateway_enabled` |
| gateway-mode turn: `/v1`, no username, or the console sign-in off (no pool JWT) | 409 `identity.as_user_requires_console`, no gateway call, nothing recorded |
| gateway-mode turn with no managed gateway (or a non-https ledger URL) | 409 `context_layer.gateway_not_enabled`, the JWT never sent |
| gateway preflight answers -32042 | `{auth_required: notice}`, a `user_jwt` pending row + `pending` grant on `gw:<id>` |
| gateway preflight answers a result (data or COA `isError`) | None, consent cached 10 min |
| gateway preflight: transport / HTTP / other JSON-RPC error / version ≠ 2025-11-25 | None (fail open), WARNING, nothing cached |
| revoke (`DELETE /api/identity/grants/{conn}`, `POST /api/context-layer/revoke`) | `gw:` grants and pending rows untouched, not counted, never `force_reauth` |
| `context_layer.via` other than `direct` / `gateway` | 422 (literal) |
| create / redeploy / Harness generate / any Strands zip stage with `via: "gateway"` and no managed gateway (Strands: or a non-https ledger URL) | 409 `context_layer.gateway_unavailable`, nothing deployed |
| gateway-mode Strands turn before the member consented | text: `{text: "", auth_required: [notice]}`; stream: one `auth_required` event; the runtime is not invoked |
| gateway-mode Strands turn: `/v1`, no username, or no role passed | 409 `identity.as_user_requires_console`, no gateway call, no runtime call |
| a gateway-mode Strands spec rendered without its deploy expansion | `ValueError` (wiring bug; never a deployed agent) |
| gateway-mode Harness turn: no role passed with the username | 409 `identity.as_user_requires_console`, no gateway call |
| gateway-mode Harness turn: deployed config lacks `launchpad_coa_gw`, or it could not be swapped | 409 `context_layer.gateway_unavailable`, no InvokeHarness |

## 5. Tests Required

`tests/test_context_layer.py` (discovery, refusals, create vs adopt, workload ensure,
status, disconnect), `tests/test_context_layer_tokens.py` (token/consent/revocation/cache/
401 force/`/v1`/no token in caplog, routes), `tests/test_agent_spec_context_layer.py`,
`tests/test_invoke_context_layer.py` (override, short-circuit sync+stream, byte-identical
non-COA requests, Strands expansion feeding template/IAM/env/return URL, the rendered
Strands runtime ending a no-consent turn at the card while a plain as_user MCP tool does not,
refusals),
`tests/test_context_layer_api.py` (each read: projection, paging, 401/403/404/429/5xx,
consent first, not connected, non-UUID, a stored non-https endpoint never sent the
bearer; ontology fan-out + failed + truncated; health),
`tests/test_context_layer_playground.py` (normalization from recorded frames, error
codes, the route's request shape with no `includeDebugInfo` and no SQL in `done`,
a `sessionId` the console could not send back is not echoed, a non-streamed body read
bounded, `auth_required`, 401 forcing consent, runtime refusals, early close and
client disconnect closing the upstream, keep-alive),
`tests/test_context_layer_demo.py` (scope guard, parameter problems with no value
and nothing written, decrypt in the COA region, keep/clear/replace on re-connect,
cache + failure not cached + cleared on connect/disconnect, no password in repr /
status / GET / caplog / error, identity cases, sign-out URL, no vault read without
an account, the identity read never flips a grant, reveal `no-store` + 404 + 409,
authorize never sends `customParameters` for any `account`, the identity read never
forces authentication and a vault failure degrades to `identity: null`, a member
reveals but cannot connect),
`tests/test_context_layer_gateway.py` (exact create shapes, the tools/list handshake
as the admin, idempotent re-enable, consent / prerequisites refusals before any write,
resume after a target failure, a gateway left FAILED still in the ledger and deletable,
a target left FAILED re-applied, adopt + reconcile by name with the policy engine echoed,
return-URL union, pending-auth failure, no token in ledger / logs, COA 401 / 403 / 5xx /
JSON-RPC error / no tools, refresh only on change, status drift / missing / unknown /
agents, ledger summary without AWS, disable refused then ordered with grant cleanup,
reconnect keeps the key, `updated_at` pinned, routes, classification, member 403s),
`tests/test_context_layer_gateway_consent.py` (the grant key's width, the -32042 ask →
`user_jwt` pending row on `gw:<id>` + the `auth_notice`-shaped card with no vault call,
the MCP 2025-11-25 handshake with the member's JWT and session id, never caching an ask,
SSE answers, data / COA `isError` ⇒ None + cached per member + TTL expiry, every
fail-open case uncached, no JWT / `request_uri` in logs, direct agents ungated, no console
user / sign-in off / no gateway / non-https URL refused before any call,
`grant_agent_id` for gateway / direct × Harness / Strands, the status route, revoke and
both revoke routes skipping `gw:` rows, completion with the cached JWT, F8 end to end
through `policy_identity`'s real cache with a signed-in member, a mismatched member
refused, disable dropping rows and the cache),
`tests/test_context_layer_gateway_harness.py` (`via` stored only for gateway, golden
direct CreateHarness params with a gateway enabled, the attachment = the Registry's
mapping of the live gateway, explicit `allowedTools`, the alias claimed first, a
Registry duplicate of it dropped, refused before any Registry read, both addenda
stripped, generate refused without the gateway / for Strands, the role's grant,
the code view, the create route + `gateway_agents`, `runtime_user_id`, the card with no
vault call sync + stream, the swap with one cached JWT, a router token riding it, `/v1`
/ no role / no user refused before any call, fail-closed without the attachment,
without trust or for a server only bearing the alias, direct unchanged, the Chat and console invoke routes),
`tests/test_context_layer_gateway_strands.py` (no `coa` tool / identity block, the baked
`CONTEXT_GATEWAY` + call line, the URL never serialized nor client-settable, the `coa___`
prompt, IAM + env equal to a layer-less spec, golden direct expansion + `IDENTITY_AUTH_TOOLS`
+ main.py with or without the gateway and equal to the template without the feature
lines, an unexpanded gateway spec refused, generate with the gateway, generate refused
without it or for a non-https URL, the create route, the rendered runtime: member bearer +
no redirects + `coa___*` filter + teardown, no token ⇒ no client, a failed start, both
gateway clients get the payload token; the card sync + stream with no runtime call and no
vault call, the payload's JWT (gate-minted or the router's), `/v1` / no role / no user,
the gateway gone, direct unchanged, no revocation forwarded, the Chat and console invoke
routes, the code view),
`frontend/src/lib/agent-spec.test.ts` (`via` glue: emitted for gateway only, read back,
`withContextVia`),
`frontend/src/v2/pages/contextLayer/{state,views,playgroundState,attach,demoAccount,gateway}.test.ts`,
`frontend/src/lib/chat.test.ts` (`sseEvents` skips keep-alive comments, Chat's frames
unchanged) and `frontend/src/lib/user-grants.test.ts` (`canPollGrant`: the console
grant polls without an agent, a Chat ask only with its agent; `isGatewayGrant`).
Fakes live in `tests/coa_fakes.py` (`Account` for AWS, `CoaHttp` + `install_http`
for COA REST and Serve behind `httpx.MockTransport`).

## 6. Wrong vs Correct

#### Wrong
```python
tools = hc.user_authenticated_tools(spec, resources, "")  # rebuilt from the spec: drops
tools.append(coa)                                          # inline functions, signs "Bearer "
```
#### Correct
```python
detail = hc.get_harness(control, agent.resource_id)        # the deployed config, verbatim
tools = [*map(dict, detail["tools"]), coa]                  # + the swap only with a pool JWT
```

#### Wrong
```python
for line in httpx.Client().stream("POST", serve_url, ...).iter_lines():  # sync, in a
    yield line                                                            # threadpool: a
                                                                          # disconnect waits
                                                                          # for the next read
```
#### Correct
```python
async with httpx.AsyncClient(...) as client, client.stream("POST", ...) as response:
    async with contextlib.aclosing(_with_heartbeat(_frames(response), 15)) as frames:
        async for frame in frames:
            if await is_disconnected(): return         # closes COA's connection now
```

#### Wrong
```
            GATEWAY_TOOLS(stack, gateway_access_token)
            + CONTEXT_GATEWAY_TOOLS(stack, gateway_access_token)   # always present, a no-op
            + IDENTITY_TOOLS(stack, session_id, force_reauth),     # lambda for direct specs:
                                                                   # every agent's main.py changes
```
#### Correct
```
            GATEWAY_TOOLS(stack, gateway_access_token)
            + __LAUNCHPAD_CONTEXT_GATEWAY_TOOLS_FN__(stack, gateway_access_token)
            + IDENTITY_TOOLS(stack, session_id, force_reauth),
# render_main_py: _feature_line(source, marker, "context_gateway_tools" if gateway else "")
# drops the whole line for every other spec, so their output is byte-identical
```

## Operational notes (live, 2026-10-19)

- **The token vault refreshes `USER_FEDERATION` tokens.** A COA access token lives 60 min. A
  Harness turn and a Strands turn 61 min after consent both worked with no re-consent. Do not
  build a refresh of your own.
- **The vault binding is keyed by (workload identity, userId) only.** A consent completed on a
  throwaway stack that shares the console workload (`launchpad-console-<ws>`) and the vault user
  (`admin`) leaves the vault bound, while the real ledger shows `none`: `POST /authorize` then
  answers `authorized: true`, but nothing writes a grant row. To realign the ledger after such a
  rehearsal, use revoke (`forceAuthentication`) followed by a fresh consent on the real stack.
- **COA's agent-client callbacks are fixed in COA's deploy** (`SCL_AGENT_OAUTH_CALLBACK_URLS`, fork
  PR #7). An environment adopts its existing Connection by name, and that Connection's callback
  must already be on the list (dev `coa-w2`, prod `coa-w2-dev-user`). A new Connection means a
  COA redeploy of `auth`.
- **The Harness tool selector is `@coa`.** Bare `coa_<tool>` matches nothing. The model sees the
  tools as `coa_<tool>`.
- **The token vault keys a token by the consent's `customParameters`** (live, 2026-10-20). After a
  demo consent made with `{login_hint, prompt}`, `GetResourceOauth2Token` *without* parameters still
  returned the member's earlier token, *with* the same parameters the demo token, and with
  `{prompt}` alone an authorization URL. The broker, the Harness override, the playground and every
  Strands runtime (the `identity_tools` template) fetch without parameters, so a consent made with
  any lands in a slot nobody reads. Never send `customParameters` on a COA consent: no
  `login_hint` (the username is NOT prefilled; the demo panel's copy buttons are the way), no
  `prompt`.

## Demo account: operational notes (live, 2026-10-20)

- **The reveal audit line is a WARNING on purpose.** Nothing configures app logging, so app
  `logger.info` never reaches the service journal, on dev or on prod. The
  `"<user> revealed the COA demo account"` line therefore logs at WARNING; a test pins the level.
- **A new prod member needs a workspace grant before any `/api` call works.** Approve with
  `PATCH /api/users/{id} {status: "active", workspaces: ["default"]}`. Without it every call
  answers 403 `workspace.forbidden`.
- **Repair / rotate the shared account** with `samples/coa-demo/demo_account.py ensure|rotate`.
  - The grant names the group `launchpad-demo`, so a re-created user keeps access.
  - `rotate` without `--signout` leaves existing consents working.
- **Never put the password on a remote command line.** A leak check must read the secret from
  stdin (`ssh host 'bash check.sh' < secrets`). An empty `grep -F ""` matches every line and
  looks like a leak.
