# Design — Memory Console Page

## 1. Boundaries

```
frontend/src/pages/Memory.tsx            tab shell (?view=), shared load/error state
frontend/src/pages/memory/
  OverviewTab.tsx                        resource card + strategy cards + stat tiles
  ShortTermTab.tsx                       actor → session → event, 3-pane drill-down
  LongTermTab.tsx                        namespace picker + record table + semantic search
  ExtractionTab.tsx                      extraction-job table + filters
  format.ts                              timestamp/duration/payload display helpers
frontend/src/lib/api.ts                  + Memory* interfaces and api.memory* methods
frontend/src/layout/nav.ts               + nav entry, PLATFORM_COUNT 5 → 6
frontend/src/layout/Sidebar.tsx          Phase-02 indices 09/10 → 10/11
frontend/src/App.tsx                     + <Route path="memory" />
frontend/src/locales/{en,zh-CN}/common.json  + nav.memory + memoryPage.* (parity)

backend/app/services/memory_console.py   NEW — read-only console projections
backend/app/services/memory.py           unchanged public API; console module reuses
                                         scoped_actor / SCOPE_SEP and _memory_id
backend/app/routers/memory.py            NEW — /api/memory router
backend/app/main.py                      + include_router(memory_router)
backend/tests/test_memory_console.py     NEW — hermetic router tests with stub clients
docs/architecture.md, docs/api.md        + Memory console section
```

**Why a separate `memory_console.py` instead of growing `memory.py`:**
`memory.py` is on the chat hot path (`create_turn_event`, `session_memory_summary`)
and is deliberately tiny. The console needs control-plane reads, actor decoding,
ledger joins, and pagination plumbing — none of which belong on the invoke path.
The console module imports the primitives it shares (`SCOPE_SEP`, `_memory_id`)
from `memory.py` so the scoping contract stays single-sourced.

**Client construction:** `memory_console.py` calls `data_client()` and
`control_client()` from `app/services/agentcore/client.py` only — no
`boto3.client(...)` anywhere else, per `CLAUDE.md`.

## 2. API contract (`/api/memory`)

All responses are JSON objects. `next_token` is `string | null` on every list.
Timestamps are ISO-8601 strings (server converts botocore `datetime`).

### `GET /overview`
```json
{
  "configured": true,
  "memory": {
    "id": "launchpad_memory-XXXX", "arn": "arn:aws:...", "name": "launchpad_memory",
    "description": "...", "status": "ACTIVE", "failure_reason": null,
    "event_expiry_days": 30, "encryption_key_arn": null,
    "execution_role_arn": "arn:aws:iam::...", "created_at": "...", "updated_at": "..."
  },
  "strategies": [
    { "strategy_id": "...", "name": "semantic_facts", "type": "SEMANTIC",
      "status": "ACTIVE", "namespaces": ["/facts/{actorId}"],
      "namespace_templates": ["/facts/{actorId}"] }
  ],
  "actor_count": 7, "actor_count_truncated": false,
  "other_memories": [{ "id": "...", "arn": "...", "status": "ACTIVE", "is_platform": false }]
}
```
`actor_count` counts one `ListActors` page (max 100); `actor_count_truncated` is
`true` when a `nextToken` remains — an explicit "there are more" signal rather
than a silently wrong number.

When `settings.resources["memory_id"]` is absent the endpoint returns
`200 {"configured": false, "memory": null, "strategies": [], "actor_count": 0,
"other_memories": []}` so the page can render a "run `make bootstrap`" state.
Every **other** endpoint raises the not-configured `AppError` instead — only the
landing view needs the soft state.

### `GET /actors?next_token=`
```json
{ "items": [{ "actor_id": "<agent32hex>__river", "agent_id": "<agent32hex>",
              "agent_name": "front-desk", "human_actor": "river",
              "scoped": true }], "next_token": null }
```
Decoding rule: split on the **first** `SCOPE_SEP` (`__`). If the left part
resolves to an `Agent` row → `scoped: true` with `agent_name`. If it does not
resolve (deleted agent) → `scoped: true`, `agent_name: null`. If there is no
separator → `scoped: false`, `agent_id: null`, `human_actor` = whole actor id.
Agent names are resolved with **one** `IN`-clause ledger query per page, not N+1.

### `GET /sessions?actor_id=&next_token=`
```json
{ "items": [{ "session_id": "...", "actor_id": "...", "created_at": "...",
              "ledger": { "agent_id": "...", "agent_name": "...",
                          "human_actor": "river", "message_count": 12 } }],
  "next_token": null }
```
`ledger` is `null` when no `ChatSession` row matches (e.g. an eval- or
API-originated session). Message counts come from one grouped
`ChatMessage` count query over the page's session ids.

### `GET /events?actor_id=&session_id=&include_payloads=true&next_token=`
```json
{ "items": [{ "event_id": "...", "at": "...", "branch": null,
              "metadata": {},
              "payload": [{ "kind": "conversational", "role": "USER", "text": "..." },
                          { "kind": "blob", "role": null, "text": null,
                            "blob_bytes": 512 }] }],
  "next_token": null }
```
Payload text is **not truncated** server-side (the UI clamps with CSS + an
expand toggle). Blob payloads report only a size so binary never reaches the
browser.

### `GET /namespaces?actor_id=`
```json
{ "items": [{ "strategy_id": "...", "strategy_name": "semantic_facts",
              "template": "/facts/{actorId}", "namespace": "/facts/<actor>" }] }
```
Substitution supports `{actorId}` and `{sessionId}`-free templates; any
unresolved `{...}` placeholder marks the entry `resolvable: false` so the UI
disables it rather than sending a broken namespace to AWS.

### `GET /records?actor_id=&strategy_id=&namespace=&max_results=&next_token=`
Resolution order: explicit `namespace` wins; otherwise `(actor_id, strategy_id)`
resolves via `/namespaces` logic; if neither is usable → `400 memory.namespace_required`.
```json
{ "namespace": "/facts/<actor>",
  "items": [{ "record_id": "...", "text": "...", "strategy_id": "...",
              "namespaces": ["..."], "created_at": "...", "score": null,
              "metadata": {} }],
  "next_token": null }
```

### `POST /records/search`
Body: `{ "query": "...", "actor_id": "...", "strategy_id": null,
"namespace": null, "top_k": 5 }` → same item shape as `/records` with `score`
populated, plus `"namespace"` echoed. `query` is `min_length=1, max_length=2000`;
`top_k` is `ge=1, le=100`.

### `GET /extraction-jobs?actor_id=&session_id=&strategy_id=&status=&next_token=`
```json
{ "items": [{ "job_id": "...", "status": "SUCCEEDED", "failure_reason": null,
              "strategy_id": "...", "actor_id": "...", "session_id": "...",
              "messages": ["..."] }], "next_token": null }
```
Filter keys are only sent to AWS when non-empty (the preview API rejects empty
strings inside `filter`). `messages` flattens `messages.messagesList` defensively —
the shape is preview-volatile, so a non-list value degrades to `[]`.

## 3. Error model

| Condition | Behaviour |
|---|---|
| `memory_id` missing, `/overview` | `200 {"configured": false, ...}` |
| `memory_id` missing, any other endpoint | `AppError("memory.not_configured", 409)` |
| namespace not derivable | `AppError("memory.namespace_required", 400)` |
| any botocore/`ClientError` | `AppError("memory.unavailable", 502)`, message includes the AWS text |

A single `_guard()` helper in the router wraps every service call so the mapping
lives in one place, mirroring the `try/except → AppError` shape already in
`routers/chat.py:199`. `AppError` already renders through
`app/core/errors.register_error_handlers`, so no new handler is needed.

## 4. Data flow

```
Overview      GetMemory(memory_id) + ListMemories() + ListActors(page 1)
Short-term    ListActors → [ledger Agent name join] → ListSessions(actor)
              → [ledger ChatSession/ChatMessage join] → ListEvents(actor, session)
Long-term     GetMemory.strategies[].namespaceTemplates × actor → namespace
              → ListMemoryRecords(namespace)  |  RetrieveMemoryRecords(namespace, query)
Extraction    ListMemoryExtractionJobs(filter)
```

Strategy metadata is needed by both Overview and Long-term. Rather than a cache
layer, `GetMemory` is called per request — it is a single fast control-plane read
and the page is low-traffic. (Observability's TTL cache exists because CloudWatch
Logs Insights queries cost seconds; that pressure does not apply here.) The
frontend keeps the overview response in component state and passes
`strategies` down to `LongTermTab` as props, so switching tabs does not refetch.

## 5. Frontend structure

`Memory.tsx` owns:
- `view` from `?view=`, validated against `["overview","short-term","long-term","extraction"]`,
  defaulting to `overview` (invalid value → overview, no crash).
- The `/overview` fetch (needed by every tab for strategy metadata + the
  `configured` flag), with a `seq` ref guard against out-of-order responses —
  the same pattern as `Observability.tsx:44`.
- `configured === false` → render a single "not bootstrapped" panel and skip the
  tabs entirely.

Each tab owns its own list fetches and its own `next_token` state, exposing a
"Load more" button that appends. Selection (`?actor=`, `?session=`, `?strategy=`)
is written through `setSearchParams` so reload/deep-link works; selecting an
actor clears `?session=` to avoid an actor/session mismatch.

Tables use the existing `DataTable` + `Column` API; statuses use `Chip` with
tone mapping (`ACTIVE`/`SUCCEEDED` → ok, `FAILED` → crit, otherwise warn).
Timestamps format through `pages/memory/format.ts` (a small local helper —
`observability/format.ts` is trace-shaped and not reusable here).

## 6. i18n

New key group `memoryPage.*` in both locale files, mirroring the existing
`obs.*`/`chatPage.*` grouping, plus `nav.memory`. `scripts/i18n_check.py`
enforces exact key-set parity, so both files are edited in the same step.

## 7. Tradeoffs

- **Read-only by decision.** The router simply has no mutating handler, which is
  a stronger guarantee than a UI-level guard and is directly greppable.
- **Server-side namespace resolution** duplicates nothing in TS and keeps the
  `{actorId}` template contract next to `scoped_actor`, at the cost of one extra
  endpoint (`/namespaces`).
- **No TTL cache.** Simpler, always-fresh; revisit only if `GetMemory` latency
  shows up.
- **Page-1 actor count** instead of full pagination for the stat tile: bounded
  cost, with `actor_count_truncated` making the bound visible instead of lying.
- **One shared memory resource assumed.** `other_memories` is display-only; the
  page reads the singleton. Multi-memory management stays out of scope.

## 8. Compatibility / rollback

- Purely additive: one new router, one new service module, one new page, plus a
  nav entry and i18n keys. No existing endpoint, model, or migration changes.
- No DB schema change — the ledger is read via existing `Agent`, `ChatSession`,
  `ChatMessage` models.
- Rollback = revert the commit; nothing persists outside it (no AWS mutation, no
  config write, no migration).
- Sidebar renumbering (`05` Memory, `06` Chat … `09` Governance, `10/11`
  Phase-02) is cosmetic; deep links to existing routes are unaffected because
  numbering is display-only and paths are unchanged.
