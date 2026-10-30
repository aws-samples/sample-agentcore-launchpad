"""N10v2-bug6: a long-term memory record can be deleted — backend (FakeOpenSearch),
`DELETE /api/memory/records/{record_id}?actor_id=…` and its workspace/actor/owner scoping."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import NotFoundError
from app.core.route_policy import PERM_MEMORY_MANAGE, ROUTE_POLICY
from app.models.ledger import Agent
from app.services import memory_backend
from app.services import opensearch_http as core
from app.services import users as users_service
from app.services.memory_selfhosted import SelfHostedMemoryBackend
from tests.conftest import set_default_resources, ws_ctx
from tests.memory_fakes import FakeEmbedder, FakeOpenSearch, FakeS3, FakeSQS
from tests.test_memory_selfhosted import SUBSTRATE, T0, _seed_record

STORE = SUBSTRATE["memory_id"]
AGENT_ID = "agent0001-aaaa-bbbb-cccc-000000000001"
ACTOR = memory_backend.scoped_actor(AGENT_ID, "river")
OTHER_ACTOR = memory_backend.scoped_actor("agent0002-aaaa-bbbb-cccc-000000000002", "river")
INDEX = core.memory_index("")

ADMIN_CREDS = {"username": "operator", "password": "s3cret-pass"}
MEMBER_CREDS = {"username": "mem-user", "email": "mem-user@acme-corp.com",
                "password": "sufficient-pass"}


@pytest.fixture
def fakes():
    return {"s3": FakeS3(), "opensearch": FakeOpenSearch(), "sqs": FakeSQS(),
            "embedder": FakeEmbedder()}


@pytest.fixture
def backend(client, fakes):
    fakes["opensearch"].ensure_index(INDEX, core.memory_index_body())
    fakes["opensearch"].ensure_index(core.jobs_index(""), core.jobs_index_body())
    impl = SelfHostedMemoryBackend(**fakes, clock=lambda: T0)
    memory_backend.set_backend(impl)
    set_default_resources(SUBSTRATE)
    return impl


@pytest.fixture
def ws():
    return ws_ctx(SUBSTRATE, region="cn-northwest-1", id=DEFAULT_WORKSPACE_ID)


def _seed(fakes):
    _seed_record(fakes, f"/facts/{ACTOR}", "likes tea", actor=ACTOR, rid="r-tea")
    _seed_record(fakes, f"/facts/{ACTOR}", "lives in Xi'an", actor=ACTOR, rid="r-city")
    _seed_record(fakes, f"/facts/{OTHER_ACTOR}", "likes coffee", actor=OTHER_ACTOR, rid="r-coffee")
    doc = dict(fakes["opensearch"].get_doc(INDEX, "r-tea"))
    doc["store_id"] = "launchpad_memory-other-team"
    fakes["opensearch"].put_doc(INDEX, "r-foreign", doc)


# ── backend ────────────────────────────────────────────────────────────────────


def test_delete_record_removes_exactly_that_document(backend, ws, fakes):
    _seed(fakes)
    out = backend.delete_record(ws, "r-tea", ACTOR)
    assert out == {"record_id": "r-tea", "actor_id": ACTOR, "namespace": f"/facts/{ACTOR}",
                   "strategy_id": "semantic", "deleted": True}
    docs = fakes["opensearch"].indices[INDEX]
    assert "r-tea" not in docs
    assert {"r-city", "r-coffee", "r-foreign"} <= set(docs)
    # the listing no longer shows it
    items = backend.list_records(ws, f"/facts/{ACTOR}")["items"]
    assert [r["record_id"] for r in items] == ["r-city"]


def test_delete_record_is_actor_and_store_scoped(backend, ws, fakes):
    _seed(fakes)
    with pytest.raises(NotFoundError):  # another actor's record: 404, never deleted
        backend.delete_record(ws, "r-coffee", ACTOR)
    with pytest.raises(NotFoundError):  # a record of another workspace's store
        backend.delete_record(ws, "r-foreign", ACTOR)
    with pytest.raises(NotFoundError):  # unknown id
        backend.delete_record(ws, "r-nope", ACTOR)
    assert {"r-coffee", "r-foreign"} <= set(fakes["opensearch"].indices[INDEX])


def test_delete_record_needs_the_substrate(backend):
    bare = ws_ctx({"artifacts_bucket": "bkt"})
    with pytest.raises(Exception) as excinfo:
        backend.delete_record(bare, "r-tea", ACTOR)
    assert getattr(excinfo.value, "code", None) == "memory.not_provisioned"


def test_fake_and_real_client_share_the_delete_doc_surface():
    """`delete_doc` lives in the mirrored core (test_memory_core_mirror re-syncs it)."""
    assert callable(core.OpenSearchHttp.delete_doc)
    fake = FakeOpenSearch()
    fake.put_doc("idx", "a", {"x": 1})
    assert fake.delete_doc("idx", "a") is True
    assert fake.delete_doc("idx", "a") is False
    assert fake.get_doc("idx", "a") is None


# ── API (auth disabled → operator identity) ───────────────────────────────────


def test_route_is_gated_on_memory_manage():
    assert ROUTE_POLICY[("DELETE", "/api/memory/records/{record_id}")] == PERM_MEMORY_MANAGE


def test_delete_route_removes_the_record_and_requires_actor_id(client, backend, fakes):
    _seed(fakes)
    res = client.delete("/api/memory/records/r-tea")
    assert res.status_code == 422  # actor scope is mandatory
    res = client.delete("/api/memory/records/r-tea", params={"actor_id": ACTOR})
    assert res.status_code == 200, res.text
    assert res.json()["deleted"] is True and res.json()["record_id"] == "r-tea"
    assert "r-tea" not in fakes["opensearch"].indices[INDEX]
    res = client.delete("/api/memory/records/r-coffee", params={"actor_id": ACTOR})
    assert res.status_code == 404 and res.json()["code"] == "memory.not_found"


def test_delete_route_maps_opensearch_outages_to_503(client, backend, fakes):
    _seed(fakes)

    def boom(*_a, **_k):
        raise core.OpenSearchError(503, "upstream unavailable: never echoed")

    fakes["opensearch"].get_doc = boom
    res = client.delete("/api/memory/records/r-tea", params={"actor_id": ACTOR})
    assert res.status_code == 503 and res.json()["code"] == "memory.backend_unavailable"
    assert "never echoed" not in res.text


# ── owner scoping (auth enabled) ──────────────────────────────────────────────


@pytest.fixture
def gated(monkeypatch, fakes):
    from app.main import create_app

    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN_CREDS["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN_CREDS["password"])
    get_settings.cache_clear()
    app = create_app()
    fakes["opensearch"].ensure_index(INDEX, core.memory_index_body())
    memory_backend.set_backend(SelfHostedMemoryBackend(**fakes, clock=lambda: T0))
    set_default_resources(SUBSTRATE)
    with (
        TestClient(app, client=("127.0.0.1", 4321)) as admin,
        TestClient(app, client=("127.0.0.1", 4321)) as member,
    ):
        assert admin.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        assert member.post("/api/auth/register", json=MEMBER_CREDS).status_code == 201
        db = SessionLocal()
        try:
            user = users_service.find_by_username(db, MEMBER_CREDS["username"])
            user.status = users_service.STATUS_ACTIVE
            user.expires_at = datetime.now(UTC) + timedelta(days=7)
            users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
            db.commit()
        finally:
            db.close()
        login = member.post("/api/auth/login", json={
            "username": MEMBER_CREDS["username"], "password": MEMBER_CREDS["password"]})
        assert login.status_code == 200, login.text
        yield admin, member
    get_settings.cache_clear()


def _agent(creator: str | None) -> None:
    """An agent created by the account named `creator` (its `users.id` is resolved) — or
    by nobody (None: a legacy / preset row), which only an administrator may act on."""
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, creator) if creator else None
        db.add(Agent(id=AGENT_ID, workspace_id=DEFAULT_WORKSPACE_ID, name="owned-agent",
                     method="zip_runtime", status="active",
                     created_by=user.id if user else None, created_by_name=creator,
                     spec={"system_prompt": "x"}))
        db.commit()
    finally:
        db.close()


def test_member_may_delete_only_records_of_agents_they_own(gated, fakes):
    admin, member = gated
    _seed(fakes)
    _agent(creator=None)
    res = member.delete("/api/memory/records/r-tea", params={"actor_id": ACTOR})
    assert res.status_code == 403, res.text
    assert res.json()["code"] == "memory.record_forbidden"
    assert "r-tea" in fakes["opensearch"].indices[INDEX]
    # an actor no agent of this workspace owns is admin-only too
    res = member.delete("/api/memory/records/r-coffee", params={"actor_id": OTHER_ACTOR})
    assert res.status_code == 403
    # the admin is never gated by ownership
    res = admin.delete("/api/memory/records/r-coffee", params={"actor_id": OTHER_ACTOR})
    assert res.status_code == 200, res.text
    assert "r-coffee" not in fakes["opensearch"].indices[INDEX]


def test_the_agents_owner_may_delete_its_records(gated, fakes):
    _, member = gated
    _seed(fakes)
    _agent(creator=MEMBER_CREDS["username"])
    res = member.delete("/api/memory/records/r-tea", params={"actor_id": ACTOR})
    assert res.status_code == 200, res.text
    assert "r-tea" not in fakes["opensearch"].indices[INDEX]


# ── N16-obs114: the delete unlinks its merge-v2 history neighbours ───────────────────────

from tests.property.ledger_invariants import record_is_active, scan_memory_index  # noqa: E402


def _chain_doc(fakes, rid: str, text: str, **fields) -> None:
    _seed_record(fakes, f"/facts/{ACTOR}", text, actor=ACTOR, rid=rid)
    doc = dict(fakes["opensearch"].get_doc(INDEX, rid))
    doc.update({"updated_at": T0.isoformat(), "confidence": 0.9, "last_op": "add", **fields})
    fakes["opensearch"].put_doc(INDEX, rid, doc)


def _docs(fakes) -> dict:
    return {rid: dict(d) for rid, d in fakes["opensearch"].indices[INDEX].items()}


def _seed_chain(fakes) -> None:
    """A → R → C: `A` superseded by `R`, `R` superseded by `C` (the extractor's shape)."""
    _chain_doc(fakes, "r-a", "likes tea", superseded_by="r-r", superseded_at=T0.isoformat())
    _chain_doc(fakes, "r-r", "switched to coffee", supersedes=["r-a"], last_op="supersede",
               superseded_by="r-c", superseded_at=T0.isoformat())
    _chain_doc(fakes, "r-c", "drinks only water now", supersedes=["r-r"], last_op="supersede")
    assert scan_memory_index(_docs(fakes)) == []


def test_deleting_the_chain_head_resurrects_its_predecessor(backend, ws, fakes):
    """head: `R.supersedes = [A]`, nothing supersedes R. The fact that retired A is gone,
    so A's `superseded_by` / `superseded_at` are cleared and A reads active again."""
    _chain_doc(fakes, "r-a", "likes tea", superseded_by="r-r", superseded_at=T0.isoformat())
    _chain_doc(fakes, "r-r", "switched to coffee", supersedes=["r-a"], last_op="supersede")
    backend.delete_record(ws, "r-r", ACTOR)
    docs = _docs(fakes)
    assert "r-r" not in docs
    a = docs["r-a"]
    assert a["superseded_by"] is None and a["superseded_at"] is None
    assert record_is_active(a) and a["text"] == "likes tea"  # history kept, link gone
    assert scan_memory_index(docs) == []
    assert [r["record_id"] for r in backend.list_records(ws, f"/facts/{ACTOR}")["items"]] == ["r-a"]


def test_deleting_a_middle_record_rechains_its_predecessor_to_its_successor(backend, ws, fakes):
    """middle: A → R → C. A stays retired, now by C; C supersedes A instead of R."""
    _seed_chain(fakes)
    backend.delete_record(ws, "r-r", ACTOR)
    docs = _docs(fakes)
    assert "r-r" not in docs
    assert docs["r-a"]["superseded_by"] == "r-c"
    assert docs["r-a"]["superseded_at"] == T0.isoformat()  # kept: A was retired then
    assert not record_is_active(docs["r-a"])
    assert docs["r-c"]["supersedes"] == ["r-a"] and record_is_active(docs["r-c"])
    assert scan_memory_index(docs) == []


def test_deleting_the_chain_tail_drops_it_from_the_successors_supersedes(backend, ws, fakes):
    """tail: A → R → C, delete A (the retired original). R keeps everything else."""
    _seed_chain(fakes)
    backend.delete_record(ws, "r-a", ACTOR)
    docs = _docs(fakes)
    assert "r-a" not in docs
    assert docs["r-r"]["supersedes"] == [] and docs["r-r"]["superseded_by"] == "r-c"
    assert docs["r-c"]["supersedes"] == ["r-r"]
    assert scan_memory_index(docs) == []


def test_deleting_a_consolidate_record_frees_only_its_newest_member(backend, ws, fakes):
    """A consolidate record supersedes several members that are near-duplicates by
    construction (here: identical embeddings). Deleting it (no successor) frees exactly ONE —
    the newest by `record_timestamp` — and re-points the others at it, so the merge-v2
    dedupe invariant (no two ACTIVE records at cosine ≥ 0.92) still holds (rev-r16 ruling)."""
    same = fakes["embedder"].embed("a tea person")
    _chain_doc(fakes, "r-m1", "likes tea", superseded_by="r-k", superseded_at=T0.isoformat(),
               updated_at=(T0 - timedelta(days=2)).isoformat(), embedding=same)
    _chain_doc(fakes, "r-m2", "drinks tea at four", superseded_by="r-k",
               superseded_at=T0.isoformat(), updated_at=(T0 - timedelta(days=1)).isoformat(),
               embedding=same)
    _chain_doc(fakes, "r-m3", "tea, always", superseded_by="r-k", superseded_at=T0.isoformat(),
               updated_at=(T0 - timedelta(days=3)).isoformat(), embedding=same)
    _chain_doc(fakes, "r-k", "a tea person", supersedes=["r-m1", "r-m2", "r-m3"],
               last_op="consolidate", embedding=same)
    backend.delete_record(ws, "r-k", ACTOR)
    docs = _docs(fakes)
    assert "r-k" not in docs
    newest = docs["r-m2"]
    assert newest["superseded_by"] is None and newest["superseded_at"] is None
    assert record_is_active(newest) and set(newest["supersedes"]) == {"r-m1", "r-m3"}
    for rid in ("r-m1", "r-m3"):
        assert docs[rid]["superseded_by"] == "r-m2" and not record_is_active(docs[rid])
    listed = backend.list_records(ws, f"/facts/{ACTOR}")["items"]
    assert [r["record_id"] for r in listed] == ["r-m2"]
    assert scan_memory_index(docs) == []


def test_an_already_dangling_neighbour_does_not_break_the_delete(backend, ws, fakes):
    """A pre-fix ledger: R's successor id no longer resolves and one member is gone. The
    delete still succeeds and the surviving member is treated as a head's predecessor."""
    _chain_doc(fakes, "r-a", "likes tea", superseded_by="r-r", superseded_at=T0.isoformat())
    _chain_doc(fakes, "r-r", "switched to coffee", supersedes=["r-a", "r-gone"],
               last_op="supersede", superseded_by="r-vanished", superseded_at=T0.isoformat())
    out = backend.delete_record(ws, "r-r", ACTOR)
    assert out["deleted"] is True
    docs = _docs(fakes)
    assert "r-r" not in docs and docs["r-a"]["superseded_by"] is None
    assert scan_memory_index(docs) == []


def _delete_doc_fails(fakes, exc: BaseException) -> list[str]:
    calls: list[str] = []
    inner = fakes["opensearch"].delete_doc

    def boom(index, doc_id, refresh=True):
        calls.append(doc_id)
        raise exc

    fakes["opensearch"].delete_doc = boom
    assert inner is not boom
    return calls


@pytest.mark.parametrize("seed", ["head", "middle", "consolidate"])
def test_a_failed_delete_relinks_the_neighbours_it_had_unlinked(backend, ws, fakes, seed):
    """N17-obs91 (fixed in cn/r17-n): `_unlink_history` runs before `delete_doc`; when the
    delete then fails (the recorded 503) every neighbour's link fields go back to what they
    were — for a chain head the revived predecessor is retired again next to the head that
    retired it. The call answers the mapped outage and the index is exactly as it was."""
    same = fakes["embedder"].embed("a tea person")
    if seed == "head":
        _chain_doc(fakes, "r-a", "likes tea", superseded_by="r-r", superseded_at=T0.isoformat())
        _chain_doc(fakes, "r-r", "switched to coffee", supersedes=["r-a"], last_op="supersede")
        victim = "r-r"
    elif seed == "middle":
        _seed_chain(fakes)
        victim = "r-r"
    else:
        for i, rid in enumerate(("r-m1", "r-m2", "r-m3")):
            _chain_doc(fakes, rid, f"tea {i}", superseded_by="r-k", superseded_at=T0.isoformat(),
                       updated_at=(T0 - timedelta(days=i)).isoformat(), embedding=same)
        _chain_doc(fakes, "r-k", "a tea person", supersedes=["r-m1", "r-m2", "r-m3"],
                   last_op="consolidate", embedding=same)
        victim = "r-k"
    before = _docs(fakes)
    calls = _delete_doc_fails(fakes, core.OpenSearchError(503, "no master: never echoed"))
    with pytest.raises(core.OpenSearchError):
        backend.delete_record(ws, victim, ACTOR)
    assert calls == [victim]
    assert _docs(fakes) == before, "a failed delete changed the neighbours"
    assert scan_memory_index(_docs(fakes)) == []


def test_a_failed_delete_of_the_head_answers_503_and_keeps_the_predecessor_retired(
    client, backend, fakes,
):
    _chain_doc(fakes, "r-a", "likes tea", superseded_by="r-r", superseded_at=T0.isoformat())
    _chain_doc(fakes, "r-r", "switched to coffee", supersedes=["r-a"], last_op="supersede")
    _delete_doc_fails(fakes, core.OpenSearchError(503, "no master: never echoed"))
    res = client.delete("/api/memory/records/r-r", params={"actor_id": ACTOR})
    assert res.status_code == 503 and res.json()["code"] == "memory.backend_unavailable"
    assert "never echoed" not in res.text
    docs = _docs(fakes)
    assert "r-r" in docs and docs["r-a"]["superseded_by"] == "r-r"
    assert not record_is_active(docs["r-a"])
    listed = backend.list_records(ws_ctx(SUBSTRATE, region="cn-northwest-1",
                                         id=DEFAULT_WORKSPACE_ID), f"/facts/{ACTOR}")["items"]
    assert [r["record_id"] for r in listed] == ["r-r"]  # the replaced fact is not back


def test_a_relink_that_fails_too_is_logged_and_the_delete_error_propagates(
    backend, ws, fakes, caplog,
):
    """Domain-wide outage: the re-link's `update_doc` fails as well. The original delete
    failure is what the caller sees; the neighbour is named in one WARNING line (id + type,
    never the body) and the member's retry re-runs the idempotent unlink."""
    import logging

    _chain_doc(fakes, "r-a", "likes tea", superseded_by="r-r", superseded_at=T0.isoformat())
    _chain_doc(fakes, "r-r", "switched to coffee", supersedes=["r-a"], last_op="supersede")
    _delete_doc_fails(fakes, core.OpenSearchError(503, "no master: never echoed"))
    seen: list[str] = []

    def put_doc(index, doc_id, doc, refresh=True):  # the re-link's write
        seen.append(doc_id)
        raise core.OpenSearchError(503, "still no master: never echoed")

    fakes["opensearch"].put_doc = put_doc
    with caplog.at_level(logging.WARNING, logger="launchpad.memory"):
        with pytest.raises(core.OpenSearchError, match="^OpenSearch HTTP 503: no master"):
            backend.delete_record(ws, "r-r", ACTOR)
    assert seen == ["r-a"]
    assert any("neighbour 'r-a' could not be re-linked (OpenSearchError)" in r.message
               for r in caplog.records)
    assert "never echoed" not in caplog.text


def test_a_record_outside_any_chain_touches_no_other_document(backend, ws, fakes):
    _seed(fakes)
    before = _docs(fakes)
    backend.delete_record(ws, "r-tea", ACTOR)
    after = _docs(fakes)
    del before["r-tea"]
    assert after == before


def test_delete_route_of_a_superseded_record_leaves_the_index_consistent(client, backend, fakes):
    """The console path (N16-obs114's reproduction): DELETE of the retired record → 200,
    the newer record no longer names it, `include_superseded` listings are clean."""
    _seed_chain(fakes)
    res = client.delete("/api/memory/records/r-a", params={"actor_id": ACTOR})
    assert res.status_code == 200, res.text
    assert res.json()["deleted"] is True
    assert scan_memory_index(_docs(fakes)) == []
    listed = client.get("/api/memory/records",
                        params={"actor_id": ACTOR, "include_superseded": "true"}).json()["items"]
    by_id = {r["record_id"]: r for r in listed}
    assert "r-a" not in by_id and by_id["r-r"]["supersedes"] == []
