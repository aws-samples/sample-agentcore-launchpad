# Design — direct-Retrieve KB mounting for container + Strands ZIP

## 1. Two mount channels, one selection model

`AgentSpec.knowledge_bases: list[KnowledgeBaseRef]` stays the single source of truth (kb_id
+ name + description). What changes is that the *mount channel* is now derived from the
method:

| method | channel | tool the model calls | retrieval shape |
|---|---|---|---|
| `harness` (方式B) | `launchpad-kb-gw` gateway attached as an `agentcore_gateway` tool, OAuth CLIENT_CREDENTIALS | `<target>___Retrieve`, `agentic-<agent>___AgenticRetrieveStream` | single-shot + agentic multi-step |
| `zip_runtime` (protocol=http) | generated code calls `bedrock-agent-runtime:Retrieve` with the runtime execution role | `kb_search` (native Strands `@tool`) | single-shot |
| `container` (方式A) | same API, exposed to the claude CLI as an in-process SDK-MCP tool | `mcp__launchpad_kb__kb_search` | single-shot |
| `studio`, `protocol=a2a` | **unsupported** (out of scope) | — | — |

Nothing about the harness path changes. No gateway target is created for zip/container
agents — that is the whole point of the direct channel.

## 2. Backend contracts

### 2.1 `AgentSpec` validator (`backend/app/schemas/agent.py`)

`_kb_needs_harness` → `_kb_method_supported`:

```python
KB_METHODS = {"harness", "zip_runtime", "container"}

if self.knowledge_bases and self.method not in KB_METHODS:
    raise ValueError(
        "knowledge_bases are supported by the harness, zip_runtime and container "
        "methods; the Strands Studio canvas (studio) has no retrieval tool contract yet"
    )
if self.knowledge_bases and self.protocol == "a2a":
    raise ValueError("knowledge_bases are not supported by protocol=a2a runtimes")
```

Two separate errors so the 422 body names the actual constraint. The field comment is
updated to describe both channels.

### 2.2 New shared module `backend/app/templates/kb_support.py`

Both renderers need the same two derivations, and both templates need the same retrieval
semantics. Keeping this in one module means the prompt wording and the passage formatting
cannot drift between 方式A and ZIP.

```python
KB_TOOL_NAME = "kb_search"
KB_MCP_SERVER = "launchpad_kb"          # container: mcp__launchpad_kb__kb_search
DEFAULT_RESULTS = 8

def mounted_kbs(spec: AgentSpec) -> list[dict[str, str]]:
    """AgentSpec.knowledge_bases → the literal baked into the template."""
    # [{"kb_id", "name", "description"}] — description falls back to name

def kb_tool_description(kbs) -> str:
    """One-line tool description that names the mounted KBs (also the
    config-bundle-tunable default for the Strands A/B contract)."""

def kb_prompt_section(kbs, tool_name: str) -> str:
    """'## Knowledge bases' block: which KB holds what, which tool to call,
    ground-and-cite instruction. Same shape as harness._kb_prompt but naming the
    direct-retrieve tool. Returns '' for no KBs."""
```

`harness._kb_prompt` is left alone (it names gateway tools) — the two functions are
deliberately separate, with cross-referencing comments.

### 2.3 Strands template (`app/templates/strands_agent/`)

`main.py.tmpl` gains a KB block that is inert when no KB is mounted:

```python
MOUNTED_KBS: list[dict[str, str]] = __LAUNCHPAD_MOUNTED_KBS__   # [] when none
KB_RESULTS = 8

def _kb_runtime():
    return boto3.client("bedrock-agent-runtime", region_name=os.environ.get("AWS_REGION", "us-west-2"))

def _format_passages(results) -> str: ...      # "[1] score=0.42 · s3://…\n<text>"

@tool
def kb_search(query: str, kb_id: str = "") -> str:
    """<rendered kb tool description>"""
    ...  # resolve kb_id (default: every mounted KB, in order), retrieve, format
```

- `kb_id=""` → query **every** mounted KB and concatenate labelled blocks (mounting 2 KBs
  must not require the model to guess an id); an explicit `kb_id` restricts to it, and an
  unknown id returns a readable "not mounted" line listing the valid ones.
- Errors: `ClientError` / any exception → `"knowledge base search failed: <type>: <msg>"`
  string, never a raise — a broken KB must not kill the turn.
- `build_agent()` appends `kb_search` to `tools` only when `MOUNTED_KBS`.
- `DEFAULT_TOOL_DESCRIPTIONS["kb_search"]` is populated from the renderer so the existing
  config-bundle A/B contract (`resolve_tool_description`) can tune it — this is why the
  description is generated, not hardcoded in the template.
- `DEFAULT_SYSTEM_PROMPT` = `spec.system_prompt + kb_prompt_section(...)`, done in
  `render_main_py` (so `resolve_system_prompt()`'s bundle-override path is untouched).

Placeholder `__LAUNCHPAD_MOUNTED_KBS__` is rendered with `repr(...)`, matching the existing
placeholder convention. Rendered output must always compile — the existing
`test_strands_template` compile assertion covers it.

`requirements.txt` is unchanged: boto3 is already present transitively (the template
already imports it for memory) and `bedrock-agent-runtime` is a boto3 client name, not a
package.

### 2.4 Container template (`app/templates/claude_sdk_agent/`)

The claude CLI runs as a subprocess and cannot see Python functions, so the tool is
registered as an **in-process SDK MCP server**, which the SDK bridges into the CLI:

```python
MOUNTED_KBS: list[dict[str, str]] = __LAUNCHPAD_MOUNTED_KBS__
KB_TOOL_DESCRIPTION = "__…__"     # rendered

@tool("kb_search", KB_TOOL_DESCRIPTION, {"query": str, "kb_id": str})
async def kb_search(args): -> {"content": [{"type": "text", "text": ...}]}

def _kb_mcp_servers() -> dict[str, Any]:
    return {"launchpad_kb": create_sdk_mcp_server(name="launchpad_kb", tools=[kb_search])} if MOUNTED_KBS else {}
```

- `build_options()` merges `MCP_SERVERS | _kb_mcp_servers()` (the rendered dict stays a
  plain literal; the SDK server object can only be built at runtime).
- The blocking boto3 call runs through `asyncio.to_thread` — the entrypoint is async.
- Renderer (`render_main_py`): when `spec.knowledge_bases`, append `"mcp__launchpad_kb"` to
  `ALLOWED_TOOLS` (server-level allow, same convention as registry MCP chips) and append
  `kb_prompt_section` to `SYSTEM_PROMPT`.
- Tool-call telemetry needs nothing new: `tracing.record_tool_call` already strips the
  `mcp__<server>__` prefix (`name.split("__")[-1]`), so `kb_search` shows up in the
  Observability trace like any other tool.

### 2.5 Deployers

- `zip_runtime.py` / `container.py`: **no stage changes**. The KB refs are baked into the
  generated code at `generate`, so a re-publish (which always re-renders) is what
  attaches/detaches — same as prompt or tool edits.
- `knowledge._strip_kb_from_agents`: gate the `sync_agentic_target` call on
  `(agent.spec or {}).get("method") == "harness"`. Today it would create a gateway
  agentic target for a zip/container agent that still has other KBs after a force-delete.
- `harness.delete_agent_resources` already only touches harness rows.

### 2.6 IAM (`infra/stacks/base_stack.py`)

New statement on `exec_role`, mirroring the gateway role's grant:

```python
exec_role.add_to_policy(iam.PolicyStatement(
    sid="ManagedKbRetrieval",
    actions=["bedrock:Retrieve", "bedrock:GetKnowledgeBase"],
    resources=[f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*"],
))
```

`bedrock:AgenticRetrieveStream` is deliberately **not** granted — the direct channel does
not use it, and it cannot be resource-scoped.

Requires `make bootstrap` (CDK deploy) before retrieval works. Existing deployments that
skip it get `AccessDeniedException`, which `kb_search` surfaces as a readable line.

## 3. Frontend

`frontend/src/pages/CreateAgent.tsx`:
- delete the `method !== "harness" → setSelectedKbs([])` effect (all three wizard methods
  now support KBs; the studio canvas is a different page);
- `...(selectedKbs.length ? { knowledge_bases: … } : {})` — drop the `method === "harness"`
  guard at the submit site;
- render the chip list for every method (drop the `kbSoon` branch); keep `kbEmpty`;
- the `[i]` note becomes method-dependent: `kbNote` (harness · gateway tools + agentic
  retrieval) vs `kbNoteDirect` (zip/container · generated `kb_search` tool, single-shot,
  needs a re-publish to change).
- `create.configure.kbSoon` becomes unused → remove the key from both locales.

New/changed i18n keys in `en/common.json` + `zh-CN/common.json` (parity enforced by
`scripts/i18n_check.py`): `create.configure.kbNoteDirect` added, `kbSoon` removed,
`kbNote` reworded to say "harness".

`src/lib/api.ts` already carries `knowledge_bases` on the spec type — no change.

## 4. Test plan

Hermetic (in `make verify`):
- `tests/test_strands_template.py`: KB tool + prompt section + kb ids present with 2 KBs;
  compiles; absent with 0 KBs. Exercise the rendered `kb_search` by exec'ing the module
  with a stubbed boto3 client (module-level `_kb_runtime` monkeypatch) for: multi-KB
  default fan-out, explicit kb_id, unknown kb_id, ClientError → readable string.
- `tests/test_claude_sdk_template.py`: `MOUNTED_KBS`, `mcp__launchpad_kb` in
  `ALLOWED_TOOLS`, prompt section, compiles, and unchanged output with 0 KBs.
- `tests/test_agents_api.py` (or the schema test): accept zip/container, reject studio and
  `protocol=a2a` with the specific messages.
- `tests/test_knowledge_kb.py`: force-delete with a zip agent mounted → no
  `sync_agentic_target` call; with a harness agent → still called.
- `infra/tests/`: synth assertion for `bedrock:Retrieve` on the agent execution role.

Real AWS (not in verify, evidence into `research/`):
- `make bootstrap` to land the IAM statement.
- deploy `kb-direct-zip` (zip_runtime) and `kb-direct-container` (container) with an
  existing managed KB mounted; invoke each with a question only the indexed document can
  answer; capture job log + answer + the tool call in the trace.

## 5. Compatibility / rollback

- Additive: specs without `knowledge_bases` render byte-identical code, so no existing
  agent's behaviour changes and no rebuild is forced.
- The IAM statement is additive on a shared role.
- Rollback = revert the commit + `make bootstrap`; zip/container agents already deployed
  with a KB keep working until re-published (their code is self-contained), they just lose
  the ability to re-publish with KBs.
