# Identity P2 — real-AWS e2e evidence (as_user / 3LO)

Run on 2026-09-28 (UTC) in account `959545103699`, region `us-west-2`, driven by
[`backend/scripts/e2e_identity_3lo.py`](../backend/scripts/e2e_identity_3lo.py)
against a throwaway stack: backend `127.0.0.1:8792` with a temporary SQLite ledger
(`/tmp/lpid-e2e-3lo/ledger.db`), and vite `localhost:5192` proxying to it. The stack
was started with `LAUNCHPAD_PUBLIC_BASE_URL=http://localhost:5192`, so the return URL
was `http://localhost:5192/auth/return`. No production host or DB was touched. Every
AWS resource created is named `lpid-e2e-*`, apart from the per-agent role noted in
§1, and everything was deleted at the end (§5).

## 1. Scope decisions

| Decision | Why |
|---|---|
| The IdP is a **new throwaway Cognito user pool** `lpid-e2e-3lo-pool` with a domain, an app client (authorization code, `openid profile email`) and one user | `launchpad-users` must not be modified. A pool we own lets the hosted-UI login run headless, so no human is needed at any step |
| `LAUNCHPAD_RESOURCES` held only `execution_role_arn` and `artifacts_bucket`, with no `registry_id`, `gateway_id` or `memory_id` | The deploy's `register` stage was therefore **skipped** ("Agent Registry is not configured"), and no Gateway or Memory was touched. The agent was created with `memory: {short_term: false, long_term: false}` |
| The zip artifact was uploaded to `s3://launchpad-artifacts-…/agents/lpid-e2e-3lo-agent/` | This is the normal zip-deploy path. The object was deleted in teardown, and the bucket and its policy were not changed |
| **Deviation:** the product's `provision` stage created the per-agent IAM role `launchpad-agent-lpid-e2e-3lo-agent-680717ab` (CloudTrail `CreateRole` / `PutRolePolicy` at 04:51:58Z) | This is how every zip Runtime deploy works (`app/services/agent_iam.py`). It is a role, not an IAM user or access key, but it is outside the PRD §7 list of allowed kinds. The agent delete removed it: `GetRole` → `NoSuchEntity`, and no `*lpid*` role or customer-managed policy remains (§5) |
| The Consent Portal was only **probed** (read-only `ListConsentPortals`), not created | Creating a portal needs an operator-supplied execution role (the rules forbid creating an IAM role for it) and a JWT-inbound gateway with the IdP's issuer (`launchpad-gw` must not be modified). See [identity.md §7.6](identity.md#76-consent-portal-as_user-gateway-targets) |

## 2. Resources created

| Resource | Identifier |
|---|---|
| Cognito user pool | `us-west-2_otbjcvk6x` (`lpid-e2e-3lo-pool`) |
| Cognito domain | `lpid-e2e-3lo-f523ce.auth.us-west-2.amazoncognito.com` |
| Cognito app client | `1sisvql5eco5tf37sjqn5fu8ko` (`lpid-e2e-3lo-client`); its CallbackURLs were set to the Connection callback below |
| Cognito user | `lpid-e2e-user` (permanent password, kept only in the `0600` state file, which was deleted in teardown) |
| OAuth2 credential provider (Connection) | `arn:aws:bedrock-agentcore:us-west-2:959545103699:token-vault/default/oauth2credentialprovider/lpid-e2e-3lo`, callback `https://bedrock-agentcore.us-west-2.amazonaws.com/identities/oauth2/callback/60479f0a-1082-4346-b71a-748a0bdcb4c9` |
| Agent (zip Runtime) | ledger `680717abc7b0437ba7a4411536f663af`, runtime `lpid_e2e_3lo_agent_cccf9a-3ltYh456kU`, with one REST tool `userinfo` → `https://lpid-e2e-3lo-f523ce.auth.us-west-2.amazoncognito.com/oauth2/userInfo`, `auth {connection: lpid-e2e-3lo, kind: oauth2, mode: as_user, scopes: [openid, profile, email]}` |
| Workload identity | `lpid_e2e_3lo_agent_cccf9a-3ltYh456kU`, created by the Runtime (service-linked) |
| Per-agent IAM role | `launchpad-agent-lpid-e2e-3lo-agent-680717ab` (see the deviation in §1) |
| Zip artifact | `s3://launchpad-artifacts-959545103699-us-west-2/agents/lpid-e2e-3lo-agent/deployment_package.zip` (38.1 MB) |

## 3. The flow: 24 assertions passed, 0 failed (04:53:00Z–04:53:41Z)

This was the run's second pass. The first pass got as far as step 1 (all green),
then stopped when the venv's Playwright could not find its browser build, before the
authorization URL was used. The script is idempotent, so the second pass reused the
same Connection and agent. `LPID_CHROMIUM` now points it at the locally installed
headless shell.

```text
deploy.stage  generate  succeeded  strands template · 45279 bytes
deploy.stage  package   succeeded  pip+zip 10.6s · 38.1MB · s3 ✓
deploy.stage  provision succeeded  iam role · launchpad-agent-lpid-e2e-3lo-agent-680717ab
deploy.stage  deploy    succeeded  READY · arn:…:runtime/lpid_e2e_3lo_agent_cccf9a-3ltYh456kU
deploy.stage  register  skipped    registry unavailable · Agent Registry is not configured
assert.ok  OAuth2 Connection created · client secret never echoed
assert.ok  app client callback = Connection callback
assert.ok  zip Runtime agent active
assert.ok  deployer reconciled the return URL onto the runtime's workload identity
           allowedResourceOauth2ReturnUrls = ["http://localhost:5192/auth/return"]
── (1) Chat turn → auth_required, grant pending
assert.ok  auth_required event emitted
assert.ok  session uri never reaches the browser      keys = [agent_id, provider, scopes, tool, url]
assert.ok  ask names Connection + tool                lpid-e2e-3lo · userinfo · [openid, profile, email]
           url host = https://bedrock-agentcore.us-west-2.amazonaws.com/identities/oauth2/authorize
assert.ok  grant pending before consent               {status: pending, force_reauth: false}
── (2) headless Cognito consent → /auth/return binding leg
assert.ok  IdP → AgentCore → our /auth/return          landed = http://localhost:5192/auth/return
assert.ok  return URL carries session_id + state
assert.ok  return page reached done                   "已为 lpid-e2e-3lo-agent 授权 lpid-e2e-3lo。"
assert.ok  spent session id removed from the address bar
assert.ok  back-to-chat CTA enabled once bound
── (3) grant authorized, next turn returns the user's claims
assert.ok  grant authorized                           authorized_at 04:53:14Z
assert.ok  no second consent ask
assert.ok  answer carries the consenting user's claims
           "- sub: 18c11300-… - email_verified: true - email: lpid-e2e-user@example.com
            - username: lpid-e2e-user"
── (4) different runtime user → not authorized
assert.ok  the first user's token did not serve another user
           "It looks like you haven't authorized access to your user information yet…"
── (5) revoke → next call asks for consent again
assert.ok  revoke accepted                            {revoked: true, provider: lpid-e2e-3lo, agents: 1}
assert.ok  revocation in force                        {status: revoked, force_reauth: true}
assert.ok  revoked grant → auth_required again
assert.ok  no claims served while revoked
── (6) re-consent clears the revocation
assert.ok  re-consent bound
assert.ok  authorized again, revocation cleared       {status: authorized, force_reauth: false,
                                                       authorized_at 04:53:36Z}
assert.ok  tool works after re-consent
consent_portal.probe  portals = []                    (ListConsentPortals 200, read-only)
RUN PASSED
```

What each step proves:

1. The generated as_user tool performs the non-blocking `GetResourceOauth2Token(USER_FEDERATION)`. The console receives only the single-use URL; the `sessionUri` stays server-side in `oauth_pending_sessions`.
2. The deployer's return-URL reconcile is correct. AgentCore redirects to `/auth/return` with `session_id` + `state` (customState), and the page performs the binding leg (`POST /api/identity/oauth/complete` → `CompleteResourceTokenAuth` for the user recorded with the session). No human was needed: the Cognito hosted UI was driven headless.
3. The token is in the vault under the console user, and the tool calls a real third-party API (`/oauth2/userInfo`) with the **user's own** token.
4. Bindings are per user.
5. Revoke works without a revoke API. The next call carries `forceAuthentication=true` and asks again.
6. The revocation clears only when a fresh consent completes (`authorized_at >= requested_at`).

Steps 3 and 5 were also driven through the real console UI while taking the
screenshots (§4). There, the Chat auth card went from pending to authorized by
polling while consent completed in a second tab, and **Retry** returned the claims.

## 4. Screenshots

These are in `/home/ec2-user/.openclaw/workspace/tmp/identity-v2-p2-shots/`. Each has a zh-CN version and an `-en` twin, and all were taken against the live stack above:

- `chat-auth-card-pending`: a real `auth_required` in Chat.
- `chat-auth-card-authorized`: taken after a real consent in a second tab. The retry is enabled.
- `chat-auth-retry-answer`: the retry returned the user's claims.
- `chat-auth-card-restored`: history restored while the grant is authorized.
- `chat-auth-card-restored-revoked`: history restored after a revoke. The URL-less card says the link was not kept and offers the retry.
- `auth-return-done`: a real binding.
- `auth-return-binding`: the completion POST was held open by a Playwright route. The CTA is disabled.
- `auth-return-failed`: a real 404 `identity.session_unknown` for an unknown session.
- `my-connections`: an authorized grant.
- `my-connections-revoke-confirm`: the ConfirmDialog, cancelled.
- `my-connections-revoked`: the status tag is gray and revoke is disabled while the revocation is in force.
- `e2e-auth-return-done-zh`, `e2e-auth-return-reconsent-zh`: captured by the e2e script itself.

## 5. Teardown (04:56:46Z–04:56:54Z) and leftover check

```text
agent.deleted              680717abc7b0437ba7a4411536f663af
connection.delete          200
runtime.gone               lpid_e2e_3lo_agent_cccf9a-3ltYh456kU
workload_identity.gone     lpid_e2e_3lo_agent_cccf9a-3ltYh456kU   (went with the runtime)
credential_provider.gone   lpid-e2e-3lo
artifact.deleted           s3://launchpad-artifacts-…/agents/lpid-e2e-3lo-agent/deployment_package.zip
artifact.gone              prefix agents/lpid-e2e-3lo-agent/ · 0 objects left
cognito.domain.deleted     lpid-e2e-3lo-f523ce
cognito.pool.gone          us-west-2_otbjcvk6x (this deletes its app client and user too)
cognito.pools.lpid_left    []
state_file.deleted         /tmp/lpid-e2e-3lo-state.json
DOWN OK
```

An independent AWS CLI sweep ran afterwards. Every list below came back empty for `lpid*`:

- `list-agent-runtimes`
- `list-workload-identities`
- `list-oauth2-credential-providers`
- `list-api-key-credential-providers`
- `list-gateways`
- `cognito-idp list-user-pools`
- `s3 ls …/agents/`
- `list-consent-portals` → `{"consentPortals": []}`
- `iam list-roles` (contains `lpid`)
- `iam list-policies --scope Local` (contains `lpid`)

`iam get-role --role-name launchpad-agent-lpid-e2e-3lo-agent-680717ab` returned
`NoSuchEntity`. CloudTrail (`lookup-events`, ResourceName = the role) shows the
role's whole lifecycle:

- `CreateRole`, `PutRolePolicy` and `DeleteRolePolicy` at 04:51:58Z.
- `DeleteRolePolicy` and `DeleteRole` at 04:56:46Z, which is the agent delete.

The sweep was repeated at 05:10Z, after the docs commits, and came back empty
again.

The throwaway backend (:8792) and vite (:5192) were stopped by PID, and both ports
were probed closed afterwards.

## 6. Not proven live

- **The Consent Portal and as_user Gateway targets.** Only the read-only probe ran (see §1). The wrappers and routes are covered by `backend/tests/test_consent_portal.py`.
- **`/v1` callers.** They receive the authorization URL, but completing it needs a console user matching the recorded runtime user. This is documented as a limitation in [identity.md](identity.md) and was not exercised.
- **The `window.close()` path of "Back to the tab that started the request".** Headless Chromium opened the IdP link from the card, so the close/fallback behavior was not asserted. The binding and the auth card's flip were both asserted.
