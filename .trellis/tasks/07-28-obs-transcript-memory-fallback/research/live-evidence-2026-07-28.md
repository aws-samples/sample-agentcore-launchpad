# Live evidence — 2026-07-28 (dev box, us-west-2, memory `launchpad_memory-hurAGN3EnF`)

Reported symptom: `/observability?tab=sessions&session=ee760f57-2757-4761-947b-83f1ec6fa022`
renders "NO MEMORY TRANSCRIPT — EXTERNAL OR MEMORY-OFF SESSION", while
`/memory?view=short-term&actor=600f1e6695e64d408e2778b74209f7db__api&session=2dd5570a…`
shows a normal transcript for the same agent (`lab-fund-assistant`, `zip_runtime`).

## Ledger lookups (data/launchpad.db)

- `chat_sessions`: no row for `ee760f57-…`, none for `2dd5570a…` either (the `/v1` path
  writes no ChatSession row).
- `eval_runs.session_ids`: no match for `ee760f57-…` (scanned all rows).
- `experiments`: 12 rows, 314 traffic session ids total across
  `artifacts.traffic.session_ids` — `ee760f57-…` is **not** among them (later stepwise
  traffic runs overwrote the artifact via `artifact={"traffic": result}`,
  `backend/app/optimization/service.py:1050`).
- Traffic session ids stored in those artifacts are dashed uuid4 strings — same shape as the
  reported id — produced by `send_gateway_traffic` (`service.py:873`).

## Memory probe (boto3 via `app.services.memory`)

20 actors total. Per-actor session listing:

```
600f1e6695e64d408e2778b74209f7db__api    2 sessions  (2dd5570a…, 45ef5086…)   ← the working URL
600f1e6695e64d408e2778b74209f7db__river  1 session   (682a8070…)
default                                317 sessions, dashed uuids            ← HIT ee760f57-…
default-user                            14 sessions, dashed uuids
```

`list_events("default", "ee760f57-2757-4761-947b-83f1ec6fa022")` → **5 events**.

So the conversation exists; only the lookup path is missing. `default` is the actor because
the experiment gateway → runtime hop passes no `actorId`, so the runtime persists under the
bare default.

## Code path that produces the empty state

`session_transcript()` `backend/app/services/observability.py:1226-1230` → both ledger
lookups miss → `{"available": False, "reason": "not_platform_session"}` →
`frontend/src/pages/observability/SessionDetailView.tsx:152` renders
`obs.session.noTranscript`.

`observability.py` contains no reference to `Experiment` (grep: 0 hits), and `list_events`
requires an explicit `actorId` — there is no "which actor owns this session" API, hence the
bounded candidate-probe design.

## Immediate workaround (no code change)

`/memory?view=short-term&actor=default&session=ee760f57-2757-4761-947b-83f1ec6fa022`
