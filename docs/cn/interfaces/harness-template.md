# Interface: harness template (N7a → N7)

Frozen 2026-10-09 (contract); vendored <date> by N7a. Spec D1/§3.L: 方式B is a Strands
runtime **rendered** from the code the Global `agentcore export` CLI emits for one
reference managed Harness, then deployed as an ordinary AgentCore Runtime in `aws-cn`.
`backend/app/deployer/harness_local.py` consumes this directory ONLY through the
contract below; `scripts/harness_template_check.py` is the machine check.

## Directory (`backend/app/templates/harness_runtime/`)

| path | origin | role at render time |
|---|---|---|
| `main.py.tmpl` | export `main.py` | entrypoint; every anchor A1–A8 and A11–A12 lives here |
| `model/load.py.tmpl` | export `model/load.py` | **replaced wholesale** by `templates/model_support.render_model_load(spec)` (R4); vendored for the record |
| `memory/session.py.tmpl` | export `memory/session.py` | **replaced wholesale** by `harness_local.MEMORY_SESSION_SHIM` (R8) |
| `skills/fetcher.py.tmpl` | export `skills/fetcher.py` | kept verbatim; resolves A6's S3 prefixes per request |
| `hooks/execution_limits.py.tmpl` | export `hooks/execution_limits.py` | kept verbatim (`ExecutionLimitsHook`, `ExecutionLimitExceeded`) |
| `mcp_client/client.py.tmpl` | export `mcp_client/client.py` (imports-only scaffold: imports `MCPClient`/`streamablehttp_client`, defines no clients) | `harness_convert.graft_missing_gateway_clients` (R9) ADDS `get_all_gateway_mcp_clients()` (and `main.py`'s import + `mcp_clients += …` line) when the spec carries gateway/MCP tools; MUST NOT construct `MCPClient(` directly |
| `requirements.txt.tmpl` | hand-written at export time | the export's `pyproject` `[project].dependencies` frozen as `==` pins (R17); one per line, `#` comments allowed |
| `_cli/strands_base_main.py.hbs` | `node_modules/@aws/agentcore/dist/assets/python/http/strands/base/main.py` | Handlebars source of the `{{#if hasShell}}` / `{{#if hasFileOperations}}` tool blocks (`graft_native_tools`) |
| `MANIFEST.md` | hand-written | export record (below) + redaction log |

Every source file carries the `.tmpl` suffix (ruff / pytest / the client-funnel test skip
it; `scripts/cn_lint.sh` scans `*.tmpl`, so C2 still applies). `__init__.py` files are
not vendored; the renderer writes an empty one into every package directory.
`README.md`, `.gitignore`, `pyproject.toml`, `Dockerfile`, `agentcore.json` are NOT
vendored.

## Reference Harness (what to create in the Global account before exporting)

Upstream `main` code, account 210987654321 / `us-west-2` — one Harness with: any
Bedrock Converse model (irrelevant — `model/load.py` is replaced); a short system
prompt; **Memory enabled** (the workspace memory); **one S3 Skill** attached (any
approved skill); **no** Gateway tool, **no** remote MCP, **no** inline function, **no**
`shell` / `file_operations` builtins (`allowedTools = ["skills"]`); `maxIterations=10`,
`timeoutSeconds=300` (any values — they are rewritten). Delete the reference Harness
after the export.

## Export invocation (Global account, repository-managed CLI — @aws/agentcore 1.0.0-preview.20, the version recorded in MANIFEST.md; anchors are deterministic across 0.21–1.0)

```bash
cd <scratch>/harnessexport            # once: agentcore create --project-name harnessexport --no-agent --json
agentcore --version                   # record in MANIFEST.md
agentcore export harness --arn <harness-arn> --target-agent-name lpexportRefAgent --build CodeZip --json
# "agentPath" → copy every runtime source file to backend/app/templates/harness_runtime/<rel>.tmpl
cp node_modules/@aws/agentcore/dist/assets/python/http/strands/base/main.py \
   backend/app/templates/harness_runtime/_cli/strands_base_main.py.hbs
```

## Anchors (deterministic codegen of @aws/agentcore 1.0.0-preview.20 — the version recorded in MANIFEST.md; anchors are deterministic across 0.21–1.0; the renderer FAILS on a missing one; A11/A12 added by N8 for the memory grafts)

| id | anchor (in `main.py.tmpl` unless noted) | consumer |
|---|---|---|
| A1 | `DEFAULT_SYSTEM_PROMPT = """…"""` constant **and** `system_prompt=DEFAULT_SYSTEM_PROMPT` | `harness_convert.graft_config_bundle` |
| A2 | module-scope line `tools = []` | `graft_direct_kb_tools`, `graft_native_tools`, `graft_tool_module` (builtins; and the A2A consumer module — see §A2A consumer graft) |
| A3 | exactly one `agent = get_or_create_agent(session_id, user_id, _skill_plugins)` statement on its own line (the renderer also accepts the three sibling shapes `_agent_assignment` lists) | `graft_config_bundle` (apply-call insertion) |
| A4 | `main.py.tmpl` has a module-scope `mcp_clients = []`; `mcp_client/client.py.tmpl` exists, imports `MCPClient`/`streamablehttp_client`, and contains NO `MCPClient(` constructor call; it need not define `get_all_gateway_mcp_clients()` — `harness_convert.graft_missing_gateway_clients` ADDS the function (and the import + `mcp_clients += …` line in main) when the spec carries gateway/MCP tools, and must not duplicate it if a future export already defines it | `graft_missing_gateway_clients`, `graft_lazy_gateway_token`, `graft_gateway_softfail` |
| A5 | one `@app.entrypoint` `async def invoke(payload, context)` whose body has `prompt = _extract_prompt(payload)` on its own line before the single `agent.stream_async(prompt)` loop | `graft_runtime_attachments` |
| A6 | one list literal `s3_skill_sources = [ … ]` (contents sanitized to `["s3://__LAUNCHPAD_SKILLS__/"]` when vendoring) | `render_skill_sources`, `discover_skills` |
| A7 | `user_id = getattr(context, 'user_id', 'default-user')` | `graft_actor_from_payload` |
| A8 | `ExecutionLimitsHook(` with `max_iterations=<int>,` and `timeout_seconds=<int>,` keywords; `timeout_seconds = <int>` assignment inside `invoke` | `render_loop_bounds` |
| A9 | `from model.load import load_model` in `main.py.tmpl`; `model/load.py.tmpl` exists | `render_model_load` (replacement) |
| A10 | `from memory.session import get_memory_session_manager` in `main.py.tmpl`; `memory/session.py.tmpl` exists | `MEMORY_SESSION_SHIM` (replacement); `harness_convert.graft_memory_import` inserts the soft `from launchpad_memory import make_memory_hooks, recall_context` block right after this line (N8) |
| A11 | exactly one `Agent(…, hooks=[ … ])` list literal spanning several lines (closing `]` on its own line) inside a factory `def get_or_create_agent(session_id, user_id, …)` — the export emits `hooks=[ExecutionLimitsHook(max_iterations=…, timeout_seconds=…)]` | `harness_convert.graft_memory_hooks` APPENDS `*make_memory_hooks(user_id, session_id)` as the last element (never replaces the `ExecutionLimitsHook`; `[]` at runtime without `LAUNCHPAD_MEMORY_ID`) — memory-runtime.md §6 step 2 |
| A12 | inside the A5 entrypoint, `agent = get_or_create_agent(session_id, user_id, …)` precedes `prompt = _extract_prompt(payload)` (both on their own statement lines) | `harness_convert.graft_memory_recall` inserts, right after the prompt line (before the A5 attachments block), `recall_context(user_id, <user text of the turn>)` and, when non-empty, `agent.system_prompt = f"{resolve_system_prompt()}\n\nKnown user context:\n{known}"` — memory-runtime.md §6 step 3; requires the A1 graft first |

## Redaction rules (vendored text; `scripts/cn_lint.sh` and the self-check enforce them)

- no `arn:aws:` / `.amazonaws.com` literal, no 12-digit account id, no gateway id,
  no memory id, no bucket name: `s3_skill_sources` → `["s3://__LAUNCHPAD_SKILLS__/"]`;
  `MEMORY_MEMORY_*` env lookups may stay (the file is replaced anyway);
- regions: none hard-coded — exported code already reads `AWS_REGION`; if a literal
  region appears, replace it with `os.environ.get("AWS_REGION", "cn-northwest-1")`;
- the system prompt in A1 is replaced by `"""__LAUNCHPAD_REFERENCE_PROMPT__"""`;
- consequently the substring `__LAUNCHPAD_` is reserved: a spec whose system prompt or tool
  descriptions contain it is refused at render time (`HarnessRenderError`), never shipped;
- `MANIFEST.md` records: export date, `agentcore --version`, Harness config summary
  (memory on, skill on, no gateway/natives/inline), every redaction performed.

## Self-check (G(N7a))

```bash
cd backend && uv run python ../scripts/harness_template_check.py
# expect: harness_template_check: OK · 7 files · anchors A1–A12 · render+compile OK
cd .. && bash scripts/cn_lint.sh         # expect cn_lint: OK
```

`harness_template_check.py` accepts only `--template PATH` (defaulting to
`backend/app/templates/harness_runtime`) — there is no environment-variable override.

The D20 P2-a policy-guard graft adds no anchor and no vendored file (the guard module is
written beside `launchpad_memory.py` only when the guard is on), so the self-check's
`7 files · anchors A1–A12` line is unchanged; `tests/test_policy_bundle_render.py` renders
the real template with `policy_guard=True`, checks the A11 append and the three drains, and
asserts the off-render is byte-identical.

## A2A consumer graft (N11 r3-C, N10v2-bug5)

A spec that mounts other Launchpad agents (`ToolRef.type="a2a"`, see
registry-backend.md §A2A consumer path) gets one more sibling module in the bundle,
`launchpad_a2a_tools.py` — the same source `templates/a2a_support.render_a2a_tools_source`
inlines into the Strands template — and one more A2 graft:
`graft_tool_module(start="# <launchpad-a2a-tools:v1>", end="# </launchpad-a2a-tools:v1>",
import_line="from launchpad_a2a_tools import A2A_TOOLS", tool_names=["*A2A_TOOLS"])`, which
renders `tools.extend([*A2A_TOOLS])` right after the builtin graft and before the config
bundle (A1/A3). The module defines one plain `@tool def ask_<slug>(message)` per peer, calls
the peer Runtime through `bedrock-agentcore:InvokeAgentRuntime` (A2A JSON-RPC `message/send`,
signed with the bundle's own execution role — `agent_iam` Sid `A2AInvokeMountedPeers`), and
derives the peer `runtimeSessionId` from `BedrockAgentCoreContext.get_session_id()` (the
Harness entrypoint has no bind hook; the Strands template binds per turn). The generated
descriptions join `DEFAULT_TOOL_DESCRIPTIONS` and the prompt gains the `## Other agents`
section, like the KB tools. No anchor changed and the module lives outside
`harness_runtime/` (the self-check still counts 7 vendored files); `check_template`'s probe
spec now mounts one peer so the gate renders and compiles the graft. `bundle.notes["a2a_agents"]`
reports it. `strands_tools.a2a_client` is deliberately not used — it speaks raw HTTP A2A at a
URL and cannot reach an AWS_IAM AgentCore peer.

## How N7 renders it (summary; authoritative code in `deployer/harness_local.py`)

load files (strip `.tmpl`, add `__init__.py`) → KB tools (A2) → native tools (A2,
`_cli/*.hbs`) → builtin tools (A2) → config bundle (A1, A3) → attachments (A5) → actor
(A7) → memory import + hooks append + recall prepend (A10–A12, `graft_memory`) → **policy guard** (D20 P2-a, only when the deployer pinned a bundle — `render_harness_bundle(..., policy_guard=True)` from `Narrowing.guard_rendered`: `graft_policy_guard` = soft import after the memory import graft, `*policy_guard_hooks(session_id)` appended to the A11 list after the memory hooks, and a drain of the guard's `policy_denied` / `policy_decision` frames before every `yield event` of `invoke` / `_launchpad_recover_max_tokens`; writes `launchpad_policy_guard.py` = `policy_guard.py.tmpl`; no new anchor, byte-identical bundle when off — policy-backend.md §"Runtime guard") → loop bounds (A8) → skills (A6) → gateway / remote-MCP client construction (the
export's scaffold defines no clients; `graft_missing_gateway_clients` adds
`get_all_gateway_mcp_clients()` and the import + `mcp_clients += …` line in `main.py`
only when the spec needs one) + soft-fail (A4) → replace `model/load.py` (A9) and
`memory/session.py` (A10) → write `launchpad_memory.py` (= `from model.load import
_resolve_bedrock_api_key, MODEL_REGION, BEDROCK_BEARER_ENV` + `memory_support.render_memory_source()`,
unconditionally — the library is inert without `LAUNCHPAD_MEMORY_ID`; never a second copy of
the model block, C4) → requirements = platform base + `requirements.txt.tmpl` + spec extras.

## Builtin tools (R10 code-interpreter; N15-obs16 browser)

An AgentCore builtin tool config (`ToolRef type=builtin`) is rendered as ONE sibling module
per builtin, grafted on A2 with its own markers (`graft_tool_module`; shape of the KB / A2A
grafts) and imported into `tools.extend([...])`. The table is
`deployer/harness_local.BUILTIN_MODULES`, one entry per key of
`harness_tool_access.BUILTIN_RUNTIME_TOOLS` (a test asserts parity):

| builtin | bundle module | template | callable the model sees | extra pins | IAM (agent_iam Sid) |
|---|---|---|---|---|---|
| `code-interpreter` | `launchpad_builtin_tools.py` | `harness_builtin_tools.py.tmpl` | `code_interpreter` (strands-agents-tools `AgentCoreCodeInterpreter`, runtime region) | `strands-agents-tools>=0.8.3,<1` | `AgentCoreCodeInterpreter` |
| `browser` | `launchpad_browser_tool.py` | `harness_builtin_browser.py.tmpl` | `browser(url)` — starts an `aws.browser.v1` session in the runtime's region (`BrowserClient`, `sessionTimeoutSeconds` 300), drives it over CDP on the session's automation stream with the `websockets` client `bedrock-agentcore` already pins (no Playwright), navigates once, returns `title / url / visible text` (≤ 20 000 characters, truncation noted) and ALWAYS stops the session (`finally`); one `browser error: …` line on any failure, never a traceback; non-http(s) URLs refused before a session exists; nothing written to disk, no screenshots | — | `AgentCoreBrowser` |

Limits of the browser tool: `title` ≤ 300 and `url` ≤ 2048 characters (bounded in the page
expression and again in Python — a page controls all three fields, rev-r16); hosts that are a
literal IP (loopback, link-local incl. the instance metadata address, RFC1918 / unique-local or
any other), `localhost` or `*.localhost` / `*.internal` / `*.local` are refused with
`browser error: host not allowed` before a session exists; one navigation per call (no clicks, forms or screenshots);
connect budget 20 s, navigation budget 30 s (a page that never fires `load` is read after the
budget); the text is `document.body.innerText`. Partition note (SDK 1.17.0):
`BrowserClient.generate_ws_headers()` builds the stream host from a Global-only suffix, so the
module signs the stream URL itself (SigV4, service `bedrock-agentcore`) from the boto3
data-plane endpoint the client resolved for its region — valid in `aws-cn`; the self-check's
redaction rules apply to the template. The hub's console demo (`POST /api/demos/browser`) signs
its stream and live-view URLs the same way in `services/agentcore/browser_stream.py` (N15-obs17 —
the SDK's `generate_live_view_url` has the same Global-only host); the template must render
standalone, so it cannot import that module, and `tests/test_browser_stream.py` asserts the two
resolve the same stream URL and SigV4 scope for an aws-cn client so they cannot drift.

**Fail-early rule (N15-obs16 → round 16).** A `harness` spec whose builtin is not a
`BUILTIN_RUNTIME_TOOLS` key is refused at the request boundary — `POST /api/agents`, every
re-publish and the architect approval answer `422 agent.method_unsupported {method, field:
"tools.builtin", value, supported}` before any agent row or job exists
(`services/method_validation.require_method_support`, the general creation-time validator;
`agent.builtin_unsupported` is its one-round alias, carried as `detail.legacy_code`). The render map's key set IS the harness
method's declared `builtin_tools` (`pipeline.method_capabilities("harness")`, pinned by
`tests/test_method_capabilities.py`). The renderer's own `HarnessRenderError` stays as the
last line of defence for stored specs. `GET /api/capabilities.methods.harness.builtin_tools`
(and the `methods: ["harness"]` of each `GET /api/tools` builtin row) is what the V2 wizard
reads to disable a builtin the chosen method does not render instead of offering it.
The self-check's probe spec selects both builtins, so both modules render and compile on
every `make verify`.

## Tool-call summaries (N16-obs85, cn/r16-i)

The console persisted only `{name, id}` per tool event and the runtime log carried no tool
arguments, so for the browser builtin nothing recorded which url was opened or what came
back. Both generated templates now carry a module-level `# <launchpad-tool-summaries:v1>`
block (vendored text next to the max_tokens block, not a graft; no anchor changed, the
self-check still counts 7 files). Strands yields one `message` event when the assistant
message (its `toolUse` blocks hold the complete arguments) and the tool-result message
(`toolResult` blocks) join the conversation; `_launchpad_tool_frames(event)` turns each block
into one additive frame, yielded before the raw event is filtered:

- `{"event": "tool_input", "id", "name", "input"}` — `json.dumps` of the arguments, whitespace
  collapsed, cut at **512** characters with an explicit `... [+N chars]` marker;
- `{"event": "tool_result", "id", "status", "summary"}` — cut at **256** characters; for the
  browser builtin's `title / url / blank / text` result shape the summary is
  `title: … | url: … | text: N chars` (the page text itself is never summarised); non-text
  result content is named (`[image]`, `[document]`), never dumped.
- **C4 (rev-r16):** before anything is cut, a `<name>=<value>` / `<name>: <value>` pair (quoted
  or bare) whose name contains `token`, `secret`, `key`, `password`, `bearer`, `credential` or
  `authorization`, and a bare `Bearer <value>`, become `<name>=[redacted]` — in the arguments
  and in the result summary alike. The native `shell` / `file_operations` tools get **no result
  summary at all** (their output is arbitrary host text — an `env` listing, a config file):
  `summary: ""`, `status` only; the block remembers each `toolUseId → name` from the assistant's
  `toolUse` block to decide, and the frame carries `name` so the hub can decide again.
  The block is byte-identical in both templates (`test_tool_summaries.py` asserts it).

The hub normalizer (`services/agentcore/runtime._runtime_payload_events`) accepts both kinds and
cuts again at the same bounds (`TOOL_INPUT_MAX_CHARS`, `TOOL_RESULT_SUMMARY_MAX_CHARS` — a BYOC
runtime may send anything), applies the same redaction (`redact_secret_pairs`) and drops the
summary of the `UNSUMMARISED_TOOLS` (defence in depth); the transcript recorder completes the `tool` row the call opened
(`chat_messages.tool_call = {id, input, result_summary, status}`, matched on the id, by name
then newest row without one) and `GET /api/chat/{id}/history` replays it as the additive
`tool_input` / `tool_result_summary` / `tool_result_status` keys. The architect's own
`tool_input` reducer (`assistant/runtime.py`) is untouched. Tests: `tests/test_tool_summaries.py`.

## max_tokens stop handling (N-SCN-bug4, cn/r7-c)

Strands raises `MaxTokensReachedException` **unwrapped** out of `agent.stream_async` when one
completion stops on `max_tokens` (observed: GLM 5.3 running to the 16 384 ceiling on a
code-interpreter tool result). The partial assistant message is already in the agent's
history, so the documented recovery is to call the agent again. Before this block the
AgentCore SDK turned the exception into a terminal `{"error": …}` frame and the hub rendered
`RuntimeError: runtime returned error: Model stopped generating due to maximum token limit…` —
two tool rows, no agent bubble, no session message. The rendered `main.py` now carries a
module-level `# <launchpad-max-tokens:v1>` block (vendored in `main.py.tmpl`, not a graft —
it is part of the template text; A5's single `agent.stream_async(prompt)` inside `invoke`
still holds because the continuation call lives in `_launchpad_recover_max_tokens`). The
shared Strands template (`strands_agent/main.py.tmpl`, zip_runtime / Studio) implements the
same semantics inside `agent_events`, so every generated runtime behaves identically.

Semantics (per turn; `LAUNCHPAD_MAX_TOKENS_CONTINUATIONS`, default `1`, `0` = never continue):

1. The partial text already streamed stays — nothing is re-sent or trimmed on the wire.
2. While continuations remain: one WARN line
   `max_tokens stop: stop_reason=max_tokens output_tokens=<n|None> continuation=<k>/<budget>
   streamed_chars=<n>` (never the prompt text), then the marker frame
   `{"event": "truncated", "truncated": false, "stop_reason": "max_tokens", "continuations": k}`,
   a `"\n"` text delta when partial text exists, and the agent is called again with the
   continuation message *"Your previous answer was cut off at the token limit. Finish it
   concisely - final answer only."* — its events stream as usual (the Harness emits raw
   Converse events, the Strands template its `delta`/`tool` envelope). That continuation
   prompt is a **real user turn** in the Strands session: it lands in short-term memory, in
   the `aws/spans` chat span and in the Observability session transcript (the hub's
   `turn_event` is unaffected — the console still sees one turn).
3. When the budget is exhausted and the turn still stopped on `max_tokens`: a visible text
   delta `[response truncated at the model's token limit]`
   (preceded by a blank line when partial text exists) and the closing marker
   `{"event": "truncated", "truncated": true, …}`; the Strands template's final `complete`
   additionally carries `"truncated": true`. The turn is a **normal success**, never an error.
   A continuation that ends on its own stop (`end_turn`) **without producing any text** is an
   ordinary empty answer, not a truncated one (r7-e review follow-up): no notice, no
   `truncated: true` — only the `truncated: false` marker from step 2 remains on the wire.
4. Every other exception (incl. the export's `EventLoopException(ExecutionLimitExceeded)`
   handling) surfaces exactly as before. The exception is matched by class name
   (`_launchpad_is_max_tokens`), not by import, so an SDK relocation does not disable it.

Hub side (`services/agentcore/runtime.py`): `truncated` is a known runtime event kind and is
forwarded additively as SSE `event: truncated` (`data: {truncated, stop_reason,
continuations}`); the console ignores event kinds it does not know. `invoke_runtime_text`
(and therefore the public `/v1 …/invoke` body) gains `truncated: true` only on a turn that
ended with the notice. `Settings.max_tokens_continuations` (default 1) is injected into the
runtime environment as `LAUNCHPAD_MAX_TOKENS_CONTINUATIONS` only when it differs from the
templates' own default; a value in the spec's `env` wins. Hermetic coverage:
`backend/tests/test_max_tokens_recovery.py` (both templates rendered for real, fake agents).

Console surface (round-6 follow-up, cn/r8-a): the marker is no longer ignored. The chat
router (`routers/chat.py`) flags the open answer bubble on `truncated: true` (a `truncated:
false` marker — the continuation case — flags nothing) and persists it as
`ChatMessage.truncated` (additive column, migrated on startup); `GET /api/chat/{id}/history`
replays it as `truncated: true` on that agent row, and the observability transcript carries
`truncated: true` on the ASSISTANT turn (from the ledger, whether the transcript itself came
from Memory or from the ledger). The buffered invoke path (`invoke_agent_events` for
non-streaming methods) forwards `invoke_runtime_text`'s `truncated` as the same SSE
`truncated` event, so the surface does not depend on the method. UI: the Chat page
renders a one-line inline notice under the bubble — en "Response truncated at
the model's token limit" / zh-CN "回复在模型 token 上限处被截断" — live and on history
restore; the Observability session transcript shows a `TRUNCATED` badge on
the turn (`components/chat/TruncatedNotice.tsx`). Hermetic coverage:
`backend/tests/test_chat_truncated_marker.py`, `frontend/src/lib/chat.test.ts`,
`frontend/src/components/chat/TruncatedNotice.test.tsx`.

## Model catalog descriptor in `model/load.py` (D21 M1-d)

`model/load.py` is still the Strands template's model block, sliced
(`templates/model_support.render_model_load`). The block now also carries
`MODEL_CATALOG_VERSION = <registry journal id>` and `MODEL_DESCRIPTOR = <the catalog row of
MODEL_ID>` (`schemas.agent.model_descriptor`), plus a process-level `MODEL_DESCRIPTORS`
registry that `_model_provider()` / `_supported_model_config()` consult **before** the
inlined `MODEL_PROVIDERS` / `MODEL_INFERENCE_PARAMS` tables. The env var
`LAUNCHPAD_MODEL_DESCRIPTOR` (JSON: a descriptor, a list or a `{model_id: descriptor}`
map) can override / extend the registry without a re-render (operator knob; the deployer
does not write it). The data literals for the seeded catalog render byte for byte as before
D21, the self-check (`scripts/harness_template_check.py`) is unaffected (anchor A9 replaces
the vendored loader wholesale), and `tests/test_model_support.py` keeps proving the
sliced block is self-contained (the only new import, `json`, is function-local).

## Tool-schema normalizer in `model/load.py` (N17-obs302)

The sliced block also carries the tool input-schema normalizer, inlined from
`backend/app/templates/tool_schema.py`. `load_model()` therefore returns a model whose
`stream` normalizes every tool spec first: `oneOf` → `anyOf`, no `discriminator`, no
`binary`/`byte` `format`. This covers the code-interpreter builtin, the KB tools, the
gateway / remote-MCP tools and the skills tool alike. The rules, the rollout and the tests
are in `model-credential.md` § *Tool schemas (N17-obs302)*.
