# Observability Session Transcript — Resolving a Conversation From a Session Id

## Scenario: `/api/observability/sessions/{id}` renders the conversation behind a trace session

### 1. Scope / Trigger

Use this contract when changing `session_transcript()` / `get_session()` in
`app/services/observability.py`, the Observability session-detail panel
(`frontend/src/pages/observability/SessionDetailView.tsx`), or anything that mints
session ids reaching AgentCore Runtime (chat, `/v1`, eval runs, experiment gateway
traffic).

The load-bearing fact: **a `session.id` in `aws/spans` may have been minted by four
different producers, and only two of them leave a row in a platform ledger.** The
transcript therefore has a resolution *order*, not a single lookup.

Related: [Memory Console](./memory-console.md) (read-side conventions over the same
Memory resource), [Claude SDK AgentCore Memory](./claude-sdk-agentcore-memory.md)
(the actor-scoping rule this must read back), [Experiment Stepwise Actions](./experiment-stepwise.md)
(the traffic step whose session ids appear here).

### 2. Session-id producers and where their memory lands

| Producer | Session id shape | Ledger row | Memory actor |
|---|---|---|---|
| Chat console (`/api/chat/{id}`) | 64-hex (`new_session_id()`) or caller-supplied | `chat_sessions` | `scoped_actor(agent_id, human)` |
| Public API `/v1/agents/{id}/invoke[-stream]` | 64-hex minted per call | **none** | `scoped_actor(agent_id, "api")` (`actor_id` defaults to `"api"`) |
| Eval run (`StartBatchEvaluation` / eval invoker) | 64-hex per example | `eval_runs.session_ids` | bare `"default"` (runtime-backed methods write no events at all → OTEL content logs) |
| Experiment gateway traffic (`send_gateway_traffic`) | dashed `uuid4()` | `experiments.artifacts["traffic"]["session_ids"]`, **overwritten** by the next traffic run | bare `"default"` (the gateway→runtime hop passes no actor) |

Consequences that the code must respect:

- A `/v1` or gateway session has **no ledger row** but **does** have memory. "No
  ledger row" must never be reported as "no memory / memory off".
- Experiment traffic ids are **not** a reliable index: the stepwise traffic action
  persists `artifact={"traffic": result}`, replacing the previous batch. Attribution
  is best-effort labeling only; resolution must not depend on it.
- Memory has **no** "which actor owns this session" API — `ListEvents` requires an
  explicit `actorId`. Any fallback must therefore probe a *bounded candidate set*.

### 3. Contracts

```python
# app/services/observability.py
build_agent_resolver(db) -> Callable[[str | None], Agent | None]   # service.name → Agent row
build_agent_mapper(db)   -> Callable[[str | None], str]            # display wrapper over it
_agent_from_traces(db, traces) -> Agent | None
_turns_from_events(events) -> list[dict]                           # sorted, envelope-decoded
_experiment_for_session(db, session_id) -> Experiment | None       # labeling only
_external_transcript(db, session_id, agent) -> dict
session_transcript(db, session_id, agent: Agent | None = None) -> dict
NOT_PLATFORM_SESSION = {"available": False, "reason": "not_platform_session"}
```

Resolution order in `session_transcript`:

1. `chat_sessions` row → `source: "chat"`, memory read under
   `scoped_actor(row.agent_id, row.actor_id)`, reconciled against `ChatMessage`
   when the ledger disagrees (`origin: "ledger"`), long-term record count included.
2. else `eval_runs.session_ids` → `source: "eval"`, bare `"default"` actor, OTEL
   content-log rebuild for `RUNTIME_LOG_METHODS` (`origin: "logs"`).
3. else `_external_transcript` → probe memory:
   - candidates = `memory.list_actor_ids(prefix=f"{agent.id}__")` (only when the
     agent is known) **then** `"default"` — scoped actors first because they are
     unambiguous; the bare actor is shared across agents and runs.
   - first candidate with conversational turns wins → `source: "experiment"` when
     `_experiment_for_session` matches, else `"external"`; `actor_id` is the actor
     actually read; `long_term_records` is **None** (namespaces are keyed on the
     actor alone, so a shared actor's count says nothing about this session).
   - no candidate has turns, or **any** exception in the probe →
     `NOT_PLATFORM_SESSION`.

`get_session` ordering is load-bearing: the trace payload is computed **first**, and
`_agent_from_traces` derives the agent hint from it, because a session with no ledger
row carries its only agent signal in `service.name`. The transcript stays **outside**
the 60s response cache so a memory failure degrades per-request instead of poisoning
cached span data.

`memory.list_actor_ids(prefix=None, max_pages=5)` pages `ListActors` (100/page) and
filters client-side — the API has no prefix filter.

### 4. Frontend contract

- `ObsTranscript.source` = `"chat" | "eval" | "experiment" | "external"`; optional
  `experiment_id` / `experiment_name`.
- Sub-line per source: `conversationSub` (chat) · `conversationEvalSub` /
  `conversationEvalLogsSub` (eval) · `conversationExperimentSub` ·
  `conversationExternalSub`. Every variant names the memory actor that was read.
- **OPEN IN CHAT is chat-only** (`source === "chat" && agent_id != null`). Eval,
  experiment and external sessions have no `chat_sessions` row to resume.
- `obs.session.noTranscript` states that **no memory events were found for this
  session id** — it must not claim the session is external or that memory is off,
  both of which are now resolvable states.

### 5. Good / Base / Bad cases

- **Good**: dashed-uuid gateway session, no ledger row → turns render, sub-line
  `external session · agentcore memory · actor default`, no resume button.
- **Base**: chat session → identical behavior to before the fallback existed
  (ledger reconciliation, long-term memnote, resume button).
- **Bad**: a session id from another account/region, or memory not bootstrapped →
  `NOT_PLATFORM_SESSION`, empty state, no exception, no 500.

### 6. Tests required

In `backend/tests/test_observability.py` (all memory calls stubbed — `conftest.py`
does **not** block boto3, and a live `config/launchpad.yaml` will otherwise make the
probe hit real AWS):

- default-actor hit: `source == "external"`, `actor_id == "default"`, turns ordered
  oldest-first, `long_term_records is None`.
- scoped-actor preference: events under `{agent_id}__api` win and `"default"` is
  never probed afterwards.
- experiment labeling: id present in `artifacts.traffic.session_ids` →
  `source == "experiment"` + `experiment_id`.
- no memory anywhere → exactly `{"available": False, "reason": "not_platform_session"}`.
- `list_actor_ids` raising **and** `list_events` raising → same unavailable dict.
- `get_session` passes the span-resolved agent into `session_transcript`.

### 7. Wrong vs Correct

#### Wrong — treat "no ledger row" as "no memory"

```python
if row is None and run is None:
    return {"available": False, "reason": "not_platform_session"}
# → /v1 and experiment-gateway sessions render "memory off" while ListEvents
#   would return their full conversation under "default" / "{agent}__api".
```

#### Correct — bounded probe over the actors that can own such a session

```python
if row is None and run is None:
    return _external_transcript(db, session_id, agent)   # scoped actors, then "default"
```

#### Wrong — index external sessions on the experiment ledger

```python
sids = experiment.artifacts["traffic"]["session_ids"]      # overwritten each traffic run
if session_id not in sids:
    return NOT_PLATFORM_SESSION                            # loses every earlier batch
```

#### Correct — memory decides availability, the experiment only labels it

```python
turns = _turns_from_events(memory.list_events(actor_id, session_id, 100))
experiment = _experiment_for_session(db, session_id)       # may be None → "external"
```
