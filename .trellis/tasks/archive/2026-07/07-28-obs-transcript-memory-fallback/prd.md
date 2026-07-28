# Observability session transcript falls back to memory for non-ledger sessions

## Problem

Opening `/observability?tab=sessions&session=ee760f57-2757-4761-947b-83f1ec6fa022` shows
the empty state *"NO MEMORY TRANSCRIPT — EXTERNAL OR MEMORY-OFF SESSION"*, even though
AgentCore Memory holds 5 conversational events for that exact session id — verified live
against `launchpad_memory-hurAGN3EnF`, under the **bare `default` actor** (317 sessions
there; the target session is one of them).

Root cause (verified, 2026-07-28):

1. `session_transcript()` (`backend/app/services/observability.py:1226-1230`) resolves a
   session only through the `chat_sessions` ledger or `eval_runs.session_ids`. Anything
   else returns `{"available": false, "reason": "not_platform_session"}`.
2. The session in question is **A/B experiment gateway traffic**: `send_gateway_traffic`
   mints `str(uuid.uuid4())` per prompt (`backend/app/optimization/service.py:873`) and
   the gateway→runtime hop passes no actor, so the runtime persists memory under the bare
   `default` actor. Those ids live in `experiments.artifacts.traffic.session_ids`, a table
   `observability.py` never consults.
3. Even an experiments-table lookup would be lossy: the stepwise traffic action overwrites
   the whole artifact (`artifact={"traffic": result}`, `service.py:1050`), so earlier
   batches' session ids are gone from the ledger while their memory events remain (the
   reported session is not in any of the 314 currently-stored ids).
4. `/v1` public-API sessions have the same class of problem from the other direction: they
   write memory under `scoped_actor(agent, "api")` (`routers/public_api.py:40`) but create
   no `chat_sessions` row, so they also render as "no transcript".

The copy itself is also wrong for these cases: memory *is* enabled and populated.

## Goal

When Observability shows a session that has memory events, render the transcript —
regardless of whether the session originated in the chat console, an eval run, an
experiment's gateway traffic, or an external `/v1` caller. When there genuinely is no
memory, say so accurately.

## Requirements

### R1 — Memory fallback for non-ledger sessions (must)

- `session_transcript()` must, when both the `chat_sessions` and `eval_runs` lookups miss,
  probe AgentCore Memory for the session id under a **bounded** set of candidate actors:
  - the bare `default` actor (gateway / experiment / unattributed runtime traffic);
  - every memory actor scoped to the agent resolved from this session's traces
    (`{agent_id}__*`, e.g. `…__api`, `…__river`).
- First candidate with conversational events wins; turns are decoded with the existing
  `_turn_text` envelope handling and ordered by `eventTimestamp`, same as today.
- The probe must be bounded and must not fan out over unrelated actors.

### R2 — Honest empty state (must)

- If no candidate actor has events, keep `{"available": false, "reason": "not_platform_session"}`
  (unchanged response shape) so existing behavior/tests hold.
- Memory/AWS failures during the probe must degrade to the same unavailable response —
  never raise into the session-detail payload (the transcript is already computed outside
  the response cache for this reason).
- Reword `obs.session.noTranscript` (en + zh-CN) so it no longer claims memory is off;
  it should state that no memory events were found for this session id.

### R3 — Label the origin in the UI (must)

- Transcript payload gains a `source` value for these sessions (`"external"`, or
  `"experiment"` when attributable) plus the `actor_id` actually read, so the console can
  explain where the conversation came from.
- The conversation panel sub-line shows that origin (new i18n keys, en + zh-CN parity).
- "OPEN IN CHAT ↗" must appear **only** for real chat-ledger sessions (`source === "chat"`).
  Today it is gated on `source !== "eval"`, which would wrongly offer resume for external
  sessions that have no ledger row.

### R4 — Experiment attribution (should)

- When the session id is present in some `experiments.artifacts.traffic.session_ids`,
  report `source: "experiment"` with that experiment id/name so the panel can name it.
- Sessions whose ids were overwritten by a later stepwise traffic run must still fall back
  to `source: "external"` and render their turns (R1 is what makes them work; R4 is only
  the label).

## Non-goals

- Changing how experiment traffic mints session ids or which actor the gateway hop uses
  (that would break comparability with already-recorded runs).
- Persisting experiment traffic session ids append-only instead of overwriting.
- Long-term (`/preferences`, `/facts`) record counts for non-chat sessions — eval and
  gateway traffic share actors across agents, so the number would be meaningless (the
  existing code already skips it for eval; keep that).
- A deep link from Observability into the Memory console.

## Acceptance criteria

- [x] `session_transcript()` returns `available: true` with ordered turns for a session
      that exists only under the bare `default` actor, and for one that exists only under
      `{agent_id}__api`, with no `chat_sessions` / `eval_runs` row.
- [x] `source` is `"experiment"` (with experiment id) when the id is in an experiment's
      stored traffic list, `"external"` otherwise; `actor_id` reflects the probed actor.
- [x] A session with no memory events anywhere still yields exactly
      `{"available": False, "reason": "not_platform_session"}`.
- [x] A raising memory client during the probe yields an unavailable transcript, not a 500.
- [x] Backend tests cover: default-actor hit, scoped-actor hit, experiment labeling,
      no-hit, and probe-error degradation.
- [x] Console: external/experiment sessions render their turns with an origin sub-line and
      **no** "OPEN IN CHAT" button; chat sessions keep the button.
- [x] `obs.session.noTranscript` copy no longer says "memory-off"; en ↔ zh-CN parity holds.
- [x] `make verify` passes.
- [x] Live check against the reported URL
      (`?tab=sessions&session=ee760f57-2757-4761-947b-83f1ec6fa022`) shows the 5-event
      conversation instead of the empty state.
