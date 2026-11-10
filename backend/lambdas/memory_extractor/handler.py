"""Launchpad memory-extractor (N6). One Lambda, two message kinds:

  memory_extract — read one turn event (+ up to N previous events of the session) from
                   S3, retrieve the actor's nearest EXISTING facts / preferences as
                   candidates, ask GLM for per-item operations (add / update / supersede /
                   delete / noop — merge strategy v2, r14-a) plus a session summary, embed
                   with the hub's active embedding row, dedupe (cosine >= 0.92), apply the
                   ops to this environment's memory index (`launchpad-memory` /
                   `launchpad-<env>-memory`, D17), then run the bounded consolidation pass
                   (per-actor caps, soft TTL) and the rolling actor summary; record the job
                   in its jobs index (idempotent per event key);
  kb_ingest      — list a data-source prefix, parse supported text, chunk, embed,
                   bulk-index into the KB's index, record per-document status in
                   the kb-docs index and progress on the seeded job doc.

Credentials: the Global Bedrock bearer is resolved from the AgentCore Identity
API-key provider's backing secret (docs/cn/interfaces/model-credential.md ->
"How N6 reuses it", ruling R1). Never logged, never written to a job doc.

This module is bundled alone (`Code.from_asset`, no package install step) and must
only import stdlib + boto3/botocore + its sibling `opensearch_http.py` — never
`app.*`.
"""

import hashlib
import html
import json
import logging
import math
import os
import re
import time
import traceback
import uuid
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from io import BytesIO
from typing import Any
from urllib.error import HTTPError

import boto3
import urllib3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from opensearch_http import (
    ACTOR_SUMMARY_MAX_WORDS,
    ACTOR_SUMMARY_SOURCES,
    CONSOLIDATE_MAX_RECORDS,
    DEDUPE_COSINE,
    EMBED_DIMENSIONS,
    EMBED_MAX_CHARS,
    EXTRACTED_STRATEGIES,
    INDEX_ENV_VAR,
    MERGE_GRAY_ZONE_LOW,
    OpenSearchError,
    OpenSearchHttp,
    TitanEmbedder,
    active_record_filter,
    actor_summary_namespace,
    chunk_text,
    cosine,
    jobs_index,
    kb_docs_index,
    memory_index,
    normalize_merge_settings,
    parse_event_key,
    summary_record_id,
    ttl_cutoff_filter,
)
from runtime_invoke import backoff_for, is_upstream_transient  # the shared predicate (sibling copy)

# D17: this Lambda belongs to one EnvName environment (the memory stack sets
# LAUNCHPAD_ENV_NAME); its three fixed indices are that environment's on the shared
# domain. Per-KB indices arrive in each kb_ingest message (`index`), already named by
# the hub.
ENV_NAME = os.environ.get(INDEX_ENV_VAR, "")
MEMORY_INDEX = memory_index(ENV_NAME)
JOBS_INDEX = jobs_index(ENV_NAME)
KB_DOCS_INDEX = kb_docs_index(ENV_NAME)

EXTRACT_MODEL_ID = os.environ.get("LAUNCHPAD_EXTRACT_MODEL_ID", "global.zai.glm-5.3")
EMBED_MODEL_ID = os.environ.get("LAUNCHPAD_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
# D21 (M1-d): every message the hub sends carries `embedding: {model_id, kind, base_url,
# api_model_name, credential_provider, dimension, max_chars}` — the registry's ACTIVE
# embedding row at send time (`services/embedding_backend.message_block`). `embedder_for`
# builds the embedder from it (Titan through the existing bearer; an OpenAI-compatible row
# through the SAME GetApiKeyCredentialProvider + GetSecretValue pair on ITS provider —
# `memory_stack.py` grants the `launchpad-*` family); `LAUNCHPAD_EMBED_MODEL_ID` + Titan
# stay the fallback for an in-flight message without the block.
KIND_OPENAI_COMPATIBLE = "openai_compatible"
EMBED_TIMEOUT_S = 30
# region-boundary: global-bedrock
MODEL_REGION = os.environ.get("LAUNCHPAD_MODEL_REGION", "us-west-2")
MODEL_CREDENTIAL = os.environ.get("LAUNCHPAD_MODEL_CREDENTIAL", "launchpad-global-bedrock")
CONTEXT_EVENTS = int(os.environ.get("LAUNCHPAD_EXTRACT_CONTEXT_EVENTS", "4"))
# N17-obs237 (default's extractor DLQ, 2026-10-20): the transcript (`CONTEXT_EVENTS` + the new
# turn) is embedded whole for candidate retrieval and sent to the extraction model; both calls
# cap the input at 8192 tokens (`ValidationException … Too many input tokens. Max input tokens:
# 8192, request input token count: 9067` from InvokeModel = the Titan embedding). CJK text runs
# at ≈1.1–1.5 tokens per character, so the embedder's own 8000-character cut was not enough.
# The transcript is fitted to this many characters BEFORE any model call — oldest context
# events dropped first, then the turn's text truncated — and a size refusal that still gets
# through is retried ONCE with the context dropped and the turn halved, then PERMANENT.
MAX_PROMPT_CHARS = int(os.environ.get("LAUNCHPAD_EXTRACT_MAX_PROMPT_CHARS", "6000"))
MIN_PROMPT_CHARS = 400  # the shrink never cuts the turn below this
_TRUNCATION_MARKER = "\n[… truncated]"
# What the model APIs say when the prompt is too large — a PERMANENT 400, never a redelivery
# (the Lambda's catch-all turned it into a RetryableError → 4 receives → DLQ, extraction lost).
_INPUT_TOO_LONG_RE = re.compile(
    r"too many input tokens|input (?:is )?too long|input token count|prompt is too long|"
    r"(?:maximum |max )?(?:context|input) length|context_length_exceeded|"
    r"exceeds the (?:maximum |max )?(?:number of )?(?:input |context )?tokens|too long for",
    re.IGNORECASE,
)

# F-LOAD-03 (N17-obs534, ten-student simulation 2026-10-30): what the model APIs say when the
# `temperature` field is sent to a model that does not take it — Bedrock Converse on Kimi K3
# ("This model doesn't support the temperature field. Remove temperature and try again.",
# N12-bug11), an OpenAI-compatible provider ("Unsupported parameter: 'temperature'"). A 400 of
# this shape is retried ONCE without the field before any classification.
_TEMPERATURE_REJECTED_RE = re.compile(r"temperature", re.IGNORECASE)

MAX_TOKENS = 2048  # C3: GLM 5.3 always reasons; >= 256 or the text block is empty.
# r29: reasoning can consume the entire small budget without a text block. Retry only
# an explicit output-token stop, once, while the operation is still a read-only model call.
RETRY_MAX_TOKENS = 8192
MODEL_CONNECT_TIMEOUT_S = 5
MODEL_READ_TIMEOUT_S = 120
MODEL_READ_TIMEOUT_ENV = "LAUNCHPAD_EXTRACT_MODEL_READ_TIMEOUT_SECONDS"
BEDROCK_EMBED_READ_TIMEOUT_S = 60
MODEL_FINISH_RESERVE_S = 90
_remaining_seconds: ContextVar[Callable[[], float] | None] = ContextVar(
    "memory_extractor_remaining_seconds", default=None
)
NATIVE_TEXT_EXT = (".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm")
SIDECAR_DIR = "_text/"
MAX_DOC_BYTES = 2_000_000
MAX_CHUNKS = 400
# N17-obs245 (user report 2026-10-22, default, job ije33be9935ca0ae51): an 81.3 KB HTML's chunks —
# ALL of them, embeddings included — went to the index in ONE `_bulk` request; on the single
# t3.small.search node (HNSW indexing, IndexingLatency ≤ 254 ms, no rejections) that one request
# exceeded the client's 30 s read timeout and the document FAILED with the transport error.
# The chunks of one document are now written in batches of this many; a transport timeout on
# a batch is retried ONCE (the sha1 chunk ids make the write idempotent) before the document
# fails with a readable reason. The timeout itself is unchanged (30 s per request).
BULK_BATCH_CHUNKS = 16
BULK_WRITE_TIMED_OUT_REASON = "index write timed out — run the sync again"
# N17-obs246 (user question 2026-10-22): every sync re-chunked, re-embedded and rewrote EVERY
# object under the prefix (≈90 s for 4 files to retry the one that failed, each document briefly
# unsearchable between its delete and its write, every rewrite another chance at obs245's
# timeout). A document whose kb-docs record is INDEXED with the same S3 ETag (+ size) as the
# listing is now skipped — no read, no embedding, no write — and counted
# `numberOfDocumentsUnchanged`; a record without an `etag` (written before this change) is
# treated as changed once. The message flag `"full": true` forces the former full rewrite (the
# hub does not send it yet; accepted here so a later route can). Records whose S3 object is gone
# are swept (chunks + record, `numberOfDocumentsDeleted`).
KB_DOCS_SWEEP_PAGE = 500
# N17-obs247 (user question 2026-10-22): one `kb_ingest` invocation handled EVERY document of a
# sync and embedded chunk by chunk (≈0.5 s each incl. writes: 4 docs = 92 s live) under the
# Lambda's 120 s timeout — a sync of ≳150–200 chunks (one large PDF) timed out, was retried 3×
# and went to the DLQ with the job left non-terminal. The sync message is now the PLANNER: it
# lists the prefix, decides changed / unchanged (obs246), sweeps deleted documents and enqueues
# one `kb_ingest_doc` message per changed document on its own queue (role grant + queue URL
# from the stack); each document message embeds + writes ONE document and records a
# `kb_ingest_doc_result` row; the job turns COMPLETE when the last result arrives (totals are
# computed from the result rows — never a read-merge-write race on the job). Without the queue
# URL (older stack, tests) the planner processes the documents inline as before.
KIND_KB_INGEST_DOC = "kb_ingest_doc"
KIND_KB_INGEST_DOC_RESULT = "kb_ingest_doc_result"
# chunk embeddings of one document run on this many threads (the embedders take one text per
# call; `_embed_with_retry` semantics per chunk, order preserved)
EMBED_WORKERS = 4
# N17-obs270 (user approval 2026-10-22, deferred item 3 of obs247): ONE document of up to
# 2 MB text / `MAX_CHUNKS` chunks no longer has to finish inside one 120 s invocation. The
# document stage works in SLICES: at most `SLICE_CHUNKS` chunks per invocation, cut earlier
# (at a `BULK_BATCH_CHUNKS` boundary of the embedding) once `SLICE_BUDGET_S` seconds have
# passed since the message was picked up. When chunks remain, a continuation `kb_ingest_doc`
# message (same job / run / document, the ETag of the text read, `slice_start`) goes back on
# the queue and the invocation returns. Slice 0 alone deletes the document's old chunks
# (after embedding its own, so they stay searchable meanwhile); chunk ids stay
# `sha1(uri#i)`, so a redelivered slice rewrites the same ids. The kb-docs record turns
# INDEXED, the per-document `_refresh` runs and the result row is written only after the LAST
# slice. A continuation whose object (PDF: text sidecar) no longer has the ETag slice 0 read
# was replaced mid-ingest: it stops, records `SUPERSEDED_REASON` and leaves the new version to
# the next sync. The 70 s budget leaves the writes of the last embedded chunks (≤ 4 batches)
# and the continuation send inside the original 120 s KB slice envelope. The larger
# function timeout for memory model reads does not enlarge these existing KB slices.
SLICE_CHUNKS = 64
# N17-obs274 (rev-r22 on obs270) — the slice's time arithmetic retains its 120 s envelope.
# The embedding of a slice runs in groups of `EMBED_WORKERS` chunks (one parallel round);
# BEFORE each group the stage projects `elapsed + slowest group so far` and stops when that
# would pass `SLICE_BUDGET_S` (70 s), so a degraded model ends the embedding near 70 s instead
# of discovering the overrun after it. The remaining 50 s (`SLICE_RESERVE_S`) cover what follows
# the embedding: ≤ 4 `_bulk` batches of 16 (obs245: a batch is seconds on the t3.small node; a
# 30 s read timeout + one retry is the documented worst case of ONE batch), the record write and
# refresh of a last slice, the continuation send and the job bump. Residual risk, documented:
# the FIRST group of a slice has no history to project from — one round of `EMBED_WORKERS` calls
# can itself take up to `EMBED_TIMEOUT_S` × 3 attempts + backoff; a slice that still times out is
# redelivered and redone idempotently (same stamp, same ids — below).
# N17-obs273 (deploy-ops-35 live 2026-10-22): the document stage's `updated_at` bump on the job
# document is a partial `_update`; concurrent document invocations of one fan-out hit
# OpenSearch's version check (409 version_conflict_engine_exception) and the stage raised
# RetryableError although its result row was already written — redelivered after the 720 s
# visibility timeout, acked DUPLICATE (wasted invocations; a large fan-out could reach the DLQ).
# The bump now asks OpenSearch to retry the conflict server-side this many times, and any
# residual failure is logged and ignored: it is best-effort liveness for the hub's stalled
# rule, the result row is the truth — never a message retry.
JOB_TOUCH_RETRY_ON_CONFLICT = 5
# N17-obs279: client-side attempts of the scripted COMPLETE write when a 409 survives the
# server-side `retry_on_conflict` (the terminal write must not be less reliable than the plain
# PUT it replaced)
COMPLETE_ONCE_ATTEMPTS = 3
SLICE_BUDGET_S = 70.0
SLICE_RESERVE_S = 50.0
# N17-obs274: every chunk carries `ingest` = "<job>:<run>" (the sync that wrote it) and its id
# is `sha1(uri#i#stamp)` — redeliveries and twin chains of ONE sync rewrite the same ids, two
# syncs never overwrite each other's chunks. Deletes are conditional on the stamp (slice 0 and
# the last slice remove this document's chunks NOT stamped by this sync; a superseded chain
# removes only its own). A per-document lease row (`KIND_KB_DOC_LEASE`, jobs index, id
# `lease:<doc_id>`, `owner` = stamp) is taken by every slice 0; a later slice of a chain that
# lost it stops (`SUPERSEDED`, its own chunks removed) — so of two concurrent syncs of one
# document exactly the newer-started one finishes, with every chunk present.
INGEST_FIELD = "ingest"
INCOMPLETE_WRITE_REASON = "incomplete index write — run the sync again"
# N17-obs274 pass 4: a document whose slice 0 started and whose last slice has not finished
INDEXING_STATUS = "INDEXING"
KIND_KB_DOC_LEASE = "kb_ingest_doc_lease"
SUPERSEDED_REASON = (
    "replaced in S3 while this sync was indexing it — the next sync indexes the new version"
)
# N17-obs120 (cn/r17-t): a TRANSIENT embedding fault (Bedrock throttle / 5xx, an
# OpenAI-compatible 429 / 503, a timeout — `runtime_invoke.is_upstream_transient`, the one
# shared predicate) on one chunk is retried in place with this bounded backoff before the
# document is marked FAILED; a permanent shape (ValidationException) fails it at once.
EMBED_RETRY_BACKOFF_S: tuple[float, ...] = (1.0, 3.0)
MAX_ITEMS_PER_STRATEGY = 20
# Merge strategy v2 (r14-a): how many existing records the model sees as candidates, the
# confidence a model item gets when it names none, and the floor under which an explicit
# `delete` is not honoured (a retraction must be stated, not guessed).
CANDIDATE_K = 8
DEFAULT_CONFIDENCE = 0.8
DELETE_MIN_CONFIDENCE = 0.6
EXPIRE_MAX_PER_EVENT = 50

# N17-obs304: every extraction / summary prompt records only what was said — handed a user
# question with no assistant answer, the model answered it itself (invented lodging rules
# stored as a long-term "fact")
_ONLY_WHAT_WAS_SAID = (
    "Record only what the conversation itself says. Never answer the user's questions "
    "yourself and never add facts, figures or outside knowledge that nobody in the "
    "conversation stated."
)
_OP_RULES = (
    'Reply with a JSON array of operations. Each operation is an object with "op" '
    '("add" | "update" | "supersede" | "delete" | "noop"), "target_id" (the id of an '
    'EXISTING item, or null), "confidence" (0 to 1), and the item fields described above. '
    "Rules: `add` = a new item no existing one covers (target_id null); `update` = an "
    "existing item is refined or corrected in place (target_id required, give the corrected "
    "item); `supersede` = a new item replaces an existing one that is no longer true "
    "(target_id = the old item, give the new item); `delete` = ONLY when the user explicitly "
    "retracts an existing item (target_id required, no item fields); `noop` = the "
    "conversation changes nothing. Never invent anything the user did not say. "
    + _ONLY_WHAT_WAS_SAID + " Reply with [] "
    "when there is nothing to record. No prose, no markdown code fences — JSON only."
)
FACTS_PROMPT = (
    "You maintain a user's long-term memory. The user message lists the EXISTING facts "
    "about the user (each with an id) and then the conversation. Extract atomic, durable "
    'facts about the user: an item has one field, "text" (one fact). ' + _OP_RULES
)
PREFERENCES_PROMPT = (
    "You maintain a user's long-term memory. The user message lists the EXISTING "
    "preferences of the user (each with an id) and then the conversation. Extract the "
    'user\'s stated preferences: an item has the fields "preference" (str) and "category" '
    "(str). " + _OP_RULES
)
SUMMARY_PROMPT = (
    "Summarize this conversation so far in 120 words or fewer: what the user asked and what "
    "the assistant replied. " + _ONLY_WHAT_WAS_SAID + " Plain text, no markdown, no preamble."
)
CONSOLIDATE_PROMPT = (
    "You maintain a user's long-term memory, which has grown past its size limit. The user "
    "message lists memory records (each with an id). Merge records that describe the same "
    "or closely related things into fewer, equally precise records; keep every distinct "
    "detail, drop nothing that is not implied by another record. Reply with a JSON array of "
    'objects shaped {"text": str, "merges": [id, ...], "confidence": 0 to 1}, where '
    '"merges" lists two or more input ids the new record replaces. Records that cannot be '
    "merged with any other are simply not mentioned. Reply with [] when nothing can be "
    "merged. No prose, no markdown code fences — JSON only."
)
ACTOR_SUMMARY_PROMPT = (
    "The user message lists summaries of a user's recent conversations with an assistant, "
    f"newest first. Write ONE rolling summary of what matters across them in "
    f"{ACTOR_SUMMARY_MAX_WORDS} words or fewer: stable facts, goals, preferences, open "
    "threads. Record only what the summaries themselves say. Never answer the user's "
    "questions yourself and never add facts, figures or outside knowledge that no summary "
    "stated. Plain text, no markdown, no preamble."
)


class RetryableError(RuntimeError):
    """Transient failure — let SQS redeliver the message (reported via
    `batchItemFailures`). Never raised for a message this Lambda understood but
    whose content was unusable (bad JSON, unknown kind) — those are poison and
    must be dropped, not retried forever."""


class LeaseUnreadable(RetryableError):
    """N17-obs278: the lease re-check slice 0 makes after embedding could not be read. Nothing
    of the slice is written yet, so — like the pre-work lease read (N17-obs274 pass 3) — it is a
    redelivery, never a FAILED record; `_index_slice`'s catch-all lets it through."""


class DroppedMessage(RuntimeError):
    """The resource the message refers to no longer exists — the knowledge base's
    index, its data-source bucket, or the agent whose memory snapshots carried the
    event (deleted with the agent, N11 r2-B). Nothing a redelivery could fix: the job
    record is marked FAILED with the reason, the message is acknowledged and dropped,
    and one WARNING line says so (N11-r3-bug4: this used to cycle to the DLQ silently)."""


class IndexWriteTimeout(RuntimeError):
    """N17-obs245: a chunk batch's `_bulk` timed out twice. `str(exc)` is the readable reason
    stored on the document record (`BULK_WRITE_TIMED_OUT_REASON`); the transport exception —
    whose text names the domain host — is the `__cause__` and reaches the log line only."""


def _is_transport_timeout(exc: BaseException) -> bool:
    """A urllib3 read / connect timeout (the live `ReadTimeoutError: HTTPSConnectionPool(host=…,
    port=443): Read timed out. (read timeout=30.0)`) or a socket timeout."""
    return isinstance(exc, (urllib3.exceptions.TimeoutError, TimeoutError))


def _bulk_in_batches(deps: "Deps", index: str, docs: list[tuple[str, dict[str, Any]]],
                     *, batch_size: int = BULK_BATCH_CHUNKS) -> int:
    """Write ``docs`` (chunk id, chunk) to ``index`` without refresh, ``batch_size`` per `_bulk`
    request (N17-obs245). A transport timeout on a batch is retried once with the same ids; a
    second timeout raises `IndexWriteTimeout`. Any other error propagates unchanged."""
    for start in range(0, len(docs), batch_size):
        part = docs[start:start + batch_size]
        try:
            deps.opensearch.bulk(index, part, refresh=False)
        except Exception as exc:  # noqa: BLE001 — classified; only a timeout is retried
            if not _is_transport_timeout(exc):
                raise
            logger.warning(
                "kb_ingest: bulk batch %d-%d of %d chunks timed out (%s) — retrying once",
                start + 1, start + len(part), len(docs), type(exc).__name__,
            )
            try:
                deps.opensearch.bulk(index, part, refresh=False)
            except Exception as again:  # noqa: BLE001
                if _is_transport_timeout(again):
                    raise IndexWriteTimeout(BULK_WRITE_TIMED_OUT_REASON) from again
                raise
    return len(docs)


class InputTooLongError(RuntimeError):
    """PERMANENT (N17-obs237): the embedding or extraction model refused the prompt for its
    size even after the one shrink retry. The job is FAILED with `failure_code:
    input_too_long`, the message is acknowledged and dropped — a redelivery would send the
    same prompt again."""


class ModelValidationFailure(RetryableError):
    """N17-obs535 (ten-student simulation 2026-10-30): the model refused the request with a 400
    the request itself caused (`failure_code: model_validation`) — after the one temperature
    retry of F-LOAD-03. The job is FAILED with the reason so the hub shows it, and the message is
    REDELIVERED (`batchItemFailures`) so it reaches the DLQ after `maxReceiveCount`; every
    receive logs one `outcome=failed_permanent reason=model_validation model=<id>` WARNING line.
    Until 2026-10-30 this class was acknowledged and dropped like a size refusal, and ten
    students' extractions vanished with nothing outside the jobs index saying so. `model` is the
    extraction model the message resolved to (`chat_model_for`)."""

    def __init__(self, reason: str, *, model: str | None) -> None:
        super().__init__(reason)
        self.model = model


class ModelOutputTruncatedError(RuntimeError):
    """Both bounded model attempts exhausted their output budget; no partial ops are used."""


class ExtractionDeadlineError(RuntimeError):
    """Not enough Lambda time remains for another bounded model call and job bookkeeping."""


def _model_read_timeout() -> float:
    """One model socket read shares the fixed invocation budget with other model reads.

    120 s is an allocation ceiling, not a provider latency guarantee. A smaller remaining
    window clips the actual transport, rather than refusing all work below the ceiling.
    """
    configured = float(os.environ.get(MODEL_READ_TIMEOUT_ENV, str(MODEL_READ_TIMEOUT_S)))
    if not math.isfinite(configured) or not 1 <= configured <= MODEL_READ_TIMEOUT_S:
        raise ValueError(f"{MODEL_READ_TIMEOUT_ENV} must be between 1 and {MODEL_READ_TIMEOUT_S}")
    remaining = _remaining_seconds.get()
    available = (
        configured if remaining is None
        else remaining() - MODEL_CONNECT_TIMEOUT_S - MODEL_FINISH_RESERVE_S
    )
    if available < 1:
        raise ExtractionDeadlineError("insufficient time for model call and terminal bookkeeping")
    return min(configured, available)


def _bounded_model_text(
    call: Callable[[int, float], tuple[str, str, dict[str, Any]]], model: str,
) -> str:
    """Only the provider's explicit truncation signal authorizes the larger read-only call.

    Never return text from a truncated response, even if its prefix happens to parse as JSON.
    No prompt, response content, reasoning, credential or arbitrary provider fields are logged.
    """
    for attempt, tokens in enumerate((MAX_TOKENS, RETRY_MAX_TOKENS), start=1):
        read_timeout = _model_read_timeout()
        _diagnostic_logger.info(
            "model_request model=%s attempt=%d max_tokens=%d read_timeout_seconds=%s",
            model, attempt, tokens, read_timeout,
        )
        text, stop, usage = call(tokens, read_timeout)
        safe_stop = stop if stop in {
            "max_tokens", "length", "end_turn", "stop", "stop_sequence", "tool_use",
            "content_filtered", "guardrail_intervened", "model_context_window_exceeded",
        } else "unknown"
        output_tokens = usage.get("outputTokens") if isinstance(usage, dict) else None
        _diagnostic_logger.info(
            "model_response model=%s attempt=%d max_tokens=%d stop_reason=%s "
            "text_chars=%d output_tokens=%s read_timeout_seconds=%s",
            model, attempt, tokens, safe_stop, len(text),
            output_tokens if isinstance(output_tokens, int) else None, read_timeout,
        )
        if stop not in ("max_tokens", "length"):
            return text
    raise ModelOutputTruncatedError(
        f"model output truncated after 2 attempts (max_tokens={RETRY_MAX_TOKENS})"
    )


def _error_text(exc: BaseException) -> str:
    """The text a model-side refusal is classified on: the exception's type and message, a
    `ClientError`'s code and message and — for a 400 urllib `HTTPError` — up to 2000 bytes of
    the response body. The body is read once and PUT BACK (`exc.fp` becomes a `BytesIO` over the
    bytes read), so `is_temperature_rejection` and `is_input_too_long` can both look at the same
    exception; the body is a convenience, never a gate."""
    parts = [type(exc).__name__, str(exc)]
    if isinstance(exc, ClientError):
        error = exc.response.get("Error") or {}
        parts += [str(error.get("Code") or ""), str(error.get("Message") or "")]
    body = getattr(exc, "fp", None)  # urllib HTTPError: the response body carries the reason
    if body is not None and getattr(exc, "code", None) == 400:
        try:
            data = body.read(2000)
            exc.fp = BytesIO(data)  # type: ignore[attr-defined]
            parts.append(data.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 — the body is a convenience, never a gate
            pass
    return " ".join(parts)


def is_input_too_long(exc: BaseException) -> bool:
    """A model-side size refusal: a 400 `ValidationException` (or any error) whose text says
    the input / prompt / context is too long or has too many tokens — Bedrock InvokeModel
    (Titan), Converse, an OpenAI-compatible `context_length_exceeded`."""
    return bool(_INPUT_TOO_LONG_RE.search(_error_text(exc)))


def is_temperature_rejection(exc: BaseException) -> bool:
    """F-LOAD-03 (N17-obs534): a 400 whose text names the `temperature` field — Bedrock Converse
    on a model without the field (Kimi K3: "This model doesn't support the temperature field"),
    an OpenAI-compatible provider's "Unsupported parameter: 'temperature'". The chat retries
    ONCE without the field (`_with_temperature_fallback`); anything else is left to the size /
    validation classifiers."""
    if isinstance(exc, HTTPError):
        if exc.code != 400:
            return False
    elif isinstance(exc, ClientError):
        error = exc.response.get("Error") or {}
        status = (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if str(error.get("Code") or "") != "ValidationException" and status != 400:
            return False
    else:
        return False
    return bool(_TEMPERATURE_REJECTED_RE.search(_error_text(exc)))


def is_model_validation_error(exc: BaseException) -> bool:
    """A 400 `ValidationException` from a Bedrock model call, or an OpenAI-compatible
    provider's 4xx other than 408 / 429 (rev-r22 (d): the same permanent class) — the
    content / request caused it, so no redelivery can fix it (`failure_code:
    model_validation`). N17-obs535: FAILED for the hub and REDELIVERED to the DLQ
    (`ModelValidationFailure`), no longer acknowledged and dropped."""
    if isinstance(exc, HTTPError):
        return 400 <= exc.code < 500 and exc.code not in (408, 429)
    return isinstance(exc, ClientError) and str(
        (exc.response.get("Error") or {}).get("Code") or "") == "ValidationException"


def _error_reason(exc: BaseException, limit: int = 300) -> str:
    text = " ".join(str(exc).split())
    reason = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return reason if len(reason) <= limit else reason[:limit] + "…"


# One line per SQS message: `message=<id> kind=<kind> job=<id> outcome=<…> duration_ms=<n>`.
# Lambda's runtime attaches its handler to the root logger at WARNING; this logger is
# raised to INFO so the ok lines reach CloudWatch too.
logger = logging.getLogger("launchpad.memory_extractor")
logger.setLevel(logging.INFO)
_diagnostic_logger = logging.getLogger("launchpad.memory_extractor.diagnostics")

_MISSING_S3_CODES = {"NoSuchKey", "NoSuchBucket", "404", "NotFound"}


def _is_missing_s3(exc: ClientError) -> bool:
    return str(exc.response.get("Error", {}).get("Code", "")) in _MISSING_S3_CODES


def _extract_job_id(event_key: str) -> str:
    return "ex" + hashlib.sha1(event_key.encode()).hexdigest()[:16]


def message_job_id(body: dict[str, Any]) -> str:
    """The job id a message maps to (for the per-message log line), '' when unknown."""
    if body.get("kind") in ("kb_ingest", KIND_KB_INGEST_DOC):  # N17-obs273: doc messages too
        return str(body.get("job_id") or "")
    if body.get("kind") == "memory_extract" and body.get("event_key"):
        return _extract_job_id(str(body["event_key"]))
    return ""


# ----------------------------------------------------------------------------
# Bearer resolution (ruling R1) — cached per container, never logged.
# ----------------------------------------------------------------------------

_BEARER: dict[str, str] = {}


def _secret_to_bearer(secret_string: str) -> str:
    """A Secrets Manager value for an AgentCore Identity API-key provider is
    usually `{"api_key": "..."}` (or an equivalent key) but may be a bare string
    for a hand-created secret — accept both."""
    try:
        data = json.loads(secret_string)
    except ValueError:
        return secret_string.strip()
    if isinstance(data, dict):
        for key in ("api_key", "apiKey", "key", "token"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        for value in data.values():
            if isinstance(value, str):
                return value
    return secret_string.strip()


def secret_arn_of(provider: dict[str, Any]) -> str:
    """`apiKeySecretArn` from GetApiKeyCredentialProvider as a plain string.

    The cn-northwest-1 preview API returns it as ``{"secretArn": "arn:…"}`` (seen live
    2026-10-10, N11-r3-bug2: ``ParamValidationError: Invalid type for parameter
    SecretId … type: dict``), while the Global API documents a string. Accept both.
    """
    value = provider.get("apiKeySecretArn")
    if isinstance(value, dict):
        value = value.get("secretArn") or value.get("arn")
    if not isinstance(value, str) or not value:
        raise RuntimeError("GetApiKeyCredentialProvider returned no usable apiKeySecretArn")
    return value


def refresh_bearer(control: Any, secrets: Any, provider_name: str) -> str:
    """rev-r22: drop the cached bearer of ``provider_name`` and resolve it again — the
    self-heal after a 401 / 403 from an OpenAI-compatible provider (a key rotated in Identity
    after this container cached the old one)."""
    _BEARER.pop(provider_name, None)
    return resolve_bearer(control, secrets, provider_name)


def _is_auth_refusal(exc: BaseException) -> bool:
    return isinstance(exc, HTTPError) and exc.code in (401, 403)


def resolve_bearer(control: Any, secrets: Any, provider_name: str) -> str:
    """`GetApiKeyCredentialProvider` -> secret ARN -> `GetSecretValue` -> bearer,
    cached in-process by provider name so a warm container never re-fetches."""
    if provider_name in _BEARER:
        return _BEARER[provider_name]
    provider = control.get_api_key_credential_provider(name=provider_name)
    arn = secret_arn_of(provider)
    secret_string = secrets.get_secret_value(SecretId=arn)["SecretString"]
    _BEARER[provider_name] = _secret_to_bearer(secret_string)
    return _BEARER[provider_name]


# ----------------------------------------------------------------------------
# Dependencies
# ----------------------------------------------------------------------------


@dataclass
class Deps:
    s3: Any
    opensearch: Any
    bedrock: Any
    embedder: Any
    now: Callable[[], datetime]
    env: dict[str, Any]
    # D21: the Identity control + Secrets Manager clients `embedder_for` resolves an
    # OpenAI-compatible row's key with (None in tests → such a message is a loud error)
    control: Any = None
    secrets: Any = None
    # test seam: ``post(url, headers, body_bytes) -> bytes``; None = urllib
    http_post: Any = None
    # the embedding retry's wait (N17-obs120) — injectable so a test never really sleeps
    sleep: Callable[[float], None] = time.sleep
    # N17-obs270: the slice budget's clock (seconds, monotonic) — injectable for tests
    clock: Callable[[], float] = time.monotonic
    # N17-obs247: the Lambda's own queue, for the per-document fan-out (None = inline)
    sqs: Any = None
    queue_url: str | None = None
    # N17-obs252: the extraction chat model of the message being processed (`chat_model_for`);
    # None = `BedrockChat` on `env["extract_model"]` (the LAUNCHPAD_EXTRACT_MODEL_ID fallback)
    chat: Any = None
    # Extraction chat only: each request gets its clipped timeout. Embedding/KB clients
    # retain their separate existing timeouts and retry behavior.
    model_client_factory: Callable[[float], Any] | None = None


class OpenAIEmbedder:
    """``POST {base_url}/embeddings`` on an OpenAI-compatible endpoint — the Lambda twin of
    `app/services/embedding_backend.OpenAIEmbedder` (stdlib only: no httpx in the bundle).
    Same ``embed(text) -> list[float]`` contract as the core's `TitanEmbedder`; a vector of
    another length than the descriptor's `dimension` is refused (the index mapping cannot
    hold it). The key is a header argument only — never logged, never in the environment."""

    def __init__(self, *, base_url: str, api_key: str, api_model_name: str,
                 dimension: int | None, max_chars: int | None, post: Any = None,
                 refresh_key: Callable[[], str] | None = None) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.model_id = api_model_name
        self.api_model_name = api_model_name
        self.dimensions = int(dimension) if dimension else None
        self.max_chars = int(max_chars) if max_chars else EMBED_MAX_CHARS
        self._headers = {"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"}
        self._post = post or _urllib_post
        self._refresh_key = refresh_key  # rev-r22: re-resolve the credential on 401 / 403

    def _post_with_reauth(self, url: str, body: bytes) -> bytes:
        """One POST; a 401 / 403 re-resolves the credential ONCE (`refresh_key`, the cached
        client object keeps the new header) and retries once — a key rotated in Identity
        heals itself; a second refusal propagates (→ `model_validation`: FAILED for the hub
        and redelivered to the DLQ, N17-obs535)."""
        try:
            return self._post(url, self._headers, body)
        except HTTPError as exc:
            if not _is_auth_refusal(exc) or self._refresh_key is None:
                raise
            logger.warning("%s: %s %s from the provider — re-resolving the credential once",
                           type(self).__name__, exc.code, exc.reason)
            self._headers["Authorization"] = f"Bearer {self._refresh_key()}"
            return self._post(url, self._headers, body)

    def embed(self, text: str) -> list[float]:
        body = json.dumps({"model": self.api_model_name,
                           "input": text[: self.max_chars]}).encode("utf-8")
        raw = self._post_with_reauth(f"{self.base_url}/embeddings", body)
        data = json.loads(raw)
        vector = [float(x) for x in data["data"][0]["embedding"]]
        if self.dimensions and len(vector) != self.dimensions:
            raise RuntimeError(
                f"{self.api_model_name} returned a {len(vector)}-dimensional vector; the "
                f"message's embedding descriptor declares {self.dimensions}"
            )
        return vector


def _urllib_post(url: str, headers: dict[str, str], body: bytes) -> bytes:
    from urllib import request as _request

    req = _request.Request(url, data=body, headers=headers, method="POST")
    with _request.urlopen(req, timeout=EMBED_TIMEOUT_S) as resp:  # noqa: S310 - https, hub-allowlisted host
        return resp.read()


def _urllib_model_post(url: str, headers: dict[str, str], body: bytes) -> bytes:
    """Chat-only transport; bound connect and read separately, with no hidden retries.

    Recompute on each HTTP request, including the existing one-time credential refresh.
    The embedding transport above remains unchanged.
    """
    pool = urllib3.PoolManager()
    try:
        response = pool.request(
            "POST", url, headers=headers, body=body, retries=False,
            timeout=urllib3.Timeout(connect=MODEL_CONNECT_TIMEOUT_S, read=_model_read_timeout()),
        )
        if response.status >= 400:
            raise HTTPError(
                url, response.status, "model request refused", response.headers,
                BytesIO(response.data),
            )
        return response.data
    finally:
        pool.clear()


_EMBEDDERS: dict[str, Any] = {}


def _with_temperature_fallback(chat: Any, run: Callable[[bool], str]) -> str:
    """F-LOAD-03 (N17-obs534): ``run(send_temperature)`` — one extraction call. The field is sent
    only while ``chat.temperature`` is True (the message block's catalog capability; False for
    the env fallback). A 400 that names the field (`is_temperature_rejection`) is retried ONCE
    without it and the chat object remembers the downgrade for the rest of the container's life,
    so the other calls of the same extraction do not meet the 400 again. Every other error
    propagates unchanged to the size / validation classifiers."""
    if not chat.temperature:
        return run(False)
    try:
        return run(True)
    except Exception as exc:  # noqa: BLE001 — classified; only a temperature refusal is retried
        if not is_temperature_rejection(exc):
            raise
        logger.warning(
            "memory_extract: model %s rejected the temperature field (%s) — retrying once "
            "without it; the catalog row's temperature capability is wrong for this model",
            chat.model_id, _error_reason(exc),
        )
        chat.temperature = False
        return run(False)


class BedrockChat:
    """The extraction model on Bedrock (Converse, the Lambda's bearer client, `MAX_TOKENS`,
    reasoning blocks dropped — C3). ``temperature`` (F-LOAD-03): send `temperature: 0` — only
    when the message's `extract_model` block says the row takes the field; the env fallback
    never sends it."""

    def __init__(
        self, bedrock: Any, model_id: str,
        client_factory: Callable[[float], Any] | None = None, *, temperature: bool = False,
    ) -> None:
        self.bedrock = bedrock
        self.model_id = model_id
        self.client_factory = client_factory
        self.temperature = bool(temperature)

    def converse_text(self, system: str, user: str) -> str:
        return _with_temperature_fallback(self, lambda send_temperature: _converse_text(
            self.bedrock, self.model_id, system, user, client_factory=self.client_factory,
            temperature=send_temperature,
        ))


class OpenAIChat:
    """``POST {base_url}/chat/completions`` on an OpenAI-compatible provider — the extraction
    twin of `OpenAIEmbedder` (stdlib only; the key is a header argument, never logged or in
    the environment). Returns the first choice's text; a list-of-parts content is joined."""

    def __init__(self, *, base_url: str, api_key: str, api_model_name: str,
                 post: Any = None, refresh_key: Callable[[], str] | None = None,
                 temperature: bool = False) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.model_id = api_model_name
        self.api_model_name = api_model_name
        self._headers = {"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"}
        self._post = post or _urllib_model_post
        self._refresh_key = refresh_key  # rev-r22: re-resolve the credential on 401 / 403
        self.temperature = bool(temperature)  # F-LOAD-03: the block's capability

    _post_with_reauth = OpenAIEmbedder._post_with_reauth

    def converse_text(self, system: str, user: str) -> str:
        return _with_temperature_fallback(
            self, lambda send_temperature: self._converse_text(system, user, send_temperature))

    def _converse_text(self, system: str, user: str, send_temperature: bool) -> str:
        def call(tokens: int, read_timeout: float) -> tuple[str, str, dict[str, Any]]:
            payload: dict[str, Any] = {
                "model": self.api_model_name,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "max_tokens": tokens,
            }
            if send_temperature:
                payload["temperature"] = 0
            body = json.dumps(payload).encode("utf-8")
            data = json.loads(self._post_with_reauth(f"{self.base_url}/chat/completions", body))
            choices = data.get("choices") or []
            choice = choices[0] if choices else {}
            content = (choice.get("message") or {}).get("content")
            if isinstance(content, list):
                content = "\n".join(str(part.get("text", "")) for part in content
                                    if isinstance(part, dict))
            return str(content or ""), str(choice.get("finish_reason") or ""), {
                "outputTokens": (data.get("usage") or {}).get("completion_tokens"),
            }
        return _bounded_model_text(call, self.api_model_name)


_CHATS: dict[str, Any] = {}


def chat_model_for(body: dict[str, Any], deps: Deps) -> Any:
    """N17-obs252: the extraction model this message names — its ``extract_model`` block is
    the hub's `memory_extract` role resolved to a catalog row (`embedding_backend.
    extract_model_block`). A Bedrock row → `BedrockChat` with that model id on the Lambda's
    bearer client (`LAUNCHPAD_MODEL_REGION` / `LAUNCHPAD_MODEL_CREDENTIAL`); an
    OpenAI-compatible row → `OpenAIChat` through the row's own Identity provider (the
    `embedder_for` pattern, cached per block for the container's life); no block (an older hub,
    a message in flight at the update) → the env `LAUNCHPAD_EXTRACT_MODEL_ID`. The N17-obs237
    size handling wraps whichever model runs. F-LOAD-03 (N17-obs534): the block's
    ``temperature`` (the catalog row's capability, `extract_model_block`) decides whether the
    chat sends `temperature: 0`; a block without the key and the env fallback send none."""
    block = body.get("extract_model")
    if not isinstance(block, dict) or not block.get("model_id"):
        return BedrockChat(deps.bedrock, deps.env["extract_model"], deps.model_client_factory)
    temperature = bool(block.get("temperature", False))
    if block.get("kind") != KIND_OPENAI_COMPATIBLE:
        return BedrockChat(deps.bedrock, str(block["model_id"]), deps.model_client_factory,
                           temperature=temperature)
    key = json.dumps(block, sort_keys=True)
    cached = _CHATS.get(key)
    if cached is not None:
        return cached
    if deps.control is None or deps.secrets is None:
        raise RuntimeError(
            "an OpenAI-compatible extract_model block needs the Identity control and Secrets "
            "Manager clients (Deps.control / Deps.secrets)"
        )
    provider_name = str(block["credential_provider"])
    bearer = resolve_bearer(deps.control, deps.secrets, provider_name)
    control, secrets_client = deps.control, deps.secrets
    chat = OpenAIChat(base_url=str(block["base_url"]), api_key=bearer,
                      api_model_name=str(block["api_model_name"]), post=deps.http_post,
                      refresh_key=lambda: refresh_bearer(control, secrets_client, provider_name),
                      temperature=temperature)
    _CHATS[key] = chat
    return chat


def _chat(deps: Deps) -> Any:
    return deps.chat if deps.chat is not None else BedrockChat(deps.bedrock,
                                                              deps.env["extract_model"],
                                                              deps.model_client_factory)


def _chat_model_id(deps: Deps) -> str | None:
    """The extraction model id a failure line names (`ModelValidationFailure.model`)."""
    model = getattr(deps.chat, "model_id", None) if deps.chat is not None else None
    return str(model or deps.env.get("extract_model") or "") or None


def embedder_for(body: dict[str, Any], deps: Deps) -> Any:
    """The embedder this message must use: built from its `embedding` descriptor (D21),
    cached per descriptor for the life of the container; a message without one (older hub,
    in-flight at the update) keeps `deps.embedder` (Titan from LAUNCHPAD_EMBED_MODEL_ID)."""
    descriptor = body.get("embedding")
    if not isinstance(descriptor, dict) or not descriptor.get("model_id"):
        return deps.embedder
    default_model = getattr(deps.embedder, "model_id", EMBED_MODEL_ID)
    if descriptor.get("kind") != KIND_OPENAI_COMPATIBLE and (
        descriptor["model_id"] == default_model
    ):
        return deps.embedder  # the Titan row this container already embeds with
    key = json.dumps(descriptor, sort_keys=True)
    cached = _EMBEDDERS.get(key)
    if cached is not None:
        return cached
    if descriptor.get("kind") == KIND_OPENAI_COMPATIBLE:
        if deps.control is None or deps.secrets is None:
            raise RuntimeError(
                "an OpenAI-compatible embedding descriptor needs the Identity control and "
                "Secrets Manager clients (Deps.control / Deps.secrets)"
            )
        provider_name = str(descriptor["credential_provider"])
        bearer = resolve_bearer(deps.control, deps.secrets, provider_name)
        control, secrets_client = deps.control, deps.secrets
        embedder: Any = OpenAIEmbedder(
            base_url=str(descriptor["base_url"]), api_key=bearer,
            api_model_name=str(descriptor["api_model_name"]),
            dimension=descriptor.get("dimension"), max_chars=descriptor.get("max_chars"),
            post=deps.http_post,
            refresh_key=lambda: refresh_bearer(control, secrets_client, provider_name),
        )
    else:
        embedder = TitanEmbedder(deps.bedrock, str(descriptor["model_id"]),
                                 int(descriptor.get("dimension") or EMBED_DIMENSIONS))
    _EMBEDDERS[key] = embedder
    return embedder


def default_deps() -> Deps:
    """The only place this module constructs a boto3 client (C8 — never exercised
    in tests; everything else takes `Deps` so fakes can stand in)."""
    region = os.environ["AWS_REGION"]
    session = boto3.Session()
    control = session.client("bedrock-agentcore-control", region_name=region)
    secrets = session.client("secretsmanager", region_name=region)
    bearer = resolve_bearer(control, secrets, MODEL_CREDENTIAL)
    os.environ["AWS_BEARER_TOKEN_BEDROCK"] = bearer
    bedrock = session.client(
        "bedrock-runtime", region_name=MODEL_REGION,
        config=Config(
            signature_version="bearer", connect_timeout=MODEL_CONNECT_TIMEOUT_S,
            read_timeout=BEDROCK_EMBED_READ_TIMEOUT_S, retries={"total_max_attempts": 1},
        ),
    )
    creds = session.get_credentials()
    os_http = OpenSearchHttp(
        os.environ["LAUNCHPAD_MEMORY_OS_ENDPOINT"],
        region,
        lambda: creds.get_frozen_credentials(),
    )
    return Deps(
        s3=session.client("s3"),
        opensearch=os_http,
        bedrock=bedrock,
        embedder=TitanEmbedder(bedrock, EMBED_MODEL_ID),
        now=lambda: datetime.now(UTC),
        env={"extract_model": EXTRACT_MODEL_ID, "context_events": CONTEXT_EVENTS,
             "max_prompt_chars": MAX_PROMPT_CHARS},
        control=control,
        secrets=secrets,
        sqs=session.client("sqs"),
        queue_url=os.environ.get("LAUNCHPAD_MEMORY_QUEUE_URL") or None,
        model_client_factory=lambda read_timeout: session.client(
            "bedrock-runtime", region_name=MODEL_REGION,
            config=Config(
                signature_version="bearer", connect_timeout=MODEL_CONNECT_TIMEOUT_S,
                read_timeout=read_timeout, retries={"total_max_attempts": 1},
            ),
        ),
    )


# ----------------------------------------------------------------------------
# Small shared helpers
# ----------------------------------------------------------------------------

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _converse_text(
    bedrock: Any, model: str, system: str, user: str, *,
    client_factory: Callable[[float], Any] | None = None, temperature: bool = False,
) -> str:
    """One bounded Converse call. ``temperature`` (F-LOAD-03): `inferenceConfig.temperature: 0`
    only when the caller's block says the model takes the field — Kimi K3 rejects it."""
    def call(tokens: int, read_timeout: float) -> tuple[str, str, dict[str, Any]]:
        client = client_factory(read_timeout) if client_factory is not None else bedrock
        inference: dict[str, Any] = {"maxTokens": tokens}
        if temperature:
            inference["temperature"] = 0
        out = client.converse(
            modelId=model,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig=inference,
        )
        # reasoningContent blocks are never persisted or logged.
        text = "\n".join(
            block["text"] for block in out["output"]["message"]["content"] if "text" in block
        )
        return text, str(out.get("stopReason") or ""), out.get("usage") or {}
    return _bounded_model_text(call, model)


def _parse_json(text: str | None) -> Any | None:
    """Strip ```json fences and parse; `None` (never an exception) on failure —
    the caller treats that as "this strategy produced nothing usable"."""
    if text is None:
        return None
    cleaned = _CODE_FENCE_RE.sub("", text).strip()
    if not cleaned:
        return None
    try:
        return json.loads(cleaned)
    except ValueError:
        return None


def _record_id(store_id: str, namespace: str, text: str) -> str:
    return hashlib.sha1(f"{store_id}|{namespace}|{text.strip().lower()}".encode()).hexdigest()[:32]


# Job statuses that are not the end of the job. Their writes skip the per-request
# `refresh` (N-SCN-obs11): a `refresh=true` PUT forces a segment flush on the single
# t3.small node that is at the same moment bulk-indexing HNSW vectors, and the hub's
# detail poll does not need to see `IN_PROGRESS` within the same second. Terminal writes
# (`COMPLETE`/`COMPLETED`/`FAILED`/`SKIPPED`) keep `refresh=True` so the hub's next poll
# sees the finished job promptly.
NON_TERMINAL_JOB_STATUSES = frozenset({"STARTING", "IN_PROGRESS"})


def _job(deps: Deps, job_id: str, **fields: Any) -> dict[str, Any]:
    """Upsert the job document by id — this is what makes re-delivery idempotent:
    the same `job_id` always resolves to the same document, never a new row."""
    current = deps.opensearch.get_doc(JOBS_INDEX, job_id) or {}
    now_iso = deps.now().isoformat()
    merged = {**current, **fields, "job_id": job_id, "updated_at": now_iso}
    merged.setdefault("created_at", now_iso)
    refresh = merged.get("status") not in NON_TERMINAL_JOB_STATUSES
    deps.opensearch.put_doc(JOBS_INDEX, job_id, merged, refresh=refresh)
    return merged


def _job_soft(deps: Deps, job_id: str, **fields: Any) -> dict[str, Any] | None:
    """`_job` that never raises (N16-obs32): the bookkeeping write that follows the work —
    FAILED next to a `RetryableError` already on its way out, the terminal COMPLETED after
    every record is in the index — must not turn the outcome into a raw exception
    `lambda_handler` would classify as poison (acked and dropped). The failure is logged
    with the exception type only; `None` says the row was not written."""
    try:
        return _job(deps, job_id, **fields)
    except Exception as exc:  # noqa: BLE001 — bookkeeping is best-effort by contract
        logger.warning(
            "job %s: status write %r failed (%s) — the row is stale, the outcome stands",
            job_id, fields.get("status"), type(exc).__name__,
        )
        return None


_TRANSIENT_OS_STATUSES = frozenset({429, 502, 503, 504})


def _is_transient(exc: BaseException) -> bool:
    """The transport / overload family a redelivery can outlive (N16-obs32 belt): an
    `OpenSearchError` whose status is a throttle or a 5xx gateway / unavailable answer, a
    urllib3 / socket failure the retrying pool gave up on, a botocore read timeout or an
    endpoint that could not be reached. Anything else — a 4xx the content caused, a
    `ValueError` in the body — stays poison."""
    if type(exc).__name__ == OpenSearchError.__name__:
        # by name + `status`, like kb_ingest's 404 check: the hub's tests raise the shape
        # from `app.services.opensearch_http`, this Lambda's sibling copy is another class
        return getattr(exc, "status", None) in _TRANSIENT_OS_STATUSES
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "")) in _TRANSIENT_AWS_CODES
    if isinstance(exc, HTTPError):
        # rev-r22 (d): an OpenAI-compatible provider's 4xx is the request's fault (poison /
        # permanent), only 408 / 429 and a 5xx are a redelivery
        return exc.code in (408, 429) or exc.code >= 500
    return isinstance(exc, (urllib3.exceptions.HTTPError, OSError, BotoCoreError))


_TRANSIENT_AWS_CODES = frozenset({
    "ThrottlingException", "Throttling", "TooManyRequestsException", "SlowDown",
    "ServiceUnavailable", "InternalError", "InternalServerException", "RequestTimeout",
})


def has_assistant_answer(event: dict[str, Any]) -> bool:
    """N17-obs304: the event carries non-empty ASSISTANT text. A failed turn written by a
    pre-fix runtime is the USER question plus an ASSISTANT part with text "" — such an event
    is never extracted, nor shown to the model as context."""
    for part in event.get("payload", []):
        conv = part.get("conversational") or {}
        text = (conv.get("content") or {}).get("text")
        if conv.get("role") == "ASSISTANT" and isinstance(text, str) and text.strip():
            return True
    return False


def _transcript(events: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for event in events:
        if not has_assistant_answer(event):
            continue  # N17-obs304: an unanswered question is never handed to the model
        for part in event.get("payload", []):
            conv = part.get("conversational")
            if not conv:
                continue
            role = conv.get("role", "")
            text = (conv.get("content") or {}).get("text", "")
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _previous_events(deps: Deps, bucket: str, event_key: str, limit: int) -> list[dict[str, Any]]:
    """Up to `limit` events strictly before `event_key`, in the same session's
    `events/` prefix, oldest-dropped-first. Keys are lexically sortable by
    construction (`opensearch_http.event_key`); `parse_event_key` is used purely
    as a defensive filter against any non-event key under the prefix."""
    if limit <= 0:
        return []
    prefix = event_key.rsplit("/", 1)[0] + "/"
    keys: list[str] = []
    for page in deps.s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            if parse_event_key(name) is not None:
                keys.append(obj["Key"])
    keys.sort()
    prior = [key for key in keys if key < event_key][-limit:]
    events: list[dict[str, Any]] = []
    for key in prior:
        try:
            events.append(json.loads(deps.s3.get_object(Bucket=bucket, Key=key)["Body"].read()))
        except ClientError:
            continue
    return events


def _truncate(text: str, max_chars: int) -> str:
    keep = max(0, int(max_chars) - len(_TRUNCATION_MARKER))
    return text[:keep] + _TRUNCATION_MARKER


def fit_transcript(
    context: list[dict[str, Any]], event: dict[str, Any], max_chars: int
) -> tuple[str, dict[str, Any]]:
    """The transcript of ``context + [event]`` fitted to ``max_chars`` (N17-obs237): the
    OLDEST context events are dropped first, one at a time, until it fits; a turn that alone
    exceeds the budget is truncated (head kept, a marker appended). Returns the transcript and
    `{context_events, dropped, truncated, chars}`."""
    kept = list(context)
    dropped = 0
    transcript = _transcript([*kept, event])
    while len(transcript) > max_chars and kept:
        kept.pop(0)
        dropped += 1
        transcript = _transcript([*kept, event])
    truncated = False
    if len(transcript) > max_chars:
        transcript = _truncate(transcript, max_chars)
        truncated = True
    return transcript, {"context_events": len(kept), "dropped": dropped,
                        "truncated": truncated, "chars": len(transcript)}


class _Prompt:
    """The transcript the model calls of one extraction share, with ONE shrink in reserve."""

    def __init__(self, context: list[dict[str, Any]], event: dict[str, Any], max_chars: int,
                 warnings: list[str]) -> None:
        self.event = event
        self.max_chars = int(max_chars)
        self.warnings = warnings
        self.shrunk = False
        self.transcript, fit = fit_transcript(context, event, self.max_chars)
        self.fit = fit
        if fit["dropped"] or fit["truncated"]:
            warnings.append(
                f"prompt fitted to {self.max_chars} chars before the model calls: "
                f"{fit['dropped']} context event(s) dropped"
                + (", the turn truncated" if fit["truncated"] else "")
            )

    def shrink(self, reason: str) -> bool:
        """Drop every context event and halve the turn's text — once. False when spent."""
        if self.shrunk:
            return False
        self.shrunk = True
        budget = max(MIN_PROMPT_CHARS, min(self.max_chars, len(self.transcript)) // 2)
        self.transcript, self.fit = fit_transcript([], self.event, budget)
        self.warnings.append(
            f"the model refused the prompt size ({reason}); retried once with the context "
            f"dropped and the turn cut to {self.fit['chars']} chars"
        )
        return True


def _call_with_shrink(prompt: _Prompt, call: Callable[[str], Any]) -> Any:
    """``call(transcript)`` — one model call (the candidate embedding or an extraction) on the
    prompt's current transcript. A size refusal (`is_input_too_long`) shrinks the prompt ONCE
    and retries the same call; a refusal after that is PERMANENT (`InputTooLongError`). Every
    other error propagates unchanged."""
    try:
        return call(prompt.transcript)
    except Exception as exc:  # noqa: BLE001 — classified, re-raised unless a size refusal
        if not is_input_too_long(exc):
            raise
        reason = _error_reason(exc)
        if not prompt.shrink(reason):
            raise InputTooLongError(reason) from exc
        logger.warning("memory_extract: model refused the prompt size (%s) — retrying once "
                       "with %d chars", reason, len(prompt.transcript))
        try:
            return call(prompt.transcript)
        except Exception as again:  # noqa: BLE001
            if is_input_too_long(again):
                raise InputTooLongError(_error_reason(again)) from again
            raise


# ----------------------------------------------------------------------------
# memory_extract — merge strategy v2 helpers (r14-a)
# ----------------------------------------------------------------------------


def _candidates(
    deps: Deps, embedder: Any, store_id: str, namespace: str, transcript: str
) -> dict[str, dict[str, Any]]:
    """The actor's nearest ACTIVE records in `namespace` (by similarity to the transcript),
    keyed by a short alias `c1..cN` the model can quote back; each value keeps `_id` and
    `embedding`. Empty when the namespace holds nothing yet."""
    if not transcript.strip():
        return {}
    hits = deps.opensearch.knn_search(
        MEMORY_INDEX, embedder.embed(transcript), CANDIDATE_K,
        [{"term": {"store_id": store_id}}, {"term": {"namespace": namespace}},
         active_record_filter()],
        include_embedding=True,
    )
    return {f"c{i + 1}": hit for i, hit in enumerate(hits)}


def _merge_user_message(candidates: dict[str, dict[str, Any]], transcript: str) -> str:
    existing = [{"id": alias, **_item_fields(hit.get("text") or "")}
                for alias, hit in candidates.items()]
    return (
        "EXISTING:\n" + json.dumps(existing, ensure_ascii=False)
        + "\n\nCONVERSATION:\n" + transcript
    )


def _item_fields(stored_text: str) -> dict[str, Any]:
    """A stored record text as the item fields the prompt describes: a JSON object stays
    an object (preferences), anything else is `{"text": ...}`."""
    try:
        value = json.loads(stored_text)
    except ValueError:
        value = None
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if k in ("preference", "category", "text")}
    return {"text": stored_text}


def _normalize_item(item: Any) -> dict[str, Any]:
    """Accept the v2 shape (`{op, target_id, confidence, …fields}`) and the pre-v2 shapes
    (a bare string; `{"preference", "category"}` without `op`) — an in-flight model that
    answers the old way still adds."""
    if isinstance(item, str):
        return {"op": "add", "text": item, "target_id": None, "confidence": DEFAULT_CONFIDENCE}
    if not isinstance(item, dict):
        return {"op": "noop"}
    op = str(item.get("op") or "add").strip().lower()
    if op not in ("add", "update", "supersede", "delete", "noop"):
        op = "add"
    # rev-r11 nit: a retraction must STATE its confidence — an absent value reads as 0 for
    # `delete` (so it fails the DELETE_MIN_CONFIDENCE floor → noop + warning); the other
    # ops keep the 0.8 default.
    fallback = 0.0 if op == "delete" else DEFAULT_CONFIDENCE
    try:
        confidence = min(max(float(item.get("confidence", fallback)), 0.0), 1.0)
    except (TypeError, ValueError):
        confidence = fallback
    target = item.get("target_id")
    return {
        **item, "op": op, "confidence": confidence,
        "target_id": str(target) if isinstance(target, str | int) and target != "" else None,
    }


def _item_text(strategy: str, item: dict[str, Any]) -> str:
    """The record text an item stores: facts are plain strings, preferences keep the N6
    JSON object shape `{"preference", "category"}` (what the console decodes)."""
    if strategy == "user_preference" and isinstance(item.get("preference"), str):
        fields = {"preference": item["preference"]}
        if isinstance(item.get("category"), str) and item["category"].strip():
            fields["category"] = item["category"]
        return json.dumps(fields, ensure_ascii=False)
    text = item.get("text")
    if isinstance(text, str):
        return text.strip()
    if strategy == "user_preference":
        return ""
    return json.dumps(item.get("fact"), ensure_ascii=False) if item.get("fact") else ""


def _nearest(
    deps: Deps, store_id: str, namespace: str, vector: list[float]
) -> tuple[dict[str, Any] | None, float]:
    top = deps.opensearch.knn_search(
        MEMORY_INDEX, vector, 1,
        [{"term": {"store_id": store_id}}, {"term": {"namespace": namespace}},
         active_record_filter()],
        include_embedding=True,
    )
    if not top:
        return None, 0.0
    return top[0], cosine(vector, top[0]["embedding"])


def _new_record(
    base_doc: dict[str, Any], strategy: str, namespace: str, text: str, vector: list[float],
    event_id: str, now_iso: str, confidence: float, last_op: str, **extra: Any,
) -> dict[str, Any]:
    return {
        **base_doc, "namespace": namespace, "strategy": strategy, "text": text,
        "embedding": vector, "created_at": now_iso, "updated_at": now_iso,
        "source_event_ids": [event_id], "confidence": confidence, "last_op": last_op,
        **extra,
    }


def _apply_op(
    deps: Deps, embedder: Any, strategy: str, namespace: str, item: dict[str, Any],
    candidates: dict[str, dict[str, Any]], base_doc: dict[str, Any], event_id: str,
    now_iso: str, counts: dict[str, int], warnings: list[str],
) -> None:
    """One model operation → zero or one index write. Decision table (N14-obs1):
    nearest active record at cosine ≥ 0.92 → duplicate (noop); `delete` needs a candidate
    target and confidence ≥ 0.6 → soft `deleted_at`; `update` / `supersede` need a
    candidate target (else they degrade to `add`): update rewrites text + embedding in
    place (same id, `updated_at` bumped), supersede writes a new record and marks the target
    `superseded_by` in one bulk request; in the gray zone 0.75–0.92 the model's choice
    stands; `add` below."""
    op = item["op"]
    store_id = base_doc["store_id"]
    target = candidates.get(item.get("target_id") or "")
    if op == "noop":
        counts["noops"] += 1
        return
    if op == "delete":
        if target is None or item["confidence"] < DELETE_MIN_CONFIDENCE:
            counts["noops"] += 1
            warnings.append(f"{strategy}: delete without a known target or confidence — ignored")
            return
        deps.opensearch.update_doc(
            MEMORY_INDEX, target["_id"],
            {"deleted_at": now_iso, "updated_at": now_iso, "last_op": "delete",
             "source_event_ids": [*target.get("source_event_ids", []), event_id]},
            refresh=False,
        )
        counts["deletes"] += 1
        return
    text = _item_text(strategy, item)
    if not text:
        return
    vector = embedder.embed(text)
    # N17-obs101: the dedupe decision is made on a STRONG read. The candidate read before the
    # model call and this nearest-record check are k-NN searches, which see only refreshed
    # segments: a record another extraction of the same actor wrote with `refresh=False`
    # seconds ago (SQS delivers two turns of one actor concurrently) is invisible to both,
    # and the near-duplicate would be added. One `_refresh` right before the re-check makes
    # every write that has landed visible; the remaining window (a write that lands between
    # this check and ours) is the per-actor serialisation memory-backend.md leaves open.
    deps.opensearch.request("POST", f"/{MEMORY_INDEX}/_refresh")
    nearest, similarity = _nearest(deps, store_id, namespace, vector)
    if nearest is not None and similarity >= DEDUPE_COSINE:
        counts["duplicates"] += 1
        _finish_half_written_supersede(deps, nearest, now_iso, counts)
        return
    if op in ("update", "supersede") and target is None:
        op = "add"  # the model named nothing it saw — a new fact, not a rewrite
    if op == "add" and nearest is not None and similarity >= MERGE_GRAY_ZONE_LOW:
        pass  # gray zone: the model chose `add`; its decision stands
    if op == "update":
        kept = {k: v for k, v in target.items() if k not in ("_id", "_score")}
        deps.opensearch.put_doc(
            MEMORY_INDEX, target["_id"],
            {**kept, "text": text, "embedding": vector, "updated_at": now_iso,
             "confidence": item["confidence"], "last_op": "update",
             "source_event_ids": [*kept.get("source_event_ids", []), event_id]},
            refresh=False,
        )
        counts["updates"] += 1
        return
    new_id = _record_id(store_id, namespace, text)
    if op == "supersede":
        if new_id == target["_id"]:
            counts["duplicates"] += 1  # same normalized text as the target
            return
        # N17-obs90: the successor and the retired target travel in ONE `_bulk` request —
        # two separate writes left both ACTIVE (the successor naming a target with no
        # `superseded_by`) when a fault or a process death landed between them. The
        # candidate hit carries the target's full source (embedding included), so the
        # retired copy is a lossless re-index; the request either fails before either item
        # is applied or applies both (an item-level error is an OpenSearchError → retry).
        retired = {k: v for k, v in target.items() if k not in ("_id", "_score")}
        deps.opensearch.bulk(
            MEMORY_INDEX,
            [
                (new_id, _new_record(base_doc, strategy, namespace, text, vector, event_id,
                                     now_iso, item["confidence"], "supersede",
                                     supersedes=[target["_id"]])),
                (target["_id"], {**retired, "superseded_by": new_id, "superseded_at": now_iso}),
            ],
            refresh=False,
        )
        counts["supersedes"] += 1
        return
    deps.opensearch.put_doc(
        MEMORY_INDEX, new_id,
        _new_record(base_doc, strategy, namespace, text, vector, event_id, now_iso,
                    item["confidence"], "add"),
        refresh=False,
    )
    counts["facts" if strategy == "semantic" else "preferences"] += 1


def _finish_half_written_supersede(
    deps: Deps, successor: dict[str, Any], now_iso: str, counts: dict[str, int],
) -> None:
    """N17-obs122: a replay converges. The duplicate branch found `successor` — a record that
    already carries this text. If its `supersedes` list names a record that is still ACTIVE,
    the pair is half-written (the pre-r17-n two-write supersede interrupted between its writes,
    a partial item failure inside the one bulk request, or an index inherited from before the
    fix): the earlier attempt's intent — retire that record — is applied now, so exactly one
    record of the pair stays active. The replayed op itself was deduped (the successor is the
    nearest record), which is why nothing else would ever repair the pair: on replay the model
    sees the successor as a candidate and names it (self-supersede guard → noop) or names the
    original (dedupe → noop) — either way the original stayed active for good. Only a record
    the successor already claims is touched; the "duplicate, nothing written" rule for an
    unrelated near-duplicate is unchanged."""
    successor_id = successor["_id"]
    for member in successor.get("supersedes") or []:
        if not member or member == successor_id:
            continue
        doc = deps.opensearch.get_doc(MEMORY_INDEX, member)
        if doc is None or doc.get("superseded_by"):
            continue
        deps.opensearch.update_doc(
            MEMORY_INDEX, member,
            {"superseded_by": successor_id, "superseded_at": now_iso}, refresh=False,
        )
        counts["supersedes"] += 1


def _write_summary(
    deps: Deps, embedder: Any, namespace: str, summary: str, base_doc: dict[str, Any],
    event_id: str, now_iso: str, **extra: Any,
) -> None:
    """Upsert the one summary document of `namespace` (session or actor) — `created_at`
    survives a rewrite, `updated_at` moves."""
    doc_id = summary_record_id(base_doc["store_id"], namespace)
    existing = deps.opensearch.get_doc(MEMORY_INDEX, doc_id) or {}
    deps.opensearch.put_doc(
        MEMORY_INDEX, doc_id,
        {
            **base_doc, "namespace": namespace, "strategy": "summarization",
            "text": json.dumps({"summary": summary}, ensure_ascii=False),
            "embedding": embedder.embed(summary),
            "created_at": existing.get("created_at") or now_iso, "updated_at": now_iso,
            "source_event_ids": [event_id], "confidence": 1.0,
            "last_op": "update" if existing else "add",
            **extra,
        },
        refresh=False,
    )


def _active_count(deps: Deps, filters: list[dict[str, Any]]) -> int:
    out = deps.opensearch.search(
        MEMORY_INDEX,
        {"query": {"bool": {"filter": filters}}, "size": 0, "track_total_hits": True},
    )
    return int(out["hits"]["total"]["value"])


def _consolidate(
    deps: Deps, embedder: Any, strategy: str, namespace: str, cap: int,
    base_doc: dict[str, Any], event_id: str, now_iso: str, counts: dict[str, int],
    warnings: list[str],
) -> None:
    """Cap + re-summarization: when the actor holds more ACTIVE records in `namespace` than
    `cap`, take the oldest (≤ CONSOLIDATE_MAX_RECORDS) and ask the model to merge them; every
    merged group becomes one new record superseding its members. One bounded pass per
    event — a namespace far over its cap converges over the next extractions."""
    store_id = base_doc["store_id"]
    filters = [{"term": {"store_id": store_id}}, {"term": {"namespace": namespace}},
               active_record_filter()]
    total = _active_count(deps, filters)
    if total <= cap:
        return
    take = min(CONSOLIDATE_MAX_RECORDS, max(4, 2 * (total - cap)), total)
    hits = deps.opensearch.search(
        MEMORY_INDEX,
        {"query": {"bool": {"filter": filters}},
         "sort": [{"created_at": {"order": "asc"}}], "size": take,
         "_source": {"excludes": ["embedding"]}},
    )["hits"]["hits"]
    records = {f"r{i + 1}": {"_id": h["_id"], **h["_source"]} for i, h in enumerate(hits)}
    if len(records) < 2:
        return
    listing = [{"id": alias, **_item_fields(r.get("text") or "")} for alias, r in records.items()]
    raw = _chat(deps).converse_text(CONSOLIDATE_PROMPT, json.dumps(listing, ensure_ascii=False))
    groups = _parse_json(raw)
    if not isinstance(groups, list):
        warnings.append(f"{strategy}: consolidation output unparseable")
        return
    used: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        ids = [str(i) for i in (group.get("merges") or []) if str(i) in records
               and str(i) not in used]
        text = _item_text(strategy, _normalize_item(group))
        if len(ids) < 2 or not text:
            continue
        used.update(ids)
        members = [records[i]["_id"] for i in ids]
        confidence = _normalize_item(group)["confidence"]
        new_id = _record_id(store_id, namespace, text)
        sources = sorted({e for i in ids for e in records[i].get("source_event_ids", [])})
        deps.opensearch.put_doc(
            MEMORY_INDEX, new_id,
            _new_record(base_doc, strategy, namespace, text, embedder.embed(text), event_id,
                        now_iso, confidence, "consolidate", supersedes=members,
                        source_event_ids=[*sources, event_id]),
            refresh=False,
        )
        for member in members:
            if member != new_id:
                deps.opensearch.update_doc(
                    MEMORY_INDEX, member, {"superseded_by": new_id, "superseded_at": now_iso},
                    refresh=False,
                )
        counts["consolidated"] += len(members)


def _expire(
    deps: Deps, store_id: str, actor_id: str, ttl_days: int | None, now_iso: str,
    counts: dict[str, int],
) -> None:
    """Soft TTL: mark ACTIVE records of the actor older than `ttl_days` (by `updated_at`,
    else `created_at`) `expired_at` — at most EXPIRE_MAX_PER_EVENT per event, never the
    rolling actor summary, never a hard delete. The hub hides them on read regardless."""
    fresh = ttl_cutoff_filter(datetime.fromisoformat(now_iso), ttl_days)
    if fresh is None:
        return
    hits = deps.opensearch.search(
        MEMORY_INDEX,
        {"query": {"bool": {
            "filter": [{"term": {"store_id": store_id}}, {"term": {"actor_id": actor_id}},
                       active_record_filter()],
            "must_not": [fresh, {"term": {"namespace": actor_summary_namespace(actor_id)}}],
        }}, "sort": [{"created_at": {"order": "asc"}}], "size": EXPIRE_MAX_PER_EVENT,
         "_source": {"excludes": ["embedding"]}},
    )["hits"]["hits"]
    for hit in hits:
        deps.opensearch.update_doc(MEMORY_INDEX, hit["_id"], {"expired_at": now_iso}, refresh=False)
    counts["expired"] += len(hits)


def _roll_actor_summary(
    deps: Deps, embedder: Any, store_id: str, actor_id: str, interval: int,
    base_doc: dict[str, Any], event_id: str, now_iso: str, counts: dict[str, int],
    warnings: list[str],
) -> None:
    """Every `interval` completed extractions of the actor (this one included), rewrite
    `/summaries/{actorId}/_actor` from the newest session summaries (≤ ACTOR_SUMMARY_SOURCES)
    in ≤ ACTOR_SUMMARY_MAX_WORDS words."""
    completed = deps.opensearch.search(
        JOBS_INDEX,
        {"query": {"bool": {"filter": [
            {"term": {"kind": "memory_extract"}}, {"term": {"store_id": store_id}},
            {"term": {"actor_id": actor_id}}, {"term": {"status": "COMPLETED"}},
        ]}}, "size": 0, "track_total_hits": True},
    )["hits"]["total"]["value"]
    if (int(completed) + 1) % max(interval, 1) != 0:
        return
    namespace = actor_summary_namespace(actor_id)
    hits = deps.opensearch.search(
        MEMORY_INDEX,
        {"query": {"bool": {
            "filter": [{"term": {"store_id": store_id}}, {"term": {"actor_id": actor_id}},
                       {"term": {"strategy": "summarization"}},
                       {"prefix": {"namespace": f"/summaries/{actor_id}/"}},
                       active_record_filter()],
            "must_not": [{"term": {"namespace": namespace}}],
        }}, "sort": [{"created_at": {"order": "desc"}}], "size": ACTOR_SUMMARY_SOURCES,
         "_source": {"excludes": ["embedding"]}},
    )["hits"]["hits"]
    summaries: list[str] = []
    for hit in hits:
        try:
            value = json.loads(hit["_source"].get("text") or "")
        except ValueError:
            value = None
        text = value.get("summary") if isinstance(value, dict) else hit["_source"].get("text")
        if isinstance(text, str) and text.strip():
            summaries.append(text.strip())
    if not summaries:
        return
    rolled = _chat(deps).converse_text(
        ACTOR_SUMMARY_PROMPT, "\n".join(f"- {s}" for s in summaries)).strip()
    if not rolled:
        warnings.append("actor summary: empty model output")
        return
    words = rolled.split()
    if len(words) > ACTOR_SUMMARY_MAX_WORDS:
        rolled = " ".join(words[:ACTOR_SUMMARY_MAX_WORDS])
    _write_summary(
        deps, embedder, namespace, rolled, base_doc, event_id, now_iso,
        metadata={"kind": "actor_summary", "sources": len(summaries)},
    )
    counts["actor_summary"] = 1


# ----------------------------------------------------------------------------
# memory_extract
# ----------------------------------------------------------------------------


class ModelResolutionError(RuntimeError):
    """rev-r22 (c): the message's embedding / extraction row could not be turned into a
    client (an Identity provider or secret that does not exist, a refused read, a descriptor
    without the clients) — PERMANENT: the job is FAILED with `failure_code: model_resolution`
    and the message dropped; a transient fault on the way stays a redelivery."""


def _resolve_or_fail(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — classified
        if _is_transient(exc):
            raise
        raise ModelResolutionError(f"model_resolution: {_error_reason(exc)}") from exc


def process_memory_extract(body: dict[str, Any], deps: Deps) -> dict[str, Any]:
    event_key = body["event_key"]
    store_id = body["store_id"]
    actor_id = body["actor_id"]
    session_id = body["session_id"]
    bucket = body.get("bucket") or os.environ.get("LAUNCHPAD_MEMORY_BUCKET", "")
    job_id = "ex" + hashlib.sha1(event_key.encode()).hexdigest()[:16]

    base = {
        "kind": "memory_extract",
        "store_id": store_id,
        "workspace_id": body.get("workspace_id"),
        "actor_id": actor_id,
        "session_id": session_id,
        "strategy_ids": list(EXTRACTED_STRATEGIES),
    }
    # N16-obs32: nothing has been extracted yet, so a jobs-index fault here — the idempotency
    # read or the IN_PROGRESS / SKIPPED upsert — is a plain redelivery, never the raw
    # exception `lambda_handler` would ack as poison.
    try:
        existing = deps.opensearch.get_doc(JOBS_INDEX, job_id)
        if existing and existing.get("status") == "COMPLETED":
            return {"job_id": job_id, "status": "COMPLETED", "skipped": "already completed"}
        if not body.get("long_term", True):
            _job(deps, job_id, status="SKIPPED", **base)
            return {"job_id": job_id, "status": "SKIPPED"}
        _job(deps, job_id, status="IN_PROGRESS", **base)
    except Exception as exc:  # noqa: BLE001 — before any work: redeliver
        raise RetryableError(
            f"job bookkeeping unavailable before extraction: {type(exc).__name__}"
        ) from exc
    warnings: list[str] = []
    try:
        # rev-r22 (c): resolved INSIDE the bookkeeping — a failure marks the job, not just the log
        embedder = _resolve_or_fail(lambda: embedder_for(body, deps))  # D21: the embedding row
        deps = replace(deps, chat=_resolve_or_fail(lambda: chat_model_for(body, deps)))  # obs252
        try:
            event = json.loads(deps.s3.get_object(Bucket=bucket, Key=event_key)["Body"].read())
        except ClientError as exc:
            if _is_missing_s3(exc):
                # the agent (and with it its memory snapshots) was deleted after the
                # turn was enqueued — nothing to extract, nothing a retry could recover
                reason = (
                    f"event {event_key} is gone (agent or its memory snapshots deleted) — "
                    "message dropped"
                )
                _job_soft(deps, job_id, status="FAILED", failure_reason=reason)
                raise DroppedMessage(reason) from exc
            _job_soft(
                deps, job_id, status="FAILED",
                failure_reason=f"event {event_key} unreadable: {exc}",
            )
            raise RetryableError(str(exc)) from exc

        if not has_assistant_answer(event):
            # N17-obs304: deterministic guard — no assistant answer, no model call, no record
            reason = "no assistant answer in the turn (failed or empty) — nothing to extract"
            _job_soft(deps, job_id, status="SKIPPED", skip_reason=reason, warnings=warnings)
            return {"job_id": job_id, "status": "SKIPPED", "skipped": reason}

        context = _previous_events(deps, bucket, event_key, deps.env["context_events"])
        # N17-obs237: fitted BEFORE any model call; one shrink in reserve for a size refusal
        prompt = _Prompt(context, event, int(deps.env.get("max_prompt_chars", MAX_PROMPT_CHARS)),
                         warnings)

        merge = normalize_merge_settings(body.get("merge"))
        counts = {
            "facts": 0, "preferences": 0, "summaries": 0, "duplicates": 0,
            "updates": 0, "supersedes": 0, "deletes": 0, "noops": 0,
            "consolidated": 0, "expired": 0, "actor_summary": 0,
        }
        now_iso = deps.now().isoformat()
        base_doc = {
            "store_id": store_id,
            "workspace_id": body.get("workspace_id"),
            "actor_id": actor_id,
            "session_id": None,
            "metadata": {},
        }

        # All core model reads finish before applying operations. A bounded output retry or
        # a deadline failure cannot replay an already-applied earlier strategy's mutations.
        plans: list[tuple[str, str, list[Any], dict[str, dict[str, Any]]]] = []
        for strategy, system_prompt, namespace in (
            ("semantic", FACTS_PROMPT, f"/facts/{actor_id}"),
            ("user_preference", PREFERENCES_PROMPT, f"/preferences/{actor_id}"),
        ):
            candidates = _call_with_shrink(
                prompt, lambda t, ns=namespace: _candidates(deps, embedder, store_id, ns, t))
            raw = _call_with_shrink(
                prompt, lambda t, sp=system_prompt, c=candidates: _chat(deps).converse_text(
                    sp, _merge_user_message(c, t)))
            items = _parse_json(raw)
            if not isinstance(items, list):
                warnings.append(f"{strategy}: unparseable model output")
                continue
            plans.append((strategy, namespace, items[:MAX_ITEMS_PER_STRATEGY], candidates))

        summary = _call_with_shrink(
            prompt, lambda t: _chat(deps).converse_text(SUMMARY_PROMPT, t)).strip()
        for strategy, namespace, items, candidates in plans:
            for item in items:
                _apply_op(
                    deps, embedder, strategy, namespace, _normalize_item(item), candidates,
                    base_doc, event["eventId"], now_iso, counts, warnings,
                )
        if summary:
            namespace = f"/summaries/{actor_id}/{session_id}"
            _write_summary(
                deps, embedder, namespace, summary, {**base_doc, "session_id": session_id},
                event["eventId"], now_iso,
            )
            counts["summaries"] = 1

        # Merge strategy v2 — bounded housekeeping, one pass per event:
        for strategy, namespace, cap_key in (
            ("semantic", f"/facts/{actor_id}", "max_facts_per_actor"),
            ("user_preference", f"/preferences/{actor_id}", "max_preferences_per_actor"),
        ):
            try:
                _consolidate(
                    deps, embedder, strategy, namespace, int(merge[cap_key]), base_doc,
                    event["eventId"], now_iso, counts, warnings,
                )
            except (ExtractionDeadlineError, ModelOutputTruncatedError) as exc:
                warnings.append(f"{strategy}: optional consolidation skipped: {exc}")
        _expire(deps, store_id, actor_id, merge["long_term_ttl_days"], now_iso, counts)
        try:
            _roll_actor_summary(
                deps, embedder, store_id, actor_id, int(merge["actor_summary_interval"]),
                base_doc, event["eventId"], now_iso, counts, warnings,
            )
        except (ExtractionDeadlineError, ModelOutputTruncatedError) as exc:
            warnings.append(f"optional actor summary skipped: {exc}")

        deps.opensearch.request("POST", f"/{MEMORY_INDEX}/_refresh")
        # the records are in the index: a failed COMPLETED write leaves a stale IN_PROGRESS
        # row (logged), never a redelivery that would re-run the model for bookkeeping
        _job_soft(
            deps, job_id, status="COMPLETED", counts=counts, warnings=warnings,
            failure_reason=None,
        )
        return {"job_id": job_id, "status": "COMPLETED", "counts": counts}
    except (RetryableError, DroppedMessage):
        raise
    except (ExtractionDeadlineError, ModelOutputTruncatedError) as exc:
        code = (
            "extraction_deadline" if isinstance(exc, ExtractionDeadlineError)
            else "model_output_truncated"
        )
        _job_soft(
            deps, job_id, status="FAILED", failure_reason=str(exc),
            failure_code=code, warnings=warnings,
        )
        # Core preparation has not applied any ops. Preserve SQS retry/DLQ semantics;
        # a budget exhaustion is not an acknowledgment that silently discards the event.
        raise RetryableError(str(exc)) from exc
    except InputTooLongError as exc:
        # N17-obs237: PERMANENT — the prompt was already shrunk once; a redelivery would send
        # the same bytes again (4 receives → DLQ, extraction silently lost)
        reason = f"input_too_long: {exc}"
        _job_soft(deps, job_id, status="FAILED", failure_reason=reason,
                  failure_code="input_too_long", warnings=warnings)
        raise DroppedMessage(reason) from exc
    except ModelResolutionError as exc:
        _job_soft(deps, job_id, status="FAILED", failure_reason=str(exc),
                  failure_code="model_resolution", warnings=warnings)
        raise DroppedMessage(str(exc)) from exc
    except (ClientError, HTTPError) as exc:
        if is_model_validation_error(exc):
            # N17-obs535: a 400 the request caused (an invalid model id, a capability the catalog
            # row got wrong, a provider refusing the request) — FAILED with the reason so the hub
            # shows it, then REDELIVERED: the message reaches the DLQ after maxReceiveCount
            # instead of being acknowledged and lost (the pre-2026-10-30 behaviour)
            detail = _error_reason(exc)
            _job_soft(deps, job_id, status="FAILED", failure_reason=f"model_validation: {detail}",
                      failure_code="model_validation", warnings=warnings)
            raise ModelValidationFailure(detail, model=_chat_model_id(deps)) from exc
        _job_soft(deps, job_id, status="FAILED", failure_reason=f"{type(exc).__name__}: {exc}")
        raise RetryableError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - last-resort job bookkeeping before retry
        _job_soft(deps, job_id, status="FAILED", failure_reason=f"{type(exc).__name__}: {exc}")
        raise RetryableError(str(exc)) from exc


# ----------------------------------------------------------------------------
# kb_ingest
# ----------------------------------------------------------------------------


def _etag(value: Any) -> str | None:
    """S3 returns the ETag quoted (`"d41d8…"`); stored and compared bare."""
    text = str(value or "").strip().strip('"')
    return text or None


def _unchanged(existing: dict[str, Any] | None, etag: str | None, size: Any) -> bool:
    """N17-obs246: the document's record says INDEXED and names the same ETag and size as the
    S3 listing — nothing to re-embed. A record without an etag (pre-obs246) is `changed` once;
    so is any other status — FAILED / UNSUPPORTED, and INDEXING (N17-obs274 pass 4: a chain
    that died between its slice 0 and its last slice is re-done by the next sync)."""
    if not existing or existing.get("status") != "INDEXED":
        return False
    stored = _etag(existing.get("etag"))
    return bool(stored) and stored == etag and existing.get("size_bytes") == size


def _after(stamp: Any, moment: datetime) -> bool:
    """``stamp`` (an ISO string on a record) is later than ``moment``; False when unparsable."""
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed > moment


def _sweep_deleted_documents(deps: Deps, index: str, kb_id: str, ds_id: str,
                             present_uris: set[str], *, bucket: str,
                             listed_at: datetime) -> int:
    """N17-obs246: kb-docs records of this data source whose S3 object is no longer under the
    prefix lose their chunks (`delete_by_query` on `doc_uri`) and their record. N17-obs255
    (rev-r22): the job-start listing is not trusted on its own — a concurrent or duplicate
    sync for the same data source may have indexed a document this listing never saw — so a
    candidate is swept only when (a) its record was not written after this job listed
    (`indexed_at` ≤ ``listed_at``) and (b) `HeadObject` on its key says the object is gone
    (404 / NoSuchKey / NotFound); a present object is kept with one INFO line. Returns the
    number of documents removed."""
    removed = 0
    start = 0
    stale: list[tuple[str, str]] = []
    prefix_uri = f"s3://{bucket}/"
    while True:
        result = deps.opensearch.search(KB_DOCS_INDEX, {
            "query": {"bool": {"filter": [{"term": {"kb_id": kb_id}},
                                          {"term": {"ds_id": ds_id}}]}},
            "sort": [{"name": {"order": "asc"}}],
            "from": start, "size": KB_DOCS_SWEEP_PAGE,
            "_source": {"excludes": ["embedding"]},
        })
        hits = result["hits"]["hits"]
        for hit in hits:
            source = hit["_source"]
            uri = str(source.get("uri") or "")
            if not uri or uri in present_uris:
                continue
            if _after(source.get("indexed_at"), listed_at):
                continue  # indexed by another job after this listing — not stale
            stale.append((hit["_id"], uri))
        if len(hits) < KB_DOCS_SWEEP_PAGE:
            break
        start += KB_DOCS_SWEEP_PAGE
    for record_id, uri in stale:
        if uri.startswith(prefix_uri):
            try:
                deps.s3.head_object(Bucket=bucket, Key=uri[len(prefix_uri):])
            except ClientError as exc:
                if not _is_missing_s3(exc):
                    raise
            else:
                logger.info("kb_ingest: document %s is still in S3 (the job-start listing "
                            "missed it) — kept, not swept", record_id)
                continue
        deps.opensearch.delete_by_query(index, {"term": {"doc_uri": uri}})
        deps.opensearch.delete_doc(KB_DOCS_INDEX, record_id)
        removed += 1
    return removed


def _doc_text(key: str, data: bytes) -> tuple[str | None, str | None]:
    """`(text, None)` on success, `(None, reason)` otherwise — never raises."""
    if len(data) > MAX_DOC_BYTES:
        return None, "document larger than 2 MB"
    lower = key.lower()
    for ext in NATIVE_TEXT_EXT:
        if not lower.endswith(ext):
            continue
        text = data.decode("utf-8", errors="replace")
        if ext == ".json":
            try:
                text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            except ValueError:
                pass  # not actually valid JSON — index the raw text anyway
        elif ext in (".html", ".htm"):
            text = html.unescape(re.sub(r"<[^>]+>", " ", text))
        return text, None
    name = key.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1] if "." in name else "unknown"
    return None, f"unsupported file type .{ext}"


def _embed_with_retry(embedder: Any, text: str, deps: Deps) -> list[float]:
    """`embedder.embed(text)` retried on a transient upstream fault (N17-obs120): the shared
    `is_upstream_transient` decides, `EMBED_RETRY_BACKOFF_S` bounds the attempts (one more than
    its length) and `deps.sleep` waits between them. The last transient failure — and any
    permanent one at once — propagates to the per-document guard (FAILED with the type)."""
    for attempt in range(len(EMBED_RETRY_BACKOFF_S) + 1):
        try:
            return embedder.embed(text)
        except Exception as exc:  # noqa: BLE001 — classified below, re-raised when not retried
            if attempt >= len(EMBED_RETRY_BACKOFF_S) or not is_upstream_transient(exc):
                raise
            wait = backoff_for(EMBED_RETRY_BACKOFF_S, attempt)
            logger.warning(
                "kb_ingest: transient embedding fault (%s), retry %d/%d in %.0fs",
                type(exc).__name__, attempt + 1, len(EMBED_RETRY_BACKOFF_S), wait,
            )
            deps.sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def _embed_chunks(embedder: Any, chunks: list[str], deps: Deps) -> list[list[float]]:
    """The embeddings of ``chunks`` in order (N17-obs247): `EMBED_WORKERS` threads, each chunk
    through `_embed_with_retry` (the N17-obs120 transient retry per item). The first failing
    chunk's exception propagates once every submitted call has finished."""
    if len(chunks) <= 1:
        return [_embed_with_retry(embedder, chunk, deps) for chunk in chunks]
    with ThreadPoolExecutor(max_workers=min(EMBED_WORKERS, len(chunks))) as pool:
        return list(pool.map(lambda chunk: _embed_with_retry(embedder, chunk, deps), chunks))


def _stamp(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _plan_document(deps: Deps, bucket: str, prefix: str, kb_id: str, ds_id: str,
                   obj: dict[str, Any]) -> dict[str, Any]:
    """What the planner knows about one original object without reading its body: identity
    (`doc_id`), the listing's ETag + size (N17-obs246 — for a PDF the text sidecar's ETag, by
    `HeadObject`), the upload stamp. Travels in the `kb_ingest_doc` message."""
    key = str(obj["Key"])
    name = key.rsplit("/", 1)[-1]
    uri = f"s3://{bucket}/{key}"
    etag = _etag(obj.get("ETag"))
    if key.lower().endswith(".pdf"):
        try:
            head = deps.s3.head_object(Bucket=bucket, Key=f"{prefix}{SIDECAR_DIR}{name}.txt")
            etag = _etag(head.get("ETag")) or etag
        except ClientError:
            pass  # no sidecar: the document stage records UNSUPPORTED
    return {
        "key": key, "name": name, "uri": uri,
        "doc_id": f"{kb_id}:{ds_id}:{hashlib.sha1(uri.encode()).hexdigest()[:16]}",
        "etag": etag, "size": obj.get("Size"), "uploaded_at": _stamp(obj.get("LastModified")),
    }


def _read_document_text(deps: Deps, ctx: dict[str, Any], plan: dict[str, Any]
                        ) -> tuple[str | None, str | None, str | None]:
    """`(text, reason, etag)` of one document — the text to chunk (None with the reason when
    it cannot be indexed) and the ETag of what was read (PDF: the text sidecar's — that text
    is what "unchanged" means, obs246; the object's otherwise). Raises on S3 errors other than
    a missing PDF sidecar."""
    bucket, prefix = ctx["bucket"], ctx["prefix"]
    key, name = plan["key"], plan["name"]
    if key.lower().endswith(".pdf"):
        sidecar_key = f"{prefix}{SIDECAR_DIR}{name}.txt"
        try:
            sidecar_obj = deps.s3.get_object(Bucket=bucket, Key=sidecar_key)
            sidecar_bytes = sidecar_obj["Body"].read()
        except ClientError:
            return (None, "pdf without an extracted text sidecar (upload it through the console)",
                    None)
        etag = _etag(sidecar_obj.get("ETag"))
        if len(sidecar_bytes) > MAX_DOC_BYTES:
            return None, "document larger than 2 MB", etag
        return sidecar_bytes.decode("utf-8", "replace"), None, etag
    obj = deps.s3.get_object(Bucket=bucket, Key=key)
    text, reason = _doc_text(key, obj["Body"].read())
    return text, reason, _etag(obj.get("ETag"))


def _index_document(deps: Deps, embedder: Any, ctx: dict[str, Any], plan: dict[str, Any]
                    ) -> str:
    """Read, chunk, embed and write ONE document and its kb-docs record in this invocation
    (the inline path: no queue to continue on); never raises. Returns the record's status:
    `INDEXED` / `UNSUPPORTED` / `FAILED`."""
    return _index_slice(deps, embedder, ctx, plan)["status"]


_INGEST_MAPPED: set[str] = set()


def _ensure_ingest_mapping(deps: Deps, index: str) -> None:
    """N17-obs274: the KB index maps `ingest` as a keyword (term-matchable) before the first
    stamped chunk lands — additive `PUT _mapping`, once per index per container. The KB index
    body lives in the byte-mirrored core, so the field is added here rather than at creation;
    indices created before this change gain it the same way."""
    if index in _INGEST_MAPPED:
        return
    live = deps.opensearch.request("GET", f"/{index}/_mapping") or {}
    entry = live.get(index) or (next(iter(live.values())) if len(live) == 1 else {}) or {}
    props = ((entry.get("mappings") or {}).get("properties")) or {}
    if INGEST_FIELD not in props:
        deps.opensearch.request("PUT", f"/{index}/_mapping",
                                {"properties": {INGEST_FIELD: {"type": "keyword"}}})
    elif (props[INGEST_FIELD] or {}).get("type") != "keyword":
        # rev-r22 pass 2 nit: an existing field of another type cannot be changed in place
        # (OpenSearch refuses); the term filters on it may then miss — say so, never PUT
        logger.warning("kb_ingest: index %s maps `%s` as %r, not keyword — the stamp filters "
                       "may not match; recreate the index (full rebuild after re-creating the "
                       "knowledge base)", index, INGEST_FIELD, (props[INGEST_FIELD] or {}).get(
                           "type"))
    _INGEST_MAPPED.add(index)


def _ingest_stamp(ctx: dict[str, Any]) -> str:
    return f"{ctx['job_id']}:{ctx.get('run') or ''}"


def _chunk_id(uri: str, i: int, stamp: str) -> str:
    return hashlib.sha1(f"{uri}#{i}#{stamp}".encode()).hexdigest()[:32]


def _not_stamped(uri: str, stamp: str) -> dict[str, Any]:
    """This document's chunks written by any OTHER sync (legacy chunks without a stamp
    included)."""
    return {"bool": {"filter": [{"term": {"doc_uri": uri}}],
                     "must_not": [{"term": {INGEST_FIELD: stamp}}]}}


def _stamped(uri: str, stamp: str) -> dict[str, Any]:
    return {"bool": {"filter": [{"term": {"doc_uri": uri}}, {"term": {INGEST_FIELD: stamp}}]}}


def _lease_id(doc_id: str) -> str:
    return f"lease:{doc_id}"


def _take_lease(deps: Deps, ctx: dict[str, Any], plan: dict[str, Any], stamp: str) -> None:
    deps.opensearch.put_doc(JOBS_INDEX, _lease_id(plan["doc_id"]), {
        "kind": KIND_KB_DOC_LEASE, "kb_id": ctx["kb_id"], "ds_id": ctx["ds_id"],
        "job_id": ctx["job_id"], "doc_id": plan["doc_id"], "owner": stamp,
        "updated_at": deps.now().isoformat(),
    }, refresh=False)  # a GET by id is realtime — no refresh needed


def _holds_lease(deps: Deps, plan: dict[str, Any], stamp: str) -> bool:
    """False when a later slice 0 (another sync) took the document; a missing lease (swept
    with its data source, or written by an older Lambda) does not stop the chain."""
    lease = deps.opensearch.get_doc(JOBS_INDEX, _lease_id(plan["doc_id"]))
    return lease is None or lease.get("owner") == stamp


def _embed_slice(deps: Deps, embedder: Any, part: list[str], started: float | None
                 ) -> list[list[float]]:
    """The embeddings of ``part`` (N17-obs274): groups of `EMBED_WORKERS`; before each group
    after the first, stop when `elapsed + slowest group so far` would pass `SLICE_BUDGET_S`
    (``started`` None = no budget, the whole part)."""
    if started is None:
        return _embed_chunks(embedder, part, deps)
    vectors: list[list[float]] = []
    slowest = 0.0
    for group in range(0, len(part), EMBED_WORKERS):
        if vectors and deps.clock() - started + slowest > SLICE_BUDGET_S:
            break
        group_started = deps.clock()
        vectors.extend(_embed_chunks(embedder, part[group:group + EMBED_WORKERS], deps))
        slowest = max(slowest, deps.clock() - group_started)
    return vectors


def _index_slice(deps: Deps, embedder: Any, ctx: dict[str, Any], plan: dict[str, Any], *,
                 slice_start: int = 0, started: float | None = None) -> dict[str, Any]:
    """One slice of ONE document (N17-obs270 / obs274); never raises. ``started`` (a
    `deps.clock()` value: when the message was picked up) None = the whole document now, else
    at most `SLICE_CHUNKS` chunks from ``slice_start`` within the `SLICE_BUDGET_S` arithmetic.
    Returns `{"status": …}` — a final record status (`INDEXED` / `UNSUPPORTED` / `FAILED` /
    `SUPERSEDED`), or `CONTINUE` with `next_start` and the `etag` of the text read (the
    continuation message carries both)."""
    job_id, kb_id, ds_id = ctx["job_id"], ctx["kb_id"], ctx["ds_id"]
    index = ctx["index"]
    name, uri, doc_id = plan["name"], plan["uri"], plan["doc_id"]
    stamp = _ingest_stamp(ctx)
    base_doc = {
        "kb_id": kb_id, "ds_id": ds_id, "uri": uri, "name": name,
        "size_bytes": plan.get("size"), "etag": plan.get("etag"),
        "uploaded_at": plan.get("uploaded_at"),
    }
    # N17-obs274 (rev-r22 pass 3): the lease is settled BEFORE any work — and a lease that
    # cannot be written / read here is not a lost lease: the slice is redelivered (nothing was
    # done yet), never stopped as SUPERSEDED on a transient error
    try:
        if slice_start == 0:
            _take_lease(deps, ctx, plan, stamp)  # N17-obs274: the newest slice 0 owns the doc
            held = True
        else:
            held = _holds_lease(deps, plan, stamp)
    except Exception as exc:  # noqa: BLE001 — classified: always a redelivery
        raise RetryableError(
            f"document {doc_id}: lease unavailable before slice {slice_start}: "
            f"{type(exc).__name__}"
        ) from exc
    try:
        if not held:
            return _supersede_document(deps, ctx, plan, base_doc, None, stamp, lease_lost=True)
        try:
            text, reason, read_etag = _read_document_text(deps, ctx, plan)
        except ClientError as exc:
            if _is_missing_s3(exc) and _source_gone(deps, ctx, plan):
                # deleted since the listing (N17-obs277: by a data-source delete, too) — no
                # record is written for a source that is gone. Confirmed by a HeadObject: a
                # NoSuchKey the object's HEAD contradicts stays the failure it was before
                return _drop_gone_document(deps, ctx, plan, stamp)
            if slice_start > 0 and _is_missing_s3(exc):
                return _supersede_document(deps, ctx, plan, base_doc, None, stamp)
            raise
        if text is None and _source_gone(deps, ctx, plan):
            # N17-obs277: a PDF whose sidecar went with its deleted source reads as None
            return _drop_gone_document(deps, ctx, plan, stamp)
        if slice_start > 0 and (text is None or (read_etag and plan.get("etag")
                                                 and read_etag != plan["etag"])):
            # the object changed (or vanished) since slice 0 read it: these chunks belong to
            # a version that is gone — stop; the next sync indexes what is there now
            return _supersede_document(deps, ctx, plan, base_doc, read_etag, stamp)
        if read_etag:
            base_doc["etag"] = read_etag
        if text is None:
            deps.opensearch.put_doc(
                KB_DOCS_INDEX, doc_id,
                {**base_doc, "status": "UNSUPPORTED", "status_reason": reason,
                 "indexed_at": None, "chunk_count": 0},
            )
            return {"status": "UNSUPPORTED"}

        chunks = chunk_text(text)
        truncated = len(chunks) > MAX_CHUNKS
        chunks = chunks[:MAX_CHUNKS]
        part = chunks[slice_start:] if started is None else \
            chunks[slice_start:slice_start + SLICE_CHUNKS]
        vectors = _embed_slice(deps, embedder, part, started)
        part = part[:len(vectors)]
        _ensure_ingest_mapping(deps, index)
        # N17-obs247: embedded before the delete — the old chunks stay searchable while the
        # new ones are computed; N17-obs274: slice 0 removes only OTHER syncs' chunks, so a
        # late duplicate of slice 0 never deletes what its own chain already wrote
        if slice_start == 0:
            # N17-obs278: a newer sync's slice 0 may have taken the document while this one
            # embedded — stop BEFORE the demotion and the delete below (nothing of this chain
            # is written yet), so a single-slice chain that lost the lease never touches the
            # owner's record or chunks. Unreadable here = a redelivery (nothing written).
            try:
                still_owner = _holds_lease(deps, plan, stamp)
            except Exception as exc:  # noqa: BLE001 — classified: a redelivery
                raise LeaseUnreadable(
                    f"document {doc_id}: lease unavailable after embedding slice 0: "
                    f"{type(exc).__name__}"
                ) from exc
            if not still_owner:
                return _supersede_document(deps, ctx, plan, base_doc, None, stamp,
                                           lease_lost=True)
        if slice_start == 0:
            # N17-obs274 (rev-r22 pass 4): before the old chunks go, the record stops saying
            # INDEXED — a chain that dies after this (DLQ, repeated timeouts) leaves INDEXING,
            # which every later sync treats as changed, never an INDEXED record over a partial
            # index. Skipped when THIS sync already finished the document (a late twin of
            # slice 0 must not demote its own chain's INDEXED record).
            current = deps.opensearch.get_doc(KB_DOCS_INDEX, doc_id)
            if not (current and current.get("status") == "INDEXED"
                    and current.get(INGEST_FIELD) == stamp):
                deps.opensearch.put_doc(KB_DOCS_INDEX, doc_id, {
                    **base_doc, "status": INDEXING_STATUS, "status_reason": None,
                    "indexed_at": None, "chunk_count": 0, INGEST_FIELD: stamp,
                    "indexing_started_at": deps.now().isoformat(),
                })
            deps.opensearch.delete_by_query(index, _not_stamped(uri, stamp))
        now_iso = deps.now().isoformat()
        chunk_docs = [
            (
                _chunk_id(uri, i, stamp),
                {
                    "kb_id": kb_id, "ds_id": ds_id, "doc_uri": uri, "doc_name": name,
                    "chunk_index": i, "text": chunk, "embedding": vector,
                    "metadata": {}, "indexed_at": now_iso, INGEST_FIELD: stamp,
                },
            )
            for i, (chunk, vector) in enumerate(zip(part, vectors, strict=True),
                                                start=slice_start)
        ]
        # N17-obs245: `BULK_BATCH_CHUNKS` per request, one timeout retry per batch
        _bulk_in_batches(deps, index, chunk_docs)
        next_start = slice_start + len(part)
        if next_start < len(chunks):
            return {"status": "CONTINUE", "next_start": next_start, "etag": base_doc["etag"]}
        if not _holds_lease(deps, plan, stamp):
            # a newer sync took the document while this last slice ran: it finishes it.
            # N17-obs278: checked on EVERY last slice — slice 0 of a single-slice document
            # included (one extra GET) — so a chain that lost the lease never writes INDEXED
            return _supersede_document(deps, ctx, plan, base_doc, None, stamp, lease_lost=True)
        # N17-obs274: whatever another sync wrote for this document meanwhile (an older chain's
        # in-flight slice after our slice 0) leaves with the last slice
        deps.opensearch.delete_by_query(index, _not_stamped(uri, stamp))
        # N-SCN-obs12: `INDEXED` must mean searchable. The chunks above were bulk-written
        # without a refresh (N-SCN-obs11) while this record is written WITH one, so the
        # hub's `documents` count used to flip before the vectors were visible to k-NN and
        # a retrieve issued right after "2/2 indexed" ranked only the earlier document's
        # chunks. One refresh of the KB index per document, before its record turns INDEXED.
        deps.opensearch.request("POST", f"/{index}/_refresh")
        # N17-obs274 (rev-r22 pass 2): INDEXED must mean complete — this sync's chunks of the
        # document, counted after the refresh, must be exactly the document's chunk count. A
        # hole from any race (the documented residual) is recorded FAILED, so the next
        # incremental sync re-indexes the document instead of skipping it as unchanged.
        present = int((deps.opensearch.request(
            "POST", f"/{index}/_count", {"query": _stamped(uri, stamp)}) or {}).get("count", -1))
        if present != len(chunks):
            if _source_gone(deps, ctx, plan):
                # N17-obs277: the hole is the data-source delete's chunk sweep — no FAILED
                # record for a source that is gone
                return _drop_gone_document(deps, ctx, plan, stamp)
            logger.warning("kb_ingest job=%s document %s: %d of %d chunks present after the "
                           "last slice — recorded FAILED", job_id, doc_id, present, len(chunks))
            # pass 3 nit: this sync's partial set does not stay searchable under FAILED
            deps.opensearch.delete_by_query(index, _stamped(uri, stamp))
            deps.opensearch.put_doc(
                KB_DOCS_INDEX, doc_id,
                {**base_doc, "status": "FAILED", "status_reason": INCOMPLETE_WRITE_REASON,
                 "indexed_at": None, "chunk_count": 0},
            )
            return {"status": "FAILED"}
        if _source_gone(deps, ctx, plan):
            # N17-obs277: the data source (or the document) was deleted while this last slice
            # ran — its cleanup may already have swept the index and the kb-docs rows, so
            # writing INDEXED now would leave an orphan record over orphan chunks
            return _drop_gone_document(deps, ctx, plan, stamp)
        deps.opensearch.put_doc(
            KB_DOCS_INDEX, doc_id,
            {**base_doc, "status": "INDEXED",
             "status_reason": "truncated" if truncated else None,
             "indexed_at": deps.now().isoformat(), "chunk_count": len(chunks),
             INGEST_FIELD: stamp},  # pass 4: which sync this INDEXED belongs to
        )
        return {"status": "INDEXED"}
    except LeaseUnreadable:
        raise  # N17-obs278: nothing written — the slice is redelivered
    except Exception as exc:  # noqa: BLE001 - one bad document never fails the job
        # N17-obs245: a timed-out index write stores the readable reason; the transport
        # exception (its text names the domain host) stays in this log line only
        cause = exc.__cause__
        logger.warning(
            "kb_ingest job=%s document %s FAILED (%s%s)", job_id, doc_id,
            type(exc).__name__, f" ← {type(cause).__name__}" if cause else "",
        )
        # rev-r22 (obs245 nit): a batch that failed after earlier batches landed must not leave
        # half a document searchable — best-effort delete of what was written. N17-obs274
        # (rev-r22 pass 2): ownership is ALWAYS read from the lease — a slice 0 too, since a
        # newer sync's slice 0 may have taken the document while this one was writing — and an
        # unreadable lease means NOT owner. This sync's own chunks always go; the other stamps
        # (legacy / older syncs) only while still owner; the FAILED record only by the owner.
        # pass 3: three outcomes — the lease READ names this sync (owner: everything of the
        # document goes, FAILED record); it names ANOTHER sync (only own chunks, no record —
        # the owner writes it); it cannot be read (only own chunks, but STILL the FAILED
        # record: conservative — an owner finishing later overwrites it with INDEXED, one that
        # already finished gets FAILED and the next sync redoes the document; skipping the
        # record could leave an INDEXED record over a document whose chunks are gone)
        try:
            lease_state = "own" if _holds_lease(deps, plan, stamp) else "other"
        except Exception:  # noqa: BLE001 — cannot tell: never touch another sync's chunks
            lease_state = "unreadable"
        cleanups = [_stamped(uri, stamp)] + (
            [_not_stamped(uri, stamp)] if lease_state == "own" else [])
        for query in cleanups:
            try:
                deps.opensearch.delete_by_query(index, query)
            except Exception as cleanup:  # noqa: BLE001 — the record below still says FAILED
                logger.warning("kb_ingest job=%s document %s: could not remove its partial "
                               "chunks (%s)", job_id, doc_id, type(cleanup).__name__)
        if lease_state == "other":
            logger.info("kb_ingest job=%s document %s: failed after a newer sync took it — "
                        "its record is the owner's", job_id, doc_id)
            return {"status": "SUPERSEDED"}
        deps.opensearch.put_doc(
            KB_DOCS_INDEX, doc_id,
            {**base_doc, "status": "FAILED", "status_reason": _stored_reason(exc),
             "indexed_at": None, "chunk_count": 0},
        )
        return {"status": "FAILED"}


def _source_gone(deps: Deps, ctx: dict[str, Any], plan: dict[str, Any]) -> bool:
    """N17-obs277: is the document's original object gone? One `HeadObject` of its key.

    Why this signal: the data-source delete (`kb_cleanup` scope `data_source`) runs `s3` →
    `index` → `docs`, so the S3 purge is the FIRST cleanup step and the object is the earliest
    trace to disappear; the lease row goes only with the LAST step (`docs`, N17-obs276), after
    the chunk sweep, and a missing lease also means "an older Lambda" (`_holds_lease`). The KB
    delete purges `kb/<kb_id>/` first, too. An INDEXED write that passes this check precedes
    the purge, so the later `index` / `docs` steps remove what it wrote. Only a missing key
    (404 family) counts; any other error says "cannot tell" — logged, the record is written
    (the delete's later steps still sweep it)."""
    try:
        deps.s3.head_object(Bucket=ctx["bucket"], Key=plan["key"])
    except ClientError as exc:
        if _is_missing_s3(exc):
            return True
        logger.warning("kb_ingest job=%s document %s: source check failed (%s) — recording "
                       "it", ctx["job_id"], plan["doc_id"], type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 — cannot tell: never a lost document
        logger.warning("kb_ingest job=%s document %s: source check failed (%s) — recording "
                       "it", ctx["job_id"], plan["doc_id"], type(exc).__name__)
    return False


def _drop_gone_document(deps: Deps, ctx: dict[str, Any], plan: dict[str, Any],
                        stamp: str) -> dict[str, Any]:
    """N17-obs277: the document's source is gone. THIS sync's chunks, its INDEXING record and
    its lease row are removed (each only when it names this sync's stamp — another sync's
    rows are theirs); no record is written. `SUPERSEDED`: the job counts it as failed (this
    sync did not index it) and completes. A KB index already dropped (a KB delete) is fine."""
    try:
        deps.opensearch.delete_by_query(ctx["index"], _stamped(plan["uri"], stamp))
    except Exception as exc:
        if getattr(exc, "status", None) != 404:  # an OpenSearchError (matched by status)
            raise
    record = deps.opensearch.get_doc(KB_DOCS_INDEX, plan["doc_id"])
    if record and record.get(INGEST_FIELD) == stamp:
        deps.opensearch.delete_doc(KB_DOCS_INDEX, plan["doc_id"])
    lease = deps.opensearch.get_doc(JOBS_INDEX, _lease_id(plan["doc_id"]))
    if lease and lease.get("owner") == stamp:
        deps.opensearch.delete_doc(JOBS_INDEX, _lease_id(plan["doc_id"]), refresh=False)
    logger.info("kb_ingest job=%s document %s: source deleted mid-ingest — stopped, nothing "
                "recorded", ctx["job_id"], plan["doc_id"])
    return {"status": "SUPERSEDED"}


def _supersede_document(deps: Deps, ctx: dict[str, Any], plan: dict[str, Any],
                        base_doc: dict[str, Any], current_etag: str | None, stamp: str, *,
                        lease_lost: bool = False) -> dict[str, Any]:
    """N17-obs270 / obs274: this chain stops — the object was replaced (or deleted) since its
    slice 0 read it, or (``lease_lost``) a newer sync's slice 0 took the document. The chunks
    THIS sync wrote are removed (no partial version stays searchable); other syncs' chunks are
    theirs. On a replaced object the kb-docs record says FAILED with `SUPERSEDED_REASON` so the
    next sync treats the document as changed — unless a newer sync already recorded the current
    version INDEXED. On a lost lease the record is the new owner's to write."""
    deps.opensearch.delete_by_query(ctx["index"], _stamped(plan["uri"], stamp))
    if lease_lost:
        logger.info("kb_ingest job=%s document %s: taken over by a newer sync — stopped",
                    ctx["job_id"], plan["doc_id"])
        return {"status": "SUPERSEDED"}
    record = deps.opensearch.get_doc(KB_DOCS_INDEX, plan["doc_id"])
    if (record and record.get("status") == "INDEXED" and current_etag
            and _etag(record.get("etag")) == current_etag):
        logger.info("kb_ingest job=%s document %s: replaced mid-ingest and already indexed by "
                    "a newer sync — stopped", ctx["job_id"], plan["doc_id"])
    else:
        logger.info("kb_ingest job=%s document %s: replaced mid-ingest (etag %s → %s) — stopped, "
                    "the next sync indexes the new version", ctx["job_id"], plan["doc_id"],
                    plan.get("etag"), current_etag or "gone")
        deps.opensearch.put_doc(KB_DOCS_INDEX, plan["doc_id"], {
            **base_doc, "status": "FAILED", "status_reason": SUPERSEDED_REASON,
            "indexed_at": None, "chunk_count": 0,
        })
    return {"status": "SUPERSEDED"}


def _stored_reason(exc: BaseException) -> str:
    """The `status_reason` a FAILED document record carries (rev-r22, obs245): the readable
    timeout text; for any other transport-level error (urllib3 / socket / botocore — their
    messages name the domain host) the class plus a fixed sentence; otherwise the class and the
    message (an `OpenSearchError` body, a decode error) bounded to 300 characters."""
    if isinstance(exc, IndexWriteTimeout):
        return str(exc)
    if isinstance(exc, (urllib3.exceptions.HTTPError, OSError, BotoCoreError)) and \
            not isinstance(exc, ClientError):
        return f"{type(exc).__name__}: transport error while indexing — run the sync again"
    return f"{type(exc).__name__}: {exc}"[:300]


def _fail_document(deps: Deps, ctx: dict[str, Any], plan: dict[str, Any], reason: str) -> str:
    """The kb-docs record of a document that could not even be attempted (rev-r22 (c))."""
    deps.opensearch.put_doc(KB_DOCS_INDEX, plan["doc_id"], {
        "kb_id": ctx["kb_id"], "ds_id": ctx["ds_id"], "uri": plan["uri"], "name": plan["name"],
        "size_bytes": plan.get("size"), "etag": plan.get("etag"),
        "uploaded_at": plan.get("uploaded_at"), "status": "FAILED",
        "status_reason": reason[:300], "indexed_at": None, "chunk_count": 0,
    })
    return "FAILED"


def _tally(statuses: list[str]) -> tuple[int, int]:
    """(indexed, failed) — UNSUPPORTED counts as failed, as it always did; so does a document
    replaced mid-ingest (`SUPERSEDED`, N17-obs270: this sync did not index it)."""
    counter = Counter(statuses)
    return counter["INDEXED"], counter["FAILED"] + counter["UNSUPPORTED"] + counter["SUPERSEDED"]


def _dispatch_documents(deps: Deps, body: dict[str, Any], ctx: dict[str, Any],
                        plans: list[dict[str, Any]], run: str
                        ) -> tuple[int, list[dict[str, Any]]]:
    """Enqueue one `kb_ingest_doc` message per plan on the Lambda's own queue (N17-obs247),
    stamped with the planner ``run`` (N17-obs266). Returns `(sent, leftovers)` — a plan whose
    send failed is processed inline by the caller (one WARNING naming the exception class), so
    a queue hiccup never loses a document."""
    sent, leftovers = 0, []
    for plan in plans:
        message = {
            "kind": KIND_KB_INGEST_DOC, "job_id": ctx["job_id"], "run": run,
            "workspace_id": body.get("workspace_id"), "kb_id": ctx["kb_id"],
            "ds_id": ctx["ds_id"], "bucket": ctx["bucket"], "prefix": ctx["prefix"],
            "index": ctx["index"], "document": plan,
        }
        if isinstance(body.get("embedding"), dict):
            message["embedding"] = body["embedding"]  # D21: the same row for every document
        try:
            deps.sqs.send_message(QueueUrl=deps.queue_url, MessageBody=json.dumps(message))
            sent += 1
        except Exception as exc:  # noqa: BLE001 — processed inline instead
            logger.warning("kb_ingest job=%s: could not enqueue document %s (%s) — indexing "
                           "it inline", ctx["job_id"], plan["doc_id"], type(exc).__name__)
            leftovers.append(plan)
    return sent, leftovers


def _job_results(deps: Deps, job_id: str, run: str | None) -> list[str]:
    """The statuses of every `kb_ingest_doc_result` row of ``job_id`` written by planner
    ``run`` (N17-obs266: rows of an earlier run of the same job never count)."""
    statuses: list[str] = []
    start = 0
    filters: list[dict[str, Any]] = [{"term": {"kind": KIND_KB_INGEST_DOC_RESULT}},
                                     {"term": {"job_id": job_id}}]
    if run:
        filters.append({"term": {"run": run}})
    while True:
        result = deps.opensearch.search(JOBS_INDEX, {
            "query": {"bool": {"filter": filters}},
            "from": start, "size": KB_DOCS_SWEEP_PAGE,
        })
        hits = result["hits"]["hits"]
        statuses.extend(str(h["_source"].get("status") or "") for h in hits)
        if len(hits) < KB_DOCS_SWEEP_PAGE:
            return statuses
        start += KB_DOCS_SWEEP_PAGE


def _maybe_complete_job(deps: Deps, job_id: str) -> bool:
    """COMPLETE a fanned-out job once every expected document reported (N17-obs247). The
    presented `counts` are always recomputed from the same two inputs — the planner's half
    (`planner_counts`: scanned / unchanged / deleted / inline documents) plus the tallies of the
    result rows — so whoever runs this last (the last document, or the planner after its final
    write) lands on the same totals; a job already COMPLETE with those totals is left alone.
    False while results are still missing."""
    job = deps.opensearch.get_doc(JOBS_INDEX, job_id)
    if not job:
        return False
    fanout = job.get("fanout") or {}
    expected = int(fanout.get("expected") or 0)
    if expected <= 0:
        return False
    statuses = _job_results(deps, job_id, fanout.get("run"))
    if len(statuses) < expected:
        return False
    indexed, failed = _tally(statuses)
    base = dict(job.get("planner_counts") or {})
    counts = {
        **base,
        "numberOfNewDocumentsIndexed": int(base.get("numberOfNewDocumentsIndexed") or 0) + indexed,
        "numberOfDocumentsFailed": int(base.get("numberOfDocumentsFailed") or 0) + failed,
    }
    if job.get("status") == "COMPLETE" and job.get("counts") == counts:
        return True
    if job.get("index"):
        deps.opensearch.request("POST", f"/{job['index']}/_refresh")  # the last doc's deletes
    written = _complete_job_once(deps, job_id, counts,
                                 fanout={**fanout, "reported": len(statuses)})
    logger.log(logging.INFO if written else logging.DEBUG,
               "kb_ingest job=%s COMPLETE after fan-out%s: expected=%d reported=%d indexed=%d "
               "failed=%d", job_id, "" if written else " (already written by another finaliser)",
               expected, len(statuses), indexed, failed)
    return True


# N17-obs279: the fan-out's terminal write, applied server-side only when the job does not
# already say COMPLETE with these counts. Two document invocations that both see the last
# result row both get here; the read-check above cannot settle that race, the script can
# (an `_update` is atomic per document; `retry_on_conflict` re-runs it on a version clash with a
# concurrent `_touch_job`, and the re-run sees the first finaliser's write → `noop`). A COMPLETE
# with OTHER counts is still overwritten: the planner's recompute repairs totals a document
# computed from the preliminary half (the `_maybe_complete_job` docstring).
_COMPLETE_ONCE_SCRIPT = (
    "if (ctx._source.status == params.status && ctx._source.counts == params.counts) "
    "{ ctx.op = 'noop' } else { ctx._source.putAll(params.fields) }"
)


def _complete_job_once(deps: Deps, job_id: str, counts: dict[str, Any], *,
                       fanout: dict[str, Any]) -> bool:
    """N17-obs279: write the job COMPLETE unless it already is with ``counts``. True when this
    call wrote it; False on a `noop` (another finaliser was first) and on any failure — the
    `_job_soft` contract: bookkeeping never raises, one WARNING names the exception type (a gone
    job is not recreated, unlike the read-merge-write it replaces)."""
    fields = {"status": "COMPLETE", "counts": counts, "failure_reason": None,
              "fanout": fanout, "updated_at": deps.now().isoformat()}
    body = {"script": {"lang": "painless", "source": _COMPLETE_ONCE_SCRIPT,
                       "params": {"status": "COMPLETE", "counts": counts, "fields": fields}}}
    for attempt in range(COMPLETE_ONCE_ATTEMPTS):
        try:
            out = deps.opensearch.request(
                "POST", f"/{JOBS_INDEX}/_update/{job_id}", body,
                {"refresh": "true", "retry_on_conflict": str(JOB_TOUCH_RETRY_ON_CONFLICT)},
            ) or {}
        except Exception as exc:  # noqa: BLE001 — bookkeeping is best-effort by contract
            if getattr(exc, "status", None) == 409 and attempt + 1 < COMPLETE_ONCE_ATTEMPTS:
                continue  # a residual version conflict after the server-side retries
            logger.warning("job %s: status write 'COMPLETE' failed (%s) — the row is stale, "
                           "the outcome stands", job_id, type(exc).__name__)
            return False
        return out.get("result") != "noop"
    return False  # unreachable: the last attempt returns or logs


def process_kb_ingest_doc(body: dict[str, Any], deps: Deps) -> dict[str, Any]:
    """ONE document of a fanned-out sync (N17-obs247). Idempotent per (job, document): a
    duplicate delivery finds the result row and does nothing. The result row is written
    with a refresh so the completion check sees it; the job completes here when this was the
    last expected document.

    N17-obs270: a document is processed in slices (`SLICE_CHUNKS` / `SLICE_BUDGET_S`); a slice
    that leaves chunks re-enqueues the continuation (``slice_start``) and returns `CONTINUE` —
    no result row, the job's `updated_at` touched. Only the last slice writes the row. A
    continuation passes the same duplicate / gone-job / stale-run / missing-index gates."""
    started = deps.clock()
    job_id, plan = body["job_id"], dict(body["document"])
    run = str(body.get("run") or "")
    slice_start = max(0, int(body.get("slice_start") or 0))
    ctx = {k: body[k] for k in ("job_id", "kb_id", "ds_id", "bucket", "prefix", "index")}
    ctx["run"] = run  # N17-obs274: part of the chunks' `ingest` stamp
    result_id = f"{job_id}:{run}:{plan['doc_id']}" if run else f"{job_id}:{plan['doc_id']}"
    try:
        if deps.opensearch.get_doc(JOBS_INDEX, result_id):
            return {"job_id": job_id, "doc_id": plan["doc_id"], "status": "DUPLICATE",
                    "skipped": "already processed"}
        job = deps.opensearch.get_doc(JOBS_INDEX, job_id)
    except Exception as exc:  # noqa: BLE001 — N16-obs32: before any work, redeliver
        raise RetryableError(
            f"job bookkeeping unavailable before the document: {type(exc).__name__}"
        ) from exc
    if job is None:
        # the data source / KB was deleted after the planner ran (the hub sweeps its jobs)
        raise DroppedMessage(f"job {job_id} is gone — document {plan['doc_id']} dropped")
    current_run = str((job.get("fanout") or {}).get("run") or "")
    if run and current_run and run != current_run:
        # N17-obs266: a later planner run of this job re-planned (and re-dispatched what was
        # still missing) — this message belongs to the superseded plan: acked, no work, no row
        logger.info("kb_ingest job=%s document %s: planner run %s superseded by %s — stale "
                    "message acked", job_id, plan["doc_id"], run, current_run)
        return {"job_id": job_id, "doc_id": plan["doc_id"], "status": "STALE",
                "skipped": f"superseded by planner run {current_run}"}
    if not deps.opensearch.index_exists(ctx["index"]):
        raise DroppedMessage(
            f"knowledge base {ctx['kb_id']} no longer exists (index {ctx['index']} missing) — "
            "document dropped"
        )
    try:
        embedder = _resolve_or_fail(lambda: embedder_for(body, deps))
    except ModelResolutionError as exc:
        # rev-r22 (c): the document cannot be indexed by anyone — recorded FAILED, the job
        # still completes
        status = _fail_document(deps, ctx, plan, str(exc))
    else:
        # N17-obs270: sliced only when a continuation can be enqueued (else the whole document)
        sliced = deps.sqs is not None and bool(deps.queue_url)
        outcome = _index_slice(deps, embedder, ctx, plan, slice_start=slice_start,
                               started=started if sliced else None)
        status = outcome["status"]
        if status == "CONTINUE":
            return _continue_document(deps, body, plan, outcome)
    now_iso = deps.now().isoformat()
    try:
        deps.opensearch.put_doc(JOBS_INDEX, result_id, {
            "job_id": job_id, "run": run or None, "kind": KIND_KB_INGEST_DOC_RESULT,
            "kb_id": ctx["kb_id"], "ds_id": ctx["ds_id"],
            "workspace_id": body.get("workspace_id"),
            "doc_id": plan["doc_id"], "status": status,
            "created_at": now_iso, "updated_at": now_iso,
        }, refresh=True)
        # rev-r22 (b): progress the hub's stalled-job rule can see (a partial update — atomic
        # server-side, never a read-merge-write of the planner's fields); N17-obs273: concurrent
        # documents conflict on it — retried server-side, a residual conflict is not an error
        _touch_job(deps, job_id, now_iso)
        _maybe_complete_job(deps, job_id)
    except Exception as exc:  # noqa: BLE001 — the document is written; the row must follow
        raise RetryableError(
            f"result bookkeeping unavailable after the document: {type(exc).__name__}"
        ) from exc
    return {"job_id": job_id, "doc_id": plan["doc_id"], "status": status}


def _touch_job(deps: Deps, job_id: str, now_iso: str) -> bool:
    """N17-obs273: bump the job's `updated_at` (progress for the hub's stalled rule) —
    `_update` with `retry_on_conflict`, never raising. False when the bump did not land (a
    residual 409 after the server-side retries, the job gone, any other error: one log line)."""
    try:
        deps.opensearch.request(
            "POST", f"/{JOBS_INDEX}/_update/{job_id}", {"doc": {"updated_at": now_iso}},
            {"refresh": "false", "retry_on_conflict": str(JOB_TOUCH_RETRY_ON_CONFLICT)},
        )
    except Exception as exc:  # noqa: BLE001 — best-effort liveness, the result row is the truth
        status = getattr(exc, "status", None)
        level = logging.INFO if status in (404, 409) else logging.WARNING
        logger.log(level, "kb_ingest job=%s: updated_at bump skipped (%s%s) — the result row "
                   "stands", job_id, type(exc).__name__, f" {status}" if status else "")
        return False
    return True


def _continue_document(deps: Deps, body: dict[str, Any], plan: dict[str, Any],
                       outcome: dict[str, Any]) -> dict[str, Any]:
    """N17-obs270: enqueue the next slice of this document — the same message with the
    ``slice_start`` to resume at and the ETag of the text slice 0 read (a later slice that
    reads another ETag stops) — and touch the job's `updated_at` (progress for the hub's
    stalled rule). A failed send is a redelivery of THIS slice (its writes are idempotent)."""
    job_id, next_start = body["job_id"], int(outcome["next_start"])
    message = {**body, "document": {**plan, "etag": outcome.get("etag")},
               "slice_start": next_start}
    try:
        deps.sqs.send_message(QueueUrl=deps.queue_url, MessageBody=json.dumps(message))
    except Exception as exc:  # noqa: BLE001 — the slice is written; the continuation must follow
        raise RetryableError(
            f"continuation of document {plan['doc_id']} at chunk {next_start} could not be "
            f"enqueued: {type(exc).__name__}"
        ) from exc
    _touch_job(deps, job_id, deps.now().isoformat())  # N17-obs273: tolerant, never a retry
    logger.info("kb_ingest job=%s document %s: slice %d-%d written, continuing", job_id,
                plan["doc_id"], int(body.get("slice_start") or 0) + 1, next_start)
    return {"job_id": job_id, "doc_id": plan["doc_id"], "status": "CONTINUE",
            "next_start": next_start}


def process_kb_ingest(body: dict[str, Any], deps: Deps) -> dict[str, Any]:
    """The sync message = the planner (N17-obs247): list, decide (obs246), sweep, then either
    fan the changed documents out as `kb_ingest_doc` messages (job `IN_PROGRESS` with
    `fanout.expected`, COMPLETE when the last one reports) or — without a queue — index them
    inline and COMPLETE here."""
    job_id = body["job_id"]
    bucket = body["bucket"]
    prefix = body["prefix"]
    index = body["index"]
    kb_id = body["kb_id"]
    ds_id = body["ds_id"]
    run = uuid.uuid4().hex[:12]  # N17-obs266: this planner run's generation
    ctx = {"job_id": job_id, "kb_id": kb_id, "ds_id": ds_id, "bucket": bucket,
           "prefix": prefix, "index": index, "run": run}

    try:
        existing = deps.opensearch.get_doc(JOBS_INDEX, job_id)
        if existing and existing.get("status") == "COMPLETE":
            # N17-obs266: a redelivered planner of a finished job re-plans nothing
            return {"job_id": job_id, "status": "COMPLETE", "skipped": "already completed"}
        _job(
            deps, job_id, status="IN_PROGRESS", kind="kb_ingest", kb_id=kb_id, ds_id=ds_id,
            workspace_id=body.get("workspace_id"), index=index,
        )
    except Exception as exc:  # noqa: BLE001 — N16-obs32: before any work, redeliver
        raise RetryableError(
            f"job bookkeeping unavailable before ingest: {type(exc).__name__}"
        ) from exc
    try:
        # rev-r22 (c): resolved INSIDE the bookkeeping — a failure marks the job FAILED
        embedder = _resolve_or_fail(lambda: embedder_for(body, deps))  # D21
        if not deps.opensearch.index_exists(index):
            # the hub drops the per-KB index when the KB is deleted (knowledge_selfhosted
            # .delete_kb); an ingest enqueued before that has nowhere to write
            reason = (
                f"knowledge base {kb_id} no longer exists (index {index} missing) — "
                "message dropped"
            )
            _job_soft(deps, job_id, status="FAILED", failure_reason=reason)
            raise DroppedMessage(reason)
        objects: list[dict[str, Any]] = []
        listed_at = deps.now()  # N17-obs255: what "written after this listing" means
        try:
            for page in deps.s3.get_paginator("list_objects_v2").paginate(
                Bucket=bucket, Prefix=prefix
            ):
                objects.extend(page.get("Contents", []))
        except ClientError as exc:
            if not _is_missing_s3(exc):
                raise
            reason = (
                f"data source {ds_id} of knowledge base {kb_id}: bucket {bucket} is gone — "
                "message dropped"
            )
            _job_soft(deps, job_id, status="FAILED", failure_reason=reason)
            raise DroppedMessage(reason) from exc
        originals = [obj for obj in objects if SIDECAR_DIR not in obj["Key"][len(prefix):]]
        full = bool(body.get("full"))  # N17-obs246: force the full rewrite

        plans = [_plan_document(deps, bucket, prefix, kb_id, ds_id, obj) for obj in originals]
        changed: list[dict[str, Any]] = []
        unchanged = 0
        for plan in plans:
            # N17-obs246: unchanged since its last INDEXED record → nothing to do
            if not full and _unchanged(deps.opensearch.get_doc(KB_DOCS_INDEX, plan["doc_id"]),
                                       plan["etag"], plan["size"]):
                unchanged += 1
            else:
                changed.append(plan)
        # N17-obs246: documents deleted from the prefix leave the index with this sync
        deleted = _sweep_deleted_documents(deps, index, kb_id, ds_id,
                                           {plan["uri"] for plan in plans},
                                           bucket=bucket, listed_at=listed_at)

        # N17-obs247: fan out when the queue is reachable, else inline (older stack, tests)
        scanned = len(originals)

        def planner_counts(indexed: int, failed: int) -> dict[str, Any]:
            counts: dict[str, Any] = {
                "numberOfDocumentsScanned": scanned,       # every original object listed
                "numberOfNewDocumentsIndexed": indexed,    # rewritten this sync (inline)
                "numberOfDocumentsFailed": failed,
                "numberOfDocumentsUnchanged": unchanged,   # N17-obs246: same ETag + size
            }
            if deleted:
                counts["numberOfDocumentsDeleted"] = deleted
            if not originals:
                # N17-obs204: the prefix held nothing (or only `_text/` sidecars) when this
                # job listed it — said explicitly, so the console can tell "nothing to ingest"
                # from "three zeros"; a sync enqueued before an upload landed looks like this.
                counts["prefix_empty"] = True
            return counts

        # N17-obs247: fan out when the queue is reachable, else inline (older stack, tests)
        sent = 0
        inline = changed
        if changed and deps.sqs is not None and deps.queue_url:
            # the expectation and the planner's half are written BEFORE the first message
            # leaves, so an early result can never complete the job against a missing count
            # N17-obs266: the run stamp makes rows / messages of an earlier planner run of
            # this job (a redelivery, a raise after the fan-out) invisible to this plan
            _job(deps, job_id, fanout={"expected": len(changed), "run": run},
                 planner_counts=planner_counts(0, 0))
            sent, inline = _dispatch_documents(deps, body, ctx, changed, run)
        statuses = [_index_document(deps, embedder, ctx, plan) for plan in inline]
        indexed, failed = _tally(statuses)

        # a refresh before the status write (covers the chunk deletes of swept documents and
        # of inline documents that then failed; the fanned-out documents refresh themselves)
        deps.opensearch.request("POST", f"/{index}/_refresh")
        counts = planner_counts(indexed, failed)
        # one forensics line per job (N17-obs204): what the listing saw and what came of it —
        # bucket/prefix and counts only, never a document name or body
        logger.info(
            "kb_ingest job=%s kb=%s ds=%s prefix=s3://%s/%s objects=%d originals=%d "
            "scanned=%d indexed=%d failed=%d unchanged=%d deleted=%d dispatched=%d full=%s "
            "prefix_empty=%s",
            job_id, kb_id, ds_id, bucket, prefix, len(objects), len(originals),
            scanned, indexed, failed, unchanged, deleted, sent, str(full).lower(),
            str(not originals).lower(),
        )
        if sent:
            # the planner's half (final: the inline documents counted), `fanout.expected`
            # corrected to what was sent; `counts` shows that half while documents run. The
            # last result row completes the job — or this recompute does, when every document
            # already reported (it also repairs totals a document computed from the
            # preliminary half).
            _job(deps, job_id, counts=counts, planner_counts=counts,
                 fanout={"expected": sent, "run": run})
            _maybe_complete_job(deps, job_id)
            return {"job_id": job_id, "status": "IN_PROGRESS", "counts": counts,
                    "dispatched": sent}
        _job_soft(deps, job_id, status="COMPLETE", counts=counts, failure_reason=None)
        return {"job_id": job_id, "status": "COMPLETE", "counts": counts}
    except DroppedMessage:
        raise  # already recorded on the job; never retried
    except ModelResolutionError as exc:
        _job_soft(deps, job_id, status="FAILED", failure_reason=str(exc),
                  failure_code="model_resolution")
        raise DroppedMessage(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - last-resort job bookkeeping before retry
        if getattr(exc, "status", None) == 404:
            # the KB index vanished mid-ingest (OpenSearchError 404 on bulk/_refresh)
            reason = f"knowledge base {kb_id} index {index} disappeared during ingest — dropped"
            _job_soft(deps, job_id, status="FAILED", failure_reason=reason)
            raise DroppedMessage(reason) from exc
        _job_soft(deps, job_id, status="FAILED", failure_reason=f"{type(exc).__name__}: {exc}")
        raise RetryableError(str(exc)) from exc


# ----------------------------------------------------------------------------
# Entry points
# ----------------------------------------------------------------------------


def process_message(body: dict[str, Any], deps: Deps) -> dict[str, Any]:
    kind = body.get("kind")
    if kind == "memory_extract":
        return process_memory_extract(body, deps)
    if kind == "kb_ingest":
        return process_kb_ingest(body, deps)
    if kind == KIND_KB_INGEST_DOC:
        return process_kb_ingest_doc(body, deps)  # N17-obs247: one document of a sync
    raise ValueError(f"unknown message kind {kind!r}")  # poison -> dropped, not retried


def _log_outcome(level: int, record: dict[str, Any], body: dict[str, Any] | None,
                 outcome: str, started: float, detail: str = "") -> None:
    logger.log(
        level,
        "message=%s kind=%s job=%s outcome=%s duration_ms=%d%s",
        record.get("messageId"),
        (body or {}).get("kind"),
        message_job_id(body or {}),
        outcome,
        int((time.monotonic() - started) * 1000),
        f" detail={detail}" if detail else "",
    )


def _handle_event(event: dict[str, Any], context: Any) -> dict[str, Any]:
    deps = default_deps()
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        started = time.monotonic()
        try:
            body = json.loads(record["body"])
            if not isinstance(body, dict):
                raise ValueError("message body is not a JSON object")
        except ValueError as exc:
            _log_outcome(logging.ERROR, record, None, "poison", started, f"unparseable: {exc}")
            continue
        _diagnostic_logger.info(
            "message_start message=%s kind=%s job=%s request_id=%s",
            record.get("messageId"), body.get("kind"), message_job_id(body),
            getattr(context, "aws_request_id", None),
        )
        try:
            result = process_message(body, deps)
        except DroppedMessage as exc:
            # ack-and-drop: the KB / data source / agent is gone (N11-r3-bug4)
            _log_outcome(logging.WARNING, record, body, "dropped", started, str(exc))
        except ModelValidationFailure as exc:
            # N17-obs535: FAILED for the hub, redelivered towards the DLQ — one structured
            # WARNING line per receive names the model and the provider's text
            failures.append({"itemIdentifier": record["messageId"]})
            _log_outcome(logging.WARNING, record, body, "failed_permanent", started,
                         f"reason=model_validation model={exc.model} error={exc}")
        except RetryableError as exc:
            failures.append({"itemIdentifier": record["messageId"]})
            _log_outcome(logging.WARNING, record, body, "retry", started, str(exc))
        except Exception as exc:  # noqa: BLE001 - poison, unless the transport family (obs32)
            if _is_transient(exc):
                # belt for the processors' own classification: an overload / transport
                # failure that escaped raw is still a redelivery, never an acked drop
                failures.append({"itemIdentifier": record["messageId"]})
                _log_outcome(logging.WARNING, record, body, "retry", started,
                             f"transient {type(exc).__name__}")
                continue
            _log_outcome(logging.ERROR, record, body, "poison", started,
                         f"{type(exc).__name__}: {exc}")
            logger.debug("poison message traceback:\n%s", traceback.format_exc(limit=3))
        else:
            status = (result or {}).get("status", "")
            _log_outcome(logging.INFO, record, body, "ok", started,
                         f"status={status}" if status else "")
    return {"batchItemFailures": failures}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    token = _remaining_seconds.set((lambda: remaining() / 1000) if callable(remaining) else None)
    try:
        return _handle_event(event, context)
    finally:
        _remaining_seconds.reset(token)
