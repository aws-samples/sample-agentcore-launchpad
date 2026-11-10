"""SelfHostedKnowledgeBackend (N6): S3 documents + per-KB k-NN index + SQS, all faked."""

import io
import json
from typing import Any

import pytest
from pypdf import PdfReader

from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent
from app.services import embedding_backend, knowledge_backend
from app.services import opensearch_http as core
from app.services.knowledge_selfhosted import SelfHostedKnowledgeBackend, kb_prefix
from tests.conftest import set_default_resources, ws_ctx
from tests.memory_fakes import FakeEmbedder, FakeOpenSearch, FakeS3, FakeSQS
from tests.test_memory_selfhosted import BUCKET, SUBSTRATE, T0


@pytest.fixture
def fakes():
    return {
        "s3": FakeS3(), "opensearch": FakeOpenSearch(),
        "sqs": FakeSQS(), "embedder": FakeEmbedder(),
    }


@pytest.fixture
def backend(client, fakes):
    fakes["opensearch"].ensure_index(core.jobs_index(""), core.jobs_index_body())
    fakes["opensearch"].ensure_index(core.kb_docs_index(""), core.kb_docs_index_body())
    return SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)


@pytest.fixture
def ws():
    return ws_ctx(SUBSTRATE, region="cn-northwest-1")


def _pdf_bytes(text: str = "Hello PDF") -> bytes:
    """A minimal, hand-built one-page PDF whose content stream draws `text` with
    the standard Helvetica font — no extra dependency (reportlab is not in this
    project's tree) and `PdfReader(...).pages[0].extract_text()` returns exactly
    `text` for it."""
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 200 200] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 24 Tf 10 100 Td ({text}) Tj ET".encode()
    objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode())
        out.write(obj)
        out.write(b"\nendobj\n")
    xref_offset = out.tell()
    n = len(objects) + 1
    out.write(f"xref\n0 {n}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(b"trailer\n")
    out.write(f"<< /Size {n} /Root 1 0 R >>\n".encode())
    out.write(b"startxref\n")
    out.write(f"{xref_offset}\n".encode())
    out.write(b"%%EOF")
    return out.getvalue()


def _agent(spec, system_key=None, name="a1"):
    db = SessionLocal()
    db.add(Agent(workspace_id="default", name=name, method="zip_runtime", status="active",
                 spec=spec, system_key=system_key))
    db.commit()
    db.close()


def test_create_kb_makes_an_index_and_one_upload_source(backend, ws, fakes):
    source = {"mode": "upload", "bucket": None, "prefix": None}
    kb = backend.create_kb(ws, "handbook", "HR handbook", source)
    assert kb["kb_id"].startswith("KB") and len(kb["kb_id"]) == 14
    assert kb["status"] == "ACTIVE" and kb["arn"] is None
    (ds,) = kb["data_sources"]
    assert ds["status"] == "AVAILABLE" and ds["bucket"] == BUCKET
    assert ds["prefix"] == kb_prefix(kb["kb_id"], ds["ds_id"]) and ds["ingestion_jobs"] == []
    assert core.kb_index_name(kb["kb_id"], "") in fakes["opensearch"].indices
    assert backend.list_kbs(ws) == [{
        "kb_id": kb["kb_id"], "name": "handbook", "description": "HR handbook",
        "status": "ACTIVE", "updated_at": T0.isoformat(), "data_source_count": 1,
        "attached_agents": [], "created_by": None, "created_by_name": None,
    }]


def test_duplicate_name_409_and_external_bucket_422(backend, ws):
    backend.create_kb(ws, "dup", "", {"mode": "upload"})
    with pytest.raises(AppError) as exc:
        backend.create_kb(ws, "dup", "", {"mode": "upload"})
    assert exc.value.code == "kb.name_exists" and exc.value.status_code == 409
    with pytest.raises(AppError) as exc:
        backend.create_kb(
            ws, "ext", "", {"mode": "existing", "bucket": "someone-elses", "prefix": "x/"},
        )
    assert exc.value.code == "kb.external_bucket_unsupported" and exc.value.status_code == 422


def test_external_prefix_outside_kb_namespace_is_rejected_422(backend, ws):
    """An 'existing' source naming the workspace's own bucket but a prefix outside this
    KB's own `kb/<kb_id>/` namespace must still be refused (ruling R18): the extractor
    Lambda's role grants `s3:List*` on the whole bucket plus `GetObject` on `memory/*`
    and `kb/*`, so an unconstrained prefix would let a member point a KB index at
    another workspace's memory events, or another KB's documents."""
    bad_prefixes = [
        "shared-docs",       # not under any kb/<kb_id>/ namespace at all
        "",                  # empty — the whole bucket
        "memory/",           # every workspace's memory events
        "memory/anything",   # same, with a sub-path
        "kb/KBOTHER00000/",  # another KB's namespace
        "..",                # path traversal
        "../",
        "/etc/passwd",       # absolute path
    ]
    for i, bad_prefix in enumerate(bad_prefixes):
        with pytest.raises(AppError) as exc:
            backend.create_kb(
                ws, f"ext-bad-{i}", "",
                {"mode": "existing", "bucket": BUCKET, "prefix": bad_prefix},
            )
        assert exc.value.code == "kb.external_prefix_unsupported"
        assert exc.value.status_code == 422
        assert "prefix" in exc.value.detail


def test_index_failure_marks_the_kb_failed(backend, ws, fakes, monkeypatch):
    def boom(name, body):
        raise core.OpenSearchError(500, "boom")

    monkeypatch.setattr(fakes["opensearch"], "ensure_index", boom)
    kb = backend.create_kb(ws, "broken", "", {"mode": "upload"})
    assert kb["status"] == "FAILED" and "boom" in kb["failure_reasons"][0]


def test_upload_stores_files_and_writes_a_pdf_text_sidecar(backend, ws, fakes):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]
    files = [("notes.md", b"# Notes\nhello"), ("guide.pdf", _pdf_bytes("Hello PDF"))]
    out = backend.upload_files(ws, kb["kb_id"], files)
    assert out["keys"] == [f"{ds['prefix']}notes.md", f"{ds['prefix']}guide.pdf"]
    sidecar_key = f"{ds['prefix']}_text/guide.pdf.txt"
    sidecar = fakes["s3"].get_object(Bucket=BUCKET, Key=sidecar_key)["Body"].read().decode()
    assert "Hello PDF" in sidecar
    with pytest.raises(NotFoundError):
        backend.upload_files(ws, "KBNOPE", [("a.txt", b"a")])


def test_start_sync_seeds_a_job_doc_and_enqueues_one_message(backend, ws, fakes):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]
    out = backend.start_sync(ws, kb["kb_id"], ds["ds_id"])
    assert out["status"] == "STARTING" and out["job_id"]
    (url, msg), = fakes["sqs"].sent
    assert url == SUBSTRATE["memory_queue_url"]
    assert msg == {
        "kind": "kb_ingest", "workspace_id": "default", "kb_id": kb["kb_id"],
        "ds_id": ds["ds_id"], "job_id": out["job_id"], "bucket": BUCKET,
        "prefix": ds["prefix"], "index": core.kb_index_name(kb["kb_id"], ""),
        # D21 (M1-d): the registry's active embedding row rides with every message
        "embedding": embedding_backend.message_block(),
    }
    assert msg["embedding"]["kind"] == "bedrock" and msg["embedding"]["dimension"] == 1024
    job = fakes["opensearch"].get_doc(core.jobs_index(""), out["job_id"])
    assert job["status"] == "STARTING" and job["kind"] == "kb_ingest"
    jobs = backend.list_ingestion_jobs(ws, kb["kb_id"], ds["ds_id"])
    assert jobs[0]["job_id"] == out["job_id"] and jobs[0]["statistics"] == {}
    assert jobs[0]["started_at"] == T0.isoformat()
    detail = backend.get_kb_detail(ws, kb["kb_id"])
    assert detail["data_sources"][0]["ingestion_jobs"][0]["status"] == "STARTING"


def test_documents_page_comes_from_the_kb_docs_index(backend, ws, fakes):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]
    for i in range(3):
        doc_id = f"{kb['kb_id']}:{ds['ds_id']}:d{i}"
        doc = {
            "kb_id": kb["kb_id"], "ds_id": ds["ds_id"],
            "uri": f"s3://{BUCKET}/{ds['prefix']}d{i}.md", "name": f"d{i}.md",
            "status": "INDEXED", "status_reason": None, "indexed_at": T0.isoformat(),
            "size_bytes": 10 + i, "uploaded_at": T0.isoformat(), "chunk_count": 1,
        }
        fakes["opensearch"].put_doc(core.kb_docs_index(""), doc_id, doc)
    page = backend.list_documents(ws, kb["kb_id"], ds["ds_id"], page_size=2)
    assert [d["name"] for d in page["documents"]] == ["d0.md", "d1.md"]
    assert page["next_token"] == "2"
    rest = backend.list_documents(ws, kb["kb_id"], ds["ds_id"], page_size=2, token="2")
    assert [d["name"] for d in rest["documents"]] == ["d2.md"] and rest["next_token"] is None
    assert rest["documents"][0]["uri"].startswith("s3://")
    assert rest["documents"][0]["status"] == "INDEXED"


def test_query_is_knn_over_the_kb_index(backend, ws, fakes):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    idx = core.kb_index_name(kb["kb_id"], "")
    texts = ["Vacation policy: 20 days per year", "Expense reports are due Friday"]
    for i, text in enumerate(texts):
        doc = {
            "kb_id": kb["kb_id"], "doc_uri": f"s3://{BUCKET}/kb/x/{i}.md", "doc_name": f"{i}.md",
            "chunk_index": 0, "text": text, "embedding": fakes["embedder"].embed(text),
            "metadata": {}, "indexed_at": T0.isoformat(),
        }
        fakes["opensearch"].put_doc(idx, f"c{i}", doc)
    out = backend.query(ws, kb["kb_id"], "how many vacation days", 1)
    assert len(out) == 1 and out[0]["text"] == "Vacation policy: 20 days per year"
    assert out[0]["score"] > 0
    assert out[0]["location_uri"] == f"s3://{BUCKET}/kb/x/0.md"
    assert out[0]["metadata"] == {"doc_name": "0.md", "chunk_index": 0}


def test_delete_refuses_attached_kbs_unless_forced_and_purges_everything(backend, ws, fakes):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]
    spec = {"knowledge_bases": [{"kb_id": kb["kb_id"], "name": "docs", "description": ""}]}
    _agent(spec, name="reader")
    assert backend.attached_agents(ws, kb["kb_id"]) == ["reader"]
    assert backend.get_kb_detail(ws, kb["kb_id"])["attached_agents"] == ["reader"]
    with pytest.raises(knowledge_backend.KBAttachedError) as exc:
        backend.delete_kb(ws, kb["kb_id"])
    assert exc.value.status_code == 409 and exc.value.detail == {"agents": ["reader"]}
    fakes["s3"].put_object(Bucket=BUCKET, Key=f"{ds['prefix']}a.md", Body=b"x")
    doc = {"kb_id": kb["kb_id"], "ds_id": ds["ds_id"], "name": "a.md"}
    fakes["opensearch"].put_doc(core.kb_docs_index(""), "doc", doc)
    assert backend.delete_kb(ws, kb["kb_id"], force=True) == {
        "kb_id": kb["kb_id"], "deleted": True,
        "cleanup": {"s3": "ok", "index": "ok", "docs": "ok"},  # WB-008: additive
        "cleanup_warnings": [], "cleanup_job_id": None,
    }
    assert core.kb_index_name(kb["kb_id"], "") not in fakes["opensearch"].indices
    assert not [k for (_, k) in fakes["s3"].objects if k.startswith(f"kb/{kb['kb_id']}/")]
    assert fakes["opensearch"].indices[core.kb_docs_index("")] == {}
    with pytest.raises(NotFoundError) as exc:
        backend.get_kb_detail(ws, kb["kb_id"])
    assert exc.value.code == "kb.not_found"


def test_system_preset_guard_is_a_ledger_read(backend, ws, client):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    _agent({"knowledge_bases": [{"kb_id": kb["kb_id"]}]}, system_key="frontdesk", name="frontdesk")
    db = SessionLocal()
    with pytest.raises(AppError) as exc:
        knowledge_backend.refuse_if_attached_to_system_agent(db, ws, kb["kb_id"])
    assert exc.value.code == "kb.attached_to_system_agent"
    assert exc.value.status_code == 409
    assert exc.value.detail == {"agents": ["frontdesk"]}
    assert knowledge_backend.attached_agent_names(db, ws, kb["kb_id"]) == ["frontdesk"]
    db.close()
    with pytest.raises(AppError):
        backend.refuse_if_attached_to_system_agent(ws, kb["kb_id"])
    with pytest.raises(AppError) as exc:
        backend.delete_kb(ws, kb["kb_id"], force=True)  # force never overrides a preset mount
    assert exc.value.code == "kb.attached_to_system_agent"


def test_data_sources_can_be_added_and_removed(backend, ws):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    # An 'existing' source is only valid inside this KB's own kb/<kb_id>/ namespace
    # (ruling R18) — the kb_id is known here because the KB already exists, unlike at
    # create_kb time. No trailing slash on input exercises the normalization too.
    prefix_in = f"kb/{kb['kb_id']}/manual-upload"
    source = {"mode": "existing", "bucket": BUCKET, "prefix": prefix_in}
    ds2 = backend.add_data_source(ws, kb["kb_id"], source)
    detail = backend.get_kb_detail(ws, kb["kb_id"])
    assert ds2["prefix"] == prefix_in + "/" and len(detail["data_sources"]) == 2
    deleted = backend.delete_data_source(ws, kb["kb_id"], ds2["ds_id"])
    assert deleted == {
        "ds_id": ds2["ds_id"], "deleted": True,
        # WB-010 (additive): an `existing`-mode source owns no objects → s3 skipped
        "cleanup": {"s3": "skipped", "index": "ok", "docs": "ok"},
        "cleanup_warnings": [], "cleanup_job_id": None,
    }
    with pytest.raises(NotFoundError) as exc:
        backend.start_sync(ws, kb["kb_id"], ds2["ds_id"])
    assert exc.value.code == "kb.data_source_not_found"


def test_update_description_and_workspace_scoping(backend, ws):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    assert backend.update_description(ws, kb["kb_id"], "new")["description"] == "new"
    other = ws_ctx(SUBSTRATE, id="acct", region="cn-northwest-1")
    assert backend.list_kbs(other) == []
    with pytest.raises(NotFoundError):
        backend.get_kb_detail(other, kb["kb_id"])


def test_without_a_substrate_list_is_empty_and_create_is_503(backend):
    bare = ws_ctx({"artifacts_bucket": BUCKET})
    assert backend.list_kbs(bare) == []
    with pytest.raises(AppError) as exc:
        backend.create_kb(bare, "x", "", {"mode": "upload"})
    assert exc.value.code == "kb.not_provisioned" and exc.value.status_code == 503


def test_table_is_scoped_and_migrated():
    from app.core.db import WORKSPACE_SCOPED_TABLES, engine, schema_drift

    assert "knowledge_bases" in WORKSPACE_SCOPED_TABLES and schema_drift(engine) == {}


def test_pdf_fixture_extracts_the_expected_text():
    reader = PdfReader(io.BytesIO(_pdf_bytes("Hello PDF")))
    assert reader.pages[0].extract_text() == "Hello PDF"


# --- N6 Task 3 fix round 1 ---
def test_delete_kb_degrades_gracefully_when_delete_index_fails(backend, ws, fakes, caplog):
    """Item 1: `delete_index` used to run unguarded after the ledger row was
    already deleted — a throttle/5xx there must not surface as an error on the
    delete the member asked for (mirrors the KB_DOCS_INDEX/JOBS_INDEX cleanup
    right below it, and `memory_selfhosted`'s own delete-cleanup pattern)."""
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})

    def boom(*args, **kwargs):
        raise core.OpenSearchError(500, "boom")

    fakes["opensearch"].delete_index = boom
    with caplog.at_level("WARNING", logger="launchpad.knowledge"):
        out = backend.delete_kb(ws, kb["kb_id"], force=True)
    # WB-008: still `deleted: true`; the failed step is reported `pending`, the warning
    # names it, and a retriable `kb_cleanup` job is filed for it (tests/test_kb_cleanup.py)
    assert out["kb_id"] == kb["kb_id"] and out["deleted"] is True
    assert out["cleanup"] == {"s3": "ok", "index": "pending", "docs": "ok"}
    assert out["cleanup_warnings"] == ["index: OpenSearchError: HTTP 500"]  # N17-obs99: no body
    assert out["cleanup_job_id"] is not None
    assert kb["kb_id"] in caplog.text and "500" in caplog.text
    with pytest.raises(NotFoundError) as exc:
        backend.get_kb_detail(ws, kb["kb_id"])
    assert exc.value.code == "kb.not_found"


def test_opensearch_errors_surface_as_503_kb_backend_unavailable_through_the_api(
    client, fakes,
):
    """Item 3: an uncaught `OpenSearchError` reaching a knowledge-base route must
    translate to `kb.backend_unavailable` (never `memory.backend_unavailable`) —
    the counterpart of `test_memory_selfhosted.py`'s
    `test_opensearch_errors_surface_as_503_backend_unavailable_through_the_api`,
    one test per feature prefix."""
    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    knowledge_backend.set_backend(backend)
    set_default_resources(SUBSTRATE)
    ws = ws_ctx(SUBSTRATE, region="cn-northwest-1")
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds_id = kb["data_sources"][0]["ds_id"]

    def boom_search(*args, **kwargs):
        raise core.OpenSearchError(503, "upstream unavailable: do not echo this body")

    fakes["opensearch"].search = boom_search
    res = client.get(f"/api/knowledge-bases/{kb['kb_id']}/data-sources/{ds_id}/ingestion-jobs")
    assert res.status_code == 503
    body = res.json()
    assert body["code"] == "kb.backend_unavailable"
    assert "do not echo this body" not in json.dumps(body)


def test_upload_rejects_an_oversized_file_before_any_s3_put(client, fakes):
    """Item 2: the per-file size cap runs before `upload_files` (and so before any
    `s3.put_object`) is ever called."""
    from app.schemas.attachments import MAX_FILE_BYTES

    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    knowledge_backend.set_backend(backend)
    set_default_resources(SUBSTRATE)
    ws = ws_ctx(SUBSTRATE, region="cn-northwest-1")
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    big = b"x" * (MAX_FILE_BYTES + 1)
    res = client.post(
        f"/api/knowledge-bases/{kb['kb_id']}/files",
        files={"files": ("big.txt", big, "text/plain")},
    )
    assert res.status_code == 413
    assert res.json()["code"] == "kb.upload_too_large"
    assert not fakes["s3"].objects


def test_upload_rejects_too_many_files(client, fakes):
    """Item 2: the file-count cap runs before any file is even read."""
    from app.schemas.attachments import MAX_FILES

    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    knowledge_backend.set_backend(backend)
    set_default_resources(SUBSTRATE)
    ws = ws_ctx(SUBSTRATE, region="cn-northwest-1")
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    files = [("files", (f"f{i}.txt", b"x", "text/plain")) for i in range(MAX_FILES + 1)]
    res = client.post(f"/api/knowledge-bases/{kb['kb_id']}/files", files=files)
    assert res.status_code == 422
    assert res.json()["code"] == "kb.too_many_files"
    assert not fakes["s3"].objects
# --- end N6 Task 3 fix round 1 ---


# --- N17-obs204 (cn/r17-aa): a successful upload starts the ingestion sync server-side ---


def _upload_backend(fakes, now):
    """A backend on a settable clock (`now["t"]`) so a job can pre-date an upload."""
    return SelfHostedKnowledgeBackend(**fakes, clock=lambda: now["t"])


def test_upload_starts_one_sync_after_every_put_landed(backend, ws, fakes):
    """Every put done → the SAME path as `start_sync`: one STARTING job document, one SQS
    message naming the upload source's prefix, the job in the response (fresh, covering)."""
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]
    out = backend.upload_files(ws, kb["kb_id"], [("a.txt", b"a"), ("b.txt", b"b")])
    job = out["job"]
    assert job["status"] == "STARTING" and job["job_id"].startswith("ij")
    assert job["coalesced"] is False and job["covers_upload"] is True
    (url, msg), = fakes["sqs"].sent
    assert url == SUBSTRATE["memory_queue_url"]
    assert msg["kind"] == "kb_ingest" and msg["job_id"] == job["job_id"]
    assert msg["prefix"] == ds["prefix"] and msg["ds_id"] == ds["ds_id"]
    # the objects were there BEFORE the message went out (the Lambda lists on pickup)
    assert {k for (_, k) in fakes["s3"].objects} == {f"{ds['prefix']}a.txt", f"{ds['prefix']}b.txt"}
    doc = fakes["opensearch"].get_doc(core.jobs_index(""), job["job_id"])
    assert doc["status"] == "STARTING" and doc["ds_id"] == ds["ds_id"]
    # the detail sees it on the next poll (cache invalidated) and no auto first sync is due
    detail = backend.get_kb_detail(ws, kb["kb_id"])
    jobs = detail["data_sources"][0]["ingestion_jobs"]
    assert [j["job_id"] for j in jobs] == [job["job_id"]]
    assert detail["data_sources"][0]["has_objects"] is True


def test_upload_coalesces_onto_a_running_job_and_says_whether_it_covers_the_files(fakes):
    """A job of the source still STARTING / IN_PROGRESS is returned instead of a second one
    being enqueued. `covers_upload` is true only for a job created at or after the upload
    began: one that started before it may already have listed the prefix."""
    from datetime import timedelta

    now = {"t": T0}
    backend = _upload_backend(fakes, now)
    ws = ws_ctx(SUBSTRATE, region="cn-northwest-1")
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]
    earlier = backend.start_sync(ws, kb["kb_id"], ds["ds_id"])  # created at T0
    assert len(fakes["sqs"].sent) == 1

    now["t"] = T0 + timedelta(seconds=30)
    out = backend.upload_files(ws, kb["kb_id"], [("a.txt", b"a")])
    assert out["job"] == {
        "job_id": earlier["job_id"], "status": "STARTING",
        "coalesced": True, "covers_upload": False,
    }
    assert len(fakes["sqs"].sent) == 1  # no second message

    # the Lambda picked it up meanwhile: IN_PROGRESS still coalesces
    jobs_index = core.jobs_index("")
    fakes["opensearch"].update_doc(jobs_index, earlier["job_id"], {"status": "IN_PROGRESS"})
    out = backend.upload_files(ws, kb["kb_id"], [("b.txt", b"b")])
    assert out["job"]["coalesced"] is True and out["job"]["status"] == "IN_PROGRESS"
    assert out["job"]["covers_upload"] is False
    assert len(fakes["sqs"].sent) == 1

    # a job created at the very instant the upload began counts as covering it
    fakes["opensearch"].update_doc(core.jobs_index(""), earlier["job_id"], {"status": "COMPLETE"})
    same_tick = backend.start_sync(ws, kb["kb_id"], ds["ds_id"])  # created at T0+30
    out = backend.upload_files(ws, kb["kb_id"], [("c.txt", b"c")])
    assert out["job"]["job_id"] == same_tick["job_id"]
    assert out["job"]["coalesced"] is True and out["job"]["covers_upload"] is True

    # a finished job never coalesces — the next upload starts a fresh sync
    fakes["opensearch"].update_doc(core.jobs_index(""), same_tick["job_id"], {"status": "COMPLETE"})
    out = backend.upload_files(ws, kb["kb_id"], [("d.txt", b"d")])
    assert out["job"]["coalesced"] is False and out["job"]["job_id"] != same_tick["job_id"]


def test_upload_still_succeeds_when_the_sync_cannot_start(backend, ws, fakes, caplog):
    """The files are stored, the keys answered, `job` is None and one WARNING names the
    failure type (never a body) — the caller syncs by hand."""
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]

    def boom(*a, **k):
        raise core.OpenSearchError(503, "jobs index unavailable: do not echo this body")

    fakes["opensearch"].search = boom
    with caplog.at_level("WARNING", logger="launchpad.knowledge"):
        out = backend.upload_files(ws, kb["kb_id"], [("a.txt", b"a")])
    assert out == {"keys": [f"{ds['prefix']}a.txt"], "job": None, "sync_error": "OpenSearchError"}
    assert fakes["sqs"].sent == []
    assert (SUBSTRATE["artifacts_bucket"], f"{ds['prefix']}a.txt") in fakes["s3"].objects
    [rec] = [r for r in caplog.records if "sync after upload" in r.getMessage()]
    assert "OpenSearchError" in rec.getMessage() and "do not echo" not in rec.getMessage()


def test_upload_route_answers_keys_and_the_job(client, fakes):
    """`POST /api/knowledge-bases/{kb_id}/files` → `{keys, job}`."""
    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    knowledge_backend.set_backend(backend)
    set_default_resources(SUBSTRATE)
    ws = ws_ctx(SUBSTRATE, region="cn-northwest-1")
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    res = client.post(
        f"/api/knowledge-bases/{kb['kb_id']}/files",
        files=[("files", ("a.txt", b"a", "text/plain")), ("files", ("b.txt", b"b", "text/plain"))],
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["keys"]) == 2
    assert body["job"]["status"] == "STARTING"
    assert body["job"]["coalesced"] is False and body["job"]["covers_upload"] is True
    assert len(fakes["sqs"].sent) == 1
    detail = client.get(f"/api/knowledge-bases/{kb['kb_id']}").json()
    assert detail["data_sources"][0]["has_objects"] is True
    assert detail["data_sources"][0]["ingestion_jobs"][0]["job_id"] == body["job"]["job_id"]


def test_detail_has_objects_ignores_sidecars_and_degrades_to_null(backend, ws, fakes):
    """`has_objects`: false on a fresh source, false when only `_text/` sidecars sit under
    the prefix, true once any other object does; a `ClientError` on the listing answers
    null and the detail still 200s."""
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]
    assert backend.get_kb_detail(ws, kb["kb_id"])["data_sources"][0]["has_objects"] is False

    fakes["s3"].put_object(Bucket=BUCKET, Key=f"{ds['prefix']}_text/old.pdf.txt", Body=b"t")
    assert backend.get_kb_detail(ws, kb["kb_id"])["data_sources"][0]["has_objects"] is False

    fakes["s3"].put_object(Bucket=BUCKET, Key=f"{ds['prefix']}old.pdf", Body=b"%PDF")
    assert backend.get_kb_detail(ws, kb["kb_id"])["data_sources"][0]["has_objects"] is True

    from botocore.exceptions import ClientError

    def denied(**kw):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "ListObjectsV2")

    fakes["s3"].list_objects_v2 = denied
    detail = backend.get_kb_detail(ws, kb["kb_id"])
    assert detail["data_sources"][0]["has_objects"] is None
    assert detail["status"] == "ACTIVE"


def test_detail_has_objects_pages_past_a_sidecar_only_first_page(backend, ws, fakes, monkeypatch):
    """A first page made only of sidecars is not the answer: the scan continues to the
    next page (bounded by the sidecar count) and finds the original behind them."""
    from app.services import knowledge_selfhosted

    monkeypatch.setattr(knowledge_selfhosted, "HAS_OBJECTS_PAGE", 2)
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]
    for i in range(3):
        fakes["s3"].put_object(Bucket=BUCKET, Key=f"{ds['prefix']}_text/p{i}.pdf.txt", Body=b"t")
    assert backend.get_kb_detail(ws, kb["kb_id"])["data_sources"][0]["has_objects"] is False
    fakes["s3"].put_object(Bucket=BUCKET, Key=f"{ds['prefix']}zed.pdf", Body=b"%PDF")  # sorts last
    assert backend.get_kb_detail(ws, kb["kb_id"])["data_sources"][0]["has_objects"] is True


def test_upload_after_an_sqs_failure_reports_the_error_type_and_keeps_the_files(
    backend, ws, fakes, caplog,
):
    """rev-r19 nit 2 (first half): SQS fails AFTER the STARTING job doc was written — the
    files stay stored, the route answers `job: None` + `sync_error` (type only), one WARNING."""
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]

    class SqsDown(Exception):
        pass

    def boom(**k):
        raise SqsDown("queue unreachable: do not echo this body")

    fakes["sqs"].send_message = boom
    with caplog.at_level("WARNING", logger="launchpad.knowledge"):
        out = backend.upload_files(ws, kb["kb_id"], [("a.txt", b"a")])
    assert out["job"] is None and out["sync_error"] == "SqsDown"
    assert (SUBSTRATE["artifacts_bucket"], f"{ds['prefix']}a.txt") in fakes["s3"].objects
    assert all("do not echo" not in r.getMessage() for r in caplog.records)


def test_upload_ignores_a_stale_running_job_and_starts_a_fresh_sync(ws, fakes, caplog):
    """rev-r19 nit 2 (second half): a STARTING/IN_PROGRESS job older than
    RUNNING_JOB_STALE_S is an orphan — it never coalesces, a new sync is enqueued."""
    from datetime import timedelta

    from app.services import knowledge_selfhosted as ks

    clock = {"now": T0}
    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: clock["now"])
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    first = backend.upload_files(ws, kb["kb_id"], [("a.txt", b"a")])
    assert first["job"]["coalesced"] is False
    # the job is left STARTING forever (SQS message lost); the clock moves past the bound
    clock["now"] = T0 + timedelta(seconds=ks.RUNNING_JOB_STALE_S + 60)
    with caplog.at_level("WARNING", logger="launchpad.knowledge"):
        second = backend.upload_files(ws, kb["kb_id"], [("b.txt", b"b")])
    assert second["job"]["coalesced"] is False and second["job"]["covers_upload"] is True
    assert second["job"]["job_id"] != first["job"]["job_id"]
    assert any("ignoring stale running job" in r.getMessage() for r in caplog.records)
    # within the bound the same orphan still coalesces (conservative)
    clock["now"] = T0 + timedelta(seconds=60)
    third = backend.upload_files(ws, kb["kb_id"], [("c.txt", b"c")])
    assert third["job"]["coalesced"] is True


# --- N17-obs236 (cn/r17-ae): a sync never leaves an orphan STARTING job behind ---


def _read_timeout():
    import urllib3

    return urllib3.exceptions.ReadTimeoutError(None, "/launchpad-jobs/_doc/x", "Read timed out")


def test_sync_goes_on_when_the_job_put_timed_out_but_the_document_is_there(
    backend, ws, fakes, caplog,
):
    """Live shape (default host, 2026-10-21 07:47:49Z): the job PUT hit the 10 s interactive
    read timeout twice although the FIRST PUT had been applied — the hub answered 502 and
    never sent the SQS message, leaving a STARTING job forever. Now: one confirming GET finds
    the document → the sync continues, exactly one SQS message, a normal `{job_id, STARTING}`."""
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]
    os_ = fakes["opensearch"]
    real_put = os_.put_doc

    def late_ack_put(index, doc_id, doc, refresh=True):
        real_put(index, doc_id, doc, refresh)  # applied server-side …
        raise _read_timeout()  # … but the ack never came back

    os_.put_doc = late_ack_put
    with caplog.at_level("WARNING", logger="launchpad.knowledge"):
        out = backend.start_sync(ws, kb["kb_id"], ds["ds_id"])
    assert out["status"] == "STARTING"
    assert len(fakes["sqs"].sent) == 1 and fakes["sqs"].sent[0][1]["job_id"] == out["job_id"]
    doc = os_.get_doc(core.jobs_index(""), out["job_id"])
    assert doc["status"] == "STARTING" and doc["counts"] == {}
    assert any("write was applied, the sync goes on" in r.getMessage() for r in caplog.records)
    # the detail shows the job as STARTING, not degraded
    detail = backend.get_kb_detail(ws, kb["kb_id"])
    assert detail["data_sources"][0]["ingestion_jobs"][0]["status"] == "STARTING"


def test_sync_fails_as_before_when_the_job_put_timed_out_and_nothing_was_written(
    backend, ws, fakes,
):
    """The PUT really was lost: the confirming GET finds nothing → the original transport
    error propagates (the router maps it to 502 `kb.unavailable`) and NO SQS message is
    sent — a job the console cannot see must not be running."""
    import urllib3

    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]

    def lost_put(index, doc_id, doc, refresh=True):
        raise _read_timeout()

    fakes["opensearch"].put_doc = lost_put
    with pytest.raises(urllib3.exceptions.ReadTimeoutError):
        backend.start_sync(ws, kb["kb_id"], ds["ds_id"])
    assert fakes["sqs"].sent == []
    assert fakes["opensearch"].indices[core.jobs_index("")] == {}


def test_sync_reraises_when_the_confirming_get_fails_too(backend, ws, fakes, caplog):
    """Both the PUT and the GET time out: nothing is known to be written, so the ORIGINAL
    error propagates and SQS is not called."""
    import urllib3

    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]

    def lost_put(index, doc_id, doc, refresh=True):
        raise _read_timeout()

    def dead_get(index, doc_id):
        raise urllib3.exceptions.ReadTimeoutError(None, "/x", "still busy")

    fakes["opensearch"].put_doc = lost_put
    fakes["opensearch"].get_doc = dead_get
    with caplog.at_level("WARNING", logger="launchpad.knowledge"), pytest.raises(
        urllib3.exceptions.ReadTimeoutError, match="Read timed out",
    ):
        backend.start_sync(ws, kb["kb_id"], ds["ds_id"])
    assert fakes["sqs"].sent == []
    assert any("confirming GET failed too" in r.getMessage() for r in caplog.records)


def test_sync_marks_the_job_failed_when_sqs_fails_after_the_job_was_written(
    backend, ws, fakes,
):
    """`send_message` raises after the STARTING document exists: the document is marked
    `FAILED` / `enqueue_failed: <class>` (best-effort) and the error still propagates — the
    console sees a failed sync, never a job that is STARTING forever."""
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]

    class SqsDown(Exception):
        pass

    def boom(**k):
        raise SqsDown("queue unreachable: do not echo this body")

    fakes["sqs"].send_message = boom
    with pytest.raises(SqsDown):
        backend.start_sync(ws, kb["kb_id"], ds["ds_id"])
    (doc,) = fakes["opensearch"].indices[core.jobs_index("")].values()
    assert doc["status"] == "FAILED" and doc["failure_reason"] == "enqueue_failed: SqsDown"
    jobs = backend.list_ingestion_jobs(ws, kb["kb_id"], ds["ds_id"])
    assert jobs[0]["status"] == "FAILED"
    assert jobs[0]["failure_reasons"] == ["enqueue_failed: SqsDown"]
    # the detail's cache was dropped after the mark: the poll sees FAILED, not STARTING
    detail = backend.get_kb_detail(ws, kb["kb_id"])
    assert detail["data_sources"][0]["ingestion_jobs"][0]["status"] == "FAILED"


def test_sqs_failure_still_propagates_when_the_failed_mark_cannot_be_written(
    backend, ws, fakes, caplog,
):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]

    class SqsDown(Exception):
        pass

    def boom(**k):
        raise SqsDown("queue unreachable")

    def no_update(index, doc_id, fields, refresh=True):
        raise _read_timeout()

    fakes["sqs"].send_message = boom
    fakes["opensearch"].update_doc = no_update
    with caplog.at_level("WARNING", logger="launchpad.knowledge"), pytest.raises(SqsDown):
        backend.start_sync(ws, kb["kb_id"], ds["ds_id"])
    assert any("could not mark the undispatched job FAILED" in r.getMessage()
               for r in caplog.records)


def _seed_job(fakes, kb_id, ds_id, job_id, status, created_at, counts=None):
    fakes["opensearch"].put_doc(core.jobs_index(""), job_id, {
        "job_id": job_id, "kind": "kb_ingest", "kb_id": kb_id, "ds_id": ds_id,
        "workspace_id": "default", "status": status, "counts": counts or {},
        "created_at": created_at.isoformat(), "updated_at": created_at.isoformat(),
    })


def test_a_stale_starting_job_is_listed_and_persisted_as_failed_never_dispatched(
    ws, fakes, caplog,
):
    """The reaper: a job still STARTING with empty counts 31 min after `created_at` was
    never picked up (the Lambda's first write is IN_PROGRESS) → both reads present it as
    FAILED / never_dispatched and the document is updated; a 5-min-old STARTING job, a
    STARTING job that carries counts and an old IN_PROGRESS job are untouched."""
    from datetime import timedelta

    from app.services import knowledge_selfhosted as ks

    clock = {"now": T0}
    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: clock["now"])
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    kb_id, ds_id = kb["kb_id"], kb["data_sources"][0]["ds_id"]
    stale = T0 - timedelta(seconds=ks.RUNNING_JOB_STALE_S + 60)  # 31 min old
    _seed_job(fakes, kb_id, ds_id, "ij-orphan", "STARTING", stale)
    _seed_job(fakes, kb_id, ds_id, "ij-fresh", "STARTING", T0 - timedelta(minutes=5))
    _seed_job(fakes, kb_id, ds_id, "ij-counted", "STARTING", stale,
              counts={"numberOfDocumentsScanned": 1})
    _seed_job(fakes, kb_id, ds_id, "ij-running", "IN_PROGRESS", stale)  # silent 31 min
    _seed_job(fakes, kb_id, ds_id, "ij-busy", "IN_PROGRESS", T0 - timedelta(minutes=5))
    with caplog.at_level("WARNING", logger="launchpad.knowledge"):
        jobs = {j["job_id"]: j for j in backend.list_ingestion_jobs(ws, kb_id, ds_id)}
    assert jobs["ij-orphan"]["status"] == "FAILED"
    assert jobs["ij-orphan"]["failure_reasons"] == [ks.NEVER_DISPATCHED_REASON]
    assert jobs["ij-orphan"]["updated_at"] == T0.isoformat()
    assert jobs["ij-fresh"]["status"] == "STARTING" and jobs["ij-fresh"]["failure_reasons"] == []
    assert jobs["ij-counted"]["status"] == "STARTING"
    # rev-r22 (b) on N17-obs247: an IN_PROGRESS job whose `updated_at` has not moved for the
    # same bound is stalled (the planner died before its final write); a busy one is left alone
    assert jobs["ij-running"]["status"] == "FAILED"
    assert jobs["ij-running"]["failure_reasons"] == [ks.STALLED_REASON]
    assert jobs["ij-busy"]["status"] == "IN_PROGRESS" and jobs["ij-busy"]["failure_reasons"] == []
    docs = fakes["opensearch"].indices[core.jobs_index("")]
    assert docs["ij-orphan"]["status"] == "FAILED"
    assert docs["ij-orphan"]["failure_reason"] == ks.NEVER_DISPATCHED_REASON
    assert {docs[i]["status"] for i in ("ij-fresh", "ij-counted")} == {"STARTING"}
    assert docs["ij-running"]["status"] == "FAILED"
    assert docs["ij-running"]["failure_reason"] == ks.STALLED_REASON
    assert docs["ij-busy"]["status"] == "IN_PROGRESS"
    assert sum("never dispatched, marked FAILED" in r.getMessage() for r in caplog.records) == 1
    assert sum("stalled, marked FAILED" in r.getMessage() for r in caplog.records) == 1
    # the detail path (cached) presents the same, and a second read reaps nothing more
    ks.reset_jobs_cache()  # the jobs were seeded behind the hub's back (create cached `[]`)
    detail = backend.get_kb_detail(ws, kb_id)
    by_id = {j["job_id"]: j for j in detail["data_sources"][0]["ingestion_jobs"]}
    assert by_id["ij-orphan"]["status"] == "FAILED" and by_id["ij-fresh"]["status"] == "STARTING"
    assert sum("never dispatched, marked FAILED" in r.getMessage() for r in caplog.records) == 1


def test_the_reaper_presents_failed_even_when_the_persist_fails(ws, fakes, caplog):
    """A busy node refuses the `_update`: the read still answers (the job is presented as
    FAILED), one WARNING, and the next read retries the persist."""
    from datetime import timedelta

    from app.services import knowledge_selfhosted as ks

    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    kb_id, ds_id = kb["kb_id"], kb["data_sources"][0]["ds_id"]
    _seed_job(fakes, kb_id, ds_id, "ij-orphan", "STARTING",
              T0 - timedelta(seconds=ks.RUNNING_JOB_STALE_S + 1))

    def no_update(index, doc_id, fields, refresh=True):
        raise _read_timeout()

    fakes["opensearch"].update_doc = no_update
    with caplog.at_level("WARNING", logger="launchpad.knowledge"):
        (job,) = backend.list_ingestion_jobs(ws, kb_id, ds_id)
    assert job["status"] == "FAILED" and job["failure_reasons"] == [ks.NEVER_DISPATCHED_REASON]
    assert fakes["opensearch"].indices[core.jobs_index("")]["ij-orphan"]["status"] == "STARTING"
    assert any("could not persist the never_dispatched" in r.getMessage() for r in caplog.records)


def test_a_job_picked_up_after_the_reap_shows_no_lingering_failure_reason(ws, fakes):
    """rev-r21 nit 4: the SQS message arrives AFTER the 30-min reap — the Lambda flips the
    document FAILED → IN_PROGRESS but `failure_reason` lingers until its terminal write
    clears it. Both reads list the job IN_PROGRESS with `failure_reasons: []`; a terminal
    FAILED keeps its reason."""
    from datetime import timedelta

    from app.services import knowledge_selfhosted as ks

    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    kb_id, ds_id = kb["kb_id"], kb["data_sources"][0]["ds_id"]
    _seed_job(fakes, kb_id, ds_id, "ij-late", "STARTING",
              T0 - timedelta(seconds=ks.RUNNING_JOB_STALE_S + 1))
    (reaped,) = backend.list_ingestion_jobs(ws, kb_id, ds_id)
    assert reaped["status"] == "FAILED"
    assert reaped["failure_reasons"] == [ks.NEVER_DISPATCHED_REASON]
    # the Lambda's late pickup: IN_PROGRESS with the stale reason still on the document
    fakes["opensearch"].update_doc(core.jobs_index(""), "ij-late", {"status": "IN_PROGRESS"})
    assert fakes["opensearch"].indices[core.jobs_index("")]["ij-late"]["failure_reason"]
    (job,) = backend.list_ingestion_jobs(ws, kb_id, ds_id)
    assert job["status"] == "IN_PROGRESS" and job["failure_reasons"] == []
    ks.reset_jobs_cache()
    (via_detail,) = backend.get_kb_detail(ws, kb_id)["data_sources"][0]["ingestion_jobs"]
    assert via_detail["status"] == "IN_PROGRESS" and via_detail["failure_reasons"] == []
    # a terminal FAILED keeps its reason
    fakes["opensearch"].update_doc(core.jobs_index(""), "ij-late",
                                   {"status": "FAILED", "failure_reason": "boom"})
    (failed,) = backend.list_ingestion_jobs(ws, kb_id, ds_id)
    assert failed["failure_reasons"] == ["boom"]


def test_the_reaper_persists_through_the_confirm_tier(ws, fakes):
    """rev-r21 nit 3: the reaper's `_update` goes through the one-attempt / 5 s client
    (`_os_confirm`), not the 2 × 10 s interactive one — the detail's single-flight rebuild
    cannot be held ~21 s by a busy node. Asserted with distinct injected clients."""
    from datetime import timedelta

    from app.services import knowledge_selfhosted as ks

    confirm = FakeOpenSearch()
    confirm.indices = fakes["opensearch"].indices  # same store, different client object
    calls: list[str] = []
    real_update = confirm.update_doc

    def counted_update(index, doc_id, fields, refresh=True):
        calls.append(doc_id)
        return real_update(index, doc_id, fields, refresh)

    confirm.update_doc = counted_update
    fakes["opensearch"].update_doc = lambda *a, **k: pytest.fail("interactive client used")
    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    backend._os_confirm = lambda ws_: confirm  # the funnel would build the confirm tier
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    kb_id, ds_id = kb["kb_id"], kb["data_sources"][0]["ds_id"]
    _seed_job(fakes, kb_id, ds_id, "ij-orphan", "STARTING",
              T0 - timedelta(seconds=ks.RUNNING_JOB_STALE_S + 1))
    (job,) = backend.list_ingestion_jobs(ws, kb_id, ds_id)
    assert job["status"] == "FAILED" and calls == ["ij-orphan"]
    assert fakes["opensearch"].indices[core.jobs_index("")]["ij-orphan"]["status"] == "FAILED"


def test_an_unparsable_or_naive_created_at_is_never_reaped(ws, fakes):
    """rev-r21 nit 5: `fromisoformat` raising ValueError (garbage) or the subtraction raising
    TypeError (a naive stamp against the aware hub clock) means "age unknown" — the job is
    left alone by the reaper (and by `_running_job`'s stale rule, which coalesces onto it)."""
    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    kb_id, ds_id = kb["kb_id"], kb["data_sources"][0]["ds_id"]
    for job_id, stamp in (("ij-garbage", "not-a-date"), ("ij-naive", "2020-01-01T00:00:00")):
        fakes["opensearch"].put_doc(core.jobs_index(""), job_id, {
            "job_id": job_id, "kind": "kb_ingest", "kb_id": kb_id, "ds_id": ds_id,
            "workspace_id": "default", "status": "STARTING", "counts": {},
            "created_at": stamp, "updated_at": stamp,
        })
    jobs = {j["job_id"]: j for j in backend.list_ingestion_jobs(ws, kb_id, ds_id)}
    assert {jobs["ij-garbage"]["status"], jobs["ij-naive"]["status"]} == {"STARTING"}
    assert backend._running_job(ws, kb_id, ds_id) is not None


def test_the_confirming_get_uses_a_one_attempt_short_timeout_client(ws, monkeypatch):
    """The confirmation after a timed-out PUT is ONE attempt with a 5 s read timeout (the
    request has already spent its 2 × 10 s interactive budget) — asked of the funnel, which
    is the only construction site."""
    from app.services import aws_clients
    from app.services import knowledge_selfhosted as ks

    seen: list[dict] = []
    monkeypatch.setattr(
        aws_clients, "opensearch_http", lambda ctx, endpoint, **kw: seen.append(kw) or object(),
    )
    SelfHostedKnowledgeBackend()._os_confirm(ws)
    assert seen == [{"attempts": 1, "read_timeout": ks.JOB_CONFIRM_READ_TIMEOUT_SECONDS}]
    assert ks.JOB_CONFIRM_READ_TIMEOUT_SECONDS == 5.0


def test_sync_route_answers_502_when_the_job_put_was_really_lost(client, fakes):
    """Through the router: the lost-PUT case is still 502 `kb.unavailable` (unchanged)."""
    import urllib3

    fakes["opensearch"].ensure_index(core.jobs_index(""), core.jobs_index_body())
    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    knowledge_backend.set_backend(backend)
    set_default_resources(SUBSTRATE)
    ws = ws_ctx(SUBSTRATE, region="cn-northwest-1")
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds_id = kb["data_sources"][0]["ds_id"]

    def lost_put(index, doc_id, doc, refresh=True):
        raise urllib3.exceptions.ReadTimeoutError(None, "/x", "Read timed out")

    fakes["opensearch"].put_doc = lost_put
    res = client.post(f"/api/knowledge-bases/{kb['kb_id']}/data-sources/{ds_id}/sync")
    assert res.status_code == 502 and res.json()["code"] == "kb.unavailable"
    assert fakes["sqs"].sent == []


# --- N17-obs242 (cn/r17-ag): an upload recreates a missing upload-mode data source ---


def _kb_without_sources(backend, ws, name="docs"):
    """A KB whose only (upload) source was removed — the user's state on 2026-10-21."""
    kb = backend.create_kb(ws, name, "", {"mode": "upload"})
    backend.delete_data_source(ws, kb["kb_id"], kb["data_sources"][0]["ds_id"])
    assert backend.get_kb_detail(ws, kb["kb_id"])["data_sources"] == []
    return kb["kb_id"]


def test_upload_on_a_kb_without_an_upload_source_creates_one(backend, ws, fakes):
    """0 sources → the upload creates the upload source through the `add_data_source` path
    (same id format, `kb/<kb_id>/<ds_id>/` prefix), stores the files under THAT prefix,
    starts one sync on it and names the source in the response."""
    kb_id = _kb_without_sources(backend, ws)
    out = backend.upload_files(ws, kb_id, [("a.txt", b"a"), ("b.txt", b"b")])
    ds = out["data_source"]
    assert ds["mode"] == "upload" and ds["ds_id"].startswith("DS") and ds["name"] == ds["ds_id"]
    assert ds["prefix"] == kb_prefix(kb_id, ds["ds_id"]) and ds["bucket"] == BUCKET
    assert ds["status"] == "AVAILABLE" and ds["created_at"] == T0.isoformat()
    assert out["keys"] == [f"{ds['prefix']}a.txt", f"{ds['prefix']}b.txt"]
    assert {k for (_, k) in fakes["s3"].objects} == set(out["keys"])
    job = out["job"]
    assert job["status"] == "STARTING" and job["coalesced"] is False
    (url, msg), = fakes["sqs"].sent
    assert msg["ds_id"] == ds["ds_id"] and msg["prefix"] == ds["prefix"]
    # the ledger holds exactly that source, and the detail shows it with its first job
    detail = backend.get_kb_detail(ws, kb_id)
    [shown] = detail["data_sources"]
    assert shown["ds_id"] == ds["ds_id"] and shown["mode"] == "upload"
    assert [j["job_id"] for j in shown["ingestion_jobs"]] == [job["job_id"]]
    assert shown["has_objects"] is True
    # a second upload reuses it: no second source, no `data_source` in the response
    again = backend.upload_files(ws, kb_id, [("c.txt", b"c")])
    assert "data_source" not in again
    assert again["keys"] == [f"{ds['prefix']}c.txt"]
    assert len(backend.get_kb_detail(ws, kb_id)["data_sources"]) == 1


def test_upload_with_an_upload_source_is_unchanged_and_adds_no_second_source(backend, ws):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]
    out = backend.upload_files(ws, kb["kb_id"], [("a.txt", b"a")])
    assert set(out) == {"keys", "job"}
    assert out["keys"] == [f"{ds['prefix']}a.txt"]
    detail = backend.get_kb_detail(ws, kb["kb_id"])
    assert [d["ds_id"] for d in detail["data_sources"]] == [ds["ds_id"]]


def test_upload_on_a_kb_with_only_an_existing_source_adds_an_upload_source(backend, ws):
    """An `existing`-mode source is never the upload target (its prefix is the member's
    own location); the upload adds an upload source next to it."""
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    kb_id = kb["kb_id"]
    manual = backend.add_data_source(
        ws, kb_id, {"mode": "existing", "bucket": BUCKET, "prefix": f"kb/{kb_id}/manual/"},
    )
    backend.delete_data_source(ws, kb_id, kb["data_sources"][0]["ds_id"])
    out = backend.upload_files(ws, kb_id, [("a.txt", b"a")])
    created = out["data_source"]
    assert created["mode"] == "upload" and created["ds_id"] != manual["ds_id"]
    assert out["keys"] == [f"{created['prefix']}a.txt"]
    modes = {d["ds_id"]: d["mode"] for d in backend.get_kb_detail(ws, kb_id)["data_sources"]}
    assert modes == {manual["ds_id"]: "existing", created["ds_id"]: "upload"}


def test_two_concurrent_first_uploads_create_one_upload_source(backend, ws, fakes):
    """Both uploads read "no upload source" before either creates one (a barrier holds
    them there); the per-KB lock makes the second re-read the row and reuse the first's
    source — one source, both files under its prefix, `data_source` reported once."""
    import threading

    kb_id = _kb_without_sources(backend, ws)
    barrier = threading.Barrier(2, timeout=10)
    state = threading.local()
    original_row = backend._row

    def row_then_meet(ws_, kb_id_):
        row = original_row(ws_, kb_id_)
        if not getattr(state, "met", False):  # the first, un-locked read of each thread
            state.met = True
            barrier.wait()
        return row

    backend._row = row_then_meet
    results: dict[str, Any] = {}

    def upload(name):
        results[name] = backend.upload_files(ws, kb_id, [(f"{name}.txt", name.encode())])

    threads = [threading.Thread(target=upload, args=(n,)) for n in ("one", "two")]
    try:
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
    finally:
        backend._row = original_row  # the main thread's reads below never meet anyone
    assert not any(th.is_alive() for th in threads)
    assert set(results) == {"one", "two"}
    [source] = backend.get_kb_detail(ws, kb_id)["data_sources"]
    assert source["mode"] == "upload"
    created = [r["data_source"] for r in results.values() if "data_source" in r]
    assert len(created) == 1 and created[0]["ds_id"] == source["ds_id"]
    assert {k for (_, k) in fakes["s3"].objects} == {
        f"{source['prefix']}one.txt", f"{source['prefix']}two.txt",
    }
    assert all(r["job"] is not None for r in results.values())


def test_upload_route_recreates_the_upload_source_and_answers_it(client, fakes):
    """`POST /api/knowledge-bases/{kb_id}/files` on a KB with 0 sources → 200 with
    `data_source`; the detail lists the new source with the upload's job (the row the
    console shows after its refetch)."""
    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    knowledge_backend.set_backend(backend)
    set_default_resources(SUBSTRATE)
    ws = ws_ctx(SUBSTRATE, region="cn-northwest-1")
    kb_id = _kb_without_sources(backend, ws)
    res = client.post(
        f"/api/knowledge-bases/{kb_id}/files",
        files=[("files", ("a.txt", b"a", "text/plain"))],
    )
    assert res.status_code == 200, res.text
    body = res.json()
    ds = body["data_source"]
    assert ds["mode"] == "upload" and ds["prefix"] == f"kb/{kb_id}/{ds['ds_id']}/"
    assert body["keys"] == [f"{ds['prefix']}a.txt"]
    assert body["job"]["status"] == "STARTING"
    detail = client.get(f"/api/knowledge-bases/{kb_id}").json()
    [shown] = detail["data_sources"]
    assert shown["ds_id"] == ds["ds_id"]
    assert shown["ingestion_jobs"][0]["job_id"] == body["job"]["job_id"]
    assert shown["has_objects"] is True


# ── N17-obs243: `POST …/data-sources {mode: upload}` is idempotent per KB ───────────────
def test_repair_route_creates_one_upload_source_then_returns_it(client, fakes):
    """Live 2026-10-22: seven clicks on 补建数据源 for KB `test` (0 sources) answered
    seven 201s and left seven empty upload sources. Now the first call creates the
    source (201, `created: true`) and every repeat answers 200 with THAT source."""
    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    knowledge_backend.set_backend(backend)
    set_default_resources(SUBSTRATE)
    ws = ws_ctx(SUBSTRATE, region="cn-northwest-1")
    kb_id = _kb_without_sources(backend, ws)
    url = f"/api/knowledge-bases/{kb_id}/data-sources"
    first = client.post(url, json={"mode": "upload"})
    assert first.status_code == 201, first.text
    made = first.json()
    assert made["created"] is True and made["mode"] == "upload"
    assert made["prefix"] == kb_prefix(kb_id, made["ds_id"])
    for _ in range(6):
        again = client.post(url, json={"mode": "upload"})
        assert again.status_code == 200, again.text
        assert again.json()["ds_id"] == made["ds_id"] and again.json()["created"] is False
    detail = client.get(f"/api/knowledge-bases/{kb_id}").json()
    assert [d["ds_id"] for d in detail["data_sources"]] == [made["ds_id"]]
    # the ledger record carries no `created` flag — it is a response-only field
    assert "created" not in backend.get_kb_detail(ws, kb_id)["data_sources"][0]


def test_add_upload_source_on_a_kb_that_has_one_returns_it(backend, ws):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    out = backend.add_data_source(ws, kb["kb_id"], {"mode": "upload"})
    assert out["ds_id"] == kb["data_sources"][0]["ds_id"] and out["created"] is False
    assert len(backend.get_kb_detail(ws, kb["kb_id"])["data_sources"]) == 1


def test_existing_mode_sources_still_append(backend, ws):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    kb_id = kb["kb_id"]
    a = backend.add_data_source(
        ws, kb_id, {"mode": "existing", "bucket": BUCKET, "prefix": f"kb/{kb_id}/a/"},
    )
    b = backend.add_data_source(
        ws, kb_id, {"mode": "existing", "bucket": BUCKET, "prefix": f"kb/{kb_id}/a/"},
    )
    assert a["created"] is True and b["created"] is True and a["ds_id"] != b["ds_id"]
    modes = [d["mode"] for d in backend.get_kb_detail(ws, kb_id)["data_sources"]]
    assert sorted(modes) == ["existing", "existing", "upload"]


def test_existing_mode_route_still_answers_201(client, fakes):
    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    knowledge_backend.set_backend(backend)
    set_default_resources(SUBSTRATE)
    ws = ws_ctx(SUBSTRATE, region="cn-northwest-1")
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    res = client.post(
        f"/api/knowledge-bases/{kb['kb_id']}/data-sources",
        json={"mode": "existing", "bucket": BUCKET, "prefix": f"kb/{kb['kb_id']}/x/"},
    )
    assert res.status_code == 201, res.text
    assert res.json()["created"] is True and res.json()["mode"] == "existing"


def test_concurrent_repair_clicks_create_one_upload_source(backend, ws):
    """Both calls read "no upload source" before either creates one (a barrier holds them
    there); the per-KB lock makes the second re-read the row and return the first's
    source — one source, exactly one `created: true`."""
    import threading

    kb_id = _kb_without_sources(backend, ws)
    barrier = threading.Barrier(2, timeout=10)
    state = threading.local()
    original_row = backend._row

    def row_then_meet(ws_, kb_id_):
        row = original_row(ws_, kb_id_)
        if not getattr(state, "met", False):
            state.met = True
            barrier.wait()
        return row

    backend._row = row_then_meet
    results: list[dict[str, Any]] = []
    lock = threading.Lock()

    def click():
        out = backend.add_data_source(ws, kb_id, {"mode": "upload"})
        with lock:
            results.append(out)

    threads = [threading.Thread(target=click) for _ in range(2)]
    try:
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
    finally:
        backend._row = original_row
    assert not any(th.is_alive() for th in threads)
    [source] = backend.get_kb_detail(ws, kb_id)["data_sources"]
    assert {r["ds_id"] for r in results} == {source["ds_id"]}
    assert sorted(r["created"] for r in results) == [False, True]


def test_repair_then_upload_share_one_source(backend, ws):
    """A 补建 click followed by an upload: the upload reuses the repaired source."""
    kb_id = _kb_without_sources(backend, ws)
    made = backend.add_data_source(ws, kb_id, {"mode": "upload"})
    out = backend.upload_files(ws, kb_id, [("a.txt", b"a")])
    assert "data_source" not in out and out["keys"] == [f"{made['prefix']}a.txt"]
    assert len(backend.get_kb_detail(ws, kb_id)["data_sources"]) == 1


# --- N17-obs272 (user approval 2026-10-22): the forced full re-sync entry ---


def _routed_sync_url(backend) -> str:
    """The backend registered for the router (as the 502 route test above does) and the
    sync URL of a fresh upload-mode KB's source."""
    knowledge_backend.set_backend(backend)
    set_default_resources(SUBSTRATE)
    kb = backend.create_kb(ws_ctx(SUBSTRATE, region="cn-northwest-1"), "docs", "",
                           {"mode": "upload"})
    return f"/api/knowledge-bases/{kb['kb_id']}/data-sources/{kb['data_sources'][0]['ds_id']}/sync"


def test_sync_route_passes_full_true_through_to_the_planner_message(client, backend, fakes):
    url = _routed_sync_url(backend)
    res = client.post(url, json={"full": True})
    assert res.status_code == 200 and res.json()["status"] == "STARTING"
    (_, msg), = fakes["sqs"].sent
    assert msg["full"] is True and msg["kind"] == "kb_ingest"
    assert msg["job_id"] == res.json()["job_id"]


def test_the_default_sync_omits_full(client, backend, fakes):
    """No body, an empty-object body and `{full: false}` all stay incremental: the planner
    message carries no `full` key at all (byte-identical to before N17-obs272)."""
    url = _routed_sync_url(backend)
    assert client.post(url).status_code == 200
    # the console's `request()` always sends the JSON content type, body or not
    assert client.post(url, headers={"Content-Type": "application/json"}).status_code == 200
    assert client.post(url, json={}).status_code == 200
    assert client.post(url, json={"full": False}).status_code == 200
    assert len(fakes["sqs"].sent) == 4
    assert all("full" not in msg for _, msg in fakes["sqs"].sent)


def test_the_sync_body_is_validated(client, backend, fakes):
    url = _routed_sync_url(backend)
    assert client.post(url, json={"full": "yes please"}).status_code == 422
    assert client.post(url, json={"full": True, "force": True}).status_code == 422  # extra=forbid
    assert fakes["sqs"].sent == []


def test_start_sync_full_marks_the_message_and_nothing_else(backend, ws, fakes):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]
    # full first: since N17-obs275 a full rebuild beside a running job is refused
    backend.start_sync(ws, kb["kb_id"], ds["ds_id"], full=True)
    backend.start_sync(ws, kb["kb_id"], ds["ds_id"])
    (_, full), (_, plain) = fakes["sqs"].sent
    assert full == {**plain, "job_id": full["job_id"], "full": True}


# --- N17-obs275 (rev-r22): a full rebuild never starts beside a running job of its source ---


def test_a_full_rebuild_is_refused_while_a_job_of_the_source_runs(backend, ws, fakes):
    from app.core.errors import AppError

    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]
    first = backend.start_sync(ws, kb["kb_id"], ds["ds_id"], full=True)  # nothing running
    with pytest.raises(AppError) as refused:
        backend.start_sync(ws, kb["kb_id"], ds["ds_id"], full=True)      # the second click
    assert refused.value.code == "kb.sync_in_progress" and refused.value.status_code == 409
    assert refused.value.detail == {"job_id": first["job_id"]}
    assert len(fakes["sqs"].sent) == 1  # no second rebuild enqueued, no job written
    # the incremental sync keeps today's behaviour beside a running job
    backend.start_sync(ws, kb["kb_id"], ds["ds_id"])
    assert len(fakes["sqs"].sent) == 2 and "full" not in fakes["sqs"].sent[1][1]


def test_a_stale_running_job_does_not_block_a_full_rebuild(fakes, ws):
    from datetime import timedelta

    from app.services import knowledge_selfhosted as ks

    fakes["opensearch"].ensure_index(core.jobs_index(""), core.jobs_index_body())
    fakes["opensearch"].ensure_index(core.kb_docs_index(""), core.kb_docs_index_body())
    clock = {"now": T0}
    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: clock["now"])
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]
    backend.start_sync(ws, kb["kb_id"], ds["ds_id"], full=True)
    clock["now"] = T0 + timedelta(seconds=ks.RUNNING_JOB_STALE_S + 1)  # the job is an orphan
    out = backend.start_sync(ws, kb["kb_id"], ds["ds_id"], full=True)
    assert out["status"] == "STARTING" and len(fakes["sqs"].sent) == 2


def test_a_full_rebuild_is_refused_when_the_running_check_times_out(backend, ws, fakes,
                                                                    monkeypatch):
    from app.core.errors import AppError
    from app.services.knowledge_selfhosted import JobsSearchTimedOut

    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]

    def timed_out(*a, **kw):
        raise JobsSearchTimedOut("partial")

    monkeypatch.setattr(backend, "_running_job", timed_out)
    with pytest.raises(AppError) as refused:
        backend.start_sync(ws, kb["kb_id"], ds["ds_id"], full=True)
    assert refused.value.code == "kb.sync_in_progress"
    assert refused.value.detail == {"job_id": None} and fakes["sqs"].sent == []
    backend.start_sync(ws, kb["kb_id"], ds["ds_id"])  # the plain sync never consults it
    assert len(fakes["sqs"].sent) == 1


def test_the_sync_route_answers_409_sync_in_progress_for_a_second_full_rebuild(
    client, backend, fakes,
):
    url = _routed_sync_url(backend)
    first = client.post(url, json={"full": True})
    assert first.status_code == 200
    again = client.post(url, json={"full": True})
    assert again.status_code == 409
    body = again.json()
    assert body["code"] == "kb.sync_in_progress"
    assert body["detail"] == {"job_id": first.json()["job_id"]}
    assert client.post(url).status_code == 200  # incremental: unchanged


# --- N17-obs276: deleting a data source sweeps its extractor rows in the jobs index ---


def test_deleting_a_data_source_sweeps_its_result_and_lease_rows(backend, ws, fakes):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    kb_id = kb["kb_id"]
    ds1 = kb["data_sources"][0]["ds_id"]
    ds2 = backend.add_data_source(ws, kb_id, {"mode": "existing", "bucket": BUCKET,
                                              "prefix": f"kb/{kb_id}/other"})["ds_id"]
    jobs = core.jobs_index("")
    rows = {
        "r1": {"kind": "kb_ingest_doc_result", "kb_id": kb_id, "ds_id": ds1, "status": "INDEXED"},
        "l1": {"kind": "kb_ingest_doc_lease", "kb_id": kb_id, "ds_id": ds1, "owner": "ij1:x"},
        "j1": {"kind": "kb_ingest", "kb_id": kb_id, "ds_id": ds1, "status": "COMPLETE"},
        "r2": {"kind": "kb_ingest_doc_result", "kb_id": kb_id, "ds_id": ds2, "status": "INDEXED"},
        "l2": {"kind": "kb_ingest_doc_lease", "kb_id": kb_id, "ds_id": ds2, "owner": "ij2:y"},
        "rx": {"kind": "kb_ingest_doc_result", "kb_id": "KBOTHER", "ds_id": ds1},
    }
    for doc_id, row in rows.items():
        fakes["opensearch"].put_doc(jobs, doc_id, row)
    out = backend.delete_data_source(ws, kb_id, ds1)
    assert out["deleted"] is True and out["cleanup"]["docs"] == "ok"
    left = set(fakes["opensearch"].indices[jobs])
    assert {"r1", "l1"}.isdisjoint(left)                # this source's extractor rows: gone
    assert {"j1", "r2", "l2", "rx"} <= left             # its jobs, other sources / KBs: kept


def test_the_swept_row_kinds_are_the_extractors_own(fakes):
    import importlib.util
    import sys
    from pathlib import Path

    from app.services import knowledge_selfhosted as ks

    lam = Path(__file__).resolve().parents[1] / "lambdas" / "memory_extractor"
    sys.path.insert(0, str(lam))
    try:
        spec = importlib.util.spec_from_file_location("me_handler_kinds", lam / "handler.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(lam))
    assert set(ks.EXTRACTOR_DOC_ROW_KINDS) == {mod.KIND_KB_INGEST_DOC_RESULT, mod.KIND_KB_DOC_LEASE}


# --- N17-obs274 pass 4: a document left INDEXING by a dead chain is listed as interrupted ---


def test_a_stale_indexing_document_is_listed_failed_interrupted(fakes, ws):
    from datetime import timedelta

    from app.services import knowledge_selfhosted as ks

    fakes["opensearch"].ensure_index(core.jobs_index(""), core.jobs_index_body())
    fakes["opensearch"].ensure_index(core.kb_docs_index(""), core.kb_docs_index_body())
    clock = {"now": T0}
    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: clock["now"])
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    kb_id, ds_id = kb["kb_id"], kb["data_sources"][0]["ds_id"]
    for name, status in (("a.md", "INDEXING"), ("b.md", "INDEXED")):
        fakes["opensearch"].put_doc(core.kb_docs_index(""), f"{kb_id}:{ds_id}:{name}", {
            "kb_id": kb_id, "ds_id": ds_id, "name": name, "uri": f"s3://{BUCKET}/{name}",
            "status": status, "status_reason": None, "indexing_started_at": T0.isoformat(),
        })

    def listed():
        docs = backend.list_documents(ws, kb_id, ds_id)["documents"]
        return {d["name"]: (d["status"], d["status_reason"]) for d in docs}

    assert listed() == {"a.md": ("INDEXING", None), "b.md": ("INDEXED", None)}  # fresh: as is
    clock["now"] = T0 + timedelta(seconds=ks.RUNNING_JOB_STALE_S + 1)
    assert listed() == {"a.md": ("FAILED", "interrupted — run the sync again"),
                        "b.md": ("INDEXED", None)}
    stored = fakes["opensearch"].get_doc(core.kb_docs_index(""), f"{kb_id}:{ds_id}:a.md")
    assert stored["status"] == "INDEXING"  # presentation only
