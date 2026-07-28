# Implementation plan

## Step 1 — memory wrapper (`backend/app/services/memory.py`)

- [ ] Add `list_actor_ids(prefix: str | None = None, max_pages: int = 5) -> list[str]`:
      page `data_client().list_actors(memoryId=…, maxResults=100)` following `nextToken`,
      collect `actorSummaries[].actorId`, filter by `prefix` client-side (no server filter),
      stop at `max_pages`.

Validation: `cd backend && uv run pytest tests/test_memory_console.py tests/test_memory_scoping.py -q`

## Step 2 — agent resolver (`backend/app/services/observability.py`)

- [ ] Extract `build_agent_resolver(db) -> Callable[[str | None], Agent | None]` from
      `build_agent_mapper`, keeping the match rules and precedence exactly as-is.
- [ ] Reimplement `build_agent_mapper` on top of it; `"unknown"` for empty service and raw
      service string when nothing matches must be preserved.
- [ ] Add `_agent_from_traces(resolver, traces) -> Agent | None` (first row with a service).

Validation: existing mapper/session/trace tests must pass unchanged —
`cd backend && uv run pytest tests/test_observability.py -q`

## Step 3 — transcript helpers + external probe

- [ ] Extract `_turns_from_events(events) -> list[dict]` (sort by `eventTimestamp`,
      `_turn_text`, drop tool-only turns) and use it in the existing chat/eval path.
- [ ] Add `_experiment_for_session(db, session_id) -> Experiment | None` scanning
      `Experiment.artifacts["traffic"]["session_ids"]` newest-first, tolerant of bad JSON.
- [ ] Add `_external_transcript(db, session_id, agent)`:
      candidates = `memory.list_actor_ids(prefix=f"{agent.id}__")` (when agent known) then
      `"default"`; first actor with turns wins; wrap the whole probe in `try/except` →
      `{"available": False, "reason": "not_platform_session"}`; label `source` from
      `_experiment_for_session`; `long_term_records: None`.
- [ ] `session_transcript(db, session_id, agent=None)` delegates to it when both ledger
      lookups miss.

## Step 4 — wire `get_session`

- [ ] Compute `payload` first, resolve the agent hint from `payload["traces"]`, then call
      `session_transcript(db, session_id, agent=hint)`; keep the transcript outside
      `_cached` and keep the `{**payload, "transcript": …}` return.

Validation: `cd backend && uv run ruff check . && uv run pytest tests/test_observability.py -q`

## Step 5 — backend tests (`backend/tests/test_observability.py`)

- [ ] default-actor hit: no ledger row, no eval run, stubbed `list_actor_ids` + `list_events`
      returning events only for `"default"` → `available`, `source == "external"`,
      `actor_id == "default"`, ordered turns, `long_term_records is None`.
- [ ] scoped-actor hit: events only under `f"{agent_id}__api"` → that `actor_id`,
      `agent_id`/`agent_name` populated, and `"default"` never probed after the hit.
- [ ] experiment labeling: seed an `Experiment` whose `artifacts.traffic.session_ids`
      contains the id → `source == "experiment"` + `experiment_id`.
- [ ] no-hit: every candidate empty → exactly
      `{"available": False, "reason": "not_platform_session"}`.
- [ ] probe error: `list_actor_ids` (and separately `list_events`) raising → same
      unavailable dict, no exception.
- [ ] keep `test_transcript_no_ledger_row_is_unavailable` green (stub the probe to empty).

## Step 6 — frontend

- [ ] `frontend/src/lib/api.ts`: widen the `source` union; add optional `experiment_id`,
      `experiment_name`.
- [ ] `SessionDetailView.tsx`: origin sub-line per source; OPEN IN CHAT gated on
      `source === "chat" && agent_id != null`.
- [ ] `locales/en/common.json` + `locales/zh-CN/common.json`: reword
      `obs.session.noTranscript`; add `conversationExternalSub`, `conversationExperimentSub`.

Validation: `cd frontend && npm run lint && npx tsc --noEmit` and
`python3 scripts/i18n_check.py`

## Step 7 — full gate + live check

- [ ] `make verify`
- [ ] Live: backend running against the dev AWS account, open
      `/observability?tab=sessions&session=ee760f57-2757-4761-947b-83f1ec6fa022` and confirm
      the 5-turn conversation renders with an EXTERNAL/EXPERIMENT sub-line and no OPEN IN
      CHAT button; then open a normal chat session and confirm nothing regressed (turns +
      button + long-term note still there).
- [ ] Screenshot both states for the task record.

## Review gates

- After Step 4: re-read `session_transcript` end-to-end and confirm the chat/eval branches
  are byte-for-byte behaviorally unchanged.
- After Step 6: confirm no user-facing string bypassed i18n.

## Rollback points

- Steps 1-5 are backend-only and independently revertable; the frontend union widening
  (Step 6) depends on Step 3's payload, so revert Step 6 first if the API shape is reverted.
