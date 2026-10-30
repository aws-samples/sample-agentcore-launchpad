# Interface: memory backend (N1 → N6 — implemented by SelfHostedMemoryBackend)

Frozen 2026-10-08. Full Python signatures are also pasted verbatim in
`docs/cn/interfaces/features.md`; this file adds call sites, error/status semantics and
the Task-5 reviewer's contract notes for whoever implements `MemoryBackend` (N6,
self-hosted Memory on OpenSearch/S3 per `docs/cn/GRAPH-LOOP-SPEC.md` §3 N6).

**Implemented 2026-10-09 (N6)** by `backend/app/services/memory_selfhosted.py::
SelfHostedMemoryBackend`, registered in `app/main.py::create_app` when `"memory"` is
in `Settings.features`. Storage: S3 snapshots `memory/<store>/<actor>/session_<sid>/…`
(events written by the runtime hook; see `memory-runtime.md` §3), OpenSearch
`launchpad-memory` (long-term records) and `launchpad-jobs` (extraction job status) —
`launchpad-<env>-memory` / `launchpad-<env>-jobs` in a named EnvName environment, since
D17 puts every environment of an account+region on ONE shared domain
(`launchpad-memory-domain` stack) and isolates them by index prefix + a per-environment
FGAC role `launchpad_memory_rw[_<env>]` (`app/core/naming.py::index_name` /
`index_patterns` / `security_role_name`; `memory_bootstrap.py` provisions them) —
SQLite `memory_stores` (console-created stores; the workspace default
`launchpad_memory-<workspace_id>` is synthesized on read until the console edits it for
the first time). Substrate keys: `MEMORY_RESOURCE_KEYS`
(`services/memory_bootstrap.py`) — missing ⇒ every substrate-touching call raises 503
`memory.not_provisioned` and `memory_overview(ws)["configured"]` is `False`.

## Where it lives

`backend/app/services/memory_backend.py` defines `MemoryBackend(Protocol)` plus
`set_backend(impl) / backend_or_none() / get_backend()`, and a set of **pure helpers**
that have no backend and work regardless of registration: `scoped_actor(agent_id,
base_actor)`, `spec_memory_id(agent_spec)`, `memory_id_or_none(workspace)`,
`decode_actor(actor_id)`, `decode_record_text(raw)`.

## Methods — signature, return shape, caller

| method | return shape | called from |
|---|---|---|
| `memory_overview(ws)` | summary dict | `GET /api/memory/overview` |
| `get_strategies(ws)` | `list[dict]` of long-term strategies | internal — feeds `resolve_namespaces` |
| `list_actors(ws, next_token=None, max_results=None)` | `{items, next_token}`; each item carries `decode_actor()` fields | `GET /api/memory/actors` |
| `list_sessions(ws, actor_id, next_token=None, max_results=None)` | `{items: [{session_id, …}], next_token}` | `GET /api/memory/sessions` |
| `list_events(ws, actor_id, session_id, include_payloads=True, next_token=None, max_results=None)` | `{items, next_token}` — **AgentCore-shaped**, see Contract notes | `GET /api/memory/events`; `services/observability.session_transcript` |
| `resolve_namespaces(ws, actor_id, strategies=None)` | `[{strategy_id, namespace, resolvable}, …]` | `GET /api/memory/namespaces`; `/records`, `/records/search` (derive namespace from `(actor_id, strategy_id)`) |
| `list_records(ws, namespace, strategy_id=None, next_token=None, max_results=None)` | `{items, next_token}` of long-term records | `GET /api/memory/records` |
| `search_records(ws, namespace, query, top_k=5, strategy_id=None)` | `dict` — semantic retrieval result | `POST /api/memory/records/search` |
| `delete_record(ws, record_id, actor_id)` | `{record_id, actor_id, namespace, strategy_id, deleted: true}` | `DELETE /api/memory/records/{record_id}?actor_id=…` (N11 r3-C, N10v2-bug6) — the only mutation in `routers/memory.py`; `actor_id` is mandatory and the backend checks the stored `store_id` **and** `actor_id` before deleting, so a record of another actor/workspace answers 404 `memory.not_found` and is never removed. Route policy `perm:memory.manage`; the handler additionally requires an administrator or the **owner** (`Agent.owner`) of the agent the actor is scoped to (`decode_actor`), 403 `memory.record_forbidden` otherwise (an unscoped actor is admin-only). Self-hosted: one `delete_doc` on the memory index — there is no per-record S3 object (S3 holds session snapshots and turn events only) — preceded by the unlinking of the record's merge-v2 history neighbours (N16-obs114, below: "the record delete unlinks"; undone when the delete fails, N17-obs91) |
| `list_extraction_jobs(ws, actor_id=None, session_id=None, strategy_id=None, status=None, next_token=None, max_results=None)` | `{items, next_token}` | `GET /api/memory/extraction-jobs` |
| `create_turn_event(ws, actor_id, session_id, prompt, answer)` | `dict` | chat/invoke write path (one USER/ASSISTANT turn) |
| `retrieve_records(ws, namespace, query, top_k=3)` | `list[dict]` | runtime prompt enrichment |
| `session_memory_summary(ws, actor_id, session_id, memory_id=None)` | `dict` | `GET /api/chat/{agent_id}/memory` |
| `list_memory_resources(ws)` | `{items: [{id, …}]}` | `GET /api/memory/resources` |
| `get_memory_resource(ws, memory_id)` | detail dict | `GET /api/memory/resources/{memory_id}`; admin `/adopt` probe |
| `create_memory_resource(ws, name, description, event_expiry_days, strategies, namespace_keys=None)` | created detail (`id` set) | `POST /api/memory/resources` |
| `update_memory_resource(ws, memory_id, *, description=None, event_expiry_days=None)` | refreshed detail | `PUT /api/memory/resources/{memory_id}` |
| `delete_memory_resource(ws, memory_id)` | `{id, deleted: true, cleanup: {s3, index: ok\|skipped}, cleanup_warnings: [...]}` | `DELETE /api/memory/resources/{memory_id}` — the ledger row is deleted and committed first; the two storage purges (the store's S3 prefix, `delete_by_query` on the memory index) are best-effort, every step guarded (N14-obs4, N17-obs93 — below: "the store delete purges best-effort") |

Every method takes the `WorkspaceContext` it targets first (`ws` above).

**Agent delete purges the agent's long-term records (N11-obs19, r3-C).** Per-agent memory
is scoped through `scoped_actor` (`<agent_id>__<human>`), so the records belong to the
agent and go with it: `deployer/teardown.py::cleanup_residue` runs one more best-effort
step, `memory_records`, right after `memory_snapshots`. It calls the module function
`memory_selfhosted.purge_agent_records(ws, agent_id, memory_id=None)` — a single
`delete_by_query` on the environment's memory index (`naming.memory_index()`, D17
prefix-aware) filtered on the store the spec pins (or the workspace default) **and** the
actor prefix `<agent_id>__`, so a neighbour agent's records and another store sharing the
index are untouched. Reported like every residue step (`deleted · N record(s)` /
`skipped` when the workspace has no substrate or the agent left no records / `failed`
with the error type), surfaced in the `DELETE /api/agents/{id}` response (`removed`,
`failed`, `steps`).

**The store delete purges best-effort (N14-obs4, N17-obs93 `cn/r17-n`).** The delete is complete
once the ledger row is gone — a retried `DELETE` would only 404 — so nothing a storage step does
after the commit may turn into an error on the delete the member asked for. Both steps are
guarded the same way: the S3 prefix purge (`list_objects_v2` + `delete_objects` pages) and the
OpenSearch `delete_by_query`. A step that raises — a non-2xx answer, a transport failure the
retrying pool gave up on, a botocore `ClientError` such as the recorded `AccessDenied` /
`SlowDown` — is reported `skipped` in `cleanup` with one `cleanup_warnings` line (`s3: ClientError:
AccessDenied (ListObjectsV2)`, `index: ReadTimeoutError` — `redacted_error`: type + status / error
code + operation, never a body) and one WARNING log line, and the NEXT step still runs. **A
`DeleteObjects` that answers 200 with `Errors[]` is a failure too (N17-obs119, `cn/r17-t`):** S3
deletes what it can and names the keys it refused (a per-key AccessDenied, a lifecycle lock) in
the body; the purge deletes through the one reporting deleter (`services/s3_sweep.py`:
`DeleteObjects` in batches, every answer's `Errors[]` read) and raises `S3SweepResidue` for the
refused keys, so the step is `skipped` with `s3: S3SweepResidue: 2 of 3 objects refused
(AccessDenied ×2)` — counts and S3 codes, never a key or a message — instead of `deleted: true`
with the keys still under `memory/<store>/`. Before `cn/r17-t` the purge never read the answer.
Before `cn/r17-n` the S3 purge was unguarded: a `list_objects_v2` failure escaped as 502
`memory.unavailable` (botocore text in the message) for a store that was already gone and the
index purge after it never ran. There is no retry job (unlike the KB's WB-008 `kb_cleanup`): an
operator re-runs the purge by hand from the warning (`aws s3 rm --recursive` on the prefix /
a `delete_by_query` on `store_id`). The managed registration is dropped after the backend returns
(`memory_ownership.forget`), which the old 502 also skipped.

## No backend registered

`memory_backend.get_backend()` raises `AppError("feature.unavailable", ..., {"feature":
"memory"}, status_code=503)` (`app.core.features.feature_unavailable`). Routers call
`_guard(memory_backend.get_backend().<method>, ...)` — `_guard`
(`routers/memory.py`) lets domain errors (incl. `feature.unavailable`) pass through
untouched, same pattern as `routers/chat.py`; an AWS `ClientError` is left to the
global handler in `app.core.errors`. **Ledger-side guards run before the backend
lookup** — see Contract notes below (404/409 precede the 503).

## How a node registers a backend

```python
from app.services import memory_backend
memory_backend.set_backend(SelfHostedMemoryBackend(...))  # at app startup (N6)
```

`memory_backend.backend_or_none()` is for callers that want to no-op rather than raise
(e.g. an optional best-effort read).

## Workspace isolation: a member reads only their own actors (N17-obs531, `cn/wk-authz`)

The memory console (`routers/memory.py`) is a read surface over the whole store, so the backend
lists every actor of the workspace. Visibility is decided in the router, per request, through
`_Hidden` (`_hidden(db, ws, viewer, request)`), which combines two axes:

* **hidden agents** (`agent_access.HiddenAgents`, users-and-ownership.md §7): the actors
  `<agent>__<human>` of agents workspace isolation hides from the caller, their sessions (ledger
  attributed) and namespaces carrying them — unchanged;
* **the caller's own human** (new): for a member of an `isolated` workspace (`viewer.filters`),
  an actor is theirs iff its human segment — `decode_actor(actor_id)["human_actor"]`, the part
  after the first `__` — equals their signed-in username, the ledger's human actor
  (`routers/chat._caller`: `identity.username`, `"river"` only on the open console where nobody
  filters). An unscoped actor (no `__`, written outside the platform) is theirs only when it IS
  their username; the legacy shared `default` actor is nobody's. Administrators, `shared`
  workspaces and the open console have `own_human = None` and this axis hides nothing.

Effects: `GET /api/memory/actors` and `GET /api/memory/extraction-jobs` (without `actor_id`)
drop every other human's actors; `GET /sessions`, `/events`, `/namespaces`, `/records`,
`/extraction-jobs?actor_id=`, `POST /records/search` and `DELETE /records/{id}` keyed by another
human's actor answer **`404 memory.actor_not_found`** — the envelope a hidden agent's actor
already got, so an actor id is never probeable (403 would confirm it exists). A namespace
(`/facts/<actor>`, `/summaries/<actor>/<session>`; the catalog substitutes `{actorId}` as one
whole segment) is readable by a scoped member only when it carries no foreign scoped actor AND
at least one segment that is their own actor — the strategy words carry no `__`, so an unscoped
foreign actor (`/facts/default`) cannot be told from one by shape, and the member's own resolved
namespaces always name their actor. The record delete keeps its own creator rule afterwards
(`memory.record_forbidden`, N10v2-bug6). Before this, a student under `isolated` listed every
actor of the workspace, other students' and the administrator's included (workshop dry run
2026-10-29). Tests: `tests/test_memory_actor_scope.py`,
`tests/test_agent_visibility.py::test_memory_console_hides_hidden_agents_actors`.

## Contract notes (from the Task 5 review; dispositions after N6's implementation)

- `list_events()["items"]` **AgentCore-shaped** ✅ — `SelfHostedMemoryBackend._event_item`
  returns both the AgentCore keys (`eventTimestamp`, `eventId`,
  `payload[].conversational.{role, content.text}`, consumed by
  `observability.session_transcript`) **and** the console keys the untouched frontend
  `ShortTermTab` reads (`event_id`, `at`, `branch`, `metadata`, and per payload part
  `kind`, `role`, `text`, `parts`, `blob_bytes`) — a hybrid shape, ruling R7. Pages are
  `{items, next_token}` with S3 `ContinuationToken`s passed straight through.
- `decode_actor` contract ✅ unchanged — `list_actors` items carry `actor_id`,
  `human_actor`, `agent_id`, `scoped` exactly as this helper produces them.
- `memory_id` ✅ = a `memory_stores.memory_id` value; the bootstrap stages
  (`memory_bootstrap.py` for the hub, `workspace_bootstrap.py::_stage_memory` per
  workspace) write `workspace.resources["memory_id"]`, read back by
  `memory_selfhosted.substrate_for`/`has_substrate` (not `memory_id_or_none`, which
  remains available for any caller that wants the raw resource value without the
  503-raising substrate resolution).
- **Ledger-guard order ✅ unchanged** — `routers/memory_resources.py` still runs
  `memory_ownership.require_managed(...)` ahead of every `_guard(memory_backend
  .get_backend()...)` call, so a 404/409 on an unknown/conflicting resource precedes
  the 503 a missing backend or substrate would otherwise raise.
- `retrieve_records` ✅ `decode_record_text`-compatible — `_record()` builds `text` /
  `structured` / `raw_text` through `decode_record_text(hit["text"])`, and
  `retrieve_records` is a thin wrapper over `search_records(...)["items"]`.

## Substrate bootstrap: transient retry and the post-failure probe (N-SCN-obs6)

`memory_unavailable_reason` is a **persisted verdict**: `workspace_has_shared_memory()`
requires it empty, so a non-empty value flips the architect's capabilities to
`shared_memory:false` and gates `memory: workspace` deploys until the next bootstrap. Two
rules keep a transient OpenSearch hiccup from becoming that verdict:

- **Retry inside the funnel.** `aws_clients.opensearch_http(ctx, endpoint, *,
  attempts=3)` is the only place the urllib3 pool is built. The pool parameters —
  5 s connect / 30 s read timeout, `retries=False` — are **unchanged** from the
  pre-existing `OpenSearchHttp.__init__` default; the fix adds the
  `opensearch_retry.RetryingHttp` wrapper (and the probe below), nothing else. The
  wrapper re-issues a request on a urllib3 timeout / connection / protocol error or an
  HTTP 429, 502, 503, 504, sleeping 1 s then 2 s (exponential from 1 s); non-transient
  statuses (400/403/404/409) surface at once. Every hub-side substrate call (index
  HEAD/PUT, FGAC security GET/PUT, document PUT/GET/DELETE by id, search,
  delete-by-query) is idempotent, so the repeat is safe; only the extractor Lambda bulks,
  and it does not go through the wrapper. The mirrored `opensearch_http.py` core is
  untouched (ruling R15) — the policy lives hub-side only.
  - **Per-caller tiers (attempts × read timeout).** Both `attempts` and `read_timeout`
    are part of the funnel's cache key (`aws_clients.opensearch_http(ctx, endpoint, *,
    attempts=3, read_timeout=30.0)`); the constants live next to each other in
    `services/opensearch_retry.py`. The urllib3 pool's 5 s connect timeout is common to
    every tier. (N-SCN-obs11 added the read-timeout dimension: a 30 s read timeout on a
    console poll turned a busy t3.small node into a 34 s stall.)

    | caller | attempts | read timeout | worst case on a dead/busy domain |
    |---|---|---|---|
    | bootstrap stages (`services/bootstrap.py`, `workspace_bootstrap.py`) — index/FGAC setup, the post-failure probe | `DEFAULT_ATTEMPTS = 3` | funnel default `OPENSEARCH_READ_TIMEOUT_SECONDS = 30` (what the FGAC security API on a t3.small can take) | ≈ 3 × 30 s + 1 s + 2 s ≈ 93 s |
    | console request threads — `SelfHostedMemoryBackend._os` (every `/api/memory` read) and `SelfHostedKnowledgeBackend._os` (every `/api/knowledge-bases` call, incl. the detail's job lookup) | `INTERACTIVE_ATTEMPTS = 2` | `INTERACTIVE_READ_TIMEOUT_SECONDS = 10` | ≈ 2 × 10 s + 1 s ≈ 21 s |
    | `purge_agent_records` (agent teardown's `_delete_by_query`) | `INTERACTIVE_ATTEMPTS = 2` | funnel default 30 s — a purge is not a poll and may legitimately outlast a search | ≈ 2 × 30 s + 1 s ≈ 61 s |

    The knowledge detail route additionally caches the job lookup and degrades instead
    of failing — see [knowledge-backend.md](knowledge-backend.md#detail-route-ingestion-job-cache-and-jobs_degraded-n-scn-obs11).
  - **Retries are logged.** The funnel wires `RetryingHttp.log` to
    `logging.getLogger("app.services.opensearch_retry").warning`: one WARNING per retry
    with method, URL, failure type and attempt — never a request or response body.
- **Probe before degrading.** When `ensure_memory_substrate` still raises, the hub
  bootstrap memory stage (`services/bootstrap.py`) runs
  `memory_bootstrap.probe_memory_substrate(os_http)`: healthy ⇔ `_cluster/health` is
  `green` or `yellow` **and** the three fixed indices exist **and** the FGAC security role
  GET answers with the role present **and** the FGAC role mapping GET answers with the
  mapping present — everything the stage provisions (never raises; failures report the
  exception type only). Probe passes ⇒ the stage writes `memory_unavailable_reason`
  **empty** — the same verdict a successful stage gives, so a stale reason left by an
  earlier failure is cleared rather than kept — and prints
  `memory stage WARN: cleared stale memory_unavailable_reason='…' (substrate healthy,
  provisioning step skipped: <ErrorType>; probe: …)` (or `memory stage WARN:
  memory_unavailable_reason stays empty (…)` when it already was), and sets
  `memory_summary["warning"]`, which `scripts/bootstrap.py` shows as a
  `memory stage WARN` row. Probe fails ⇒ the reason is recorded as before
  (`OpenSearch FGAC provisioning failed (<ErrorType>)`, probe detail — e.g.
  `missing indices: …` or `role mapping … absent` — appended to the log line). The
  per-workspace `_stage_memory` is unchanged (it fails the workspace bootstrap loudly
  rather than degrading) but inherits the retry through the same funnel.

## Fixed indices: race-safe ensure (WB-007)

`ensure_memory_substrate` creates the three fixed indices through
`app/services/opensearch_ensure.py::ensure_index`, the hub-side wrapper that closes the
core's HEAD → PUT create race: an index another creator made first counts as **reused**
when its live mapping is compatible with the requested body (same property types, same
`knn_vector` dimension/engine/space/name, `index.knn` on), and fails the stage with
`IndexIncompatible` (an `OpenSearchError`, 409, `.problems`) when it is not. The core
module and its two mirrors (ruling R15) are unchanged and byte-identical — the extractor
Lambda and the runtime never create indices, so no redeploy follows. The rule is spelled
out in [knowledge-backend.md](knowledge-backend.md#index-ensure-the-create-race-is-closed-hub-side-wb-007).

## Error codes

| Code | Status | Raised by |
|---|---|---|
| `memory.not_provisioned` | 503 | `substrate_for(ws)` — the workspace has no `memory_os_endpoint`/`memory_id` |
| `memory.not_found` | 404 | `NotFoundError` from `_store`/`update_memory_resource`/`delete_memory_resource`, or `mapped_opensearch_error` on an OpenSearch 404 |
| `memory.name_exists` | 409 | `create_memory_resource` — duplicate store name in the workspace |
| `memory.unknown_strategy` | 422 | `create_memory_resource` — a strategy id outside the fixed catalog below |
| `memory.default_protected` | 409 | `delete_memory_resource` on the workspace's default store |
| `memory.record_forbidden` | 403 | `DELETE /api/memory/records/{record_id}` by a member who does not own the agent the record's actor is scoped to (or for an unscoped actor) — decided on the ledger before the backend is called |
| `memory.backend_unavailable` | 503 | `mapped_opensearch_error` — the OpenSearch domain itself is throttling (429) or unhealthy (5xx) |
| `memory.unavailable` | 502 | the routers' `_guard` fallback (`routers/memory._unavailable`, shared by `memory_resources` and the chat memory rail) for a failure neither `mapped_aws_error` nor `mapped_opensearch_error` translates — an OpenSearch 403 / 400, a transport failure. The message is STATIC (`memory lookup failed` / `memory operation failed`) and `detail.error_type` names the exception type; the redacted line (type + status / error code, `app.core.errors.redacted_error`) goes to the `launchpad.memory` log. N17-obs92 (`cn/r17-n`): `str(exc)` used to be the message — for the recorded IAM 403 the raw body with the instance role ARN reached the toast |

## Strategy catalog (fixed in v1, ruling R11)

| `strategy_id` | `type` | Namespace template | Extracted? |
|---|---|---|---|
| `semantic` | `SEMANTIC` | `/facts/{actorId}` | yes |
| `user_preference` | `USER_PREFERENCE` | `/preferences/{actorId}` | yes |
| `summarization` | `SUMMARIZATION` | `/summaries/{actorId}/{sessionId}` | yes |

No store can register a strategy outside this table (`create_memory_resource` 422s on
an unknown id); the catalog itself is not configurable per store beyond picking a
subset of these three. The never-extracted `episodic` entry (`CUSTOM`,
`/episodes/{actorId}/{sessionId}`, once a "v2 item") was **removed** in round 13
(N13-obs4, user decision 2026-10-17): a store row that still lists it loads with the
entry ignored (`_row_to_dict` logs one `ignoring retired strategies` line), and the
console no longer offers it.

## Embedding model (D21 M1-d)

The self-hosted backend no longer hard-codes Titan: `SelfHostedMemoryBackend._embedder()`
calls `services/embedding_backend.build_embedder()`, which follows the model registry's
**active embedding row** (`schemas.agent.active_embedding()`; Titan v2 / 1024 for the
seed) — `TitanEmbedder` for a Bedrock row, `OpenAIEmbedder` for an `openai_compatible` row
(key via `aws_clients.provider_api_key`). The memory index is created with the row's k-NN
`dimension` (`memory_bootstrap.fixed_indices()` → `embedding_backend.vector_index_body`),
and every `memory_extract` SQS message carries the row as `embedding: {model_id, kind,
base_url, api_model_name, credential_provider, dimension, max_chars, provider,
model_region, catalog_version}` so the extractor Lambda embeds with the same model
(`handler.embedder_for`). Changing the row while the memory index (or any KB index) holds
documents is refused by the registry with 409 `embedding.reindex_required` (see
`model-registry.md` §10). The mirrored core `opensearch_http.py` is unchanged; the
injected `embedder` seam of the backend constructor keeps the hermetic tests on
`FakeEmbedder`.

## Merge strategy (v2) — r14-a, N14-obs1

User decision 2026-10-18. The fixed three-strategy catalogue is unchanged; everything below
is additive and backward compatible with records written before it.

**Unanswered turns (N17-obs304).** An event is extracted only when it has non-empty
`ASSISTANT` text (`handler.has_assistant_answer`). This guard is deterministic and runs before
any model call. An event without such text gets a job `SKIPPED` with
`skip_reason: "no assistant answer in the turn (failed or empty) — nothing to extract"`, and
no fact, preference or summary record is written. Earlier unanswered events in the context
window are also left out of the transcript, so the model is never given a bare question.
Every extraction prompt (facts, preferences, the session summary, the rolling actor summary)
carries the same rule: record only what the conversation, or the summaries, say. The model
must never answer the user's questions itself, and must never add facts, figures or outside
knowledge that nobody stated. The runtime side of the rule (no event for a failed turn) is in
`memory-runtime.md` §3.

**Extraction with operation intent** (`backend/lambdas/memory_extractor/handler.py`). For
`/facts/{actorId}` and `/preferences/{actorId}` the extractor first retrieves the actor's
8 nearest ACTIVE records by similarity to the transcript and sends them to the model as
`EXISTING` candidates with short aliases (`c1`…`c8`). The model answers one operation per
item: `{op, target_id, confidence, …fields}`. A bare string or a pre-v2 object is read as
`add`. Each operation then goes through this table:

| condition (checked in order) | result |
|---|---|
| `op = noop` | nothing written (`counts.noops`) |
| `op = delete` with a candidate target and `confidence ≥ 0.6` | target gets `deleted_at` (soft); never a hard delete |
| `op = delete` otherwise (no candidate target, confidence < 0.6, or **no stated confidence** — an absent value reads as 0 for `delete` only) | ignored with a job warning (`counts.noops`) |
| nearest active record at cosine `≥ 0.92` | duplicate, nothing written, whatever the op |
| `op = update / supersede` without a candidate target | treated as `add` |
| `op = update` | target's text + embedding rewritten in place: same id, `created_at` kept, `updated_at` bumped |
| `op = supersede` | new record (`supersedes: [target]`); target gets `superseded_by`, `superseded_at` — both documents in ONE `_bulk` request (below) |
| `op = add` (cosine `0.75–0.92` included: the model decided) | new record |

**The supersede pair is one request (N17-obs90, `cn/r17-n`).** The successor and the retired
target are written by a single `_bulk` call (`index` action for both: the candidate hit carries
the target's full source, embedding included, so the retired copy is a lossless re-index). The
invariant the index scan checks — a record named in a `supersedes` list carries `superseded_by`
— therefore never depends on a second write landing: a fault on the request, or a process death
around it, leaves the index exactly as it was (the job is FAILED with the exception type and
SQS redelivers the event), and a partial item-level failure inside the bulk answer is an
`OpenSearchError` the extractor retries the same way. Before `cn/r17-n` the pair was a `put_doc`
followed by an `update_doc`; a 503 between the two left both records ACTIVE — and the redelivery
did NOT repair it (N17-obs122): on replay the successor is the nearest active record, the op is
deduped, and the original stays active for good. The duplicate branch therefore finishes a
half-written pair it meets (`_finish_half_written_supersede`): when the record a text dedupes
against lists a still-ACTIVE record in its `supersedes`, that record is retired under it
(`superseded_by` / `superseded_at`; `counts.supersedes` +1), so a replay — whatever the model
names: the original, the successor, nothing — converges to exactly one active record. Only a
record the successor already claims is touched; a near-duplicate of an unrelated record keeps
the table's "duplicate, nothing written" rule.
The consolidate pass still writes its merged record and then retires each member with one
`update_doc` per member (the members come from a search without their embeddings, so they are
not re-indexed); it is not reached by the fault walks and keeps the pre-r17-n shape.

**The dedupe decision is a strong read (N17-obs101, `cn/r17-n`).** The candidate read before the
model call and the nearest-record check before a write are k-NN searches, and OpenSearch search
is near-real-time: a record written with `refresh=False` is invisible until the next refresh of
its segment. The extractor refreshes the memory index once at the end of every event, which
covers SEQUENTIAL extractions of one actor; two extractions of the same actor running at the
same time (SQS delivers each turn as its own message and the Lambda scales out) could both read
an index without the other's write and both add — two ACTIVE records at cosine ≥ 0.92. Since
`cn/r17-n` the extractor issues one `POST /<memory index>/_refresh` right before the
nearest-record re-check of every item that writes (`add` / `update` / `supersede`), so every
write that has LANDED is visible to the decision — the stale candidate read is tolerated (the
model may name no target: the op degrades to `add`, and the re-check then finds the duplicate).
Cost: at most `2 × MAX_ITEMS_PER_STRATEGY` refreshes per event on a small index.

*Open (infra): per-actor serialisation.* The remaining window is a write that lands between the
re-check and our own write — a true race only per-actor serialisation closes. The design the
hub does not implement yet: the extraction queue becomes a FIFO queue (`<name>.fifo`,
content-based deduplication off, `MessageGroupId = actor_id`, `MessageDeduplicationId =
event_key`) so SQS hands the Lambda one in-flight message per actor while any number of actors
run in parallel; `memory_selfhosted.put_event` sets the two attributes, the memory stack renames
the queue (a standard queue cannot be converted — bootstrap must create the new one and the
EC2 template's `MemoryQueueSend` statement, the Lambda event source and `config/launchpad.yaml`
follow), and the Lambda's `report_batch_item_failures` keeps working (a FIFO batch stops at the
first failed item of a group). It touches four surfaces and a live migration, so it is recorded
as the open half of N17-obs101 rather than shipped with the strong read. Reserved concurrency 1
on the Lambda would serialise everything, not per actor, and is not the design.

**Extractor failure classification (N16-obs32, `cn/r17-n`).** `lambda_handler` acks and drops a
message only when its CONTENT was unusable (bad JSON, unknown kind, `DroppedMessage`: the
resource is gone); every infrastructure failure is reported in `batchItemFailures` so SQS
redelivers it (three receives, then the DLQ). The job row in `launchpad-jobs` is bookkeeping,
not the outcome: a jobs-index fault BEFORE any work (the idempotency read, the `IN_PROGRESS` /
`SKIPPED` upsert) is a `RetryableError`; the `FAILED` write next to a retry and the terminal
`COMPLETED` / `COMPLETE` write after every record is in the index are fail-soft (`_job_soft`:
one WARNING line with the exception type, the row left stale rather than the extraction
re-run for bookkeeping — a job the console shows `IN_PROGRESS` long after its session is the
trace of such a write). As a belt, a raw `OpenSearchError` 429 / 502 / 503 / 504, a urllib3 /
socket failure or a botocore throttle / timeout that still escapes a processor is classified
`retry`, never `poison`. Before `cn/r17-n` one transient on the first bookkeeping write escaped
raw and the turn was silently dropped (`tests/property/test_memory_merge_properties.py`
"domain_wide" / "classification" / "belt", graph pin `test_N16_obs32_*`).

**Output truncation and execution budget (r29 acceptance, 2026-10-28).** The synthetic
GLM 5.3 comparison returned `stopReason=max_tokens`, 2048 output tokens and no text at
`maxTokens=2048`; at 8192 it returned `end_turn` and a parseable preference array. Separately,
seven of twenty observed extractor invocations reached the old 120 s Lambda limit; the
evidence did not map all seven to one job.

The extractor starts each model read at 2048 tokens and allows exactly one larger, 8192-token
read only for Bedrock `max_tokens` / compatible-provider `length`. A truncated response is
discarded even if its text happens to be valid JSON. Non-truncated parse errors keep the
existing per-strategy warning/partial-success behavior and do not trigger this retry.
Facts, preferences and summary model reads finish before any core memory operation is
applied. Persistent truncation records `FAILED` / `model_output_truncated` and remains a
retryable SQS failure; it is not silently acknowledged. A completed event remains idempotent.
The read-only retry never encloses memory writes. This does not make SQS delivery or
multi-record OpenSearch writes transactional or exactly-once; existing transport-failure
redelivery, deduplication and partial-write recovery remain in force.

The extraction chat transport uses a 5 s connect timeout and a configurable read ceiling,
`LAUNCHPAD_EXTRACT_MODEL_READ_TIMEOUT_SECONDS` (CDK context
`extract_model_read_timeout_seconds`, range 1–120 s, default 120 s). The actual socket-read
timeout on each request is `min(configured ceiling, Lambda remaining seconds - 90 - 5)`.
When less than one second of read time remains, the request is not sent. Without a Lambda
context the configured ceiling still applies. Each Bedrock model request receives a client
with this timeout and `total_max_attempts=1`; the compatible-provider chat transport uses
separate connect/read timeouts with retries disabled, recalculating the remaining budget
after its existing one-time credential refresh as well. Embedding and KB transport timeouts
are unchanged. Exhaustion during core preparation records `FAILED` / `extraction_deadline`
and returns the message to SQS.
Optional consolidation/rolling-summary work with insufficient budget is skipped with a
warning after the core records have been stored. It does not fail the core extraction.

The outer Lambda budget remains **480 s**. Three initial reads at the default ceiling fit
within it: `3 × (5 + 120) + 90 = 465 s`; token retries compete for the same remaining time
and are not promised six full-length requests. The 120 s value is an allocation ceiling,
not a claim about a provider's p99 latency. Queue visibility remains six times the fixed
Lambda timeout, 2880 s, with batch size one and no batching window; receive-count/DLQ
behavior is unchanged. The existing KB 70 s work slice / 50 s reserve remains conservative.
This is a bounded model-call envelope, not a promise that arbitrarily slow embedding/
OpenSearch writes can always finish in the reserve.
AWS requires the function timeout not to exceed queue visibility and recommends the
six-times margin: [SQS event-source configuration](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html).

The first post-upgrade natural job failed explicitly after a 60 s Converse read timeout:
the Lambda completed normally at 62.379 s and returned the message for retry; it did not
hit the outer Lambda limit. The old implicit botocore read timeout was also 60 s—the prior
change made it explicit and disabled hidden SDK retries, rather than introducing that
default. Two later read-only synthetic reconstructions of the same new conversation
returned valid arrays in 30.47 s and 10.76 s. These are censored/variable latency observations,
not evidence that the provider always needs more than 60 s or that 120 s guarantees success.
No old job or real tool write was replayed for these measurements.

The diagnostics child logger emits only model identifier, attempt, token limit, normalized
stop reason, text length, numeric token usage and allocated read timeout; no raw model output, prompt, reasoning
or credentials. The model-request record retains the allocated budget even if the transport
fails before a response. A message-start record links SQS message/job IDs to the Lambda request ID,
so a subsequent platform timeout can be attributed without logging conversation content.

**Prompt size is a PERMANENT failure, fitted first, shrunk once (N17-obs237, `cn/r17-aj`).**
Default's extractor DLQ held a `memory_extract` message whose every receive logged
`outcome=retry … ValidationException when calling InvokeModel: 400 Bad Request: Too many input
tokens. Max input tokens: 8192, request input token count: 9067` — the transcript
(`CONTEXT_EVENTS` = 4 earlier turns + the new one) embedded whole for candidate retrieval
(Titan's 8192-token cap; CJK text runs at ≈1.1–1.5 tokens per character, so the embedder's
8000-character cut was not enough) and `process_memory_extract`'s last-resort `except Exception
→ RetryableError` redelivered a 400 the bytes themselves caused (the shared
`runtime_invoke.is_upstream_transient` had already answered False for it). Now: (1) the
transcript is fitted to `MAX_PROMPT_CHARS` (`LAUNCHPAD_EXTRACT_MAX_PROMPT_CHARS`, default 6000)
BEFORE any model call — oldest context events dropped first, then the turn truncated with a
marker (`fit_transcript`; a `warnings` entry records it); (2) a size refusal that still gets
through (`is_input_too_long`: "too many input tokens" / "input too long" / "context length" …
from the embedding or the Converse call) shrinks the prompt ONCE — every context event dropped,
the turn halved, floor 400 chars — and retries the same call, the rest of the extraction then
runs on the shrunk transcript; (3) a refusal after that is `InputTooLongError`: the job is
`FAILED` with `failure_reason: input_too_long: <the model's message>` and `failure_code:
input_too_long`, the message is acknowledged and dropped (`outcome=dropped`, one WARNING) —
never a redelivery. Any other 400 `ValidationException` from a model call (an invalid model id)
is `failure_code: model_validation` — since N17-obs535 (2026-10-30) FAILED for the hub and
REDELIVERED towards the DLQ, no longer dropped (see below). Transient faults keep their redelivery. DLQ
depth is not surfaced on the memory status route: the hub's instance role holds
`sqs:SendMessage` only (`infra/ec2/launchpad-cn-ec2.yaml`), and a read would need a new grant.
Tests: `tests/test_memory_extractor.py::test_the_live_size_refusal_is_permanent_and_the_shared_predicate_never_called_it_transient`,
`…::test_fit_transcript_drops_the_oldest_context_first_then_truncates_the_turn`,
`…::test_the_transcript_is_fitted_to_the_budget_before_any_model_call`,
`…::test_a_size_refusal_shrinks_the_prompt_once_and_the_extraction_completes`,
`…::test_a_size_refusal_after_the_shrink_is_failed_with_the_reason_and_dropped_not_retried`,
`…::test_a_size_refusal_from_the_extraction_model_is_shrunk_too`,
`…::test_another_model_validation_error_is_failed_and_redelivered_not_dropped`.

**The extraction model is the `memory_extract` role's (N17-obs252, user ruling 2026-10-22
「需要都读模型角色来确定模型」; `cn/r17-aj` carries the message + Lambda half, r17-am's `cn/r17-ah`
the role and its Settings field `memory_extract_model_id`).** `create_turn_event` adds an
`extract_model` block to the `memory_extract` message — `embedding_backend.extract_model_block()`:
the role's catalog row in the `message_block` shape minus the embedding fields
(`{model_id, kind, base_url, api_model_name, credential_provider, provider, model_region,
catalog_version, temperature}` — `temperature` since N17-obs534, below); the block is OMITTED when the Settings field is absent (a build before
r17-am; read with `getattr`) or names no catalog row. The Lambda's `chat_model_for(body, deps)`
turns it into the message's chat model: a Bedrock row → `BedrockChat` (Converse with that model
id on the Lambda's bearer client — `LAUNCHPAD_MODEL_REGION` / `LAUNCHPAD_MODEL_CREDENTIAL`; a
block naming another region or credential is not honoured, the Lambda has one bearer); an
OpenAI-compatible row → `OpenAIChat` (`POST {base_url}/chat/completions`, the row's own Identity
provider through `resolve_bearer`, cached per block for the container's life, the `embedder_for`
pattern); no block → the env `LAUNCHPAD_EXTRACT_MODEL_ID` as before, so messages in flight across
the update keep working. Every extraction call (`facts`, `preferences`, `summary`,
`consolidate`, `actor summary`) goes through `_chat(deps).converse_text`, and the N17-obs237 size
handling wraps whichever model runs. Tests: `tests/test_memory_selfhosted.py::
test_the_memory_extract_message_carries_the_roles_extraction_model`;
`tests/test_memory_extractor.py::test_the_extraction_model_comes_from_the_message_block_else_the_env`,
`…::test_an_openai_compatible_extraction_row_goes_through_its_own_provider`,
`…::test_a_size_refusal_from_the_message_model_is_shrunk_like_the_default`.

**Temperature follows the row's capability (F-LOAD-03, N17-obs534; found 2026-10-30 in the
ten-student simulation, `designs/ws-load-20261030`).** The Lambda used to hard-code
`temperature: 0` on every Converse / chat-completions call; Kimi K3 takes no temperature field
(N12-bug11) and refused every extraction of a role pointed at it. `extract_model_block()` now
carries `temperature: bool(row.temperature)` — the catalog row's capability (Kimi K3 False,
GLM 5.3 True; an OpenAI-compatible row its trait, True when absent) — and `BedrockChat` /
`OpenAIChat` send the field ONLY when the block says True; a block without the key (an older
hub) and the env fallback (no block) never send it. Belt: a 400 whose text names the field
(`is_temperature_rejection`: Bedrock "This model doesn't support the temperature field", an
OpenAI-compatible "Unsupported parameter: 'temperature'") is retried ONCE without it before any
classification (`_with_temperature_fallback`; the chat object remembers the downgrade for the
container's life; one WARNING line); the N17-obs237 size handling is untouched. Tests:
`tests/test_memory_selfhosted.py::test_the_extract_model_block_carries_the_rows_temperature_capability`;
`tests/test_memory_extractor.py::test_bedrock_chat_sends_temperature_only_when_the_block_says_so`,
`…::test_openai_chat_sends_temperature_only_when_the_block_says_so`,
`…::test_a_bedrock_400_naming_temperature_is_retried_once_without_it`,
`…::test_the_temperature_retry_happens_exactly_once_before_classification`,
`…::test_an_openai_400_naming_temperature_is_retried_once_without_it`,
`…::test_a_400_not_about_temperature_is_not_retried_and_the_size_classifier_reads_the_body`.

**The Runtime's own messages carry the block too (F-LOAD-02, N17-obs533, same run).** The
rendered memory library's `LaunchpadMemoryHook` enqueued `{kind, store_id, actor_id,
session_id, event_key, long_term}` with no `extract_model`, so every agent-side turn ran the
Lambda's env fallback model whatever the role said. `deployer/environment.runtime_environment`
now writes `LAUNCHPAD_MEMORY_EXTRACT_MODEL` (compact JSON of `extract_model_block()`,
`memory_support.ENV_MEMORY_EXTRACT_MODEL`, listed in `PLATFORM_ENV_KEYS` — 25 of the reserved
28) wherever it writes the queue URL, and the hook forwards it parsed as `extract_model`
(`_memory_env()["extract_model"]`; absent / unparseable / no `model_id` → the message omits
the block exactly as before). The value is baked in at deploy: **a `memory_extract` role change
reaches an agent on its next redeploy; hub-side messages (`create_turn_event`) follow the role
immediately.** The Lambda never calls the hub. Contract: `memory-runtime.md` §2. Tests:
`tests/test_runtime_environment.py::test_the_extract_model_block_rides_with_the_queue_url_and_is_absent_without_a_row`,
`tests/test_memory_runtime.py::test_after_invocation_sends_the_deployed_extract_model_block`,
`…::test_after_invocation_omits_extract_model_without_a_usable_env_value`.

**A `model_validation` outcome is FAILED and redelivered, never dropped (N17-obs535, same
run).** A 400 the request caused (an invalid model id, a capability the catalog row got wrong, a
provider refusing the request after the temperature retry) used to be recorded FAILED and then
acknowledged like a size refusal — the message vanished with nothing outside the jobs index
saying so. `process_memory_extract` still writes the job `FAILED` / `failure_code:
model_validation` / `failure_reason: model_validation: <text>` (the hub's extraction-jobs view
shows it), then raises `ModelValidationFailure` (a `RetryableError`): the receive is reported in
`batchItemFailures`, SQS redelivers and the message reaches the DLQ after `maxReceiveCount`,
and every receive logs ONE structured WARNING line `… outcome=failed_permanent …
detail=reason=model_validation model=<extraction model id> error=<provider text>`. The poison
path stays for `input_too_long` (genuinely permanent after the shrink retry) and for unknown
kinds / unparseable bodies. Tests:
`tests/test_memory_extractor.py::test_another_model_validation_error_is_failed_and_redelivered_not_dropped`,
`…::test_an_openai_compatible_4xx_is_permanent_like_the_bedrock_validation_error`,
`…::test_a_401_that_survives_the_re_resolution_is_permanent`.

**New record fields** (all optional; `opensearch_http.MEMORY_RECORD_V2_PROPERTIES`):
`updated_at` (date), `confidence` (float), `superseded_by` / `supersedes` (keyword),
`superseded_at` / `deleted_at` / `expired_at` (date), `last_op` (keyword:
`add|update|supersede|delete|consolidate`). A document without them is ACTIVE with
confidence 1.0 and timestamp `created_at` (epoch when absent). The mapping is extended in
place by `opensearch_ensure.ensure_mapping` from `memory_bootstrap.ensure_memory_substrate`
on a reused memory index. It is idempotent, makes no second PUT, and never reindexes.

**Recall.** `search_records` / `retrieve_records` and the runtime library over-fetch
`min(4 × top_k, 20)` k-NN hits with their embeddings. They drop superseded, deleted and
expired hits and rank by `cosine × 0.5^(age_days / half_life) × max(confidence, 0.5)`.
Age counts from `updated_at`, else `created_at`. `score` on a search hit is this recall
score and `similarity` is the raw cosine. `list_records` and the Chat rail
(`session_memory_summary`) hide inactive records. `list_records(…, include_superseded=True)`
shows them, and every record item carries the op-history fields plus `active`.

**Per-store settings** (`memory_stores.merge_settings` JSON, NULL = defaults; detail
`merge` block; create / update `merge` body, partial, `null` resets one knob). On create the
console omits every knob left empty and sends no `merge` block at all when all five are at
their defaults (N14-obs2); on edit a cleared nullable knob is sent as `null`:

| knob | default | bounds | effect |
|---|---|---|---|
| `recall_half_life_days` | 90 | 1–3650 | recall recency decay |
| `max_facts_per_actor` | 200 | 10–10000 | consolidation trigger for `/facts/{actorId}` |
| `max_preferences_per_actor` | 100 | 10–10000 | consolidation trigger for `/preferences/{actorId}` |
| `long_term_ttl_days` | `null` (keep) | 7–3650 | soft TTL |
| `actor_summary_interval` | 1 | 1–100 | rolling actor summary cadence (5 before 2026-10-29; recall also reads the per-session summaries directly) |

The `memory_extract` SQS message carries the store's knobs as `merge`. Agents get
`LAUNCHPAD_MEMORY_HALF_LIFE_DAYS` / `LAUNCHPAD_MEMORY_TTL_DAYS` only when those differ from
the defaults (`memory_selfhosted.recall_env_for`). A changed half-life or TTL therefore
reaches an existing agent at its next deploy, while the hub applies it at once.

**Cap and re-summarization.** After the strategies run, a namespace holding more ACTIVE
records than its cap gets one consolidation pass. The pass takes the oldest
`min(20, max(4, 2 × overflow))` records and asks the model to merge related ones
(`{text, merges: [ids], confidence}`). Each group of two or more becomes one `consolidate`
record that supersedes its members. Single-member groups, unknown ids and reused ids are
ignored. The pass is bounded to one per event, so a namespace far over its cap converges
over several extractions.

**TTL.** With `long_term_ttl_days` set, the hub hides older records on read through a range
filter on `updated_at` / `created_at`, with no write. The extractor marks up to 50 such
records of the actor `expired_at` per event. The actor summary is never expired.

**Rolling actor summary.** On every `actor_summary_interval`-th completed extraction of an
actor, the extractor rewrites `/summaries/{actorId}/_actor`. Its id is
`summary_record_id(store, namespace)` and its `metadata.kind` is `actor_summary`. The text
comes from the newest ≤ 10 session summaries and is capped at 150 words. The runtime's
`recall_context` appends it as one line.

**Not done in v2.** Consolidation clusters by age, not by an embedding clustering or by
recall counts: the substrate keeps no per-record recall statistics. Superseded records are
kept forever as history; only the existing record delete and agent purge remove them.

**The record delete unlinks (N16-obs114, `cn/r16-q`).** `DELETE /api/memory/records/{id}`
of a record inside a history chain never leaves a dangling link: in the same operation the
successor (`superseded_by`) drops the id from its `supersedes`; each predecessor
(`supersedes[]`) is re-pointed at the successor when there is one (`superseded_by =
successor`, which gains it in `supersedes` — the chain stays connected, the predecessor stays
retired); when the deleted record was the chain's head, exactly one predecessor — the newest by
`record_timestamp` (`updated_at`, else `created_at`) — has `superseded_by` / `superseded_at`
cleared and reads active again (the fact that retired it is gone; nothing is re-extracted), and
the other predecessors are re-pointed at it (`superseded_by` = the newest, which gains them in
`supersedes`): a consolidate's members are near-duplicates by construction, so freeing them all
would break "no two ACTIVE records at cosine ≥ the dedupe threshold" (rev-r16 ruling on
`cn/r16-q`). Nothing else about a neighbour changes; a neighbour id that no longer resolves is
skipped. **A failed delete changes nothing (N17-obs91, `cn/r17-n`):** the neighbours are
unlinked first and the document deleted second (a retry after a failed delete still finds the
record and re-runs the idempotent unlink); when `delete_doc` fails — the member sees the mapped
503 / 502 — `_relink_history` re-indexes every neighbour exactly as the unlink had read it, so
the head's revived predecessor is retired again next to the head that retired it and the index
is as it was before the call. The re-link is best-effort (a neighbour it cannot reach is one
WARNING line, id + exception type); the member's retry heals it because the record is still
there. Tests: `tests/test_memory_record_delete.py` (head / middle / tail /
consolidate / already-dangling / no-chain / the console route / the failed delete re-links),
`tests/property/test_memory_delete_unlink_properties.py` (any record of any chain shape, with
consolidate members at cosine ≥ 0.95: the index scan — dangling links AND the dedupe invariant —
is clean, exactly one predecessor comes back active in the head case, and only the link fields
of the direct neighbours changed),
graph `memory_record.api_delete_superseded`.

**Deploy (deploy-ops).** The memory stack CDK deploy changes the extractor Lambda code only.
The next `make bootstrap` adds the mapping fields (`mapping launchpad[-<env>]-memory v2
fields +…` in the created list once, then reused). The hub restart applies the
`memory_stores.merge_settings` column migration. Agents pick up the ranking at their next
deploy.
