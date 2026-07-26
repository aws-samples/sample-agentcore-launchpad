# Memory Console Page

## Goal

Give the console a first-class **Memory** page that manages and visualizes the
AgentCore Memory resource behind the platform: its configuration and long-term
strategies, the short-term event store (actor → session → event), the long-term
record store (namespace listing + semantic retrieval), and the asynchronous
extraction pipeline that turns the former into the latter.

Today the only memory surface is the read-only right rail in the Chat playground
(`GET /api/chat/{agent_id}/memory`), which shows event count + a truncated
`/facts` and `/preferences` record list for **one** session. There is no way to
see the memory resource itself, its strategies, which actors/sessions exist, or
whether extraction actually ran. This page closes that gap.

## Scope decision (confirmed with the user, 2026-07-26)

**Read-only visualization + semantic retrieval.** The page issues no mutating
AgentCore call: no `CreateEvent`, `DeleteEvent`, `DeleteMemoryRecord`,
`Batch*MemoryRecords`, `StartMemoryExtractionJob`, `CreateMemory`,
`UpdateMemory`, or `DeleteMemory`. Write/delete/strategy-editing and multi-memory
provisioning are explicitly out of scope for this task.

## Background — verified AgentCore facts

Verified against the docs and against the **pinned botocore in this repo**
(`cd backend && uv run python` introspection of the service model), so the shapes
below are authoritative for `bedrock-agentcore 1.17.*`:

| Plane | Client | Operations available |
|---|---|---|
| Control | `bedrock-agentcore-control` | `ListMemories` `GetMemory` `CreateMemory` `UpdateMemory` `DeleteMemory` |
| Data — short-term | `bedrock-agentcore` | `ListActors` `ListSessions` `ListEvents` `GetEvent` `CreateEvent` `DeleteEvent` |
| Data — long-term | `bedrock-agentcore` | `ListMemoryRecords` `RetrieveMemoryRecords` `GetMemoryRecord` `DeleteMemoryRecord` `BatchCreate/Update/DeleteMemoryRecords` |
| Data — extraction | `bedrock-agentcore` | `ListMemoryExtractionJobs` `StartMemoryExtractionJob` |

Response shapes this task depends on:

- `ListActors` → `actorSummaries[].actorId`
- `ListSessions(memoryId, actorId)` → `sessionSummaries[].{sessionId, actorId, createdAt}`
- `ListEvents(memoryId, actorId, sessionId, includePayloads)` →
  `events[].{eventId, eventTimestamp, payload[], branch, metadata}`
- `ListMemoryRecords(memoryId, namespace|namespacePath, memoryStrategyId?)` →
  `memoryRecordSummaries[].{memoryRecordId, content.text, memoryStrategyId, namespaces[], createdAt, score, metadata}`
- `RetrieveMemoryRecords(memoryId, namespace|namespacePath, searchCriteria{searchQuery, memoryStrategyId?, topK})`
  → same summary shape, `score` populated
- `ListMemoryExtractionJobs(memoryId, filter{strategyId, sessionId, actorId, status})`
  → `jobs[].{jobID, status, failureReason, strategyId, sessionId, actorId, messages}`
- `GetMemory(memoryId)` → `memory.{arn, id, name, description, status, failureReason,
  eventExpiryDuration, encryptionKeyArn, memoryExecutionRoleArn, createdAt, updatedAt,
  strategies[].{strategyId, name, type, namespaces, namespaceTemplates, status}}`

All list operations are `nextToken`-paginated. `ListMemoryRecords` /
`RetrieveMemoryRecords` **require** either `namespace` or `namespacePath`.

Platform-side facts that shape the UI:

- One shared singleton memory (`launchpad_memory`, id in
  `settings.resources["memory_id"]`) created by `app/services/bootstrap.py:67`
  with two strategies: `semantic_facts → /facts/{actorId}` and
  `user_preferences → /preferences/{actorId}`, plus an event expiry.
- AgentCore namespaces key only on `{actorId}` — no `{agentId}` template var — so
  the platform folds the agent into the actor: `scoped_actor(agent_id, human)` →
  `<agent_id>__<human>` (`app/services/memory.py:20`). Raw actor ids in the AWS
  responses are therefore compound and must be decoded for display.
- Long-term extraction is **asynchronous**, so a session can have events but no
  records yet — the page must make that state legible rather than look empty.

## Requirements

### R1 — Navigation and page shell
- New route `/memory` registered in `src/App.tsx`, new sidebar entry
  `nav.memory` placed in the **PLATFORM** group (after Knowledge Bases), with
  `PLATFORM_COUNT` and the hardcoded Phase-02 indices in `Sidebar.tsx` updated so
  numbering stays contiguous.
- Sub-surfaces are `?view=` query params (project convention, not nested routes):
  `?view=overview|short-term|long-term|extraction`, defaulting to `overview`.
- Selection state is URL-addressable (`?actor=`, `?session=`, `?strategy=`) so a
  view is shareable/reloadable, matching Observability's `?trace=`/`?session=`.

### R2 — Overview view (memory resource + strategies)
- Show the resource: id, arn, name, description, status (+ `failureReason` when
  `FAILED`), event expiry in days, encryption key, execution role, created/updated.
- Show every long-term strategy as a card: name, `strategyId`, type, status, and
  both `namespaces` and `namespaceTemplates`.
- Show stat tiles: actor count, strategy count, event-expiry days.
- List the other memory resources in the account (`ListMemories`) and mark which
  one is the platform singleton, so a reader can tell "this console manages one
  memory resource" without leaving the page.

### R3 — Short-term view (actor → session → event)
- Actor list from `ListActors`, each row decoded into `agent_id` + resolved agent
  **name** (ledger lookup) + human actor, with the raw compound id still available.
- Selecting an actor lists its sessions (`ListSessions`) with `createdAt`, and
  cross-references the ledger `ChatSession` rows where present.
- Selecting a session renders its events as a chronological timeline:
  timestamp, `eventId`, and per-payload entries — conversational payloads show
  role + text, blob payloads are labelled as blob (never dumped raw).
- Long text is expandable rather than silently truncated at 120 chars like the
  Chat rail.

### R4 — Long-term view (records + semantic retrieval)
- Namespace picker derived from strategy `namespaceTemplates` × selected actor
  (`{actorId}` substitution done **server-side**, so the template contract is not
  duplicated in TypeScript).
- Record table for the resolved namespace via `ListMemoryRecords`: record id,
  strategy, namespaces, created at, content text.
- Semantic search box → `RetrieveMemoryRecords` with `searchQuery`, `topK`, and
  optional strategy filter; results show the relevance `score` and are visually
  distinguished from a plain listing.
- Record detail (content + metadata) viewable without leaving the view.
- Empty state explains asynchronous extraction (events exist, records pending)
  rather than implying "no memory".

### R5 — Extraction view (short-term → long-term pipeline)
- `ListMemoryExtractionJobs` table: job id, status chip, strategy, actor, session,
  `failureReason` when present.
- Filters for actor / session / strategy / status, passed through to the API.

### R6 — Backend API
- New router `app/routers/memory.py` under `/api/memory`, wired in `app/main.py`.
- Endpoints (all `GET`, except the search POST which carries a query body):
  `/overview`, `/actors`, `/sessions`, `/events`, `/namespaces`, `/records`,
  `POST /records/search`, `/extraction-jobs`.
- `nextToken` exposed as `next_token` in and out on every list endpoint — no
  silent result caps.
- No `boto3.client(...)` outside `app/services/agentcore/client.py`; memory calls
  go through the service layer so tests can inject stubs.
- Missing `memory_id` → a clean error envelope, not a 500 traceback; botocore
  failures surface as `memory.unavailable` (502) like `routers/chat.py` does.

### R7 — i18n and quality gate
- All new user-facing strings are i18n keys with **en + zh-CN parity**
  (`scripts/i18n_check.py`).
- `make verify` passes (backend ruff+pytest, infra ruff+pytest, frontend
  eslint+tsc+build, i18n parity).
- New hermetic backend tests cover the router with stubbed AWS clients.

## Constraints

- Read-only: no mutating AgentCore API call may be reachable from this page.
- No new AWS resource is created; the page reads the existing bootstrap singleton.
- Preview-SDK drift stays inside the service/wrapper layer per `CLAUDE.md`.
- Tests must stay hermetic — `backend/tests/` may not touch real AWS.
- Reuse existing components (`ViewHead`, `Panel`, `DataTable`, `Chip`,
  `StatTile`, `Btn`, `useToast`); no new UI framework or chart dependency.
- Do not change `scoped_actor` semantics or the Chat rail contract.

## Acceptance Criteria

- [ ] `/memory` is reachable from the sidebar; the four `?view=` sub-surfaces
      render and survive a page reload with `?actor=`/`?session=`/`?strategy=`.
- [ ] Overview shows the real singleton resource (id/arn/status/expiry) and both
      bootstrap strategies with their namespace templates.
- [ ] Short-term view drills actor → session → event and shows conversational
      role + text for a session written by the Chat playground.
- [ ] Actor rows display the decoded agent name + human actor, not the raw
      `<agent_id>__<human>` string alone.
- [ ] Long-term view lists records for a resolved namespace and semantic search
      returns scored results for a natural-language query.
- [ ] Extraction view lists jobs with status and honours the actor/session/
      strategy/status filters.
- [ ] Every list endpoint round-trips `next_token`; nothing is capped silently.
- [ ] With `memory_id` absent from config, the page shows a clear
      "not configured" state and the API returns an error envelope (no 500).
- [ ] `grep` confirms no mutating memory operation (`create_event`,
      `delete_event`, `delete_memory_record`, `batch_*_memory_records`,
      `start_memory_extraction_job`, `create_memory`, `update_memory`,
      `delete_memory`) is reachable from the new router.
- [ ] `make verify` is green, including en ↔ zh-CN key parity.
- [ ] `docs/architecture.md` gains the Memory-console row/section.

## Out of scope

- Strategy CRUD, `eventExpiryDuration` edits, creating/deleting memory resources.
- Deleting events or records; manually triggering extraction jobs.
- Multi-memory-resource management or per-agent dedicated memory resources.
- Changing how the runtime writes memory, or the Chat right-rail panel.
- Payments/Settings nav placeholders.
