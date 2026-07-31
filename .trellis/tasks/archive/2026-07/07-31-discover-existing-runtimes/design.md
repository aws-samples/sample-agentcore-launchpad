# Design: discover existing AgentCore runtimes

## Boundaries

The feature is an onboarding path, not a deployment method. It adds a fourth
entry to Agent Management but does not add a stage implementation to the unified
deployment pipeline.

```
AgentCore control plane
  ListAgentRuntimes (paginated)
    -> GetAgentRuntime (one per summary)
      -> sanitized discovery candidates
        -> explicit operator selection
          -> discovered Agent ledger rows
            -> existing GET /api/agents consumers
```

AWS remains the source of truth. The ledger stores identity and a sanitized
snapshot so imported resources can participate in Launchpad navigation without
claiming ownership of their lifecycle.

## Backend API

### `GET /api/agents/discovery`

Reads all Runtime pages, inspects every item, and returns:

```json
{
  "region": "us-west-2",
  "runtimes": [
    {
      "runtime_id": "name-suffix",
      "runtime_arn": "arn:...",
      "name": "name",
      "description": "...",
      "version": "3",
      "aws_status": "READY",
      "protocol": "HTTP",
      "artifact_type": "code",
      "authorizer_type": "none",
      "last_updated_at": "...",
      "managed_agent_id": null,
      "importable": true,
      "reason_code": null,
      "reason": null
    }
  ]
}
```

The list call is fatal if it cannot run. A failed detail lookup produces a
candidate with `importable=false` and an inspection error instead of dropping
the remainder of the scan.

### `POST /api/agents/discovery/import`

Input:

```json
{"runtime_ids": ["name-suffix"]}
```

The server re-fetches every selected Runtime; it does not trust scan output.
The result reports `imported`, `updated`, `already_managed`, and `failed` rows so
one inaccessible resource does not discard successful imports.

The request is bounded to 100 unique Runtime IDs.

## Runtime Wrapper

Add a paginated `list_runtimes(client)` wrapper beside `get_runtime()` in
`app.services.agentcore.runtime`. It receives an explicit client for test
injection and contains preview API field handling.

Add `app.services.runtime_discovery` as the owner of:

- AWS detail sanitization and candidate projections;
- protocol/import eligibility;
- AWS-status to ledger-status projection;
- resource identity matching;
- batch upsert behavior.

No new boto3 client is constructed outside `agentcore/client.py`.

## Ledger Projection

Imported agents use:

- `method = "discovered_runtime"`
- `resource_id = agentRuntimeId`
- `arn = agentRuntimeArn`
- `version = agentRuntimeVersion`
- `owner = "aws-discovery"`
- `status = active | deploying | failed`, projected from AWS status
- `spec.protocol = "http" | "a2a" | "mcp"`
- `spec.discovery` containing only description, artifact type, authorizer type,
  AWS status, and AWS last-updated time

No schema migration is needed. The method and spec metadata encode the ownership
boundary. Existing rows are matched by ARN first and Runtime ID second. A
Launchpad-created match is returned as already managed and is never rewritten.
A discovered match is refreshed in place.

If the AWS name conflicts with an unrelated active ledger row, the imported
display name receives a deterministic Runtime-ID suffix while the original AWS
name remains in `spec.discovery.runtime_name`.

No `Deployment` or `Job` row is created, so deployment revision remains separate
from AWS Runtime version. The UI displays `Agent.version` for discovered rows.

## Invocation Capability

Add one backend-owned `invoke_capability` projection to every agent response:

- existing managed methods: eligible under their current contracts;
- discovered HTTP/A2A + READY + no custom authorizer: eligible;
- MCP: ineligible (`not-agent-protocol`);
- custom JWT: ineligible (`external-authorizer`);
- non-READY: ineligible (`runtime-not-ready`);
- unknown protocol: ineligible (`unsupported-protocol`).

The console invoke route, Chat, and `/v1` enforce this projection. The shared
invoke service dispatches eligible discovered HTTP runtimes through
`invoke_runtime_text` and A2A runtimes through `invoke_a2a_text`. Discovered
container runtimes use the buffered compatibility stream because Launchpad
cannot assume they emit the generated Claude SDK event contract.

Experiments, canaries, and managed batch evaluation remain excluded because the
method is not in their existing supported sets and Launchpad has no source/spec
contract for rebuilding the resource.

## Lifecycle Safety

The delete route branches explicitly on `discovered_runtime`: it marks the
ledger row deleted without calling a deployer teardown function. The response
includes whether an AWS resource was deleted so the UI can use "Remove from
Launchpad" language.

Re-publish rejects discovered rows before AgentSpec-based editing. The UI hides
Edit and other ownership-dependent actions.

## Frontend

Use the established complex-page query pattern:

- `/create`: current method selection and agent list;
- `/create?view=discover`: Runtime scan/import view.

The fourth method card opens the discovery view. The view provides refresh,
selection checkboxes, Select eligible, Import selected, status/protocol/artifact
chips, and candidate reasons. Already managed rows link back to the normal agent
list state.

The existing list renders a `DISCOVERED RT` method chip, AWS Runtime version,
Chat only when invoke-capable, and a detach-only removal confirmation.

All visible text uses matching `en` and `zh-CN` keys.

## Compatibility And Rollback

- Existing creation and pipeline APIs are unchanged.
- No database migration or AWS mutation is introduced.
- Rollback is code-only. Imported ledger rows can remain inert under older code;
  their unknown method already avoids current teardown dispatch, but the new code
  makes that safety explicit.
- Removing an imported row allows the same AWS Runtime to be discovered again.

## Documentation

Update the authoritative architecture map and add a Launchpad spec describing
the discovery/import ownership and invocation contracts.
