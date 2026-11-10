# Interface: gateway-iam (N3 → N7, N8)

Frozen 2026-10-09. The shared gateway `launchpad-gw` is AWS_IAM
(SigV4) inbound-only — no CUSTOM_JWT, no "No authorization" (both unavailable or
disallowed in AWS China Gateway). Every path that reaches it signs with the
caller's own AWS credentials; there is no bearer token to mint or vault.

## The shared SigV4 transport

`backend/app/templates/sigv4_gateway_transport.py.tmpl` is the single source. It
defines `_LaunchpadSigV4Auth(httpx.Auth)` and
`_launchpad_sigv4_streamablehttp(url, region)`, built from an in-container
`boto3.Session()` (the Runtime's own execution-role credentials — no identity-vault
round trip). Two renderers inline it as raw text (never imported as a Python
module — it is concatenated into generated source):

| Renderer | Where | How |
|---|---|---|
| `templates/gateway_support.py::render_gateway_source(spec)` | `zip_runtime` deploy (standard create-agent path) | Returns `_SIGV4_TRANSPORT_IMPORTS` (the `httpx`/`SigV4Auth`/`AWSRequest`/`streamablehttp_client` import lines the transport class needs, since it is pasted in as a class statement that resolves at module *import* time) + the `.tmpl` file's text + the trimmed `gateway_tools.py.tmpl` body exposing `gateway_tools(stack) -> list` / `gateway_client() -> MCPClient \| None`, into `templates/strands_agent/main.py.tmpl`'s `__LAUNCHPAD_GATEWAY_SOURCE__` placeholder (line ~177). |
| `services/harness_convert.py::graft_missing_gateway_clients` / `graft_lazy_gateway_token` | Harness → Runtime conversion (N7's `harness_local` calls it with a synthetic awsIam tool for the shared gateway; a remote-MCP branch was added) | Rebuilds the agentcore-CLI export's Gateway client(s); an `awsIam`-outbound-auth Harness Gateway target gets `_launchpad_sigv4_streamablehttp` the same way (constants `GW_SIGV4_TRANSPORT`/`_SIGV4_TRANSPORT_SOURCE`), an OAuth-outbound one still mints an M2M bearer via `@requires_access_token(..., auth_flow="M2M")` (that branch serves *third-party* Gateways a Harness may have been pointed at outside the shared `launchpad-gw` — unrelated to this build's own inbound-auth collapse). |

**N7 must not duplicate this file.** If `harness_local.py` needs its own gateway
client for the self-built Harness runtime, it should import/read the same
`templates/sigv4_gateway_transport.py.tmpl` path, not a copy.

## Env vars the generated client reads

| Var | Set by | Meaning |
|---|---|---|
| `LAUNCHPAD_GATEWAY_URL` | `deployer/environment.py::runtime_environment` | The gateway's `gatewayUrl` (`https://<id>.gateway.bedrock-agentcore.<region>.<dns-suffix>/mcp`). Unset ⇒ no gateway tools (fail-soft). |
| `LAUNCHPAD_GATEWAY_TOOLS` | same | Comma-separated fnmatch patterns of allowed tool names (Registry target scoping). Unset ⇒ every tool. |
| `AWS_REGION` | Runtime (ambient) | Fallback default is `cn-northwest-1` if ever absent — the Runtime always sets this in practice. |

`LAUNCHPAD_GATEWAY_PROVIDER` / `LAUNCHPAD_GATEWAY_SCOPE` (the OAuth M2M era's env vars for
the shared gateway) no longer exist — do not reintroduce them for `launchpad-gw`. The
gateway block itself needs no workload token (SigV4 with the runtime's own execution role).
`runtime_user_id()` (`gateway_support.py`) is nevertheless sent on **every**
`InvokeAgentRuntime` since the N2 merge (R2 superseded): each generated agent exchanges the
Runtime-injected workload token for the Global Bedrock API key (`GetResourceApiKey`, see
`model-credential.md`), and tool-level `auth` blocks (`identity_tools.py.tmpl`) reuse the
same token. `deployer/environment.py` writes `LAUNCHPAD_WORKLOAD_NAME` (the runtime id, known
from the second deploy on) for the self-mint fallback — independent of the gateway block.

## IAM

Execution role (`services/agent_iam.py::policy_document`), Sid `AgentCoreGatewayInvoke`:
```json
{
  "Sid": "AgentCoreGatewayInvoke",
  "Effect": "Allow",
  "Action": ["bedrock-agentcore:InvokeGateway"],
  "Resource": ["arn:{partition}:bedrock-agentcore:*:{account}:gateway/{gateway_id}"]
}
```
scoped per the spec's `gateway` ToolRef `config.gateway_id`(s) (`ctx.arn("bedrock-agentcore",
f"gateway/{gateway_id}", region="*")` — region wildcarded because a Registry-resolved
gateway may live in a different region of the account than the agent's own). This
statement predates N3 (shipped at N1 head, N3 only added its regression test —
`tests/test_agent_iam_policy.py::test_gateway_tool_grants_scoped_invoke_gateway`).

A bare `gateway` ToolRef also still triggers the unconditional `AgentCoreWorkloadIdentity`
statement (`agent_iam.py` — granted whenever `_uses_gateway(spec)` is true, which a
`gateway` ToolRef alone satisfies). That grant (`GetResourceApiKey`/
`GetResourceOauth2Token`/`GetWorkloadAccessToken*`) is harmless-but-unused for a
SigV4-only gateway tool today — the gateway's own comment says granting it for an
OAuth Gateway is inert, and the same is now true for an AWS_IAM one. N3 deliberately
does not narrow `_uses_gateway`/`policy_document`'s branching to drop it: a spec that
also has a knowledge-base ref or another MCP tool relies on the same family-wide
grant, so tightening this is a candidate for a future pass, not an N3 change.

Gateway bootstrap (`services/gateway_bootstrap.py::ensure_gateway`):
`authorizerType="AWS_IAM"`, no `authorizerConfiguration`. Targets:
`hr-database` (Lambda, `credentialProviderConfigurations=[{"credentialProviderType":
"GATEWAY_IAM_ROLE"}]` — the gateway service role itself invokes the Lambda, no vault
credential), `office-facts` (OpenAPI, `credentialProviderConfigurations=[{"credentialProviderType":
"API_KEY", ...}]` — the backend REST API still needs an `x-api-key`, which is a
target-level credential, independent of the gateway's own IAM inbound auth).

### Per-agent role teardown (N11 r2-B, N10-bug7)

`DELETE /api/agents/{id}` → `routers/agents.py::_delete_agent_resources` deletes the
Runtime first, then runs `deployer/teardown.py::cleanup_residue` — six best-effort
steps (`RESIDUE_STEPS`), each logged and reported in the response (`removed` / `failed` /
`failed_detail` / `steps`):

| step | what goes | client (funnel) |
|---|---|---|
| `execution_role` | the platform's two known inline policies (`launchpad-caps-<name>`, `launchpad-fs-<name>`) **then** `agent_iam.delete_execution_role` | `iam` |
| `log_groups` | every `/aws/bedrock-agentcore/runtimes/<runtime_id>-*`, once the Runtime is gone; then the background **settle re-check** below | `logs` (+ `bedrock-agentcore-control` for the gone-poll) |
| `artifacts` | `s3://<artifacts_bucket>/agents/<name>/` | `s3` |
| `memory_snapshots` | `s3://<memory_bucket>/<memory_prefix>/<store>/<agent_id>__*` (every scoped actor; the store the spec pins or the workspace default) | `s3` |
| `memory_records` | the long-term records of every scoped actor (`<agent_id>__*`) in the store the spec pins or the workspace default — one `delete_by_query` on the memory index (N11-obs19) | OpenSearch (`aws_clients.opensearch_http`) |
| `registry_record` | the agent's own A2A record (see `registry-backend.md`) | — |

**Log-group settle (N-SCN-obs8, `cn/r7-g`).** The runtime container's trailing log flush
re-created the just-deleted `…-DEFAULT` group 2 s and 18 s *after* `DELETE /api/agents/{id}`
had answered (runs #5 and #6), past the old 5 s inline re-check. The step now deletes the
groups and hands the prefix to a daemon thread (`deployer/teardown.settle_log_groups`) that
re-lists it at **+10 s, +30 s and +60 s** after the first delete
(`Settings.teardown_log_group_settle_s`, default `[10, 30, 60]`, strictly ascending, empty
disables), deletes whatever came back, and stops after **two consecutive empty checks**
(so 30 s in the common case, ≤ 60 s plus three API round-trips at most). The delete
response never waits for it: the `log_groups` step keeps its status (`deleted` / `skipped`)
with the note `settle re-check at +10/30/60 s in the background (job <id>)` and a
`job_id`, and the settle reports on a **`log_group_settle` job row** (`GET /api/jobs/{job_id}`,
JSONL events per check, `succeeded` / `failed`) — nothing in the settle can fail the purge.
A backend restart that interrupts a settle leaves its job `queued` / `running`;
`pipeline.resume_pending_jobs` re-runs it on startup (`teardown.start_settle_resume`: the whole
recorded schedule from now against the recorded prefix — list + delete is idempotent) and the
row closes `succeeded` / `failed`; a job whose workspace or payload cannot be resolved closes
`failed` with `interrupted by backend restart; not re-run: …` (cn/r7-h).
`scripts/teardown.py` runs the same wait → delete → settle **synchronously** for the
runtime log groups of every runtime it deletes (`runtime-log-groups` target right after each
`agent-runtime` / `skill-lab-runtime`); before `cn/r7-g` the script deleted no log groups.

Why the policies are deleted by name before the helper: the hub's EC2 ops role
(`infra/ec2/launchpad-cn-ec2.yaml`, Sid with `iam:CreateRole`/`iam:DeleteRole`/
`iam:DeleteRolePolicy`/…) grants **no `iam:ListRolePolicies`**, so
`agent_iam.delete_role`'s listing was denied live, `DeleteRole` then failed with
`DeleteConflict` (inline policies still attached) and only a log line remained — the
N10 roles `launchpad-agent-e2e-*` survived for that reason. The names are
deterministic (`agent_iam.capability_policy_name`, `fs_policy_name`), so no listing is
needed. Adding `iam:ListRolePolicies` to the ops role (infra, not this round) would make
the helper's own path work too; the spoke role (`infra/spoke/launchpad-workspace-role.yaml`)
already has it. The ops role carries `logs:*`; the S3 deletes use the same grants the
upload paths already need.

A failed step never fails the delete: the row is marked `deleted`, the step is
`failed` with the exception type/message, and the backend log carries the role name so
`scripts/teardown.py` can sweep it by the `launchpad:managed` tag. The Runtime delete
itself stays the one hard step — if it raises, the row stays and the member retries.

**`failed_detail` (N-SCN-obs9).** `failed` names the step only; the live twin delete
answered `failed=["memory_records"]` and the operator could not tell a transient on the
self-hosted substrate from a real leak. The response now also carries
`failed_detail: {step: reason}` (additive), one line per failed step: the exception type
plus the code or status that classifies it — `OpenSearchError: HTTP 503`,
`ClientError: ThrottlingException on DeleteLogGroup`, `RuntimeError: s3 is down`,
`2 of 2 log group delete(s) failed · …` — never a response body (`OpenSearchError`'s
message embeds the first 500 bytes of the body; `failed_detail` strips it), never more than
the first line, capped at 160 characters. Each entry of `steps` carries the same text as
`reason`; `detail` keeps the longer `type: message` text as before.

## Console-side MCP client

`services/mcp_client.py::tools_list`/`tools_call` sign through
`WorkspaceContext.credentials()` (the hub's own ambient/assumed-role credentials,
same funnel as every other `aws_clients.py`-constructed client — no bare
`boto3.client(...)`), service `bedrock-agentcore`, region = `workspace.region`. The
module also keeps one unsigned path, `_unsigned_rpc(url, token, method, params)`,
used only by `app.assistant.tool_catalog.remote_tool_names` to probe a Registry
`kind: "mcp"` record's own third-party `url` — a standalone MCP server that is not
the shared AWS_IAM gateway and in general cannot be SigV4-signed (it may not even be
AWS-hosted); `token`, when given, travels as a bearer token for a server that wants
one. This is unrelated to `launchpad-gw`'s own inbound auth.

## What this build removed (do not reintroduce)

- `gateway_bearer_token()`/`GetResourceOauth2Token(oauth2Flow="M2M")` against the
  shared gateway (deleted from `gateway_tools.py.tmpl` in N3 Task 1).
- `gateway_access_token` ("invoke as me" for Gateway tools) end to end — it was
  already dead (always `None`) since N1 and is fully deleted in N3 Task 1.
- `inbound_auth.py`'s JWT mode, the console switch dialog, the workspace
  inbound-auth default, and the "invoke as me" Chat toggle (N3 Task 3/4) — v1
  Runtime inbound is AWS_IAM only (D9); a future external-IdP node would re-add a
  JWT mode from scratch, not un-delete this code.
