# ruff: noqa: F811  — pytest fixtures are imported by name from the sibling suites
"""Round-7 fault probes at the storage seams no lifecycle graph edge reaches (r17-m; testing.md
§6.9 "Round 7"): the KB ingest Lambda (S3 `get_object`, the embedding model, the OpenSearch bulk),
the registry skill-record sweep (`skills/<name>/` per-key `delete_object`) and the S3
`DeleteObjects` partial-failure body on the memory store purge. Each probe serves ONE recorded /
pinned shape at the seam and judges the outcome against the contract sentence it cites; a probe
that fails is a finding (registered + strict pin), never a weaker oracle.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID
from app.services import aws_clients
from app.services import opensearch_http as core
from app.services.opensearch_http import OpenSearchError
from tests.conftest import set_default_resources
from tests.graph import faults
from tests.graph.test_graph_crossgraph import kbmem_fakes  # noqa: F401 — fixture by name
from tests.property.test_knowledge_ingest_properties import (  # noqa: F401 — fixtures by name
    _SEQ,
    BUCKET,
    T0,
    _create_kb,
    handler,
    world,
)
from tests.test_registry_manage import FakeS3 as RegistryFakeS3
from tests.test_visibility import ada, admin, app, bob  # noqa: F401 — fixtures by name

# ---------------------------------------------------------------------------
# KB ingest — one shape at one of the three seams of one document
# ---------------------------------------------------------------------------


class FaultyS3:
    """`FakeS3` whose `get_object` raises the shape for ONE key (every other object is read)."""

    def __init__(self, inner: Any, key: str, exc: BaseException) -> None:
        self.inner, self.key, self.exc, self.calls = inner, key, exc, []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if name != "get_object":
            return attr

        def get_object(**kw: Any) -> Any:
            self.calls.append(kw["Key"])
            if kw["Key"] == self.key:
                raise self.exc
            return attr(**kw)

        return get_object


class FaultyEmbedder:
    """The embedding model raising the shape on its FIRST call (then embedding normally)."""

    def __init__(self, inner: Any, exc: BaseException) -> None:
        self.inner, self.exc, self.calls, self.armed = inner, exc, 0, True

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        if self.armed:
            self.armed = False
            raise self.exc
        return self.inner.embed(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class FaultyBulk:
    """`FakeOpenSearch` whose `bulk` raises the shape on its FIRST call."""

    def __init__(self, inner: Any, exc: BaseException) -> None:
        self.inner, self.exc, self.armed = inner, exc, True

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if name != "bulk":
            return attr

        def bulk(*a: Any, **kw: Any) -> Any:
            if self.armed:
                self.armed = False
                raise self.exc
            return attr(*a, **kw)

        return bulk


def _ingest(world, handler, kb: dict[str, Any], deps_over: dict[str, Any]) -> dict[str, Any]:
    (ds,) = kb["data_sources"]
    index = core.kb_index_name(kb["kb_id"], "")
    deps = handler.Deps(
        s3=deps_over.get("s3", world.s3), opensearch=deps_over.get("opensearch", world.opensearch),
        bedrock=None, embedder=deps_over.get("embedder", world.embedder), now=lambda: T0, env={},
        sleep=deps_over.get("sleep", _never_sleep),  # the retry's wait never really sleeps
    )
    return handler.process_kb_ingest(
        {"kind": "kb_ingest", "job_id": f"fjob-{next(_SEQ)}", "bucket": BUCKET,
         "prefix": ds["prefix"], "index": index, "kb_id": kb["kb_id"], "ds_id": ds["ds_id"],
         "workspace_id": DEFAULT_WORKSPACE_ID},
        deps,
    )


def _never_sleep(seconds: float) -> None:
    raise AssertionError(f"the ingest tried to sleep {seconds}s — inject a recorder")


def _records(world, kb_id: str) -> dict[str, dict[str, Any]]:
    return {rid: d for rid, d in world.opensearch.indices[core.kb_docs_index("")].items()
            if d.get("kb_id") == kb_id}


def _chunks(world, kb: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return dict(world.opensearch.indices.get(core.kb_index_name(kb["kb_id"], ""), {}))


def _seed(world, kb: dict[str, Any], names=("a.txt", "b.txt", "c.txt")) -> list[str]:
    (ds,) = kb["data_sources"]
    keys = []
    for name in names:
        key = f"{ds['prefix']}{name}"
        world.s3.put_object(Bucket=BUCKET, Key=key, Body=(f"lorem ipsum {name} " * 40).encode())
        keys.append(key)
    return keys


S3_GET_SHAPES = ["s3_access_denied", "s3_no_such_key", "s3_internal_error", "s3_slow_down"]


@pytest.mark.parametrize("shape_name", S3_GET_SHAPES)
def test_kb_ingest_one_unreadable_object_fails_that_document_only(world, handler, shape_name):
    """handler.py kb_ingest: "one bad document never fails the job" — the object the shape hits
    is a FAILED record naming the exception, the others INDEXED, the job COMPLETE with
    scanned == indexed + failed (knowledge-backend.md: terminal `COMPLETE`); a FAILED document
    owns no chunks (no half state)."""
    kb = _create_kb(world)
    keys = _seed(world, kb)
    exc = faults.SHAPES[shape_name].exception("GetObject")
    out = _ingest(world, handler, kb, {"s3": FaultyS3(world.s3, keys[1], exc)})
    counts = out["counts"]
    assert out["status"] == "COMPLETE", out
    assert counts["numberOfDocumentsScanned"] == 3
    assert counts["numberOfNewDocumentsIndexed"] == 2 and counts["numberOfDocumentsFailed"] == 1
    records = _records(world, kb["kb_id"])
    failed = [r for r in records.values() if r["status"] == "FAILED"]
    assert len(failed) == 1 and type(exc).__name__ in failed[0]["status_reason"], failed
    assert failed[0]["chunk_count"] == 0
    chunk_uris = {c.get("doc_uri") for c in _chunks(world, kb).values()}
    assert failed[0]["uri"] not in chunk_uris, "a FAILED document still owns chunks"
    assert len(chunk_uris) == 2


EMBED_SHAPES = ["bedrock_embed_throttle", "bedrock_embed_5xx", "bedrock_embed_validation"]
TRANSIENT_EMBED_SHAPES = ["bedrock_embed_throttle", "bedrock_embed_5xx"]


def test_kb_ingest_a_permanent_embedding_fault_fails_that_document_without_retry(world, handler):
    """N17-obs120 companion (flipped in cn/r17-t): a PERMANENT shape — `ValidationException`,
    an unknown embedding model — lands in the per-document `except` at once: the document is
    FAILED (reason = the exception type), ONE `embed` call, no wait, the job COMPLETE."""
    kb = _create_kb(world)
    _seed(world, kb, names=("a.txt",))
    shape = faults.SHAPES["bedrock_embed_validation"]
    embedder = FaultyEmbedder(world.embedder, shape.exception("InvokeModel"))
    slept: list[float] = []
    out = _ingest(world, handler, kb, {"embedder": embedder, "sleep": slept.append})
    assert out["status"] == "COMPLETE", out
    (rec,) = _records(world, kb["kb_id"]).values()
    assert rec["status"] == "FAILED" and "ClientError" in rec["status_reason"], rec
    assert embedder.calls == 1 and slept == [], (embedder.calls, slept)


class AlwaysFaultyEmbedder:
    """The embedding model raising the shape on EVERY call (a persistent transient fault)."""

    def __init__(self, exc: BaseException) -> None:
        self.exc, self.calls = exc, 0

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        raise self.exc


@pytest.mark.parametrize("shape_name", TRANSIENT_EMBED_SHAPES)
def test_kb_ingest_a_persistent_transient_embedding_fault_fails_the_document_after_the_backoff(
    world, handler, shape_name
):
    """N17-obs120 (cn/r17-t): the retry is BOUNDED — `EMBED_RETRY_BACKOFF_S` waits, one more
    attempt than waits; a fault that outlives them fails the document the way it always did
    (FAILED with the type, the job COMPLETE with `numberOfDocumentsFailed: 1`) and the hub's
    `start_sync` stays the manual retry."""
    kb = _create_kb(world)
    _seed(world, kb, names=("a.txt",))
    embedder = AlwaysFaultyEmbedder(faults.SHAPES[shape_name].exception("InvokeModel"))
    slept: list[float] = []
    out = _ingest(world, handler, kb, {"embedder": embedder, "sleep": slept.append})
    assert out["status"] == "COMPLETE" and out["counts"]["numberOfDocumentsFailed"] == 1, out
    (rec,) = _records(world, kb["kb_id"]).values()
    assert rec["status"] == "FAILED" and "ClientError" in rec["status_reason"], rec
    assert embedder.calls == len(handler.EMBED_RETRY_BACKOFF_S) + 1
    assert slept == list(handler.EMBED_RETRY_BACKOFF_S)
    assert rec["chunk_count"] == 0 and _chunks(world, kb) == {}


@pytest.mark.parametrize("status", [503, 429])
def test_kb_ingest_a_transient_bulk_fault_fails_the_document_not_the_job(world, handler, status):
    """`opensearch.bulk` (the chunk write) raising a transient OpenSearch status for one document
    is the per-document `except` too: the document FAILED, its half-written chunks removed by the
    final refresh path, the job COMPLETE."""
    kb = _create_kb(world)
    _seed(world, kb, names=("a.txt", "b.txt"))
    body = (
        '{"error":{"root_cause":[{"type":"es_rejected_execution_exception"}]},'
        f'"status":{status}}}'
    )
    faulty = FaultyBulk(world.opensearch, OpenSearchError(status, body))
    out = _ingest(world, handler, kb, {"opensearch": faulty})
    assert out["status"] == "COMPLETE", out
    counts = out["counts"]
    assert counts["numberOfDocumentsFailed"] == 1 and counts["numberOfNewDocumentsIndexed"] == 1
    records = _records(world, kb["kb_id"])
    failed = [r for r in records.values() if r["status"] == "FAILED"]
    assert len(failed) == 1 and "OpenSearchError" in failed[0]["status_reason"]
    assert failed[0]["chunk_count"] == 0


# ---------------------------------------------------------------------------
# registry skill-record delete — the `skills/<name>/` sweep (N16-obs35, cn/r17-j)
# ---------------------------------------------------------------------------


class RefusingRegistryS3(RegistryFakeS3):
    """The registry test fake whose `DeleteObjects` raises the shape (the shared deleter
    `s3_sweep.delete_keys` batches through it since cn/r17-t)."""

    def __init__(self, exc: BaseException, existing=None) -> None:
        super().__init__(existing)
        self.exc = exc
        self.attempts: list[str] = []

    def delete_objects(self, Bucket, Delete, **kw):  # noqa: N803 — boto kwargs
        self.attempts.extend(o["Key"] for o in Delete.get("Objects", []))
        raise self.exc


def _register_skill(client, fake, monkeypatch) -> tuple[str, str]:
    set_default_resources({"artifacts_bucket": "bkt", "registry_id": "local-default"})
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    name = f"fault-skill-{uuid.uuid4().hex[:6]}"
    res = client.post("/api/registry/records", json={
        "type": "AGENT_SKILLS", "name": name, "skill_md": f"---\nname: {name}\n---\n# {name}",
    })
    assert res.status_code == 201, res.text
    assert fake.uploaded == [f"skills/{name}/SKILL.md"]
    return name, res.json()["record_id"]


@pytest.mark.parametrize("shape_name", ["s3_access_denied", "s3_slow_down", "s3_internal_error"])
def test_registry_skill_sweep_reports_a_refused_object_delete(client, monkeypatch, shape_name):
    """N17-obs118 (fixed in cn/r17-t). api.md "Deleting a skill record removes its files" /
    registry-backend.md `delete_record`: the sweep is best-effort — "`objects.error` on an S3
    failure, the delete still 200". A `DeleteObjects` the shape refuses answers
    `objects_deleted: false` with a REDACTED `error` (type, code, operation — never the S3
    message), never `objects_deleted: true` while the file is still there. Before the fix
    `skill_ingest._delete_keys` swallowed every per-key exception."""
    shape = faults.SHAPES[shape_name]
    exc = shape.exception("DeleteObjects")
    fake = RefusingRegistryS3(exc)
    name, record_id = _register_skill(client, fake, monkeypatch)
    res = client.delete(f"/api/registry/records/{record_id}")
    assert res.status_code == 200 and res.json()["deleted"] is True, res.text
    assert fake.attempts == [f"skills/{name}/SKILL.md"], fake.attempts  # the sweep did try
    assert f"skills/{name}/SKILL.md" not in fake.deleted
    objects = res.json()["objects"]
    assert objects["objects_deleted"] is False, (
        "the sweep reported the object deleted although the delete was refused", objects)
    assert objects["error"] == f"ClientError: {shape.code} (DeleteObjects)", objects
    assert shape.message not in res.text  # api.md "Errors": no AWS message leaves the hub
    assert client.get(f"/api/registry/records/{record_id}").status_code == 404


def test_registry_skill_sweep_reports_a_key_refused_inside_a_200(client, monkeypatch):
    """N17-obs118 × obs119: the `DeleteObjects` partial-failure body (200 + `Errors[]`) on the
    registry sweep — the refused key is residue the response names (`objects_deleted: false`,
    `error` = the sweep summary with the S3 CODE only), the row is gone either way."""
    fake = RegistryFakeS3()
    name, record_id = _register_skill(client, fake, monkeypatch)
    shape = faults.SHAPES["s3_delete_partial"]
    seen: list[list[str]] = []

    def delete_objects(Bucket, Delete, **kw):  # noqa: N803 — boto kwargs
        keys = [o["Key"] for o in Delete.get("Objects", [])]
        seen.append(keys)
        return shape.partial_delete([""] + keys)  # every REAL key refused

    monkeypatch.setattr(fake, "delete_objects", delete_objects)
    res = client.delete(f"/api/registry/records/{record_id}")
    assert res.status_code == 200 and res.json()["deleted"] is True, res.text
    assert seen == [[f"skills/{name}/SKILL.md"]], seen
    objects = res.json()["objects"]
    assert objects == {"objects_deleted": False, "prefix": f"skills/{name}/",
                       "error": "1 of 1 objects refused (AccessDenied)"}, objects
    assert "Access Denied" not in res.text


# ---------------------------------------------------------------------------
# S3 DeleteObjects partial failure — the memory store purge
# ---------------------------------------------------------------------------


class PartialDeleteS3:
    """`FakeS3` whose `delete_objects` answers the partial-failure body: the first key goes,
    the rest stay (S3 answers 200 with `Errors[]` for the keys it refused)."""

    def __init__(self, inner: Any, shape: faults.Shape) -> None:
        self.inner, self.shape, self.calls = inner, shape, 0

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if name != "delete_objects":
            return attr

        def delete_objects(*, Bucket, Delete, **kw):  # noqa: N803 — boto kwargs
            self.calls += 1
            keys = [o["Key"] for o in Delete.get("Objects", [])]
            if keys:
                attr(Bucket=Bucket, Delete={"Objects": [{"Key": keys[0]}]})
            return self.shape.partial_delete(keys)

        return delete_objects


def test_memory_store_purge_reports_a_partial_delete_objects_failure(admin, kbmem_fakes, caplog):
    """N17-obs119 (fixed in cn/r17-t). memory-backend.md / N14-obs4: the store delete is complete
    once the ledger row is gone and the storage purge is best-effort — but best-effort has to
    SEE a refused key: a `DeleteObjects` 200 whose `Errors[]` names keys that stayed is now
    `cleanup.s3 = skipped` with one redacted `cleanup_warnings` line (`S3SweepResidue`: counts
    and S3 codes, never a key or the S3 message) and one WARNING log line; the index purge
    still runs. Before the fix `_purge_s3_prefix` called `delete_objects` once per page and
    never read the answer."""
    fakes = kbmem_fakes
    res = admin.post("/api/memory/resources", json={"name": f"g_purge_{uuid.uuid4().hex[:6]}"})
    assert res.status_code == 201, res.text
    sid = res.json()["id"]
    from tests.test_memory_selfhosted import SUBSTRATE

    prefix = f"{SUBSTRATE['memory_prefix']}/{sid}/"
    bucket = SUBSTRATE["memory_bucket"]
    keys = [f"{prefix}events/{i}.json" for i in range(3)]
    for k in keys:
        fakes["s3"].put_object(Bucket=bucket, Key=k, Body=b"{}")
    partial = PartialDeleteS3(fakes["s3"], faults.SHAPES["s3_delete_partial"])
    from app.services import memory_backend
    from app.services.memory_selfhosted import SelfHostedMemoryBackend
    from tests.memory_fakes import FakeSQS

    memory_backend.set_backend(SelfHostedMemoryBackend(
        s3=partial, opensearch=fakes["opensearch"], sqs=FakeSQS(), embedder=fakes["embedder"],
        clock=lambda: T0))
    with caplog.at_level("WARNING", logger="launchpad.memory"):
        res = admin.delete(f"/api/memory/resources/{sid}")
    assert res.status_code == 200, res.text
    assert res.json() == {
        "id": sid, "deleted": True, "cleanup": {"s3": "skipped", "index": "ok"},
        "cleanup_warnings": ["s3: S3SweepResidue: 2 of 3 objects refused (AccessDenied ×2)"],
    }, res.text
    assert partial.calls == 1
    left = sorted(k for (b, k) in fakes["s3"].objects if k.startswith(prefix))
    assert left == keys[1:], left  # the residue S3 named is exactly what the warning counts
    assert "Access Denied" not in res.text and "Access Denied" not in caplog.text
    assert not any(k in res.text for k in keys), "no key in the response"
    assert sum(1 for r in caplog.records if r.levelname == "WARNING" and sid in r.getMessage()) == 1
    assert admin.get(f"/api/memory/resources/{sid}").status_code == 404


@pytest.mark.parametrize("shape_name", TRANSIENT_EMBED_SHAPES)
def test_N17_obs120_transient_embedding_fault_is_retried(world, handler, shape_name):
    """N17-obs120 (fixed in cn/r17-t): ONE transient Bedrock fault (throttle / 5xx) on one
    chunk's embedding is retried in place — the shared `is_upstream_transient` predicate, the
    first `EMBED_RETRY_BACKOFF_S` wait through the injected sleep — and the document is INDEXED
    with every chunk; before the fix it was FAILED for good after one attempt."""
    kb = _create_kb(world)
    _seed(world, kb, names=("a.txt",))
    embedder = FaultyEmbedder(world.embedder, faults.SHAPES[shape_name].exception("InvokeModel"))
    slept: list[float] = []
    out = _ingest(world, handler, kb, {"embedder": embedder, "sleep": slept.append})
    assert out["status"] == "COMPLETE" and out["counts"]["numberOfDocumentsFailed"] == 0, out
    (rec,) = _records(world, kb["kb_id"]).values()
    assert rec["status"] == "INDEXED" and rec["chunk_count"] >= 1, rec
    assert slept == [handler.EMBED_RETRY_BACKOFF_S[0]], slept
    assert embedder.calls == rec["chunk_count"] + 1, (embedder.calls, rec["chunk_count"])
    assert len(_chunks(world, kb)) == rec["chunk_count"]
