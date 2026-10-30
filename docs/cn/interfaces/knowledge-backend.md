# Interface: knowledge-base backend (N1 → N6 — implemented by SelfHostedKnowledgeBackend)

Frozen 2026-10-08. Full Python signatures are also pasted verbatim in
`docs/cn/interfaces/features.md`; this file adds call sites and the Task-5 reviewer's
contract notes for whoever implements `KnowledgeBackend` (N6, self-hosted KB on
S3 + OpenSearch per `docs/cn/GRAPH-LOOP-SPEC.md` §3 N6, same node as Memory).

**Implemented 2026-10-09 (N6)** by `backend/app/services/knowledge_selfhosted.py::
SelfHostedKnowledgeBackend`, registered in `app/main.py::create_app` alongside Memory
(same `"memory"` feature flag — Knowledge Bases share the Memory substrate, not a
separate feature flag). Storage: S3 documents under the artifacts bucket
`kb/<kb_id>/<ds_id>/` (plus a `_text/` sidecar per uploaded PDF, R9), one OpenSearch
index per KB `launchpad-kb-<kb_id lower>` (chunks) and the shared `launchpad-kb-docs`
(document status) / `launchpad-jobs` (ingestion jobs) indices — all prefixed
`launchpad-<env>-…` in a named EnvName environment (D17: one OpenSearch domain per
account+region, `app/core/naming.py::kb_index` / `kb_docs_index` / `jobs_index`; the
`kb_ingest` SQS message carries the already-named `index`), SQLite `knowledge_bases`
(the console-facing row — there is no AWS KB resource to read back; `data_sources` is
stored inline as JSON since there is no AWS DataSource resource either).

## Where it lives

`backend/app/services/knowledge_backend.py` defines `KnowledgeBackend(Protocol)` plus
`set_backend(impl) / backend_or_none() / get_backend()`, and `KBAttachedError` (409 —
a KB with agents mounted was deleted without `force`; carries the blocking agent names
in `detail.agents`, same envelope shape as `agents.py`).

Mirrors the former `services/knowledge.py` public surface. `kb_gateway.py` (the
AgentCore KB connector gateway) has no CN equivalent and is gone for good — the
`POST /api/knowledge-bases/ensure-gateway` route is removed with it.

## Methods — signature, return shape, caller

| method | return shape | called from |
|---|---|---|
| `list_kbs(ws)` | `[{kb_id, name, description, status, …}]` | `GET /api/knowledge-bases`; assistant catalog; Create-Agent picker (`?status=ACTIVE`) |
| `get_kb_detail(ws, kb_id)` | detail incl. data sources and recent jobs — each data source carries `ingestion_jobs` (≤ 5, from a 10 s in-process cache), `jobs_degraded` and `has_objects` (`true` / `false` / `null`, see "Upload starts the sync" below); the detail carries `jobs_degraded` = any (see "Detail route" below) | `GET /api/knowledge-bases/{kb_id}`; system-preset install's KB verification |
| `create_kb(ws, name, description, source)` | created detail — `source` is `{mode, bucket, prefix}` | `POST /api/knowledge-bases` (202) |
| `update_description(ws, kb_id, description)` | updated detail | `PATCH /api/knowledge-bases/{kb_id}` |
| `delete_kb(ws, kb_id, force=False)` | `{kb_id, deleted: true, cleanup: {s3, index, docs: ok\|pending}, cleanup_warnings: [...], cleanup_job_id}` (see "Delete contract" below) — raises `KBAttachedError` (409) when agents still mount the KB and `force` is false | `DELETE /api/knowledge-bases/{kb_id}` |
| `upload_files(ws, kb_id, files)` | `{keys: [str], job: {job_id, status, coalesced, covers_upload} \| null, sync_error?: str, data_source?: {ds_id, name, mode, bucket, prefix, status, created_at}}` — the stored object keys plus the ingestion sync the upload started (or coalesced onto); see "Upload starts the sync" below (N17-obs204). `data_source` is present only when the KB had no `upload`-mode source and this upload created one (N17-obs242, "Upload recreates a missing upload source" below) | `POST /api/knowledge-bases/{kb_id}/files` |
| `add_data_source(ws, kb_id, source)` | the data source + `created: bool`. Mode `upload` is idempotent per KB (N17-obs243): a KB that already has an `upload`-mode source returns it (`created: false`) instead of appending; mode `existing` always appends (`created: true`) | `POST /api/knowledge-bases/{kb_id}/data-sources` (201 `created: true`; 200 `created: false`) |
| `delete_data_source(ws, kb_id, ds_id)` | `{ds_id, deleted: true, cleanup: {s3, index, docs: ok\|pending\|skipped}, cleanup_warnings, cleanup_job_id}` — same best-effort contract as `delete_kb` (WB-010): pending steps go to a `kb_cleanup` job with `scope: data_source`; `s3` is `skipped` for an `existing`-mode source. Removing the KB's last `upload`-mode source is allowed and changes nothing else server-side: the next upload creates a fresh one (N17-obs242) | `DELETE /api/knowledge-bases/{kb_id}/data-sources/{ds_id}` |
| `start_sync(ws, kb_id, ds_id)` | `dict` | `POST /api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync` |
| `list_ingestion_jobs(ws, kb_id, ds_id)` | `list[dict]` | `GET /api/knowledge-bases/{kb_id}/data-sources/{ds_id}/ingestion-jobs` |
| `list_documents(ws, kb_id, ds_id, *, page_size=50, token=None)` | `{documents, next_token}` (ruling R8 — not `{items, next_token}`; see below) | `GET /api/knowledge-bases/{kb_id}/data-sources/{ds_id}/documents` |
| `query(ws, kb_id, text, number_of_results=8)` | `list[dict]` | `POST /api/knowledge-bases/{kb_id}/query` — retrieval playground |
| `attached_agents(ws, kb_id)` | `list[str]` of agent names | **declared, no current caller** (see below) |
| `refuse_if_attached_to_system_agent(ws, kb_id)` | raises when a system-managed preset mounts `kb_id` | **declared, no current caller** (see below) |

Every method takes the `WorkspaceContext` it targets first (`ws` above).

## Detail route: ingestion-job cache and `jobs_degraded` (N-SCN-obs11)

`GET /api/knowledge-bases/{kb_id}` is what the console (and the e2e scenario) polls while
an ingestion runs. In the self-hosted backend every poll used to issue one synchronous
`_search` on `launchpad-jobs` per data source through the funnel client; while the
extractor Lambda bulk-indexes HNSW vectors and `_refresh`es on the single t3.small node
that tiny search could exceed the 30 s read timeout, and one timeout + 1 s backoff + the
retry turned a < 0.4 s poll into a 34 s HTTP 200 (round-5 runs #7 and #9). Three things
changed, all hub-side (the mirrored `opensearch_http.py` core is untouched, ruling R15):

- **Cache.** `knowledge_selfhosted.cached_jobs()` serves the per-data-source job list
  from an in-process cache keyed by `(workspace_id, kb_id, ds_id)` with a
  `JOBS_CACHE_TTL_SECONDS = 10` TTL and single-flight per key (concurrent polls share one
  search; the pattern is `services/observability.py::cached`). `start_sync`,
  `delete_data_source` and `delete_kb` drop the affected keys, so a freshly seeded
  `STARTING` job is visible on the very next poll and a removed source leaves nothing
  behind. Expired entries are evicted on every store. `reset_jobs_cache()` exists for
  tests/config reloads (the test conftest calls it between tests).
- **Generation guard (r7-j).** Dropping the key is not enough when a poll's search is
  already running: a `_search` that ran *before* `start_sync` wrote `STARTING`, stored
  *after* the invalidation, would pin a pre-write `[]` as fresh for a whole TTL (the UI
  shows idle, and the assistant's manual retry would start a second sync). Each key
  therefore carries a generation in `_JOBS_GEN`, registered together with the key lock
  before `build()` starts; `invalidate_jobs_cache` bumps it (and leaves the key lock in
  place, so the in-flight build and the next poll stay single-flight), and `cached_jobs`
  stores its result only if the generation is unchanged across `build()`. A racing build
  still answers its own caller with what it saw — it just is not cached, and the next
  poll rebuilds. The guard applies to the degraded path too.
- **Degraded, never 5xx.** When the search raises — an `OpenSearchError` 5xx or a raw
  urllib3 `ReadTimeoutError` (`OpenSearchHttp.request` does not wrap pool errors) — the
  detail still answers **200** with the last cached jobs for that source (or `[]`) and
  `jobs_degraded: true` on the data source and on the detail. The same holds when
  OpenSearch itself cut the search short: the `_jobs` body carries OpenSearch's own
  `timeout: "5s"` (`JOBS_SEARCH_TIMEOUT`), and a busy shard then answers with a
  well-formed `timed_out: true` and whatever hits it had collected — often none. On the
  cached detail path (`_jobs(..., partial_ok=False)`) that raises `JobsSearchTimedOut`,
  so the last **complete** list is kept and the flag set instead of the partial list being
  cached as the truth; the live ingestion-jobs route (`partial_ok=True`, default) keeps
  the partial hits since it has no flag to carry them on. The degraded answer is cached
  for the same TTL, so one slow node answer costs at most one poll; the next window
  re-queries and clears the flag. One WARNING is logged under `launchpad.knowledge`
  (kb, ds, exception type — never a body). Both flags are **additive**: `DataSource.
  jobs_degraded?` / `KnowledgeBaseDetail.jobs_degraded?` in `frontend/src/lib/
  knowledgeBases.ts`; `kbInFlight()` is unchanged and treats a degraded `[]` as "no
  running job", which is the conservative reading for a poller. The console renders the
  per-source flag as a small "job list may be stale" hint beside the sync status
  (`components/KbJobsStaleHint.tsx`, on the V2 `KbDetail`);
  it disables nothing — the sync button still follows the last known job list.
- **The dedicated route is live.** `GET …/data-sources/{ds_id}/ingestion-jobs`
  (`list_ingestion_jobs`, 20 jobs) is **not** cached and still maps a failure to the
  `kb.*` envelope (503 `kb.backend_unavailable`) — a caller that needs the current state
  uses it.

Two load-side changes accompany the cache:

- **Interactive OpenSearch tier.** The self-hosted knowledge and memory backends build
  their funnel client with `attempts=INTERACTIVE_ATTEMPTS` (2) **and**
  `read_timeout=INTERACTIVE_READ_TIMEOUT_SECONDS` (10 s) — see the tier table in
  [memory-backend.md](memory-backend.md#substrate-bootstrap-transient-retry-and-the-post-failure-probe-n-scn-obs6).
  The `_jobs` search body also carries OpenSearch's own `"timeout": "5s"`
  (`JOBS_SEARCH_TIMEOUT`), so a busy shard returns what it has (`timed_out: true`)
  instead of holding the request thread. **Pool size (N17-obs238, `cn/r17-ae`):** the
  funnel's urllib3 pool (`aws_clients._opensearch_pool`) KEEPS `OPENSEARCH_POOL_MAXSIZE`
  = 8 connections per host with `block=False`. urllib3's default keeps one: `block=False`
  never queues (a request that finds no idle connection opens its own, so nothing waits in
  front of the 10 s read timeout), but every connection returning past `maxsize` is closed
  with "Connection pool is full, discarding connection … size: 1" (twice in the 2026-10-21
  forensics while the console polled a KB concurrently — documents list, `/query`, `/sync`,
  the detail's job lookup — alongside four read timeouts and four 502s), and the next
  request pays a fresh TLS handshake against the busy domain. The interactive tier is ONE
  pool shared by the KB and the memory consoles (same session, endpoint, budget, timeout),
  so eight keeps both consoles' concurrent polls on warm connections; past eight the
  warning is back and only costs a handshake. The tier's timeout and retry budget are
  unchanged. Hub only — the Lambda's mirrored `OpenSearchHttp` default pool is not the
  funnel's (`tests/test_opensearch_retry.py::
  test_funnel_pool_keeps_eight_connections_per_host_without_blocking`).
- **Extractor job-status writes.** `backend/lambdas/memory_extractor/handler.py::_job`
  writes the job document with `refresh=False` for the non-terminal statuses
  (`NON_TERMINAL_JOB_STATUSES = {STARTING, IN_PROGRESS}`) and `refresh=True` only for
  the terminal ones (`COMPLETE`/`COMPLETED`/`FAILED`/`SKIPPED`), so the hub's next poll
  sees a finished job promptly while the intermediate write no longer forces a segment
  flush on the node that is bulk-indexing at that moment. The Lambda is a CDK
  `Code.from_asset` of the handler directory, so the next `make bootstrap` redeploys it
  when the handler changes; nothing else needs doing.
- **`INDEXED` means searchable (N-SCN-obs12).** The per-document chunks are bulk-written
  with `refresh=False` (above) while the document's `launchpad-kb-docs` record is written
  with a refresh, so `documents: 2/2 INDEXED` used to be visible before the second
  document's vectors were refreshed into the k-NN index — a retrieve issued 1 s after the
  count flipped ranked only the first document's chunks (round-5 s01, KB `KBEXAMPLE00002`).
  `process_kb_ingest` now issues `POST /<kb index>/_refresh` after each document's bulk
  and **before** its record turns `INDEXED`, and once more before the job turns
  `COMPLETE`; a document that failed or was unsupported forces no refresh. The hub side is
  unchanged: whatever `GET …/documents` reports as `INDEXED` is already retrievable, so
  the scenario's "query right after the count" is now deterministic. Redeploy: the handler
  is a CDK asset — `host-update.sh --with-cdk` / `make bootstrap` (N11-obs55).
- **A transient embedding fault is retried in place (N17-obs120, `cn/r17-t`).** One chunk's
  `embed` failing with a throttle / 5xx (Bedrock `ThrottlingException` /
  `InternalServerException`, an OpenAI-compatible 429 / 503, a timeout) used to mark the
  whole document `FAILED` for good after ONE attempt while the job turned `COMPLETE` with
  `numberOfDocumentsFailed: 1` — the hub's `start_sync` was the only retry, by hand.
  `process_kb_ingest` now classifies the failure with the ONE shared predicate
  (`runtime_invoke.is_upstream_transient`, shipped into the Lambda bundle as a byte-identical
  sibling copy next to `opensearch_http.py` — `tests/test_memory_core_mirror.py` pins it; the
  Lambda cannot import `app.*`) and retries the chunk with the bounded backoff
  `EMBED_RETRY_BACKOFF_S = (1 s, 3 s)` through `Deps.sleep` (injectable; three attempts).
  A fault that outlives the backoff fails the document exactly as before (`FAILED`, reason =
  the exception type, job `COMPLETE`); a permanent shape (`ValidationException` — an unknown
  embedding model) fails it at once, no wait. The memory extractor's own `embed` calls keep
  their job-level `RetryableError` → SQS redelivery (N16-obs32).
- **One document's chunks go to the index in batches (N17-obs245, `cn/r17-aj`).** User report
  2026-10-22 (default, job `ije33be9935ca0ae51`, `KBEXAMPLE00001` / `DSC5A1E57C42`): of 4 objects
  the 81.3 KB HTML FAILED with `ReadTimeoutError: HTTPSConnectionPool(host=…, port=443): Read
  timed out. (read timeout=30.0)` while the domain (t3.small.search ×1) showed IndexingLatency
  ≤ 254 ms and no rejections — `process_kb_ingest` sent ALL chunks of the document, embeddings
  included, in ONE `_bulk` request after the `delete_by_query` on its `doc_uri`, and that one
  request outlived the client's 30 s read timeout on the single HNSW node. Now
  `_bulk_in_batches` writes `BULK_BATCH_CHUNKS = 16` chunks per request (`refresh=False`, the
  delete-then-write order and the single `_refresh` per document unchanged); a transport
  timeout (`urllib3.exceptions.TimeoutError` / `TimeoutError`) on a batch is retried ONCE with
  the same sha1 chunk ids (idempotent), a second timeout raises `IndexWriteTimeout` and the
  document's record reads `status_reason: "index write timed out — run the sync again"` — the
  exception class (and the host-naming transport text) reach the WARNING log line only
  (`kb_ingest job=… document … FAILED (IndexWriteTimeout ← ReadTimeoutError)`); the job's
  other documents still index, the job turns `COMPLETE`. Any other bulk error fails the
  document at once, as before. rev-r22 nits: a batch that fails after earlier batches landed
  triggers a best-effort `delete_by_query` on the `doc_uri` so half a document is never
  searchable; every transport-level error (urllib3 / socket / botocore) is stored as
  `<Class>: transport error while indexing — run the sync again` (`_stored_reason`) — hostnames
  live in the log only; an `OpenSearchError` keeps its host-free body. Other notes: a PDF's
  sidecar is `HeadObject`-ed by the planner (ETag) and `GetObject`-ed once when the document is
  indexed; `"full": true` has no hub route yet (the Lambda accepts it); a memory extraction's
  non-size 400 `ValidationException` is `failure_code: model_validation` — FAILED and redelivered towards the DLQ since N17-obs535 (memory-backend.md). The 30 s request timeout is unchanged (a per-request override
  would touch the byte-mirrored `opensearch_http.py` core). Tests:
  `tests/test_memory_extractor.py::test_a_documents_chunks_are_written_in_batches_of_sixteen`,
  `…::test_a_timed_out_batch_is_retried_once_and_the_document_indexes`,
  `…::test_a_batch_timing_out_twice_fails_only_that_document_with_a_readable_reason`,
  `…::test_a_non_timeout_bulk_error_is_not_retried`.
- **A sync re-embeds only what changed (N17-obs246, `cn/r17-aj`).** User question 2026-10-22:
  every sync re-chunked, re-embedded and rewrote EVERY original object under the prefix
  (`delete_by_query` on the `doc_uri` → bulk), so the 4-file KB spent ≈90 s to retry the one
  document that had failed, every document was briefly unsearchable between its delete and its
  write, and every rewrite was another chance at the obs245 timeout. `process_kb_ingest` now reads
  the document's kb-docs record first (`KB_DOCS_INDEX`, id `{kb_id}:{ds_id}:{sha1(uri)[:16]}`): a
  record that is `INDEXED` with the same `etag` (new field on the record — the S3 listing's ETag,
  bare; for a PDF the text SIDECAR object's ETag, since the sidecar is what is indexed) and
  `size_bytes` as the listing is skipped — no read, no embedding, no delete, no write — and counted
  in the new job statistic `numberOfDocumentsUnchanged` (always present; `Scanned` = every original
  listed, `NewDocumentsIndexed` = rewritten this sync, `Failed` as before). A record without an
  `etag` (written before this change) is treated as changed once and gains it; `FAILED` /
  `UNSUPPORTED` records are never "unchanged", so the next sync retries them (what "run the sync
  again" promises). Records whose S3 object is no longer under the prefix are swept — chunks by
  `doc_uri`, then the record — and reported as `numberOfDocumentsDeleted` (only when > 0). **The
  sweep does not trust the job-start listing alone (N17-obs255, rev-r22):** a concurrent or
  duplicate sync of the same data source (`start_sync` has no running-job check;
  `covers_upload: false` asks for a manual sync; a `JobsSearchTimedOut` enqueues a duplicate) may
  index a document this listing never saw, so a candidate is swept only when its record was not
  written after this job listed (`indexed_at` ≤ the job's `listed_at`) AND `HeadObject` on its key
  says the object is gone (404 / NoSuchKey / NotFound); a present object is kept with one INFO
  line. Tests: `tests/test_memory_extractor.py::
  test_a_sync_with_a_stale_listing_does_not_sweep_a_document_another_job_just_indexed`,
  `…::test_a_record_indexed_after_this_jobs_listing_is_never_swept`. The
  message flag `"full": true` forces the former complete rewrite; the hub does not send it yet and
  this branch adds no route or UI for it (since N17-obs272: `POST …/sync {"full": true}` and the
  console's 「全量重建」, below). The console shows the new statistic through its generic
  stat-key rendering (`knowledgeBases.ts` labels AWS stat keys from their camel case); no
  frontend change was made here (since N17-obs271 the console labels the keys itself, below). Tests:
  `tests/test_memory_extractor.py::test_a_second_sync_of_an_unchanged_prefix_embeds_nothing_and_counts_unchanged`,
  `…::test_only_the_changed_document_is_re_indexed`,
  `…::test_a_record_without_an_etag_is_re_indexed_once_then_skipped`,
  `…::test_full_true_forces_the_complete_rewrite`,
  `…::test_a_failed_or_unsupported_document_is_tried_again_on_the_next_sync`,
  `…::test_a_deleted_object_leaves_the_index_with_the_next_sync`.
- **The sync message plans; one message per changed document does the work (N17-obs247,
  `cn/r17-aj`).** User question 2026-10-22: one `kb_ingest` invocation handled EVERY document
  of a sync and embedded chunk by chunk (≈0.5 s each with the writes — 4 documents = 92 s live)
  under the Lambda's 120 s timeout (`infra/stacks/memory_stack.py`, visibility 720 s,
  `max_receive_count` 3), so a sync of ≳150–200 chunks (one large PDF) timed out, was retried
  three times and went to the DLQ with the job left non-terminal. Now `process_kb_ingest` is the
  PLANNER: it lists the prefix, decides changed / unchanged (obs246; a PDF's sidecar ETag by
  `HeadObject`), sweeps deleted documents, writes the job `IN_PROGRESS` with `fanout.expected`
  and `planner_counts` (scanned / unchanged / deleted) BEFORE the first message leaves, then
  enqueues one `kb_ingest_doc` message per changed document on the extractor's own queue
  (`LAUNCHPAD_MEMORY_QUEUE_URL`; the role's new `sqs:SendMessage` on that queue only); a send
  that fails is indexed inline. `process_kb_ingest_doc` embeds + writes ONE document
  (`_index_document`: read, chunk, embed on `EMBED_WORKERS = 4` threads in order — the
  embedders take one text per call, so bounded concurrency rather than list input — then
  `delete_by_query` → obs245 batches → `_refresh` → record) and writes a `kb_ingest_doc_result`
  row (`{job_id}:{doc_id}`, `refresh=true`; a duplicate delivery finds it and does nothing);
  the job turns COMPLETE when the result rows reach `fanout.expected`: `_maybe_complete_job`
  recomputes `counts` = `planner_counts` + the result tallies from the same inputs whoever runs
  it last (the last document, or the planner after its final write), so there is no
  read-merge-write race on the job and a document finishing before the planner's final write
  cannot leave stale totals. `UNSUPPORTED` counts as failed, as before. Without a queue URL
  (older stack, tests) the planner indexes inline and completes the job itself. **Planner runs are
  generations (N17-obs266, rev-r22):** every planner run stamps `fanout.run` (12 hex) into the
  job, into each `kb_ingest_doc` message and into its result row (`{job}:{run}:{doc}`); the
  completion check counts only rows of the job's current run, a document message whose `run` is
  not the job's current one is acked as `STALE` (no work, no row), and a planner redelivered for a
  job already `COMPLETE` re-plans nothing — so a redelivered planner (SQS at-least-once, or a
  raise after the fan-out such as the final write / `_refresh` failing) re-plans from the kb-docs
  records (documents the first run indexed are now unchanged), re-dispatches what is still
  missing, and can neither complete early against the old rows nor count a document twice. The
  document stage also partial-updates the job's `updated_at` after every result row, which is the
  progress the hub's stalled rule reads: an `IN_PROGRESS` job silent for `RUNNING_JOB_STALE_S`
  (30 min) is presented and persisted `FAILED` / `stalled: no progress for 30 min — run the sync
  again` next to the never-dispatched rule (`knowledge_selfhosted._reap_never_dispatched`). Model
  resolution (`embedder_for`, the memory extractor's `chat_model_for`) now happens inside the job
  bookkeeping: an Identity / Secrets failure marks the job `FAILED` / `failure_code:
  model_resolution` (a document message records its document FAILED and still counts); an
  OpenAI-compatible provider's 4xx other than 408 / 429 is permanent (`model_validation`; a
  `memory_extract` message is FAILED + redelivered towards the DLQ since N17-obs535, memory-backend.md)
  like the Bedrock `ValidationException`, only 408 / 429 / 5xx are redelivered. The per-document
  time guard stays deferred. The hub reads
  jobs by `kind: kb_ingest`, so result rows never appear as jobs; they carry `ds_id` and are swept
  with the data source. The event source is now `batch_size=1` (one document's work per
  invocation; Lambda concurrency, not the batch, is the parallelism), capped by the event
  source's `MaximumConcurrency` — CDK context knob `extract_max_concurrency`, default 5, the
  service minimum 2 enforced at synth (rev-r22) — so a large fan-out launches at most that many
  invocations against the single t3.small node and takes at most that much of the account's
  Lambda concurrency. An OpenAI-compatible embedding / extraction provider's 401 / 403
  re-resolves the credential once (`refresh_bearer`: the cached bearer dropped, the client's
  header refreshed in place) and retries the call once — a key rotated in Identity heals
  itself; a second refusal is `model_validation`. A per-document time
  guard (slicing one huge document across continuation messages) was NOT built here: with four
  embedding threads the 400-chunk cap is ≈50 s of embeddings, under the 120 s timeout (built
  since by N17-obs270, the next bullet). Tests:
  `tests/test_memory_extractor.py::test_a_sync_fans_out_one_message_per_changed_document_and_completes_on_the_last`,
  `…::test_a_failed_document_in_the_fan_out_still_completes_the_job_with_failed_1`,
  `…::test_a_duplicate_document_message_is_idempotent`, `…::test_unchanged_documents_are_never_enqueued`,
  `…::test_a_document_whose_enqueue_fails_is_indexed_inline`,
  `…::test_the_planner_completes_the_job_itself_when_every_result_already_reported`,
  `…::test_chunk_embeddings_run_on_a_bounded_pool_and_keep_their_order`,
  `…::test_lambda_handler_drives_the_planner_and_the_document_messages`,
  `…::test_a_document_message_for_a_vanished_job_or_kb_is_dropped`;
  `infra/tests/test_memory_stack.py::test_extractor_may_send_to_its_own_queue_and_knows_its_url`.
- **One huge document is indexed in slices (N17-obs270, `cn/r17-ar`).** User approval
  2026-10-22 of obs247's deferred item 3: a single document of up to 2 MB text / `MAX_CHUNKS`
  (400) chunks no longer has to finish inside one 120 s invocation. `process_kb_ingest_doc` works
  in SLICES (`_index_slice`): at most `SLICE_CHUNKS` = 64 chunks per invocation, the embedding
  cut earlier at a `BULK_BATCH_CHUNKS` (16) boundary once `SLICE_BUDGET_S` = 70 s have passed
  since the message was picked up (`Deps.clock`, monotonic) — the margin to 120 s covers the
  writes of the last embedded chunks and the continuation send; whichever limit comes first ends
  the slice. When chunks remain, the stage enqueues a continuation `kb_ingest_doc` message (the
  same body — job, `run`, document — plus `slice_start` and, as `document.etag`, the ETag of the
  text slice 0 read: the object's, a PDF's text sidecar's), partial-updates the job's
  `updated_at` (progress for the stalled rule) and returns `CONTINUE`; a failed send raises
  `RetryableError`, so THIS slice is redelivered. Slice 0 alone does the document's
  delete-then-write start (its own chunks embedded first, then `delete_by_query` on the
  `doc_uri` once); chunk ids stay `sha1(uri#i)`, so a redelivered slice (a crash or timeout
  mid-slice) rewrites the same ids. The kb-docs record turns `INDEXED`, the per-document
  `_refresh` runs and the `kb_ingest_doc_result` row (completion counting, obs266 generations)
  is written only after the LAST slice. A continuation passes the same gates as any document
  message: its result row present → `DUPLICATE`; job gone / index gone → dropped; a `run` that
  is not the job's current one → acked `STALE` (no work, no row). A continuation that reads
  another ETag (or finds the object / sidecar gone) was replaced mid-ingest: it writes nothing,
  returns `SUPERSEDED` (a result row counted as failed, so the job still completes) and records
  the document `FAILED` / `replaced in S3 while this sync was indexing it — the next sync indexes
  the new version`, which makes the next sync treat it as changed; it does not touch a record a
  newer sync already wrote `INDEXED` for the current ETag. (Since N17-obs274 it also removes the
  chunks THIS sync wrote, by their `ingest` stamp, and slice 0 deletes only other syncs' chunks
  — see the obs274 bullet for the stamps, the lease and the budget arithmetic.) The
  obs255 sweep rules are untouched (the record is written only at the end, `indexed_at` = then;
  a document mid-ingest is present in S3 and kept). Without a queue (the planner's inline path,
  older stacks) a document is still indexed whole in one call. Lambda timeout and event-source
  settings are unchanged. Tests:
  `tests/test_memory_extractor.py::test_a_300_chunk_document_is_indexed_in_five_slices_and_completes_once`,
  `…::test_a_slice_is_cut_early_when_the_time_budget_runs_out`,
  `…::test_a_crash_mid_slice_is_redone_idempotently_on_redelivery`,
  `…::test_a_continuation_that_cannot_be_enqueued_redelivers_its_slice`,
  `…::test_a_continuation_of_a_superseded_planner_run_is_acked_stale`,
  `…::test_a_document_replaced_mid_ingest_stops_and_the_next_sync_takes_it`,
  `…::test_a_replaced_document_already_indexed_by_a_newer_sync_keeps_its_record`,
  `…::test_a_document_deleted_mid_ingest_stops_as_superseded`.
- **The console labels the ingestion statistics (N17-obs271, `cn/r17-ar`).** User approval
  2026-10-22: the ingestion-jobs table showed `numberOfDocumentsUnchanged` (obs246) as the
  humanized raw key "Documents Unchanged" in both languages. `lib/knowledgeBases.ts` now owns the
  labels of the five keys the self-hosted backend writes (`STAT_LABEL_KEYS` → `v2.knowledge.stat*`:
  `numberOfDocumentsScanned` 「扫描」 / Scanned, `numberOfNewDocumentsIndexed` 「新索引」 / Newly
  indexed, `numberOfDocumentsUnchanged` 「未变化」 / Unchanged, `numberOfDocumentsFailed` 「失败」 /
  Failed, `numberOfDocumentsDeleted` 「已移除」 / Removed — the last only when the job reports it)
  and their display order (`STAT_ORDER`; any other numeric key follows in server order, labelled by
  `humanizeStat`). Unchanged and Removed carry a hover title saying what they mean. The jobs table
  uses these labels; the data-source row's latest-ingestion cell adds the one-line summary
  (`jobStatsSummary`), e.g. 「扫描 4 · 新索引 1 · 未变化 3 · 失败 0」 / "Scanned 4 · Newly indexed 1 ·
  Unchanged 3 · Failed 0" (an empty-prefix job keeps its N17-obs244 hint instead). No API change.
  Tests: `frontend/src/lib/knowledgeBases.stats.test.ts`,
  `frontend/src/v2/pages/knowledge/KbDetail.test.tsx` ("ingestion statistics labels (N17-obs271)").
- **A forced full re-sync entry (N17-obs272, `cn/r17-ar`).** User approval 2026-10-22: obs246
  made every sync incremental and accepted `"full": true` on the planner message, but nothing
  could send it. `POST /api/knowledge-bases/{kb}/data-sources/{ds}/sync` now takes an optional
  JSON body `SyncRequest {full: bool = false}` (`extra="forbid"`; no body, `{}` and
  `{"full": false}` are the incremental sync); `start_sync(…, full=True)` →
  `_enqueue_sync(…, full=True)` adds `"full": true` to the planner message and nothing else (the
  default message is byte-identical to before — no `full` key), and the Lambda re-chunks and
  re-embeds every document. Permission: the incremental sync stays any viewer's of the KB
  (`_require_visible`; a hidden KB is 404 `kb.not_found` for both); the full rebuild is the
  creator's or an administrator's since N17-obs530 (`_require_owner`, 403 `auth.forbidden
  {kb_id, action: "full_rebuild", created_by, created_by_name}`) — see "Mutations are the
  creator's" below. Console: each data-source row carries a secondary
  「全量重建」 / FULL REBUILD action next to 「立即同步」, enabled exactly when the sync is (source
  AVAILABLE, no job running, no sync starting), behind a confirm dialog — 「全量重建会重新切片并生成所有
  文档的向量，耗时更长；仅在检索结果异常时使用。」 / "A full rebuild re-chunks and re-embeds every
  document, which takes longer; use it only when retrieval results look wrong." — whose
  confirmation calls `v2KnowledgeApi.sync(kb, ds, {full: true})` (body `{"full": true}`; the plain
  sync still sends no body). Tests: `tests/test_knowledge_selfhosted.py::
  test_sync_route_passes_full_true_through_to_the_planner_message`,
  `…::test_the_default_sync_omits_full`, `…::test_the_sync_body_is_validated`,
  `…::test_start_sync_full_marks_the_message_and_nothing_else`; `tests/test_visibility.py::
  test_isolated_knowledge_bases` (the `{full: true}` POST refused like the plain one);
  `frontend/src/lib/knowledge-sync-api.test.ts`; `frontend/src/v2/pages/knowledge/KbDetail.test.tsx`
  ("full rebuild (N17-obs272)").
- **The job's `updated_at` bump tolerates concurrent documents (N17-obs273, `cn/r17-ar`).**
  deploy-ops-35, live on t1 2026-10-22: in a 3-document fan-out, 2 of the 3 concurrent
  `kb_ingest_doc` invocations logged `outcome=retry` / "result bookkeeping unavailable after the
  document: OpenSearchError". Every document stage partial-updates the SAME job document
  (rev-r22 (b)'s liveness bump), and concurrent `_update`s fail OpenSearch's version check with a
  409. The result rows were already written, so the job completed. The messages were still
  redelivered after the 720 s visibility timeout and acked `DUPLICATE`: wasted invocations, a
  12-minute in-flight message and spurious WARNINGs, and a larger fan-out could reach
  `maxReceiveCount` 3 and the DLQ. The bump is now `_touch_job`: `POST /<jobs>/_update/<job>`
  with `retry_on_conflict=5` (`JOB_TOUCH_RETRY_ON_CONFLICT`, sent through `request` so the
  byte-mirrored `opensearch_http` core is unchanged), and it never raises. A residual 409, or a
  404 for a gone job, is one INFO line; any other error is one WARNING. It is best-effort
  liveness, the result row is the truth, and it is never a message retry. The obs270
  continuation path uses the same tolerant bump AFTER the continuation is sent, so a bump failure
  cannot start a second continuation chain. The per-message log line now names the job of a
  `kb_ingest_doc` message too (`job=` was empty). Tests: `tests/test_memory_extractor.py::
  test_a_conflicting_job_bump_never_turns_a_finished_document_into_a_retry`,
  `…::test_the_job_bump_passes_retry_on_conflict_and_still_lands`,
  `…::test_a_conflicting_bump_on_a_continuation_still_continues_exactly_once`,
  `…::test_any_other_bump_failure_is_logged_not_retried`.
- **Twin chains and concurrent syncs keep every chunk (N17-obs274, `cn/r17-ar`, rev-r22 BLOCKING
  on obs270).** rev-r22's repro: chain A ran slices 0, 64 and 128. Slice 0 was then redelivered as
  chain B, through an SQS duplicate or a crash after the continuation send. B deleted EVERY chunk
  of the document (term `doc_uri`) and rewrote 0..63. A finished with INDEXED and a result row,
  and B's 64 was a DUPLICATE. The result was an INDEXED record with `chunk_count` 300 but only 172
  chunks, and the next incremental sync skipped it (same ETag) — permanently. The fix:
  - **Stamps.** Every chunk carries `ingest` = `"<job>:<run>"`, the sync that wrote it, mapped as
    a keyword. The Lambda adds the mapping with an additive `PUT _mapping` once per index per
    container (`_ensure_ingest_mapping`), because the KB index body lives in the byte-mirrored
    core. Indices created before this change gain it the same way.
  - **Stamped ids.** A chunk's id is `sha1(uri#i#stamp)`. Redeliveries and twin chains of one
    sync rewrite the same ids; two syncs never overwrite each other's chunks.
  - **Conditional deletes.** Slice 0 deletes only this document's chunks NOT stamped by its own
    sync; legacy unstamped chunks count as "not own" and go as before. The last slice repeats that
    delete before its refresh, so chunks another sync wrote meanwhile leave with it.
  - **Lease.** rev-r22's proposal (stamps + conditional deletes) alone does not cover two
    DIFFERENT syncs of one document running at once — a double-clicked sync, an upload's sync plus
    a manual one; `start_sync` has no running-job check. Each would delete the other's chunks, and
    the last finisher could record INDEXED with holes. So every slice 0 takes a per-document lease
    in the jobs index: `lease:<doc_id>`, kind `kb_ingest_doc_lease`, `owner` = the stamp, carrying
    `kb_id` / `ds_id`, swept by the KB-delete and (since N17-obs276) the data-source-delete
    cleanup. A later slice whose chain lost the lease,
    checked at slice start and again before the last slice's delete and record, stops with
    `SUPERSEDED`. It removes its own chunks and leaves the record to the owner, and its job counts
    it as failed and completes. Of two concurrent syncs, the newer-started one finishes with every
    chunk. Twins share the stamp, so they never stop or delete each other. A failing slice of a
    chain that lost the lease removes only its own chunks and writes no FAILED record.
  - **Superseded cleanup.** A chain stopped by an ETag change (or a missing object) now deletes
    the chunks carrying ITS stamp, so no partial old version stays searchable while the record says
    FAILED. Other syncs' chunks are left alone. (Since N17-obs277 a missing object writes no
    record at all; see that bullet.)
  - **Time budget.** The embedding runs in groups of `EMBED_WORKERS` (4). BEFORE each group after
    the first, the stage projects `elapsed + slowest group so far` and stops when that would pass
    `SLICE_BUDGET_S` (70 s). `SLICE_RESERVE_S` (50 s; 70 + 50 = the 120 s timeout) covers what
    follows: ≤ 4 bulk batches, the last slice's refresh and record, the continuation send and the
    job bump. Residual risk, documented in the constants: the first group of a slice has no
    history, and one round of 4 calls can itself take up to `EMBED_TIMEOUT_S` × 3 attempts plus
    backoff. A slice that still times out is redelivered and redone idempotently.

  Residual race: an old owner that checked its lease just before a newer slice 0 took it, and
  then stalled longer than that slice's embedding before its last-slice delete, could remove
  the newer sync's first chunks. Since pass 2 (below), the newer sync's completeness check
  records such a document FAILED, so the next sync repairs it. Tests: `tests/test_memory_extractor.py::
  test_a_late_duplicate_of_slice_0_never_deletes_its_own_chains_chunks` (rev-r22's repro),
  `…::test_a_newer_sync_takes_the_document_and_the_older_chain_stops`,
  `…::test_an_older_slice_writing_after_the_newer_slice_0_leaves_no_trace`,
  `…::test_the_last_slice_removes_another_syncs_chunks_written_meanwhile`,
  `…::test_slice_0_removes_legacy_unstamped_chunks`,
  `…::test_the_kb_index_gains_the_ingest_keyword_before_the_first_stamped_chunk`,
  `…::test_a_slice_is_cut_before_the_group_that_would_pass_the_budget`,
  `…::test_a_document_replaced_mid_ingest_stops_and_the_next_sync_takes_it` (no chunk left).
  **Pass 2 (rev-r22, 2026-10-22).** rev-r22 verified the stamped ids and the lease (no holes
  for the winner of a takeover). It found one remaining hole, in the failure cleanup: the
  `except` branch treated every slice 0 as the owner and deleted every chunk of the `doc_uri`.
  Repro: during X's first bulk, Y's slice 0 runs in full; X's batch then times out twice, and X
  deleted Y's 64 chunks and wrote FAILED. Y drained to INDEXED with 236 of 300 chunks, which
  stayed until a full rebuild. Now:
  - **Failure cleanup.** Ownership is always read from the lease, for slice 0 too, and an
    unreadable lease means NOT owner. The cleanup always deletes this sync's own stamped chunks;
    the other-stamp / legacy delete and the FAILED record happen only while still owner.
    Otherwise the slice returns `SUPERSEDED`.
  - **Completeness check.** The last slice counts this sync's chunks of the document
    (`POST /<index>/_count`) after its refresh. A count other than the document's chunk count is
    recorded FAILED / `incomplete index write — run the sync again`
    (`INCOMPLETE_WRITE_REASON`), so the next incremental sync re-indexes it instead of skipping
    an INDEXED record with holes. This also closes the residual race documented above.
  - **Mapping.** An existing `ingest` field mapped as a type other than keyword is a WARNING,
    with no PUT.

  Tests: `…::test_a_failing_slice_0_never_deletes_a_newer_owners_chunks` (the repro),
  `…::test_an_unreadable_lease_on_failure_touches_only_this_syncs_chunks`,
  `…::test_a_last_slice_with_a_missing_chunk_is_recorded_failed_not_indexed`,
  `…::test_an_ingest_field_of_another_type_is_warned_about_not_remapped`.
  **Pass 3 (rev-r22, 2026-10-22).** rev-r22's repro: a document is INDEXED with 300 chunks and a
  full rebuild runs its slice 0, which deletes them as not its own. Slice 64 then met two
  consecutive lease-GET errors: the top read and the cleanup's read. The slice stopped as
  SUPERSEDED with no record, so the index held 0 chunks under the old INDEXED record (same ETag),
  and the next incremental sync skipped it for good. Now:
  - **Top of the slice.** The lease is written (slice 0) or read (slice > 0) BEFORE the slice's
    work. An error there is not a lost lease: `RetryableError`, nothing written, the slice
    redelivered.
  - **Failure cleanup, three outcomes.** A lease read that names this sync: owner, everything of
    the document deleted, FAILED record. A read that names another sync: own chunks only, no
    record. An unreadable lease: own chunks only, but the FAILED record IS written. This is
    conservative: an owner finishing later overwrites it with INDEXED, and one that already
    finished gets FAILED, which the next sync redoes.
  - **Count mismatch.** The last slice's mismatch branch also deletes its own stamped chunks
    before writing FAILED.

  Tests: `…::test_a_lease_read_error_before_any_work_redelivers_the_slice`,
  `…::test_an_unreadable_lease_after_a_failed_slice_writes_failed_and_the_next_sync_repairs`,
  `…::test_an_unreadable_lease_on_failure_touches_only_this_syncs_chunks` (now FAILED + record),
  `…::test_a_last_slice_with_a_missing_chunk_is_recorded_failed_not_indexed` (no chunk left).
  **Pass 4 (rev-r22, 2026-10-22, the last path it could construct).** The problem: a chain that
  dies after its slice 0 left the old INDEXED record over a partial index. Slice 0 deletes every
  other stamp's chunks and writes only its own slice; the record was written only by the last
  slice or the `except` branch. A later slice that never reaches either — 3 RetryableErrors in a
  row (lease unavailable during an incident, a failing continuation send) or 3 timeouts, then the
  DLQ; the stalled reaper fails the job but never the kb-docs record — left the previous INDEXED
  record (same ETag) standing. Repro: S1 INDEXED 300; a full rebuild's slice 0 writes 0..63;
  slice 64 goes to the DLQ. The record said INDEXED with chunk_count 300, the index held 64, and
  the next incremental sync counted the document unchanged — permanently. Now:
  - **Slice 0 demotes the record.** Right before its `_not_stamped` delete, slice 0 writes the
    kb-docs record as `INDEXING`, with the new stamp in `ingest`, `indexing_started_at`,
    chunk_count 0 and the size / ETag of what it read. The last slice flips it to INDEXED after
    the count check; the INDEXED record now also carries `ingest`.
  - **Healing.** `_unchanged` treats everything but INDEXED (INDEXING included) as changed, so
    a dead chain heals on the next sync.
  - **Late twins.** A late twin of slice 0 skips the demotion when the record is already INDEXED
    with its own stamp, so it never demotes its own chain's finished document.
  - **Hub listing.** `list_documents` presents an `INDEXING` record older than
    `RUNNING_JOB_STALE_S` as `FAILED` / `interrupted — run the sync again`
    (`INDEXING_INTERRUPTED_REASON`). This is presentation only; the stored record is unchanged.
    No hub count reads INDEXED, and the obs244 empty-KB logic reads `has_objects` / jobs, so
    neither changes.
  - **Console.** The documents table labels INDEXING 「索引中」 / "Indexing" (`ResourceTag`
    `label`); other statuses stay raw.

  Tests: `tests/test_memory_extractor.py::
  test_a_chain_that_dies_after_slice_0_leaves_indexing_and_the_next_sync_heals_it` (the repro),
  `…::test_the_record_reads_indexing_between_slice_0_and_the_last_slice`,
  `…::test_a_late_twin_of_slice_0_does_not_demote_its_own_syncs_indexed_record`;
  `tests/test_knowledge_selfhosted.py::test_a_stale_indexing_document_is_listed_failed_interrupted`;
  `frontend/src/v2/pages/knowledge/KbDocuments.test.tsx`.
- **Deleting a data source sweeps its extractor rows (N17-obs276, `cn/r17-ar`).** The
  obs247 bullet says result rows "carry `ds_id` and are swept with the data source", but the
  data-source cleanup (`kb_cleanup` scope `data_source`, step `docs`) deleted only the kb-docs
  records. The jobs-index rows the extractor writes per document — `kb_ingest_doc_result`
  (obs247 / obs266) and, since obs274, `kb_ingest_doc_lease` — stayed until the whole KB was
  deleted. The step now also runs `delete_by_query` on the jobs index filtered by `kb_id` +
  `ds_id` + `kind` ∈ `EXTRACTOR_DOC_ROW_KINDS`. It is retried with the step like the kb-docs
  delete (a pending step goes to the `kb_cleanup` job). The source's ingestion jobs
  (`kind: kb_ingest`) are untouched, as before. Tests: `tests/test_knowledge_selfhosted.py::
  test_deleting_a_data_source_sweeps_its_result_and_lease_rows`,
  `…::test_the_swept_row_kinds_are_the_extractors_own` (the hub's names pinned to the Lambda's
  constants).
- **A full rebuild never starts beside a running job of its source (N17-obs275, `cn/r17-ar`,
  rev-r22).** `POST …/sync {"full": true}` had no in-flight guard. Repeated clicks, or an API
  caller past the confirm dialog, started N concurrent full jobs, each re-embedding the whole
  source. `start_sync(…, full=True)` now asks `_running_job` first: the newest job of the data
  source in STARTING / IN_PROGRESS / RUNNING that is not older than `RUNNING_JOB_STALE_S`, read
  from a live, complete search. When one exists, it answers 409 `kb.sync_in_progress` with
  `detail: {job_id}` and nothing is written or enqueued. A timed-out jobs search
  (`JobsSearchTimedOut`) cannot rule a running job out, so it is refused too, with
  `job_id: null`; a rebuild is never urgent. The incremental sync keeps today's behaviour (no
  check), and the permission stays sync parity (visibility). The console turns the 409 into
  「已有 ingestion 任务在运行。」 / "An ingestion job is already running.", closes the dialog and
  refreshes the detail. Tests: `tests/test_knowledge_selfhosted.py::
  test_a_full_rebuild_is_refused_while_a_job_of_the_source_runs`,
  `…::test_a_stale_running_job_does_not_block_a_full_rebuild`,
  `…::test_a_full_rebuild_is_refused_when_the_running_check_times_out`,
  `…::test_the_sync_route_answers_409_sync_in_progress_for_a_second_full_rebuild`;
  `frontend/src/v2/pages/knowledge/KbDetail.test.tsx` ("says a sync is already running …").
- **A chain that lost the lease never writes INDEXED, single-slice documents included
  (N17-obs278, `cn/r17-lp`, rev-r22).** The last slice's lease re-check ran only for
  `slice_start > 0`. Slice 0 of a single-slice document skipped it, so an older chain that lost
  the lease mid-write deleted the newer sync's chunks and recorded INDEXED under its own stamp.
  The outcome stayed consistent, but the wrong sync won. Now:
  - **Every last slice re-checks the lease** before its `_not_stamped` delete and its record, at
    the cost of one extra GET. A lost lease means `SUPERSEDED`: own chunks removed, no record.
  - **Slice 0 also re-checks after embedding**, right before its INDEXING demotion and its
    delete. Without this, an older chain that lost the lease while embedding would demote the
    newer sync's INDEXED record and delete its chunks before stopping at the last-slice check.
    Nothing of the chain is written at that point. A lease that cannot be read there is
    `LeaseUnreadable` (a `RetryableError`): the slice is redelivered and no FAILED record is
    written, matching the pass-3 rule for the pre-work read.

  Tests: `tests/test_memory_extractor.py::
  test_a_single_slice_chain_losing_the_lease_mid_write_stops_superseded`,
  `…::test_a_single_slice_chain_losing_the_lease_while_embedding_writes_nothing`,
  `…::test_an_unreadable_lease_at_the_slice_0_recheck_redelivers_with_nothing_written`.
- **A data source deleted mid-slice leaves no orphans (N17-obs277, `cn/r17-lp`, rev-r22).**
  A last slice that read its text before a data-source delete could write its chunks and its
  INDEXED record after the delete's `index` / `docs` sweeps. That left orphan chunks plus an
  INDEXED kb-docs row of a source that no longer exists. Now, before any record write that
  follows the last slice's count check, the Lambda calls `_source_gone`: one `HeadObject` of the
  document's original key. This applies to the INDEXED write and to the count-mismatch FAILED
  write. When the key is missing, `_drop_gone_document` deletes this sync's stamped chunks and
  removes the kb-docs record and the lease row only if they name this sync's stamp. It writes
  no record and returns `SUPERSEDED`. The job counts the document as failed and completes.
  - **Why the object.** The delete runs `s3` → `index` → `docs`, so the S3 purge is the first
    cleanup step and the object is the earliest trace to disappear. The lease row goes only
    with the last step, after the chunk sweep. A missing lease also means "an older Lambda"
    to `_holds_lease`. The KB delete purges `kb/<kb_id>/` first, too.
  - **Why it closes the window.** An INDEXED write that passes the check came before the
    purge, so the later `index` / `docs` steps remove what it wrote. The residual is one
    `put_doc` between the HEAD and the write against the whole three-step delete.
  - **Cannot tell.** Only the 404 family counts as gone. Any other HEAD error is logged and the
    record is written; the delete's later steps still sweep it.
  - **Same rule on the other paths.** A missing object on ANY slice, confirmed by the same
    HEAD, now drops the document this way. A GetObject `NoSuchKey` that the HEAD contradicts
    keeps the old outcome. Before, slice 0 wrote a FAILED record and a later slice wrote FAILED
    `SUPERSEDED_REASON`. A text-less read whose original is gone also drops it: a PDF whose
    sidecar went with the source used to be recorded UNSUPPORTED. A PDF without a sidecar
    whose original exists stays UNSUPPORTED.
  - **What stays.** The document's `kb_ingest_doc_result` row is still written, because the
    job's completion counts it. Like the `kb_ingest` job row the data-source delete keeps, it
    goes with the KB delete.

  Tests: `tests/test_memory_extractor.py::
  test_a_data_source_deleted_while_the_last_slice_writes_leaves_no_orphans` (the repro),
  `…::test_a_data_source_deleted_after_the_count_check_gets_no_indexed_record`,
  `…::test_the_source_check_runs_once_right_before_the_indexed_write`,
  `…::test_a_source_check_that_cannot_tell_still_records_indexed`,
  `…::test_a_document_deleted_before_slice_0_writes_no_record`,
  `…::test_a_data_source_deleted_between_slices_leaves_no_record`,
  `…::test_a_missing_pdf_sidecar_of_a_deleted_source_writes_no_unsupported_record`,
  `…::test_a_pdf_without_a_sidecar_whose_original_exists_is_still_unsupported`,
  `…::test_a_document_deleted_mid_ingest_stops_as_superseded` (now: no record),
  `…::test_a_no_such_key_the_head_contradicts_is_not_a_gone_document`;
  `tests/graph/test_graph_fault_probes_storage.py::
  test_kb_ingest_one_unreadable_object_fails_that_document_only` (unchanged).
- **The fan-out finalise writes COMPLETE once (N17-obs279, `cn/r17-lp`).** deploy-ops-36 saw this
  live on t1, 2026-10-22: a full rebuild's "COMPLETE after fan-out" was written twice by two
  document invocations. Both saw the last result row, so it was a benign double finalise with
  the right counts. `_maybe_complete_job`'s read-check (already COMPLETE with these counts →
  done) cannot settle two finalisers that both read IN_PROGRESS. The terminal write is now
  `_complete_job_once`:
  - **The write.** `POST /<jobs>/_update/<job>` with a painless script
    (`_COMPLETE_ONCE_SCRIPT`), sent through `request` so the byte-mirrored core is unchanged.
    The script sets `ctx.op = 'noop'` when the job already says COMPLETE with exactly these
    `counts`, else `putAll` of the fields the old `_job_soft` wrote (status, counts,
    `failure_reason: null`, `fanout` with `reported`, `updated_at`). It runs with
    `refresh=true` and `retry_on_conflict=5`. A re-run after a version clash sees the first
    finaliser's write and becomes a `noop`.
  - **Logging.** The finaliser that writes logs the INFO line. A `noop` logs it at DEBUG with
    "(already written by another finaliser)". Counts are unchanged: both compute them from the
    same inputs.
  - **Repair kept.** A COMPLETE job with OTHER counts is still overwritten, so the planner's
    recompute keeps repairing totals a document computed from the preliminary half. A FAILED
    job is overwritten as before: a finaliser that sees every expected result completes it.
    The guard is deliberately "COMPLETE with these counts", not "any terminal status".
  - **Errors.** A residual 409 is retried client-side (`COMPLETE_ONCE_ATTEMPTS` = 3). Any other
    error is one WARNING and never raises, matching the `_job_soft` contract. Unlike the
    read-merge-write it replaces, a job deleted meanwhile is not recreated (404).

  Ops half of the observation, outside the repo: deploy-ops scripts on a host must load
  `/etc/launchpad/env`, or they resolve the `default` environment's index names. The lead
  recorded that rule. Tests: `tests/test_memory_extractor.py::
  test_two_concurrent_finalisers_write_complete_once`,
  `…::test_the_complete_write_sends_the_guard_script_with_a_refresh`,
  `…::test_a_complete_job_with_other_counts_is_still_repaired`,
  `…::test_a_residual_conflict_on_the_complete_write_is_retried_client_side`,
  `…::test_the_complete_write_never_recreates_a_gone_job`; the obs273 conflict helper now
  conflicts only the `updated_at` bumps.

## Upload starts the sync; `has_objects`; `prefix_empty` (N17-obs204, `cn/r17-aa`)

A fresh KB's first upload used to show *success* while its first ingestion job reported
`Documents Scanned 0`, and only a second upload of the same file indexed it (user report
2026-10-21, `KBEXAMPLE00003`). The console fired the first sync on its own schedule — the
detail page started one as soon as a data source was `AVAILABLE` with no jobs, whether or
not the files had landed — while its upload action started none, so a sync that listed the
prefix before the puts finished was the only sync there ever was. Ruling (2026-10-21): a
PRODUCT defect, fixed on three sides, all hermetic:

- **A successful upload starts one ingestion sync; running jobs are coalesced.**
  `upload_files` puts every file (and PDF sidecar), then — and only then — runs the same
  code path as `start_sync` (`_enqueue_sync`: the `STARTING` job document, the cache
  invalidation, the SQS message) for the upload-mode data source, so the Lambda lists the
  prefix with the files already in it. Before enqueueing it looks (live, uncached) at the
  source's newest jobs: one still in `RUNNING_JOB_STATUSES` (`STARTING`, `IN_PROGRESS`;
  `RUNNING` reserved) is returned instead of a second one being enqueued — the Lambda scans
  the whole prefix anyway, and two concurrent ingests would race on the same document
  records. The response's `job` is `{job_id, status, coalesced, covers_upload}`: a fresh
  job is `coalesced: false, covers_upload: true`; a coalesced job is `covers_upload: true`
  only when its `created_at` is **at or after the hub timestamp taken after the LAST put landed (rev-r19: a job created between two puts may have listed the prefix before the later files arrived)** (both from the hub clock — the Lambda's `IN_PROGRESS` write is not consulted, it is
  `refresh=false`), else `false`. **Rule: a sync started before this upload may not cover
  it; the next upload or a manual sync does.** The route answers **200** in every case —
  including `job: null` when the sync could not start (the jobs search, the job write or
  SQS failed): the files are stored, one WARNING under `launchpad.knowledge` names the
  exception type, and the caller syncs by hand. `start_sync`'s own shape is unchanged.
- **`has_objects` on every data source of the detail.** `true` when at least one object
  under the prefix is not under the `_text/` sidecar directory, `false` when none is
  (fresh source, or sidecars only), `null` when the listing raised a `ClientError` (bucket
  gone, access denied — logged, never a failed detail). One `list_objects_v2` page of
  `HAS_OBJECTS_PAGE` (100) keys, strongly consistent on S3, so a just-finished upload flips
  it on the very next poll; a further page is read only while a page held nothing but
  sidecars. The console's automatic first sync (`KbDetail.autoFirstSync`) now requires
  `has_objects === true` on top of `AVAILABLE` + no jobs — an empty prefix is never
  synced on the page's initiative; the upload's own sync covers the files.
- **`prefix_empty: true` in the job counts.** `kb_ingest` adds it to the three counters
  when its listing held no original (nothing, or `_text/` sidecars only); the console's
  ingestion-jobs table renders such a job as "empty prefix" instead of three unexplained
  zeros. Absent (not `false`) otherwise. The Lambda also logs ONE INFO line per completed
  job (`kb_ingest job=… kb=… ds=… prefix=s3://<bucket>/<prefix> objects=N originals=M
  scanned=… indexed=… failed=… prefix_empty=…` — identifiers and counts only, never a
  document name or body), so a 0-document job is diagnosable from CloudWatch (until now
  only `outcome=ok … status=COMPLETE` was logged). Redeploy: the handler is a CDK asset
  (`host-update.sh --with-cdk` / `make bootstrap`).

Console side: the detail page's upload reads `job` from the response, toasts
"Uploaded N file(s), indexing started" / "… an indexing job is already running" (coalesced
and covering) / "… sync manually" (`covers_upload: false`) / "the sync could not be started" (`job: null`, with `sync_error` = the exception type only, never a message body; a `STARTING`/`IN_PROGRESS` job whose `updated_at` is older than `RUNNING_JOB_STALE_S` (1800 s) is an orphan and never coalesces) and polls that
job like a manual sync; the assistant's KB wizard (`KnowledgeBaseCreate.tsx`) tracks the
upload's job instead of starting its own (`startSync` stays its manual retry, and the
wizard's own sync runs only when the upload reported `job: null`). The create page
(`KbCreate.tsx`, `?view=new`) needed no sync change — its upload now syncs server-side and
the detail page it lands on sees the job — but the T136 forensics showed the reported
case's upload never left the browser while the page toasted "created" over the error and
navigated anyway: a failed upload now keeps the page, shows the error and the created KB's
name and id, and turns the primary action into "Retry upload / 重试上传" to that same KB
(create is never repeated; "Open knowledge base / 前往知识库" is the way out). Tests: `tests/test_knowledge_selfhosted.py` (N17-obs204
block), `tests/test_memory_extractor.py::test_kb_ingest_over_an_empty_prefix_says_so_in_the_counts`,
`frontend/src/v2/pages/knowledge/KbDetail.test.tsx`, `KbCreate.test.tsx`,
`frontend/src/v2/pages/assistant/KnowledgeBaseCreate.test.tsx`.

## Upload recreates a missing upload source (N17-obs242, `cn/r17-ag`)

User report 2026-10-21 (default host): the user removed the ONLY upload-mode data source
of knowledge base `test` (`KBEXAMPLE00001`, source `DS3B78CBD8D0`, 08:44:58Z), leaving the
KB with 0 sources; 新增数据源 → 上传文件 — whose copy promises "Files are uploaded to the
artifacts bucket and configured as a data source automatically" — then failed with
`kb.no_upload_source` ("knowledge base … has no upload-mode data source"). After deleting
the upload source there was no way to upload again short of recreating the KB (the
`Repair data source` button appears only on the no-source warning, and the user had reached
for the upload dialog). Ruling: PRODUCT (medium); the dialog copy is the contract.

- **`upload_files` creates the upload source when the KB has none.** The self-hosted
  upload source is nothing but a prefix record `kb/<kb_id>/<ds_id>/` in the artifacts
  bucket — exactly what `create_kb` / `add_data_source` write for mode `upload` — so
  `_ensure_upload_source` builds it through the same `_source_to_ds` + `_append_source`
  path (same `DS…` id format, same ledger write, `name` = `ds_id`, `status: AVAILABLE`,
  `has_objects` flips to `true` on the next detail poll like any upload source). The puts
  and `_sync_after_upload` then run exactly as before on the new prefix: one `STARTING`
  job on the new source, the files already in place when the Lambda lists it. An
  `existing`-mode source is never the upload target (its prefix is the member's own
  location), so a KB whose sources are all `existing` also gets an upload source added.
- **Response:** `{keys, job, sync_error?, data_source?}` — `data_source` carries the created
  record (`ds_id, name, mode, bucket, prefix, status, created_at`) and is **absent** when an
  upload source already existed; `keys` are under the new prefix. Backward compatible.
- **Concurrency:** two concurrent FIRST uploads of such a KB serialize on a per-KB process
  lock (`_upload_source_lock(ws.id, kb_id)`, the `_JOBS_KEY_LOCKS` shape); the second
  re-reads the row inside the lock and reuses the source the first created — one source,
  both files under its prefix, `data_source` reported by one response only. The hub is a
  single uvicorn process (`infra/ec2/bootstrap-host.sh`), so the process lock is the whole
  guard; `data_sources` is one JSON column and the read-modify-write in `_append_source`
  would otherwise lose one of the two appends.
- **`kb.no_upload_source` is gone** (delete-not-flag): the self-hosted backend can always
  host an upload source once the substrate exists (`_substrate` still answers 503
  `kb.not_provisioned` before that), so the code had no remaining case. `docs/api.md` /
  `api.zh-CN.md`, `architecture.md` / `architecture.zh-CN.md` and `E17-knowledge-bases.md`
  NC-E17-11 follow; the property walk's `delete_ds` step now asserts the fresh source.
- **Delete semantics (unchanged server-side):** removing the last upload-mode source is
  allowed and triggers nothing; the console's remove confirmation for an `upload` source
  adds one sentence — "The next upload to this knowledge base creates a fresh upload data
  source (a new prefix) automatically." / "下一次向此知识库上传文件时，会自动创建一个新的上传数据源
  （新前缀）。" — so the user knows what the next upload does. The detail's `data_sources[]`
  now carry `mode` in the console type (`DataSource.mode`, additive) for that check.
- **Console:** `KbDetail`'s add-source dialog reads `data_source` from the response, toasts
  "Data source created automatically." before the upload outcome, selects the new source
  and refetches — the sources table shows the new row with its first `STARTING` job (the
  server invalidated that source's job cache when it enqueued). Tests:
  `tests/test_knowledge_selfhosted.py` (N17-obs242 block: 0 sources → created + files under
  the new prefix + one job + `data_source`; existing upload source → unchanged, no second
  source; `existing`-only KB → upload source added; two concurrent first uploads → one
  source; the route), `tests/property/test_knowledge_ingest_properties.py` (`delete_ds`
  step), `frontend/src/v2/pages/knowledge/KbDetail.test.tsx` (N17-obs242 block).

## Idempotent upload source + empty-ingestion hints (N17-obs243 / obs244, `cn/r17-al`)

Live 2026-10-22 02:45:02–02:45:09Z (default host, KB `test` `KBEXAMPLE00001`, 0 sources
after the user removed its upload source): the detail page showed the creation-window
warning 「数据源还没建好……重复点击不会创建多个数据源。」 with 补建数据源; seven clicks
answered seven `201`s and left seven EMPTY upload sources (`DSC5A1E57C42` … `DS4E9B9F22BF`),
no `POST …/files` at all; each source's later sync was `COMPLETE` with 0 scanned and
`prefix_empty: true`, and an empty retrieval read as "ingestion is broken".

- **`add_data_source({"mode": "upload"})` goes through `_ensure_upload_source`** — the same
  per-KB process lock and re-read as the upload's auto-create (N17-obs242). A KB that already
  has an `upload`-mode source gets THAT source back with `created: false` (route: **200**);
  a new one answers **201** with `created: true`. Concurrent clicks, or a click racing a first
  upload, serialize onto one source. `created` is a response-only field (never stored in the
  ledger). Mode `existing` keeps today's semantics: several member-named prefixes per KB are
  allowed, every call appends and answers 201 `created: true`.
- **Console banner variants.** A KB with 0 sources whose row was never touched since the
  INSERT (`created_at === updated_at` — `create_kb` writes the source in the same INSERT, and
  every source change bumps `updated_at`) is in its creation window: the warning + 补建数据源
  stays, and its "clicking twice cannot create a duplicate" copy is now true. Any other ACTIVE
  KB with 0 sources (the user removed the last one) shows a neutral info line instead —
  「这个知识库还没有数据源。点「新增数据源 → 上传文件」上传文件，会自动创建上传数据源。」 — with no
  repair button, and the page does not poll for a source there. The repair button stays
  disabled from the click until the refetched detail has landed; the success toast shows only
  when the server reports `created: true`.
- **Empty-ingestion hints (obs244).** The sources table's latest-ingestion cell adds a muted
  「前缀为空 · 无文件」 / "empty prefix · no files" under a `COMPLETE` job with
  `statistics.prefix_empty === true` or 0 documents scanned. The 检索测试 empty state says
  「知识库里还没有文件——先在「数据源」上传文件并等待 ingestion 完成。」 when every source is known to
  hold no file (`has_objects: false`, or — when the server could not tell — its latest job
  found none; a KB with no source at all counts too); otherwise today's text.
- Tests: `tests/test_knowledge_selfhosted.py` N17-obs243 block (route 201 then six 200s with
  the same `ds_id`; backend returns the existing source; `existing` still appends, route 201;
  barrier-held concurrent clicks → one source, one `created: true`; repair then upload share
  one source), `frontend/src/v2/pages/knowledge/KbDetail.test.tsx` N17-obs243 / obs244 blocks.

## Supported document types and limits (N17-obs239 / obs240, `cn/r17-af`)

What the ingestion Lambda (`backend/lambdas/memory_extractor/handler.py`) indexes, and the
limits it applies — the console's data-source form states exactly this (user decision
2026-10-21: the "coming soon" connector list — web crawler / SharePoint / Confluence /
Google Drive / OneDrive — was replaced by the supported types and limits; N17-obs240).

| What | Where it is decided | Behaviour |
|---|---|---|
| `.txt` `.md` `.markdown` `.csv` `.json` `.html` `.htm` | `handler.NATIVE_TEXT_EXT`, `_doc_text` | Indexed as UTF-8 text (`errors="replace"`). `.json` is pretty-printed (`json.dumps(…, indent=2)`; invalid JSON is indexed raw). `.html` / `.htm` have their tags stripped (`re.sub(r"<[^>]+>", " ")` + `html.unescape`). |
| `.pdf` | `knowledge_selfhosted.upload_files` (hub) + the Lambda's sidecar branch | Supported **only through the console upload**: the hub extracts the text with `pypdf.PdfReader` into a `_text/<name>.txt` sidecar next to the file, and the Lambda indexes the sidecar instead of the PDF. A scanned or encrypted PDF yields an empty sidecar (the upload never fails on it). A PDF placed in an *existing S3 location* has no sidecar → the document is marked `UNSUPPORTED`, reason `pdf without an extracted text sidecar (upload it through the console)`. |
| any other extension | `_doc_text` | The document is marked `UNSUPPORTED`, reason `unsupported file type .<ext>`, counted in `failed`. |
| size | `handler.MAX_DOC_BYTES = 2_000_000` | A text object (or a PDF's sidecar) over 2 MB is skipped: `UNSUPPORTED`, reason `document larger than 2 MB`. The limit is on the *text*, so a larger PDF whose extracted text stays under 2 MB still indexes. |
| length | `handler.MAX_CHUNKS = 400` | `chunk_text(text)` is cut after 400 chunks; the document record carries `truncated: true`. |

Console side (`frontend/src/v2/pages/knowledge/SourceFields.tsx`, `lib/knowledgeBases.ts`):

- The "Data source" field shows one chip per extension (`KB_SUPPORTED_EXT`) and the hint
  "Text files are indexed directly; PDF text is extracted on upload (scanned or encrypted PDFs
  yield no text). Other types are rejected. Up to 2 MB of text per document; longer documents
  are truncated after 400 chunks." The existing-S3 mode's hint adds that only those types are
  indexed and that a PDF placed there directly is skipped.
- **N17-obs239** — the file input carries `accept=".txt,.md,.markdown,.csv,.json,.html,.htm,.pdf"`,
  and a pick containing an unsupported extension (case-insensitive — `A.PDF` passes) is
  refused as a whole with the field error "Unsupported file type: {names}. Supported: …":
  nothing reaches the draft, nothing is uploaded. Before this, an unsupported file was
  uploaded and only failed after ingestion. A picked file over 2 MB only produces a warning
  hint (the limit is on extracted text).
- The console constants are twinned with the Lambda's by
  `backend/tests/consistency/test_surface_consistency.py` (`KB_SUPPORTED_EXT` ==
  `NATIVE_TEXT_EXT` ∪ {`.pdf`}; `KB_MAX_DOC_BYTES` / `KB_MAX_CHUNKS` == `MAX_DOC_BYTES` /
  `MAX_CHUNKS`), so widening the server set without the form fails the gate. Tests:
  `frontend/src/v2/pages/knowledge/SourceFields.test.tsx`.

The server-side set did not change on this branch (no Lambda edit, no CDK deploy).

## Sync enqueue ordering and the never-dispatched reaper (N17-obs236, `cn/r17-ae`)

User report 2026-10-21 (default host, forensics deploy-ops-30): 同步 on a KB → `POST
…/data-sources/{ds_id}/sync` → the job PUT (`launchpad-jobs/_doc/<job_id>?refresh=true`)
hit the 10 s interactive read timeout twice (retry 1/1), `RetryingHttp` re-raised, the
router's `_guard` answered 502 `kb.unavailable`. The FIRST PUT had been applied server-side
(the document existed, `STARTING`, `counts: {}`) but `send_message` — which runs after the
PUT — was never reached: an orphan `STARTING` job that no reaper closed, shown by the
console as running forever (empty statistics), and the user believed the upload had
failed. `_running_job` already ignored it for coalescing after `RUNNING_JOB_STALE_S`, so
later uploads were not blocked — only the display lied. Three rules now hold in
`_enqueue_sync` (the one path behind `start_sync` and the upload's sync):

- **Order:** job PUT first, then the SQS message (unchanged — a job the console cannot see
  must never be running). **(a) A transport error on the PUT is confirmed, not trusted.**
  When the PUT raises one of `opensearch_retry.TRANSPORT_EXCEPTIONS` (urllib3 timeouts /
  connection / protocol errors after the retry budget, or a bare `OSError`) the hub GETs
  the document ONCE through a one-attempt, 5 s-read-timeout funnel client — the **confirm
  tier** (`_os_confirm`, `JOB_CONFIRM_READ_TIMEOUT_SECONDS`; its own funnel cache key and
  therefore its own PoolManager, so a fresh connection may be needed and its worst case is
  connect 5 s + read 5 s ≈ 10 s on top of the ~21 s the interactive budget has already
  spent): present → the write was applied and
  only the ack was late, the sync goes on to SQS and answers `{job_id, STARTING}` normally
  (one WARNING under `launchpad.knowledge`, exception type only); absent, or the GET itself
  fails → the ORIGINAL error is re-raised (nothing is known to be written; the 502 is
  truthful) and SQS is not called. An `OpenSearchError` (a 4xx/5xx the node did answer) is
  not a transport error and propagates as before.
- **(b) SQS failing after the document exists marks it `FAILED`.** `send_message` raising
  (any exception) → the document is updated best-effort to `status: FAILED`,
  `failure_reason: "enqueue_failed: <exception class>"` (never the message body), the
  jobs cache is dropped again so the next poll sees `FAILED` rather than `STARTING`, and
  the original error still propagates (502 on the sync route; the upload route keeps its
  200 + `job: null` + `sync_error`). A failed mark is one WARNING; the reaper below closes
  the job later.
- **(c) The never-dispatched reaper closes the orphans an older hub left.** The two
  presenting reads — `list_ingestion_jobs` (the dedicated route) and the detail's cached
  job list (`_cached_jobs` → `_reap_never_dispatched`) — turn a job that is still
  `STARTING` with empty `statistics` and a `created_at` older than `RUNNING_JOB_STALE_S`
  (1800 s) into `FAILED` with `failure_reasons: ["never_dispatched: no worker picked the
  job up within 30 min"]` (`NEVER_DISPATCHED_REASON`), and persist the same on the
  document (`failure_reason`, `updated_at` = now; best-effort through the confirm tier —
  one attempt / 5 s, so a busy node adds ≤ ~10 s to the detail's single-flight rebuild,
  not the interactive ~21 s; a refused `_update` is one WARNING, the presented list is
  already corrected and the next read retries). A message delivered AFTER the reap flips
  the document `FAILED → IN_PROGRESS` while `failure_reason` lingers until the Lambda's
  terminal write clears it, so `_jobs` projects `failure_reasons: []` for every status in
  `RUNNING_JOB_STATUSES` — a running job never shows a failure reason. An unparsable or
  naive `created_at` (`ValueError` / `TypeError` against the aware hub clock) means "age
  unknown": neither the reaper nor `_running_job`'s stale rule touches the job. The
  extractor Lambda's first write on a job is `STARTING → IN_PROGRESS`, so a long-`STARTING`
  job really was never picked up; a `STARTING` job within the bound, a `STARTING` job that
  already carries counts and any `IN_PROGRESS` job are left alone whatever their age
  (those are the Lambda's, and `_running_job`'s own stale rule still governs coalescing).
  The reaper runs inside the detail's `cached_jobs` build, so one TTL window presents the
  corrected list and a failed persist cannot fail the detail. On the default host this
  retroactively closes the reported job on the first detail poll after deploy.
- **One age computation (N17-obs286, design review C4).** `_running_job`, both reaper rules
  and the `INDEXING` presentation (`_doc_status`) read a stamp's age through one helper —
  `stamp_age` (absent / unparsable / naive = unknown), `job_age(job, now, basis=…)` and
  `is_stale` (strictly over `RUNNING_JOB_STALE_S`; unknown is never stale). The stamp stays
  explicit per caller (`JOB_AGE_BASES`): `last_write` = `updated_at`, else `started_at`, else
  `created_at` (the coalescer); `dispatch` = `started_at` (never-dispatched, the projection of
  the document's `created_at`); `progress` = `updated_at` (stalled). Behaviour unchanged;
  `tests/test_knowledge_job_age.py` pins the helpers against the inline formulas they replaced.

The console needs no new copy: a `FAILED` job already renders its `failure_reasons`
(`KbDocuments.tsx`); the reason strings are technical, like the Lambda's own. Tests:
`tests/test_knowledge_selfhosted.py` (N17-obs236 block — late-ack PUT continues with exactly
one SQS message; lost PUT → error + no SQS; PUT and GET both failing → original error;
SQS failure → `enqueue_failed` mark + propagation, also when the mark itself fails; the
reaper on a 31-min / 5-min / counted / `IN_PROGRESS` quartet through both reads; the
confirming client's tier; the 502 through the router).

## Index ensure: the create race is closed hub-side (WB-007)

`create_kb` makes the KB's own k-NN index through
`app/services/opensearch_ensure.py::ensure_index(http, name, body)`, not the core's
`OpenSearchHttp.ensure_index` directly. The core's ensure is a non-atomic
`HEAD /<index>` → `PUT /<index>`; when another creator wins the race (or the HEAD answered
from a stale view) the PUT fails with `400 resource_already_exists_exception`, and the KB
used to be written `FAILED` for an index that exists and is usable.

The wrapper delegates to the core and, only on that already-exists answer, `GET`s the live
index and compares it with the requested body:

- **compatible** → returns `False` ("ensured, not created"); the KB is `ACTIVE`;
- **incompatible** → raises `IndexIncompatible` (an `OpenSearchError` subclass, status
  409, `.name`, `.problems`), which `create_kb` records in `failure_reasons`
  (`index '…' exists with an incompatible mapping: field 'embedding' dimension 512 != 1024`).

**Compatible** is deliberately conservative: every property the requested mapping names
must exist live with the same `type` (a missing field would be dynamically mapped on first
write — `keyword` coming back as `text` silently breaks term filters); a `knn_vector` field
must match `dimension` and every requested `method.*` key (`engine`, `space_type`, `name`);
an `enabled` flag the request carries must match; a requested `index.knn: true` must be
live `true`. Extra live fields are allowed (dynamic mapping adds them) and shard/replica
counts are capacity, not semantics. The HEAD-found path is unchanged (no verification —
the pre-existing behaviour for a second bootstrap pass).

The core module and its two mirrors (ruling R15) are byte-identical as before: the
extractor Lambda and the agent runtime never create indices, so the fix needs no
`--with-cdk` redeploy. `memory_bootstrap.ensure_memory_substrate` uses the same wrapper
for the three fixed indices (a raced index counts as `reused`).

## Mutations are the creator's or an administrator's (N17-obs530, `cn/wk-authz`)

Workshop dry run 2026-10-29: a service-desk member opened the detail of a KB the trainer
(an administrator) had created — visible to the whole desk under `isolated`, so the desk can
sync it and run 检索测试 — and was offered 删除 / 新增数据源 / 全量重建 / 移除; every one of
them went through, because the routes checked only `_require_visible`. The routes now apply the
creator-or-admin rule of `services/ownership.py` (users-and-ownership.md §8) through
`routers/knowledge._require_owner(db, ws, request, kb_id, action)`:

| route | gate | `action` |
|---|---|---|
| `PATCH /api/knowledge-bases/{kb}` | creator-or-admin | `edit` |
| `DELETE /api/knowledge-bases/{kb}` (with or without `force`) | creator-or-admin, then the N17-obs4 `force` override (admin), then the attached 409 | `delete` |
| `POST …/{kb}/files` | creator-or-admin | `upload` |
| `POST …/{kb}/data-sources` | creator-or-admin | `add_data_source` |
| `DELETE …/{kb}/data-sources/{ds}` | creator-or-admin | `delete_data_source` |
| `POST …/{kb}/data-sources/{ds}/sync` with `{"full": true}` | creator-or-admin | `full_rebuild` |
| `POST …/sync` (incremental), `POST …/query`, every `GET` | any viewer of the KB (unchanged) | — |

The gate reads the `knowledge_bases` ledger row's `created_by` against the caller's `users.id`;
a row with no creator (preset-installed, legacy, the config admin's) — or a `kb_id` with no
ledger row — is an administrator's. Order: the visibility 404 first (a hidden KB stays
indistinguishable from a missing one), then this 403, then `force` / attached / status checks,
so a forbidden caller learns nothing about the KB's state and nothing reaches the backend. The
refusal is `403 auth.forbidden {kb_id, action, created_by, created_by_name}` with a message
naming the rule; it holds in BOTH visibility modes (under `shared` another member's KB is
readable and refused on mutation — 403, never 404). Console (`frontend/src/v2/ownership.ts`
`useMayMutate`): the detail hides 删除 / 新增数据源 / 全量重建 / 移除 / 编辑描述 and the 补建 action
for a KB the member cannot change and shows the `creatorOnlyActions` alert naming the creator;
the list hides the per-row 删除; 立即同步, 文档 and 检索测试 stay. Tests:
`tests/test_knowledge_ownership.py`, `tests/test_knowledge_force_strip.py` (the member's own
KB), `tests/test_agent_visibility.py::test_kb_used_by_omits_hidden_agent_names`,
`frontend/src/v2/pages/knowledge/KbDetail.test.tsx` ("creator-or-admin gating").

## Delete contract (WB-008)

`DELETE /api/knowledge-bases/{kb_id}` commits the ledger delete **first**, then runs three
best-effort storage steps — `s3` (purge `kb/<kb_id>/`), `index` (drop the KB's own index,
404 tolerated), `docs` (`_delete_by_query` of the KB's rows in `launchpad-kb-docs` and
`launchpad-jobs`). Every step is wrapped: `OpenSearchError`, a urllib3 timeout re-raised
by `RetryingHttp`, a botocore error from the purge — nothing raises past the route once
the row is gone (a retried `DELETE` would only 404). The response is additive:

```json
{"kb_id": "KB…", "deleted": true,
 "cleanup": {"s3": "ok", "index": "pending", "docs": "ok"},
 "cleanup_warnings": ["index: ReadTimeoutError"],
 "cleanup_job_id": "…"}
```

- `cleanup_warnings` carries, per pending step, the exception type plus the status or the
  error code and operation — `index: OpenSearchError: HTTP 403`,
  `s3: ClientError: SlowDown (ListObjectsV2)`, `docs: ReadTimeoutError` — and never a
  message or a response body (`app.core.errors.redacted_error`, N17-obs99 in `cn/r17-n`: the
  first line of `str(exc)` used to carry the OpenSearch root cause, the IAM sentence with
  the role ARN, botocore's `An error occurred (…)` framing). The same lines go into the
  `kb_cleanup` job payload and the WARNING log. The `s3` step deletes through the one
  reporting deleter (`services/s3_sweep.py`, N17-obs119 `cn/r17-t`): keys S3 refuses inside a
  200 `DeleteObjects` answer (`Errors[]`) leave the step `pending` with
  `s3: S3SweepResidue: 1 of 2 objects refused (AccessDenied)` (counts + S3 code, never a key or
  the S3 message) and the job re-runs it — never `ok` with the keys still there.
- The pending steps are handed to a **`kb_cleanup` job** (`app/services/kb_cleanup.py`,
  same shape as the teardown's `log_group_settle`): a `jobs` row the console can read on
  `GET /api/jobs/{cleanup_job_id}` (payload `{kb_id, bucket, steps, schedule_s, warnings}`,
  JSONL events with `stage: "kb_cleanup"`), a daemon thread that retries **only the steps
  still pending** on a bounded schedule (`+5 s, +30 s, +120 s`; three retries, then
  `failed` with the reason — every step is idempotent), and a startup resume starter
  registered in `pipeline.resume_pending_jobs` that re-runs a job a restart left
  `queued`/`running` (needs the registered knowledge backend and the job's workspace;
  otherwise it closes `failed` with the reason, never raising).
- `cleanup_job_id` is `null` when nothing was pending (or the ledger refused the row —
  the retries then still run, logging only).
- The console (V2 `useKbDelete`) shows
  `knowledge.detail.deletedCleanupPending` instead of `knowledge.detail.deleted` when
  `cleanup_warnings` is non-empty; the KB list no longer carries the row either way.

## No backend registered

`knowledge_backend.get_backend()` raises `AppError("feature.unavailable", ...,
{"feature": "knowledge_bases"}, status_code=503)`. `routers/knowledge.py` calls
`knowledge_backend.get_backend().<method>(...)` directly (no `_guard` wrapper, unlike
`memory.py`) — a 503 and a `KBAttachedError` 409 both propagate as ordinary `AppError`s
through `app.core.errors.register_error_handlers`.

**`attached_agents` / `refuse_if_attached_to_system_agent` ✅ wired (N6).** Two pure
ledger helpers now live at module scope in `knowledge_backend.py` —
`attached_agent_names(db, workspace, kb_id)` and
`refuse_if_attached_to_system_agent(db, workspace, kb_id)` (raises `AppError
("kb.attached_to_system_agent", …, status_code=409)`) — and `routers/knowledge.py::
delete_knowledge_base` calls both **before** `knowledge_backend.get_backend()` is ever
touched: a system-preset mount refuses first, then an ordinary mount 409s
(`KBAttachedError`) unless `force=true`, matching the Memory pattern
(`memory_ownership.require_managed` ahead of `memory_backend.get_backend()`). The
`KnowledgeBackend` Protocol's own `attached_agents`/`refuse_if_attached_to_system_agent`
instance methods (used by `_summary`/`_detail` to populate the `attached_agents` field
on every KB response) delegate to these same two module-level functions — one
implementation, two call shapes.

## How a node registers a backend

```python
from app.services import knowledge_backend
knowledge_backend.set_backend(SelfHostedKnowledgeBackend(...))  # at app startup (N6)
```

## Contract notes (from the Task 5 review; dispositions after N6's implementation)

- `delete_kb` ✅ raises `KBAttachedError` (409) when agents still mount the KB and
  `force` is false — enforced twice: the router's pre-check (above) and, redundantly,
  inside `SelfHostedKnowledgeBackend.delete_kb` itself (so a direct backend caller
  that skips the router still gets the guard).
- `force=true` strips the KB from every mounting agent **in the shared layer**, not in a
  backend (N17-obs31 = N16-obs34, `cn/r17-h`): `knowledge_backend.strip_kb_from_agents(db,
  ws, kb_id, by=…)` runs in `routers/knowledge.py::delete_knowledge_base` before
  `get_backend().delete_kb` — one transaction over EVERY mounting agent (hidden ones
  included), `Agent.last_strip = {kb_ids, at, by}` (projection `kb_stale`, cleared by the next
  successful publish in `pipeline._finish`), ONE commit for every agent and only then a `ledger`
  line on each agent's latest deploy job log (informational, best-effort — a log failure neither
  splits the strip nor fails the delete), no spec snapshot; a mounting agent with a live deploy job refuses the force with `409
  agent.deploy_in_progress {kb_id, agents}` before anything is written. The strip commits
  first: a storage failure afterwards leaves at worst a KB with no mounts, never a mount on a
  gone KB. `force` is an administrator's override (N17-obs4): a member's `?force=true` is `403
  auth.forbidden` before any guard — a strip would rewrite other members' agents. A backend's own `delete_kb(force=True)` called directly (jobs, tests) does not strip
  — the router is the entrance. The AWS-managed backend used to re-sync per-agent gateway
  `Retrieve` targets here; the CN port has none (retrieval is a tool in the Runtime bundle), so
  the deployed Runtime keeps the tool until its next re-publish — hence `kb_stale`.
- `create_kb` ✅ `source` is `{mode, bucket, prefix}` (`SourceSpec` in
  `routers/knowledge.py`); `mode="existing"` is rejected with `kb.external_bucket_
  unsupported` (422) unless `bucket` is the workspace's own artifacts bucket —
  ruling R18, since the extractor Lambda and every agent runtime are granted that
  bucket only, never an arbitrary external one. The `prefix` is constrained too
  (same ruling, N6 node-fix round): it must satisfy `prefix.startswith(f"kb/{kb_id}/")`
  — the KB's own S3 namespace (`kb_prefix_root`) — else `kb.external_prefix_
  unsupported` (422); `..`, absolute, and empty prefixes are rejected outright. The
  extractor Lambda's role grants `s3:List*` on the **whole** artifacts bucket plus
  `GetObject` on `memory/*` and `kb/*`, so an unconstrained prefix (e.g. `"memory/"`
  or `""`) would let a member point a KB's ingestion at another workspace's memory
  events or another KB's documents — a cross-workspace exfiltration path. Because
  `kb_id` is only assigned once the KB row exists, an `existing` source can only ever
  legitimately pass this check via `add_data_source` on an already-created KB (whose
  `kb_id` the member already knows and may have placed objects under directly), not
  as the initial source at `create_kb` time.
- `list_kbs` ✅ items are `{kb_id, name, description, status, updated_at,
  data_source_count, attached_agents}` — a superset of the four original fields.
- `list_documents` returns **`{documents, next_token}`**, not `{items, next_token}`
  (ruling R8) — the frozen interface said "a page `{items, next_token}`" generically,
  but the untouched frontend (`KbDocuments.tsx`) reads `page.documents`; the frontend
  contract won.
- `kb_gateway_*` resource keys (`kb_gateway_id`, `kb_gateway_arn`, …) are **still
  dead** — still unaddressed as of N6; owner **N7** (ruling R20). The 14 stale reads
  remain in `assistant/service.py` / `assistant/proposal.py` and gate only the
  *assistant* flow's KB picker (the Create wizard's own picker, `GET
  /api/knowledge-bases?status=ACTIVE`, is unaffected). One-line fix for whoever picks
  this up: `capabilities.kb_gateway = knowledge_backend.backend_or_none() is not None`.

## Error codes

| Code | Status | Raised by |
|---|---|---|
| `kb.not_provisioned` | 503 | `_substrate(ws)` — the workspace has no Memory/KB substrate (translates `memory_selfhosted`'s `memory.not_provisioned` so a KB caller never sees a Memory-flavored code) |
| `kb.not_found` | 404 | `_row(ws, kb_id)` — unknown `kb_id`, or `mapped_opensearch_error(exc, feature="kb")` on an OpenSearch 404 |
| `kb.data_source_not_found` | 404 | `_ds(row, ds_id)` — unknown `ds_id` on a known KB |
| `kb.name_exists` | 409 | `create_kb` — duplicate KB name in the workspace |
| `kb.external_bucket_unsupported` | 422 | `_source_to_ds` — an `existing` source names a bucket other than the workspace's artifacts bucket (R18) |
| `kb.external_prefix_unsupported` | 422 | `_source_to_ds` — an `existing` source's `prefix` is outside this KB's own `kb/<kb_id>/` namespace, or is empty/`..`/absolute (R18, N6 node-fix round) |
| `kb.not_active` | 409 | `query` — the KB is not `ACTIVE` (e.g. still `CREATING`/`FAILED`) |
| `kb.has_attached_agents` | 409 | `KBAttachedError` — `delete_kb(force=False)` while agents still mount the KB |
| `kb.attached_to_system_agent` | 409 | `refuse_if_attached_to_system_agent` — a system-managed preset mounts the KB |
| `kb.backend_unavailable` | 503 | `mapped_opensearch_error(exc, feature="kb")` — the OpenSearch domain itself is throttling (429) or unhealthy (5xx) |
| `kb.no_files` / `kb.too_many_files` / `kb.upload_too_large` | 400 / 422 / 413 | `routers/knowledge.py::upload_knowledge_base_files` — upload-size/count caps checked before any S3 write |

## Embedding model (D21 M1-d)

`SelfHostedKnowledgeBackend._embedder()` follows the model registry's active embedding row
through `services/embedding_backend.build_embedder()` (Titan v2 for the seed; an
`openai_compatible` row → `OpenAIEmbedder` with the provider's key resolved hub-side). A
new per-KB index is created with the row's k-NN `dimension`
(`embedding_backend.vector_index_body(core.kb_index_body())` in `create_knowledge_base`),
and every `kb_ingest` SQS message carries the row as `embedding: {…}` (same block as the
memory backend) so the extractor Lambda ingests with the same model. The registry refuses
an embedding change while any KB index (or the memory index) holds documents — 409
`embedding.reindex_required` naming the indices (`model-registry.md` §10). Existing
indices keep their mapping; ingesting into an index created under another row is exactly
what the guard prevents.
