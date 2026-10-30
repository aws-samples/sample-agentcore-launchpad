"""Hermetic fakes for the N6 self-hosted Memory/KB substrate (OpenSearch, S3, SQS,
Titan embeddings, Bedrock converse). Every later N6 test suite (`test_memory_selfhosted`,
`test_knowledge_selfhosted`, `test_memory_extractor`, …) is driven exclusively through
these — spec C8 forbids any AWS network call at test time."""

import copy
import hashlib
import io
import math
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from app.services.opensearch_http import OpenSearchError


class FakeEmbedder:
    """Deterministic 16-dim unit vector derived from a token bag — identical texts
    produce cosine similarity 1.0; related texts (shared tokens) score higher than
    unrelated ones, which is all the N6 suites need from "semantic" similarity.

    Each token's contribution is a dense pseudo-random vector derived from
    ``hashlib.sha256`` (the first 16 digest bytes, read as signed floats) rather
    than Python's built-in ``hash()``, which is randomly salted per process
    (``PYTHONHASHSEED`` is unset in this repo) — a single-bucket scheme built on
    it collides unpredictably between runs. SHA-256 is stable across processes
    and interpreters, so two different tokens land at the same (deterministic,
    not one-hot) point in the 16-dim space only when their full digests happen
    to agree there, which is far less likely than a 1-in-16 bucket collision and,
    crucially, is either always true or never true for a given pair of tokens —
    never flaky.
    """

    DIMS = 16

    def _token_vector(self, token: str) -> list[float]:
        digest = hashlib.sha256(token.encode()).digest()[: self.DIMS]
        return [float(b - 128) for b in digest]

    def embed(self, text: str) -> list[float]:
        counts = Counter(text.lower().split())
        vec = [0.0] * self.DIMS
        for token, n in counts.items():
            token_vec = self._token_vector(token)
            for i, v in enumerate(token_vec):
                vec[i] += v * float(n)
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]


def _match(doc: dict[str, Any], clause: dict[str, Any]) -> bool:
    if "term" in clause:
        ((field, value),) = clause["term"].items()
        current = doc.get(field)
        if isinstance(current, list):
            return value in current
        return current == value
    if "terms" in clause:
        ((field, values),) = clause["terms"].items()
        current = doc.get(field)
        if isinstance(current, list):
            return any(v in current for v in values)
        return current in values
    if "prefix" in clause:
        ((field, value),) = clause["prefix"].items()
        current = doc.get(field)
        return isinstance(current, str) and current.startswith(value)
    if "exists" in clause:
        field = clause["exists"]["field"]
        return doc.get(field) is not None
    if "range" in clause:
        # ISO-8601 strings compare lexically in UTC with a fixed offset — what the memory
        # records carry (`created_at` / `updated_at`); numbers compare as numbers.
        ((field, bounds),) = clause["range"].items()
        current = doc.get(field)
        if current is None:
            return False
        for op, bound in bounds.items():
            if op == "gte" and not current >= bound:
                return False
            if op == "gt" and not current > bound:
                return False
            if op == "lte" and not current <= bound:
                return False
            if op == "lt" and not current < bound:
                return False
        return True
    if "bool" in clause:
        # nested bool: filter / must are AND, must_not is NOT, should is OR (when present
        # and nothing else decides — OpenSearch's `minimum_should_match: 1` semantics for a
        # should-only clause, which is how the memory core writes it)
        spec = clause["bool"]
        for sub in list(spec.get("filter", [])) + list(spec.get("must", [])):
            if not _match(doc, sub):
                return False
        for sub in spec.get("must_not", []):
            if _match(doc, sub):
                return False
        should = list(spec.get("should", []))
        if should and not any(_match(doc, sub) for sub in should):
            return False
        return True
    if "match_all" in clause:
        return True
    raise ValueError(f"FakeOpenSearch cannot match clause: {clause!r}")


def _filters_from_query(query: dict[str, Any]) -> list[dict[str, Any]]:
    """Accept a bare clause or `{"bool": {"filter": [...]}}` (both appear in the
    N6 call sites: `delete_by_query` takes a bare clause, `search` a bool query)."""
    if "bool" in query:
        spec = query["bool"]
        if set(spec) <= {"filter"}:
            return list(spec.get("filter", []))
        return [query]  # must_not / should present: evaluate the whole bool clause
    return [query]


class FakeOpenSearch:
    """Same method surface as `opensearch_http.OpenSearchHttp`, backed by an
    in-memory dict of indices. Good enough k-NN: ranks by cosine similarity on the
    (still-present) embedding field rather than any real HNSW approximation."""

    def __init__(self) -> None:
        self.indices: dict[str, dict[str, dict[str, Any]]] = {}
        # the body each index was created with (r14-a: served back on `GET /<i>/_mapping`)
        self.bodies: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, path: str, body: Any = None, params: Any = None) -> dict:
        self.requests.append({"method": method, "path": path, "body": body, "params": params})
        handled = self.mapping_request(method, path, body)
        if handled is None:
            handled = self.update_request(method, path, body)
        if handled is None:
            handled = self.count_request(method, path, body)
        return {} if handled is None else handled

    def count_request(self, method: str, path: str, body: Any = None) -> dict | None:
        """`POST /<index>/_count {"query": …}` (N17-obs274: the last slice's completeness
        check); `None` for any other path."""
        parts = path.split("/")
        if method != "POST" or len(parts) != 3 or parts[2] != "_count":
            return None
        bucket = self.indices.get(parts[1], {})
        clauses = _filters_from_query((body or {}).get("query") or {"match_all": {}})
        return {"count": sum(1 for doc in bucket.values()
                             if all(_match(doc, c) for c in clauses))}

    def update_request(self, method: str, path: str, body: Any = None) -> dict | None:
        """`POST /<index>/_update/<id> {"doc": …}` the way OpenSearch applies it (N17-obs273:
        the extractor's job `updated_at` bump goes through `request` to pass
        `retry_on_conflict`); 404 for a missing document, `None` for any other path."""
        parts = path.split("/")
        if method != "POST" or len(parts) != 4 or parts[2] != "_update":
            return None
        bucket = self.indices.get(parts[1], {})
        if parts[3] not in bucket:
            raise OpenSearchError(404, "document_missing_exception")
        script = (body or {}).get("script")
        if script is not None:
            return self._scripted_update(bucket, parts[3], script)
        bucket[parts[3]] = {**bucket[parts[3]], **(body or {}).get("doc", {})}
        return {"result": "updated"}

    @staticmethod
    def _scripted_update(bucket: dict, doc_id: str, script: dict) -> dict:
        """The one painless script the extractor sends (N17-obs279's complete-once guard,
        `handler._COMPLETE_ONCE_SCRIPT`): `noop` when the document already has
        `params.status` AND `params.counts`, else `putAll(params.fields)`. Any other script
        is refused so a new one cannot pass unemulated."""
        source, params = script.get("source", ""), script.get("params") or {}
        if "ctx.op = 'noop'" not in source or "putAll(params.fields)" not in source:
            raise NotImplementedError(f"FakeOpenSearch cannot run script {source!r}")
        doc = bucket[doc_id]
        if doc.get("status") == params["status"] and doc.get("counts") == params["counts"]:
            return {"result": "noop"}
        bucket[doc_id] = {**doc, **copy.deepcopy(params["fields"])}
        return {"result": "updated"}

    def mapping_request(self, method: str, path: str, body: Any = None) -> dict | None:
        """`GET` / `PUT /<index>/_mapping` the way OpenSearch answers them (r14-a's additive
        mapping update); `None` for any other path so a subclass can layer its own."""
        if not path.endswith("/_mapping") or path.count("/") != 2:
            return None
        name = path[1:-len("/_mapping")]
        if name not in self.indices:
            raise OpenSearchError(404, "index_not_found_exception")
        mapping = self.bodies.setdefault(name, {}).setdefault("mappings", {})
        if method == "GET":
            return {name: {"mappings": mapping}}
        props = mapping.setdefault("properties", {})
        for field, spec in (body or {}).get("properties", {}).items():
            if field in props and props[field] != spec:
                raise OpenSearchError(400, "illegal_argument_exception: mapper conflict")
            props[field] = spec
        return {"acknowledged": True}

    def index_exists(self, name: str) -> bool:
        return name in self.indices

    def ensure_index(self, name: str, body: dict) -> bool:
        if name in self.indices:
            return False
        self.indices[name] = {}
        self.bodies[name] = copy.deepcopy(body)
        return True

    def delete_index(self, name: str) -> None:
        self.indices.pop(name, None)

    def put_doc(self, index: str, doc_id: str, doc: dict, refresh: bool = True) -> None:
        self.indices.setdefault(index, {})[doc_id] = dict(doc)

    def get_doc(self, index: str, doc_id: str) -> dict | None:
        doc = self.indices.get(index, {}).get(doc_id)
        return dict(doc) if doc is not None else None

    def update_doc(self, index: str, doc_id: str, fields: dict, refresh: bool = True) -> bool:
        bucket = self.indices.get(index, {})
        if doc_id not in bucket:
            return False
        bucket[doc_id] = {**bucket[doc_id], **fields}
        return True

    def delete_doc(self, index: str, doc_id: str, refresh: bool = True) -> bool:
        return self.indices.get(index, {}).pop(doc_id, None) is not None

    def bulk(self, index: str, docs: list[tuple[str, dict]], refresh: bool = True) -> int:
        bucket = self.indices.setdefault(index, {})
        for doc_id, doc in docs:
            bucket[doc_id] = dict(doc)
        return len(docs)

    def search(self, index: str, body: dict) -> dict:
        docs = self.indices.get(index, {})
        query = body.get("query") or {}
        clauses = _filters_from_query(query) if query else []
        matched = [(doc_id, doc) for doc_id, doc in docs.items()
                   if all(_match(doc, c) for c in clauses)]
        for sort_spec in reversed(body.get("sort") or []):
            ((field, opts),) = sort_spec.items()
            reverse = opts.get("order") == "desc"
            matched.sort(key=lambda item: (item[1].get(field) is None, item[1].get(field)),
                         reverse=reverse)
        total = len(matched)
        start = int(body.get("from", 0) or 0)
        size = body.get("size")
        page = matched[start: start + size] if size is not None else matched[start:]
        excludes = (body.get("_source") or {}).get("excludes") or []
        hits = []
        for doc_id, doc in page:
            source = {k: v for k, v in doc.items() if k not in excludes}
            hits.append({"_id": doc_id, "_score": 1.0, "_source": source})
        return {"hits": {"total": {"value": total}, "hits": hits}}

    def knn_search(self, index: str, vector: list[float], k: int,
                    filters: list[dict], include_embedding: bool = False) -> list[dict]:
        docs = self.indices.get(index, {})
        matched = [(doc_id, doc) for doc_id, doc in docs.items()
                   if all(_match(doc, c) for c in filters) and doc.get("embedding") is not None]
        scored = sorted(
            matched, key=lambda item: cosine(vector, item[1]["embedding"]), reverse=True
        )
        out = []
        for doc_id, doc in scored[:k]:
            source = dict(doc)
            if not include_embedding:
                source.pop("embedding", None)
            out.append({"_id": doc_id, "_score": cosine(vector, doc["embedding"]), **source})
        return out

    def delete_by_query(self, index: str, query: dict) -> int:
        bucket = self.indices.get(index, {})
        clauses = _filters_from_query(query)
        to_delete = [doc_id for doc_id, doc in bucket.items()
                     if all(_match(doc, c) for c in clauses)]
        for doc_id in to_delete:
            del bucket[doc_id]
        return len(to_delete)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class FakeS3:
    """Enough of S3 for snapshot/document storage: `objects` is keyed by
    `(bucket, key)` so a single fake instance can stand in for the whole account."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}

    @staticmethod
    def _etag(body: bytes) -> str:
        return '"' + hashlib.md5(body).hexdigest() + '"'  # noqa: S324 — S3's own ETag shape

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kw: Any) -> dict:
        body = Body if isinstance(Body, bytes) else bytes(Body)
        self.objects[(Bucket, Key)] = {"Body": body, "LastModified": datetime.now(UTC)}
        return {"ETag": self._etag(body)}

    def get_object(self, *, Bucket: str, Key: str, **kw: Any) -> dict:
        entry = self.objects.get((Bucket, Key))
        if entry is None:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject")
        return {
            "Body": io.BytesIO(entry["Body"]),
            "LastModified": entry["LastModified"],
            "ContentLength": len(entry["Body"]),
            "ETag": self._etag(entry["Body"]),
        }

    def head_object(self, *, Bucket: str, Key: str, **kw: Any) -> dict:
        entry = self.objects.get((Bucket, Key))
        if entry is None:
            raise ClientError({"Error": {"Code": "404", "Message": "not found"}}, "HeadObject")
        return {"ContentLength": len(entry["Body"]), "LastModified": entry["LastModified"],
                "ETag": self._etag(entry["Body"])}

    def list_objects_v2(self, *, Bucket: str, Prefix: str = "", Delimiter: str = "",
                         MaxKeys: int = 1000, ContinuationToken: str | None = None,
                         **kw: Any) -> dict:
        keys = sorted(k for (b, k) in self.objects if b == Bucket and k.startswith(Prefix))
        common_prefixes: list[str] = []
        contents_keys: list[str] = []
        if Delimiter:
            seen: set[str] = set()
            for key in keys:
                rest = key[len(Prefix):]
                if Delimiter in rest:
                    segment = Prefix + rest.split(Delimiter, 1)[0] + Delimiter
                    if segment not in seen:
                        seen.add(segment)
                        common_prefixes.append(segment)
                else:
                    contents_keys.append(key)
        else:
            contents_keys = keys

        items = common_prefixes if Delimiter else contents_keys
        offset = int(ContinuationToken) if ContinuationToken else 0
        page = items[offset: offset + MaxKeys]
        truncated = offset + MaxKeys < len(items)

        out: dict[str, Any] = {"IsTruncated": truncated, "KeyCount": len(page)}
        if Delimiter:
            out["CommonPrefixes"] = [{"Prefix": p} for p in page]
            out["Contents"] = []
        else:
            out["Contents"] = [
                {"Key": k, "LastModified": self.objects[(Bucket, k)]["LastModified"],
                 "Size": len(self.objects[(Bucket, k)]["Body"]),
                 "ETag": self._etag(self.objects[(Bucket, k)]["Body"])}
                for k in page
            ]
            out["CommonPrefixes"] = []
        if truncated:
            out["NextContinuationToken"] = str(offset + MaxKeys)
        return out

    def delete_objects(self, *, Bucket: str, Delete: dict, **kw: Any) -> dict:
        deleted = []
        for obj in Delete.get("Objects", []):
            key = obj["Key"]
            if (Bucket, key) in self.objects:
                del self.objects[(Bucket, key)]
                deleted.append({"Key": key})
        return {"Deleted": deleted}

    def get_paginator(self, operation_name: str) -> "_FakeS3Paginator":
        if operation_name != "list_objects_v2":
            raise NotImplementedError(operation_name)
        return _FakeS3Paginator(self)


class _FakeS3Paginator:
    def __init__(self, s3: FakeS3) -> None:
        self._s3 = s3

    def paginate(self, **kwargs: Any):
        token = kwargs.pop("ContinuationToken", None)
        while True:
            page = self._s3.list_objects_v2(ContinuationToken=token, **kwargs)
            yield page
            if not page.get("IsTruncated"):
                return
            token = page.get("NextContinuationToken")


class FakeSQS:
    """`send_message` records `(QueueUrl, parsed-JSON-body)` tuples on `.sent`."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def send_message(self, *, QueueUrl: str, MessageBody: str, **kw: Any) -> dict:
        import json

        self.sent.append((QueueUrl, json.loads(MessageBody)))
        return {"MessageId": f"msg-{len(self.sent)}"}


class FakeBedrockRuntime:
    """Stands in for a `bedrock-runtime` client: `invoke_model` embeds via the given
    `FakeEmbedder` (Titan-shaped response), `converse` pops the next scripted output
    text (wrapped with a `reasoningContent` block the extractor must ignore)."""

    def __init__(self, embedder: FakeEmbedder, converse_outputs: list[str] | None = None) -> None:
        self.embedder = embedder
        self._converse_outputs = list(converse_outputs or [])
        self.converse_calls: list[dict] = []

    @property
    def outputs(self) -> list[str]:
        """Public alias for `_converse_outputs` so a test can re-script the queue
        between calls on the same fake instance (e.g. `deps.bedrock.outputs =
        [...]`) without reaching into the private attribute."""
        return self._converse_outputs

    @outputs.setter
    def outputs(self, value: list[str]) -> None:
        self._converse_outputs = list(value)

    def invoke_model(self, *, modelId: str, body: str, **kw: Any) -> dict:
        import json

        payload = json.loads(body)
        vector = self.embedder.embed(payload["inputText"])
        return {"body": io.BytesIO(json.dumps({"embedding": vector}).encode())}

    def converse(self, **kwargs: Any) -> dict:
        self.converse_calls.append(kwargs)
        text = self._converse_outputs.pop(0) if self._converse_outputs else ""
        return {
            "output": {
                "message": {
                    "content": [
                        {"reasoningContent": {"reasoningText": {"text": "thinking…"}}},
                        {"text": text},
                    ]
                }
            }
        }
