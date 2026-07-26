# Memory Console — Read-Only Short-/Long-Term Memory Views

## Scenario: `/api/memory` console projections over the shared memory resource

### 1. Scope / Trigger

Use this contract when changing the Memory console (`/memory`, `?view=`
sub-surfaces), `app/services/memory_console.py`, `app/routers/memory.py`, or any
console-side read of AgentCore Memory. It is a cross-layer request/response
contract over a **preview** AWS API whose declared service model understates two
real bounds (§4), so the code-spec depth is mandatory.

Related: [Claude SDK AgentCore Memory](./claude-sdk-agentcore-memory.md) owns the
**write** path and the actor-scoping rule this console must read back unchanged.

### 2. Signatures

```python
# app/services/memory.py  — shared with the invoke hot path
memory_id_or_none() -> str | None          # None before bootstrap; never raises
SCOPE_SEP = "__"
scoped_actor(agent_id: str, base_actor: str = "river") -> str

# app/services/memory_console.py  — console-only, AWS reads only
PAGE_MAX = 100
EXTRACTION_PAGE_MAX = 50
EXTRACTION_STATUS_FILTERS = ("FAILED",)

require_memory_id() -> str                             # raises memory.not_configured (409)
memory_overview() -> dict                              # GetMemory + ListMemories + ListActors
get_strategies() -> list[dict]
decode_actor(actor_id: str) -> dict
list_actors(next_token=None, max_results=None) -> dict
list_sessions(actor_id, next_token=None, max_results=None) -> dict
list_events(actor_id, session_id, include_payloads=True, next_token=None, max_results=None) -> dict
resolve_namespaces(actor_id, strategies=None) -> list[dict]
list_records(namespace, strategy_id=None, next_token=None, max_results=None) -> dict
search_records(namespace, query, top_k=5, strategy_id=None) -> dict
list_extraction_jobs(actor_id=None, session_id=None, strategy_id=None,
                     status=None, next_token=None, max_results=None) -> dict
```

Router surface (all `GET` except the retrieval `POST`):

```
GET  /api/memory/overview
GET  /api/memory/actors            ?next_token&max_results
GET  /api/memory/sessions          ?actor_id*&next_token&max_results
GET  /api/memory/events            ?actor_id*&session_id*&include_payloads&next_token&max_results
GET  /api/memory/namespaces        ?actor_id*
GET  /api/memory/records           ?actor_id|namespace&strategy_id&next_token&max_results
POST /api/memory/records/search    {query*, actor_id, strategy_id, namespace, top_k}
GET  /api/memory/extraction-jobs   ?actor_id&session_id&strategy_id&status&next_token&max_results
```

### 3. Contracts

**Read-only is structural.** Neither `memory_console.py` nor `routers/memory.py`
may contain `create_event`, `delete_event`, `delete_memory_record`,
`batch_*_memory_records`, `start_memory_extraction_job`, `create_memory`,
`update_memory`, or `delete_memory`. A UI-level guard is not equivalent: the
guarantee is that the code path does not exist, and it is greppable and tested.

**Clients.** Only `control_client()` / `data_client()` from
`app/services/agentcore/client.py`. Tests inject stubs by monkeypatching
`memory_console.data_client` / `memory_console.control_client`; settings are
stubbed at `app.services.memory.get_settings`.

**Layering.** The service is AWS-only so it stays stub-friendly; ledger joins
(`Agent`, `ChatSession`, `ChatMessage`) live in the router where a `Session` is
available, and must be **one batched query per page** — never N+1 per row.

**Actor decode** (`decode_actor`) — `partition(SCOPE_SEP)` on the **first**
separator only, because a human actor id may itself contain `__`
(`agentX__runtime__diagnostic` → agent `agentX`, human `runtime__diagnostic`):

| Input | `agent_id` | `human_actor` | `scoped` | `agent_name` (router) |
|---|---|---|---|---|
| `<agent>__river`, agent row exists | `<agent>` | `river` | `True` | agent name |
| `<agent>__river`, agent deleted | `<agent>` | `river` | `True` | `None` |
| `river` (no separator) | `None` | `river` | `False` | `None` |

A scoped actor whose agent row is gone stays `scoped: True` — the memory
partition outlives the agent it belonged to.

**Namespace resolution** (`resolve_namespaces`) — `ListMemoryRecords` and
`RetrieveMemoryRecords` both require a concrete `namespace`/`namespacePath`.
Substitution of `{actorId}` into each strategy `namespaceTemplates` entry happens
**server-side** so the template contract lives next to `scoped_actor` instead of
being re-derived in TypeScript. Any remaining `{...}` placeholder (e.g.
`{sessionId}`) sets `resolvable: False`; the UI disables those instead of sending
a broken namespace. Resolution order on `/records` and `/records/search`:
explicit `namespace` → `(actor_id[, strategy_id])` → error.

**Event payload normalization** (`_payload_entry` + `_decode_turn`):

- `conversational` → `{kind, role, text, parts, blob_bytes: None}`.
  Harness agents persist a **whole message envelope** as the event text
  (`{"message": {"role", "content": [{"text"|"toolUse"|"toolResult"…}]}}`);
  platform-written events store plain text. Decode the envelope, join `text`
  parts, and return every part **kind** in `parts`.
  Unlike `observability._turn_text`, which drops tool-only turns from a
  transcript, a memory inspector must never hide a payload that exists: a
  tool-only turn returns `text: ""` with `parts: ["toolUse"]` so the UI renders
  the turn as itself. Anything that is not a recognisable envelope (unparsable
  JSON, `content` not a list, unrelated object) is returned **verbatim**.
- `blob` → `{kind, role: None, text: None, parts: [], blob_bytes: <len>}`.
  Binary agent state never reaches the browser; only its size does.
- Unknown payload kinds are dropped (the preview SDK may add more).

**Pagination.** Every list route accepts and returns `next_token`; `max_results`
is clamped server-side. Nothing is capped silently: `/overview` counts one
`ListActors` page and reports `actor_count_truncated` rather than a wrong total.

**No TTL cache.** Unlike Observability (Logs Insights is billed per scan and
takes seconds), `GetMemory` is a single fast control-plane read. The frontend
fetches `/overview` once in `Memory.tsx` and passes `strategies` down, so
switching tabs does not refetch.

### 4. Validation & Error Matrix

| Condition | Result |
|---|---|
| `memory_id` missing, `/overview` | `200 {"configured": false, "memory": null, "strategies": [], "actor_count": 0, "actor_count_truncated": false, "other_memories": []}` |
| `memory_id` missing, any other route | `memory.not_configured` (409) |
| `/records` or `/records/search` with no derivable namespace | `memory.namespace_required` (400) |
| `strategy_id` matches no resolvable namespace | `memory.namespace_required` (400) |
| `status` filter not in `EXTRACTION_STATUS_FILTERS` | `memory.invalid_status_filter` (400), `detail={"allowed": ["FAILED"]}`, **no AWS call** |
| `query` empty or > 2000 chars, `top_k` outside 1..100 | `422` (pydantic) |
| any botocore/`ClientError` | `memory.unavailable` (502), message carries the AWS text |

> **Warning — two preview-API bounds the botocore service model understates.**
> Both were found only by calling the live API; unit tests alone would not have
> caught either.
>
> 1. `ListMemoryExtractionJobs` rejects `maxResults > 50`
>    (`Value at 'maxResults' failed to satisfy constraint: Member must have
>    value less than or equal to 50`). The shared `PAGE_MAX = 100` therefore
>    502s **every** request to that operation — hence `EXTRACTION_PAGE_MAX = 50`.
> 2. Its `filter.status` enum accepts **only** `FAILED`
>    (`Member must satisfy enum value set: [FAILED]`). Offering `SUCCEEDED` /
>    `IN_PROGRESS` in a picker produces a guaranteed 502. Jobs of every status
>    still appear in the *unfiltered* listing — you just cannot filter *for*
>    them, and the UI must say so.
>
> Extraction jobs are also **transient**: an account with populated long-term
> records can legitimately return zero jobs. Treat an empty job list as "none
> retained", never as "extraction never ran".

### 5. Good/Base/Bad Cases

- **Good** — `/records?actor_id=<agent>__river&strategy_id=semantic_facts-…`
  resolves `/facts/<agent>__river`, lists records, and `POST /records/search`
  returns the same shape with `score` populated and ranked.
- **Base** — a session has events but no records yet (extraction is
  asynchronous). The long-term view must render the "extraction pending — check
  the extraction tab" empty state, not an "empty memory" state.
- **Bad** — `?status=SUCCEEDED` → typed 400 before any AWS call;
  `?actor_id=` (empty) → the key is omitted from the AWS `filter` dict entirely,
  because the preview API rejects empty strings inside `filter`.

### 6. Tests Required

`backend/tests/test_memory_console.py`, hermetic (stubbed clients, no AWS):

- Overview: field projection, datetime → ISO, `is_platform` marking,
  `actor_count_truncated` true when a `nextToken` remains.
- `configured: false` on `/overview` pre-bootstrap; 409 `memory.not_configured`
  on every other route (parametrized over all of them).
- Actor decode: resolved name, deleted agent (`agent_name is None`, still
  `scoped`), unscoped actor, `__` inside the human part, and **one** AWS call for
  many actors sharing an agent (asserts no N+1).
- Sessions: ledger join present/absent + `message_count`.
- Events: full text preserved (no server truncation), blob → size only, unknown
  kind dropped; harness envelope decoded; tool-only envelope keeps
  `parts == ["toolUse"]` with `text == ""`; non-envelope JSON verbatim.
- Namespaces: `{actorId}` substituted; leftover placeholder → `resolvable: false`.
- Records: namespace derived from (actor, strategy); explicit `namespace` wins;
  no derivable namespace → 400; unknown strategy → 400 (never a silent fallback
  to another strategy's namespace).
- Search: `searchCriteria == {searchQuery, topK, memoryStrategyId}` and `score`
  surfaced; empty query → 422.
- Extraction: empty filters omitted; no `filter` key at all when unfiltered;
  `maxResults == 50`; unsupported status → 400 with `detail.allowed` **and**
  `data.calls == []`; `messages` shape drift → `[]`.
- `next_token` round-trips in **and** out, parametrized across all list routes.
- Read-only: `inspect.getsource` of both modules contains no forbidden operation
  name, and the router exposes no `PUT`/`PATCH`/`DELETE` method.

### 7. Wrong vs Correct

#### Wrong — one page cap for every operation, and a plausible status enum

```python
PAGE_MAX = 100

def list_extraction_jobs(status=None, ...):
    kwargs = {"memoryId": mem_id, "maxResults": min(max_results or 100, 100)}
    if status:
        kwargs["filter"] = {"status": status}   # "SUCCEEDED" → AWS ValidationException
    return data_client().list_memory_extraction_jobs(**kwargs)
```

```tsx
const STATUSES = ["", "SUCCEEDED", "FAILED", "IN_PROGRESS"];  // 3 of 4 always 502
```

Both bounds pass ruff, pytest and tsc — the failure only appears against real
AWS, as a 502 on the console's default view.

#### Correct — per-operation cap, enum pinned and validated before the call

```python
PAGE_MAX = 100
EXTRACTION_PAGE_MAX = 50            # ListMemoryExtractionJobs rejects > 50
EXTRACTION_STATUS_FILTERS = ("FAILED",)   # the API's entire enum

def list_extraction_jobs(status=None, max_results=None, ...):
    if status and status not in EXTRACTION_STATUS_FILTERS:
        raise AppError("memory.invalid_status_filter", ...,
                       detail={"allowed": list(EXTRACTION_STATUS_FILTERS)},
                       status_code=400)
    kwargs = {"memoryId": require_memory_id(),
              "maxResults": _page_size(max_results, EXTRACTION_PAGE_MAX)}
```

```tsx
// only FAILED can be filtered; the unfiltered listing still shows every status
const STATUSES = ["", "FAILED"] as const;
```

#### Wrong — showing the stored event text directly

```python
"text": (conv.get("content") or {}).get("text", "")
# harness turn renders as: {"message": {"role": "assistant", "content": [{"text": …
```

#### Correct — decode the envelope, keep tool turns visible

```python
text, kinds = _decode_turn((conv.get("content") or {}).get("text", "") or "")
return {"kind": "conversational", "role": conv.get("role"),
        "text": text, "parts": kinds, "blob_bytes": None}
```
