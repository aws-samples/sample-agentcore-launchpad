# Harness → Runtime Conversion

> One-click conversion of a managed-harness agent into a NEW zip_runtime
> agent so it can run experiments (config-bundle A/B). Introduced 2026-07-13
> (task 07-13-harness-to-runtime). Companion to
> `evaluation-agent-eligibility.md` (why harness is experiment-excluded).

## Scenario: converting a harness agent

### 1. Scope / Trigger

- New API + AgentSpec fields + deployer extension; anything touching
  `app/services/harness_convert.py`, `AgentSpec.code_bundle`, or the
  convert endpoint.

### 2. Signatures

```text
POST /api/agents/{agent_id}/convert            → 202 {"agent", "job_id", "deployment_id"}
```

```python
# app/services/harness_convert.py
resolve_agentcore_cli() -> str                       # managed 0.21.1 path or AppError
export_harness(harness_arn) -> dict[str, str]        # managed CLI, scratch project
graft_config_bundle(main_py) -> str                   # raises ConversionError on anchor miss
graft_direct_kb_tools(main_py) -> str                 # tools=[] import/register graft
discover_env(files) -> dict[str, str | None]          # wired value | None (degrades)
flatten_requirements(files, base) -> list[str]        # pyproject deps minus base pins
build_conversion_spec(source, files, base, name) -> AgentSpec
# AgentSpec additions (schemas/agent.py):
code_bundle: dict[str, str] | None      # relpath→content, main.py required, ≤64 files/1MB,
                                        # safe relpaths only, XOR with code
source_harness: dict[str, str] | None   # {agent_id, agent_name, harness_arn}
conversion_notes: dict[str, str] | None # per-capability wiring outcome (UI renders)
# deployer (zip_runtime.py): _generate_code returns bundle main.py;
# write_bundle_files() stages the rest via the on_pkg_ready hook
```

### 3. Contracts

- Conversion is **sync-in-request** for export+graft (~10-15s; the CLI needs
  a project cwd — reusable scratch under `data/harness-export/`), then the
  bundle is **materialized into the spec** and deployed by the untouched
  async zip pipeline. Deploy resume never re-runs the CLI (spec carries all
  files).
- `make bootstrap` owns the conversion CLI. It installs exactly
  `@aws/agentcore@0.21.1` beneath `data/agentcore-cli/`, verifies
  `node_modules/.bin/agentcore --version`, and skips npm when the managed
  binary already reports `0.21.1`. Conversion always invokes that absolute
  managed path; it never falls back to an `agentcore` executable on `PATH`.
  A missing or non-executable managed binary raises
  `agent.convert_cli_missing` with a `make bootstrap` instruction.
- **The graft is mandatory**: exported code bakes `DEFAULT_SYSTEM_PROMPT`
  and never reads `get_config_bundle()` — without the graft, experiment
  A/B variants no-op exactly as on the harness. Anchors (CLI 0.21.x):
  the `DEFAULT_SYSTEM_PROMPT = """…"""` constant and the
  `system_prompt=DEFAULT_SYSTEM_PROMPT` construction site. Anchor miss →
  conversion FAILS (no agent row).
- Tool-description grafting parses exported `main.py` as Python and requires
  exactly one assignment whose value is
  `get_or_create_agent(session_id, user_id, ...)`. CLI 0.21.1 emits the
  two-argument form without Skills and the three-argument
  `get_or_create_agent(session_id, user_id, _skill_plugins)` form when Agent
  Skills are attached. Both are supported; malformed Python, unrelated
  assignments, ambiguous construction sites, inline/semicolon statement
  sharing, or misplaced owned apply calls fail before persistence.
- Fidelity: prompt (bundle-overridable) + inline tools + memory
  (`MEMORY_MEMORY_*_ID` env wired from `settings.resources.memory_id`).
  A source Harness's `knowledge_bases` are also copied into the new spec and
  remounted through the Runtime's direct retrieval channel: the reusable
  `direct_kb_tools.py.tmpl` is materialized as `launchpad_kb_tools.py`, and
  `kb_search` + `kb_deep_search` are appended to the exported `tools = []`.
  That collection is a mandatory KB-only anchor; if it disappears, conversion
  fails before persistence.
- A KB-bearing conversion's fallback prompt is the original source prompt plus
  the direct-tool `## Knowledge bases` section, never the exported
  `...___Retrieve` Gateway prompt. Generated KB descriptions seed both
  config-bundle defaults beneath explicit source overrides.
- **`GATEWAY_*_URL` is never wired**: the exported MCP client no-ops when
  the URL is absent but crashes at import when the URL is set and the M2M
  token fetch fails — wiring it needs verified AgentCore Identity access
  for the NEW runtime's workload identity (future work). Every outcome is
  recorded in `conversion_notes` and rendered in agent detail.
- Naming: `{source}-rt`, then `-2`, `-3`… against non-deleted names.
- pyproject deps are flattened into `spec.requirements` minus base-pin
  package names (base wins; export set verified pure-python/aarch64).

### 4. Validation & Error Matrix

| Condition | Error |
|---|---|
| source not found / deleted | 404 `agent.not_found` |
| source not (harness ∧ active) | 400 `agent.convert_unsupported` |
| conversion of same source still deploying | 409 `agent.convert_in_flight` |
| managed AgentCore CLI absent or unusable | 502 `agent.convert_cli_missing`; run `make bootstrap` |
| export/config or direct-KB graft failure (anchor miss, CLI error) | 502 `agent.convert_failed`, no row |
| bundle w/o main.py, unsafe path, >64 files/1MB, code+code_bundle | 422 (pydantic) |

### 5. Good/Base/Bad Cases

- **Good**: convert `aurora-support` → `aurora-support-rt` deploying in ~15s
  request time; notes = prompt wired / tools carried / memory wired /
  kb_gateway not wired. If the source has KBs, the new spec retains them,
  carries `launchpad_kb_tools.py`, and records the direct-channel replacement;
  the agent is experiment-selectable when active.
- **Base**: converting again while the first deploys → 409; after it lands,
  a re-convert yields `-rt-2`.
- **Bad**: CLI codegen drift removes anchors → 502 with "graft anchor
  missing", zero side effects.

### 6. Tests Required

`backend/tests/test_bootstrap_script.py`: exact-version skip, absent probe,
pinned npm install, post-install verification, and npm failure propagation with
all subprocesses mocked. `backend/tests/test_harness_convert.py`: managed-path
command construction; config and direct-KB grafts on the REAL export fixture
(`tests/fixtures/harness_export_main.py`); two-argument and Skill-bearing
three-argument agent construction; idempotence, malformed-Python,
unrelated/ambiguous assignments, misplaced apply calls, and anchor-miss
failures; CLI timeout and malformed-result boundaries; KB prompt/default/env/spec
shape; KB-less bundle compatibility; env discovery (memory wired, gateway None,
AWS_REGION skipped); requirements flattening; code_bundle staging; endpoint
guards and clean failures with no leftover rows. `test_strands_template.py` proves
generated ZIP `main.py` inlines the same standalone-compilable direct source.

### 7. Wrong vs Correct

#### Wrong
```python
# deploy the export verbatim — experiments would silently no-op
spec.code_bundle = exported_files
# or: wire the gateway URL "for fidelity"
env["GATEWAY_GATEWAY_X_URL"] = url   # M2M failure now crashes at import
```

#### Correct
```python
if source_kbs:
    grafted["launchpad_kb_tools.py"] = render_direct_kb_source(source_kbs)
    grafted["main.py"] = graft_direct_kb_tools(files["main.py"])
grafted["main.py"] = graft_config_bundle(grafted["main.py"])  # A/B-able or fail
env = discover_env(grafted)          # memory wired, gateway stays None + noted
```

> **Warning**: the exported code's model id is baked (whatever the harness
> used); the exec role must be able to invoke it. The scratch export dir under
> `data/harness-export/` is a cache — safe to delete anytime. Deleting
> `data/agentcore-cli/` removes a bootstrap-owned prerequisite; rerun
> `make bootstrap` before the next conversion.
