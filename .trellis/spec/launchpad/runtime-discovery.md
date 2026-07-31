# Existing AgentCore Runtime Discovery

> Read-only Region discovery plus explicit import of externally owned
> AgentCore Runtime resources into the Launchpad ledger.

## Scope

Use this contract when changing Runtime listing/detail wrappers, Agent
Management discovery, imported-agent lifecycle, or shared invocation
eligibility.

Discovery is an onboarding path, not a deploy method. Never register
`discovered_runtime` with the deployment pipeline or add it to `AgentSpec.Method`.

## API And Projection

```text
GET  /api/agents/discovery
POST /api/agents/discovery/import  {"runtime_ids": ["..."]}  # <=100 unique
```

`runtime.list_runtimes(control)` follows all `ListAgentRuntimes` pages.
Discovery then calls `GetAgentRuntime` once per summary. A list failure is a
typed `runtime.discovery_failed` error; a detail failure produces one
non-importable candidate and does not hide the remaining resources.

The candidate projection is an allow-list:

- Runtime ID/ARN, name, description, version, status, and last update time;
- protocol (`HTTP`, `A2A`, `MCP`, or unknown);
- artifact type (`code`, `container`, or unknown);
- authorizer type (`none`, `custom_jwt`, or unknown);
- managed-agent match (including the method needed to distinguish refreshable
  discovered rows from Launchpad-owned rows) and typed import/invoke capabilities.

Never return or persist environment values, S3/ECR source locations, role ARNs,
network/filesystem detail, request headers, JWT issuer/client configuration, or
raw `GetAgentRuntime` responses.

## Import And Identity

Import re-fetches every selected Runtime and does not trust prior scan data.
HTTP and A2A are importable; MCP and unknown protocols are not. Per-item
failures are returned beside successful imports.

Imported rows use:

```text
method      = discovered_runtime
owner       = aws-discovery
resource_id = agentRuntimeId
arn         = agentRuntimeArn
version     = agentRuntimeVersion
status      = READY -> active; *_FAILED -> failed; otherwise deploying
spec        = {protocol, discovery: {
                 runtime_name, description, artifact_type,
                 authorizer_type, aws_status, last_updated_at
              }}
```

Match non-deleted rows by ARN first and Runtime ID second. A managed-method
match is `already_managed` and must never be rewritten. A discovered match is
refreshed in place. If its AWS name conflicts with an unrelated ledger row,
append a deterministic Runtime-ID suffix to the display name while preserving
the AWS name in `spec.discovery.runtime_name`.

No Deployment, Job, Registry record, source bundle, or secret is created.
Deleting a discovered row marks only the ledger row deleted and returns
`aws_resource_deleted=false`.

## Invoke Capability

`runtime_discovery.invoke_capability(agent)` is the single projection returned
on every agent API object and enforced by console invoke, Chat, and `/v1`.
Regardless of the stored AWS snapshot, a deleted/non-active row or a row without
an ARN is never invoke-eligible.

| Imported Runtime state | Result |
|---|---|
| HTTP/A2A + `READY` + no custom authorizer | eligible |
| MCP | `not-agent-protocol` |
| unknown protocol | `unsupported-protocol` |
| custom JWT | `external-authorizer` |
| non-`READY` | `runtime-not-ready` |

Eligible discovered HTTP resources call `invoke_runtime_text`; A2A resources
call `invoke_a2a_text` with the stable session at both protocol layers.
Discovered resources always use buffered Chat compatibility, including
container artifacts, because their application event contract is unknown.

The public `/v1/agents` list and Chat picker expose only invoke-eligible rows.
Direct invoke attempts enforce the same capability server-side.

## Ownership Restrictions

- no Edit or re-publish;
- no Harness conversion;
- no configuration experiment or Runtime canary;
- no managed batch evaluation;
- no automatic Registry registration;
- removal is labeled and implemented as detach-only.

Existing method allow-lists remain the control for experiment, canary, and
evaluation exclusion. Do not broaden those lists when adding discovery.

## Required Tests

- Runtime pagination and fatal list errors;
- allow-list sanitization and per-detail failure retention;
- HTTP/A2A/MCP protocol eligibility and custom-JWT inventory behavior;
- ARN/ID idempotency, managed-row protection, metadata refresh, and name conflict;
- no Deployment/Job on import and no AWS mutation on delete;
- shared HTTP/A2A dispatch and typed ineligible invocation;
- frontend lint, strict TypeScript, build, and English/Chinese i18n parity.
