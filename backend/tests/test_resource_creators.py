"""Who created each user-managed resource (`app.services.creators`, upstream #261 ported
as Family B step 1).

Every in-scope create path records the calling principal as `created_by` (`users.id`,
NULL for principals without a `users` row) + `created_by_name` (username, display only);
list and detail responses return both. Legacy rows are never backfilled, and a client
can never set the creator. In the CN build every kind is ledger-backed, so the
`resource_creators` side table holds no rows — its API is kept (and tested) for parity.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalDataset, EvalEvaluator, EvalPipeline
from app.main import create_app
from app.models.ledger import ChatSession, ManagedMemory, MemoryStore, ResourceCreator
from app.routers.auth import Identity
from app.services import chat_ledger, creators, memory_ownership
from app.services import users as users_service
from app.services.creators import Creator
from tests.conftest import ws_ctx
from tests.evaluation.test_evaluator_crud import FIVE_POINT_SCALE

ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)
FORGED = {"created_by": "u-forged", "created_by_name": "mallory"}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


@pytest.fixture
def admin(app):
    with TestClient(app, client=PEER) as client:
        assert client.post("/api/auth/login", json=ADMIN).status_code == 200
        yield client


def _member(app, username: str) -> tuple[str, TestClient]:
    """An approved member on the default workspace, and a logged-in client for it."""
    password = "sufficient-pass"
    client = TestClient(app, client=PEER)
    registered = client.post(
        "/api/auth/register",
        json={"username": username, "email": f"{username}@acme-corp.com", "password": password},
    )
    assert registered.status_code == 201, registered.text
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, username)
        user.status = users_service.STATUS_ACTIVE
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
        db.commit()
        user_id = user.id
    finally:
        db.close()
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return user_id, client


@pytest.fixture
def ada(app, admin):
    """A member (`users.id`, client)."""
    return _member(app, "ada")


def _creator(body: dict) -> tuple:
    return body["created_by"], body["created_by_name"]


# ─── the helper itself ───────────────────────────────────────────────────────


def test_stamp_and_fields_round_trip():
    row = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="d", items=[])
    assert creators.fields(row) == {"created_by": None, "created_by_name": None}
    creators.stamp(row, None)  # no human principal: nothing is recorded
    assert creators.fields(row) == {"created_by": None, "created_by_name": None}
    creators.stamp(row, Creator("u1", "ada"))
    assert creators.fields(row) == {"created_by": "u1", "created_by_name": "ada"}
    assert creators.fields(None) == creators.empty()
    assert creators.fields(Creator(None, "operator")) == {
        "created_by": None, "created_by_name": "operator",
    }


def test_creator_of_an_identity_and_ownership():
    member = Identity(username="ada", role="member", user_id="u1")
    assert Creator.of(member) == Creator("u1", "ada")
    assert Creator.of(Identity(username="operator", role="admin")) == Creator(None, "operator")
    assert Creator.of(None) is None
    row = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="d", items=[])
    creators.stamp(row, Creator("u1", "ada"))
    assert Creator.of(member).owns(row) is True
    assert Creator("u2", "bob").owns(row) is False
    # a principal without a row never owns anything by id — even a NULL-creator row
    legacy = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="legacy", items=[])
    assert Creator(None, "operator").owns(legacy) is False


def test_reject_client_creator_names_the_fields():
    from app.core.errors import AppError

    creators.reject_client_creator(None)
    creators.reject_client_creator({"name": "x"})
    with pytest.raises(AppError) as exc:
        creators.reject_client_creator({"name": "x", **FORGED})
    assert exc.value.code == "validation.creator_not_settable" and exc.value.status_code == 422
    assert exc.value.detail == {"fields": ["created_by", "created_by_name"]}


def test_record_lookup_forget_are_workspace_and_kind_scoped():
    db = SessionLocal()
    try:
        creators.record(db, "ws-a", creators.KB, "KB1", Creator("u1", "ada"))
        creators.record(db, "ws-a", creators.KB, "KB1", Creator("u2", "bob"))  # idempotent
        creators.record(db, "ws-a", creators.EVALUATOR, "KB1", Creator("u3", "cy"))
        creators.record(db, "ws-b", creators.KB, "KB1", Creator("u4", "dee"))
        creators.record(db, "ws-a", creators.KB, "", Creator("u5", "eve"))  # no id: nothing
        creators.record(db, "ws-a", creators.KB, "KB2", None)  # no principal: nothing

        assert creators.lookup(db, "ws-a", creators.KB, ["KB1", "KB2", ""]) == {
            "KB1": {"created_by": "u1", "created_by_name": "ada"},
        }
        assert creators.lookup_one(db, "ws-a", creators.EVALUATOR, "KB1") == {
            "created_by": "u3", "created_by_name": "cy",
        }
        assert creators.lookup_one(db, "ws-b", creators.KB, "KB1")["created_by"] == "u4"
        assert creators.lookup_one(db, "ws-a", creators.KB, "KB2") == creators.empty()
        assert db.query(ResourceCreator).count() == 3

        creators.forget(db, "ws-a", creators.KB, "KB1")
        creators.forget(db, "ws-a", creators.KB, "KB1")  # already gone: no error
        assert creators.lookup_one(db, "ws-a", creators.KB, "KB1") == creators.empty()
        assert creators.lookup_one(db, "ws-b", creators.KB, "KB1")["created_by"] == "u4"
    finally:
        db.close()


def test_resource_creators_is_workspace_scoped():
    from app.core.db import WORKSPACE_SCOPED_TABLES

    assert "resource_creators" in WORKSPACE_SCOPED_TABLES


# ─── console create paths record the caller ───────────────────────────────────


def test_a_member_dataset_records_the_member(ada):
    user_id, client = ada
    res = client.post("/api/eval/datasets", json={"name": "golden", "items": [{"prompt": "hi"}]})
    assert res.status_code == 201, res.text
    assert _creator(res.json()) == (user_id, "ada")
    listed = client.get("/api/eval/datasets").json()["datasets"]
    assert [_creator(d) for d in listed] == [(user_id, "ada")]
    db = SessionLocal()
    try:
        row = db.get(EvalDataset, res.json()["id"])
        assert (row.created_by, row.created_by_name) == (user_id, "ada")
    finally:
        db.close()


def test_a_posted_creator_is_never_honoured(ada):
    """Request models that are open ignore the keys; closed ones (agents, keys, KBs)
    refuse with 422 — either way the server-derived creator wins."""
    user_id, client = ada
    res = client.post(
        "/api/eval/datasets", json={"name": "forged", "items": [{"prompt": "hi"}], **FORGED}
    )
    assert res.status_code == 201, res.text
    assert _creator(res.json()) == (user_id, "ada")


def test_the_config_admin_records_a_name_only(admin):
    res = admin.post("/api/eval/datasets", json={"name": "ops", "items": [{"prompt": "hi"}]})
    assert res.status_code == 201, res.text
    assert _creator(res.json()) == (None, ADMIN["username"])


def test_the_auth_disabled_operator_records_a_name_only(client):
    res = client.post("/api/eval/datasets", json={"name": "local", "items": [{"prompt": "hi"}]})
    assert res.status_code == 201, res.text
    assert _creator(res.json()) == (None, get_settings().auth_username)


def test_a_legacy_row_lists_no_creator(client):
    db = SessionLocal()
    try:
        db.add(EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="old", items=[{"prompt": "x"}]))
        db.add(EvalPipeline(workspace_id=DEFAULT_WORKSPACE_ID, name="old-p",
                            config={"output": {"dataset_name": "o"}}))
        db.commit()
    finally:
        db.close()
    (dataset,) = client.get("/api/eval/datasets").json()["datasets"]
    assert _creator(dataset) == (None, None)
    (pipeline,) = client.get("/api/eval/pipelines").json()["pipelines"]
    assert _creator(pipeline) == (None, None)


def test_a_member_pipeline_records_the_member(ada):
    user_id, client = ada
    res = client.post("/api/eval/pipelines", json={
        "name": "hr trajectories",
        "source": {"agent": "hr-bot", "range": "24h", "status": "all", "max_sessions": 10},
        "processing": {"first_turn_only": True},
        "output": {"dataset_name": "hr-regression"},
    })
    assert res.status_code == 201, res.text
    assert _creator(res.json()) == (user_id, "ada")
    assert _creator(client.get(f"/api/eval/pipelines/{res.json()['id']}").json()) == (
        user_id, "ada",
    )


def test_a_member_evaluator_records_the_member_in_every_shape(ada):
    user_id, client = ada
    res = client.post("/api/eval/evaluators", json={
        "name": "my_judge", "instructions": "Rate the tone of {context}", "level": "SESSION",
        "description": "checks tone", "rating_scale": FIVE_POINT_SCALE,
    })
    assert res.status_code == 201, res.text
    assert _creator(res.json()) == (user_id, "ada")
    evaluator_id = res.json()["evaluator_id"]
    assert _creator(client.get(f"/api/eval/evaluators/{evaluator_id}").json()) == (user_id, "ada")
    rows = {r["id"]: r for r in client.get("/api/eval/evaluators").json()["evaluators"]}
    assert _creator(rows[evaluator_id]) == (user_id, "ada")
    # builtins have no creator
    assert _creator(rows["Builtin.Helpfulness"]) == (None, None)
    assert _creator(client.get("/api/eval/evaluators/Builtin.Helpfulness").json()) == (None, None)
    db = SessionLocal()
    try:
        row = db.get(EvalEvaluator, evaluator_id)
        assert (row.created_by, row.created_by_name) == (user_id, "ada")
    finally:
        db.close()


# ─── memory ownership: created vs adopted, and the N6 store mirror ─────────────


def test_a_created_memory_records_its_creator_and_an_adopted_one_only_a_name():
    ws = ws_ctx()  # the default workspace (`id` defaults to "default")
    db = SessionLocal()
    try:
        db.add(MemoryStore(workspace_id=DEFAULT_WORKSPACE_ID, memory_id="mem-created",
                           name="created", strategies=[], namespace_keys=[]))
        db.commit()
        memory_ownership.register(
            db, ws, "mem-created", origin="created", creator=Creator("u1", "ada")
        )
        memory_ownership.register(
            db, ws, "mem-adopted", origin="adopted", creator=Creator("u2", "operator")
        )
        memory_ownership.register(db, ws, "mem-system", origin="created", creator=None)
        memory_ownership.register(  # idempotent: the first writer's creator stays
            db, ws, "mem-created", origin="created", creator=Creator("u9", "zed")
        )
        rows = memory_ownership.managed_rows(db, ws)
        assert creators.fields(rows["mem-created"]) == {
            "created_by": "u1", "created_by_name": "ada",
        }
        assert creators.fields(rows["mem-adopted"]) == {
            "created_by": None, "created_by_name": "operator",
        }
        assert creators.fields(rows["mem-system"]) == creators.empty()
        store = db.query(MemoryStore).filter_by(memory_id="mem-created").one()
        assert (store.created_by, store.created_by_name) == ("u1", "ada")
        assert db.query(ManagedMemory).count() == 3
    finally:
        db.close()


# ─── chat sessions (CN extra): the opener is recorded once ────────────────────


def test_a_chat_session_records_its_opener_once():
    chat_ledger.track_session(DEFAULT_WORKSPACE_ID, "ag1", "s1", "ada",
                              creator=Creator("u1", "ada"))
    chat_ledger.track_session(DEFAULT_WORKSPACE_ID, "ag1", "s1", "ada",
                              creator=Creator("u2", "bob"))  # a later turn by someone else
    chat_ledger.track_session(DEFAULT_WORKSPACE_ID, "ag1", "s2", "api")  # legacy / no creator
    db = SessionLocal()
    try:
        rows = {r.session_id: r for r in db.query(ChatSession).all()}
        assert (rows["s1"].created_by, rows["s1"].created_by_name, rows["s1"].turns) == (
            "u1", "ada", 2,
        )
        assert (rows["s2"].created_by, rows["s2"].created_by_name) == (None, None)
    finally:
        db.close()
