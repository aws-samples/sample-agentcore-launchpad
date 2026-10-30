# harness_runtime — vendored Global Harness export (N7a)

- Export date: 2026-10-09 (UTC)
- CLI: @aws/agentcore 1.0.0-preview.20 — `agentcore export harness --arn <ref> --target-agent-name lpexportRefAgent --build CodeZip --json`
- Strands version the generator targets: strands-agents >= 1.15.0 (EXPORT_NOTES.md); platform pins in requirements.txt.tmpl
- Reference Harness (deleted after export): name lpexportRef, Bedrock Converse model, short system prompt, Memory ON (AgentCore Memory, SEMANTIC), exactly one S3 skill, allowedTools=["skills"], no gateway / remote MCP / inline function / browser / code-interpreter, maxIterations=10, timeoutSeconds=300, network PUBLIC (default)
- Export note carried over: "allowedTools: per-invocation overrides dropped" — the allowed-tools filter is applied statically at generation time.

## Files (export path → vendored name)
- main.py → main.py.tmpl
- model/load.py → model/load.py.tmpl
- memory/session.py → memory/session.py.tmpl
- skills/fetcher.py → skills/fetcher.py.tmpl
- hooks/execution_limits.py → hooks/execution_limits.py.tmpl
- mcp_client/client.py → mcp_client/client.py.tmpl (contains no `MCPClient(`)
- CLI base asset node_modules/@aws/agentcore/dist/assets/python/http/strands/base/main.py → _cli/strands_base_main.py.hbs
- NOT vendored: README.md, EXPORT_NOTES.md, .gitignore, pyproject.toml, s3-skills-policy.json, model/mantle_compat.py (unreferenced by main.py / model/load.py; Mantle is deleted in the CN build), __init__.py files

## Redactions
- main.py.tmpl: `DEFAULT_SYSTEM_PROMPT = """..."""` → `"""__LAUNCHPAD_REFERENCE_PROMPT__"""`
- main.py.tmpl: `s3_skill_sources = ["s3://<bucket>/skills/<skill>/"]` → `["s3://__LAUNCHPAD_SKILLS__/"]`
- model/load.py.tmpl: `model_id="<reference model id>"` → `model_id="__LAUNCHPAD_MODEL_ID__"`
- _cli/strands_base_main.py.hbs: `os.environ.get("AWS_DEFAULT_REGION", "us-east-1")` fallback (git-skills credential branch) → `"cn-northwest-1"` (contract redaction rule: no literal Global region)
- CLI version note: the contract was drafted against 0.21.x; this export is @aws/agentcore 1.0.0-preview.20 — anchors A1–A10 verified identical; `mcp_client/client.py.tmpl` is an imports-only scaffold (no `get_all_gateway_mcp_clients()`, no `MCPClient(` call) because the reference Harness has no gateway/MCP tool (contract A4 amended accordingly)
- Verified absent in every vendored file: `arn:aws`, `.amazonaws.com`, the 12-digit account id, bucket name, region literal, harness name, model id. Exception (contract-permitted, see `docs/cn/interfaces/harness-template.md`'s redaction rules): `memory/session.py.tmpl` line 8 carries the reference Memory's env-var name verbatim, `MEMORY_MEMORY_MYBETAMEMORY_FE57HMASR0_ID` — a `MEMORY_MEMORY_*` lookup may stay because this file is replaced wholesale at render (`MEMORY_SESSION_SHIM`), never shipped as-is.

## Anchors present in main.py.tmpl (contract A1–A10, see docs/cn/interfaces/harness-template.md)
A1 DEFAULT_SYSTEM_PROMPT + system_prompt=DEFAULT_SYSTEM_PROMPT · A2 `tools = []` · A3 `agent = get_or_create_agent(session_id, user_id, _skill_plugins)` · A4 `mcp_clients = []` · A5 `@app.entrypoint` / `async def invoke(payload, context)` / `prompt = _extract_prompt(payload)` / single `agent.stream_async(` · A6 `s3_skill_sources = [...]` · A7 `user_id = getattr(context, 'user_id', 'default-user')` · A8 `ExecutionLimitsHook(` + `timeout_seconds = 300` · A9 `from model.load import load_model` · A10 `from memory.session import get_memory_session_manager`

## Pins maintenance (N9 checklist item)
`requirements.txt.tmpl` pins (bedrock-agentcore, botocore[crt], mcp, strands-agents[otel]) must be refreshed together with `backend/app/templates/strands_agent/requirements.txt` whenever the platform base moves; the combined set (platform base + these pins + optional `strands-agents-tools`) is only resolved live by `uv pip compile` in the package stage for the ARM64 binary-only target (`botocore[crt]` → awscrt aarch64 wheel) — N9 must run one harness deploy end to end before declaring the pins good.
