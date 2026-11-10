"""Self-registration abuse controls (r12-g, A-2): per-IP window, pending-queue cap,
admin-created accounts unaffected."""

import logging

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import create_app
from app.models.ledger import User
from app.services import users as users_service

ADMIN = {"username": "operator", "password": "s3cret-pass"}


PASSWORD = "Tr0ub4dor&3xy-pass"


def body(i: int) -> dict:
    return {"username": f"user-{i}", "email": f"user-{i}@acme-corp.com", "password": PASSWORD}


def post(client: TestClient, payload: dict) -> int:
    return client.post("/api/auth/register", json=payload).status_code


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def test_failed_registration_attempts_per_peer_are_limited(monkeypatch, app):
    """rev-r11: the window counts FAILED attempts (validation errors, collisions, backlog)
    and only turns further failures into 429 — behind nginx / the NLB every caller shares
    one peer, so successful registrations are never counted or refused by it."""
    monkeypatch.setenv("LAUNCHPAD_AUTH_REGISTER_IP_LIMIT", "3")
    monkeypatch.setenv("LAUNCHPAD_AUTH_REGISTER_IP_WINDOW_SECONDS", "3600")
    get_settings.cache_clear()
    with TestClient(app, client=("203.0.113.9", 40000)) as client:
        assert [post(client, body(i)) for i in range(6)] == [201] * 6  # never limited
        assert [post(client, {**body(9), "password": "short"}) for _ in range(3)] == [400] * 3
        limited = client.post("/api/auth/register", json={**body(9), "email": "nope"})
        assert limited.status_code == 429
        assert limited.json()["code"] == "auth.rate_limited"
        assert "registration" in limited.json()["message"]
        assert 1 <= int(limited.headers["Retry-After"]) <= 3600
        assert post(client, body(20)) == 201  # a valid registration still goes through
    with TestClient(app, client=("198.51.100.7", 40000)) as other:
        assert post(other, {**body(10), "email": "nope"}) == 400  # other peer: own window


def test_422_bodies_do_not_count_and_collisions_do(monkeypatch, app):
    monkeypatch.setenv("LAUNCHPAD_AUTH_REGISTER_IP_LIMIT", "2")
    get_settings.cache_clear()
    with TestClient(app, client=("203.0.113.9", 40000)) as client:
        for _ in range(5):
            assert client.post("/api/auth/register", json={"username": "x"}).status_code == 422
        assert post(client, body(1)) == 201
        # a collision under approval mode answers the pending shape (never a 429, which
        # would disclose the collision) but counts as a failed attempt
        assert post(client, body(1)) == 201
        assert post(client, {**body(2), "email": "nope"}) == 400
        assert post(client, {**body(3), "email": "nope"}) == 429


def test_pending_cap_pauses_self_registration_and_is_journaled(monkeypatch, app, caplog):
    monkeypatch.setenv("LAUNCHPAD_AUTH_REGISTRATION_PENDING_MAX", "2")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert [post(client, body(i)) for i in range(2)] == [201] * 2
        with caplog.at_level(logging.WARNING, logger="launchpad.auth"):
            refused = client.post("/api/auth/register", json=body(3))
        assert refused.status_code == 409
        assert refused.json()["code"] == "auth.registration_backlog"
        assert refused.json()["detail"] == {"pending": 2, "max": 2}
        messages = [r.getMessage() for r in caplog.records]
        lines = [m for m in messages if "registration backlog" in m]
        assert lines and "pending=2 cap=2" in lines[0]
        # approving one frees a slot
        assert client.post("/api/auth/login", json=ADMIN).status_code == 200
        pending_id = client.get("/api/users?status=pending").json()["items"][0]["id"]
        approved = client.patch(f"/api/users/{pending_id}", json={"status": "active"})
        assert approved.status_code == 200
        client.cookies.clear()
        assert post(client, body(3)) == 201


def test_admin_created_accounts_ignore_the_cap_and_keep_precise_errors(monkeypatch, app):
    monkeypatch.setenv("LAUNCHPAD_AUTH_REGISTRATION_PENDING_MAX", "1")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert post(client, body(1)) == 201
        refused = client.post("/api/auth/register", json=body(2))
        assert refused.json()["code"] == "auth.registration_backlog"
    db = SessionLocal()
    try:
        created = users_service.register_user(
            db, "ops-made", "ops-made@acme-corp.com", PASSWORD, created_by="operator"
        )
        assert created.status == "active"
        with pytest.raises(users_service.AppError) as exc:
            users_service.register_user(
                db, "ops-made", "other@acme-corp.com", PASSWORD, created_by="operator"
            )
        assert exc.value.code == "auth.username_taken"  # the service stays precise for admins
        assert db.query(User).count() == 2
    finally:
        db.close()


def test_cap_zero_means_unlimited(monkeypatch, app):
    monkeypatch.setenv("LAUNCHPAD_AUTH_REGISTRATION_PENDING_MAX", "0")
    monkeypatch.setenv("LAUNCHPAD_AUTH_REGISTER_IP_LIMIT", "0")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert [post(client, body(i)) for i in range(60)] == [201] * 60
