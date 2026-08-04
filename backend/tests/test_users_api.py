"""Self-service registration + admin user management (/api/users)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import create_app
from app.models.ledger import User
from app.services import users as users_service

ADMIN = {"username": "operator", "password": "s3cret-pass"}
# A member-reachable route, used here purely as a "is this session live"
# probe. Not /api/apikeys: credential minting is admin-only (see
# app/core/route_policy.py), so a member gets 403 there by design.
MEMBER_PROBE = "/api/agents"
MEMBER = {
    "username": "qa-user",
    "email": "qa-user@acme-corp.com",
    "password": "sufficient-pass",
}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


@pytest.fixture
def anon(app) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin(app) -> TestClient:
    with TestClient(app) as test_client:
        assert test_client.post("/api/auth/login", json=ADMIN).status_code == 200
        yield test_client


def register(client: TestClient, **overrides):
    return client.post("/api/auth/register", json={**MEMBER, **overrides})


def approve_stored(username: str = MEMBER["username"], days: int = 7) -> None:
    """Stand in for an admin approval: activate and start the validity window."""
    patch_stored(
        username,
        status="active",
        expires_at=datetime.now(UTC) + timedelta(days=days),
    )


def member_session(app) -> TestClient:
    client = TestClient(app)
    assert register(client).status_code == 201
    approve_stored()
    login = client.post(
        "/api/auth/login",
        json={"username": MEMBER["username"], "password": MEMBER["password"]},
    )
    assert login.status_code == 200, login.text
    return client


def stored(username: str = MEMBER["username"]) -> User:
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, username)
        assert user is not None
        db.expunge(user)
        return user
    finally:
        db.close()


def patch_stored(username: str = MEMBER["username"], **fields) -> None:
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, username)
        assert user is not None
        for key, value in fields.items():
            setattr(user, key, value)
        db.commit()
    finally:
        db.close()


class TestRegistration:
    def test_registration_awaits_admin_approval_by_default(self, anon):
        response = register(anon)
        assert response.status_code == 201
        body = response.json()
        assert body["username"] == MEMBER["username"]
        assert body["email"] == MEMBER["email"]
        assert body["status"] == "pending"
        assert body["requires_approval"] is True
        # the clock only starts at approval, so there is no window yet
        assert body["expires_at"] is None
        assert body["valid_days"] == 7
        assert anon.get("/api/auth/status").json()["registration_requires_approval"] is True

        user = stored()
        assert user.role == "member"
        assert user.status == "pending"
        assert user.expires_at is None
        assert user.created_by == "self"
        assert user.password_hash.startswith("pbkdf2_sha256$")
        assert MEMBER["password"] not in user.password_hash

    def test_pending_account_cannot_sign_in(self, anon):
        assert register(anon).status_code == 201
        response = anon.post(
            "/api/auth/login",
            json={"username": MEMBER["username"], "password": MEMBER["password"]},
        )
        assert response.status_code == 401
        assert response.json()["code"] == "auth.account_pending"
        assert anon.get(MEMBER_PROBE).status_code == 401

    def test_approval_starts_the_validity_window_and_unlocks_sign_in(self, anon, admin, app):
        assert register(anon).status_code == 201
        user_id = stored().id

        approved = admin.patch(f"/api/users/{user_id}", json={"status": "active"})
        assert approved.status_code == 200
        assert approved.json()["state"] == "active"
        assert approved.json()["days_remaining"] in (6, 7)

        client = TestClient(app)
        assert client.post(
            "/api/auth/login",
            json={"username": MEMBER["username"], "password": MEMBER["password"]},
        ).status_code == 200
        assert client.get(MEMBER_PROBE).status_code == 200

    def test_rejection_keeps_the_account_out(self, anon, admin):
        assert register(anon).status_code == 201
        user_id = stored().id
        rejected = admin.patch(f"/api/users/{user_id}", json={"status": "disabled"})
        assert rejected.json()["state"] == "disabled"
        assert rejected.json()["expires_at"] is None  # never got a window
        login = anon.post(
            "/api/auth/login",
            json={"username": MEMBER["username"], "password": MEMBER["password"]},
        )
        assert login.status_code == 401
        assert login.json()["code"] == "auth.account_disabled"

    def test_approval_can_be_turned_off_for_instant_accounts(self, monkeypatch, anon, app):
        monkeypatch.setenv("LAUNCHPAD_AUTH_REGISTRATION_REQUIRE_APPROVAL", "false")
        get_settings.cache_clear()

        body = register(anon).json()
        assert body["status"] == "active"
        assert body["requires_approval"] is False
        remaining = datetime.fromisoformat(body["expires_at"]) - datetime.now(UTC)
        assert timedelta(days=6, hours=23) < remaining <= timedelta(days=7)
        assert anon.get("/api/auth/status").json()["registration_requires_approval"] is False

        client = TestClient(app)
        assert client.post(
            "/api/auth/login",
            json={"username": MEMBER["username"], "password": MEMBER["password"]},
        ).status_code == 200

    def test_approved_account_can_use_the_console(self, app):
        client = member_session(app)
        status = client.get("/api/auth/status").json()
        assert status["authenticated"] is True
        assert status["username"] == MEMBER["username"]
        assert status["role"] == "member"
        assert status["email"] == MEMBER["email"]
        assert status["account_expires_at"] is not None
        assert client.get(MEMBER_PROBE).status_code == 200
        # ...but the console a member sees is read-only: admin-only routes answer
        # 403, not 200. See app/core/route_policy.py.
        assert client.get("/api/apikeys").status_code == 403
        assert client.post("/api/agents", json={}).status_code == 403
        assert stored().login_count == 1

    @pytest.mark.parametrize(
        ("overrides", "code", "expected_status"),
        [
            ({"username": "ab"}, "auth.invalid_username", 400),
            ({"username": "bad name"}, "auth.invalid_username", 400),
            ({"username": "OPERATOR"}, "auth.username_taken", 409),
            ({"email": "not-an-email"}, "auth.invalid_email", 400),
            ({"email": "someone@gmail.com"}, "auth.email_domain_blocked", 400),
            ({"email": "someone@mail.qq.com"}, "auth.email_domain_blocked", 400),
            ({"password": "short"}, "auth.weak_password", 400),
        ],
    )
    def test_validation_rules(self, anon, overrides, code, expected_status):
        response = register(anon, **overrides)
        assert response.status_code == expected_status
        assert response.json()["code"] == code

    def test_duplicate_username_and_email_are_case_insensitive(self, anon):
        assert register(anon).status_code == 201

        taken = register(anon, username="QA-User", email="other@acme-corp.com")
        assert taken.status_code == 409
        assert taken.json()["code"] == "auth.username_taken"

        duplicate_email = register(anon, username="other", email="QA-USER@Acme-Corp.com")
        assert duplicate_email.status_code == 409
        assert duplicate_email.json()["code"] == "auth.email_taken"

    def test_registration_can_be_disabled_by_configuration(self, monkeypatch, anon):
        monkeypatch.setenv("LAUNCHPAD_AUTH_REGISTRATION_ENABLED", "false")
        get_settings.cache_clear()
        response = register(anon)
        assert response.status_code == 400
        assert response.json()["code"] == "auth.registration_disabled"
        assert anon.get("/api/auth/status").json()["registration_enabled"] is False

    def test_allowed_domains_override_the_blacklist(self, monkeypatch, anon):
        monkeypatch.setenv("LAUNCHPAD_AUTH_ALLOWED_EMAIL_DOMAINS", '["partner.io"]')
        get_settings.cache_clear()
        assert register(anon, email="who@partner.io").status_code == 201
        blocked = register(anon, username="other", email="who@acme-corp.com")
        assert blocked.status_code == 400
        assert blocked.json()["code"] == "auth.email_domain_blocked"


class TestAccountLifecycle:
    def test_expired_account_loses_an_established_session(self, app):
        client = member_session(app)
        assert client.get(MEMBER_PROBE).status_code == 200

        patch_stored(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        assert client.get(MEMBER_PROBE).status_code == 401
        assert client.get(MEMBER_PROBE).json()["code"] == "auth.required"

        relogin = client.post(
            "/api/auth/login",
            json={"username": MEMBER["username"], "password": MEMBER["password"]},
        )
        assert relogin.status_code == 401
        assert relogin.json()["code"] == "auth.account_expired"

    def test_disabled_account_loses_an_established_session(self, app):
        client = member_session(app)
        patch_stored(status="disabled")
        assert client.get(MEMBER_PROBE).status_code == 401

        relogin = client.post(
            "/api/auth/login",
            json={"username": MEMBER["username"], "password": MEMBER["password"]},
        )
        assert relogin.status_code == 401
        assert relogin.json()["code"] == "auth.account_disabled"

    def test_deleted_account_loses_an_established_session(self, app, admin):
        client = member_session(app)
        user_id = stored().id
        assert admin.delete(f"/api/users/{user_id}").status_code == 200
        assert client.get(MEMBER_PROBE).status_code == 401

    def test_wrong_password_reports_generic_credentials_error(self, anon):
        assert register(anon).status_code == 201
        response = anon.post(
            "/api/auth/login",
            json={"username": MEMBER["username"], "password": "wrong-password"},
        )
        assert response.status_code == 401
        assert response.json()["code"] == "auth.invalid_credentials"

    def test_session_cookie_never_outlives_the_account(self, anon):
        assert register(anon).status_code == 201
        patch_stored(status="active", expires_at=datetime.now(UTC) + timedelta(minutes=30))
        login = anon.post(
            "/api/auth/login",
            json={"username": MEMBER["username"], "password": MEMBER["password"]},
        )
        assert login.status_code == 200
        # 12h default session TTL is clamped down to the 30 minutes left
        assert login.json()["expires_at"] <= int(datetime.now(UTC).timestamp()) + 1810


class TestAdminUserApi:
    def test_members_cannot_reach_the_management_surface(self, app):
        client = member_session(app)
        user_id = stored().id
        for method, path in (
            ("get", "/api/users"),
            ("get", "/api/users/stats"),
            ("patch", f"/api/users/{user_id}"),
            ("delete", f"/api/users/{user_id}"),
        ):
            response = getattr(client, method)(
                path, **({"json": {"extend_days": 7}} if method == "patch" else {})
            )
            assert response.status_code == 403, (method, path)
            assert response.json()["code"] == "auth.forbidden"

    def test_unauthenticated_callers_get_the_session_error(self, anon):
        assert anon.get("/api/users").status_code == 401
        assert anon.get("/api/users").json()["code"] == "auth.required"

    def test_management_surface_is_open_when_the_gate_is_disabled(self, client):
        # no password configured: the console is open and the local operator is
        # the implicit administrator
        assert client.get("/api/users").status_code == 200
        assert client.get("/api/users/stats").status_code == 200

    def test_list_search_filter_and_pagination(self, anon, admin):
        assert register(anon).status_code == 201
        assert register(
            anon, username="second", email="second@other-corp.com"
        ).status_code == 201
        approve_stored()
        patch_stored("second", status="disabled")

        listing = admin.get("/api/users").json()
        assert listing["total"] == 2
        assert {row["username"] for row in listing["items"]} == {"qa-user", "second"}
        assert all("password_hash" not in row for row in listing["items"])

        assert admin.get("/api/users?status=disabled").json()["total"] == 1
        assert admin.get("/api/users?status=active").json()["total"] == 1
        assert admin.get("/api/users?status=pending").json()["total"] == 0
        assert admin.get("/api/users?q=OTHER-CORP").json()["total"] == 1
        assert admin.get("/api/users?q=qa-").json()["items"][0]["username"] == "qa-user"

        page = admin.get("/api/users?limit=1&offset=1").json()
        assert page["total"] == 2 and len(page["items"]) == 1

        assert admin.get("/api/users?status=bogus").status_code == 422

    def test_pending_accounts_show_up_in_the_approval_queue(self, anon, admin):
        assert register(anon).status_code == 201
        queue = admin.get("/api/users?status=pending").json()
        assert queue["total"] == 1
        assert queue["items"][0]["state"] == "pending"
        assert queue["items"][0]["expires_at"] is None
        assert queue["items"][0]["days_remaining"] is None
        assert admin.get("/api/users/stats").json()["pending"] == 1

    def test_expired_accounts_show_up_in_the_expired_filter(self, anon, admin):
        assert register(anon).status_code == 201
        patch_stored(status="active", expires_at=datetime.now(UTC) - timedelta(days=1))
        expired = admin.get("/api/users?status=expired").json()
        assert expired["total"] == 1
        assert expired["items"][0]["state"] == "expired"
        assert expired["items"][0]["days_remaining"] == 0
        assert admin.get("/api/users?status=active").json()["total"] == 0

    def test_stats_summarize_the_account_population(self, anon, admin):
        assert register(anon).status_code == 201
        assert register(
            anon, username="lapsed", email="lapsed@other-corp.com"
        ).status_code == 201
        assert register(
            anon, username="blocked-user", email="blocked@third-corp.com"
        ).status_code == 201
        assert register(
            anon, username="waiting", email="waiting@fourth-corp.com"
        ).status_code == 201
        patch_stored("lapsed", status="active", expires_at=datetime.now(UTC) - timedelta(days=1))
        patch_stored("blocked-user", status="disabled")
        patch_stored(status="active", expires_at=datetime.now(UTC) + timedelta(days=2))

        stats = admin.get("/api/users/stats").json()
        assert stats["total"] == 4
        assert stats["pending"] == 1
        assert stats["active"] == 1
        assert stats["expired"] == 1
        assert stats["disabled"] == 1
        assert stats["expiring_soon"] == 1
        assert stats["registered_last_7d"] == 4
        assert stats["active_last_7d"] == 0
        assert stats["valid_days"] == 7
        assert len(stats["registrations"]) == 14
        assert stats["registrations"][-1]["count"] == 4
        assert {row["domain"] for row in stats["top_domains"]} == {
            "acme-corp.com",
            "other-corp.com",
            "third-corp.com",
            "fourth-corp.com",
        }

    def test_extend_adds_to_the_remaining_validity(self, anon, admin):
        assert register(anon).status_code == 201
        approve_stored()
        user_id = stored().id
        response = admin.patch(f"/api/users/{user_id}", json={"extend_days": 30})
        assert response.status_code == 200
        assert response.json()["days_remaining"] in (36, 37)

    def test_extend_revives_an_expired_account_from_now(self, anon, admin):
        assert register(anon).status_code == 201
        patch_stored(status="active", expires_at=datetime.now(UTC) - timedelta(days=10))
        user_id = stored().id
        body = admin.patch(f"/api/users/{user_id}", json={"extend_days": 7}).json()
        assert body["state"] == "active"
        assert body["days_remaining"] in (6, 7)

    def test_absolute_expiry_and_never_expires(self, anon, admin):
        assert register(anon).status_code == 201
        approve_stored()
        user_id = stored().id
        target = (datetime.now(UTC) + timedelta(days=90)).isoformat()
        assert admin.patch(
            f"/api/users/{user_id}", json={"expires_at": target}
        ).json()["days_remaining"] in (89, 90)

        never = admin.patch(f"/api/users/{user_id}", json={"expires_at": None}).json()
        assert never["expires_at"] is None
        assert never["days_remaining"] is None
        assert never["state"] == "active"

    def test_disable_enable_and_role_change(self, anon, admin, app):
        assert register(anon).status_code == 201
        approve_stored()
        user_id = stored().id

        disabled = admin.patch(f"/api/users/{user_id}", json={"status": "disabled"})
        assert disabled.json()["state"] == "disabled"
        assert admin.patch(
            f"/api/users/{user_id}", json={"status": "active"}
        ).json()["state"] == "active"
        assert admin.patch(
            f"/api/users/{user_id}", json={"role": "admin"}
        ).json()["role"] == "admin"

        # a promoted member reaches the management surface with its own session
        promoted = TestClient(app)
        assert promoted.post(
            "/api/auth/login",
            json={"username": MEMBER["username"], "password": MEMBER["password"]},
        ).status_code == 200
        assert promoted.get("/api/users").status_code == 200

        assert admin.patch(f"/api/users/{user_id}", json={"role": "root"}).status_code == 400
        assert (
            admin.patch(f"/api/users/{user_id}", json={"status": "sleepy"}).status_code == 400
        )

    def test_password_reset_returns_a_generated_password_once(self, anon, admin, app):
        assert register(anon).status_code == 201
        approve_stored()
        user_id = stored().id
        before = stored().password_hash

        reset = admin.patch(f"/api/users/{user_id}", json={"password": None}).json()
        generated = reset["generated_password"]
        assert len(generated) >= 12
        assert stored().password_hash != before
        assert generated not in stored().password_hash

        client = TestClient(app)
        assert client.post(
            "/api/auth/login",
            json={"username": MEMBER["username"], "password": generated},
        ).status_code == 200

        explicit = admin.patch(
            f"/api/users/{user_id}", json={"password": "chosen-by-admin"}
        ).json()
        assert "generated_password" not in explicit
        assert client.post(
            "/api/auth/login",
            json={"username": MEMBER["username"], "password": "chosen-by-admin"},
        ).status_code == 200
        assert admin.patch(
            f"/api/users/{user_id}", json={"password": "tiny"}
        ).status_code == 400

    def test_unknown_user_and_empty_patch(self, admin, anon):
        assert admin.get("/api/users/stats").status_code == 200
        assert admin.patch("/api/users/missing", json={"extend_days": 1}).status_code == 404
        assert admin.delete("/api/users/missing").status_code == 404

        assert register(anon).status_code == 201
        user_id = stored().id
        assert admin.patch(f"/api/users/{user_id}", json={}).status_code == 422

    def test_delete_removes_the_account(self, anon, admin):
        assert register(anon).status_code == 201
        user_id = stored().id
        assert admin.delete(f"/api/users/{user_id}").status_code == 200
        assert admin.get("/api/users").json()["total"] == 0
