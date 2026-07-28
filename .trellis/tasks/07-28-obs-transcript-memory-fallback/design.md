# Design — memory fallback for non-ledger observability sessions

## Boundaries

Touched:

| File | Change |
|---|---|
| `backend/app/services/memory.py` | add `list_actor_ids(prefix=None, max_pages=…)` wrapper over `ListActors` |
| `backend/app/services/observability.py` | agent resolver from trace service; `_external_transcript()` probe; `session_transcript(..., agent_hint)`; `get_session` ordering |
| `backend/app/optimization/…` (read-only) | query `Experiment.artifacts` for traffic attribution (no schema change) |
| `frontend/src/lib/api.ts` | widen `transcript.source` union, add `experiment_id` / `experiment_name` |
| `frontend/src/pages/observability/SessionDetailView.tsx` | origin sub-line; gate OPEN IN CHAT on `source === "chat"` |
| `frontend/src/locales/{en,zh-CN}/common.json` | reword `obs.session.noTranscript`; add external/experiment sub keys |
| `backend/tests/test_observability.py` | 5 new cases (see implement.md) |

Not touched: `send_gateway_traffic`, the gateway→runtime actor, experiment artifact
persistence, the `/api/memory` console.

## Data flow today vs after

```
get_session(session_id, range)
  transcript = session_transcript(db, session_id)     ← runs BEFORE the trace query today
  payload    = _cached(...)  → build() → CWLI queries → rows[] (service, agent name)
  return {**payload, "transcript": transcript}
```

`session_transcript` resolution order today: `chat_sessions` row → `eval_runs.session_ids`
→ give up. The fallback needs the **agent** to build scoped-actor candidates, and the only
place the agent is known for a non-ledger session is the trace rows (span `service.name` →
`build_agent_mapper`). So `get_session` is reordered: compute `payload` first, then pass an
agent hint into `session_transcript`.

```
payload = _cached(...)
hint    = _agent_from_traces(db, payload["traces"])      # Agent row | None
transcript = session_transcript(db, session_id, agent=hint)
```

The transcript stays outside the cache (unchanged rationale: memory errors must not poison
cached span data). Reordering is safe — `session_transcript` has no effect on `build()`.

## Agent resolution from traces

`build_agent_mapper(db)` already matches a span's `service` against
`Agent.resource_id.rsplit("-", 1)[0]` and returns the agent **name**. The fallback needs the
agent **id**, so refactor into:

- `build_agent_resolver(db) -> Callable[[str | None], Agent | None]` — same matching rules,
  same precedence (later `updated_at` wins; active beats deleted), returns the row.
- `build_agent_mapper(db)` becomes a thin wrapper: `resolver(service).name or service`,
  preserving today's `"unknown"` / raw-service fallbacks byte-for-byte so no existing
  session/trace row output changes.

`_agent_from_traces` takes the first trace row with a `service` and resolves it. No agent →
no scoped candidates (bare `default` is still probed).

## Candidate actors (bounded)

Memory has no "which actor owns this session" API, so probe in this order and stop at the
first actor with conversational events:

1. `scoped_actor(agent_id, …)` — every actor id returned by `ListActors` that starts with
   `{agent_id}__`. Live shape today: 2 for the reported agent (`__api`, `__river`). Most
   specific, so probed first: an agent-scoped hit is unambiguous.
2. `"default"` — the bare actor the gateway/experiment hop leaves in place.

Cost ceiling per uncached session-detail request that misses both ledgers:
1 `ListActors` (paged, `max_pages` capped) + ≤ N+1 `ListEvents`, N = that agent's actor
count. For the reported case that is 1 + 3 calls. No probe at all for chat/eval sessions
(the existing paths return first).

`memory.list_actor_ids(prefix=None, max_pages=5)` is added to `memory.py` — the only module
allowed to touch `data_client()` for memory (per CLAUDE.md, boto3 clients stay in
`agentcore/client.py`; memory wrappers stay here). It pages `ListActors` (100/page) and
filters client-side, since `ListActors` has no prefix filter.

## `session_transcript` shape after the change

```python
def session_transcript(db, session_id, agent: Agent | None = None) -> dict[str, Any]:
    row = ...chat_sessions...
    run = None if row else _eval_run_for_session(db, session_id)
    if row is None and run is None:
        return _external_transcript(db, session_id, agent)   # ← new
    ...unchanged chat / eval logic...
```

`_external_transcript`:

```python
{
  "available": True,
  "actor_id": <probed actor>,
  "agent_id": agent.id | None,
  "agent_name": agent.name | None,
  "source": "experiment" | "external",
  "origin": "memory",
  "run_id": None,
  "experiment_id": <id> | None,      # only for source == "experiment"
  "experiment_name": <name> | None,
  "turns": [...],
  "long_term_records": None,         # meaningless for shared actors — see PRD non-goals
}
```

No events under any candidate, or any exception from the probe (`ListActors`/`ListEvents`
failure, missing `memory_id` before bootstrap) → the untouched
`{"available": False, "reason": "not_platform_session"}`. Keeping that exact dict means
`test_transcript_no_ledger_row_is_unavailable` and the frontend empty-state path stay valid.

Turn decoding reuses the existing loop (`_turn_text` + `eventTimestamp` sort + drop
tool-only turns). Extract it into a helper `_turns_from_events(events)` used by both the
chat/eval path and the new probe, so envelope handling cannot drift between them.

## Experiment attribution

`_experiment_for_session(db, session_id)`: scan `Experiment` rows (12 today, table is small
and bounded by the console's own experiment list) newest-first, JSON-decode `artifacts`, and
match `artifacts["traffic"]["session_ids"]`. First match wins; parse errors are ignored.
Because the stepwise traffic action overwrites `artifacts["traffic"]`, a hit is best-effort
labeling only — a miss still renders the transcript as `"external"`.

This is a read-only cross-module query. `observability.py` already imports from
`app.evaluation.models`, so importing `Experiment` from `app.optimization.models` follows the
existing precedent.

## Frontend

`ObsSessionDetail["transcript"].source` widens from `"chat" | "eval"` to
`"chat" | "eval" | "experiment" | "external"`, plus optional `experiment_id` /
`experiment_name`.

`SessionDetailView.tsx`:

- sub-line: `chat` → unchanged; `eval` → unchanged; `experiment` →
  `obs.session.conversationExperimentSub` (`{exp}`, `{actor}`); `external` →
  `obs.session.conversationExternalSub` (`{actor}`).
- OPEN IN CHAT: `transcript.source === "chat" && transcript.agent_id != null` (was
  `source !== "eval"`), because external sessions have no ledger row to resume.

i18n (en + zh-CN, parity enforced by `scripts/i18n_check.py`):

- `obs.session.noTranscript` → "NO MEMORY EVENTS FOR THIS SESSION ID" /
  「该 session 在记忆中没有事件」 (drops the false "memory-off" claim).
- `obs.session.conversationExternalSub` → "EXTERNAL SESSION · MEMORY ACTOR {{actor}}".
- `obs.session.conversationExperimentSub` → "EXPERIMENT {{exp}} · MEMORY ACTOR {{actor}}".

## Compatibility & risk

- Response shape is additive; `source` gains members (frontend union widened in the same
  change). No API version bump.
- Chat and eval sessions take byte-identical paths — the probe is only reached where the
  response is currently an empty state, so the worst regression case is "still empty".
- Extra AWS calls only on non-ledger sessions; bounded and skipped entirely when
  `memory_id` is unset (pre-bootstrap → probe raises → unavailable, as today).
- `build_agent_mapper` refactor is the one shared-surface risk (sessions list + trace rows
  read it); mitigated by keeping it a wrapper with unchanged semantics and by the existing
  mapper tests in `test_observability.py`.

## Rollback

Single-commit revert. The only durable artifacts are code + i18n strings; no migrations, no
AWS-side state.
