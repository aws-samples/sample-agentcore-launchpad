# Design

## Boundaries

The change stays inside host bootstrap and the existing Harness conversion service.
It does not change the exported bundle schema, deployment pipeline, AWS resources, or
runtime invocation contract.

## Managed CLI

Bootstrap installs the exact npm package into:

```text
data/agentcore-cli/node_modules/.bin/agentcore
```

using:

```text
npm install --prefix data/agentcore-cli --no-save --package-lock=false
  @aws/agentcore@0.21.1
```

This avoids global npm permissions and PATH/version drift. The install function first
executes the managed binary's `--version`; it skips npm when the output is exactly
`0.21.1`, otherwise it installs and verifies. Bootstrap reports the version in its
summary.

The conversion service builds every CLI command from one resolver. It requires the
managed path and raises `agent.convert_cli_missing` with a `make bootstrap` instruction
when absent. It does not fall back to a global binary because that would reintroduce
non-deterministic code generation.

## Skill-Compatible Graft

The exported `main.py` is valid Python, so assignment discovery uses `ast.parse` rather
than a text regex. A valid construction site is an assignment to the name `agent` whose
value calls `get_or_create_agent` with:

- first positional argument `session_id`;
- second positional argument `user_id`;
- either no third argument or the exact `_skill_plugins` third argument emitted by
  CLI 0.21.1.

The graft inserts `_launchpad_apply_tool_descriptions(agent)` immediately after that
assignment using AST line offsets and the assignment's indentation. The current CLI
0.21.1 no-Skill and Skill variants are both valid; unrelated calls are rejected.
Existing marker-based idempotence remains unchanged.

Syntax errors and missing valid assignments become `ConversionError` at the existing
pre-persistence boundary.

## Testing

- Bootstrap tests mock subprocess execution and cover current, absent, install, and
  failed-verification paths without npm or AWS access.
- Conversion tests retain the checked-in real no-Skill fixture and derive the exact
  CLI 0.21.1 Skill invocation shape for a focused regression.
- Existing clean-failure endpoint tests continue proving that anchor failures leave no
  agent row.

## Rollback

Reverting this change restores global CLI lookup and the fixed two-argument anchor.
No persisted schema or AWS resource migration is involved.
