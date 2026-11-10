"""E17 — knowledge bases: upload acceptance, delete guard and the dangling-reference scan.

Round 16, channel A (BUG-DISCOVERY-PLAN §E17 / §4). Random documents (0 bytes, the per-file
and total caps, non-UTF-8 bytes, duplicate names, many KBs on one agent) go through
`POST /api/knowledge-bases/{kb_id}/files` and the extractor's `kb_ingest`; random
create / attach / delete walks are checked against `docs/cn/interfaces/knowledge-backend.md`
("Delete contract", "Error codes", the R18 prefix rule) and the ledger scan in
`tests/property/ledger_invariants.py`. Oracles are round trips (bytes in == bytes out,
one object per file), differential (the router's refusal order replayed on the request)
and the ledger scan. Shrunk failing examples are findings (`xfail(strict=True)`, N16 ids).
"""

from __future__ import annotations

import json
from itertools import count
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, KnowledgeBase
from app.schemas.attachments import MAX_FILE_BYTES, MAX_FILES, MAX_REQUEST_BYTES, MAX_TOTAL_BYTES
from app.services import knowledge_backend
from app.services import opensearch_http as core
from app.services.knowledge_selfhosted import SelfHostedKnowledgeBackend
from tests.conftest import set_default_resources
from tests.memory_fakes import FakeEmbedder, FakeOpenSearch, FakeS3, FakeSQS
from tests.property.ledger_invariants import assert_ledger_invariants, scan_ledger
from tests.test_memory_selfhosted import BUCKET, SUBSTRATE, T0

_SEQ = count()
HYP = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.large_base_example,
    ],
)
# knowledge-backend.md "Error codes": the three upload refusals and their statuses. The caps
# themselves are the API's published limits (`detail.max_files` / `detail.max_bytes` echo
# them) — the invariant under test is the refusal ORDER and "before any S3 write".
NO_FILES = (400, "kb.no_files")
TOO_MANY = (422, "kb.too_many_files")
TOO_LARGE = (413, "kb.upload_too_large")
# The ASGI-level `AttachmentBodyCap` bounds the WHOLE multipart body before the router runs;
# a request over it is refused with ITS envelope (N16-obs37 pins the code it answers with —
# the property below only requires a 413 and no S3 write for that layer).
BODY_CAP = (413, None)
_PART_OVERHEAD = 1024  # generous multipart framing allowance per part


@pytest.fixture
def world(client):
    fakes = {
        "s3": FakeS3(),
        "opensearch": FakeOpenSearch(),
        "sqs": FakeSQS(),
        "embedder": FakeEmbedder(),
    }
    fakes["opensearch"].ensure_index(core.jobs_index(""), core.jobs_index_body())
    fakes["opensearch"].ensure_index(core.kb_docs_index(""), core.kb_docs_index_body())
    impl = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    knowledge_backend.set_backend(impl)
    set_default_resources(SUBSTRATE)
    return SimpleNamespace(client=client, impl=impl, **fakes)


def _create_kb(world, name: str | None = None) -> dict[str, Any]:
    res = world.client.post("/api/knowledge-bases", json={"name": name or f"kb-{next(_SEQ)}"})
    assert res.status_code == 202, res.text
    return res.json()


def _agent(
    name: str, kb_ids: list[str], *, created_by: str | None = None, status: str = "active"
) -> str:
    db = SessionLocal()
    try:
        row = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID,
            name=name,
            method="zip_runtime",
            status=status,
            created_by=created_by,
            created_by_name="someone" if created_by else None,
            spec={"name": name, "knowledge_bases": [{"kb_id": k} for k in kb_ids]},
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _set_pins(agent_id: str, kb_ids: list[str]) -> None:
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        row.spec = {**row.spec, "knowledge_bases": [{"kb_id": k} for k in kb_ids]}
        db.commit()
    finally:
        db.close()


def _reset_ledger() -> None:
    db = SessionLocal()
    try:
        db.query(Agent).delete()
        db.query(KnowledgeBase).delete()
        db.commit()
    finally:
        db.close()


# ── upload acceptance ─────────────────────────────────────────────────────────────────

_name_st = st.from_regex(r"[a-z][a-z0-9_-]{0,11}", fullmatch=True)
_size_st = st.sampled_from([0, 1, 17, 4096, MAX_FILE_BYTES - 1, MAX_FILE_BYTES, MAX_FILE_BYTES + 1])
file_st = st.fixed_dictionaries(
    {
        "stem": _name_st,
        "size": _size_st,
        "binary": st.booleans(),  # non-UTF-8 bytes (0xFF 0xFE …) instead of ASCII
    }
)
files_st = st.lists(file_st, max_size=MAX_FILES + 2, unique_by=lambda f: f["stem"])


def _payload(spec: dict[str, Any]) -> tuple[str, bytes]:
    body = (
        (b"\xff\xfe\x00\xc3" * (spec["size"] // 4 + 1))[: spec["size"]]
        if spec["binary"]
        else (b"a" * spec["size"])
    )
    return f"{spec['stem']}.txt", body


def _expected(files: list[tuple[str, bytes]]) -> tuple[int, str, dict[str, Any] | None]:
    """The router's refusal order replayed on the request (differential oracle)."""
    if sum(len(d) for _n, d in files) + _PART_OVERHEAD * max(len(files), 1) > MAX_REQUEST_BYTES:
        return (*BODY_CAP, None)
    if not files:
        return (*NO_FILES, None)
    if len(files) > MAX_FILES:
        return (*TOO_MANY, {"max_files": MAX_FILES})
    total = 0
    for _name, data in files:
        if len(data) > MAX_FILE_BYTES:
            return (*TOO_LARGE, {"max_bytes": MAX_FILE_BYTES})
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            return (*TOO_LARGE, {"max_bytes": MAX_TOTAL_BYTES})
    return 200, "", None


@HYP
@given(specs=files_st)
def test_upload_refusals_follow_the_contract_order_and_never_touch_s3(world, specs):
    kb = _create_kb(world)
    (ds,) = kb["data_sources"]
    files = [_payload(s) for s in specs]
    before = dict(world.s3.objects)
    res = world.client.post(
        f"/api/knowledge-bases/{kb['kb_id']}/files",
        files=[("files", (name, data, "text/plain")) for name, data in files],
    )
    status, code, detail = _expected(files)
    assert res.status_code == status, (res.text, [(n, len(d)) for n, d in files])
    if status != 200:
        body = res.json()
        if code is not None:
            assert body["code"] == code and body["detail"] == detail, body
        assert dict(world.s3.objects) == before, "a refused upload wrote to S3"
        return
    keys = res.json()["keys"]
    # round trip: one object per file, under the data source's prefix, bytes intact
    assert keys == [f"{ds['prefix']}{name}" for name, _ in files]
    new = {k: v for k, v in world.s3.objects.items() if k not in before}
    assert set(new) == {(BUCKET, k) for k in keys}
    for (name, data), key in zip(files, keys, strict=True):
        assert world.s3.objects[(BUCKET, key)]["Body"] == data, name
    assert_ledger_invariants(SessionLocal)


@pytest.mark.xfail(
    strict=True,
    reason="N16-obs36: two files with one basename in a single "
    "upload collapse onto one S3 key — `keys` lists it twice, the first body is "
    "silently replaced (upload_files returns 'stored object keys', one per file)",
)
def test_duplicate_basenames_in_one_upload_are_two_objects_or_a_refusal(world):
    kb = _create_kb(world)
    res = world.client.post(
        f"/api/knowledge-bases/{kb['kb_id']}/files",
        files=[
            ("files", ("notes.txt", b"first", "text/plain")),
            ("files", ("notes.txt", b"second", "text/plain")),
        ],
    )
    if res.status_code != 200:
        assert res.status_code in (400, 409, 422), res.text  # a refusal is acceptable
        return
    keys = res.json()["keys"]
    assert len(set(keys)) == len(keys) == 2, keys


def test_a_fifty_megabyte_upload_is_refused_before_any_s3_write(world):
    """knowledge-backend.md: `kb.upload_too_large` 413 — "upload-size/count caps checked
    before any S3 write"."""
    kb = _create_kb(world)
    before = dict(world.s3.objects)
    res = world.client.post(
        f"/api/knowledge-bases/{kb['kb_id']}/files",
        files=[("files", ("huge.txt", b"\0" * (50 * 1024 * 1024), "text/plain"))],
    )
    assert res.status_code == 413, res.text
    assert dict(world.s3.objects) == before


@pytest.mark.xfail(
    strict=True,
    reason="N16-obs37: a KB upload over the 15 MiB request body cap "
    "(one 50 MB file, or six 3 MB files that the contract orders as "
    "`kb.too_many_files`) answers the chat attachment envelope "
    "`chat.attachment_too_large` instead of the KB contract's `kb.*` code",
)
@pytest.mark.parametrize(
    "files",
    [
        [("huge.txt", b"\0" * (50 * 1024 * 1024))],
        [(f"{c}.txt", b"a" * MAX_FILE_BYTES) for c in "abcdef"],
    ],
)
def test_oversize_kb_uploads_answer_the_kb_contract_codes(world, files):
    kb = _create_kb(world)
    res = world.client.post(
        f"/api/knowledge-bases/{kb['kb_id']}/files",
        files=[("files", (name, data, "text/plain")) for name, data in files],
    )
    assert res.status_code in (413, 422), res.text
    assert res.json()["code"].startswith("kb."), res.json()


# ── ingest: the extractor over random documents ───────────────────────────────────────

import importlib.util  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

LAMBDA_DIR = Path(__file__).resolve().parents[2] / "lambdas" / "memory_extractor"


@pytest.fixture(scope="module")
def handler():
    sys.path.insert(0, str(LAMBDA_DIR))
    spec = importlib.util.spec_from_file_location(
        "memory_extractor_kb_prop", LAMBDA_DIR / "handler.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.path.remove(str(LAMBDA_DIR))


doc_st = st.fixed_dictionaries(
    {
        "stem": _name_st,
        "ext": st.sampled_from([".txt", ".md", ".json", ".html", ".bin", ".pdf"]),
        "size": st.sampled_from([0, 1, 300, 2500, 2_000_000, 2_000_001]),
        "binary": st.booleans(),
    }
)


@HYP
@given(docs=st.lists(doc_st, max_size=6, unique_by=lambda d: (d["stem"], d["ext"])))
def test_ingest_accounts_for_every_object_exactly_once(world, handler, docs):
    """kb_ingest: every object under the prefix gets exactly one `launchpad-kb-docs`
    record; scanned == indexed + failed; chunks exist iff the record is INDEXED; a 0-byte,
    non-UTF-8 or oversize document never leaves a half state and never fails the job."""
    kb = _create_kb(world)
    (ds,) = kb["data_sources"]
    index = core.kb_index_name(kb["kb_id"], "")
    objects = []
    for d in docs:
        name = f"{d['stem']}{d['ext']}"
        body = (
            (b"\xff\xfe\x00\xc3" * (d["size"] // 4 + 1))[: d["size"]]
            if d["binary"]
            else (b"lorem ipsum " * (d["size"] // 12 + 1))[: d["size"]]
        )
        world.s3.put_object(Bucket=BUCKET, Key=f"{ds['prefix']}{name}", Body=body)
        objects.append((name, body))
    deps = handler.Deps(
        s3=world.s3,
        opensearch=world.opensearch,
        bedrock=None,
        embedder=world.embedder,
        now=lambda: T0,
        env={},
    )
    job_id = f"job-{next(_SEQ)}"
    out = handler.process_kb_ingest(
        {
            "kind": "kb_ingest",
            "job_id": job_id,
            "bucket": BUCKET,
            "prefix": ds["prefix"],
            "index": index,
            "kb_id": kb["kb_id"],
            "ds_id": ds["ds_id"],
            "workspace_id": DEFAULT_WORKSPACE_ID,
        },
        deps,
    )
    counts = out["counts"]
    assert out["status"] == "COMPLETE"
    assert counts["numberOfDocumentsScanned"] == len(objects)
    assert counts["numberOfNewDocumentsIndexed"] + counts["numberOfDocumentsFailed"] == len(objects)
    records = [
        d
        for d in world.opensearch.indices[core.kb_docs_index("")].values()
        if d["kb_id"] == kb["kb_id"]
    ]
    assert sorted(r["name"] for r in records) == sorted(n for n, _ in objects)
    chunks = world.opensearch.indices.get(index, {})
    for r in records:
        mine = [c for c in chunks.values() if c["doc_uri"] == r["uri"]]
        if r["status"] == "INDEXED":
            assert r["chunk_count"] == len(mine) and r["indexed_at"]
        else:
            assert r["status"] == "UNSUPPORTED" and r["chunk_count"] == 0 and not mine, r
            assert r["status_reason"]
    job = world.opensearch.get_doc(core.jobs_index(""), job_id)
    assert job["status"] == "COMPLETE" and job["counts"] == counts


# ── delete guard and the dangling-reference scan ──────────────────────────────────────


def test_a_kb_mounted_by_ten_agents_refuses_delete_and_names_each_once(world):
    """knowledge-backend.md: `kb.has_attached_agents` 409 "carries the blocking agent
    names in `detail.agents`"; the KB row and its index survive the refusal."""
    kb = _create_kb(world)
    names = [f"reader-{i}" for i in range(10)]
    for n in names:
        _agent(n, [kb["kb_id"]])
    _agent("gone", [kb["kb_id"]], status="deleted")  # a deleted agent holds nothing
    res = world.client.delete(f"/api/knowledge-bases/{kb['kb_id']}")
    assert res.status_code == 409 and res.json()["code"] == "kb.has_attached_agents", res.text
    assert sorted(res.json()["detail"]["agents"]) == sorted(names)
    assert world.client.get(f"/api/knowledge-bases/{kb['kb_id']}").status_code == 200
    assert core.kb_index_name(kb["kb_id"], "") in world.opensearch.indices
    listed = world.client.get("/api/knowledge-bases").json()["items"]
    (row,) = [i for i in listed if i["kb_id"] == kb["kb_id"]]
    assert sorted(row["attached_agents"]) == sorted(names) and row["attached_agent_count"] == 10
    assert_ledger_invariants(SessionLocal)


def test_force_delete_leaves_no_agent_pinning_the_gone_kb(world):
    """N16-obs34 = N17-obs31, fixed in cn/r17-h — routers/knowledge.py: "force still strips the
    KB from EVERY mounting agent (integrity: no agent may keep a dangling KB id)"; the ledger
    scan comes back clean and every stripped agent is marked `last_strip`."""
    kb = _create_kb(world)
    agents = [_agent(f"reader-{i}", [kb["kb_id"]]) for i in range(3)]
    res = world.client.delete(f"/api/knowledge-bases/{kb['kb_id']}", params={"force": "true"})
    assert res.status_code == 200 and res.json()["deleted"] is True, res.text
    violations = scan_ledger(SessionLocal)
    assert violations == [], violations
    assert [a["id"] for a in res.json()["stripped_agents"]] == agents
    db = SessionLocal()
    try:
        for aid in agents:
            row = db.get(Agent, aid)
            pins = [p["kb_id"] for p in row.spec["knowledge_bases"]]
            assert kb["kb_id"] not in pins
            assert row.last_strip["kb_ids"] == [kb["kb_id"]]
    finally:
        db.close()


action_st = st.one_of(
    st.just({"kind": "create"}),
    st.fixed_dictionaries(
        {"kind": st.just("attach"), "kb": st.integers(0, 5), "agent": st.integers(0, 3)}
    ),
    st.fixed_dictionaries(
        {"kind": st.just("detach"), "kb": st.integers(0, 5), "agent": st.integers(0, 3)}
    ),
    st.fixed_dictionaries({"kind": st.just("delete"), "kb": st.integers(0, 5)}),
    st.fixed_dictionaries({"kind": st.just("delete_ds"), "kb": st.integers(0, 5)}),
    st.fixed_dictionaries({"kind": st.just("upload"), "kb": st.integers(0, 5)}),
)


@HYP
@given(actions=st.lists(action_st, min_size=1, max_size=14))
def test_random_create_attach_delete_walks_keep_mounts_and_the_ledger_consistent(world, actions):
    """After every step: the API's `attached_agents` equals the ledger's mounts, a mounted
    KB refuses an unforced delete (409, names the mounting agents), an unmounted one is
    deleted with the full cleanup block, a deleted KB answers 404, and the ledger scan is
    clean (unforced walks only — the forced path is N16-obs34 above)."""
    _reset_ledger()
    world.s3.objects.clear()
    kbs: list[dict[str, Any] | None] = []
    agents = [_agent(f"walker-{i}", []) for i in range(4)]
    mounts: dict[str, set[str]] = {}  # kb_id → agent names

    def names(kb_id: str) -> set[str]:
        return {f"walker-{agents.index(a)}" for a in mounts.get(kb_id, set())}

    for step in actions:
        kind = step["kind"]
        if kind == "create":
            kb = _create_kb(world)
            kbs.append(kb)
            mounts[kb["kb_id"]] = set()
            assert kb["status"] == "ACTIVE" and kb["attached_agents"] == []
        elif kind in ("attach", "detach"):
            live = [k for k in kbs if k is not None]
            if not live:
                continue
            kb = live[step["kb"] % len(live)]
            agent = agents[step["agent"]]
            if kind == "attach":
                mounts[kb["kb_id"]].add(agent)
            else:
                mounts[kb["kb_id"]].discard(agent)
            _set_pins(agent, [k for k, who in mounts.items() if agent in who])
        elif kind == "delete":
            live = [k for k in kbs if k is not None]
            if not live:
                continue
            kb = live[step["kb"] % len(live)]
            res = world.client.delete(f"/api/knowledge-bases/{kb['kb_id']}")
            if mounts[kb["kb_id"]]:
                assert res.status_code == 409 and res.json()["code"] == "kb.has_attached_agents"
                assert set(res.json()["detail"]["agents"]) == names(kb["kb_id"])
            else:
                assert res.status_code == 200, res.text
                body = res.json()
                assert body["deleted"] is True and body["kb_id"] == kb["kb_id"]
                assert body["cleanup"] == {"s3": "ok", "index": "ok", "docs": "ok"}
                assert body["cleanup_warnings"] == [] and body["cleanup_job_id"] is None
                assert core.kb_index_name(kb["kb_id"], "") not in world.opensearch.indices
                assert not any(k.startswith(f"kb/{kb['kb_id']}/") for _b, k in world.s3.objects)
                kbs[kbs.index(kb)] = None
                mounts.pop(kb["kb_id"])
                assert world.client.get(f"/api/knowledge-bases/{kb['kb_id']}").status_code == 404
                # a retried delete only 404s (contract: "a retried DELETE would only 404")
                again = world.client.delete(f"/api/knowledge-bases/{kb['kb_id']}")
                assert again.status_code == 404 and again.json()["code"] == "kb.not_found"
        elif kind == "delete_ds":
            live = [k for k in kbs if k is not None and k["data_sources"]]
            if not live:
                continue
            kb = live[step["kb"] % len(live)]
            (ds,) = kb["data_sources"]
            res = world.client.delete(
                f"/api/knowledge-bases/{kb['kb_id']}/data-sources/{ds['ds_id']}"
            )
            assert res.status_code == 200, res.text
            assert res.json()["deleted"] is True
            assert not any(k.startswith(ds["prefix"]) for _b, k in world.s3.objects)
            again = world.client.delete(
                f"/api/knowledge-bases/{kb['kb_id']}/data-sources/{ds['ds_id']}"
            )
            assert again.status_code == 404 and again.json()["code"] == "kb.data_source_not_found"
            # the KB survives with no upload source: the next upload creates a fresh one
            # (N17-obs242) under the KB's own namespace and stores the file there
            res = world.client.post(
                f"/api/knowledge-bases/{kb['kb_id']}/files",
                files=[("files", ("a.txt", b"x", "text/plain"))],
            )
            assert res.status_code == 200, res.text
            fresh = res.json()["data_source"]
            assert fresh["mode"] == "upload" and fresh["ds_id"] != ds["ds_id"]
            assert fresh["prefix"] == f"kb/{kb['kb_id']}/{fresh['ds_id']}/"
            assert res.json()["keys"] == [f"{fresh['prefix']}a.txt"]
            assert (BUCKET, f"{fresh['prefix']}a.txt") in world.s3.objects
            kb["data_sources"] = [fresh]
        elif kind == "upload":
            live = [k for k in kbs if k is not None and k["data_sources"]]
            if not live:
                continue
            kb = live[step["kb"] % len(live)]
            res = world.client.post(
                f"/api/knowledge-bases/{kb['kb_id']}/files",
                files=[("files", ("a.txt", b"hello", "text/plain"))],
            )
            assert res.status_code == 200, res.text
        # consistency: list == ledger mounts for every live KB; every pin resolves
        listed = {i["kb_id"]: i for i in world.client.get("/api/knowledge-bases").json()["items"]}
        for kb in kbs:
            if kb is None:
                continue
            assert set(listed[kb["kb_id"]]["attached_agents"]) == names(kb["kb_id"])
            assert listed[kb["kb_id"]]["attached_agent_count"] == len(names(kb["kb_id"]))
        assert set(listed) == {k["kb_id"] for k in kbs if k is not None}
        assert_ledger_invariants(SessionLocal)


def test_create_duplicate_name_is_409_and_leaves_one_row_and_one_index(world):
    kb = _create_kb(world, "handbook")
    dup = world.client.post("/api/knowledge-bases", json={"name": "handbook"})
    assert dup.status_code == 409 and dup.json()["code"] == "kb.name_exists"
    db = SessionLocal()
    try:
        assert db.query(KnowledgeBase).filter_by(name="handbook").count() == 1
    finally:
        db.close()
    per_kb = [
        n
        for n in world.opensearch.indices
        if n.startswith("launchpad-kb-") and n != core.kb_docs_index("")
    ]
    assert per_kb == [core.kb_index_name(kb["kb_id"], "")]
    assert_ledger_invariants(SessionLocal)


def test_delete_of_a_kb_mounted_by_a_system_preset_is_refused_even_with_force(world):
    kb = _create_kb(world)
    db = SessionLocal()
    try:
        db.add(
            Agent(
                workspace_id=DEFAULT_WORKSPACE_ID,
                name="frontdesk",
                method="zip_runtime",
                status="active",
                system_key="frontdesk",
                spec={"knowledge_bases": [{"kb_id": kb["kb_id"]}]},
            )
        )
        db.commit()
    finally:
        db.close()
    for params in ({}, {"force": "true"}):
        res = world.client.delete(f"/api/knowledge-bases/{kb['kb_id']}", params=params)
        assert res.status_code == 409 and res.json()["code"] == "kb.attached_to_system_agent"
    assert world.client.get(f"/api/knowledge-bases/{kb['kb_id']}").status_code == 200
    assert_ledger_invariants(SessionLocal)


def test_delete_cleanup_steps_degrade_to_a_pending_job_never_a_5xx(world):
    """knowledge-backend.md "Delete contract (WB-008)": a storage step that raises (an
    OpenSearch 5xx, a urllib3 timeout) leaves the row gone, the step `pending`, one warning
    with the exception type and a `kb_cleanup` job id — never an error on the delete."""
    import urllib3

    kb = _create_kb(world)
    original = world.opensearch.delete_index

    def flaky(name: str) -> None:
        raise urllib3.exceptions.ReadTimeoutError(
            None, f"/{name}", "Read timed out. (read timeout=10.0)"
        )

    world.opensearch.delete_index = flaky
    try:
        res = world.client.delete(f"/api/knowledge-bases/{kb['kb_id']}")
    finally:
        world.opensearch.delete_index = original
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["cleanup"]["index"] == "pending" and body["cleanup"]["s3"] == "ok"
    assert body["cleanup_warnings"] and body["cleanup_warnings"][0].startswith(
        "index: ReadTimeoutError"
    )
    assert body["cleanup_job_id"]
    assert world.client.get(f"/api/knowledge-bases/{kb['kb_id']}").status_code == 404
    job = world.client.get(f"/api/jobs/{body['cleanup_job_id']}")
    assert job.status_code == 200, job.text
    assert_ledger_invariants(SessionLocal)


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)
