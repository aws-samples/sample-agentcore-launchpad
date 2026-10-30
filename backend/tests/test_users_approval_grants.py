"""CN extra (Family B step 1): approving a pending account may grant its workspaces in
the same `PATCH /api/users/{id}` — `{"status": "active", "workspace_ids": [...]}` — so a
freshly approved member never sits with an account and nothing to open. `workspace_ids`
is the same full replacement as `workspaces` (one write path for grants); sending both
is 422, an unknown workspace id is refused like it always was.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import Workspace
from app.services import users as users_service

ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)
PENDING = {"username": "newbie", "email": "newbie@acme-corp.com", "password": "sufficient-pass"}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_REGISTRATION_REQUIRE_APPROVAL", "true")
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


@pytest.fixture
def admin(app):
    with TestClient(app, client=PEER) as client:
        assert client.post("/api/auth/login", json=ADMIN).status_code == 200
        yield client


def _pending(app) -> str:
    anon = TestClient(app, client=PEER)
    assert anon.post("/api/auth/register", json=PENDING).status_code == 201
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, PENDING["username"])
        assert user.status == users_service.STATUS_PENDING
        return user.id
    finally:
        db.close()


def _west() -> str:
    db = SessionLocal()
    try:
        db.add(Workspace(id="west", name="West", account_id="444455556666",
                         region="cn-north-1", bootstrap_status="ready"))
        db.commit()
    finally:
        db.close()
    return "west"


def test_approval_grants_workspaces_in_the_same_call(app, admin):
    user_id = _pending(app)
    west = _west()
    res = admin.patch(
        f"/api/users/{user_id}",
        json={"status": "active", "workspace_ids": [DEFAULT_WORKSPACE_ID, west]},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "active" and body["expires_at"] is not None
    assert sorted(body["workspaces"]) == [DEFAULT_WORKSPACE_ID, west]
    # the member can now open both environments
    member = TestClient(app, client=PEER)
    assert member.post("/api/auth/login", json={
        "username": PENDING["username"], "password": PENDING["password"]}).status_code == 200
    for workspace in (DEFAULT_WORKSPACE_ID, west):
        listed = member.get("/api/apikeys", headers={"X-Workspace": workspace})
        assert listed.status_code == 200, (workspace, listed.text)


def test_workspace_ids_is_the_same_replacement_as_workspaces(app, admin):
    user_id = _pending(app)
    west = _west()
    assert admin.patch(f"/api/users/{user_id}",
                       json={"status": "active", "workspace_ids": [west]}).status_code == 200
    # a later replacement through either name replaces the whole set
    res = admin.patch(f"/api/users/{user_id}", json={"workspace_ids": [DEFAULT_WORKSPACE_ID]})
    assert res.status_code == 200 and res.json()["workspaces"] == [DEFAULT_WORKSPACE_ID]
    res = admin.patch(f"/api/users/{user_id}", json={"workspaces": [west]})
    assert res.status_code == 200 and res.json()["workspaces"] == [west]
    res = admin.patch(f"/api/users/{user_id}", json={"workspace_ids": None})
    assert res.status_code == 200 and res.json()["workspaces"] == []


def test_both_grant_fields_at_once_and_unknown_ids_are_refused(app, admin):
    user_id = _pending(app)
    res = admin.patch(
        f"/api/users/{user_id}",
        json={"status": "active", "workspaces": [DEFAULT_WORKSPACE_ID], "workspace_ids": []},
    )
    assert res.status_code == 422, res.text
    res = admin.patch(f"/api/users/{user_id}", json={"status": "active", "workspace_ids": ["nope"]})
    assert res.status_code == 400, res.text
    assert res.json()["code"] == "users.invalid_workspaces"
    db = SessionLocal()
    try:
        assert users_service.get_user(db, user_id).status == users_service.STATUS_PENDING
    finally:
        db.close()
