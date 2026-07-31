# Discover existing AgentCore runtimes

## Goal

Add a fourth Agent Management entry that discovers AgentCore Runtime resources
in the configured AWS Region and explicitly imports existing agent-like runtimes
into the Launchpad agent ledger without redeploying or taking ownership of the
AWS resources.

## Requirements

- Scan the configured Region through the AgentCore control plane and follow all
  `ListAgentRuntimes` pages.
- Inspect each runtime through `GetAgentRuntime` so the UI can distinguish HTTP,
  A2A, and MCP protocols, code and container artifacts, custom authorization,
  AWS status, version, and last update time.
- Show every discovered Runtime in the discovery view, including why a resource
  cannot be imported.
- Treat HTTP and A2A runtimes as agent-like and eligible for import. Show MCP
  runtimes as non-agent resources and do not allow importing them into the
  agents list.
- Let the operator select one or more eligible runtimes and import them. Scanning
  alone must not mutate the ledger.
- Make import idempotent by AWS Runtime ARN/ID. Existing Launchpad-managed agents
  must never be rewritten as discovered agents.
- Preserve only management metadata needed by Launchpad. Do not expose or persist
  environment variable values, container image URIs, source locations, role
  ARNs, or authorizer secrets.
- Mark imported rows as externally owned. They are not editable, cannot be
  re-published, and do not enter the five-stage deployment pipeline or automatic
  Registry registration.
- Removing an imported row removes only the local ledger entry. It must never
  delete or update the AWS Runtime.
- Imported runtimes with `READY`, no custom authorizer, and HTTP or A2A protocol
  may use Chat and `/v1` through the shared invoke chain. Other imported runtimes
  remain visible but non-invokable, with a typed capability reason.
- Runtime status, protocol, version, and metadata are refreshed when an already
  imported runtime is selected and imported again.
- Preserve existing behavior for Harness, zip runtime, Claude SDK container, and
  Studio agents.
- Add English and Chinese UI strings with parity.

## Acceptance Criteria

- [ ] Agent Management offers a fourth "Discover existing runtimes" entry and a
      query-parameter discovery sub-view.
- [ ] A scan returns all pages from the configured Region and marks already
      managed resources.
- [ ] HTTP/A2A candidates can be batch-imported; MCP candidates cannot.
- [ ] Re-importing a discovered Runtime updates one existing row and never creates
      a duplicate.
- [ ] A Runtime already represented by a Launchpad-created agent remains that
      agent and is reported as already managed.
- [ ] Imported rows appear in all management lists with AWS protocol/version
      metadata and a distinct method chip.
- [ ] Edit, re-publish, conversion, experiments, canaries, and managed evaluation
      remain unavailable for imported rows.
- [ ] Chat and `/v1` expose only imported rows whose invoke capability is eligible.
- [ ] Removing an imported row performs no AgentCore delete/update API call.
- [ ] AWS failures use typed API errors; per-runtime inspection/import failures do
      not hide successful candidates or imports.
- [ ] Backend tests cover pagination, sanitization, protocol eligibility,
      idempotency, existing-agent matching, metadata refresh, and detach-only
      deletion.
- [ ] `make verify` passes.

## Notes

- "Current Region" means `get_settings().region`; the backend's existing
  `control_client()` remains the only AgentCore control client factory.
- Importing an arbitrary HTTP runtime does not prove its application payload
  contract. Invocation is best-effort against Launchpad's standard prompt
  envelope; discovery itself performs no probe invocation.
