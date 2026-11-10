# Detached turns — architect assistant (v1) and Chat playground (v2)

Contract for the architect assistant's *turns* after N-ARCH-obs15 (`cn/r10-a`, 2026-10-15) and, since
v2 (`cn/r11-b`, 2026-10-16, §8), for the Chat playground's turns, which follow the same lifecycle.
Code: `backend/app/services/live_turns.py` (the shared buffer / registry / batcher / ledger tail /
streaming response), `backend/app/assistant/service.py` (section *live turns*),
`backend/app/routers/assistant.py`, `frontend/src/lib/assistant-turn.ts`; chat:
`backend/app/services/chat_turns.py`, `backend/app/routers/chat.py`, `frontend/src/lib/chat-turn.ts`.
Register rows: `docs/cn/reports/bugs-round-5.md` → N-ARCH-obs15 (v1 + v2 state), N-CHAT-obs1.

## 1. Why

A proposal turn takes 5–10 minutes. Before this change the turn's producer lived and died
with the SSE response: when the browser connection closed (phone screen lock, app
backgrounded, workspace scope change, reload) the response generator got `GeneratorExit`,
the partial answer was persisted as `turn_interrupted`, a proposal that had not fully
streamed was discarded, and the member had to resend and wait again.

**Ruled change:** a turn is a server-side unit of work, independent of any connection.

## 2. Lifecycle

```
POST …/turns ──► start_turn() ──► LiveTurn (registry, one per conversation)
                                    │  driver thread iterates run_turn()
                                    │  every event → seq (1, 2, …) → in-memory buffer
                                    │                              → assistant_turn_events
                                    ▼
      subscribers: the POST response (from seq 0) · GET …/turns/{turn}/stream?from=<seq>
```

* **Claim** — unchanged. `run_turn` takes the atomic claim (`active_turn`, token, `TURN_CLAIM_TTL_S`
  = 1800 s); one running turn per conversation; a second `POST` answers
  `409 assistant.turn_in_progress` (`require_turn_capacity` before the stream, and the
  registry refuses a second `start_turn` for the same conversation). `_LIVE_TURNS` still fences
  every write; a live local claim is never taken over.
* **Producer** — `start_turn()` spawns `assistant-turn-driver-<cid>` which iterates the unchanged
  `run_turn` generator to its end. Each non-heartbeat event is stamped with a monotonic `seq`
  (per turn, starting at 1), published to the `LiveTurn` buffer (condition variable) and
  appended to the `assistant_turn_events` ledger table — batched at most every
  `TURN_EVENT_FLUSH_S` (1 s) while only deltas arrive, immediately for any other event and for
  the terminal one. **The terminal event is durable before `release_turn` runs** (the generator
  is suspended at the terminal `yield` until the driver resumes it).
* **States** (`live_turn.state`): `running` → `finishing` (the upstream stream ended; the reply
  is being validated / persisted) → gone (`live_turn: null`).
* **Subscribers** — `LiveTurn.subscribe(after)` yields buffered events with `seq > after`, then
  live ones, a `heartbeat` whenever nothing arrived for `HEARTBEAT_S`; the terminal event is
  handed out only once the turn is finalized (claim released, upstream closed) so a reader that
  saw `done` may rely on the ledger. Closing a subscriber (client gone, `GeneratorExit`) closes
  the subscription only. **Pre-`meta` window:** `live_run(cid, turn)` never matches a live run
  whose turn number is not published yet (between `start_turn` and the claim); it waits for
  `meta` up to `HEARTBEAT_S`, then compares. A reattach to turn N racing a just-started N+1
  therefore takes the ledger path and ends on N's own terminal event — it never tails N+1.
* **Driver cleanup is explicit** — `_drive_turn`'s `finally` closes the `run_turn` generator
  (`live.run.inner.close()`, idempotent) before the turn is marked done, so a failure in the
  driver's own loop body (publish / event-log write) still runs `run_turn`'s finalizer —
  interrupted row, claim release, upstream close — deterministically, never left to GC.
* **Interrupted** — only the producer's own failure, a lost claim (`assistant.turn_superseded`),
  an explicit `TurnRun.cancel()` (no route calls it any more) or a dead process persist the
  `turn_interrupted` marker. The text no longer blames the connection:
  `interrupted: the turn stopped before the reply finished — …`.
* **Startup sweep** (`clear_stale_turn_claims`) — a claim found at boot belongs to a dead
  process. Before clearing it, the sweep rebuilds the partial text from the turn's event log
  (text after the last `retry` event), runs `_persist_interrupted` (partial `assistant` row +
  `turn_interrupted` row + a `launchpad-proposal` block that had streamed completely, validated
  like a completed reply) and appends a terminal `error {"message": "interrupted"}` event so a
  reattaching client sees the turn end. A turn that already has its terminal row, or never wrote
  its user row, is only cleared. **Never resumed blind.**

## 3. Endpoints (additive)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/assistant/architect/conversations/{id}/turns` | Unchanged request/refusals. SSE frames now carry `id: <seq>`. Closing the response does **not** stop the turn. |
| `GET` | `/api/assistant/architect/conversations/{id}/turns/{turn}/stream?from=<seq>` | Reattach. Replays persisted events with `seq > from` (default 0), then tails the live producer (same process) or polls the ledger every `HEARTBEAT_S` while the claim is held (another process / after a restart), then the terminal event. `404 assistant.turn_not_found` (turn ≤ 0 or > `turns`); `409 assistant.turn_not_yours {author}` when another member sent the turn (the sender = live starter, else the turn's `user` row author) — collaborators only: a **read-only viewer** tails any turn (§9); `422 assistant.invalid_seq` (`from` not an integer in `0..2^63-1`; `turn` outside it is `422 validation.invalid_request` — N16-obs86). A turn with no log and no claim ends at once: `done` when it has an `assistant` row, else `error {"message": "interrupted"}`. |
| `GET` | `/api/assistant/architect/conversations/{id}` (+ the list) | `live_turn: {turn, seq, started_at, state: running\|finishing, author} \| null` — from the registry when live here, else from a still-valid claim (`seq` = last persisted). |

Route policy: the stream route is `MEMBER` (parity with the turn).

## 4. Events and `seq`

Event order per turn: `meta → (tool | tool_input | delta | retry)* → ((autocorrect | reask) → (tool | tool_input | delta | retry)*)? → proposal? → preparation? → done` or `error` (`autocorrect`: §11, `reask`: §12).
`seq` is assigned by the single producer; `(conversation_id, turn, seq)` is unique
(`uq_assistant_turn_events_seq`) and contiguous from 1. `heartbeat` frames are SSE comments
(`: keep-alive`) and never get a `seq`. A `retry` event means: discard what the previous attempt
streamed (text and tool cards) — the reducer and the sweep both honour it. Its data is
`{attempt, reason, backoff_s}`: after a transient upstream failure the single replay waits
`assistant_replay_backoff_seconds` (default 15 s, N-ARCH-obs18) before the fresh session, so a
`retry` may arrive that long after the last `tool` / `delta`; heartbeats keep flowing meanwhile.

SSE frame: `id: 7\nevent: delta\ndata: {"text": "…"}\n\n`.

The terminal `error` event's data is `{code, message}` for an `AppError` (the console renders
`apiErrors.<code>`), else `{message}` rendered verbatim; when the Harness stream ended on the
AgentCore SDK's terminal error frame (`{"error", "error_type", "message"}` — a model timeout,
throttle or validation error after a tool step, N-ARCH-obs18) the data also carries
`detail: {error_type, error, message}` and the `message` names the `error_type`, so the real
cause is in the log and on the screen instead of `harness.incomplete_response`.

`assistant_turn_events` rows: `workspace_id, conversation_id, turn, seq, event, data (JSON), created_at`;
deleted with the conversation (`purge`). Retention beyond that is v2.

## 5. Client contract (`frontend/src/lib/assistant-turn.ts`)

* `runTurnLink({open, reattach, signal, onEvent, onStatus, wait})` consumes a turn to its terminal
  event. A stream that closes without `done`/`error` after `meta` was seen is a **reconnect**:
  status `reconnecting` → `GET …/stream?from=<last seq>` with exponential backoff
  (1 s, 2 s, 4 s … capped at 30 s; `domWait` wakes early on `visibilitychange` → visible, `online`,
  or abort) → status `reattached`. The link gives up (`gave_up`) only once
  `TURN_RECONNECT_GIVE_UP_MS` (= claim TTL, 1800 s) has passed since the first disconnect; the
  page then shows the existing `assistantPage.turnInterrupted` copy. Refusals of the reattach
  itself (`turn_not_found`, `turn_not_yours`, auth) are final. The initial `POST` being refused
  throws exactly as before.
* `signal` abort (scope change via `bump()`, unmount, selecting another conversation) ends the
  **subscription** only (`aborted`); the turn keeps running server-side.
* Page load: `reattachPlan(detail, viewer)` → when `detail.live_turn` is the viewer's (or the
  viewer is unknown), show the transcript without the live turn's non-user rows, append a
  streaming row and run the link with `open = GET …/stream?from=0`.
* UI: the streaming row shows `assistantPage.turnReconnecting`
  ("Generating in the background — reconnecting…" / "正在后台生成，连接恢复后继续…") while
  `reconnecting`; once a reattach succeeds (`onStatus("reattached")`) both consumers toast
  `assistantPage.turnReattached` ("Reattached to the turn that was still generating." /
  "已重新接入仍在生成中的这一轮。"); "重新发送 / edit & retry" stays only for truly interrupted turns.

## 6. Tests

Backend `tests/test_assistant_async_turns.py` (15: + `test_driver_failure_closes_run_turn_and_finalizes_without_gc`,
`test_reattach_to_turn_n_never_tails_a_pre_meta_turn_n_plus_one`, `cn/r10-a2`;
`test_read_only_admin_viewer_tails_a_members_live_turn_but_never_sends`, `cn/r11-e`) + adapted residuals in
`tests/test_assistant_residuals.py` (`test_whole_asgi_disconnect_finalizes_the_turn_deterministically`,
`test_disconnect_while_the_upstream_read_blocks_is_cleaned_up_within_budget`: disconnect → turn
completes, upstream closed by the turn's own finalizer). Frontend
`src/lib/assistant-turn.test.ts` (17 — `reattachPlan` watch mode, `cn/r11-e`).

## 7. Left for later

* ~~Chat page parity~~ — shipped as v2, §8.
* A graceful-shutdown hook that finalizes live turns before the process exits (today the next
  startup sweep does it).
* **Multi-process deployment.** The startup sweep finalizes **every** claim it finds at boot,
  whatever its age — `_claim_is_stale` / `TURN_CLAIM_TTL_S` are deliberately not consulted. This
  is correct for the production deployment, which runs exactly one backend process per ledger: a
  claim can only be held by a live turn of this process, so any claim present at boot is an orphan.
  Running several processes against one ledger would need the sweep gated on
  `_claim_is_stale` (or a per-process owner marker on the claim), otherwise a restarting
  sibling would interrupt a turn that is still live elsewhere.
* A collaborator watching another member's live turn in a shared conversation (today 409; the
  transcript reload shows the result). A *read-only* administrator viewer already tails it
  (§9); extending the watch mode to collaborators means dropping the sender rule for the
  stream route and pinning the new one in `test_reattach_refusals_unknown_turn_and_another_members_turn`.
* Pruning `assistant_turn_events` / `chat_turn_events` of old completed turns.
* The public `/v1/agents/{id}/invoke-stream` stays connection-bound (an API client has no page to
  reattach from; `record_stream` is unchanged).

## 8. Chat playground parity (v2)

The Chat page's turn (`POST /api/chat/{agent_id}`) used to be the response body itself: the generator
in `routers/chat.py` drove `chat_stream` *and* wrote the `ChatMessage` rows, so a closed connection got
`GeneratorExit`, the open bubble was saved as whatever had streamed and the member had to resend.
Since v2 a chat turn is the same kind of unit of work as an architect turn; what differs is listed here.

### 8.1 Shared vs domain code

`services/live_turns.py` holds what v1 had inlined in the assistant service and is now shared, behaviour
unchanged: `LiveTurn` (buffer, `publish`, `subscribe` with the terminal-event hold-back), `LiveTurnRegistry`
(one live turn per key, the pre-`meta` wait of §2), `EventLogBatcher` (§2 flush policy), `tail_turn` (§3
ledger path), `TurnStreamResponse` / `sse_body` / `parse_from`. The assistant keeps its public names
(`service.LiveTurn`, `live_run`, `stream_turn`, `HEARTBEAT_S`, `TURN_TERMINAL_EVENTS`, …) as thin wrappers
or aliases; its claim (`assistant_conversations.active_turn*`), `run_turn`, transcript rows and sweep are
untouched. `services/chat_turns.py` is the chat domain: the claim on `chat_sessions`, the producer wrapper
around `chat_stream`, the transcript reducer, the sweep.

### 8.2 Lifecycle differences

* **Key and claim.** The live key is `(agent_id, session_id)`. The router fixes the session id before the
  turn starts (`req.session_id`, else `new_session_id()` — the same kind of id `chat_stream` would have
  minted at `meta`), checks `require_turn_capacity` (409 `chat.turn_in_progress {active_turn}` when a turn
  is live here or the session row holds a claim younger than `TURN_CLAIM_TTL_S` = 1800 s) and starts the
  driver. The durable claim — `chat_sessions.active_turn` (= `turns` after `track_session` counted the
  turn), `active_turn_started_at`, `active_turn_token` — is taken **at `meta`**, when the session row
  exists; before that the in-process registry alone refuses a second start. Released after the terminal
  event is durable, before subscribers are handed it.
* **Turn number and sender.** `meta.turn` (additive) is the session's turn count; `GET …/turns/{turn}/stream`
  keys on it. `meta.author` (additive, r11-b2) is the member who sent **this** turn, in the ledger's
  human-actor convention (`"river"` while the auth gate is off): the base `POST` lets any workspace member
  continue a session while `chat_sessions.actor_id` keeps the first sender (the Memory owner), and
  `ChatMessage` rows carry no author, so the logged `meta` is the durable per-turn ownership record. `/v1`
  streams carry neither (they do not go through the driver).
* **Producer.** `chat_stream` is unchanged (so is `invoke_agent_events`: the policy gate still runs there,
  on the driver thread, before any AWS call; a refusal is still the first `error` after `meta`, with its
  `policy_decisions` row; the p2-a `policy_denied` / `policy_decision` frames flow through the log with their
  `decision_id`). **Terminal `error` data — §4 parity (N17-obs98, `cn/r17-o`).** The chat turn's `error`
  event carries exactly what §4 gives the architect turn: `{code, message[, detail]}` for an `AppError`
  (the console renders `apiErrors.<code>`), else `{message}` with the exception class and text
  (`SdkStreamError: runtime returned error: …`) — and when the Runtime stream ended on the AgentCore SDK's
  terminal error frame (`{"error", "error_type", "message"}`), on a malformed frame (N17-obs96,
  `error_type: MalformedFrame`) or on a structured REJECTED body (N17-obs97, `error_type: RuntimeRejected`,
  `detail.code`), the data also carries `detail: {error_type, error, message[, code, state]}` — the
  `chat_stream` branch mirrors `service.run_turn`'s. The console shows the `message`; the `detail` is for
  the event log and the Inspector. Two codes the hub itself raises on a turn: `agent.empty_answer` (the
  runtime completed with nothing to show, N17-obs114 — an `error` row where the console used to show
  nothing) and, on the **buffered** path only (`invoke_agent_events` → `invoke_agent_text`: studio and the
  A2A fallback), `agent.runtime_error` — the 502 envelope the sync entrances answer (api.md `/v1`), whose
  `detail` is the same frame structure. The native-stream path keeps the §4 `{message, detail}` shape.
  Per event the driver runs the transcript reducer **first**, then publishes, then logs — so
  "event in the log ⇒ its rows exist", which is what lets the sweep rebuild only the open bubble. A
  scripted producer that ends without `done`/`error` gets a terminal `error {"message": "interrupted"}`
  appended (`chat_stream` itself always ends on one).
* **Transcript reducer** (`TranscriptRecorder`) — the former `generate()` body, byte-for-byte: `meta` →
  `track_session` + user row; `tool` splits the bubble; `auth_required` after an open bubble is saved after
  its text; `policy_denied` follows its tool row; `truncated`/`complete{truncated}` flags the bubble;
  `error` keeps the partial answer and adds the error row; `done` closes the bubble.
* **Event log** — `chat_turn_events {workspace_id, agent_id, session_id, turn, seq, event, data,
  created_at}`, unique `(agent_id, session_id, turn, seq)`, a workspace-scoped table. A sibling of
  `assistant_turn_events` rather than a `kind` column on it: that table FKs `assistant_conversations` and is
  purged with the conversation, chat rows are never deleted and key on the session. **C4:** the stored
  `auth_required` frame drops the single-use authorization `url` (the history row drops it too); the wire
  and the in-memory buffer carry it. **Size:** the log keeps every frame of every console turn, so its
  `delta` rows duplicate the `ChatMessage` text (roughly 2× the transcript, plus one row per tool / policy
  frame); nothing prunes it yet — pruning (e.g. dropping the log of a turn once it ended and its rows are
  durable) is v3.
* **Sweep** (`chat_turns.clear_stale_turn_claims`, called next to the assistant's in `main.py`): every
  claim found at boot is a dead process's. If the turn's log has no terminal event, the log is replayed
  through the reducer with persistence off, then the open bubble (text after the last `tool`, with its
  truncated flag and deferred asks) and an `error` row `interrupted: the turn stopped before the reply
  finished — what had streamed is kept` are written, and a terminal `error {"message": "interrupted"}`
  event is appended. A claim whose log already ended is only cleared. Never resumed.

### 8.3 Endpoints (additive)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/chat/{agent_id}` | Unchanged request and frames; frames carry `id: <seq>`, `meta` carries `turn`. Closing the response does **not** stop the turn. `409 chat.turn_in_progress {active_turn}` before the stream when the session's turn is still running. |
| `GET` | `/api/chat/{agent_id}/sessions/{session_id}/turns/{turn}/stream?from=<seq>` | Reattach: replays `seq > from` (default 0), tails the live producer (same process) or polls the ledger every `HEARTBEAT_S` while the claim is held, then the terminal event. `404 chat.session_not_found` (another agent / workspace), `404 chat.turn_not_found` (turn ≤ 0 or > `turns`), `409 chat.turn_not_yours {author}` when another member **sent the turn** — ownership is per turn (`chat_turns.turn_author`): the live run's username while the turn runs here, the `author` of its logged `meta` otherwise, the session's actor only for a log from before the stamp; a member who continued a shared session reattaches to their own turn, the session's first owner is refused on it, a viewer who sent neither is refused on both; since N16-obs71 another member's session is `404 chat.session_not_found` before any turn is looked at. `422 chat.invalid_seq` (`from` not an integer in `0..2^63-1`; `turn` outside it is `422 validation.invalid_request` — N16-obs86; `tests/test_turn_param_bounds.py`). A turn with no log and no claim answers a bare `done` (the history has it) — in particular **a turn made through `POST /v1/agents/{id}/invoke`** (sync, no event log; N16-obs112): it counts as a session turn, and a reattach to it answers `200` with exactly one frame, `event: done` `{"turn": n}` — no `meta`, no `delta`; the console reads the answer from `…/history`. Route policy: `MEMBER`. |
| `GET` | `/api/chat/{agent_id}/history?session_id=` | `+ live_turn: {turn, seq, started_at, state: running\|finishing, author} \| null` — the page-load detail; `author` is the turn's sender (same rule), which the page compares with the signed-in member before reattaching. |
| `GET` | `/api/chat/{agent_id}/sessions` | each row `+ live_turn` (same shape). |

### 8.4 Client (`frontend/src/lib/chat-turn.ts`)

`runChatTurnLink` is `runTurnLink` (§5 — same backoff, `domWait` wake-ups, give-up at the claim TTL) with
the chat reattach (`chatApi.reattach`), the chat refusal codes as final (`chat.turn_not_found`,
`chat.turn_not_yours`, `chat.session_not_found`, `chat.invalid_seq`, `agent.not_found`, auth) and
`chat.turn_lost` for a close before `meta`; the session id is read from a getter because a new session's id
is only known from `meta`. Pure helpers shared by `pages/Chat.tsx` and `v2/pages/Chat.tsx`:
`markChatReconnecting` (flags the open bubble; an empty placeholder when none is open yet, removed on
reattach — the link reports `reattached` before it dispatches the replayed events), `giveUpChatMessages`,
`chatReattachPlan(rows, history.live_turn, viewer)` (keeps the rows up to the last `user` row — one turn
runs per session, so the live turn's rows are the trailing ones — and **adds no placeholder**: the replay
from seq 0 builds the rows exactly as a just-sent turn does and the page's busy state is the activity
signal; r11-b2 — the earlier empty streaming bubble was never adopted by the first replayed `delta` and
stayed as an empty row above the answer), and `appendDelta(messages, text, open)` — the one `delta`
reducer of both pages (moved here from `v2/pages/chat/messages.ts`, which re-exports it; the classic
page's inline copy is gone). Copy: `chatPage.turnReconnecting` /
`turnReattached` / `turnInterrupted`. The `error` event with code `policy.denied` still renders the V2
policy-deny card; `policy_denied` frames still render their cards. An unmount aborts the subscription only.

### 8.5 Tests

Backend `tests/test_chat_async_turns.py` (16): seq/`meta.turn`/log parity; a subscriber leaving mid-turn
→ rows identical to a watched turn; reattach from a seq (live tail, after completion, ledger poll while
another process holds the claim); refusals; second POST → 409 (live, foreign fresh claim) and the orphan
takeover past the TTL; the policy gate's refusal + audit row + frame; p2-a frames through log and replay;
the `url` never logged; producer failure while nobody watches; a producer ending without a terminal event;
the sweep (finalize from the log, idempotent, only-clear when ended); r11-b2: per-turn ownership on a
shared session (live, finished, wire 409 for the first owner on the continuer's turn) and the
session-actor fallback for a log without `author`. Frontend `src/lib/chat-turn.test.ts` (15): + the
page-load replay driven through the pages' reducer steps (`meta, delta, delta, done` → exactly one agent
row; `meta, tool, delta, done` → tool row then one agent row; a reconnect before the first delta), and
`appendDelta`. No existing chat test needed an adapted assertion: the `POST` wire and the `ChatMessage`
rows are unchanged (`meta.author` is additive).

## 9. Viewers — who reads a turn, who tails it (upstream #241 parity, `cn/r11-e`, 2026-10-16)

Three roles meet the stream route, resolved per request by `service.viewable_conversation` +
`service.require_turn(…, read_only=…)` (`backend/app/routers/assistant.py::turn_stream`):

| Viewer | Resolves through | `GET …/turns/{turn}/stream` | `POST …/turns` |
|---|---|---|---|
| **Owner** / the member who sent the turn | collaboration (`may_collaborate`) | 200 — reattach as the sender (page: composer busy, "reconnecting…", retry) | as before |
| **Collaborator** on a shared conversation who did not send the turn | collaboration | `409 assistant.turn_not_yours {author}` — unchanged sender rule; the transcript reload shows the result | 200 (one turn at a time: `409 turn_in_progress` while another runs) |
| **Read-only viewer** — an administrator on another member's *unshared* conversation (`read_only: true`) | `may_view` (reads only) | 200 — **tails any turn read-only** (`require_turn(read_only=True)` keeps only the `404 turn_not_found` check). Rationale: the stream is a subscription and writes nothing; the detail already exposes `live_turn` and every event lands in the transcript the viewer may read; and a read-only viewer never was and never will be a sender, so there is no "its own turn" to protect | `404 assistant.conversation_not_found` — the collaboration rule, before the live claim's 409 |
| Another **member** (unshared, not the owner) | — | `404 assistant.conversation_not_found` | 404 |

Sharing moves a read-only viewer into the collaborator row: an admin that tails a member's
turn, then shares the conversation and reloads, gets the collaborator's 409 for that turn (the
page it already has keeps its open subscription). Unsharing moves it back.

**V2 page (`frontend/src/v2/pages/assistant/AssistantDetail.tsx`):** `reattachPlan(detail, username,
{ watchReadOnly: true })` returns `watch: true` for a `read_only` detail whose `live_turn.author` is
another member; `attachLive(…, watch)` then tails from seq 0 **without** setting `busy` and the
`ReadOnlyContext` hides the composer, edit-and-retry, catalog refresh, preparation, proposal
actions, evaluation-asset writes, next-step runs and recommendation new/accept — the viewer sees
the text stream in and nothing that could send or reattach as the sender. Tests: `test_read_only_admin_viewer_tails_a_members_live_turn_but_never_sends`
(backend), `reattachPlan` watch-mode cases in `src/lib/assistant-turn.test.ts`.

## 10. CLEAR — the conversation purge job (upstream #254 parity, `cn/r15-c`, 2026-10-19)

`DELETE …/conversations/{id}` is owner-bound (§9's collaborators and read-only viewers get
404). Upstream afed15d8 turned the inline purge into a durable job after `DeleteHarness` 409'd
on a canary's named endpoints (issue #246); the CN port keeps the shape and swaps the
dependencies for what exists here (`backend/app/assistant/purge.py`).

**Footprint** (`GET …/footprint`, ledger only): `agents[]`, `canaries[]` (the Runtime canaries
on those Agents — `kind: runtime`, `resources{ab_test, endpoints[], online_evaluations}`,
`pending`), `operations[]`, `datasets[]`, `blockers[]` (`turn` / `preparation` / `operation` /
`job` / `canary`), `required_permissions[]`, `can_clear`, `purge` (the newest job: `job_id`,
`status`, `error`, `blocker`, `step`). No `experiments` and no `gateway`: CN retired the
configuration experiments (N13-obs2) and the canary routes in the invoke chain.

**Request** (`purge.request_purge`, before any side effect): blockers → `409
assistant.conversation_busy`; missing `agents.deploy` / `agents.delete` → `403
auth.permission_required`; then **creator-or-admin on every canary and Agent** the clear would
tear down (`services/ownership.require_creator_or_admin`, `403 canary.forbidden` /
`agent.forbidden`, `users-and-ownership.md` §8) — a NULL creator is an administrator's. A
transcript-only conversation is deleted inline (`200 deleted: true`); anything else pins the
ownership evidence (Agent / canary / Dataset ids, unioned with the previous attempt's) on a
`purge_assistant_conversation` job claimed through `assistant_conversations.purge_job_id` (one
live job per conversation; a repeated DELETE returns it) and answers `202 {job_id}`.

**Worker** (`purge.execute_purge_job`; daemon thread, advisory file lock per conversation,
`resume_pending_jobs` re-adopts a `running` row after a restart): steps `canaries → operations
→ datasets → agents → canary_artifacts`, each fenced (job still `running`, conversation still
claimed) and checkpointed in `payload.progress[key]`; a verified checkpoint and an accepted
Runtime delete request carry forward to the next attempt.

| step | what it does | verified by |
|---|---|---|
| `canary:<id>` | `canary_service.act_cleanup` under the canary's own `running_action = cleanup` claim (progress prefixed `MARKER_PREFIX` so a crashed attempt's claim is re-adopted; `clear_stale_running_actions` skips a canary whose Agent's conversation has a live purge job whatever its progress says — `purge.canary_under_live_clear` — and clears only by compare-and-set on the value it read, N15-obs10); `cleanup_blockers` = any `skipped` result except a retained `s3:` zip and the `endpoints-gone` wait marker | `canary_service.remaining_resources`: `online.config_ids_for_owner` (ledger) + `GetAgentRuntimeEndpoint` for both named endpoints; bounded `CANARY_GONE_TIMEOUT_S` |
| `operations` | the fenced `evaluation_assets.cleanup_operation` | its `cleaned` status (`conversation_assets_remain` otherwise) |
| `datasets` | local `EvalDataset` rows | — |
| `agent:<id>` | preset / discovered / deploying guards; `foreign_endpoints` (a named endpoint no canary owns → `purge_agent_dependency`); `agents_router.delete_agent_cloud_resource`; then `deployer/teardown.cleanup_residue` (role, artifacts, memory snapshots / records, A2A Registry record, log groups); `mark_agent_deleted` | `GetAgentRuntime` not found (`AGENT_GONE_TIMEOUT_S`); a `failed` role step → `purge_role_remains`, a `failed` Registry step → `purge_step_failed`; the other residue steps stay best-effort and are recorded on the result (`agents[].teardown`) |
| `canary-artifacts:<id>` | the retained candidate zips (`retained_artifact_keys`) | best-effort (logged) |
| ledger | `_delete_rows` (operations, plans, proposals, messages, **turn events**, conversation) | fenced finalization |

Blocker codes: `assistant.purge_canary_incomplete`, `conversation_assets_remain`,
`purge_still_deleting`, `purge_delete_failed`, `purge_agent_dependency`, `purge_agent_conflict`,
`purge_agent_busy`, `purge_role_remains`, `purge_dependency_busy`, `purge_unowned`,
`purge_step_failed`, `purge_superseded`. While a job is live: conversation writes `409
assistant.conversation_clearing` (`service.accessible_conversation`), the Agents' canary action
/ create, redeploy and delete routes `409 assistant.agent_clearing` (`purge.refuse_while_clearing`).
`cascade_principal` (N-ARCH-obs6) reassigns a conversation with canaries or a live job like one
with Agents.

**Console** (`v2/pages/assistant/useClearConversation.tsx`, `lib/assistant.ts`): the confirm
names the canaries (`clearCanaryList`), the last failed attempt and a live job; `runConversationClear`
follows `GET /api/jobs/{id}` until terminal, closing the dialog only detaches.

**Tests:** `backend/tests/test_assistant_purge_dependencies.py` (ordering, blockers, resume,
ownership, fences, the real readback), `tests/faults/test_restart_sweeps.py::
test_backend_restart_mid_clear_resumes_the_purge_job_from_its_checkpoints`,
`frontend/src/lib/assistant-clear.test.ts`.

## 11. Automatic proposal correction (N17-obs295, `cn/r17-ax`, 2026-10-23)

**Why.** Live 2026-10-23 (E22, conversation `88eef039…`): a complete brief earned a first
proposal that platform validation rejected four times on one rule — a code evaluator with a
`reference_trajectory` check (bundled with a `tool_set` prohibition) scores every scenario, but
four refusal scenarios carried `expected_trajectory: []`, which counts as missing. Everything
else was correct; the member had to relay "please fix it" for a purely mechanical mistake. The
architect skill 1.7.3 states the rule where the model reads it (§5 "`[]` is not a
trajectory"); this section is the platform's deterministic backstop.

**When.** After a reply's proposal block is recorded as an `invalid` revision, the turn makes
ONE correction call iff every validation error carries a structural code
(`assistant/validation_codes.AUTOCORRECT_CODES`: `evaluation_plan.reference_missing`,
`evaluation_plan.trace_turn_missing_response`, `evaluation_plan.evaluator_partial_routing` —
the sentences of `evaluation_plan._routing_errors`), the block itself assembled (no patch /
two-block input error) and `Settings.assistant_proposal_autocorrect` (default `true`). A
shape, catalog, reference, gate or size error anywhere in the list means no correction.

**How.** In the same turn, under the same claim, heartbeat and transient-replay rules:

1. The first reply, its invalid revision N and a notice row `proposal_autocorrect_pending`
   (role `error`, its `- ` lines = the errors) are committed; `autocorrect{revision, errors[],
   validation_error_details[]}` is emitted.
2. A fresh Harness session is reserved on the ledger (`SESSION_ROLE` row
   `proposal_autocorrect`) before the call. The replay is the ordinary `compose_messages`
   over the conversation INCLUDING this turn's exchange, with the internal follow-up
   (`service.autocorrect_prompt`: the errors, the reference each evaluator reads, `[]` counts
   as missing, "patch revision N, change nothing else") on the member's side; it is never
   stored as a member row and never replayed later. The preamble's stored proposal is
   revision N with its errors, so the correction is a `launchpad-proposal-patch` against N.
3. The correction reply is stored as a second `assistant` row of the turn; its block becomes
   revision N+1 (revision N is `superseded`). The notice is renamed `proposal_autocorrected`
   when N+1 validates, else `proposal_autocorrect_failed` with the reason, and the usual
   `proposal_rejected` row (of the latest invalid revision) follows — so the next member turn
   replays the errors exactly as without the correction.
4. `proposal` (the revision the member is shown), `preparation`, `done`.

**Bounds.** One correction call per turn, never chained: a correction that fails (no change
block, still invalid, an upstream failure after the ordinary single replay — whose `retry`
carries `phase: "autocorrect"` so the client drops only the correction's stream — or
`assistant.conversation_full`) leaves the last invalid revision with its errors and the turn
ends `done`. A correction that ends without its outcome is never left `pending`
(`_settle_pending_autocorrect`, rev-r27): the notice becomes `proposal_autocorrected` when the
latest revision is valid (a complete correction block salvaged by the interrupted path), else
`proposal_autocorrect_failed` with the reason, plus a `proposal_rejected` row for the latest
invalid revision unless the turn already has one — on a cancel / client gone (the interrupted
path, reason "interrupted"), on a stale-claim takeover (settled by the successor in its claim
transaction, since the orphan's worker is fenced out — "the turn's claim expired and was taken
over"), and at startup for a dead process (`_finalize_dead_autocorrect`, "interrupted by a
backend restart", plus a terminal `error` event). The console settles only the current turn's
pending notice on a `proposal` event (rows after the last user row).

**Call budget.** A corrected turn makes up to FOUR `InvokeAgentRuntime` calls: the reply and
the correction each keep the turn's single transient replay. Each call is bounded by the
Harness execution timeout; there is no separate turn-level deadline beyond the claim TTL
(`TURN_CLAIM_TTL_S`, 30 min, which only orphan takeover reads). The internal prompt lists
every error (cap `AUTOCORRECT_PROMPT_MAX_CHARS` = 16 000 characters; the member-facing note
keeps the 2 400-character cap).

**Bindings after a structural-only first version.** `proposal.validate` computes the binding
errors only when no other error was found, so a first version whose only errors were
structural can earn a correction whose result then fails on bindings (a resource that no
longer resolves). That is still one correction call: the notice is `failed` and the binding
errors are the `proposal_rejected` row. Notice rows are notices, not failures: `check_retry` and the
console's `retryableTurn` ignore them (`NOTICE_NAMES` / `isNoticeRow`).

**Coded errors (N17-obs296).** Every proposal / evaluation-plan payload carries
`validation_error_details[]` and every notice row `errors[]` — `{code, params, message}`,
the English `message` verbatim (api.md). The console (`lib/validation-errors.ts`,
`v2/pages/assistant/ValidationErrors.tsx`) renders the known codes in the member's language
with the English sentence as the collapsed "Original message" and any other error as the
sentence itself; the notice renders 「已自动修正提案校验问题」/ "Proposal auto-corrected"
with the first version's errors collapsed.

**Tests:** `backend/tests/test_assistant_autocorrect.py` (exactly one correction and the
stored corrected revision; the three failed-correction shapes never loop; non-structural
errors and the operator switch skip it; the internal follow-up is never a member row; the
classifier against the validator's own sentences; the dead-process settlement),
`backend/tests/test_architect_prompt_policy.py::test_skill_states_that_an_empty_trajectory_is_missing`,
`frontend/src/v2/pages/assistant/ValidationErrors.test.tsx` (zh / en rendering, the live
reducer, notice ≠ failed turn).

## 12. A reply that claims a revision without a block (N17-obs468, `cn/r17-bl-obs468`, 2026-10-26)

**Live case.** Conversation `785829de…` on default: revision 1 was a draft; to 「更新提案」 the
architect answered 「修订已提交，控制台中的提案已更新为最新版」 twice, but each reply carried the
literal replay placeholder (`REPLAY_PROPOSAL_MARKER`, "[proposal block of this reply omitted from
the replay — …]") instead of a change block — and an earlier reply a model-invented "[No patch
emitted in this reply; …]" (no such text exists in the platform or the skill). Nothing was
extracted, no revision 2 was stored, nothing told anyone.

**Cause.** `compose_messages` replaced a past reply's block with that placeholder INSIDE the
model's own replayed message; the model imitated it in new replies.

**Detection** (`service.phantom_reason`, only when the reply carried no proposal block or patch
at all): `replay_marker` — the placeholder or a bracketed variant about an omitted / emitted
block; `claimed_revision` — a past-tense submission claim (「修订已提交」, 「已重新提交提案」,
"submitted the revised proposal"). A question, a future intent (「我会在你确认后更新提案」) or
「提案未修改」 is not caught.

**What happens.** In the same turn, under the same claim / heartbeat / transient-replay rules as
§11 (one generic in-turn follow-up, `correction_phase`):

1. The reply is stored as said, plus a notice row `proposal_missing_pending` (role `error`, text
   names the still-current revision); `reask{revision, reason}` is emitted.
2. ONE internal follow-up (`service.reask_prompt`, member side, never stored as a member row,
   own Harness session `proposal_reask`): the reply carried no block, the proposal is still
   revision N, never write the placeholder; emit the change as one patch against N (a complete
   block when none is stored), or say in one sentence that nothing changed.
3. A block in that reply is recorded as the next revision (`proposal_missing_recovered`,
   "stored revision M"; an invalid one also gets its `proposal_rejected` row); otherwise the
   notice becomes `proposal_missing` — 「本轮回复没有包含提案块，提案未更新（仍为 rN）。」.
   Bounded to once: a follow-up that echoes the placeholder again ends the turn `done` with
   `proposal_missing`. Placeholder text is never recorded as a proposal.
4. `LAUNCHPAD_ASSISTANT_PROPOSAL_REASK=false` keeps only the `proposal_missing` notice.

A re-ask that ends without its outcome is settled like §11 (`_settle_pending_autocorrect`):
`recovered` when a model revision beyond the named one was stored meanwhile, else `missing` —
on cancel / client gone, stale-claim takeover and at startup. Notice rows are notices
(`NOTICE_NAMES` / `isNoticeRow`); `GET` detail carries `missing{base, revision}` on them.

**Prevention.** A replayed reply no longer carries any placeholder: its block is removed
outright and the preamble's stored-proposal section says so (`REPLAY_BLOCK_NOTE`); an
imitated placeholder is scrubbed from a replayed reply and Launchpad's note
`REPLAY_NO_BLOCK_NOTE` follows the member's words of the next turn. The preamble's rule 2 adds:
only a block in this reply changes the proposal, never claim a submitted revision without one,
never write the replay placeholder; to change the proposal emit the complete block (or the
patch).

**Per-turn state.** `GET` detail `turn_revisions: {"<turn>": [revision…]}` — the model revisions
each answered turn produced (recorded between the turn's first row and the next turn's; derived
from timestamps, so older conversations get it too). The console shows under each turn's last
row 「本轮生成修订 rN」 or 「本轮未修改提案」, so a false claim is visible.

**Tests.** `backend/tests/test_assistant_phantom_proposal.py` (the live shape → notice + one
corrective call that recovers r2; bounded to once; variants and bare claims; ordinary replies
left alone; a real block is not re-asked; switched off; the replay never carries the
placeholder; the preamble rule; `turn_revisions`; cancel / dead-process settlement), vitest
`lib/assistant-missing.test.ts`, `v2/pages/assistant/Discussion.missing.test.tsx`.
