# AgentSkills contract evidence

## Current SDK behavior

Checked against the current Strands documentation on 2026-08-04:

- `AgentSkills` can be imported from `strands` or
  `strands.vended_plugins.skills`.
- It is registered through `Agent(plugins=[AgentSkills(...)])`.
- `skills` accepts a single Skill directory, a parent directory whose child
  directories contain Skills, or a list of paths/Skill objects.
- Skills use progressive disclosure: metadata is available up front and full
  instructions are loaded through the Skill tool only when activated.

Documentation:
`https://strandsagents.com/docs/user-guide/concepts/plugins/skills`

## Repository-verified dependency and layout

The archived Studio integration research verified the same contract against the
Strands 1.47.0 wheel:

- `strands.__init__` exports `AgentSkills`.
- Skills need no optional package extra; the current
  `strands-agents[otel]>=1.0,<2` base requirement is sufficient.
- AgentCore zip artifacts must place bundles at
  `skills/<skill-name>/SKILL.md` next to `main.py`.
- Nested files remain below the Skill directory.

Source:
`.trellis/tasks/archive/2026-07/07-11-studio-canvas-pr31-sync/research/pr31-canvas-delta.md`

## Design consequence

Platform-generated HTTP and A2A zip templates can point `AgentSkills` at the
artifact's parent `skills/` directory. Packaging remains responsible for
including only the explicitly selected `AgentSpec.skills` prefixes. No new
runtime dependency or per-request Registry lookup is required.

## Live A2A packaging finding

The first real A2A validation build on 2026-08-04 failed before upload:

```text
No matching distribution found for greenlet==3.5.4
```

`uv pip compile` had accepted that release's source distribution even though
the following pip install is `--only-binary=:all:` for Python 3.13 ARM64
manylinux2014. Recompiling the identical input with
`uv pip compile --only-binary=:all:` selected `greenlet==3.2.5`, which has a
compatible wheel. The implementation therefore aligns the resolver with the
installer instead of pinning one incidental transitive dependency. Harness
conversion's `resolve_pins` pre-pass carries the same wheel-only target so it
cannot persist a direct sdist-only pin that the package resolver will reject.

## Live A2A startup finding

After packaging was fixed, the first A2A invocation returned AgentCore 424.
CloudWatch showed that current Strands calls `agent_factory("__agent_card__")`
while constructing `A2AServer`. The template attempted to create an
`AgentCoreMemorySessionManager` with that value, but Memory session ids must
start with an ASCII alphanumeric character. The template now attaches Memory
only when the context id matches `[a-zA-Z0-9][a-zA-Z0-9-_]*`; the internal card
agent remains stateless while real Launchpad context ids retain persistence.
