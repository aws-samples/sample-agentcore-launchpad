# Claude Agent SDK Containers - Runtime Invocation

## Scenario: native response streaming with synchronous compatibility

### 1. Scope / Trigger

Use this contract when changing the shared AgentCore data client, Claude SDK
container response behavior, Chat/public SSE, or runtime invocation parsing.
Claude containers must expose SDK partial text as it is generated; returning a
complete JSON result and re-chunking it after EOF is not streaming.

### 2. Signatures

```python
# app/core/config.py
Settings.agentcore_read_timeout_s: int = 1000

# generated main.py
build_options(...)  # ClaudeAgentOptions(include_partial_messages=True)
_query_events(prompt, memory, outcome) -> AsyncIterator[dict[str, Any]]
_events_with_heartbeat(events, interval_s=15.0) -> AsyncIterator[dict[str, Any]]
invoke(payload, context) -> AsyncIterator[dict[str, Any]]

# app/services/agentcore/runtime.py
stream_runtime_events(
    client,
    runtime_arn: str,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "default",
    qualifier: str | None = None,
) -> Iterator[dict[str, Any]]

invoke_runtime_text(...) -> dict[str, Any]
```

Runtime SSE data payloads:

```json
{"event": "delta", "text": "..."}
{"event": "tool", "name": "...", "id": "..."}
{"event": "heartbeat", "timestamp": 1784210000.0}
{"event": "complete", "result": "...", "usage": {}}
{"event": "error", "message": "..."}
```

Environment override:

```text
LAUNCHPAD_AGENTCORE_READ_TIMEOUT_S=<positive integer seconds>
```

### 3. Contracts

- Generated containers set `include_partial_messages=True`, consume only
  `StreamEvent` values whose raw event is `content_block_delta` /
  `text_delta`, and yield one `delta` payload for each non-empty text fragment.
- `AssistantMessage` remains authoritative for the complete response and tool
  calls. Do not emit its complete text after partials; emit it only as a
  fallback when a CLI version supplies no partial text for that assistant
  message.
- `ResultMessage` owns usage and failure status. The runtime emits `complete`
  only after tracing and best-effort Memory persistence finish.
- The generated `@app.entrypoint` is an async generator. BedrockAgentCoreApp
  serializes each yielded dictionary as one `data: <json>` SSE event.
- While the invocation is otherwise silent, the generated container emits a
  `heartbeat` frame every 15 seconds. The heartbeat wrapper keeps one
  `anext()` task pending across timeout checks; it must not cancel and restart
  the Claude SDK iterator. The timestamp keeps the serialized frame larger
  than the backend's 32-byte read chunk.
- `stream_runtime_events()` checks `contentType`. For
  `text/event-stream`, consume `StreamingBody.iter_lines(chunk_size=32)` and
  yield normalized `heartbeat` / `tool` / `delta` events immediately. Never use the
  1024-byte default or call an unbounded `.read()` first.
- The shared Chat/public SSE encoder converts the internal heartbeat event to
  the comment `: keep-alive\n\n`. It is forwarded as bytes but creates no
  frontend event, transcript row, or response text. Buffered consumers ignore
  heartbeat events and continue joining only text deltas.
- The parser also accepts converted-Harness envelopes
  (`{"event":{"contentBlockDelta":...}}`) and legacy buffered
  `application/json` responses.
- `complete.result` is a fallback only. Once any delta has been observed, do
  not emit the final full result again.
- `invoke_runtime_text()` consumes `stream_runtime_events()` and joins deltas.
  Chat and `/v1/invoke-stream` use the same parser, so sync and stream paths do
  not own separate response decoders.
- Direct HTTP container invocations stream natively. A2A and active canary
  Gateway routes retain their existing buffered compatibility paths.
- `data_client()` passes
  `Config(read_timeout=settings.agentcore_read_timeout_s)` to the
  `bedrock-agentcore` client. The 1000-second default still protects quiet
  periods and legacy buffered runtimes; it is not a substitute for streaming.
- Existing deployed containers contain their old generated `main.py`. A
  template change requires republishing each agent before its DEFAULT endpoint
  can stream.
- AgentCore binds an existing `runtimeSessionId` to the runtime version that
  first served it. After republishing, start a new Chat session to reach the
  new streaming image; an old session intentionally continues using its
  previous version and may still return one buffered JSON result.

### 4. Validation & Error Matrix

| Condition | Behavior |
|---|---|
| SDK emits text deltas | forward each delta before query completion |
| SDK is silent for 15 seconds | emit a heartbeat without cancelling the pending SDK read |
| SDK emits no partials | emit `AssistantMessage` text once as fallback |
| runtime emits `complete` after deltas | suppress duplicate full result |
| runtime returns legacy JSON | emit its `result` as one delta |
| converted Harness SSE | normalize text/tool events |
| runtime SSE emits `error` | raise; Chat converts it to one `error` event |
| Claude query fails | AgentCore emits stream error; write no Memory event |
| old session reused after republish | remains on its original runtime version; start a new session to test the new image |
| setting omitted | use 1000-second read timeout |
| invalid timeout | settings validation fails at startup |
| synchronous work exceeds 15 minutes | AgentCore service limit still applies |

### 5. Good / Base / Bad Cases

- **Good:** the first Claude `text_delta` reaches Chat while the SDK query is
  still running; later deltas append to the same message.
- **Idle but healthy:** a long model/tool step emits `: keep-alive` comments at
  the browser boundary until the next real event, preventing proxy idle
  timeout without changing the rendered thread.
- **Base:** a zip/studio or pre-republish container returns legacy JSON and the
  compatibility decoder emits one complete delta.
- **Version-pinned:** a session created before republish continues returning
  its original version's response shape; `NEW SESSION` reaches the new image.
- **Bad but surfaced:** Claude emits partial text and then fails; Chat preserves
  the visible partial answer and appends an error event.
- **Bad implementation:** read the whole StreamingBody, parse the final JSON,
  then split it into 60-character chunks. The UI animates chunks only after the
  model has finished.

### 6. Tests Required

- Rendered source compiles, replaces all placeholders, imports `StreamEvent`,
  and enables `include_partial_messages`.
- Template tests prove two partial text events are emitted once, the final
  `AssistantMessage` is not duplicated, and `QueryOutcome.result` is complete.
- Template tests prove a pending SDK event survives at least one heartbeat
  timeout and the upstream heartbeat frame exceeds the 32-byte Runtime reader
  chunk.
- Successful streaming persists one USER/ASSISTANT Memory event after query
  completion; failure persists none.
- Runtime parser tests prove the first SSE event is yielded before later lines
  are consumed, the 32-byte read size is used, tool events survive, and
  `complete.result` is not duplicated.
- Legacy JSON, converted-Harness SSE, qualifier, and error tests remain green.
- Chat tests prove container `meta.mode=stream` and native event order is
  preserved, and heartbeat comments are not persisted as messages.
- The canonical `make verify` gate and one real republished-container browser
  invocation with more than 120 seconds between business events must pass.

Studio/zip runtime heartbeat support is deferred in
`docs/issues/2026-07-16-studio-agent-sse-heartbeat.md`.

### 7. Wrong vs Correct

```python
# WRONG: no bytes reach the caller until the runtime closes the response.
raw = response["response"].read()
text = json.loads(raw)["result"]
for chunk in artificial_chunks(text):
    yield chunk

# WRONG: wait_for cancels the active SDK read whenever the heartbeat fires.
try:
    event = await asyncio.wait_for(anext(events), timeout=15)
except TimeoutError:
    yield {"event": "heartbeat"}

# CORRECT: parse each SSE event directly from the StreamingBody.
for line in response["response"].iter_lines(chunk_size=32):
    for event in parse_sse_line(line):
        yield event

# CORRECT: keep one SDK read alive while timeout checks emit heartbeats.
pending = asyncio.create_task(anext(events))
done, _ = await asyncio.wait({pending}, timeout=15)
if not done:
    yield {"event": "heartbeat", "timestamp": time.time()}
```
