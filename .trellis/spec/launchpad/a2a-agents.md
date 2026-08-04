# A2A-Protocol Agents

> Create agents that serve the standard Agent-to-Agent protocol (JSON-RPC)
> on AgentCore Runtime via `protocolConfiguration.serverProtocol=A2A`, with
> real (resolvable) Registry agent cards. Introduced 2026-07-13 (task
> 07-13-a2a-agent-create, parent 07-13-a2a-e2e). Companion research:
> `.trellis/tasks/07-13-a2a-e2e/research/a2a-registry-findings.md`.

## Scenario: creating / operating an A2A agent

### 1. Scope / Trigger

- Anything touching `AgentSpec.protocol` / `a2a_skills`, the
  `templates/strands_a2a_agent/` template, protocolConfiguration deploy
  params, the JSON-RPC invoke branch, or A2A card registration.

### 2. Signatures

```python
# schemas/agent.py
AgentSpec.protocol: Literal["http", "a2a"] = "http"   # zip_runtime only for a2a
AgentSpec.skills: list[str]                           # mounted Registry/custom bundles
AgentSpec.a2a_skills: list[A2ASkill]                  # advertised AgentCard metadata only
# a2a is incompatible with code/code_bundle (always the platform template)

# services/agentcore/runtime.py
create_code_runtime(..., protocol=None)   # a2a → protocolConfiguration {serverProtocol: A2A}
update_code_runtime(..., protocol=None)   # MUST echo protocol on every update (omit=RESET)
invoke_a2a_text(client, arn, prompt, session_id=None) -> {text, session_id}
# session_id is both InvokeAgentRuntime.runtimeSessionId and message.contextId
a2a_result_text(result) -> str            # Task artifacts[].parts[] | Message parts[]

# services/agentcore/registry.py
data_plane_invocations_url(arn, region) -> str
build_a2a_card(..., url=None, skills=None, transport="agentcore-http")
# transport: "a2a-jsonrpc" (real A2A endpoint) | "agentcore-http" (platform invoke)

# templates/strands_a2a_agent/__init__.py
render_a2a_main_py(spec) -> str           # A2AServer(agent_factory=..., serve_at_root=True, skills=...)
a2a_base_requirements() -> list[str]      # strands-agents[a2a,otel] + fastapi/uvicorn
# agent_factory(context_id) attaches AgentCoreMemorySessionManager when
# LAUNCHPAD_MEMORY_ID is present and context_id is valid; AgentSkills is
# independently attached when packaged bundles exist
```

### 3. Load-bearing facts (all probed live 2026-07-13)

- **zip + A2A works**: `codeConfiguration` zip artifact + `protocolConfiguration
  {serverProtocol: A2A}` → READY. The managed PYTHON_3_13 runtime does NOT
  install requirements.txt — wheels must be vendored (production `build_zip`
  does; `strands-agents[a2a,otel]` resolves against the wheel-only ARM64 target,
  ≈46 MB package). Keep `uv pip compile --only-binary=:all:` aligned with the
  binary-only install: live validation on 2026-08-04 otherwise locked the
  sdist-only `greenlet==3.5.4`, which pip could not install for manylinux2014;
  wheel-only resolution selected the compatible `greenlet==3.2.5`.
- **`AGENTCORE_RUNTIME_URL` is auto-injected** by the runtime — the served
  card carries the correct data-plane invocations URL with no env wiring.
- **UpdateAgentRuntime protocolConfiguration is omit=RESET** (opposite of
  UpdateHarness omit=keep): omitting it silently reverts the runtime to HTTP
  (well-known card → 400). Every update path echoes `spec.protocol`.
- **JSON-RPC passthrough**: `InvokeAgentRuntime` with a `message/send`
  envelope returns the A2A result unmodified. Task replies carry the final
  text in `result.artifacts[].parts[].text`; `result.history` contains
  STREAMING FRAGMENTS (agent messages split mid-word) — never join history.
- **A2A conversation identity is `message.contextId`**, not
  `runtimeSessionId`. Launchpad deliberately uses the same stable platform
  session value for both fields, but both must be present. Omitting
  `contextId` causes each `message/send` to enter a fresh Strands context even
  when AgentCore receives the same `runtimeSessionId`.
- **A2A context is not an authentication boundary**. The template scopes the
  shared Memory actor to `<agent-name>__a2a__<context-id>` for short-term
  isolation; cross-session human identity needs a future authenticated actor
  envelope.
- Well-known card fetch: SigV4 GET
  `https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{urlencode(arn)}/invocations/.well-known/agent-card.json`
  (needs `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header).

### 4. Behavior contracts

- Chat playground and the public API dispatch through
  `services/invoke.py::invoke_agent_text` (`spec.protocol == "a2a"` →
  `invoke_a2a_text`). The eval runner and persona simulator BYPASS that entry
  point (`evaluation/service.py::execute_run`, `evaluation/simulation.py`) and
  carry their own protocol branch — any NEW direct `InvokeAgentRuntime` caller
  must dispatch on protocol too, or A2A runtimes reject the `{prompt}` payload
  with JSON-RPC -32600 (the live eval-run failure that forced this note).
  Users see plain text either way.
- The A2A template deliberately drops the config-bundle contract. It uses the
  platform-injected `LAUNCHPAD_MEMORY_ID`: `agent_factory(context_id)` creates
  an `AgentCoreMemorySessionManager` so the context survives A2AServer LRU
  eviction and Runtime restart. `A2AServer` also calls the factory once with
  its internal `__agent_card__` context while building the card; that value is
  not a valid AgentCore Memory session id, so the template deliberately skips
  the session manager for invalid/internal context ids while retaining it for
  real platform context ids. Because config bundles remain unsupported,
  **experiments reject A2A agents** (400
  `experiment.protocol_unsupported`; UI picker disables with a reason —
  mirrors the harness pattern).
- **Mounted Skills and AgentCard skills are independent.** `spec.skills`
  contains APPROVED Registry or custom S3 prefixes. The package stage snapshots
  them under `skills/<name>/`, and each context's Strands agent receives
  `AgentSkills` only when a packaged child contains `SKILL.md`.
  `spec.a2a_skills` is passed separately to `A2AServer(skills=...)` and
  published as routing metadata; it neither selects nor mounts runtime Skill
  content. Registry Skill edits require an agent re-publish to refresh the
  snapshot.
- `invoke_a2a_text` and the front-desk's `a2a-jsonrpc` branch put the stable
  session in both `runtimeSessionId` and `message.contextId`. New direct A2A
  callers must do the same.
- Register stage publishes a REAL card for A2A agents: `url` = data-plane
  invocations URL, `skills` = spec.a2a_skills, `metadata.launchpad.transport`
  = `a2a-jsonrpc`. HTTP agents keep `agentcore-http` cards (platform invoke).
- UI: SERVICE PROTOCOL selector on the Strands zip configure step; AGENT CARD
  SKILLS editor (seeded from template tools; id auto-slugged from name);
  agent list shows `zip_runtime · a2a`.

- **UpdateRegistryRecord resets record status to DRAFT** (after async
  UPDATING settles) — every card refresh / re-register of an APPROVED record
  re-enters the approval flow. `backend/scripts/refresh_a2a_cards.py` settles,
  re-submits, and restores prior APPROVED records; redeploys of approved
  agents leave the record DRAFT→PENDING_APPROVAL for a human to re-approve.
- `derive_card_skills(spec)`: explicit a2a_skills win; else tools + KBs
  (name+description) + attached skills + zip-template baked-ins; code-defined
  agents (studio/container/code_bundle) derive [].

### 5. Verification

- `backend/tests/test_a2a_agent.py` (validator matrix, template compile,
  deploy params incl. update-echo, stable contextId/runtimeSessionId, invoke
  parsing incl. history-fragment guard, memory-session template wiring, card
  builder, experiment 400).
- `backend/tests/test_a2a_demo.py` (front-desk context envelope, scoped memory
  config, request-local ContextVars, routing helpers).
- Live proof: agent `aurora-faq-a2a` (KEPT — demo specialist for
  07-13-a2a-frontdesk-demo): UI create → card fetch 200 with configured
  skills → chat `17*23 → "391"` → registry record PENDING_APPROVAL with
  a2a-jsonrpc transport → experiment picker disabled.

### 5b. Front-desk routing demo (child 3)

- `samples/frontdesk_agent/main.py` (zip code_bundle deploy via
  `scripts/deploy_frontdesk_agent.py`): discover_agents = data-plane
  `search_registry_records` (returns FULL records incl. descriptors + status
  — filter APPROVED/A2A client-side); call_agent dispatches by card
  `launchpad.transport` (a2a-jsonrpc → JSON-RPC passthrough, harness →
  InvokeHarness, else {prompt}); entrypoint returns `a2a_trace` next to
  `result` — extra payload fields ride through InvokeAgentRuntime.
- Downstream `runtimeSessionId` = `sha256(f"{fd_session}:{agent_name}")`
  (derive_session, 07-14): same front-desk session + same specialist reuses
  the specialist session, so harness short-term memory survives repeat
  routings and its memory events are locatable from the front-desk session
  (random fallback when adhoc/no session). Each invoke trace entry carries
  the derived `session`. Live-proven: two routings to aurora-support in one
  fd session accumulated 10→18 events under the derived id.
- `POST /api/registry/a2a-demo` {agent_id, question} → {answer, trace} —
  bypasses invoke_agent_text deliberately (needs the trace field).
- Registry `?view=a2a-demo` sub-page narrates DISCOVER→SELECT→INVOKE→RESPOND.
- Execution role needs `launchpad-a2a-frontdesk` inline policy
  (SearchRegistryRecords/Get/List on the registry + InvokeHarness).
- Demo script: docs/a2a-demo.md (bilingual; governance loop uses
  REJECT/APPROVE — live-proven the routing flips within one question).

### 5c. Stateful conversation validation

#### Validation & error matrix

| Condition | Behavior |
|---|---|
| Same platform `session_id` on later call | Same `runtimeSessionId` and `message.contextId`; prior turns restore |
| Different platform `session_id` | Different A2A context and Memory partition |
| Missing `LAUNCHPAD_MEMORY_ID` | A2AServer remains usable with in-process context only |
| Internal `__agent_card__` factory context | No Memory session manager; card construction succeeds |
| Memory API/session-manager failure | Invocation fails; do not claim the turn was persisted |
| Caller knows another context id | Transport can attach to it; authenticated isolation must be enforced above A2A |

#### Good / base / bad cases

- **Good**: two `message/send` calls carry the same explicit `contextId`; the
  second call recalls the first after an A2AServer context eviction.
- **Base**: memory is disabled; repeated calls still share the in-process
  A2AServer agent while that context remains cached.
- **Bad**: only `runtimeSessionId` is reused. The transport session is stable,
  but the A2A server creates a new conversation and loses prior turns.

#### Wrong vs correct

```python
# Wrong: runtimeSessionId alone does not select an A2A conversation.
message = {"role": "user", "messageId": mid, "parts": parts}

# Correct: use the platform session at both protocol layers.
message = {
    "role": "user",
    "messageId": mid,
    "contextId": session_id,
    "parts": parts,
}
client.invoke_agent_runtime(runtimeSessionId=session_id, payload=...)
```

### 6. Known gaps / follow-ups

- A2A streaming (`message/stream`) not surfaced in chat (sync only).
- Authenticated human identity is not carried in direct A2A requests, so
  persistent memory is context-scoped rather than user-scoped.
- Sibling task 07-13-a2a-registry-cards generalizes card enrichment
  (skills derivation) for HTTP agents + Registry drawer AGENT CARD panel.
- OAuth inbound auth unsupported (SigV4 only, platform-mediated).
