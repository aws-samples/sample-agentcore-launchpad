# Runtime discovery research

Date: 2026-07-31

## Sources

- AWS Boto3 reference:
  `bedrock-agentcore-control.Client.list_agent_runtimes`
- AWS API reference: `GetAgentRuntime`
- Repository architecture, Agent ledger/routes, Runtime wrapper, shared invoke
  service, Agent Management page, and API tests.
- Read-only live probe against the repository-configured account and Region.

## API findings

`ListAgentRuntimes` accepts `maxResults` and `nextToken`. Its summaries contain
ARN, ID, version, name, optional description, last update time, and status.

`GetAgentRuntime` is required for protocol, artifact, authorizer, network,
lifecycle, and other detail. Sensitive detail exists in this response, including
environment variables and artifact locations, so discovery needs an allow-list
projection rather than returning or storing the raw AWS response.

The repository-pinned SDK reports that `list_agent_runtimes` is pageable.

## Live account findings

Configured Region: `us-west-2`

- Total runtimes: 52
- Protocols: 46 HTTP, 3 A2A, 3 MCP
- Artifacts: 33 container, 19 code
- Authorization: 45 none, 7 custom JWT
- Current statuses: all 52 READY
- Runtime-backed rows already present in the local non-deleted ledger: 12

The three MCP resources are runtime-hosted MCP servers, not agents. They must be
visible in discovery for completeness but excluded from the agents ledger.

Some A2A and HTTP runtimes use custom JWT authorization. Launchpad has no token
mapping for those external authorizers, so importing them for inventory is safe
but exposing Chat or `/v1` would create a known-broken invocation path.

## Existing contract findings

- `GET /api/agents` is ledger-backed; AWS resources cannot simply be concatenated
  into that response because all downstream surfaces use local agent IDs.
- Agent deletion dispatches by method. A distinct imported method can provide an
  explicit detach-only branch.
- Re-publish requires a complete `AgentSpec` and source/artifact ownership, which
  imported runtimes do not have.
- The shared invoke service already has HTTP and A2A Runtime paths.
- Experiments, canaries, and evaluation use method allow-lists, so a distinct
  imported method is excluded by default.
- Agent Management already uses a single list consumed by Overview, Chat,
  Evaluation, canaries, Registry demos, and other surfaces. Invocation eligibility
  must therefore be projected centrally rather than inferred separately by each
  page.
