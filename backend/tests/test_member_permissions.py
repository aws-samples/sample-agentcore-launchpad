"""Member agent-management permissions: default-granted, per-user revocable.

The authorization outcome is asserted without executing any deploy: a granted
member is proven past the gate by the handler's own 422/404 (validation runs
after the route-policy dependency), never by a real state change.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import create_app
from app.services import users as users_service

ADMIN_CREDS = {"username": "operator", "password": "s3cret-pass"}
MEMBER_CREDS = {
    "username": "perm-user",
    "email": "perm-user@acme-corp.com",
    "password": "sufficient-pass",
}

# One safe probe per permission: (permission, method, path, json, expected
# status for a granted caller). 422/404 prove authorization passed while the
# handler refused the probe input before touching AWS.
PROBES = [
    ("agents.deploy", "POST", "/api/agents", {}, 422),
    ("agents.deploy", "POST", "/api/agents/no-such-agent/redeploy", {}, 422),
    ("agents.import", "POST", "/api/agents/discovery/import", {}, 422),
    ("agents.delete", "DELETE", "/api/agents/no-such-agent", None, 404),
    ("agents.convert", "POST", "/api/agents/no-such-agent/convert", None, 404),
    ("eval.run", "POST", "/api/eval/runs", {}, 422),
]


@pytest.fixture
def gated_app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN_CREDS["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN_CREDS["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


@pytest.fixture
def sessions(gated_app):
    """(admin client, member client, member user id) with live sessions."""
    with (
        TestClient(gated_app, client=("127.0.0.1", 4321)) as admin,
        TestClient(gated_app, client=("127.0.0.1", 4321)) as member,
    ):
        assert admin.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        assert member.post("/api/auth/register", json=MEMBER_CREDS).status_code == 201
        db = SessionLocal()
        try:
            user = users_service.find_by_username(db, MEMBER_CREDS["username"])
            assert user is not None
            user.status = users_service.STATUS_ACTIVE
            user.expires_at = datetime.now(UTC) + timedelta(days=7)
            db.commit()
            user_id = user.id
        finally:
            db.close()
        login = member.post(
            "/api/auth/login",
            json={
                "username": MEMBER_CREDS["username"],
                "password": MEMBER_CREDS["password"],
            },
        )
        assert login.status_code == 200, login.text
        yield admin, member, user_id


def _request(client, method, path, body):
    return client.request(method, path, json=body)


def test_member_holds_every_agent_permission_by_default(sessions):
    _, member, _ = sessions
    status = member.get("/api/auth/status").json()
    assert status["permissions"] == sorted(users_service.AGENT_PERMISSIONS)
    for _, method, path, body, expected in PROBES:
        response = _request(member, method, path, body)
        assert response.status_code == expected, (method, path, response.text)


@pytest.mark.parametrize(("permission", "method", "path", "body", "expected"), PROBES)
def test_revoking_one_permission_denies_only_that_surface(
    sessions, permission, method, path, body, expected
):
    admin, member, user_id = sessions
    patched = admin.patch(
        f"/api/users/{user_id}", json={"permissions": {permission: False}}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["permissions"][permission] is False

    denied = _request(member, method, path, body)
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "auth.permission_required"
    assert denied.json()["detail"]["permission"] == permission

    # takes effect per request, without re-login; every other surface still works
    assert permission not in member.get("/api/auth/status").json()["permissions"]
    for other_permission, o_method, o_path, o_body, o_expected in PROBES:
        if other_permission != permission:
            response = _request(member, o_method, o_path, o_body)
            assert response.status_code == o_expected, (o_method, o_path, response.text)

    # re-granting restores the default without leaving a stored override
    restored = admin.patch(
        f"/api/users/{user_id}", json={"permissions": {permission: True}}
    )
    assert restored.status_code == 200
    assert _request(member, method, path, body).status_code == expected
    db = SessionLocal()
    try:
        assert db.get(users_service.User, user_id).permissions is None
    finally:
        db.close()


def test_admin_is_never_gated_by_member_permissions(sessions):
    admin, _, _ = sessions
    for _, method, path, body, expected in PROBES:
        response = _request(admin, method, path, body)
        assert response.status_code == expected, (method, path, response.text)


def test_permissions_patch_rejects_unknown_keys_and_non_booleans(sessions):
    admin, _, user_id = sessions
    for bad in ({"agents.launch": False}, {"agents.deploy": "no"}):
        response = admin.patch(f"/api/users/{user_id}", json={"permissions": bad})
        assert response.status_code in (400, 422), response.text
    listed = admin.get("/api/users").json()["items"]
    row = next(item for item in listed if item["id"] == user_id)
    assert row["permissions"] == dict.fromkeys(users_service.AGENT_PERMISSIONS, True)
