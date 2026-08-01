# Converted runtime direct managed KB support

## Goal

Preserve managed knowledge-base capability when converting an active managed
Harness agent into a new `zip_runtime` agent, using Launchpad's existing direct
retrieval channel rather than the Harness-only KB Gateway/MCP channel.

## Requirements

- Copy the source Harness agent's `knowledge_bases` into the converted
  `AgentSpec`.
- Materialize `kb_search` and `kb_deep_search` into the converted code bundle.
- Use the same direct retrieval implementation and metadata contract as
  Launchpad-generated ZIP agents.
- Replace the exported Gateway-oriented KB prompt with the direct-tool prompt
  in the converted runtime's config-bundle fallback.
- Seed both KB tool descriptions into the converted runtime's config-bundle
  defaults so Bundle A/B experiments can tune them.
- Keep every exported `GATEWAY_*_URL` unset; converted runtimes must not depend
  on AgentCore Identity M2M token retrieval.
- Preserve existing conversion behavior for agents without knowledge bases.
- Fail conversion before creating an Agent row if the pinned Harness export no
  longer has the code anchors required to register direct KB tools.

## Acceptance Criteria

- [x] A converted Harness with mounted KBs has `method=zip_runtime`, retains the
      same KB references, and contains both direct retrieval tools.
- [x] Its default prompt names `kb_search` and `kb_deep_search`, not Gateway MCP
      target names.
- [x] Its config-bundle defaults include descriptions for both KB tools.
- [x] Gateway environment variables remain absent from `spec.env`.
- [x] A converted Harness without KBs remains byte-for-byte equivalent apart
      from existing config-bundle conversion behavior.
- [x] Existing generated ZIP agents continue to use the same tested direct KB
      behavior.
- [x] Focused backend tests and the canonical `make verify` gate pass.

## Notes

- The source Harness remains untouched; conversion creates a new Runtime agent.
- Direct retrieval uses the shared Runtime execution role and existing
  `bedrock:Retrieve` / `bedrock:AgenticRetrieveStream` grants.
- Gateway policy enforcement and Gateway invocation traces are intentionally
  outside this direct-channel scope.
