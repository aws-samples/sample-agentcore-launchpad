# Design

## Current Gap

Harness export includes a Gateway MCP client and a Gateway-specific prompt.
Conversion deliberately leaves `GATEWAY_*_URL` unset because that client tries
to obtain an M2M token during module import. The converted `code_bundle` also
bypasses `strands_agent.render_main_py`, so merely copying
`AgentSpec.knowledge_bases` would not add Launchpad's direct retrieval tools.

## Decision

Render the existing direct retrieval implementation from one reusable source
template:

- Generated ZIP main modules inline the rendered source, preserving their
  current public module/test contract.
- Harness conversions add the same rendered source as
  `launchpad_kb_tools.py`, then graft imports and tool registration into the
  exported main module.

This keeps one implementation of the AWS request shapes and error handling
without forcing a runtime layout change on existing generated ZIP agents.

## Conversion Flow

1. Read the source Harness `AgentSpec` and mounted KB references.
2. Export Harness source through the pinned AgentCore CLI.
3. If KBs are mounted:
   - render `launchpad_kb_tools.py` with the mounted KB literals;
   - graft imports and append `kb_search` / `kb_deep_search` to the exported
     `tools` collection;
   - build the fallback prompt from the original system prompt plus the direct
     KB prompt section;
   - merge generated KB tool descriptions beneath explicit source overrides.
4. Apply the existing config-bundle graft.
5. Keep Gateway URL variables unset and record the direct replacement in
   `conversion_notes`.
6. Persist the resulting files and KB references in the new `zip_runtime` spec.

## Anchors And Failure Policy

The KB graft requires the exported `tools = []` collection anchor. Missing
anchors raise `ConversionError`; the `/convert` endpoint returns 502 and creates
no Agent row. Silent loss of KB capability is not acceptable.

The graft is idempotent so a future promotion/re-publish can regenerate defaults
without duplicating imports or tool registration.

## Compatibility

- KB-less conversions retain the existing code path.
- Gateway MCP files remain in the exported bundle but are inert because their
  URL variables remain unset.
- Standard ZIP agents keep direct retrieval function names, behavior, and
  config-bundle semantics.
- No database or infrastructure migration is required.

## Verification

- Unit-test reusable direct KB rendering and existing request/error behavior.
- Unit-test KB conversion spec, prompt, descriptions, environment, and anchors.
- Exercise the `/convert` endpoint with a KB-bearing source Harness.
- Run backend focused tests, then `make verify`.
