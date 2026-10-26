# Claude Agent SDK Containers — AgentCore Memory

## Scenario: automatic memory restore and persistence

### 1. Scope / Trigger

Use this contract when changing Claude Agent SDK container generation, runtime
environment derivation, or memory restore/persist behavior. The integration
crosses the deploy-time `AgentSpec` boundary and the generated runtime boundary;
it must preserve the platform's shared-memory actor isolation.

The [Memory Console](./memory-console.md) reads these same partitions back
read-only; keep both specs in sync when the actor-scoping rule changes.

### 2. Signatures

```python
# app/deployer/environment.py
runtime_environment(
    spec: AgentSpec,
    resources: Mapping[str, Any],
) -> dict[str, str]

# generated main.py
AgentCoreMemory(memory_id: str, actor_id: str, session_id: str)
AgentCoreMemory.context_for(prompt: str) -> str
AgentCoreMemory.save_turn(prompt: str, response: str) -> bool
_create_memory(actor_id: str, session_id: str) -> AgentCoreMemory | None
build_options(memory: AgentCoreMemory | None = None) -> ClaudeAgentOptions
run_query(prompt: str, memory: AgentCoreMemory | None = None) -> QueryOutcome
```

Pinned `bedrock-agentcore==1.17.*` imports:

```python
from bedrock_agentcore.memory import MemorySessionManager
from bedrock_agentcore.memory.constants import ConversationalMessage, MessageRole
```

`EventMessage` and `MemoryRecord` are dictionary wrappers, not `dict`
subclasses. Consume them through `.get(...)`.

### 3. Contracts

- `runtime_environment` starts with a copy of `spec.env`. When either memory
  flag is enabled and `resources.memory_id` exists, it sets
  `LAUNCHPAD_MEMORY_ID` to the platform value; the platform value wins over a
  same-named user value.
- Normal republish and canary candidate publication use the same helper.
  Update wrappers include `environmentVariables={}` when the derived map is
  empty so an old memory ID is removed. Create wrappers may omit an empty map.
- The generated source bakes `memory.short_term` and `memory.long_term` into
  booleans. Missing `LAUNCHPAD_MEMORY_ID`, both flags false, or a missing
  `payload.actor_id` disables all memory calls.
- `payload.actor_id` is already scoped as `<agent>__<human>` by the shared
  invoke chain. Pass it unchanged to
  `create_memory_session(actor_id=..., session_id=...)`; do not scope it again.
- A console Chat session keeps the bare actor recorded when its `ChatSession`
  row is first created. When a later request reuses that `session_id`, the
  router must use the recorded actor for both invocation and Memory-summary
  reads instead of accepting a different request actor and splitting one
  session across Memory partitions.
- Session identity is `context.session_id`, then payload `session_id`, then the
  existing `adhoc` fallback.
- Construct one `MemorySessionManager` per invocation. It is not thread-safe.
  Its synchronous calls run through `asyncio.to_thread` so the runtime event
  loop is not blocked.
- Short-term restore reads at most five recent turns and renders them in
  chronological order. Long-term restore searches the exact namespaces
  `/facts/<actor>` and `/preferences/<actor>` with at most three records each.
  Individual items and total injected context are character-bounded.
- A request-local `UserPromptSubmit` hook returns the restored text as
  `hookSpecificOutput.additionalContext`. The original prompt remains unchanged
  for query execution, tracing, and persistence.
- After a successful `QueryOutcome`, call `MemorySession.add_turns` exactly
  once with one USER and one ASSISTANT `ConversationalMessage`. Never also save
  from a Stop hook. A failed Claude query writes no event.
- Initialization, retrieval, and persistence are independently best-effort.
  Warnings contain only operation, session ID, and exception type; they never
  contain prompts, responses, or retrieved memory.
- Observability reads Chat transcripts from AgentCore Memory first. It compares
  those USER/ASSISTANT turns with the `ChatMessage` ledger and uses the ledger
  when Memory is incomplete or differs; this covers eventual consistency and
  historical actor-partition drift while preserving Memory as the normal
  source. Eval sessions continue to use Memory or runtime content logs because
  they have no Chat ledger.

### Template / renderer synchronization

- `main.py.tmpl` is read for every build, but `render_main_py` stays imported in
  a long-lived backend process. After changing either side of this contract,
  restart or reload the backend before a real deployment; otherwise a new
  template can be combined with a stale replacement chain.
- Rendered source must contain no unresolved `__LAUNCHPAD_*` tokens. Keep a
  regression assertion over the complete rendered output whenever a placeholder
  is added.
- A newly added token must remain syntactically valid and fail closed if a
  stale renderer leaves it untouched. For boolean feature flags, quote the
  token and compare it with the enabled literal:

  ```python
  FEATURE_ENABLED = "__LAUNCHPAD_FEATURE_ENABLED__" == "True"
  ```

  Do not emit a bare token into executable Python; an unresolved name makes the
  AgentCore runtime fail during process startup.

### 4. Validation & Error Matrix

| Condition | Runtime behavior |
|---|---|
| both memory flags false | no manager, hook, retrieval, or write |
| enabled flag but no platform/user memory ID | invocation proceeds without memory |
| missing scoped actor | invocation proceeds without memory to avoid cross-user fallback |
| manager initialization failure | warning; Claude query proceeds |
| short-term retrieval failure | warning; long-term retrieval still attempted |
| one long-term namespace fails | warning; other available sections are still injected |
| empty or malformed record content | record ignored |
| Claude query failure | exception propagates through existing tracing; no memory write |
| memory write failure | warning; completed Claude response is returned unchanged |
| update derives an empty environment | send `environmentVariables={}` to clear old values |
| existing Chat session requested with another actor | invoke and read Memory with the actor stored on `ChatSession` |
| Chat Memory events lag or cover only one actor partition | Observability reconciles USER/ASSISTANT turns from `ChatMessage` |

### 5. Good / Base / Bad Cases

- **Good:** same scoped actor and runtime session restores recent turns; relevant
  facts/preferences are injected; one completed event is written.
- **Base:** memory is disabled or not bootstrapped; generated containers remain
  importable and invoke exactly as before.
- **Bad but tolerated:** AgentCore Memory is unavailable; warnings are emitted
  without content leakage and the Claude result remains authoritative.
- **Bad and rejected by design:** a missing actor must not fall back to a shared
  actor such as `default`.

### 6. Tests Required

- Rendered source replaces both memory placeholders and still compiles/imports.
- Build context retains the pinned `bedrock-agentcore==1.17.*` dependency.
- Short-term turns render oldest-to-newest with item/context bounds.
- Long-term calls use both exact namespaces and the original prompt query.
- Distinct actor/session pairs create distinct memory sessions.
- Reusing a Chat session keeps its original actor for invoke and summary reads.
- Incomplete Chat Memory events are reconciled from the exact rendered-message
  ledger; matching events remain Memory-origin transcripts.
- `UserPromptSubmit` exposes restored text only through `additionalContext`.
- Success writes one event with USER then ASSISTANT; Claude failure writes none.
- Initialization/read/write failures do not fail the invocation or leak content
  into warnings.
- Zip, container, and canary update paths use shared environment precedence.
- Container create and update inject the platform ID; disabled update sends an
  empty environment map.

### 7. Wrong vs Correct

```python
# WRONG: module-global manager; concurrent invocations share non-thread-safe state
manager = MemorySessionManager(memory_id=MEMORY_ID)

# CORRECT: one manager/session adapter per invocation
memory = AgentCoreMemory(MEMORY_ID, payload["actor_id"], context.session_id)
```

```python
# WRONG: save in a Stop hook and again from the collected result
hooks["Stop"] = save_transcript
memory.save_turn(prompt, outcome.result)

# CORRECT: QueryOutcome is authoritative; save exactly once after success
outcome = await run_query(prompt, memory)
await asyncio.to_thread(memory.save_turn, prompt, outcome.result)
```

```python
# WRONG: omit an empty environment on update; a prior memory ID may survive
if environment:
    params["environmentVariables"] = environment

# CORRECT: distinguish "not supplied" from "explicitly clear"
if environment is not None:
    params["environmentVariables"] = environment
```
