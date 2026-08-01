# Harden Harness conversion prerequisites and Skill anchors

## Goal

Make Harness-to-Runtime conversion work predictably on a fresh Launchpad host and
for Harness exports that include Agent Skills.

## Requirements

- `make bootstrap` must install the exact AgentCore CLI version supported by the
  conversion code without requiring a global npm install or elevated permissions.
- Harness export must use the repository-managed CLI rather than whichever
  `agentcore` executable happens to be on `PATH`.
- Missing or unusable managed CLI installations must fail with the existing
  `agent.convert_cli_missing` boundary and an actionable message.
- Config-bundle grafting must support both CLI 0.21.1 invocation shapes:
  `get_or_create_agent(session_id, user_id)` and
  `get_or_create_agent(session_id, user_id, _skill_plugins)`.
- Agent construction-site discovery must remain strict: unrelated assignments or
  malformed generated Python must fail conversion instead of producing a runtime
  whose A/B tool overrides silently do not apply.
- Existing no-Skill, direct-KB, memory, Gateway-degrade, and A/B conversion behavior
  must remain unchanged.
- Setup and conversion specs must state the pinned CLI ownership and the supported
  Skill-bearing export shape.

## Acceptance Criteria

- [x] Bootstrap installs `@aws/agentcore@0.21.1` beneath gitignored `data/` and is
      idempotent.
- [x] Conversion commands resolve to that managed executable and never depend on a
      global AgentCore CLI.
- [x] Unit tests cover missing CLI, command construction, two-argument exports,
      Skill-bearing three-argument exports, idempotence, and anchor misses.
- [x] The complete `make verify` gate passes.
- [x] The diff contains no changes to AWS resources, frontend behavior, or vendored
      Studio code.

## Notes

- Upstream package inspected: `@aws/agentcore@0.21.1`.
- Its Strands HTTP template adds `_skill_plugins` as the third
  `get_or_create_agent` argument when a Harness has Skills.
