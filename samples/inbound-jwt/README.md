# Invoking a JWT-inbound agent from outside the console

[中文版本](README.zh-CN.md)

An agent deployed with **inbound auth = JWT** carries a `customJWTAuthorizer`
on its AgentCore Runtime. Such a runtime rejects SigV4 — every caller sends an
OAuth2/OIDC **bearer token** instead, over the Runtime data-plane HTTPS
endpoint (the AWS SDKs cannot send a bearer token):

```
POST https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{URL-encoded runtime ARN}/invocations?qualifier=DEFAULT
Authorization: Bearer <jwt>
Content-Type: application/json
X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: <session id, ≥ 33 chars>

{"prompt": "...", "actor_id": "..."}
```

No AWS credentials are involved in the call — the JWT is the whole
authentication. What the authorizer validates:

| Authorizer field | Token claim | Meaning |
|---|---|---|
| `discoveryUrl` → issuer | `iss` | the token must come from this IdP |
| `allowedClients` | `client_id` | the app client that minted the token |
| `allowedAudience` | `aud` | intended audience |
| `allowedScopes` | `scope` | granted scopes |
| `customClaims` | any claim | e.g. `cognito:groups CONTAINS_ANY [platform-admin]` |

A request whose token fails any configured rule gets **HTTP 403**; a request
with no token gets **HTTP 401** with a `WWW-Authenticate` header pointing at
the runtime's protected-resource metadata.

## Files

- `invoke_with_jwt.py` — obtains a Cognito token (client_credentials **or**
  USER_PASSWORD_AUTH, or accepts a ready-made `--token`) and invokes the
  runtime, printing streamed deltas as they arrive.
- `invoke_with_jwt.sh` — the same request as plain curl; token minting recipes
  in the header comment.

## Quick start against the Launchpad workspace pool

The Launchpad bootstrap provisions a Cognito user pool with two app clients
that the console's suggested JWT default pre-lists in `allowedClients`:

- the **console client** (no secret, `USER_PASSWORD_AUTH`) — human identities,
  tokens carry `username` and `cognito:groups`;
- the **M2M client** (secret, `client_credentials`) — machine identities, the
  same client the platform itself uses for non-interactive calls
  (public `/v1` API, evaluation runs).

Machine caller (client_credentials):

```bash
python3 invoke_with_jwt.py \
  --agent-arn "$AGENT_ARN" --region us-west-2 \
  --client-id "$M2M_CLIENT_ID" --client-secret "$M2M_CLIENT_SECRET" \
  --token-url "https://<domain>.auth.us-west-2.amazoncognito.com/oauth2/token" \
  --prompt "hello"
```

The token endpoint host is the pool's hosted-UI domain — read it from the
pool's discovery document
(`https://cognito-idp.<region>.amazonaws.com/<pool id>/.well-known/openid-configuration`,
field `token_endpoint`).

Human caller (USER_PASSWORD_AUTH; the client must allow that flow):

```bash
python3 invoke_with_jwt.py \
  --agent-arn "$AGENT_ARN" --region us-west-2 \
  --client-id "$CONSOLE_CLIENT_ID" --username demo --password '…' \
  --prompt "hello"
```

## Memory actor and the console

The runtime reads the memory actor from the payload's `actor_id`, not from
the token. Launchpad scopes memory per agent. Its Chat console sends
`<agent id>__<username>` in **both** modes: its "Invoke as me" toggle (the
user's own Cognito JWT) and the platform M2M token. So a direct caller that
passes `--actor-id <agent id>__<username>` (`ACTOR_ID=` for the shell script)
shares that console user's short- and long-term memory. Any other value gets
its own partition. The authorizer does not bind `actor_id` to the token's
subject, so if callers must not reach each other's memory, derive the actor
id server-side (see [docs/identity.md](../../docs/identity.md)).

The agent's page in the console (入站认证 / Inbound auth card) shows a
ready-made client_credentials curl for that exact runtime.

## Custom claims

A `custom_claims` rule such as `cognito:groups` `STRING_ARRAY` `CONTAINS_ANY`
`[platform-admin]` passes only tokens whose group list contains one of the
values. Cognito **user** tokens carry `cognito:groups`; **client_credentials**
tokens do not — so such a rule cleanly separates human callers from machine
callers (machines are rejected unless a separate rule admits them).

## Adapting to an enterprise IdP (Entra ID / Okta / any OIDC provider)

> Generic guidance — not tested against a live enterprise tenant in this
> repository. Validate in your own environment.

1. **Discovery URL** — your tenant's OIDC discovery document, e.g.
   `https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration`
   (Entra ID) or `https://<org>.okta.com/oauth2/default/.well-known/openid-configuration`
   (Okta). It must end in `/.well-known/openid-configuration` and expose a
   `jwks_uri` — the console probes this at save time. If the IdP is already a
   workspace OAuth2 Connection, **Choose from a Connection** in any JWT editor
   (wizard, workspace default, the agent page's *Switch to JWT* dialog) fills
   the discovery URL for you.
2. **Allowed clients / audience** — register an application (Entra "app
   registration", Okta "app integration") for each **caller** and list its
   client id in `allowed_clients`; if your IdP mints tokens with a distinct
   `aud` (Entra often uses an Application ID URI), list it in
   `allowed_audience`. Do not list a Connection's own client id: that is the
   agent's outbound client (how it calls tools), not an inbound caller.
3. **Machine callers** — use the client-credentials grant against your
   tenant's token endpoint; the `--token-url` flag of `invoke_with_jwt.py`
   takes any OAuth2 token endpoint.
4. **Human callers** — obtain the user's access token via your normal OIDC
   flow (auth code + PKCE) and pass it as `--token`.
5. **Claims** — map your IdP's group/role claim (Entra: `groups` or `roles`;
   Okta: a custom claim) into `custom_claims` rules; check the exact claim
   name in a decoded token first, because names differ per IdP and tenant
   configuration.

> **The console cannot invoke an agent on another IdP.** Console Chat (both
> "Invoke as me" and the default M2M path), the `/v1` API, direct invoke and
> evaluation all present **workspace-Cognito** tokens, so an agent whose
> authorizer trusts a different issuer refuses them. Launchpad names this up
> front (`409 agent.inbound_issuer_mismatch`) and warns in every JWT editor.
> Such an agent is reachable only by external callers holding tokens from its
> IdP, for example with the scripts in this directory.
