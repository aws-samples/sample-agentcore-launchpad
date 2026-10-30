"""API keys: the creator is recorded by account id and the member rule (Family B step 1).

N10v2-obs11 recorded the minting USERNAME and made DELETE owner-or-admin. Upstream #259 /
#263 (ported as Family B step 1, user decision 2026-10-16) re-point that to the account:
`created_by` = the minting `users.id`, `created_by_name` = the username at mint time, and a
member lists, toggles and deletes ONLY the keys they minted — another member's key reads
like a missing one (404 `apikey.not_found`, never 403: a key id is not probeable).
Administrators reach every key of the workspace; a legacy NULL-creator key is theirs.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import ApiKey
from app.services import users as users_service

ADMIN_CREDS = {"username": "operator", "password": "s3cret-pass"}
MEMBER_CREDS = {"username": "key-owner", "email": "key-owner@acme-corp.com",
                "password": "sufficient-pass"}
OTHER_CREDS = {"username": "key-other", "email": "key-other@acme-corp.com",
               "password": "sufficient-pass"}


@pytest.fixture
def gated_app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN_CREDS["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN_CREDS["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def _member(app, creds) -> TestClient:
    client = TestClient(app, client=("127.0.0.1", 4321))
    assert client.post("/api/auth/register", json=creds).status_code == 201
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, creds["username"])
        user.status = users_service.STATUS_ACTIVE
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
        db.commit()
    finally:
        db.close()
    login = client.post("/api/auth/login",
                        json={"username": creds["username"], "password": creds["password"]})
    assert login.status_code == 200, login.text
    return client


def _user_id(username: str) -> str:
    db = SessionLocal()
    try:
        return users_service.find_by_username(db, username).id
    finally:
        db.close()


@pytest.fixture
def sessions(gated_app):
    with TestClient(gated_app, client=("127.0.0.1", 4321)) as admin:
        assert admin.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        yield admin, _member(gated_app, MEMBER_CREDS), _member(gated_app, OTHER_CREDS)


def test_the_creator_is_recorded_by_account_id_and_name(sessions):
    admin, member, _ = sessions
    minted = member.post("/api/apikeys", json={"name": "mine"})
    assert minted.status_code == 201, minted.text
    assert minted.json()["created_by"] == _user_id(MEMBER_CREDS["username"])
    assert minted.json()["created_by_name"] == MEMBER_CREDS["username"]
    ops = admin.post("/api/apikeys", json={"name": "ops"}).json()
    # the config admin has no `users` row: name only, never offboarded
    assert ops["created_by"] is None and ops["created_by_name"] == "operator"
    listed = {k["name"]: k["created_by_name"] for k in admin.get("/api/apikeys").json()["keys"]}
    assert listed == {"mine": "key-owner", "ops": "operator"}


def test_the_creator_is_never_client_settable(sessions):
    _, member, _ = sessions
    for forged in ({"created_by": "u-forged"}, {"created_by_name": "mallory"}):
        res = member.post("/api/apikeys", json={"name": "forged", **forged})
        assert res.status_code == 422, res.text
    assert member.get("/api/apikeys").json()["keys"] == []


def test_members_list_and_manage_only_the_keys_they_minted(sessions):
    """The CN member rule: another member's key is invisible — list, toggle and delete
    all answer as if it did not exist — while the administrator reaches every key."""
    admin, member, other = sessions
    mine = member.post("/api/apikeys", json={"name": "mine"}).json()
    theirs = other.post("/api/apikeys", json={"name": "theirs"}).json()

    assert [k["id"] for k in member.get("/api/apikeys").json()["keys"]] == [mine["id"]]
    assert [k["id"] for k in other.get("/api/apikeys").json()["keys"]] == [theirs["id"]]
    assert {k["id"] for k in admin.get("/api/apikeys").json()["keys"]} == {mine["id"], theirs["id"]}

    for route in (f"/api/apikeys/{theirs['id']}/disable", f"/api/apikeys/{theirs['id']}/enable"):
        denied = member.post(route)
        assert denied.status_code == 404, denied.text
        assert denied.json()["code"] == "apikey.not_found"
    denied = member.delete(f"/api/apikeys/{theirs['id']}")
    assert denied.status_code == 404 and denied.json()["code"] == "apikey.not_found"
    assert other.get("/v1/agents", headers={"X-Api-Key": theirs["key"]}).status_code == 200

    # the administrator toggles and deletes any member's key
    assert admin.post(f"/api/apikeys/{theirs['id']}/disable").json()["enabled"] is False
    assert admin.post(f"/api/apikeys/{theirs['id']}/enable").json()["enabled"] is True
    assert admin.delete(f"/api/apikeys/{theirs['id']}").status_code == 200


def test_delete_removes_the_row_for_good(sessions):
    admin, member, _ = sessions
    minted = member.post("/api/apikeys", json={"name": "mine"}).json()
    key_id, raw = minted["id"], minted["key"]
    assert member.get("/v1/agents", headers={"X-Api-Key": raw}).status_code == 200

    gone = member.delete(f"/api/apikeys/{key_id}")
    assert gone.status_code == 200 and gone.json() == {"deleted": True, "key_id": key_id}
    assert member.get("/api/apikeys").json()["keys"] == []  # the row is gone, not disabled
    # the plaintext a client still holds no longer authenticates on the public API
    assert member.get("/v1/agents", headers={"X-Api-Key": raw}).status_code == 401
    assert member.delete(f"/api/apikeys/{key_id}").status_code == 404

    theirs = member.post("/api/apikeys", json={"name": "theirs"}).json()["id"]
    assert admin.delete(f"/api/apikeys/{theirs}").status_code == 200


def test_a_legacy_key_without_a_creator_is_admin_only(sessions):
    """Rows minted before creators were recorded carry NULL: no member can see or claim
    them (404), the administrator lists and deletes them."""
    admin, member, _ = sessions
    db = SessionLocal()
    legacy = ApiKey(workspace_id=DEFAULT_WORKSPACE_ID, name="legacy", prefix="lp_live_old…",
                    key_hash="0" * 64, created_by=None, created_by_name=None)
    db.add(legacy)
    db.commit()
    key_id = legacy.id
    db.close()
    assert member.get("/api/apikeys").json()["keys"] == []
    denied = member.delete(f"/api/apikeys/{key_id}")
    assert denied.status_code == 404 and denied.json()["code"] == "apikey.not_found"
    listed = {k["id"]: k for k in admin.get("/api/apikeys").json()["keys"]}
    assert listed[key_id]["created_by"] is None and listed[key_id]["created_by_name"] is None
    assert listed[key_id]["creator_offboarded"] is None
    assert admin.delete(f"/api/apikeys/{key_id}").status_code == 200


def test_another_workspaces_key_is_not_found(client):
    db = SessionLocal()
    foreign = ApiKey(workspace_id="other-ws", name="x", prefix="lp_live_x…", key_hash="1" * 64)
    db.add(foreign)
    db.commit()
    key_id = foreign.id
    db.close()
    res = client.delete(f"/api/apikeys/{key_id}")
    assert res.status_code == 404 and res.json()["code"] == "apikey.not_found"


def test_an_optional_label_is_recorded_and_listed(client):
    """N-SCN-obs4: keys are workspace-wide and outlive their agent by design; the
    Chat → API panel records the agent it minted a key from as a display label.
    Additive — a key without a label lists `label: null`; the label never scopes `/v1`."""
    plain = client.post("/api/apikeys", json={"name": "plain"})
    assert plain.status_code == 201, plain.text
    assert plain.json()["label"] is None

    labelled = client.post("/api/apikeys", json={"name": "hr", "label": "  HR assistant  "})
    assert labelled.status_code == 201, labelled.text
    assert labelled.json()["label"] == "HR assistant"  # trimmed
    blank = client.post("/api/apikeys", json={"name": "blank", "label": "   "})
    assert blank.json()["label"] is None  # a blank label is no label

    listed = {k["name"]: k["label"] for k in client.get("/api/apikeys").json()["keys"]}
    assert listed == {"plain": None, "hr": "HR assistant", "blank": None}

    too_long = client.post("/api/apikeys", json={"name": "x", "label": "y" * 121})
    assert too_long.status_code == 422

    # the label is display only: the key authenticates every agent list exactly as before
    raw = labelled.json()["key"]
    assert client.get("/v1/agents", headers={"X-Api-Key": raw}).status_code == 200
