# Implementation Plan — Memory Console Page

Ordered so the backend contract is provable before any UI is written, and so
`make verify` can run at each gate.

## Step 1 — Backend service layer

- [ ] Create `backend/app/services/memory_console.py`:
  - `memory_overview()` — `GetMemory` + `ListMemories` + one `ListActors` page;
    normalize keys to snake_case, ISO-format timestamps, return `configured` flag.
  - `list_actors(next_token)` — `ListActors` passthrough + `decode_actor()`.
  - `decode_actor(actor_id)` — split on first `SCOPE_SEP` from `memory.py`;
    return `{actor_id, agent_id, human_actor, scoped}`.
  - `list_sessions(actor_id, next_token)`, `list_events(actor_id, session_id,
    include_payloads, next_token)` with payload normalization
    (`conversational` → role/text, `blob` → size only).
  - `resolve_namespaces(actor_id, strategies)` — `{actorId}` substitution,
    `resolvable` flag for leftover placeholders.
  - `list_records(namespace, strategy_id, max_results, next_token)`,
    `search_records(namespace, query, top_k, strategy_id)`.
  - `list_extraction_jobs(filters, next_token)` — drop empty filter keys,
    defensively flatten `messages.messagesList`.
- [ ] Reuse `data_client()` / `control_client()` only; no new boto3 construction.
- [ ] Ledger joins live in the router (they need a `Session`), not the service —
  service stays AWS-only and stub-friendly.

Validation: `cd backend && uv run ruff check .`

## Step 2 — Backend router

- [ ] Create `backend/app/routers/memory.py` with prefix `/api/memory`,
  `tags=["memory"]`, the 8 endpoints from `design.md` §2, pydantic
  `SearchRequest` model (`query` 1..2000, `top_k` 1..100).
- [ ] `_guard()` wrapper → `memory.not_configured` (409) /
  `memory.namespace_required` (400) / `memory.unavailable` (502).
- [ ] Agent-name and ChatSession/ChatMessage joins via `Depends(get_db)` with a
  single batched query per page (no N+1).
- [ ] Wire `include_router(memory_router)` in `backend/app/main.py` (import in the
  alphabetical block with the other `app.routers.*` imports).
- [ ] Confirm read-only:
  `grep -nE "create_event|delete_event|delete_memory_record|batch_(create|update|delete)_memory_records|start_memory_extraction_job|create_memory|update_memory|delete_memory" backend/app/routers/memory.py backend/app/services/memory_console.py`
  must return **nothing**.

Validation: `cd backend && uv run ruff check . && uv run pytest -q`

## Step 3 — Backend tests

- [ ] `backend/tests/test_memory_console.py`, hermetic (stub clients injected by
  monkeypatching `memory_console.data_client` / `control_client`):
  - overview shape + strategy normalization + `is_platform` marking.
  - `configured: false` when `memory_id` is absent; other endpoints → 409 envelope.
  - actor decoding: scoped hit (agent name resolved), scoped miss (deleted agent →
    `agent_name: null`), unscoped actor (`scoped: false`).
  - sessions ledger join present/absent + message count.
  - events payload normalization: conversational role/text kept in full, blob
    reduced to a size, unknown payload kind ignored without raising.
  - namespace resolution: `{actorId}` substituted; unknown placeholder →
    `resolvable: false`; `/records` without a derivable namespace → 400.
  - search passes `searchCriteria{searchQuery, topK, memoryStrategyId}` through and
    surfaces `score`.
  - extraction-job filters: empty values omitted from the AWS `filter` dict.
  - `next_token` round-trips in and out on every list endpoint.
  - botocore `ClientError` → 502 `memory.unavailable`.

Validation: `cd backend && uv run pytest tests/test_memory_console.py -q`

## Step 4 — Frontend API client

- [ ] `frontend/src/lib/api.ts`: add `MemoryOverview`, `MemoryStrategy`,
  `MemoryActor`, `MemorySession`, `MemoryEvent`, `MemoryEventPayload`,
  `MemoryNamespace`, `MemoryRecord`, `MemoryExtractionJob`, and list-envelope
  types; add `api.memoryOverview/​memoryActors/​memorySessions/​memoryEvents/​
  memoryNamespaces/​memoryRecords/​memorySearchRecords/​memoryExtractionJobs`.
- [ ] Keep interfaces field-for-field aligned with the FastAPI responses.

Validation: `cd frontend && npx tsc --noEmit`

## Step 5 — Page shell + nav + i18n

- [ ] `frontend/src/pages/Memory.tsx` — `?view=` tab shell, `/overview` fetch with
  `seq` guard, `configured === false` panel, tab bar via `Btn`.
- [ ] `frontend/src/pages/memory/format.ts` — timestamp + byte + text-clamp helpers.
- [ ] `frontend/src/App.tsx` — `<Route path="memory" element={<Memory />} />`.
- [ ] `frontend/src/layout/nav.ts` — insert `{ idx: "05", to: "/memory",
  labelKey: "nav.memory" }` after knowledge-bases, renumber `06..09`,
  `PLATFORM_COUNT` 5 → 6.
- [ ] `frontend/src/layout/Sidebar.tsx` — Phase-02 placeholder indices `09/10` → `10/11`.
- [ ] Both locale files: `nav.memory` + the `memoryPage.*` group.

Validation: `python3 scripts/i18n_check.py && cd frontend && npm run lint && npx tsc --noEmit`

## Step 6 — Tab components

- [ ] `OverviewTab.tsx` — resource `Panel` (id/arn/status/expiry/role/KMS/timestamps),
  `StatTile` row (actors, strategies, expiry days), strategy cards with
  namespaces + templates, `other_memories` table with the platform marker.
- [ ] `ShortTermTab.tsx` — actor list → session list → event timeline, `?actor=` /
  `?session=` URL state, "Load more" per pane, expandable payload text, blob chips.
- [ ] `LongTermTab.tsx` — namespace picker from `/namespaces` (disabled when
  `resolvable: false`), record `DataTable`, search box (query + `top_k` + strategy)
  with score column and a visually distinct results mode, record detail panel,
  async-extraction empty state.
- [ ] `ExtractionTab.tsx` — job table, status `Chip` tones, actor/session/strategy/
  status filters wired to the query params of the API.

Validation: `cd frontend && npm run lint && npx tsc --noEmit && npm run build`

## Step 7 — Docs

- [ ] `docs/architecture.md` — add the Memory console entry: which AgentCore
  operations back which view, the actor-scoping decode, and the read-only stance.
- [ ] `docs/api.md` — document the 8 `/api/memory` endpoints.
- [ ] Keep bilingual counterparts in sync if the touched docs have `*.zh-CN` twins.

## Step 8 — Full gate + review

- [ ] `make verify` (backend ruff+pytest, infra ruff+pytest, frontend
  eslint+tsc+build, i18n parity) — must be green.
- [ ] Re-run the read-only grep from Step 2 across the whole diff.
- [ ] Dispatch `trellis-check` for a full-scope review against `prd.md`
      acceptance criteria.
- [ ] Live smoke (needs `make bootstrap` + AWS creds; report explicitly if the
      environment cannot provide them): `make dev`, open `/memory`, confirm the
      real singleton renders, drill one Chat-written session down to its events,
      run one semantic search, and check the extraction-job list.

## Review gates

- After Step 3: backend contract is proven by tests before any UI exists.
- After Step 5: nav/i18n/typecheck green before tab bodies are written.
- After Step 8: full `make verify` + acceptance-criteria walkthrough.

## Rollback points

- Steps 1–3 are backend-only and additive: revert the two new files + the
  `main.py` line.
- Steps 4–6 are frontend-only: revert the page dir, the `api.ts` block, and the
  nav/i18n edits.
- No migration, no AWS mutation, no config write — a single `git revert` of the
  task commit fully restores the prior state.
