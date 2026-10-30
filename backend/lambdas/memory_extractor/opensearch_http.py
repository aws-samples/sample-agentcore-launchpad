"""Shared OpenSearch/Titan core for the self-hosted Memory + Knowledge Base substrate
(N6). AgentCore Memory and Bedrock Knowledge Bases do not exist in `cn-northwest-1`, so
both features are self-built on an Amazon OpenSearch Service domain (k-NN, Titan v2
embeddings) reached over a hand-signed SigV4 REST client — there is no `opensearch-py`
in this project's dependency tree.

This module is the **single source of truth** (ruling R15) and is mirrored
byte-for-byte, between the `# <launchpad-opensearch-core>` / `# </launchpad-opensearch-core>`
markers below, into two other places that cannot import `app.*`:

  - `backend/lambdas/memory_extractor/opensearch_http.py` — the extraction/ingestion
    Lambda bundle (plain `Code.from_asset`, no package install step).
  - the fenced block inside `backend/app/templates/memory_runtime.py.tmpl` — inlined
    into every generated agent's `main.py` via `__LAUNCHPAD_MEMORY_SOURCE__`.

A mirror test (`tests/test_memory_core_mirror.py`, added in a later task) pins all
three copies to be identical. Because of that, this module must stay a pure consumer of
an injected credentials-provider callable and (for `TitanEmbedder`) an injected
bedrock-runtime client — it never imports `app.*` and never constructs a boto3 client
itself, so it stays invisible to the `app/services/aws_clients.py` construction funnel
guard (`tests/test_client_funnel.py`) and importable with no package installed beyond
botocore/urllib3 (both already present as `boto3`'s own dependencies).
"""

# <launchpad-opensearch-core>
import hashlib
import json
import math
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
EMBED_DIMENSIONS = 1024
EMBED_MAX_CHARS = 8000
DEDUPE_COSINE = 0.92
# Merge strategy v2 (r14-a, N14-obs1). Below `DEDUPE_COSINE` and at or above this bound the
# extractor lets the model decide between add / update / supersede; below it a new fact is
# always an `add` unless the model names a candidate it saw.
MERGE_GRAY_ZONE_LOW = 0.75
# Recall ranking: similarity × 0.5 ** (age_days / half_life) × max(confidence, floor).
RECALL_HALF_LIFE_DAYS = 90
RECALL_CONFIDENCE_FLOOR = 0.5
RECALL_CANDIDATES_PER_K = 4
RECALL_MAX_CANDIDATES = 20
# Per-store merge settings (ledger `memory_stores.merge_settings`, SQS `merge` block, agent
# env `LAUNCHPAD_MEMORY_HALF_LIFE_DAYS` / `LAUNCHPAD_MEMORY_TTL_DAYS`). `None` TTL = keep.
MERGE_SETTINGS_DEFAULTS: dict[str, Any] = {
    "recall_half_life_days": RECALL_HALF_LIFE_DAYS,
    "max_facts_per_actor": 200,
    "max_preferences_per_actor": 100,
    "long_term_ttl_days": None,
    "actor_summary_interval": 1,
}
MERGE_SETTINGS_BOUNDS: dict[str, tuple[int, int]] = {
    "recall_half_life_days": (1, 3650),
    "max_facts_per_actor": (10, 10000),
    "max_preferences_per_actor": (10, 10000),
    "long_term_ttl_days": (7, 3650),
    "actor_summary_interval": (1, 100),
}
# Consolidation pass: at most this many records are merged per extraction event.
CONSOLIDATE_MAX_RECORDS = 20
# Actor-level rolling summary: `/summaries/{actorId}/_actor`, rewritten from the most
# recent session summaries, at most this many words.
ACTOR_SUMMARY_SEGMENT = "_actor"
ACTOR_SUMMARY_MAX_WORDS = 150
ACTOR_SUMMARY_SOURCES = 10
# Record ops the extractor / consolidation pass write into `last_op`.
RECORD_OPS = ("add", "update", "supersede", "delete", "noop")
# Additive v2 record fields. Every pre-v2 document lacks them and is read as ACTIVE with
# confidence 1.0 and `created_at` as its timestamp; `ensure_mapping` (hub bootstrap) PUTs
# the missing properties onto a legacy index, never a reindex.
MEMORY_RECORD_V2_PROPERTIES: dict[str, dict[str, Any]] = {
    "updated_at": {"type": "date"},
    "confidence": {"type": "float"},
    "superseded_by": {"type": "keyword"},
    "superseded_at": {"type": "date"},
    "supersedes": {"type": "keyword"},
    "deleted_at": {"type": "date"},
    "expired_at": {"type": "date"},
    "last_op": {"type": "keyword"},
}

# D17: one OpenSearch domain is shared by every EnvName environment of an account+region,
# so every index carries the environment: `launchpad-<base>` in the default environment,
# `launchpad-<env>-<base>` otherwise. The expression in `index_name` is the one in
# `app/core/naming.py::index_name` (the hub reads the env from Settings); the runtime
# library and the extractor Lambda read it from INDEX_ENV_VAR, set by their deployer.
INDEX_ENV_VAR = "LAUNCHPAD_ENV_NAME"

STRATEGIES: tuple[dict[str, str], ...] = (
    {"strategy_id": "semantic", "name": "Semantic facts", "type": "SEMANTIC",
     "template": "/facts/{actorId}"},
    {"strategy_id": "user_preference", "name": "User preferences", "type": "USER_PREFERENCE",
     "template": "/preferences/{actorId}"},
    {"strategy_id": "summarization", "name": "Session summaries", "type": "SUMMARIZATION",
     "template": "/summaries/{actorId}/{sessionId}"},
)
# Every catalogued strategy is extracted (the never-extracted `episodic` entry was
# removed in R13, N13-obs4); stores that still list it load with it ignored.
EXTRACTED_STRATEGIES = ("semantic", "user_preference", "summarization")
NAMESPACE_BY_STRATEGY = {s["strategy_id"]: s["template"] for s in STRATEGIES}


def index_name(base: str, env_name: str) -> str:
    return f"launchpad-{base}" if not env_name else f"launchpad-{env_name}-{base}"


def memory_index(env_name: str) -> str:
    return index_name("memory", env_name)


def jobs_index(env_name: str) -> str:
    return index_name("jobs", env_name)


def kb_docs_index(env_name: str) -> str:
    return index_name("kb-docs", env_name)


def kb_index_name(kb_id: str, env_name: str) -> str:
    return index_name(f"kb-{kb_id.lower()}", env_name)


def _knn_field() -> dict[str, Any]:
    return {
        "type": "knn_vector",
        "dimension": EMBED_DIMENSIONS,
        "method": {"name": "hnsw", "space_type": "cosinesimil", "engine": "lucene"},
    }


def _settings() -> dict[str, Any]:
    return {"index.knn": True, "number_of_shards": 1, "number_of_replicas": 0}


def memory_index_body() -> dict[str, Any]:
    """Long-term records: one row per fact/preference/summary a store holds."""
    keyword_fields = ("store_id", "workspace_id", "actor_id", "namespace", "strategy",
                       "session_id", "source_event_ids")
    return {
        "settings": _settings(),
        "mappings": {
            "properties": {
                **{f: {"type": "keyword"} for f in keyword_fields},
                "text": {"type": "text"},
                "embedding": _knn_field(),
                "created_at": {"type": "date"},
                "metadata": {"type": "object", "enabled": False},
                **MEMORY_RECORD_V2_PROPERTIES,
            }
        },
    }


def normalize_merge_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    """The per-store merge settings with every default filled in; unknown keys dropped,
    values outside `MERGE_SETTINGS_BOUNDS` clamped (the API validates and 422s before a
    row is written — this is the read-side safety net for a hand-edited row or an
    in-flight SQS message)."""
    out = dict(MERGE_SETTINGS_DEFAULTS)
    for key, value in (raw or {}).items():
        if key not in MERGE_SETTINGS_DEFAULTS:
            continue
        if value is None:
            out[key] = None if key == "long_term_ttl_days" else MERGE_SETTINGS_DEFAULTS[key]
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        low, high = MERGE_SETTINGS_BOUNDS[key]
        out[key] = min(max(number, low), high)
    return out


def active_record_filter() -> dict[str, Any]:
    """One filter clause that keeps only ACTIVE long-term records: nothing superseded,
    soft-deleted or expired. Legacy documents carry none of the three fields and match."""
    return {"bool": {"must_not": [
        {"exists": {"field": "superseded_by"}},
        {"exists": {"field": "deleted_at"}},
        {"exists": {"field": "expired_at"}},
    ]}}


def ttl_cutoff_filter(now: datetime, ttl_days: int | None) -> dict[str, Any] | None:
    """Records older than the store's TTL (by `updated_at`, else `created_at`) are hidden on
    read even before the consolidation pass marks them `expired_at`; `None` TTL = no clause."""
    if not ttl_days:
        return None
    cutoff = (now.astimezone(UTC) - timedelta(days=int(ttl_days))).isoformat()
    return {"bool": {"should": [
        {"range": {"updated_at": {"gte": cutoff}}},
        {"bool": {"must_not": [{"exists": {"field": "updated_at"}}],
                  "filter": [{"range": {"created_at": {"gte": cutoff}}}]}},
    ], "minimum_should_match": 1}}


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def record_timestamp(doc: dict[str, Any]) -> datetime:
    """`updated_at`, else `created_at`, else the epoch (a legacy document without either
    decays to the floor rather than being dropped)."""
    return (
        parse_timestamp(doc.get("updated_at"))
        or parse_timestamp(doc.get("created_at"))
        or datetime(1970, 1, 1, tzinfo=UTC)
    )


def record_is_active(doc: dict[str, Any]) -> bool:
    return not (doc.get("superseded_by") or doc.get("deleted_at") or doc.get("expired_at"))


def record_confidence(doc: dict[str, Any]) -> float:
    value = doc.get("confidence")
    if value is None:
        return 1.0  # pre-v2 record
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        return 1.0


def recency_weight(doc: dict[str, Any], now: datetime, half_life_days: float) -> float:
    age_days = max((now.astimezone(UTC) - record_timestamp(doc)).total_seconds(), 0.0) / 86400
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (age_days / float(half_life_days))


def rank_records(
    hits: list[dict[str, Any]], vector: list[float], now: datetime, top_k: int,
    half_life_days: float = RECALL_HALF_LIFE_DAYS,
    confidence_floor: float = RECALL_CONFIDENCE_FLOOR,
) -> list[dict[str, Any]]:
    """Recall ranking (v2): `similarity × recency × max(confidence, floor)` over k-NN hits
    that still carry their `embedding`; inactive hits are dropped, the embedding is
    stripped, and each kept hit gains `similarity` and `recall_score`. Deterministic for
    equal scores (stable sort on the input order)."""
    scored: list[tuple[float, dict[str, Any]]] = []
    for hit in hits:
        if not record_is_active(hit):
            continue
        embedding = hit.get("embedding")
        similarity = cosine(vector, embedding) if embedding else float(hit.get("_score") or 0.0)
        weight = recency_weight(hit, now, half_life_days) * max(
            record_confidence(hit), confidence_floor
        )
        out = {k: v for k, v in hit.items() if k != "embedding"}
        out["similarity"] = similarity
        out["recall_score"] = similarity * weight
        scored.append((out["recall_score"], out))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [doc for _, doc in scored[:top_k]]


def actor_summary_namespace(actor_id: str) -> str:
    return f"/summaries/{actor_id}/{ACTOR_SUMMARY_SEGMENT}"


def summary_record_id(store_id: str, namespace: str) -> str:
    """Deterministic id of a summary document (one per namespace): the same rule the
    extractor used for session summaries since N6, now shared with the actor summary."""
    return hashlib.sha1(f"{store_id}|{namespace}".encode()).hexdigest()[:32]


def jobs_index_body() -> dict[str, Any]:
    """Extraction + ingestion job status (read by the hub, written by the Lambda)."""
    keyword_fields = ("job_id", "kind", "store_id", "kb_id", "ds_id", "workspace_id",
                       "actor_id", "session_id", "strategy_ids", "status")
    return {
        "settings": _settings(),
        "mappings": {
            "properties": {
                **{f: {"type": "keyword"} for f in keyword_fields},
                "failure_reason": {"type": "text"},
                "warnings": {"type": "text"},
                "counts": {"type": "object"},
                "created_at": {"type": "date"},
                "updated_at": {"type": "date"},
            }
        },
    }


def kb_docs_index_body() -> dict[str, Any]:
    """One row per source document (status, not chunk content)."""
    keyword_fields = ("kb_id", "ds_id", "uri", "name", "status")
    return {
        "settings": _settings(),
        "mappings": {
            "properties": {
                **{f: {"type": "keyword"} for f in keyword_fields},
                "status_reason": {"type": "text"},
                "size_bytes": {"type": "long"},
                "chunk_count": {"type": "integer"},
                "uploaded_at": {"type": "date"},
                "indexed_at": {"type": "date"},
            }
        },
    }


def kb_index_body() -> dict[str, Any]:
    """One index per knowledge base: one row per indexed chunk."""
    keyword_fields = ("kb_id", "ds_id", "doc_uri", "doc_name")
    return {
        "settings": _settings(),
        "mappings": {
            "properties": {
                **{f: {"type": "keyword"} for f in keyword_fields},
                "chunk_index": {"type": "integer"},
                "text": {"type": "text"},
                "embedding": _knn_field(),
                "metadata": {"type": "object", "enabled": False},
                "indexed_at": {"type": "date"},
            }
        },
    }


def cosine(a: list[float], b: list[float]) -> float:
    """Dot product of normalized vectors — used for dedupe/recall scoring in Python
    rather than trusting `_score`, whose scale differs by k-NN engine (ruling R3)."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")


def _split_long_paragraph(paragraph: str, size: int) -> list[str]:
    sentences = _SENTENCE_SPLIT.split(paragraph)
    pieces: list[str] = []
    for sentence in sentences:
        if len(sentence) <= size:
            pieces.append(sentence)
            continue
        # A single sentence longer than `size`: hard-wrap it so no piece this
        # function returns exceeds `size` on its own.
        for start in range(0, len(sentence), size):
            pieces.append(sentence[start:start + size])
    return [p for p in pieces if p.strip()]


def chunk_text(text: str, size: int = 1000, overlap: int = 200) -> list[str]:
    """Split on paragraphs, then sentences for paragraphs longer than `size`, then
    greedily pack pieces into windows of at most `size` characters; each window after
    the first starts with the last `overlap` characters of the previous one so the
    embedder sees local context across a chunk boundary."""
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return []
    if len(normalized) <= size:
        return [normalized]

    pieces: list[str] = []
    for paragraph in normalized.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= size:
            pieces.append(paragraph)
        else:
            pieces.extend(_split_long_paragraph(paragraph, size))

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current} {piece}".strip() if current else piece
        if len(candidate) <= size:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(piece) <= size:
            current = piece
        else:
            # A piece this long only happens if `_split_long_paragraph` could not
            # bring it under `size` (one sentence longer than `size` already hard-
            # wrapped above) — fall back to a hard slice so no chunk ever exceeds it.
            for start in range(0, len(piece), size):
                chunks.append(piece[start:start + size])
            current = ""
    if current:
        chunks.append(current)

    if overlap <= 0 or len(chunks) <= 1:
        return [c for c in chunks if c]

    out = [chunks[0]]
    for chunk in chunks[1:]:
        tail = out[-1][-overlap:].lstrip()
        merged = f"{tail} {chunk}".strip() if tail else chunk
        out.append(merged[:size] if len(merged) > size else merged)
    return [c for c in out if c]


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def resolve_namespace(
    template: str, actor_id: str, session_id: str | None = None
) -> tuple[str, bool, bool]:
    """Substitute `{actorId}` (and `{sessionId}` when given) into a namespace
    template. An unresolved placeholder that trails every resolved/literal segment
    (no `session_id` supplied, nothing after it) is dropped and reported as a
    prefix match; an unresolved placeholder with a resolved segment AFTER it stays
    in place (braces kept) and the whole namespace is reported unresolvable."""
    values = {"actorId": actor_id}
    if session_id is not None:
        values["sessionId"] = session_id

    segments = template.split("/")
    resolved_segments: list[str] = []
    unresolved_indices: list[int] = []
    for segment in segments:
        match = _PLACEHOLDER.fullmatch(segment)
        if match is None:
            resolved_segments.append(segment)
            continue
        key = match.group(1)
        if key in values:
            resolved_segments.append(values[key])
        else:
            resolved_segments.append(segment)  # keep the "{key}" marker
            unresolved_indices.append(len(resolved_segments) - 1)

    if not unresolved_indices:
        return "/".join(resolved_segments), True, False

    last_resolved_idx = max(
        (i for i in range(len(resolved_segments)) if i not in unresolved_indices), default=-1
    )
    if all(i > last_resolved_idx for i in unresolved_indices):
        # Every unresolved placeholder trails the last resolved/literal segment —
        # drop them and report a prefix match.
        namespace = "/".join(resolved_segments[: last_resolved_idx + 1])
        if not namespace.endswith("/"):
            namespace += "/"
        return namespace, True, True

    return "/".join(resolved_segments), False, False


def event_key(ts: datetime, uid: str) -> str:
    """Lexically sortable event filename: UTC timestamp (microsecond) + a short
    unique suffix, so S3 listing order is chronological with no extra metadata."""
    return f"{ts.astimezone(UTC).strftime('%Y%m%dT%H%M%S%f')}Z_{uid}.json"


_EVENT_KEY_TS_RE = re.compile(r"^(\d{8}T\d{6}\d{6})Z_")


def parse_event_key(key: str) -> datetime | None:
    match = _EVENT_KEY_TS_RE.match(key)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%f").replace(tzinfo=UTC)
    except ValueError:
        return None


def turn_event(
    actor_id: str, session_id: str, prompt: str, answer: str, ts: datetime | None = None
) -> dict[str, Any]:
    """One AgentCore-shaped event body for a single USER/ASSISTANT turn — the shape
    `observability.session_transcript` already knows how to read."""
    ts = ts or datetime.now(UTC)
    return {
        "eventId": uuid.uuid4().hex,
        "eventTimestamp": ts.isoformat(),
        "actorId": actor_id,
        "sessionId": session_id,
        "payload": [
            {"conversational": {"role": "USER", "content": {"text": prompt}}},
            {"conversational": {"role": "ASSISTANT", "content": {"text": answer}}},
        ],
    }


class OpenSearchError(RuntimeError):
    """Raised for any non-2xx OpenSearch response; `.status` is the HTTP status."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"OpenSearch HTTP {status}: {body[:500]}")
        self.status = status


class OpenSearchHttp:
    """A SigV4-signed REST client for the OpenSearch Service data plane (service
    name `es`) over `urllib3`, with no `opensearch-py` dependency. `credentials_provider`
    is a zero-argument callable returning frozen botocore credentials — never a session
    — so this class never constructs or caches a boto3 client itself."""

    def __init__(
        self,
        endpoint: str,
        region: str,
        credentials_provider: Callable[[], Any],
        http: Any = None,
        service: str = "es",
    ) -> None:
        self.endpoint = endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        self.region = region
        self.service = service
        self.credentials_provider = credentials_provider
        self.http = http or urllib3.PoolManager(
            timeout=urllib3.Timeout(connect=5.0, read=30.0), retries=False
        )

    def request(
        self, method: str, path: str, body: Any = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"https://{self.endpoint}{path}"
        if params:
            url += "?" + urlencode(params)
        data: str | None
        if body is None:
            data = None
        elif isinstance(body, str):
            data = body
        else:
            data = json.dumps(body)

        req = AWSRequest(
            method=method, url=url, data=data, headers={"Content-Type": "application/json"}
        )
        SigV4Auth(self.credentials_provider(), self.service, self.region).add_auth(req)
        resp = self.http.request(method, url, body=data, headers=dict(req.headers))

        if method == "HEAD":
            if resp.status == 404:
                return {"found": False}
            if resp.status >= 300:
                raise OpenSearchError(resp.status, "")
            return {"found": True}

        if resp.status >= 300:
            raise OpenSearchError(resp.status, (resp.data or b"").decode(errors="replace"))
        return json.loads(resp.data or b"{}")

    def index_exists(self, name: str) -> bool:
        return bool(self.request("HEAD", f"/{name}")["found"])

    def ensure_index(self, name: str, body: dict[str, Any]) -> bool:
        if self.index_exists(name):
            return False
        self.request("PUT", f"/{name}", body)
        return True

    def delete_index(self, name: str) -> None:
        try:
            self.request("DELETE", f"/{name}")
        except OpenSearchError as exc:
            if exc.status != 404:
                raise

    def put_doc(self, index: str, doc_id: str, doc: dict[str, Any], refresh: bool = True) -> None:
        self.request(
            "PUT", f"/{index}/_doc/{doc_id}", doc, {"refresh": "true" if refresh else "false"}
        )

    def get_doc(self, index: str, doc_id: str) -> dict[str, Any] | None:
        try:
            return dict(self.request("GET", f"/{index}/_doc/{doc_id}")["_source"])
        except OpenSearchError as exc:
            if exc.status == 404:
                return None
            raise

    def update_doc(
        self, index: str, doc_id: str, fields: dict[str, Any], refresh: bool = True
    ) -> bool:
        """Partial update (`_update` with a `doc` body) — the merge strategy's way of marking
        a record superseded / deleted / expired without re-sending its embedding. ``False``
        when the document is gone (404)."""
        try:
            self.request(
                "POST", f"/{index}/_update/{doc_id}", {"doc": fields},
                {"refresh": "true" if refresh else "false"},
            )
        except OpenSearchError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def delete_doc(self, index: str, doc_id: str, refresh: bool = True) -> bool:
        """Delete one document; ``False`` when it was already gone (404)."""
        try:
            self.request(
                "DELETE", f"/{index}/_doc/{doc_id}", None,
                {"refresh": "true" if refresh else "false"},
            )
        except OpenSearchError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def bulk(self, index: str, docs: list[tuple[str, dict[str, Any]]], refresh: bool = True) -> int:
        lines: list[str] = []
        for doc_id, doc in docs:
            lines.append(json.dumps({"index": {"_index": index, "_id": doc_id}}))
            lines.append(json.dumps(doc))
        body = "\n".join(lines) + "\n" if lines else ""
        out = self.request("POST", "/_bulk", body, {"refresh": "true" if refresh else "false"})
        if out.get("errors"):
            raise OpenSearchError(400, json.dumps(out))
        return len(docs)

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/{index}/_search", body)

    def knn_search(
        self,
        index: str,
        vector: list[float],
        k: int,
        filters: list[dict[str, Any]],
        include_embedding: bool = False,
    ) -> list[dict[str, Any]]:
        body = {
            "size": k,
            "_source": {"excludes": [] if include_embedding else ["embedding"]},
            "query": {
                "knn": {
                    "embedding": {
                        "vector": vector,
                        "k": k,
                        "filter": {"bool": {"filter": filters}},
                    }
                }
            },
        }
        hits = self.search(index, body)["hits"]["hits"]
        return [{"_id": h["_id"], "_score": h.get("_score"), **h["_source"]} for h in hits]

    def delete_by_query(self, index: str, query: dict[str, Any]) -> int:
        out = self.request(
            "POST", f"/{index}/_delete_by_query", {"query": query}, {"refresh": "true"}
        )
        return int(out.get("deleted", 0))


class TitanEmbedder:
    """Amazon Titan Text Embeddings V2 (`amazon.titan-embed-text-v2:0`, 1024 dims,
    normalized) over an injected `bedrock-runtime` client — never constructed here."""

    def __init__(
        self,
        bedrock_runtime: Any,
        model_id: str = EMBED_MODEL_ID,
        dimensions: int = EMBED_DIMENSIONS,
    ) -> None:
        self.bedrock_runtime = bedrock_runtime
        self.model_id = model_id
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        body = json.dumps({
            "inputText": text[:EMBED_MAX_CHARS],
            "dimensions": self.dimensions,
            "normalize": True,
        })
        out = self.bedrock_runtime.invoke_model(
            modelId=self.model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        return list(json.loads(out["body"].read())["embedding"])
# </launchpad-opensearch-core>
