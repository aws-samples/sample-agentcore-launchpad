# Design - Registry Skills for platform-generated zip runtimes

## 1. Existing gap

The catalog and storage layers already provide everything required:

```text
APPROVED Registry AGENT_SKILLS
  -> GET /api/registry/attachables
  -> {name, description, path=s3://.../skills/<name>/}
```

The missing links are method gates:

1. `CreateAgent.tsx` renders/submits `skills` only for Harness/container.
2. `zip_runtime.bundle_skills()` is a no-op unless `method == "studio"`.
3. The platform HTTP/A2A templates do not initialize `AgentSkills`.

No API, schema, ledger migration, Registry mutation, or invoke-path change is
needed.

## 2. Data flow

```text
Registry attachables (APPROVED only)
  -> Create Agent Skill picker
  -> AgentSpec.skills: list[s3-prefix]
  -> per-agent IAM policy grants selected prefixes
  -> zip package stage downloads each prefix
  -> deployment_package.zip/skills/<name>/**
  -> generated HTTP or A2A template discovers the local skills root
  -> Agent(plugins=[AgentSkills(...)])
  -> model activates a Skill on demand
```

Registry is not consulted during invocation. The deployed artifact is a
snapshot, so a Registry reimport requires agent re-publish.

## 3. Frontend contract

Extend the two existing method guards in `CreateAgent.tsx`:

- render the Skill picker for `harness | container | zip_runtime`;
- include `{skills}` in `buildSpec()` for those methods when non-empty.

The existing state already satisfies edit behavior:

- `skills` holds selected S3 prefixes;
- `startEdit()` restores `spec.skills`;
- custom attached Skills use the same path list;
- `resetForm()` already clears the shared state.

Do not create a second zip-specific picker. For A2A, keep the two surfaces
visually and semantically separate:

- `SKILLS - REGISTRY & CUSTOM` -> mounted runtime bundles (`spec.skills`);
- `AGENT CARD SKILLS` -> A2A routing metadata (`spec.a2a_skills`).

No new user-facing strings are expected; if implementation needs clarifying
copy, add matching English and zh-CN i18n keys.

## 4. Packaging dispatch

Keep one public package hook, `bundle_skills(spec, code, pkg_dir, log)`, but
dispatch by artifact ownership:

```python
if spec.method == "studio":
    # Existing behavior: names are extracted from generated code and resolved
    # against APPROVED Registry records.
    return bundle_skills_into(code, pkg_dir, log)

if spec.method == "zip_runtime" and not spec.code_bundle:
    # New behavior: explicit selected S3 prefixes are the source of truth.
    return bundle_skill_paths_into(spec.skills, pkg_dir, log)

return empty_result
```

This reuses the existing explicit-path consumer already used by the container
deployer. It supports Registry prefixes and the existing custom
`agent-skills/...` prefixes without a second download implementation.

The `code_bundle` guard is load-bearing. Harness conversion stores S3 Skill
sources in `spec.skills` to obtain IAM grants, while the exported
`skills/fetcher.py` retrieves them at request time. Snapshotting those paths
again would duplicate content and change an already verified runtime contract.

The existing log-and-continue download policy stays unchanged. The package
result continues to list successfully bundled names in the deployment detail.

## 5. Generated template contract

Both platform templates gain the same local-root behavior:

```python
from pathlib import Path
from strands import Agent, tool

SKILLS_ENABLED = __LAUNCHPAD_SKILLS_ENABLED__
SKILLS_ROOT = Path(__file__).parent / "skills"

def skill_plugins() -> list:
    if not SKILLS_ENABLED or not SKILLS_ROOT.is_dir():
        return []
    if not any(
        child.is_dir() and (child / "SKILL.md").is_file()
        for child in SKILLS_ROOT.iterdir()
    ):
        return []
    from strands import AgentSkills

    return [AgentSkills(skills=str(SKILLS_ROOT))]
```

The renderer replaces `__LAUNCHPAD_SKILLS_ENABLED__` with `repr(bool(spec.skills))`.
Passing the parent directory is intentional: current Strands `AgentSkills`
loads child Skill directories from a parent path. The package contains only
the selected bundles, so the local root is the exact deployment snapshot.

HTTP `build_agent()` and A2A `agent_factory()` add `plugins` only when
`skill_plugins()` returns a non-empty list. This avoids an import/startup
failure when every selected download was skipped. The function-local
`AgentSkills` import also means a no-Skill agent does not resolve that symbol,
preserving compatibility for existing artifacts that never use the feature.

`AgentSkills` is part of the existing Strands 1.x base package; no requirement
or lock composition changes are needed.

## 6. Compatibility matrix

| Agent shape | Skill source of truth | Package behavior | Runtime behavior |
|---|---|---|---|
| `zip_runtime`, HTTP, no `code_bundle` | `spec.skills` | explicit prefixes -> `skills/` | generated `AgentSkills` plugin |
| `zip_runtime`, A2A, no `code_bundle` | `spec.skills` | explicit prefixes -> `skills/` | generated `AgentSkills` plugin per context |
| `studio` | generated code references | APPROVED name lookup -> `skills/` | Studio-generated plugin |
| converted `zip_runtime` + `code_bundle` | exported code + `spec.skills` for IAM | no new snapshot | exported fetcher |
| `container` | `spec.skills` | `.claude/skills/` | Claude SDK `Skill` tool |
| `harness` | `spec.skills` | no artifact | native Harness Skill source |

Mounted `AGENT_SKILLS` and `a2a_skills` remain separate. The A2A server still
publishes only `a2a_skills` on its AgentCard.

## 7. Failure behavior

- Registry catalog unavailable at create time: existing empty/error behavior is
  unchanged.
- Selected S3 prefix missing, oversized, or unreadable: log and skip using the
  existing bundler policy; remove partial local content.
- Some Skills succeed and others fail: package the successful subset and list
  them in the deployment stage detail.
- No valid local Skill remains: omit the plugin at runtime.
- Runtime IAM lacks a selected prefix: package-stage download fails visibly in
  job logs; per-agent IAM tests protect the expected policy generation.

## 8. Tests

Backend:

- HTTP renderer replaces all placeholders, compiles, and conditionally builds
  the plugin.
- A2A renderer compiles with mounted Skills and keeps AgentCard skills
  independent.
- Explicit zip paths bundle complete nested directories.
- A no-Skill zip is a no-op.
- `studio` still resolves names from code.
- converted `code_bundle` remains a package no-op even when `spec.skills` is
  populated.
- existing per-agent IAM scope tests remain green.

Frontend/browser:

- zip method displays the catalog and custom-source controls;
- selecting a Skill sends `skills` in `POST /api/agents`;
- edit/re-publish restores and resends the selected path;
- A2A payload contains both independent fields when both are configured.

Real AWS:

- use an APPROVED Skill with a deterministic, distinctive instruction;
- create one HTTP and one A2A zip runtime through Portal;
- wait for ACTIVE, inspect job logs for `skills bundled`, invoke through Chat,
  and capture the response proving Skill activation;
- remove temporary agents/resources unless they are intentionally retained as
  named demo assets.

## 9. Documentation

- Update `docs/architecture.md` method-specific Skill mounting description.
- Add a Launchpad spec for zip-runtime Skill mounting and register it in
  `.trellis/spec/launchpad/index.md`.
- Update `harness-conversion.md`, whose current statement that `spec.skills`
  has only an IAM consumer on the zip path becomes true only for
  `code_bundle` conversions.
- Update `a2a-agents.md` to distinguish mounted Registry Skills from AgentCard
  skills.

All new documentation is written in English.
