"""SelfHostedMemoryBackend (N6): S3 snapshots + OpenSearch records + SQS, all faked."""

import json
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
import urllib3
from botocore.exceptions import ClientError

from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.models.ledger import ManagedMemory, MemoryStore
from app.services import embedding_backend, memory_backend
from app.services import opensearch_http as core
from app.services.memory_selfhosted import SelfHostedMemoryBackend, os_ctx, store_id_for
from tests.conftest import set_default_resources, ws_ctx
from tests.memory_fakes import FakeEmbedder, FakeOpenSearch, FakeS3, FakeSQS

BUCKET = "launchpad-artifacts-test"
SUBSTRATE = {
    "artifacts_bucket": BUCKET,
    "memory_id": "launchpad_memory-default",
    "memory_bucket": BUCKET,
    "memory_prefix": "memory",
    "memory_os_endpoint": "search-mem.example",
    "memory_queue_url": "https://sqs.example/123/launchpad-memory-extract",
    "memory_admin_role_arn": (
        "arn:aws-cn:iam::111122223333:role/launchpad-memory-admin-cn-northwest-1"
    ),
}
STORE = "launchpad_memory-default"
ACTOR = memory_backend.scoped_actor("agent1", "river")  # agent1__river
T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
def fakes():
    return {
        "s3": FakeS3(),
        "opensearch": FakeOpenSearch(),
        "sqs": FakeSQS(),
        "embedder": FakeEmbedder(),
    }


@pytest.fixture
def backend(client, fakes):  # `client` → init_db ran (memory_stores table exists)
    fakes["opensearch"].ensure_index(core.memory_index(""), core.memory_index_body())
    fakes["opensearch"].ensure_index(core.jobs_index(""), core.jobs_index_body())
    return SelfHostedMemoryBackend(**fakes, clock=lambda: T0)


@pytest.fixture
def ws():
    return ws_ctx(SUBSTRATE, region="cn-northwest-1")


def _seed_turn(fakes, actor=ACTOR, session="sess-1", prompt="I like tea", answer="Noted!", ts=T0):
    ev = core.turn_event(actor, session, prompt, answer, ts)
    key = f"memory/{STORE}/{actor}/session_{session}/events/{core.event_key(ts, ev['eventId'][:8])}"
    fakes["s3"].put_object(Bucket=BUCKET, Key=key, Body=json.dumps(ev).encode())
    fakes["s3"].put_object(
        Bucket=BUCKET, Key=f"memory/{STORE}/{actor}/session_{session}/session.json", Body=b"{}"
    )
    return key, ev


def _seed_record(
    fakes, namespace, text, strategy="semantic", actor=ACTOR, session=None, ts=T0, rid=None
):
    doc = {
        "store_id": STORE,
        "workspace_id": "default",
        "actor_id": actor,
        "namespace": namespace,
        "strategy": strategy,
        "session_id": session,
        "text": text,
        "embedding": fakes["embedder"].embed(text),
        "created_at": ts.isoformat(),
        "source_event_ids": [],
    }
    fakes["opensearch"].put_doc(core.memory_index(""), rid or f"r-{abs(hash(text))}", doc)


def test_every_substrate_method_503s_memory_not_provisioned_without_an_endpoint(backend):
    bare = ws_ctx({"artifacts_bucket": BUCKET})
    for call in (
        lambda: backend.list_actors(bare),
        lambda: backend.list_memory_resources(bare),
        lambda: backend.search_records(bare, "/facts/x", "q"),
    ):
        with pytest.raises(AppError) as exc:
            call()
        assert exc.value.code == "memory.not_provisioned" and exc.value.status_code == 503
    assert backend.memory_overview(bare)["configured"] is False


def test_os_ctx_assumes_the_admin_role_when_recorded(ws):
    assert os_ctx(ws).role_arn == SUBSTRATE["memory_admin_role_arn"]
    assert os_ctx(ws).id == ws.id and os_ctx(ws).resources is ws.resources
    assert os_ctx(ws_ctx({**SUBSTRATE, "memory_admin_role_arn": ""})).role_arn is None


def test_actors_sessions_and_events_come_from_the_snapshot_prefixes(backend, ws, fakes):
    _seed_turn(fakes)
    _seed_turn(
        fakes, session="sess-2", prompt="and coffee", answer="Sure", ts=T0 + timedelta(minutes=1)
    )
    _seed_turn(fakes, actor="agent2__alice", session="s9", ts=T0)

    actors = backend.list_actors(ws)
    assert [a["actor_id"] for a in actors["items"]] == ["agent1__river", "agent2__alice"]
    assert actors["items"][0] == {
        "actor_id": "agent1__river",
        "agent_id": "agent1",
        "human_actor": "river",
        "scoped": True,
    }
    assert actors["next_token"] is None

    sessions = backend.list_sessions(ws, ACTOR)
    assert [s["session_id"] for s in sessions["items"]] == ["sess-2", "sess-1"]  # newest first
    assert sessions["items"][0]["actor_id"] == ACTOR and sessions["items"][0][
        "created_at"
    ].startswith("2026-10-09T12:01")
    assert sessions["items"][0]["ledger"] is None  # the router fills it in

    (event,) = backend.list_events(ws, ACTOR, "sess-1")["items"]
    # hybrid shape (ruling R7): AgentCore keys for observability …
    assert event["eventTimestamp"] == T0.isoformat() and event["eventId"] == event["event_id"]
    assert [p["conversational"]["role"] for p in event["payload"]] == ["USER", "ASSISTANT"]
    # … and console keys for the untouched frontend
    assert event["at"] == T0.isoformat() and event["branch"] is None and event["metadata"] == {}
    assert event["payload"][0] == {
        "kind": "conversational",
        "role": "USER",
        "text": "I like tea",
        "parts": [],
        "blob_bytes": None,
        "conversational": {"role": "USER", "content": {"text": "I like tea"}},
    }


def test_list_sessions_probes_each_session_with_at_most_two_s3_calls(backend, ws, fakes):
    # Fix round 2: the per-session oldest/newest probe used to paginate every
    # events/ page per session (O(sessions * pages) S3 round trips per request).
    for i, session in enumerate(["sess-a", "sess-b", "sess-c"]):
        _seed_turn(fakes, session=session, prompt=f"p{i}", ts=T0 + timedelta(seconds=i))
    calls_by_prefix: Counter[str] = Counter()
    original = fakes["s3"].list_objects_v2

    def counting(*, Prefix="", **kw):
        calls_by_prefix[Prefix] += 1
        return original(Prefix=Prefix, **kw)

    fakes["s3"].list_objects_v2 = counting
    out = backend.list_sessions(ws, ACTOR)
    assert [s["session_id"] for s in out["items"]] == ["sess-c", "sess-b", "sess-a"]
    event_prefixes = [p for p in calls_by_prefix if p.endswith("/events/")]
    assert len(event_prefixes) == 3  # one probed prefix per session, no repeats
    assert all(calls_by_prefix[p] <= 2 for p in event_prefixes)


def test_events_paginate_with_s3_tokens_and_honour_max_results(backend, ws, fakes):
    for i in range(5):
        _seed_turn(fakes, prompt=f"p{i}", answer=f"a{i}", ts=T0 + timedelta(seconds=i))
    page = backend.list_events(ws, ACTOR, "sess-1", True, None, 2)
    assert [e["payload"][0]["text"] for e in page["items"]] == ["p0", "p1"] and page["next_token"]
    rest = backend.list_events(ws, ACTOR, "sess-1", True, page["next_token"], 10)
    assert [e["payload"][0]["text"] for e in rest["items"]] == ["p2", "p3", "p4"] and rest[
        "next_token"
    ] is None


def test_events_without_payloads_drop_the_text(backend, ws, fakes):
    _seed_turn(fakes)
    (event,) = backend.list_events(ws, ACTOR, "sess-1", include_payloads=False)["items"]
    assert event["payload"] == [] and event["event_id"]


def test_expired_events_are_hidden_by_the_stores_expiry(backend, ws, fakes):
    _seed_turn(fakes, session="old", ts=T0 - timedelta(days=31))
    _seed_turn(fakes, session="new", ts=T0)
    assert [s["session_id"] for s in backend.list_sessions(ws, ACTOR)["items"]] == ["new"]
    assert backend.list_events(ws, ACTOR, "old")["items"] == []


def test_unknown_actor_or_session_lists_empty_not_404(backend, ws):
    assert backend.list_sessions(ws, "nobody__x")["items"] == []
    assert backend.list_events(ws, "nobody__x", "s")["items"] == []


def test_create_turn_event_writes_one_event_file_and_enqueues_extraction(backend, ws, fakes):
    out = backend.create_turn_event(ws, ACTOR, "sess-1", "hello", "hi there")
    keys = [
        k
        for (b, k) in fakes["s3"].objects
        if k.startswith(f"memory/{STORE}/{ACTOR}/session_sess-1/events/")
    ]
    assert len(keys) == 1 and keys[0].endswith(".json") and out["event_key"] == keys[0]
    body = json.loads(fakes["s3"].get_object(Bucket=BUCKET, Key=keys[0])["Body"].read())
    assert body["payload"][1]["conversational"]["content"]["text"] == "hi there"
    ((queue_url, msg),) = fakes["sqs"].sent
    assert queue_url == SUBSTRATE["memory_queue_url"]
    assert msg == {
        "kind": "memory_extract",
        "store_id": STORE,
        "workspace_id": "default",
        "actor_id": ACTOR,
        "session_id": "sess-1",
        "event_key": keys[0],
        "long_term": True,
        # D21 (M1-d): the registry's active embedding row rides with every message
        "embedding": embedding_backend.message_block(),
        # r14-a: the store's merge strategy knobs (defaults for the synthesized store)
        "merge": core.MERGE_SETTINGS_DEFAULTS,
        # N17-obs252: the memory_extract model role rides with every message once the
        # Settings field exists (cn/r17-ah + cn/r17-aj merged together)
        **({"extract_model": block} if (block := embedding_backend.extract_model_block()) else {}),
    }
    assert msg.get("extract_model") == embedding_backend.extract_model_block()
    assert msg["embedding"]["model_id"] == core.EMBED_MODEL_ID
    assert msg["embedding"]["dimension"] == core.EMBED_DIMENSIONS


def test_strategies_and_namespaces_follow_the_fixed_catalog(backend, ws):
    strategies = backend.get_strategies(ws)
    assert [s["strategy_id"] for s in strategies] == [
        "semantic",
        "user_preference",
        "summarization",
    ]
    assert (
        strategies[0]["namespaces"] == ["/facts/{actorId}"] and strategies[0]["type"] == "SEMANTIC"
    )
    ns = backend.resolve_namespaces(ws, ACTOR)
    assert ns[0] == {
        "strategy_id": "semantic",
        "strategy_name": "Semantic facts",
        "strategy_type": "SEMANTIC",
        "template": "/facts/{actorId}",
        "namespace": f"/facts/{ACTOR}",
        "resolvable": True,
        "prefix": False,
    }
    assert ns[2]["namespace"] == f"/summaries/{ACTOR}/" and ns[2]["prefix"] is True


def test_list_records_filters_by_namespace_and_pages_by_offset(backend, ws, fakes):
    _seed_record(fakes, f"/facts/{ACTOR}", "User likes tea", ts=T0, rid="a")
    _seed_record(
        fakes, f"/facts/{ACTOR}", "User lives in Ningxia", ts=T0 + timedelta(seconds=1), rid="b"
    )
    _seed_record(fakes, "/facts/other__x", "noise", rid="c")
    _seed_record(
        fakes,
        f"/summaries/{ACTOR}/sess-1",
        json.dumps({"summary": "Tea chat"}),
        "summarization",
        session="sess-1",
        rid="d",
    )
    page = backend.list_records(ws, f"/facts/{ACTOR}", None, None, 1)
    assert page["namespace"] == f"/facts/{ACTOR}" and [r["record_id"] for r in page["items"]] == [
        "b"
    ]
    assert page["next_token"] == "1"
    rest = backend.list_records(ws, f"/facts/{ACTOR}", None, "1", 10)
    assert [r["record_id"] for r in rest["items"]] == ["a"] and rest["next_token"] is None
    assert rest["items"][0] == {
        "record_id": "a",
        "text": "User likes tea",
        "structured": None,
        "raw_text": "User likes tea",
        "strategy_id": "semantic",
        "namespaces": [f"/facts/{ACTOR}"],
        "created_at": T0.isoformat(),
        "score": None,
        "similarity": None,
        "metadata": {"store_id": STORE, "session_id": None},
        # r14-a: a pre-v2 document reads as active, confidence 1.0, no op history
        "updated_at": None,
        "confidence": 1.0,
        "superseded_by": None,
        "superseded_at": None,
        "supersedes": [],
        "deleted_at": None,
        "expired_at": None,
        "last_op": None,
        "active": True,
    }
    summ = backend.list_records(ws, f"/summaries/{ACTOR}/")[
        "items"
    ]  # prefix namespace matches every session
    assert summ[0]["text"] == "Tea chat" and summ[0]["structured"] == {"summary": "Tea chat"}
    assert (
        backend.list_records(ws, f"/facts/{ACTOR}", "user_preference")["items"] == []
    )  # strategy filter


def test_search_and_retrieve_use_knn_over_the_namespace(backend, ws, fakes):
    _seed_record(fakes, f"/facts/{ACTOR}", "User likes green tea in the morning", rid="tea")
    _seed_record(fakes, f"/facts/{ACTOR}", "User owns two cats", rid="cats")
    _seed_record(fakes, "/facts/other__x", "User likes green tea", rid="leak")
    out = backend.search_records(ws, f"/facts/{ACTOR}", "tea", 1)
    assert (
        out["namespace"] == f"/facts/{ACTOR}"
        and out["query"] == "tea"
        and out["next_token"] is None
    )
    assert [r["record_id"] for r in out["items"]] == ["tea"] and out["items"][0]["score"] > 0
    assert "embedding" not in json.dumps(out)
    hits = backend.retrieve_records(ws, f"/facts/{ACTOR}", "cats", 3)
    assert hits[0]["text"] == "User owns two cats"
    assert memory_backend.decode_record_text(hits[0]["raw_text"])[0] == hits[0]["text"]


def test_extraction_jobs_are_read_from_the_jobs_index(backend, ws, fakes):
    fakes["opensearch"].put_doc(
        core.jobs_index(""),
        "j1",
        {
            "job_id": "j1",
            "kind": "memory_extract",
            "store_id": STORE,
            "workspace_id": "default",
            "actor_id": ACTOR,
            "session_id": "sess-1",
            "strategy_ids": ["semantic", "user_preference"],
            "status": "COMPLETED",
            "counts": {"facts": 2, "preferences": 1, "duplicates": 1},
            "failure_reason": None,
            "created_at": T0.isoformat(),
            "updated_at": T0.isoformat(),
        },
    )
    fakes["opensearch"].put_doc(
        core.jobs_index(""),
        "j2",
        {
            "job_id": "j2",
            "kind": "kb_ingest",
            "kb_id": "KB1",
            "workspace_id": "default",
            "status": "FAILED",
            "created_at": T0.isoformat(),
            "updated_at": T0.isoformat(),
        },
    )
    page = backend.list_extraction_jobs(ws)
    assert [j["job_id"] for j in page["items"]] == ["j1"]  # memory jobs of THIS store only
    assert page["items"][0]["status"] == "COMPLETED" and page["items"][0]["counts"]["facts"] == 2
    assert backend.list_extraction_jobs(ws, status="FAILED")["items"] == []
    assert backend.list_extraction_jobs(ws, strategy_id="semantic")["items"][0]["job_id"] == "j1"


def test_session_memory_summary_counts_events_and_lists_actor_and_session_records(
    backend, ws, fakes
):
    _seed_turn(fakes)
    _seed_turn(fakes, prompt="more", answer="ok", ts=T0 + timedelta(seconds=1))
    _seed_record(fakes, f"/facts/{ACTOR}", "User likes tea", rid="f")
    _seed_record(
        fakes,
        f"/preferences/{ACTOR}",
        json.dumps({"preference": "concise answers"}),
        "user_preference",
        rid="p",
    )
    _seed_record(
        fakes,
        f"/summaries/{ACTOR}/sess-1",
        json.dumps({"summary": "Tea talk"}),
        "summarization",
        session="sess-1",
        rid="s",
    )
    _seed_record(
        fakes,
        f"/summaries/{ACTOR}/other",
        json.dumps({"summary": "Other"}),
        "summarization",
        session="other",
        rid="o",
    )
    out = backend.session_memory_summary(ws, ACTOR, "sess-1")
    assert out["event_count"] == 2
    assert sorted((r["namespace"], r["text"]) for r in out["records"]) == sorted(
        [
            (f"/facts/{ACTOR}", "User likes tea"),
            (f"/preferences/{ACTOR}", "concise answers"),
            (f"/summaries/{ACTOR}/sess-1", "Tea talk"),
        ]
    )
    assert backend.session_memory_summary(ws, ACTOR, "sess-1", memory_id=STORE)["event_count"] == 2


def test_the_default_store_is_synthesized_until_edited(backend, ws):
    rows = backend.list_memory_resources(ws)
    assert rows["default_id"] == STORE
    (row,) = rows["items"]
    assert row["id"] == STORE and row["name"] == "launchpad_memory" and row["is_default"] is True
    assert row["status"] == "ACTIVE" and row["agents"] == [] and row["arn"] is None
    detail = backend.get_memory_resource(ws, STORE)
    assert (
        detail["event_expiry_days"] == 30
        and detail["is_default"] is True
        and detail["namespace_keys"] == []
    )
    assert [s["strategy_id"] for s in detail["strategies"]] == [
        "semantic",
        "user_preference",
        "summarization",
    ]
    db = SessionLocal()
    assert db.query(MemoryStore).count() == 0
    db.close()
    updated = backend.update_memory_resource(ws, STORE, description="shared", event_expiry_days=14)
    assert updated["description"] == "shared" and updated["event_expiry_days"] == 14
    db = SessionLocal()
    assert db.query(MemoryStore).filter_by(memory_id=STORE).one().event_expiry_days == 14
    db.close()


def test_create_update_delete_a_store(backend, ws, fakes):
    created = backend.create_memory_resource(
        ws,
        "team_notes",
        "notes",
        60,
        ["semantic"],
        [{"key": "team", "allowed_values": ["a"], "regex_pattern": None}],
    )
    sid = created["id"]
    assert (
        sid.startswith("team_notes-")
        and len(sid) == len("team_notes-") + 8
        and created["is_default"] is False
    )
    assert [s["strategy_id"] for s in created["strategies"]] == ["semantic"] and created[
        "namespace_keys"
    ][0]["key"] == "team"
    assert [r["id"] for r in backend.list_memory_resources(ws)["items"]] == [STORE, sid]
    fakes["s3"].put_object(
        Bucket=BUCKET, Key=f"memory/{sid}/a__b/session_s/events/x.json", Body=b"{}"
    )
    fakes["opensearch"].put_doc(
        core.memory_index(""),
        "z",
        {
            "store_id": sid,
            "namespace": "/facts/a__b",
            "text": "t",
            "embedding": fakes["embedder"].embed("t"),
            "created_at": T0.isoformat(),
        },
    )
    assert backend.delete_memory_resource(ws, sid) == {
        "id": sid, "deleted": True, "cleanup": {"s3": "ok", "index": "ok"}, "cleanup_warnings": []}
    assert not [k for (_, k) in fakes["s3"].objects if k.startswith(f"memory/{sid}/")]
    assert "z" not in fakes["opensearch"].indices[core.memory_index("")]
    with pytest.raises(NotFoundError) as exc:
        backend.get_memory_resource(ws, sid)
    assert exc.value.code == "memory.not_found"
    with pytest.raises(AppError) as exc:
        backend.delete_memory_resource(ws, STORE)
    assert exc.value.code == "memory.default_protected" and exc.value.status_code == 409


def test_delete_logs_a_warning_when_opensearch_cleanup_fails_but_the_row_is_still_gone(
    backend, ws, fakes, caplog
):
    created = backend.create_memory_resource(ws, "flaky", "", 30, ["semantic"])
    sid = created["id"]

    def boom(*args, **kwargs):
        raise core.OpenSearchError(503, "upstream unavailable")

    fakes["opensearch"].delete_by_query = boom
    with caplog.at_level("WARNING", logger="launchpad.memory"):
        out = backend.delete_memory_resource(ws, sid)
    assert out == {"id": sid, "deleted": True, "cleanup": {"s3": "ok", "index": "skipped"},
                   "cleanup_warnings": ["index: OpenSearchError: HTTP 503"]}
    assert sid in caplog.text and "503" in caplog.text and "upstream unavailable" not in caplog.text
    db = SessionLocal()
    assert db.query(MemoryStore).filter_by(memory_id=sid).first() is None
    db.close()
    with pytest.raises(NotFoundError):
        backend.get_memory_resource(ws, sid)


def test_delete_swallows_a_transport_timeout_once_the_row_is_gone(backend, ws, fakes, caplog):
    """N14-obs4: the retrying pool re-raises a urllib3 timeout on its last attempt; the
    best-effort purge must not turn a finished delete into an error."""
    created = backend.create_memory_resource(ws, "slow", "", 30, ["semantic"])
    sid = created["id"]

    def timeout(*args, **kwargs):
        raise urllib3.exceptions.ReadTimeoutError(
            None, "/launchpad-memory/_delete_by_query", "Read timed out. (read timeout=10.0)"
        )

    fakes["opensearch"].delete_by_query = timeout
    with caplog.at_level("WARNING", logger="launchpad.memory"):
        out = backend.delete_memory_resource(ws, sid)
    assert out == {"id": sid, "deleted": True, "cleanup": {"s3": "ok", "index": "skipped"},
                   "cleanup_warnings": ["index: ReadTimeoutError"]}
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert sid in warnings[0].getMessage() and "ReadTimeoutError" in warnings[0].getMessage()
    db = SessionLocal()
    assert db.query(MemoryStore).filter_by(memory_id=sid).first() is None
    db.close()
    with pytest.raises(NotFoundError):
        backend.get_memory_resource(ws, sid)


def test_delete_api_answers_200_when_the_store_purge_times_out(client, fakes, caplog):
    """N14-obs4 through the router: `DELETE /api/memory/resources/{id}` is 200 (not 502
    memory.unavailable) when only the OpenSearch purge timed out; the id is 404 after."""
    set_default_resources(SUBSTRATE)
    memory_backend.set_backend(SelfHostedMemoryBackend(**fakes, clock=lambda: T0))
    created = client.post(
        "/api/memory/resources", json={"name": "hr_memory_test", "strategies": ["semantic"]}
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]

    def timeout(*args, **kwargs):
        raise urllib3.exceptions.ReadTimeoutError(
            None, "/launchpad-memory/_delete_by_query", "Read timed out. (read timeout=10.0)"
        )

    fakes["opensearch"].delete_by_query = timeout
    with caplog.at_level("WARNING", logger="launchpad.memory"):
        res = client.delete(f"/api/memory/resources/{sid}")
    assert res.status_code == 200, res.text
    assert res.json() == {"id": sid, "deleted": True, "cleanup": {"s3": "ok", "index": "skipped"},
                          "cleanup_warnings": ["index: ReadTimeoutError"]}
    assert sum(1 for r in caplog.records if r.levelname == "WARNING" and sid in r.getMessage()) == 1
    assert client.get(f"/api/memory/resources/{sid}").status_code == 404


@pytest.mark.parametrize("operation, code", [
    ("list_objects_v2", "AccessDenied"), ("list_objects_v2", "SlowDown"),
    ("delete_objects", "InternalError"),
])
def test_delete_answers_200_and_still_purges_the_index_when_the_s3_purge_fails(
    client, fakes, caplog, operation, code,
):
    """N17-obs93 (fixed in cn/r17-n): the S3 purge is as best-effort as the OpenSearch one.
    A recorded S3 shape (`AccessDenied` / `SlowDown` — neither in AWS_ERROR_MAP) on the
    prefix listing or the batch delete used to escape as 502 `memory.unavailable` (botocore
    text in the message) for a store whose row was already gone, and the index purge after it
    never ran. Now: 200, `cleanup.s3 = skipped` with a redacted warning, the index purged, the
    id 404 after, the managed registration dropped."""
    set_default_resources(SUBSTRATE)
    memory_backend.set_backend(SelfHostedMemoryBackend(**fakes, clock=lambda: T0))
    created = client.post(
        "/api/memory/resources", json={"name": "s3_flaky", "strategies": ["semantic"]}
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    fakes["s3"].put_object(Bucket=BUCKET, Key=f"memory/{sid}/a__b/session_s/events/x.json",
                           Body=b"{}")
    fakes["opensearch"].put_doc(core.memory_index(""), "orphan", {
        "store_id": sid, "namespace": "/facts/a__b", "text": "t",
        "embedding": fakes["embedder"].embed("t"), "created_at": T0.isoformat()})

    def boom(**kwargs):
        raise ClientError({"Error": {"Code": code, "Message":
                                     "User: arn:aws-cn:iam::123456789012:role/hub is not "
                                     "authorized to perform: s3:ListBucket"}}, operation)

    setattr(fakes["s3"], operation, boom)
    with caplog.at_level("WARNING", logger="launchpad.memory"):
        res = client.delete(f"/api/memory/resources/{sid}")
    assert res.status_code == 200, res.text
    assert res.json() == {"id": sid, "deleted": True, "cleanup": {"s3": "skipped", "index": "ok"},
                          "cleanup_warnings": [f"s3: ClientError: {code} ({operation})"]}
    for marker in ("is not authorized", "An error occurred", "arn:"):
        assert marker not in res.text and marker not in caplog.text, marker
    assert "orphan" not in fakes["opensearch"].indices[core.memory_index("")]
    assert client.get(f"/api/memory/resources/{sid}").status_code == 404
    db = SessionLocal()
    assert db.query(ManagedMemory).filter_by(memory_id=sid).first() is None
    db.close()


IAM_403_BODY = (
    '{"error":{"root_cause":[{"type":"security_exception","reason":"User: arn:aws-cn:sts::'
    '123456789012:assumed-role/launchpad-hub-role/i-0abc is not authorized to perform: '
    'es:ESHttpGet"}],"type":"security_exception"},"status":403}'
)
SEAM_MARKERS = ("is not authorized", "root_cause", "assumed-role", "OpenSearch HTTP")


def _unmapped_403(client, fakes, method: str, caplog):
    import logging

    def boom(*args, **kwargs):
        raise core.OpenSearchError(403, IAM_403_BODY)

    setattr(fakes["opensearch"], method, boom)
    set_default_resources(SUBSTRATE)
    memory_backend.set_backend(SelfHostedMemoryBackend(**fakes, clock=lambda: T0))
    with caplog.at_level(logging.WARNING, logger="launchpad.memory"):
        res = client.delete("/api/memory/records/r-tea", params={"actor_id": ACTOR}) \
            if method == "get_doc" else \
            client.get("/api/memory/records", params={"namespace": f"/facts/{ACTOR}"})
    return res


@pytest.mark.parametrize("method", ["get_doc", "search"])
def test_an_unmapped_opensearch_status_answers_a_static_502_without_the_body(
    client, fakes, caplog, method,
):
    """N17-obs92 (fixed in cn/r17-n): for a status the console cannot translate (the recorded
    IAM 403) `routers/memory._guard` answers 502 `memory.unavailable` with a STATIC message —
    `str(exc)`, which embeds the raw body with the instance role ARN, used to be the message.
    The exception type travels in `detail`; the log line carries type + status only."""
    res = _unmapped_403(client, fakes, method, caplog)
    assert res.status_code == 502, res.text
    body = res.json()
    assert body["code"] == "memory.unavailable"
    assert body["message"] == "memory lookup failed"
    assert body["detail"] == {"error_type": "OpenSearchError"}
    for marker in SEAM_MARKERS:
        assert marker not in res.text, marker
        assert marker not in caplog.text, marker
    assert "OpenSearchError: HTTP 403" in caplog.text


def test_the_resources_and_chat_guards_answer_the_same_static_502(caplog):
    """`routers/memory_resources._guard` and the chat memory rail share the fallback
    (N17-obs92): the resources guard's message is static too, the chat rail's call goes
    through `routers/memory._unavailable`."""
    import logging

    from app.routers import memory as memory_router
    from app.routers import memory_resources

    def boom():
        raise core.OpenSearchError(403, IAM_403_BODY)

    with caplog.at_level(logging.WARNING, logger="launchpad.memory"):
        with pytest.raises(AppError) as exc:
            memory_resources._guard(boom)
        err = memory_router._unavailable("memory lookup failed", boom,
                                         core.OpenSearchError(400, '{"root_cause":"x"}'))
    assert exc.value.status_code == 502 and exc.value.code == "memory.unavailable"
    assert exc.value.message == "memory operation failed"
    assert exc.value.detail == {"error_type": "OpenSearchError"}
    assert err.message == "memory lookup failed" and err.status_code == 502
    for marker in SEAM_MARKERS:
        assert marker not in caplog.text, marker
    assert "boom failed: OpenSearchError: HTTP 403" in caplog.text


def test_opensearch_errors_surface_as_503_backend_unavailable_through_the_api(client, fakes):
    # Fix round 2, item 3: an OpenSearchError the router's _guard() swallowed
    # into a generic 502 memory.unavailable now maps to a translatable 503
    # memory.backend_unavailable (and 404 -> memory.not_found) through the
    # app.core.errors global handler.
    def boom_search(*args, **kwargs):
        raise core.OpenSearchError(503, "upstream unavailable: do not echo this body")

    fakes["opensearch"].search = boom_search
    set_default_resources(SUBSTRATE)
    memory_backend.set_backend(SelfHostedMemoryBackend(**fakes, clock=lambda: T0))

    res = client.get("/api/memory/records", params={"namespace": f"/facts/{ACTOR}"})
    assert res.status_code == 503
    body = res.json()
    assert body["code"] == "memory.backend_unavailable"
    assert "do not echo this body" not in json.dumps(body)


def test_legacy_store_listing_episodic_loads_without_it(backend, ws, caplog):
    """R13 (N13-obs4): `episodic` left the catalog; a pre-R13 row that still lists it
    must keep loading, with the retired entry ignored and one log line."""
    created = backend.create_memory_resource(ws, "legacy", "", 30, ["semantic"])
    db = SessionLocal()
    row = db.query(MemoryStore).filter_by(memory_id=created["id"]).one()
    row.strategies = ["semantic", "episodic", "summarization"]
    db.commit()
    db.close()
    with pytest.raises(AppError) as exc:
        backend.create_memory_resource(ws, "fresh", "", 30, ["episodic"])
    assert exc.value.code == "memory.unknown_strategy"
    with caplog.at_level("INFO", logger="launchpad.memory"):
        detail = backend.get_memory_resource(ws, created["id"])
    assert [s["strategy_id"] for s in detail["strategies"]] == ["semantic", "summarization"]
    lines = [r.getMessage() for r in caplog.records if "retired strategies" in r.getMessage()]
    assert lines == [f"memory store {created['id']}: ignoring retired strategies ['episodic']"]
    listed = next(r for r in backend.list_memory_resources(ws)["items"] if r["id"] == created["id"])
    assert listed["name"] == "legacy"


def test_create_refuses_a_duplicate_name_and_unknown_strategies(backend, ws):
    backend.create_memory_resource(ws, "dup", "", 30, ["semantic"])
    with pytest.raises(AppError) as exc:
        backend.create_memory_resource(ws, "dup", "", 30, ["semantic"])
    assert exc.value.code == "memory.name_exists" and exc.value.status_code == 409
    with pytest.raises(AppError) as exc:
        backend.create_memory_resource(ws, "bad", "", 30, ["telepathy"])
    assert exc.value.code == "memory.unknown_strategy" and exc.value.status_code == 422


def test_stores_are_workspace_scoped(backend, ws):
    created = backend.create_memory_resource(ws, "mine", "", 30, ["semantic"])
    other = ws_ctx(
        {**SUBSTRATE, "memory_id": "launchpad_memory-acct"}, id="acct", region="cn-northwest-1"
    )
    with pytest.raises(NotFoundError):
        backend.get_memory_resource(other, created["id"])
    assert backend.list_memory_resources(other)["default_id"] == "launchpad_memory-acct"


def test_overview_reports_the_store_strategies_and_actor_count(backend, ws, fakes):
    _seed_turn(fakes)
    _seed_turn(fakes, actor="agent2__x", session="s")
    ov = backend.memory_overview(ws)
    assert (
        ov["configured"] is True
        and ov["memory"]["id"] == STORE
        and ov["memory"]["event_expiry_days"] == 30
    )
    assert [s["strategy_id"] for s in ov["strategies"]] == [
        "semantic",
        "user_preference",
        "summarization",
    ]
    assert ov["actor_count"] == 2 and ov["actor_count_truncated"] is False
    assert ov["other_memories"] == [
        {
            "id": STORE,
            "arn": None,
            "status": "ACTIVE",
            "created_at": None,
            "updated_at": None,
            "is_platform": True,
        }
    ]


def test_new_table_is_scoped_and_migrated():
    from app.core.db import WORKSPACE_SCOPED_TABLES, engine, schema_drift

    assert "memory_stores" in WORKSPACE_SCOPED_TABLES and schema_drift(engine) == {}
    assert store_id_for(ws_ctx({}, id="acct")) == "launchpad_memory-acct"


# ── N17-obs252: the extraction model rides with the message when the role names a row ──────
def test_the_memory_extract_message_carries_the_roles_extraction_model(backend, ws, fakes,
                                                                        monkeypatch):
    """User ruling 2026-10-22 「需要都读模型角色来确定模型」: the `memory_extract` role
    (`Settings.memory_extract_model_id`, r17-am) resolves to its catalog row and travels as
    `extract_model`; without the field, or for an id outside the catalog, the block is omitted
    and the Lambda keeps its env fallback."""
    from types import SimpleNamespace

    real = embedding_backend.get_settings()
    stub = SimpleNamespace(memory_extract_model_id="global.zai.glm-5.3",
                           model_region=real.model_region)
    monkeypatch.setattr(embedding_backend, "get_settings", lambda: stub)
    backend.create_turn_event(ws, ACTOR, "sess-1", "hello", "hi there")
    (_, msg) = fakes["sqs"].sent[-1]
    block = msg["extract_model"]
    assert block["model_id"] == "global.zai.glm-5.3" and block["kind"] == "bedrock"
    assert block["credential_provider"] and block["model_region"] == real.model_region
    assert set(block) == {"model_id", "kind", "base_url", "api_model_name",
                          "credential_provider", "provider", "model_region", "catalog_version",
                          "temperature"}
    # an id the catalog does not know → omitted (the Lambda's env fallback)
    stub.memory_extract_model_id = "nope/unknown"
    backend.create_turn_event(ws, ACTOR, "sess-1", "again", "ok")
    assert "extract_model" not in fakes["sqs"].sent[-1][1]
    # a build without the Settings field at all → omitted too
    monkeypatch.setattr(embedding_backend, "get_settings",
                        lambda: SimpleNamespace(model_region=real.model_region))
    backend.create_turn_event(ws, ACTOR, "sess-1", "third", "ok")
    assert "extract_model" not in fakes["sqs"].sent[-1][1]


def test_the_extract_model_block_carries_the_rows_temperature_capability(monkeypatch):
    """F-LOAD-03 (N17-obs534, ten-student simulation 2026-10-30): the Lambda hard-coded
    `temperature: 0` and Kimi K3 — which takes no temperature field (N12-bug11) — refused every
    extraction with a 400. The block now says whether the row takes the field
    (`ModelRow.temperature`): False for Kimi K3, True for GLM 5.3 (an OpenAI-compatible row its
    trait, True when absent), and the Lambda sends `temperature` only when it is True."""
    from types import SimpleNamespace

    real = embedding_backend.get_settings()
    stub = SimpleNamespace(memory_extract_model_id="global.moonshotai.kimi-k3",
                           model_region=real.model_region)
    monkeypatch.setattr(embedding_backend, "get_settings", lambda: stub)
    kimi = embedding_backend.extract_model_block()
    assert kimi["model_id"] == "global.moonshotai.kimi-k3" and kimi["temperature"] is False
    stub.memory_extract_model_id = "global.zai.glm-5.3"
    glm = embedding_backend.extract_model_block()
    assert glm["model_id"] == "global.zai.glm-5.3" and glm["temperature"] is True
    # the capability is the catalog row's, not a per-provider constant
    assert embedding_backend.model_row("global.moonshotai.kimi-k3").temperature is False
    assert embedding_backend.model_row("global.zai.glm-5.3").temperature is True
