# Zip Runtime Skills

## Scenario: mounting Registry/custom Skills in generated zip runtimes

### 1. Scope / Trigger

Cross-layer contract between `frontend/src/pages/CreateAgent.tsx`,
`backend/app/deployer/zip_runtime.py`, and the generated HTTP/A2A templates.
Touch this spec when changing zip Skill selection, package dispatch, local
layout, or Strands plugin initialization.

Companion boundaries:

- Registry storage and approval:
  [registry-skill-ingestion.md](./registry-skill-ingestion.md)
- Converted Harness exports:
  [harness-conversion.md](./harness-conversion.md)
- A2A AgentCard metadata:
  [a2a-agents.md](./a2a-agents.md)

### 2. Data Flow and Signatures

```text
APPROVED Registry AGENT_SKILLS or attached custom bundle
  -> Create Agent picker
  -> AgentSpec.skills: list[s3-prefix]
  -> per-agent SkillBundle IAM grants
  -> package-time download to skills/<name>/**
  -> conditional AgentSkills(skills=<package>/skills)
  -> progressive disclosure at invocation
```

```python
# deployer/zip_runtime.py
bundle_skills(spec, code, pkg_dir, log) -> dict
bundle_skill_paths_into(paths, dest_parent, log, *, s3_client=None) -> dict

# templates/strands_agent/__init__.py
render_main_py(spec) -> str

# templates/strands_a2a_agent/__init__.py
render_a2a_main_py(spec) -> str
```

The result shape is
`{"bundled": list[str], "files": int, "bytes": int}`. The package stage adds
successfully bundled names to its persisted detail.

### 3. Contracts

- The wizard exposes the same Registry/custom Skill picker for `harness`,
  `container`, and `zip_runtime`. It submits selected paths as `spec.skills`
  and restores them from stored specs during edit/re-publish.
- Only APPROVED Registry records enter the catalog. Custom inspect/attach
  sources already produce equivalent S3 prefixes and use the same path list.
- A platform-generated `zip_runtime` (`code_bundle is None`) treats
  `spec.skills` as the source of truth. Each prefix is copied to
  `skills/<prefix-tail>/`, preserving nested files.
- Studio remains code-driven. `bundle_skills_into` extracts Skill names from
  generated `AgentSkills` references and resolves those names against APPROVED
  Registry records.
- A converted Harness `code_bundle` is explicitly excluded from snapshot
  packaging even when `spec.skills` is populated. Those paths exist for IAM;
  the exported `skills/fetcher.py` remains the content loader.
- Both generated templates render `SKILLS_ENABLED = bool(spec.skills)` and set
  `SKILLS_ROOT = Path(__file__).parent / "skills"`.
- `skill_plugins()` returns no plugin unless the feature is enabled, the root
  exists, and at least one child directory contains `SKILL.md`. `AgentSkills`
  is imported inside that function, so a no-Skill or fully skipped package
  does not resolve the symbol at module import.
- The plugin receives the parent Skill directory. Strands exposes Skill
  metadata first and loads full instructions/resources only when activated.
- `strands-agents` 1.x already exports `AgentSkills`; no requirement or lock
  change is needed.
- Registry is never queried during invocation. Zip Skill content is a
  deployment snapshot, so Registry edits/reimports require agent re-publish.

### 4. HTTP/A2A Distinction

| Field | Purpose | Runtime effect |
|---|---|---|
| `AgentSpec.skills` | Mounted Registry/custom Skill prefixes | Snapshot under `skills/`; `AgentSkills` plugin |
| `AgentSpec.a2a_skills` | A2A AgentCard routing descriptors | `A2AServer(skills=...)` and Registry card metadata |

The fields are independent. An A2A zip runtime can configure both, one, or
neither. Mounted Skills must never be derived from AgentCard metadata.

### 5. Failure Behavior

- Empty `spec.skills`: package no-op; template does not initialize a plugin.
- Missing, unreadable, invalidly named, or oversized prefix: log and skip that
  Skill; remove partial local content; continue packaging other Skills.
- A path-escaping object is logged and ignored. If no safe file remains, remove
  the empty Skill directory.
- Every selected download skipped: rendered flag remains true, but the local
  directory check returns no plugins, so startup still succeeds.
- Partial success: package the successful subset and list only those names in
  the deployment stage detail.
- IAM mismatch: package logs the failed download. Per-agent policy generation
  remains the authority for selected-prefix access.

### 6. Tests Required

- `backend/tests/test_zip_runtime_deployer.py`: HTTP/A2A explicit-path
  dispatch, nested files, no-Skill no-op, skipped-download cleanup, Studio
  reference dispatch, and `code_bundle` exclusion.
- `backend/tests/test_strands_template.py`: placeholder replacement,
  compilation, no-directory behavior, and plugin creation from a packaged
  Skill.
- `backend/tests/test_a2a_agent.py`: mounted-Skill rendering and independence
  from `a2a_skills`.
- `backend/tests/test_container_skill_bundle.py` and
  `backend/tests/test_agent_iam_policy.py`: shared downloader and selected
  prefix IAM regressions.

### 7. Wrong vs Correct

```python
# Wrong: changes converted Harness runtime semantics and duplicates its fetcher.
if spec.method == "zip_runtime":
    return bundle_skill_paths_into(spec.skills, pkg_dir, log)

# Correct: snapshot only platform-generated zip artifacts.
if spec.method == "zip_runtime" and spec.code_bundle is None:
    return bundle_skill_paths_into(spec.skills, pkg_dir, log)
```

```python
# Wrong: selected-but-failed downloads make the runtime import a broken plugin.
plugins = [AgentSkills(skills=str(SKILLS_ROOT))] if SKILLS_ENABLED else []

# Correct: require materialized Skill content before resolving AgentSkills.
if SKILLS_ENABLED and any(
    child.is_dir() and (child / "SKILL.md").is_file()
    for child in SKILLS_ROOT.iterdir()
):
    from strands import AgentSkills
```
