# Design — `kb_deep_search` over AgenticRetrieveStream

## 1. Live-verified API shape (botocore 1.43.44, 2026-07-28)

Probed with the real client, not from docs — the AWS blog example's `messages[].content`
is a **list**, the actual model wants a **structure**:

```python
resp = client.agentic_retrieve_stream(
    messages=[{"role": "user", "content": {"text": "<query>"}}],   # content is a STRUCT
    retrievers=[{"description": "<kb description>",
                 "configuration": {"knowledgeBase": {"knowledgeBaseId": "<id>"}}}],
    agenticRetrieveConfiguration={"foundationModelType": "MANAGED",
                                  "rerankingModelType": "MANAGED",
                                  "maxAgentIteration": 3},
)
```

Required input members: `agenticRetrieveConfiguration`, `messages`, `retrievers`.
`retrievers[].configuration.knowledgeBase.knowledgeBaseId` is the SAME shape
`kb_gateway._agentic_target_configuration` already sends for the harness target.

`resp["stream"]` is an **event stream** whose members are:

| member | payload | notes |
|---|---|---|
| `traceEvent` | `attributes{step, status, message, actions[], retrievalMetadata[], failures[], warnings[]}`, `id`, `timestamp` | one per planner step |
| `responseEvent` | `text` | incremental deltas of the synthesized answer (110 of them in the probe) |
| `result` | `generatedResponse{answer, citations[]}`, `results[]{content{text}, metadata{}, sourceRetriever{identifier}}`, `nextToken` | terminal; carries answer AND deduped chunks |
| `accessDeniedException`, `validationException`, `throttlingException`, `resourceNotFoundException`, `conflictException`, `dependencyFailedException`, `serviceQuotaExceededException`, `internalServerException`, `badGatewayException` | modeled errors that arrive **inside** the stream | must be handled as data, not only as raised exceptions |

Probe result (`lab-fund-kb`, comparison question, `maxAgentIteration=3`):
`SpeculativeRetrieval IN_PROGRESS→SUCCEEDED`, `Planning IN_PROGRESS→SUCCEEDED`, then
`result` with 10 chunks + a 687-char answer + 3 citations. Note **no `Retrieval` step
fired** — the planner judged the speculative pass sufficient. So the tool must not assume
any particular step sequence.

Differences from `Retrieve` that the formatter has to absorb:
- agentic results have **no `score`** and **no `location`** — the source URI lives in
  `metadata["_source_uri"]` (also `_document_title`, `_chunk_id`, `_file_type`,
  `_data_source_type`), and the KB id in `sourceRetriever.identifier`.

## 2. `kb_support.py` changes

```python
KB_TOOL_NAME = "kb_search"              # unchanged
KB_DEEP_TOOL_NAME = "kb_deep_search"    # new
KB_MCP_SERVER = "launchpad_kb"          # unchanged — both tools live in it
KB_RESULTS = 8                          # kb_search numberOfResults
KB_DEEP_ITERATIONS_SINGLE = 3           # AWS guidance
KB_DEEP_ITERATIONS_MULTI = 5

def kb_deep_tool_description(kbs) -> str          # names the KBs, says it is the slow one
def kb_prompt_section(kbs) -> str                 # SIGNATURE CHANGE: drops tool_name,
                                                  # names both tools from the constants
```

`kb_prompt_section` losing its `tool_name` parameter is the only breaking change; both
call sites are in this repo (the two renderers) and both are updated. Keeping the
parameter would let a caller name one tool while the template registers two.

New prompt section shape:

```
## Knowledge bases
Two retrieval tools are mounted for you.
- `kb_deep_search` — a planning loop that decomposes the question, searches across every
  mounted knowledge base, may pull whole documents, and returns a cited answer plus its
  sources. Prefer it for comparisons, "list everything…", summaries, and anything whose
  evidence is spread across documents. Slower (seconds) and more expensive.
- `kb_search` — one similarity search. Prefer it for a single fact you can name.
Do not answer from memory when a question touches the content below.
Mounted knowledge bases:
- <name> (kb_id `<id>`) — <description>
Ground answers on retrieved passages and cite their sources; …
```

## 3. Template implementation (identical logic, two hosts)

Shared helper written into both templates (the container's copy is `async`-wrapped):

```python
def _deep_retrievers(kb_id: str) -> list[dict] | str      # reuse the kb_id resolution
def _format_agentic(payload: dict) -> str                 # answer + citations + chunks

def kb_deep_search_text(query_text: str, kb_id: str = "") -> str:
    targets = <same resolution as kb_search: all mounted, or the one id, or error text>
    iterations = 3 if len(targets) == 1 else 5
    try:
        stream = _kb_runtime().agentic_retrieve_stream(
            messages=[{"role": "user", "content": {"text": query_text}}],
            retrievers=[{"description": kb["description"][:200],
                         "configuration": {"knowledgeBase": {"knowledgeBaseId": kb["kb_id"]}}}
                        for kb in targets],
            agenticRetrieveConfiguration={"foundationModelType": "MANAGED",
                                          "rerankingModelType": "MANAGED",
                                          "maxAgentIteration": iterations},
        )["stream"]
        steps, final, failure = [], None, None
        for event in stream:
            if "traceEvent" in event:  steps.append(f"{step}:{status}")
            elif "result" in event:    final = event["result"]
            elif <any modeled error member>: failure = f"{member}: {message}"
        # responseEvent deltas are ignored — `result.generatedResponse.answer` is the
        # same text, already complete, and the tool return value is not streamed anyway
    except Exception as exc:
        return f"deep search failed: {type(exc).__name__}: {exc}"
    ...
```

Contract details:
- **Both** failure channels are covered: a modeled error member inside the stream and a
  raised exception (client error, timeout). Both return text.
- `traceEvent`s are reduced to a compact `steps=SpeculativeRetrieval:SUCCEEDED, …` line
  prefixed to the output — cheap, and it is what makes the lab's "did it really plan?"
  story checkable without opening CloudWatch. Trace `failures[]`/`warnings[]` messages are
  appended when present.
- No cap on returned chunks: `kb_search` already returns its 8 uncapped, and the deduped
  agentic set is the same order of magnitude. Documented in the tool description.
- Container: the whole consume runs in `asyncio.to_thread` (blocking botocore iterator).
  Both tools are passed to the existing `create_sdk_mcp_server(name="launchpad_kb", …)`,
  so `ALLOWED_TOOLS` (server-level `mcp__launchpad_kb`) needs no change.
- Strands: `build_agent` appends both tools when `MOUNTED_KBS`.
- botocore read timeout stays default (60s **between events**, not total) — the stream
  emits trace events continuously, so a long planning run does not trip it.

## 4. IAM

```python
exec_role.add_to_policy(iam.PolicyStatement(
    sid="ManagedKbAgenticRetrieval",
    actions=["bedrock:AgenticRetrieveStream"],
    resources=["*"],          # not resource-scopable — same as launchpad-gateway-role
))
```

Kept as its own statement (not folded into `ManagedKbRetrieval`) precisely so the `*`
resource is visible in isolation to anyone reading the role. `bedrock:InvokeModel` is
already granted account-wide, and `foundationModelType: MANAGED` means the service
supplies the planner, so nothing else is needed — the gateway role proves that set is
sufficient.

## 5. Frontend / copy

Only `create.configure.kbNoteDirect` changes (en + zh-CN): drop
「仅单次检索（无 agentic 多步）」, describe the two tools and that deep search costs more.
No component logic changes — the picker already works for all three methods.

## 6. Test plan

Hermetic:
- `tests/test_strands_template.py`: both tools registered / neither when KB-less; both
  descriptions in `DEFAULT_TOOL_DESCRIPTIONS`; prompt section names both.
- Stubbed stream fixture yielding `traceEvent → traceEvent → result` asserts the
  formatted output (answer, `citations: 3`, chunk with `2MBGUNVMS4` + `_source_uri`,
  `steps=` line) and the derived `maxAgentIteration` (1 KB → 3, 2 KBs → 5).
- Stream carrying `{"accessDeniedException": {"message": …}}` → readable text; client
  raising → readable text; unknown `kb_id` → no request issued.
- `tests/test_claude_sdk_template.py`: both tools in the SDK server; `kb_deep_search`
  handler returns the MCP content envelope; `ALLOWED_TOOLS` unchanged.
- `infra/tests/test_stack.py`: synth assertion for the new sid.

Real AWS: `cd infra && uv run cdk deploy`, deploy `kb-deep-zip` + `kb-deep-container`
with `lab-fund-kb`, ask a comparison question, confirm `execute_tool kb_deep_search` in
the trace and a grounded answer; delete both afterwards.

## 7. Compatibility / rollback

- Additive for every existing agent: KB-less specs render unchanged, and agents already
  deployed with `kb_search` keep working until re-published.
- The IAM statement is additive; rollback is a revert + `cdk deploy`.
- An agent re-published before the CDK deploy lands gets `kb_deep_search` returning an
  `AccessDeniedException` line while `kb_search` keeps working — degraded, not broken.
