# Identity P1 — real-AWS e2e evidence

Run on 2026-09-28 (UTC) in account `959545103699`, region `us-west-2`, driven by
[`backend/scripts/e2e_identity_connections.py`](../backend/scripts/e2e_identity_connections.py)
against a local backend on `127.0.0.1:8791` with a throwaway SQLite ledger
(`/tmp/lpid-e2e-run/launchpad.db`). No `config/launchpad.yaml` was present, no
production host or DB was touched, and every AWS resource created is named
`lpid-e2e-*`. Everything was deleted at the end; the deletion evidence is in §4.

## 1. Scope decisions

| Decision | Why |
|---|---|
| The Gateway targets live on a **throwaway gateway `lpid-e2e-gw`**, not on `launchpad-gw` | Adding a target to the shared `launchpad-gw` would modify an existing `launchpad-*` resource, which the rules forbid. The backend under test was started with `LAUNCHPAD_RESOURCES='{"gateway_id": "lpid-e2e-gw-7b874fovyx"}'`, so `/api/identity/gateway-targets` operated on it only |
| `lpid-e2e-gw` is created with `roleArn = arn:aws:iam::959545103699:role/launchpad-gateway-role` (`authorizerType AWS_IAM`, `protocolType MCP`) | The role is only **referenced**; it was not modified. No IAM role, user or access key was created |
| The OAuth2 Connection uses the `cognito` template with the `launchpad-users` pool's discovery URL (`https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Qy79RAFIr/.well-known/openid-configuration`) and **placeholder** client credentials | Creating an app client inside the existing `launchpad-users` pool would modify that pool. `CreateOauth2CredentialProvider` does not validate the client credentials, and an OpenAPI target binds the provider without fetching a token, so creation and binding are exercised in full. No Cognito app client was created |
| The identity-page screenshot uses one agent row **seeded only in the temporary ledger** (`lpid-e2e-crm-agent`, no AWS resource), whose spec references both Connections | A real deploy would need a zip Runtime and is not required for P1 acceptance. The row was deleted before cleanup |

## 2. Resources created

| Resource | Identifier |
|---|---|
| Gateway | `lpid-e2e-gw-7b874fovyx` (`lpid-e2e-gw`), READY at 03:32:39Z |
| API-key credential provider | `arn:aws:bedrock-agentcore:us-west-2:959545103699:token-vault/default/apikeycredentialprovider/lpid-e2e-apikey` |
| OAuth2 credential provider | `arn:aws:bedrock-agentcore:us-west-2:959545103699:token-vault/default/oauth2credentialprovider/lpid-e2e-oauth` (callback `https://bedrock-agentcore.us-west-2.amazonaws.com/identities/oauth2/callback/7dc7ba51-bf5d-439a-a973-76a9650dff73`) |
| Gateway target (OpenAPI, OAuth2, as_agent) | `GZCP6S4EYN` `lpid-e2e-crm-oauth` |
| Gateway target (OpenAPI, API key, as_agent) | `ZICUEGDTHA` `lpid-e2e-facts-key` |

## 3. Assertions (`run --keep`, 03:33:08Z–03:33:15Z): 14 passed, 0 failed

```text
assert.ok  api-key Connection created                                   201
assert.ok  secret never echoed
assert.ok  duplicate name answers 409 identity.connection_exists        409
assert.ok  OAuth2 Connection created                                    201
assert.ok  OAuth2 Connection returns a callback URL
assert.ok  client secret never echoed
assert.ok  api_key:lpid-e2e-apikey listed as launchpad/ready
assert.ok  oauth2:lpid-e2e-oauth listed as launchpad/ready
assert.ok  OpenAPI target bound to the OAuth2 Connection                201
assert.ok  OpenAPI target bound to the API-key Connection               201
assert.ok  lpid-e2e-crm-oauth reads back bound to lpid-e2e-oauth as_agent   (status READY)
assert.ok  lpid-e2e-facts-key reads back bound to lpid-e2e-apikey as_agent  (status READY)
assert.ok  OAuth2 Connection lists the target in referenced_by          ["lpid-e2e-crm-oauth"]
assert.ok  deleting a referenced Connection answers 409 identity.connection_referenced
```

An independent CLI read-back (`aws bedrock-agentcore-control get-gateway-target`)
confirmed the stored `credentialProviderConfigurations`:

```json
{"name": "lpid-e2e-crm-oauth", "status": "READY", "cred": [{"credentialProviderType": "OAUTH",
  "credentialProvider": {"oauthCredentialProvider": {
    "providerArn": "arn:aws:bedrock-agentcore:us-west-2:959545103699:token-vault/default/oauth2credentialprovider/lpid-e2e-oauth",
    "scopes": ["lpid-e2e/read"], "grantType": "CLIENT_CREDENTIALS"}}}]}
{"name": "lpid-e2e-facts-key", "status": "READY", "cred": [{"credentialProviderType": "API_KEY",
  "credentialProvider": {"apiKeyCredentialProvider": {
    "providerArn": "arn:aws:bedrock-agentcore:us-west-2:959545103699:token-vault/default/apikeycredentialprovider/lpid-e2e-apikey",
    "credentialParameterName": "X-Api-Key", "credentialLocation": "HEADER"}}}]}
```

While the resources were kept, the six P1 screenshots were taken against them
(`/home/ec2-user/.openclaw/workspace/tmp/identity-v2-p1-shots/`).

## 4. Deletion evidence

Cleanup through the API (`cleanup`, 03:38:00Z–03:38:06Z):

```text
target.delete   lpid-e2e-crm-oauth  200
target.delete   lpid-e2e-facts-key  200
targets.remaining  []
connection.delete  oauth2  lpid-e2e-oauth   200 {"deleted":true}
connection.readback_after_delete  oauth2  lpid-e2e-oauth   404 identity.connection_not_found
connection.delete  api_key lpid-e2e-apikey  200 {"deleted":true}
connection.readback_after_delete  api_key lpid-e2e-apikey  404 identity.connection_not_found
```

`gateway-down` (03:38:35Z): `{"event": "gateway.gone", "gateway_id": "lpid-e2e-gw-7b874fovyx"}`.

Independent CLI check afterwards:

```text
## gateways
launchpad-gw-hsfsvucxar	launchpad-gw	READY
## get-gateway lpid-e2e-gw-7b874fovyx
ResourceNotFoundException: Failed to retrieve gateway because it doesn't exist.
## launchpad-gw targets (unchanged from the pre-run baseline)
G0B6N4AXBD	office-facts	READY
QQI3WPVIR9	hr-database	READY
## oauth2 providers
launchpad-gw-m2m	p2-github
## api-key providers
launchpad-office-facts-key
## get lpid-e2e-oauth
ResourceNotFoundException: CredentialProvider not found
## get lpid-e2e-apikey
ResourceNotFoundException: ApiKeyCredentialProvider not found for lpid-e2e-apikey
## workload identities matching lpid: 0
## Cognito app clients matching lpid in launchpad-users: 0
## token-vault secrets (incl. planned deletion) matching lpid: none
```

The pre-run baseline was identical: one gateway (`launchpad-gw`), whose targets were
`office-facts` and `hr-database`; OAuth2 providers `launchpad-gw-m2m` and `p2-github`;
and the API-key provider `launchpad-office-facts-key`. **Leftover AWS resources: none.**

## 5. Not exercised live

- A real client-credentials token exchange through a bound target (the OAuth2
  Connection holds placeholder credentials; see §1).
- A deployed agent calling a Connection-bound tool (the identity page ran against a
  ledger-only agent, so its workload identity reads `not_deployed`).
