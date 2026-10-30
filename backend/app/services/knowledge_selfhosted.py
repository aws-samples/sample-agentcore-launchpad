"""Self-hosted Knowledge Bases (N6, D4/D8): S3 documents · Titan chunks in OpenSearch ·
SQS → Lambda ingest.

Bedrock Knowledge Bases do not exist in `cn-northwest-1` (spec C5), so this module
implements the frozen `KnowledgeBackend` Protocol (`app/services/knowledge_backend.py`)
on the **same substrate as self-hosted Memory** (`memory_selfhosted.py`): documents live
in S3 under `kb/<kb_id>/<ds_id>/`, indexed chunks live in a dedicated OpenSearch index
per KB (`opensearch_http.kb_index_name()` / `kb_index_body()`, ruling R3), and ingestion
is handed off to SQS for the Lambda extractor (a later task) to run asynchronously —
job state lives in OpenSearch (ruling R4), never the ledger.

A `KnowledgeBase` ledger row is the console-facing identity of a KB (there is no AWS
resource to read back); `data_sources` is stored inline on the row since there is no
AWS DataSource resource either. The ledger-side attached-agent guards
(`knowledge_backend.attached_agent_names` / `refuse_if_attached_to_system_agent`) are
pure reads with no backend dependency — this class only wraps them with a short-lived
session for the Protocol's instance-method shape.
"""

import io
import json
import logging
import mimetypes
import secrets
import threading
import time
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from pypdf import PdfReader
from sqlalchemy.exc import IntegrityError

from app.core import naming
from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.models.ledger import KnowledgeBase
from app.services import (
    aws_clients,
    creators,
    embedding_backend,
    kb_cleanup,
    knowledge_backend,
    memory_selfhosted,
    opensearch_ensure,
    opensearch_retry,
    s3_sweep,
)
from app.services import opensearch_http as core
from app.services.workspace import WorkspaceContext

logger = logging.getLogger("launchpad.knowledge")

TEXT_SIDECAR_DIR = "_text"
DOC_PAGE = 50
# An ingestion job in one of these states still has the prefix listing ahead of it or in
# hand — a second sync for the same data source would only re-scan the same objects, so an
# upload coalesces onto it (N17-obs204). The extractor Lambda writes only STARTING →
# IN_PROGRESS → terminal; `RUNNING` is accepted for a future writer.
RUNNING_JOB_STATUSES = frozenset({"STARTING", "IN_PROGRESS", "RUNNING"})
#: N17-obs274 pass 4: how a document left `INDEXING` past `RUNNING_JOB_STALE_S` is listed
INDEXING_INTERRUPTED_REASON = "interrupted — run the sync again"
#: N17-obs276: the jobs-index row kinds the extractor Lambda writes per document of a source
#: (`handler.KIND_KB_INGEST_DOC_RESULT` / `KIND_KB_DOC_LEASE` — the Lambda bundle is not
#: importable from the hub; `tests/test_knowledge_selfhosted.py` pins the two names together)
EXTRACTOR_DOC_ROW_KINDS = ("kb_ingest_doc_result", "kb_ingest_doc_lease")
#: 2× the extractor Lambda's timeout (900 s): a "running" job older than this is an orphan.
RUNNING_JOB_STALE_S = 1800
# N17-obs236: the extractor Lambda's first write on a job is `STARTING → IN_PROGRESS`, so a
# job still `STARTING` with empty counts after RUNNING_JOB_STALE_S was never picked up —
# the SQS message was never sent (the hub's `send_message` did not run after the job PUT)
# or was lost. The detail / jobs reads present AND persist such a job as `FAILED` with this
# reason, so the console stops showing it as running forever.
NEVER_DISPATCHED_REASON = "never_dispatched: no worker picked the job up within 30 min"
# rev-r22 (b) on N17-obs247: an IN_PROGRESS job whose `updated_at` has not moved for
# RUNNING_JOB_STALE_S made no progress (the planner died before its final write, or every
# document message went to the DLQ) — presented and persisted as FAILED with this reason
STALLED_REASON = "stalled: no progress for 30 min — run the sync again"
# Read timeout (seconds) of the one confirming `GET` the hub issues after the job PUT raised
# a transport error (N17-obs236): the write may have been applied although the ack timed out.
JOB_CONFIRM_READ_TIMEOUT_SECONDS = 5.0
# `has_objects` lists this many keys per page; more pages are fetched only while a page
# held nothing but `_text/` sidecars (≥ 100 PDFs whose names sort after `_`).
HAS_OBJECTS_PAGE = 100

# ── ingestion-job cache for the detail route (N-SCN-obs11) ───────────────────────
# `GET /api/knowledge-bases/{kb_id}` is the console's poll while an ingestion runs, and
# every poll used to issue one synchronous `_search` on `launchpad-jobs` per data source.
# While the extractor bulk-indexes HNSW vectors and `_refresh`es on the single t3.small
# node, that tiny search can take > 30 s — one read timeout + 1 s backoff + the retry
# turned a < 0.4 s poll into a 34 s HTTP 200. The detail therefore serves the per-data-
# source job list from this short in-process cache: one key per (workspace, kb, ds), a
# ~10 s TTL, single-flight per key (concurrent polls share one search), and on an
# OpenSearch error/timeout the detail still answers 200 with the last cached jobs (or
# `[]`) plus `jobs_degraded: true`; the degraded answer is cached for the same TTL so one
# slow node answer costs at most one poll. The dedicated ingestion-jobs route stays live
# and uncached. `start_sync` / `delete_data_source` / `delete_kb` drop the affected keys
# so a freshly seeded `STARTING` job is visible on the very next poll. Each key also
# carries a generation counter (r7-j): an invalidation bumps it, and a `build()` that
# started before the bump returns its (pre-write) result to its own caller without
# storing it, so a poll racing `start_sync` cannot pin a stale `[]` for a whole TTL.
# A search that OpenSearch itself cut short (`timed_out: true`, partial hits) counts as
# degraded too — the last complete list stays, never the partial one.
JOBS_CACHE_TTL_SECONDS = 10.0
JOBS_SEARCH_TIMEOUT = "5s"  # OpenSearch search-body `timeout` for the `_jobs` query
_JobsKey = tuple[str, str, str]
# key → (monotonic timestamp, jobs, degraded)
_JOBS_CACHE: dict[_JobsKey, tuple[float, list[dict[str, Any]], bool]] = {}
_JOBS_CACHE_LOCK = threading.Lock()
_JOBS_KEY_LOCKS: dict[_JobsKey, threading.Lock] = {}
# key → generation; registered with the key lock, bumped by `invalidate_jobs_cache`
_JOBS_GEN: dict[_JobsKey, int] = {}

# (workspace_id, kb_id) → the lock two concurrent FIRST uploads of a KB without an
# upload-mode source take before one of them creates it (N17-obs242). `data_sources` is
# one JSON column, so the read-modify-write in `_append_source` is not atomic across
# threads; the hub is one uvicorn process (`infra/ec2/bootstrap-host.sh`), so a process
# lock keyed per KB is the whole guard — same shape as `_JOBS_KEY_LOCKS`.
_UPLOAD_SOURCE_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_UPLOAD_SOURCE_LOCKS_GUARD = threading.Lock()


#: N17-obs286 (design review C4): which stamp each orphan rule measures a job's age from — the
#: first present (truthy) field of the tuple. The choice differs per caller on purpose:
#: * ``last_write`` — the upload coalescer (`_running_job`): any write proves the job alive,
#:   falling back to the start / creation stamp for a job that never wrote one;
#: * ``dispatch`` — the never-dispatched reaper: a `STARTING` job with no counts is judged by
#:   when it was started (its `updated_at` is the hub's own PUT, not a worker's);
#: * ``progress`` — the stalled reaper: an `IN_PROGRESS` job is judged by its last write only.
JOB_AGE_BASES: dict[str, tuple[str, ...]] = {
    "last_write": ("updated_at", "started_at", "created_at"),
    "dispatch": ("started_at",),
    "progress": ("updated_at",),
}


def stamp_age(stamp: Any, now: datetime) -> float | None:
    """Seconds from an ISO-8601 ``stamp`` to ``now``; None when the stamp is absent, unparsable,
    or naive while ``now`` is aware."""
    if not stamp:
        return None
    try:
        return (now - datetime.fromisoformat(str(stamp))).total_seconds()
    except (ValueError, TypeError):  # unparsable stamp, or a naive one vs aware now
        return None


def job_age(job: dict[str, Any], now: datetime, *, basis: str) -> float | None:
    """A job's age by the stamp ``basis`` names (`JOB_AGE_BASES`) — the one age computation
    the coalescer and the reaper share."""
    stamp = next((job.get(f) for f in JOB_AGE_BASES[basis] if job.get(f)), None)
    return stamp_age(stamp, now)


def is_stale(age: float | None) -> bool:
    """Older than `RUNNING_JOB_STALE_S` (2× the extractor Lambda's timeout); an unknown age is
    never stale."""
    return age is not None and age > RUNNING_JOB_STALE_S


def _upload_source_lock(workspace_id: str, kb_id: str) -> threading.Lock:
    with _UPLOAD_SOURCE_LOCKS_GUARD:
        return _UPLOAD_SOURCE_LOCKS.setdefault((workspace_id, kb_id), threading.Lock())


class JobsSearchTimedOut(Exception):
    """OpenSearch answered the `_jobs` search with `timed_out: true` — the hits are
    whatever the shard had collected when `JOBS_SEARCH_TIMEOUT` ran out, so the detail
    route treats the answer as degraded rather than caching the partial list as fresh."""


def _jobs_now() -> float:
    return time.monotonic()


def reset_jobs_cache() -> None:
    """Drop every cached ingestion-job list (tests, config reload)."""
    with _JOBS_CACHE_LOCK:
        _JOBS_CACHE.clear()
        _JOBS_KEY_LOCKS.clear()
        _JOBS_GEN.clear()


def invalidate_jobs_cache(workspace_id: str, kb_id: str, ds_id: str | None = None) -> None:
    """Forget the cached job list of one data source, or of every data source of a KB
    when ``ds_id`` is None — called after a write that changes what the next poll must
    see (a new `STARTING` job, a removed data source or KB). The key's generation is
    bumped as well, so a `build()` already in flight for it (registered in `_JOBS_GEN`
    before it started) will not store its pre-write result; the key lock is left in
    place so that in-flight build and the next poll stay single-flight."""
    with _JOBS_CACHE_LOCK:
        doomed = [
            k for k in set(_JOBS_CACHE) | set(_JOBS_GEN)
            if k[0] == workspace_id and k[1] == kb_id and (ds_id is None or k[2] == ds_id)
        ]
        for k in doomed:
            _JOBS_CACHE.pop(k, None)
            _JOBS_GEN[k] = _JOBS_GEN.get(k, 0) + 1


def cached_jobs(
    key: _JobsKey, build: Any, *, log: logging.Logger | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """``(jobs, degraded)`` for ``key`` — a fresh cache hit, else one ``build()`` under
    the key's lock (single-flight). A ``build()`` that raises (OpenSearch 5xx, urllib3
    read timeout — `OpenSearchHttp.request` does not wrap pool errors) yields the last
    cached jobs (or ``[]``) with ``degraded=True``; that answer is cached too, so the
    polls of the next TTL window answer instantly instead of each re-hitting the busy
    node. A ``build()`` that raises `JobsSearchTimedOut` (OpenSearch cut the search short,
    partial hits) is degraded the same way — the last complete list is kept. The result is
    stored only if the key's generation is unchanged across ``build()``: an
    ``invalidate_jobs_cache`` that landed meanwhile (`start_sync` wrote `STARTING` after
    our search ran) makes the result stale by definition, so it is returned to this
    caller but not cached — the next poll rebuilds. Expired entries are evicted on every
    store (keys are per data source)."""

    def lookup() -> tuple[list[dict[str, Any]], bool] | None:
        hit = _JOBS_CACHE.get(key)
        if hit and _jobs_now() - hit[0] < JOBS_CACHE_TTL_SECONDS:
            return hit[1], hit[2]
        return None

    if (fresh := lookup()) is not None:
        return fresh
    with _JOBS_CACHE_LOCK:
        key_lock = _JOBS_KEY_LOCKS.setdefault(key, threading.Lock())
        _JOBS_GEN.setdefault(key, 0)  # visible to invalidate() before the build starts
    with key_lock:
        if (fresh := lookup()) is not None:  # a concurrent poll built it
            return fresh
        with _JOBS_CACHE_LOCK:
            stale = _JOBS_CACHE.get(key)
            gen_before = _JOBS_GEN.setdefault(key, 0)
        try:
            jobs, degraded = list(build()), False
        except Exception as exc:  # noqa: BLE001 — the detail must still answer 200
            jobs, degraded = (list(stale[1]) if stale else []), True
            if log is not None:
                log.warning(
                    "knowledge_selfhosted: ingestion-job lookup for kb %s ds %s failed "
                    "(%s: %s) — detail answers with %d cached job(s), jobs_degraded=true",
                    key[1], key[2], type(exc).__name__, exc, len(jobs),
                )
        now = _jobs_now()
        with _JOBS_CACHE_LOCK:
            for k in [k for k, (ts, _, _) in _JOBS_CACHE.items()
                      if now - ts >= JOBS_CACHE_TTL_SECONDS]:
                _JOBS_CACHE.pop(k, None)
                other = _JOBS_KEY_LOCKS.get(k)
                if k != key and (other is None or not other.locked()):
                    _JOBS_KEY_LOCKS.pop(k, None)  # nobody is building it right now
                    _JOBS_GEN.pop(k, None)
            if _JOBS_GEN.get(key) == gen_before:
                _JOBS_CACHE[key] = (now, jobs, degraded)
            elif log is not None:
                log.debug(
                    "knowledge_selfhosted: ingestion-job list for kb %s ds %s invalidated "
                    "while it was being built — returned to the caller, not cached",
                    key[1], key[2],
                )
    return jobs, degraded
# ── end ingestion-job cache ───────────────────────────────────────────────────────


def new_kb_id() -> str:
    return "KB" + secrets.token_hex(6).upper()


def new_ds_id() -> str:
    return "DS" + secrets.token_hex(5).upper()


def kb_prefix(kb_id: str, ds_id: str) -> str:
    return f"kb/{kb_id}/{ds_id}/"


def kb_prefix_root(kb_id: str) -> str:
    """The KB's own S3 namespace root — the only prefix an `existing` data source
    (ruling R18) may point into, since it is the only place a member can
    legitimately already have documents (`kb_prefix` is a subfolder of this)."""
    return f"kb/{kb_id}/"


def _iso(value: datetime | None) -> str | None:
    """SQLite hands back naive datetimes on a fresh read (round-tripped through a
    new session, e.g. `list_kbs`'s query) while the same-session object the write
    path just set keeps its tzinfo — re-attach UTC before formatting so both read
    the same (`app.services.users.as_utc` is the established pattern for this)."""
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _safe_name(name: str) -> str:
    """The basename only — strips any directory component so an uploaded
    filename can never escape the data source's S3 prefix."""
    base = (name or "file").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return base or "file"


class SelfHostedKnowledgeBackend:
    """`KnowledgeBackend` over S3 + OpenSearch + SQS. The four injection points
    (`s3`, `opensearch`, `sqs`, `embedder`) mirror `SelfHostedMemoryBackend`'s
    contract exactly — tests inject the hermetic fakes from `tests/memory_fakes.py`
    directly, production falls back to the real funnel."""

    def __init__(
        self,
        *,
        s3: Any = None,
        opensearch: Any = None,
        sqs: Any = None,
        embedder: Any = None,
        clock: Any = None,
    ) -> None:
        self._s3_init = s3
        self._os_init = opensearch
        self._sqs_init = sqs
        self._embedder_init = embedder
        self._clock = clock or (lambda: datetime.now(UTC))

    # ── client resolvers (same contract as SelfHostedMemoryBackend) ─────────
    def _now(self) -> datetime:
        return self._clock()

    def _s3(self, ws: WorkspaceContext) -> Any:
        if self._s3_init is None:
            return ws.client("s3")
        if callable(self._s3_init):
            return self._s3_init(ws)
        return self._s3_init

    def _os(self, ws: WorkspaceContext) -> Any:
        if self._os_init is None:
            endpoint = memory_selfhosted.substrate_for(ws).endpoint
            # Request thread: 2 attempts × 10 s read timeout (~21 s worst case on a dead
            # or busy domain), not the bootstrap's 3 × 30 s (~93 s) — N-SCN-obs11.
            return aws_clients.opensearch_http(
                memory_selfhosted.os_ctx(ws), endpoint,
                attempts=opensearch_retry.INTERACTIVE_ATTEMPTS,
                read_timeout=opensearch_retry.INTERACTIVE_READ_TIMEOUT_SECONDS,
            )
        if callable(self._os_init):
            return self._os_init(ws)
        return self._os_init

    def _os_confirm(self, ws: WorkspaceContext) -> Any:
        """The confirm tier (N17-obs236): a single attempt with a short read timeout
        (`attempts=1`, `JOB_CONFIRM_READ_TIMEOUT_SECONDS`) for the ONE confirming `GET`
        after a job PUT raised a transport error, and for the reaper's best-effort
        `FAILED` persist. The tier is its own funnel cache key, hence its own PoolManager:
        a fresh connection may be needed, so its worst case is connect 5 s + read 5 s ≈
        10 s — on top of the ~21 s the interactive budget (2 × 10 s + 1 s backoff) has
        already spent when the PUT failed; not another 2 × 10 s. Built by the funnel like
        `_os`; an injected client (tests) is reused as is."""
        if self._os_init is None:
            endpoint = memory_selfhosted.substrate_for(ws).endpoint
            return aws_clients.opensearch_http(
                memory_selfhosted.os_ctx(ws), endpoint,
                attempts=1, read_timeout=JOB_CONFIRM_READ_TIMEOUT_SECONDS,
            )
        return self._os(ws)

    def _sqs(self, ws: WorkspaceContext) -> Any:
        if self._sqs_init is None:
            return ws.client("sqs")
        if callable(self._sqs_init):
            return self._sqs_init(ws)
        return self._sqs_init

    def _embedder(self) -> Any:
        if self._embedder_init is None:
            # D21: the registry's active embedding row decides the class (Titan / OpenAI-
            # compatible) and the key the hub resolves — never spelled here.
            return embedding_backend.build_embedder()
        if callable(self._embedder_init):
            return self._embedder_init()
        return self._embedder_init

    def _substrate(self, ws: WorkspaceContext) -> memory_selfhosted.MemorySubstrate:
        """The shared Memory/KB substrate, or 503 `kb.not_provisioned` (ruling R16)
        — translates `memory_selfhosted`'s own `memory.not_provisioned` so a caller
        asking about a knowledge base never sees a Memory-flavored error code."""
        try:
            return memory_selfhosted.substrate_for(ws)
        except AppError as exc:
            raise AppError(
                "kb.not_provisioned",
                "self-hosted knowledge bases are not provisioned for this workspace — "
                "deploy the launchpad-memory stack and run its bootstrap",
                {"workspace": ws.id},
                status_code=503,
            ) from exc

    # ── ledger rows ───────────────────────────────────────────────────────────
    def _row(self, ws: WorkspaceContext, kb_id: str) -> KnowledgeBase:
        db = SessionLocal()
        try:
            row = (
                db.query(KnowledgeBase)
                .filter(KnowledgeBase.workspace_id == ws.id, KnowledgeBase.kb_id == kb_id)
                .first()
            )
        finally:
            db.close()
        if row is None:
            raise NotFoundError(
                "kb.not_found", f"knowledge base {kb_id!r} not found", {"kb_id": kb_id}
            )
        return row

    def _ds(self, row: KnowledgeBase, ds_id: str) -> dict[str, Any]:
        for d in row.data_sources:
            if d["ds_id"] == ds_id:
                return d
        raise NotFoundError(
            "kb.data_source_not_found",
            f"data source {ds_id!r} not found on knowledge base {row.kb_id!r}",
            {"kb_id": row.kb_id, "ds_id": ds_id},
        )

    def _source_to_ds(
        self, sub: memory_selfhosted.MemorySubstrate, kb_id: str, source: dict[str, Any],
    ) -> dict[str, Any]:
        mode = source.get("mode") or "upload"
        ds_id = new_ds_id()
        name = source.get("name") or ds_id
        if mode == "existing":
            bucket = source.get("bucket")
            if bucket != sub.bucket:
                raise AppError(
                    "kb.external_bucket_unsupported",
                    "an 'existing' data source must point at the workspace's own "
                    "artifacts bucket (ruling R18) — upload files instead or name "
                    "that bucket",
                    {"bucket": bucket}, status_code=422,
                )
            prefix = source.get("prefix") or ""
            if prefix and not prefix.endswith("/"):
                prefix += "/"
            own_namespace = kb_prefix_root(kb_id)
            if (
                not prefix
                or prefix.startswith("/")
                or ".." in prefix.split("/")
                or not prefix.startswith(own_namespace)
            ):
                raise AppError(
                    "kb.external_prefix_unsupported",
                    "an 'existing' data source's prefix must be inside this knowledge "
                    "base's own S3 namespace, kb/<kb_id>/ (ruling R18) — the extractor "
                    "Lambda's s3:List*/GetObject reach spans the whole artifacts bucket, "
                    "so an unconstrained prefix (e.g. 'memory/' or another KB's prefix) "
                    "would ingest other workspaces' or other KBs' documents; upload files "
                    "instead, or add this data source after placing the files under that "
                    "prefix",
                    {"prefix": prefix}, status_code=422,
                )
        else:
            bucket = sub.bucket
            prefix = kb_prefix(kb_id, ds_id)
        return {
            "ds_id": ds_id, "name": name, "mode": mode, "bucket": bucket, "prefix": prefix,
            "status": "AVAILABLE", "created_at": self._now().isoformat(),
        }

    # ── jobs (OpenSearch, ruling R4) ─────────────────────────────────────────
    def _cached_jobs(
        self, ws: WorkspaceContext, kb_id: str, ds_id: str, size: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """The detail route's view of a data source's jobs — see `cached_jobs`."""
        return cached_jobs(
            (ws.id, kb_id, ds_id),
            lambda: self._reap_never_dispatched(
                ws, self._jobs(ws, kb_id, ds_id, size, partial_ok=False),
            ),
            log=logger,
        )

    def _reap_never_dispatched(
        self, ws: WorkspaceContext, jobs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """N17-obs236: close orphan `STARTING` jobs in a job list about to be presented. A
        job still `STARTING` with empty `statistics` whose `created_at` is older than
        `RUNNING_JOB_STALE_S` was never dispatched (the Lambda's first write is
        `IN_PROGRESS`), so it is presented as `FAILED` / `NEVER_DISPATCHED_REASON` and the
        same is persisted on the document (best-effort, through the confirm tier —
        `_os_confirm`, one attempt / 5 s — so a busy node adds ≤ ~10 s to the detail's
        single-flight rebuild, not the interactive 2 × 10 s; a failed `_update` is one
        WARNING, the presented list is already corrected). A `STARTING` job with counts,
        any `IN_PROGRESS` job, and a `STARTING` job within the bound are left alone — the
        first two are the Lambda's, the last may still be picked up. Returns ``jobs``
        (mutated)."""
        now = self._now()
        for job in jobs:
            status = str(job.get("status") or "").upper()
            if status == "STARTING" and not job.get("statistics"):
                age, reason = job_age(job, now, basis="dispatch"), NEVER_DISPATCHED_REASON
                what = "STARTING with no counts"
            elif status == "IN_PROGRESS":
                # rev-r22 (b): the extractor touches `updated_at` on every document it finishes
                # (N17-obs247 fan-out) and on every status write — a job silent that long is dead
                age, reason = job_age(job, now, basis="progress"), STALLED_REASON
                what = "IN_PROGRESS with no progress"
            else:
                continue
            if not is_stale(age):
                continue
            stamp_now = now.isoformat()
            job["status"] = "FAILED"
            job["failure_reasons"] = [reason]
            job["updated_at"] = stamp_now
            logger.warning(
                "kb ingestion job %s: %s for %.0f s — %s, marked FAILED", job.get("job_id"),
                what, age, "never dispatched" if reason == NEVER_DISPATCHED_REASON else "stalled",
            )
            try:
                self._os_confirm(ws).update_doc(naming.jobs_index(), str(job.get("job_id")), {
                    "status": "FAILED", "failure_reason": reason, "updated_at": stamp_now,
                })
            except Exception as exc:  # noqa: BLE001 — the read must still answer
                logger.warning(
                    "kb ingestion job %s: could not persist the %s FAILED "
                    "status (%s) — presented as FAILED, retried on the next read",
                    job.get("job_id"),
                    "never_dispatched" if reason == NEVER_DISPATCHED_REASON else "stalled",
                    type(exc).__name__,
                )
        return jobs

    def _jobs(
        self, ws: WorkspaceContext, kb_id: str, ds_id: str, size: int, *,
        partial_ok: bool = True,
    ) -> list[dict[str, Any]]:
        """The data source's jobs, newest first. With ``partial_ok=False`` (the cached
        detail path) an answer OpenSearch cut short at `JOBS_SEARCH_TIMEOUT`
        (`timed_out: true`) raises `JobsSearchTimedOut` instead of being returned as if
        it were complete; the live ingestion-jobs route keeps the partial hits."""
        body = {
            "query": {"bool": {"filter": [
                {"term": {"kind": "kb_ingest"}},
                {"term": {"kb_id": kb_id}},
                {"term": {"ds_id": ds_id}},
            ]}},
            "sort": [{"created_at": {"order": "desc"}}],
            "size": size,
            # OpenSearch's own per-shard budget (N-SCN-obs11): a busy node answers with
            # what it has (`timed_out: true`) instead of holding the request thread.
            "timeout": JOBS_SEARCH_TIMEOUT,
        }
        result = self._os(ws).search(naming.jobs_index(), body)
        if result.get("timed_out") and not partial_ok:
            raise JobsSearchTimedOut(
                f"launchpad-jobs search for kb {kb_id} ds {ds_id} timed out after "
                f"{JOBS_SEARCH_TIMEOUT} ({len(result['hits']['hits'])} partial hit(s))"
            )
        out: list[dict[str, Any]] = []
        for h in result["hits"]["hits"]:
            src = h["_source"]
            status = src.get("status")
            failure_reason = src.get("failure_reason")
            if str(status or "").upper() in RUNNING_JOB_STATUSES:
                # N17-obs236 (rev-r21 nit 4): a message delivered AFTER the 30-min reap flips
                # the document FAILED → IN_PROGRESS while `failure_reason` lingers until the
                # terminal write clears it — a running job never shows a failure reason.
                failure_reason = None
            out.append({
                "job_id": src.get("job_id", h["_id"]),
                "status": status,
                "started_at": src.get("created_at"),
                "updated_at": src.get("updated_at"),
                "statistics": src.get("counts") or {},
                "failure_reasons": [failure_reason] if failure_reason else [],
            })
        return out

    # ── shapes ────────────────────────────────────────────────────────────────
    def _summary(self, ws: WorkspaceContext, row: KnowledgeBase) -> dict[str, Any]:
        return {
            "kb_id": row.kb_id, "name": row.name, "description": row.description,
            "status": row.status, "updated_at": _iso(row.updated_at),
            "data_source_count": len(row.data_sources or []),
            "attached_agents": self.attached_agents(ws, row.kb_id),
            # who created it through the console (nulls on legacy / preset-installed rows)
            **creators.fields(row),
        }

    def _has_objects(self, ws: WorkspaceContext, ds: dict[str, Any]) -> bool | None:
        """Whether at least one object other than a `_text/` sidecar sits under the data
        source's prefix (N17-obs204 — the console's automatic first sync runs only then; a
        sync over an empty prefix is three zeros nobody asked for). One short
        `list_objects_v2` page, strongly consistent on S3, so a just-finished upload flips
        it on the very next poll; a `ClientError` (bucket gone, access denied) answers
        `None` with one WARNING — never a failed detail."""
        bucket, prefix = ds.get("bucket"), ds.get("prefix") or ""
        if not bucket:
            return None
        sidecar = f"{prefix}{TEXT_SIDECAR_DIR}/"
        s3 = self._s3(ws)
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": HAS_OBJECTS_PAGE}
        try:
            while True:
                page = s3.list_objects_v2(**kwargs)
                if any(not o["Key"].startswith(sidecar) for o in page.get("Contents", [])):
                    return True
                if not page.get("IsTruncated"):
                    return False
                kwargs["ContinuationToken"] = page["NextContinuationToken"]
        except (ClientError, BotoCoreError) as exc:
            response = getattr(exc, "response", None) or {}
            code = response.get("Error", {}).get("Code") or type(exc).__name__
            logger.warning(
                "kb ds %s (%s): has_objects listing failed (%s) — reported as unknown",
                ds.get("ds_id"), prefix, code,
            )
            return None

    def _detail(self, ws: WorkspaceContext, row: KnowledgeBase) -> dict[str, Any]:
        data_sources = []
        any_degraded = False
        for ds in row.data_sources or []:
            jobs, degraded = self._cached_jobs(ws, row.kb_id, ds["ds_id"], size=5)
            any_degraded = any_degraded or degraded
            data_sources.append({
                **ds,
                "ingestion_jobs": jobs,
                "jobs_degraded": degraded,
                "has_objects": self._has_objects(ws, ds),
                "failure_reasons": [],
            })
        return {
            "kb_id": row.kb_id, "name": row.name, "description": row.description,
            "status": row.status, "arn": None,
            "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
            "failure_reasons": list(row.failure_reasons or []),
            "data_sources": data_sources,
            "jobs_degraded": any_degraded,
            "attached_agents": self.attached_agents(ws, row.kb_id),
            **creators.fields(row),
        }

    # ── CRUD ──────────────────────────────────────────────────────────────────
    def list_kbs(self, ws: WorkspaceContext) -> list[dict[str, Any]]:
        if not memory_selfhosted.has_substrate(ws):
            return []
        db = SessionLocal()
        try:
            rows = (
                db.query(KnowledgeBase)
                .filter(KnowledgeBase.workspace_id == ws.id)
                .order_by(KnowledgeBase.created_at)
                .all()
            )
        finally:
            db.close()
        return [self._summary(ws, row) for row in rows]

    def get_kb_detail(self, ws: WorkspaceContext, kb_id: str) -> dict[str, Any]:
        return self._detail(ws, self._row(ws, kb_id))

    def create_kb(
        self, ws: WorkspaceContext, name: str, description: str, source: dict[str, Any],
        *, creator: creators.Creator | None = None,
    ) -> dict[str, Any]:
        sub = self._substrate(ws)  # 503 before any ledger write if not provisioned
        db = SessionLocal()
        try:
            existing = (
                db.query(KnowledgeBase)
                .filter(KnowledgeBase.workspace_id == ws.id, KnowledgeBase.name == name)
                .first()
            )
            if existing is not None:
                raise AppError(
                    "kb.name_exists", f"a knowledge base named {name!r} already exists",
                    {"name": name}, status_code=409,
                )
            kb_id = new_kb_id()
            ds = self._source_to_ds(sub, kb_id, source)
            now = self._now()
            status = "ACTIVE"
            failure_reasons: list[str] = []
            try:
                # WB-007: the race-safe ensure — an index another creator (or a stale
                # HEAD) made first is "ensured" when its live mapping is compatible,
                # never FAILED; a conflicting one raises `IndexIncompatible`, which is
                # an `OpenSearchError` and lands in `failure_reasons` below.
                # D21: the k-NN dimension comes from the registry's active embedding row
                # at creation; an existing index keeps its mapping (the guard protects it)
                opensearch_ensure.ensure_index(
                    self._os(ws), naming.kb_index(kb_id),
                    embedding_backend.vector_index_body(core.kb_index_body()),
                )
            except core.OpenSearchError as exc:
                status = "FAILED"
                failure_reasons = [str(exc)]
            # One commit (INSERT) with the final status already decided: a second
            # UPDATE that re-sets `updated_at` to the SAME clock value SQLAlchemy
            # already holds would not register as a change, letting the column's
            # `onupdate=_now` default (real wall-clock) win over the test clock.
            row = KnowledgeBase(
                kb_id=kb_id, workspace_id=ws.id, name=name, description=description or "",
                status=status, failure_reasons=failure_reasons, data_sources=[ds],
                created_at=now, updated_at=now,
            )
            creators.stamp(row, creator)
            db.add(row)
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise AppError(
                    "kb.name_exists", f"a knowledge base named {name!r} already exists",
                    {"name": name}, status_code=409,
                ) from exc
            return self._detail(ws, row)
        finally:
            db.close()

    def update_description(
        self, ws: WorkspaceContext, kb_id: str, description: str,
    ) -> dict[str, Any]:
        db = SessionLocal()
        try:
            row = (
                db.query(KnowledgeBase)
                .filter(KnowledgeBase.workspace_id == ws.id, KnowledgeBase.kb_id == kb_id)
                .first()
            )
            if row is None:
                raise NotFoundError(
                    "kb.not_found", f"knowledge base {kb_id!r} not found", {"kb_id": kb_id}
                )
            row.description = description or ""
            row.updated_at = self._now()
            db.commit()
            return self._detail(ws, row)
        finally:
            db.close()

    def delete_kb(
        self, ws: WorkspaceContext, kb_id: str, force: bool = False,
    ) -> dict[str, Any]:
        db = SessionLocal()
        try:
            # Ledger guards first (N1 contract note): a preset mount is never
            # deletable, and an ordinary mount 409s unless forced — both are pure
            # ledger reads that must run before any AWS call.
            knowledge_backend.refuse_if_attached_to_system_agent(db, ws, kb_id)
            names = knowledge_backend.attached_agent_names(db, ws, kb_id)
            if names and not force:
                raise knowledge_backend.KBAttachedError(kb_id, names)
            row = (
                db.query(KnowledgeBase)
                .filter(KnowledgeBase.workspace_id == ws.id, KnowledgeBase.kb_id == kb_id)
                .first()
            )
            if row is None:
                raise NotFoundError(
                    "kb.not_found", f"knowledge base {kb_id!r} not found", {"kb_id": kb_id}
                )
            # Resolved before the row goes: a 503 `kb.not_provisioned` must never fire
            # after the commit (pure settings read, no AWS call).
            bucket = self._substrate(ws).bucket
            row.status = "DELETING"
            db.commit()
            db.delete(row)
            db.commit()
        finally:
            db.close()
        invalidate_jobs_cache(ws.id, kb_id)
        # WB-008: the row is gone, so every storage step below is best-effort by
        # contract — `run_steps` catches *everything* (OpenSearchError, a urllib3
        # timeout re-raised by `RetryingHttp`, a botocore error from the S3 purge) and
        # the steps still pending go to the retriable `kb_cleanup` job the response
        # names, instead of surfacing as a 502 on a delete that already happened.
        cleanup, warnings = kb_cleanup.run_steps(self, ws, kb_id, bucket, kb_cleanup.STEPS)
        pending = [s for s in kb_cleanup.STEPS if cleanup[s] != kb_cleanup.OK]
        job_id = None
        if pending:
            job_id = kb_cleanup.schedule_cleanup(self, ws, kb_id, bucket, pending, warnings)
        return {
            "kb_id": kb_id,
            "deleted": True,
            "cleanup": cleanup,
            "cleanup_warnings": warnings,
            "cleanup_job_id": job_id,
        }

    def run_cleanup_step(
        self, ws: WorkspaceContext, kb_id: str, bucket: str, step: str,
        scope: str = kb_cleanup.SCOPE_KB, ds_id: str | None = None,
        prefix: str | None = None,
    ) -> None:
        """One idempotent storage-cleanup step (`kb_cleanup.STEPS`). KB scope: ``s3``
        purges `kb/<kb_id>/`, ``index`` drops the KB's own index (404 tolerated), ``docs``
        deletes its rows from the fixed kb-docs + jobs indices. Data-source scope (WB-010,
        `ds_id` + `prefix`): ``s3`` purges the source's prefix, ``index`` deletes the
        source's chunks from the KB index (a missing KB index — a FAILED KB — counts as
        done), ``docs`` deletes its rows from kb-docs. Raises on failure —
        `kb_cleanup.run_steps` is the catcher, here and on the background retries."""
        if scope == kb_cleanup.SCOPE_DATA_SOURCE:
            if not ds_id:
                raise ValueError("data_source scope needs a ds_id")
            if step == kb_cleanup.STEP_S3:
                if not prefix or not prefix.startswith(f"kb/{kb_id}/"):
                    raise ValueError(f"refusing to purge prefix {prefix!r} outside kb/{kb_id}/")
                self._purge_prefix(ws, bucket, prefix)
            elif step == kb_cleanup.STEP_INDEX:
                try:
                    self._os(ws).delete_by_query(naming.kb_index(kb_id), {"term": {"ds_id": ds_id}})
                except core.OpenSearchError as exc:
                    if exc.status != 404:
                        raise
            elif step == kb_cleanup.STEP_DOCS:
                self._os(ws).delete_by_query(naming.kb_docs_index(), {"term": {"ds_id": ds_id}})
                # N17-obs276: the extractor's per-document rows of this source in the jobs
                # index — fan-out result rows (obs247) and slice leases (obs274) — go with it
                # (they carry `ds_id`; the KB-scope sweep removes them by `kb_id`). The
                # ingestion jobs themselves (`kind: kb_ingest`) stay as before.
                self._os(ws).delete_by_query(naming.jobs_index(), {"bool": {"filter": [
                    {"term": {"kb_id": kb_id}}, {"term": {"ds_id": ds_id}},
                    {"terms": {"kind": list(EXTRACTOR_DOC_ROW_KINDS)}},
                ]}})
            else:
                raise ValueError(f"unknown cleanup step {step!r}")
            return
        if step == kb_cleanup.STEP_S3:
            self._purge_prefix(ws, bucket, f"kb/{kb_id}/")
        elif step == kb_cleanup.STEP_INDEX:
            self._os(ws).delete_index(naming.kb_index(kb_id))
        elif step == kb_cleanup.STEP_DOCS:
            os_client = self._os(ws)
            for index in (naming.kb_docs_index(), naming.jobs_index()):
                os_client.delete_by_query(index, {"term": {"kb_id": kb_id}})
        else:
            raise ValueError(f"unknown cleanup step {step!r}")

    # ── data sources ──────────────────────────────────────────────────────────
    def add_data_source(
        self, ws: WorkspaceContext, kb_id: str, source: dict[str, Any],
    ) -> dict[str, Any]:
        """`POST …/data-sources`. Mode `upload` is idempotent per KB (N17-obs243, live
        2026-10-22: seven clicks on 补建数据源 for KB `test` created seven empty upload
        sources, each later synced COMPLETE with 0 scanned): a KB that already has an
        upload-mode source gets THAT source back, `created: false`; otherwise one is created
        under the same per-KB lock the upload's `_ensure_upload_source` takes, so concurrent
        clicks and a concurrent first upload serialize onto one source. Mode `existing` keeps
        appending (several member-named prefixes per KB are allowed) and always answers
        `created: true`. The router turns `created` into 201 / 200."""
        if (source.get("mode") or "upload") == "upload":
            _row, ds, created = self._ensure_upload_source(ws, kb_id)
        else:
            _row, ds = self._append_source(ws, kb_id, source)
            created = True
        return {**ds, "created": created}

    def _append_source(
        self, ws: WorkspaceContext, kb_id: str, source: dict[str, Any],
    ) -> tuple[KnowledgeBase, dict[str, Any]]:
        """The one ledger write that adds a data source to a KB — `add_data_source`
        (`POST …/data-sources`) and the upload's `_ensure_upload_source` (N17-obs242) both
        build the record through `_source_to_ds` (same id format, same `kb/<kb_id>/<ds_id>/`
        prefix for mode `upload`) and append it to the row's `data_sources`."""
        sub = self._substrate(ws)
        db = SessionLocal()
        try:
            row = (
                db.query(KnowledgeBase)
                .filter(KnowledgeBase.workspace_id == ws.id, KnowledgeBase.kb_id == kb_id)
                .first()
            )
            if row is None:
                raise NotFoundError(
                    "kb.not_found", f"knowledge base {kb_id!r} not found", {"kb_id": kb_id}
                )
            ds = self._source_to_ds(sub, kb_id, source)
            row.data_sources = [*row.data_sources, ds]
            row.updated_at = self._now()
            db.commit()
            return row, ds
        finally:
            db.close()

    def _ensure_upload_source(
        self, ws: WorkspaceContext, kb_id: str,
    ) -> tuple[KnowledgeBase, dict[str, Any], bool]:
        """The KB row and its upload-mode data source, created when the KB has none
        (N17-obs242, user report 2026-10-21: the user removed the only upload source of
        KB `test`, and 新增数据源 → 上传文件 — whose copy promises "uploaded to the artifacts
        bucket and configured as a data source automatically" — answered
        `kb.no_upload_source`; recreating the KB was the only way to upload again). The
        self-hosted upload source is nothing but a prefix record in the artifacts bucket,
        exactly what `add_data_source({"mode": "upload"})` writes, so the upload writes it
        through the same path. Two concurrent first uploads serialize on the per-KB lock and
        the second re-reads the row, so a KB never ends up with two upload sources from one
        race. `add_data_source({"mode": "upload"})` goes through here too (N17-obs243), so a
        repeated or concurrent 补建 click returns the existing source instead of appending.
        Returns `(row, source, created)`."""
        row = self._row(ws, kb_id)
        target = next((d for d in row.data_sources if d["mode"] == "upload"), None)
        if target is not None:
            return row, target, False
        with _upload_source_lock(ws.id, kb_id):
            row = self._row(ws, kb_id)  # a concurrent first upload may have created it
            target = next((d for d in row.data_sources if d["mode"] == "upload"), None)
            if target is not None:
                return row, target, False
            row, target = self._append_source(ws, kb_id, {"mode": "upload"})
            logger.info(
                "kb %s: no upload-mode data source — created %s (%s) for the upload",
                kb_id, target["ds_id"], target["prefix"],
            )
            return row, target, True

    def delete_data_source(
        self, ws: WorkspaceContext, kb_id: str, ds_id: str,
    ) -> dict[str, Any]:
        db = SessionLocal()
        try:
            row = (
                db.query(KnowledgeBase)
                .filter(KnowledgeBase.workspace_id == ws.id, KnowledgeBase.kb_id == kb_id)
                .first()
            )
            if row is None:
                raise NotFoundError(
                    "kb.not_found", f"knowledge base {kb_id!r} not found", {"kb_id": kb_id}
                )
            ds = self._ds(row, ds_id)
            row.data_sources = [d for d in row.data_sources if d["ds_id"] != ds_id]
            row.updated_at = self._now()
            db.commit()
        finally:
            db.close()
        invalidate_jobs_cache(ws.id, kb_id, ds_id)
        # WB-010: same best-effort contract as `delete_kb` — the row has already lost the
        # data source, so every storage step is wrapped and what is still pending goes
        # to a `kb_cleanup` job scoped to this data source. An `existing`-mode source
        # owns no objects: its s3 step is `skipped`, never run.
        target = {"scope": kb_cleanup.SCOPE_DATA_SOURCE, "ds_id": ds_id, "prefix": ds["prefix"]}
        steps = [s for s in kb_cleanup.STEPS
                 if s != kb_cleanup.STEP_S3 or ds["mode"] == "upload"]
        cleanup, warnings = kb_cleanup.run_steps(self, ws, kb_id, ds["bucket"], steps, target)
        if kb_cleanup.STEP_S3 not in steps:
            cleanup[kb_cleanup.STEP_S3] = kb_cleanup.SKIPPED
        cleanup = {s: cleanup[s] for s in kb_cleanup.STEPS}
        pending = [s for s in steps if cleanup[s] != kb_cleanup.OK]
        job_id = None
        if pending:
            job_id = kb_cleanup.schedule_cleanup(
                self, ws, kb_id, ds["bucket"], pending, warnings, target=target,
            )
        return {
            "ds_id": ds_id,
            "deleted": True,
            "cleanup": cleanup,
            "cleanup_warnings": warnings,
            "cleanup_job_id": job_id,
        }

    def _purge_prefix(self, ws: WorkspaceContext, bucket: str, prefix: str) -> None:
        """The `s3` cleanup step through the shared reporting deleter (N17-obs119): a key S3
        refused inside a 200 `DeleteObjects` answer (`Errors[]`) raises `S3SweepResidue`, so
        `kb_cleanup.run_steps` records the step `pending` with a redacted warning and the
        cleanup job re-runs it (WB-008) instead of calling the prefix purged."""
        s3_sweep.delete_prefix(self._s3(ws), bucket, prefix).raise_for_residue()

    # ── uploads ───────────────────────────────────────────────────────────────
    def upload_files(
        self, ws: WorkspaceContext, kb_id: str, files: list[tuple[str, bytes]],
    ) -> dict[str, Any]:
        # N17-obs242: a KB without an upload-mode source gets one here (the dialog copy
        # promises "configured as a data source automatically"); the response names it.
        row, target, created = self._ensure_upload_source(ws, kb_id)
        s3 = self._s3(ws)
        keys: list[str] = []
        for name, data in files:
            safe = _safe_name(name)
            key = f"{target['prefix']}{safe}"
            s3.put_object(
                Bucket=target["bucket"], Key=key, Body=data,
                ContentType=mimetypes.guess_type(name)[0] or "application/octet-stream",
            )
            if safe.lower().endswith(".pdf"):
                try:
                    text = "\n\n".join(
                        (p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages
                    )
                except Exception:  # a corrupt/encrypted PDF must not fail the upload
                    text = ""
                s3.put_object(
                    Bucket=target["bucket"],
                    Key=f"{target['prefix']}{TEXT_SIDECAR_DIR}/{safe}.txt",
                    Body=text.encode(), ContentType="text/plain",
                )
            keys.append(key)
        # N17-obs204: every put landed — the server starts the ingestion itself, so a file
        # is never stored without a sync that lists it (the console used to fire the first
        # sync on its own schedule and could scan the prefix before the files arrived).
        # rev-r19: the comparison point is the LAST put — a job created between two puts may
        # have listed the prefix before the later files landed.
        upload_finished_at = self._now().isoformat()
        job, sync_error = self._sync_after_upload(ws, row, target, upload_finished_at)
        out: dict[str, Any] = {"keys": keys, "job": job}
        if sync_error:
            out["sync_error"] = sync_error  # rev-r19: type only, never a message body
        if created:
            out["data_source"] = dict(target)  # additive: only when this upload created it
        return out

    def _sync_after_upload(
        self, ws: WorkspaceContext, row: KnowledgeBase, ds: dict[str, Any],
        upload_finished_at: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """The sync a successful upload starts on its data source — the same code path as
        `start_sync`, with one rule in front (N17-obs204): a job of this source that is
        still running is returned instead of a second one being enqueued (the Lambda
        scans the whole prefix anyway, and two concurrent ingests would race on the same
        document records). `covers_upload` says whether that coalesced job is known to
        list these files: only a job created after the LAST put landed can; one that started
        before it may already have listed the prefix — "a sync started before this upload
        may not cover it; the next upload or a manual sync does". A failure here (the jobs
        search, the job write, SQS) is logged and answered `None`: the upload itself
        succeeded and must stay a 200, the caller syncs by hand."""
        try:
            try:
                running = self._running_job(ws, row.kb_id, ds["ds_id"])
            except JobsSearchTimedOut:
                # rev-r19 nit 5: a partial list could hide the running job; "cannot decide"
                # enqueues (one duplicate ingest is benign — the Lambda scans the whole prefix)
                running = None
            if running is not None:
                started = running.get("started_at") or ""
                covers = bool(started) and started >= upload_finished_at
                return {
                    "job_id": running["job_id"], "status": running["status"],
                    "coalesced": True, "covers_upload": covers,
                }, None
            out = self._enqueue_sync(ws, row.kb_id, ds)
            return {**out, "coalesced": False, "covers_upload": True}, None
        except Exception as exc:  # noqa: BLE001 — the upload is done; the sync is best-effort
            logger.warning(
                "kb %s ds %s: sync after upload could not start (%s) — manual sync needed",
                row.kb_id, ds["ds_id"], type(exc).__name__,
            )
            return None, type(exc).__name__

    def _running_job(self, ws: WorkspaceContext, kb_id: str, ds_id: str) -> dict[str, Any] | None:
        """The newest job of the data source still in `RUNNING_JOB_STATUSES`, from a live
        (uncached, complete — `partial_ok=False`) search, else None. rev-r19 nit 2: a job
        whose `updated_at` is older than `RUNNING_JOB_STALE_S` is an orphan (SQS failed after
        the job write, or the Lambda died) and never coalesces — otherwise every later upload
        would hang off it forever."""
        now = self._now()
        for job in self._jobs(ws, kb_id, ds_id, size=5, partial_ok=False):
            if str(job.get("status") or "").upper() not in RUNNING_JOB_STATUSES:
                continue
            age = job_age(job, now, basis="last_write")
            if is_stale(age):
                logger.warning(
                    "kb %s ds %s: ignoring stale running job %s (%.0f s old) — a new sync starts",
                    kb_id, ds_id, job.get("job_id"), age,
                )
                continue
            return job
        return None

    # ── ingestion ─────────────────────────────────────────────────────────────
    def start_sync(
        self, ws: WorkspaceContext, kb_id: str, ds_id: str, *, full: bool = False,
    ) -> dict[str, Any]:
        row = self._row(ws, kb_id)
        ds = self._ds(row, ds_id)
        if full:
            # N17-obs275 (rev-r22): a full rebuild re-embeds the whole source — never start a
            # second one (or one beside a running incremental sync) while a job of this source
            # is still running: repeated clicks, or an API caller past the confirm dialog, would
            # otherwise pay for N concurrent rebuilds. A stale running job (RUNNING_JOB_STALE_S)
            # does not block; a timed-out jobs search cannot rule a running job out → refused
            # too (a rebuild is never urgent). The incremental sync keeps today's behaviour.
            try:
                running = self._running_job(ws, kb_id, ds_id)
            except JobsSearchTimedOut:
                running = {"job_id": None}
            if running is not None:
                raise AppError(
                    "kb.sync_in_progress",
                    "A sync of this data source is already running — wait for it to finish "
                    "before starting a full rebuild.",
                    {"job_id": running.get("job_id")}, status_code=409,
                )
        return self._enqueue_sync(ws, kb_id, ds, full=full)

    def _enqueue_sync(
        self, ws: WorkspaceContext, kb_id: str, ds: dict[str, Any], *, full: bool = False,
    ) -> dict[str, Any]:
        """Seed the `STARTING` job document and hand the prefix to the extractor Lambda —
        the one ingestion path behind `start_sync` and the sync an upload starts.

        Ordering and the two failure seams (N17-obs236, user report 2026-10-21): the job
        PUT (`refresh=true`) goes first, then the SQS message. (a) When the PUT raises a
        transport error (urllib3 read timeout after the retry budget — the busy t3.small
        node had in fact APPLIED the first PUT and only the ack was late) the hub GETs the
        document once with a short timeout: present → the write succeeded, the sync goes
        on to SQS and returns normally; absent, or the GET itself fails → the original
        error is re-raised (nothing is known to be written; the 502 `kb.unavailable` is
        truthful). (b) When `send_message` raises after the document exists, the document
        is marked `FAILED` / `enqueue_failed: <exception class>` best-effort and the
        original error still propagates — a `STARTING` job no worker will ever pick up is
        never left behind on purpose; the ones left by an older hub are closed by
        `_reap_never_dispatched`.

        ``full`` (N17-obs272) puts ``"full": true`` in the planner message: the Lambda then
        re-chunks and re-embeds every document instead of skipping the unchanged ones
        (N17-obs246). Absent by default — an ordinary sync stays incremental."""
        sub = self._substrate(ws)
        ds_id = ds["ds_id"]
        job_id = "ij" + secrets.token_hex(8)
        now = self._now().isoformat()
        try:
            self._os(ws).put_doc(naming.jobs_index(), job_id, {
                "job_id": job_id, "kind": "kb_ingest", "kb_id": kb_id, "ds_id": ds_id,
                "workspace_id": ws.id, "status": "STARTING", "counts": {},
                "created_at": now, "updated_at": now,
            })
        except opensearch_retry.TRANSPORT_EXCEPTIONS as exc:
            if not self._job_doc_written(ws, job_id, exc):
                raise
        invalidate_jobs_cache(ws.id, kb_id, ds_id)  # the next detail poll sees STARTING
        message: dict[str, Any] = {
            "kind": "kb_ingest", "workspace_id": ws.id, "kb_id": kb_id,
            "ds_id": ds_id, "job_id": job_id, "bucket": ds["bucket"],
            "prefix": ds["prefix"], "index": naming.kb_index(kb_id),
            # D21: the embedding row the Lambda must use (= the one the hub queries)
            "embedding": embedding_backend.message_block(),
        }
        if full:
            message["full"] = True  # N17-obs272: the forced full rebuild
        try:
            self._sqs(ws).send_message(QueueUrl=sub.queue_url, MessageBody=json.dumps(message))
        except Exception as exc:
            self._fail_job_soft(ws, job_id, f"enqueue_failed: {type(exc).__name__}")
            invalidate_jobs_cache(ws.id, kb_id, ds_id)  # … and sees FAILED, not STARTING
            raise
        return {"job_id": job_id, "status": "STARTING"}

    def _job_doc_written(self, ws: WorkspaceContext, job_id: str, exc: BaseException) -> bool:
        """After the job PUT raised ``exc`` (a transport error): whether the document is
        nevertheless there — one short `GET` (`_os_confirm`). ``False`` when it is absent
        OR the confirmation itself failed (then nothing is known to be written and the
        caller re-raises the original error)."""
        try:
            found = self._os_confirm(ws).get_doc(naming.jobs_index(), job_id) is not None
        except Exception as confirm_exc:  # noqa: BLE001 — the original error is what matters
            logger.warning(
                "kb ingestion job %s: job write raised %s and the confirming GET failed too "
                "(%s) — the sync is reported failed", job_id, type(exc).__name__,
                type(confirm_exc).__name__,
            )
            return False
        if found:
            logger.warning(
                "kb ingestion job %s: job write raised %s but the document is present — the "
                "write was applied, the sync goes on", job_id, type(exc).__name__,
            )
        return found

    def _fail_job_soft(self, ws: WorkspaceContext, job_id: str, reason: str) -> None:
        """Best-effort `FAILED` on a job document the hub wrote but could not hand to the
        worker (`enqueue_failed: …`) — logs, never raises; the caller re-raises its own
        error."""
        try:
            self._os(ws).update_doc(naming.jobs_index(), job_id, {
                "status": "FAILED", "failure_reason": reason,
                "updated_at": self._now().isoformat(),
            })
        except Exception as mark_exc:  # noqa: BLE001
            logger.warning(
                "kb ingestion job %s: could not mark the undispatched job FAILED (%s) — the "
                "stale-STARTING reaper closes it", job_id, type(mark_exc).__name__,
            )

    def list_ingestion_jobs(
        self, ws: WorkspaceContext, kb_id: str, ds_id: str,
    ) -> list[dict[str, Any]]:
        row = self._row(ws, kb_id)
        self._ds(row, ds_id)
        return self._reap_never_dispatched(ws, self._jobs(ws, kb_id, ds_id, size=20))

    def list_documents(
        self, ws: WorkspaceContext, kb_id: str, ds_id: str, *,
        page_size: int = DOC_PAGE, token: str | None = None,
    ) -> dict[str, Any]:
        row = self._row(ws, kb_id)
        self._ds(row, ds_id)
        from_ = int(token or 0)
        body = {
            "query": {"bool": {"filter": [
                {"term": {"kb_id": kb_id}}, {"term": {"ds_id": ds_id}},
            ]}},
            "sort": [{"name": {"order": "asc"}}],
            "from": from_, "size": page_size,
        }
        result = self._os(ws).search(naming.kb_docs_index(), body)
        hits = result["hits"]["hits"]
        total = result["hits"]["total"]["value"]
        documents = [
            {
                "name": h["_source"].get("name"), "uri": h["_source"].get("uri"),
                **self._doc_status(h["_source"]),
                "indexed_at": h["_source"].get("indexed_at"),
                "size_bytes": h["_source"].get("size_bytes"),
                "uploaded_at": h["_source"].get("uploaded_at"),
            }
            for h in hits
        ]
        next_tok = str(from_ + page_size) if from_ + page_size < total else None
        return {"documents": documents, "next_token": next_tok}

    def _doc_status(self, source: dict[str, Any]) -> dict[str, Any]:
        """`status` / `status_reason` of one kb-docs record as listed. N17-obs274 pass 4: the
        extractor writes `INDEXING` at a document's slice 0 and `INDEXED` at its last slice;
        an `INDEXING` record older than `RUNNING_JOB_STALE_S` belongs to a chain that died
        (DLQ, repeated timeouts) and is PRESENTED as `FAILED` / `INDEXING_INTERRUPTED_REASON`
        — presentation only, the stored record is untouched (the next sync re-does it either
        way: anything but INDEXED is "changed")."""
        status, reason = source.get("status"), source.get("status_reason")
        if status == "INDEXING" and is_stale(
                stamp_age(source.get("indexing_started_at"), self._now())):
            status, reason = "FAILED", INDEXING_INTERRUPTED_REASON
        return {"status": status, "status_reason": reason}

    # ── retrieval playground (ruling R10 — k-NN only) ───────────────────────
    def query(
        self, ws: WorkspaceContext, kb_id: str, text: str, number_of_results: int = 8,
    ) -> list[dict[str, Any]]:
        row = self._row(ws, kb_id)
        if row.status != "ACTIVE":
            raise AppError(
                "kb.not_active",
                f"knowledge base {kb_id!r} is {row.status or 'in an unknown state'}, not ACTIVE",
                {"kb_id": kb_id, "status": row.status}, status_code=409,
            )
        vector = self._embedder().embed(text)
        hits = self._os(ws).knn_search(
            naming.kb_index(kb_id), vector, number_of_results, [{"term": {"kb_id": kb_id}}],
        )
        return [
            {
                "text": h.get("text"), "score": h.get("_score"),
                "location_uri": h.get("doc_uri"),
                "metadata": {"doc_name": h.get("doc_name"), "chunk_index": h.get("chunk_index")},
            }
            for h in hits
        ]

    # ── ledger-side guards (delegate to the pure helpers; N1 contract note) ──
    def attached_agents(self, ws: WorkspaceContext, kb_id: str) -> list[str]:
        db = SessionLocal()
        try:
            return knowledge_backend.attached_agent_names(db, ws, kb_id)
        finally:
            db.close()

    def refuse_if_attached_to_system_agent(self, ws: WorkspaceContext, kb_id: str) -> None:
        db = SessionLocal()
        try:
            knowledge_backend.refuse_if_attached_to_system_agent(db, ws, kb_id)
        finally:
            db.close()
