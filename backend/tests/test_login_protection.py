"""Console login protection (r12-g, N12-obs57): per-account lockout with exponential
backoff, the secondary per-peer window, the 429 envelope, no user enumeration, the admin
unlock, the session TTL setting and the `/v1` invalid-key window."""

import logging
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.main import create_app
from app.routers import auth
from app.services import login_guard
from app.services import users as users_service

ADMIN = {"username": "operator", "password": "s3cret-pass"}
MEMBER = {
    "username": "qa-user",
    "email": "qa-user@acme-corp.com",
    "password": "correct horse battery staple",
}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_REGISTRATION_REQUIRE_APPROVAL", "false")
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


def register_member(client: TestClient) -> str:
    response = client.post("/api/auth/register", json=MEMBER)
    assert response.status_code == 201, response.text
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, MEMBER["username"])
        assert user is not None
        return user.id
    finally:
        db.close()


def login(client: TestClient, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def fail(client: TestClient, username: str, times: int) -> list[int]:
    return [login(client, username, "definitely-wrong-pass").status_code for _ in range(times)]


# --- per-account lockout ------------------------------------------------------


class TestAccountLockout:
    def test_threshold_failures_lock_the_name_with_429_and_retry_after(self, anon):
        register_member(anon)
        assert fail(anon, MEMBER["username"], 4) == [401] * 4
        fifth = login(anon, MEMBER["username"], "definitely-wrong-pass")
        assert fifth.status_code == 401  # the 5th wrong password is still a 401 ...
        locked = login(anon, MEMBER["username"], MEMBER["password"])
        assert locked.status_code == 429  # ... but the name is locked from then on
        body = locked.json()
        assert body["code"] == "auth.locked"
        assert body["detail"] == {"retry_after": 60}
        assert locked.headers["Retry-After"] == "60"
        assert "password" not in body["message"].lower() or "attempt" in body["message"]

    def test_attempts_during_a_lock_do_not_count_and_the_lock_doubles_to_the_cap(self):
        settings = Settings(auth_login_lockout_threshold=5)
        guard = login_guard.LoginGuard()
        now = 1_000.0
        for _ in range(4):
            assert guard.record_failure("alice", "peer", settings, now) == 0
        assert guard.record_failure("alice", "peer", settings, now) == 60
        assert guard.locked_for("alice", settings, now + 1) == 59
        # the route refuses during the lock BEFORE recording, so a further failure
        # only lands once the lock lapsed — and doubles
        now += 61
        assert guard.locked_for("alice", settings, now) == 0
        assert guard.record_failure("alice", "peer", settings, now) == 120
        now += 121
        assert guard.record_failure("alice", "peer", settings, now) == 240
        now += 241
        assert guard.record_failure("alice", "peer", settings, now) == 480
        now += 481
        assert guard.record_failure("alice", "peer", settings, now) == 960
        now += 961
        assert guard.record_failure("alice", "peer", settings, now) == 1800  # capped
        now += 1801
        assert guard.record_failure("alice", "peer", settings, now) == 1800

    def test_a_successful_sign_in_resets_the_counter(self, anon):
        register_member(anon)
        assert fail(anon, MEMBER["username"], 4) == [401] * 4
        assert login(anon, MEMBER["username"], MEMBER["password"]).status_code == 200
        anon.cookies.clear()
        # four more wrong passwords are tolerated again — the count started over
        assert fail(anon, MEMBER["username"], 4) == [401] * 4
        assert login(anon, MEMBER["username"], MEMBER["password"]).status_code == 200

    def test_the_built_in_admin_is_exempt_from_the_per_name_lock(self, anon):
        """rev-r11 ruling: the config admin is the break-glass credential with the strong
        configured password — a per-name lock would let anyone keep the operator out.
        Its wrong passwords still count against the peer window."""
        assert fail(anon, ADMIN["username"], 8) == [401] * 8
        assert login_guard.guard.state(ADMIN["username"]) == (0, None)
        assert login(anon, **ADMIN).status_code == 200
        assert login_guard.guard.peers.count("testclient", 300.0) == 8

    def test_the_admin_exemption_is_case_insensitive_and_only_for_the_configured_name(
        self, anon
    ):
        assert fail(anon, "OPERATOR", 6) == [401] * 6
        assert login(anon, **ADMIN).status_code == 200
        anon.cookies.clear()
        assert fail(anon, "operator2", 5) == [401] * 5
        assert login(anon, "operator2", "x").status_code == 429

    def test_lockout_is_keyed_case_insensitively_like_sign_in(self, anon):
        register_member(anon)
        assert fail(anon, "QA-User", 5) == [401] * 5
        assert login(anon, "qa-user", MEMBER["password"]).status_code == 429

    def test_disabled_lockout_setting_never_locks(self, monkeypatch, app):
        monkeypatch.setenv("LAUNCHPAD_AUTH_LOGIN_LOCKOUT_ENABLED", "false")
        monkeypatch.setenv("LAUNCHPAD_AUTH_LOGIN_IP_LIMIT", "0")
        get_settings.cache_clear()
        with TestClient(app) as client:
            register_member(client)
            assert fail(client, MEMBER["username"], 8) == [401] * 8
            assert login(client, MEMBER["username"], MEMBER["password"]).status_code == 200

    def test_wrong_password_on_a_disabled_account_is_not_a_brute_force_failure(self, anon):
        user_id = register_member(anon)
        db = SessionLocal()
        try:
            user = users_service.get_user(db, user_id)
            users_service.apply_patch(db, user, {"status": "disabled"})
        finally:
            db.close()
        for _ in range(7):
            response = login(anon, MEMBER["username"], MEMBER["password"])
            assert response.status_code == 401
            assert response.json()["code"] == "auth.account_disabled"
        assert login_guard.guard.state(MEMBER["username"]) == (0, None)

    def test_lockout_is_journaled_without_password_material(self, anon, caplog):
        register_member(anon)
        with caplog.at_level(logging.WARNING, logger="launchpad.auth"):
            fail(anon, MEMBER["username"], 5)
        records = [r for r in caplog.records if "login lockout" in r.getMessage()]
        assert len(records) == 1
        message = records[0].getMessage()
        assert "account=qa-user" in message and "locked_for=60s" in message
        assert "definitely-wrong-pass" not in message
        assert MEMBER["password"] not in message


# --- no user enumeration -----------------------------------------------------------


class TestNoEnumeration:
    def test_unknown_and_wrong_password_share_status_code_and_message(self, anon):
        register_member(anon)
        unknown = login(anon, "nobody-here", "definitely-wrong-pass")
        wrong = login(anon, MEMBER["username"], "definitely-wrong-pass")
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json() == wrong.json()
        assert unknown.json()["code"] == "auth.invalid_credentials"

    def test_an_unknown_name_locks_exactly_like_an_existing_one(self, anon):
        register_member(anon)
        assert fail(anon, "nobody-here", 5) == [401] * 5
        assert fail(anon, MEMBER["username"], 5) == [401] * 5
        ghost = login(anon, "nobody-here", "definitely-wrong-pass")
        real = login(anon, MEMBER["username"], "definitely-wrong-pass")
        assert ghost.status_code == real.status_code == 429
        assert ghost.json() == real.json()
        assert ghost.headers["Retry-After"] == real.headers["Retry-After"]


# --- per-peer window (secondary) -----------------------------------------------------


class TestPeerWindow:
    def test_failures_from_one_peer_trip_the_window_across_account_names(self, monkeypatch, app):
        monkeypatch.setenv("LAUNCHPAD_AUTH_LOGIN_IP_LIMIT", "6")
        monkeypatch.setenv("LAUNCHPAD_AUTH_LOGIN_IP_WINDOW_SECONDS", "300")
        get_settings.cache_clear()
        with TestClient(app) as client:
            register_member(client)
            # spread over names so no single account lock fires first
            codes = [fail(client, f"user-{i}", 1)[0] for i in range(6)]
            assert codes == [401] * 6
            limited = login(client, "user-7", "definitely-wrong-pass")
            assert limited.status_code == 429
            body = limited.json()
            assert body["code"] == "auth.rate_limited"
            assert 1 <= body["detail"]["retry_after"] <= 300
            assert limited.headers["Retry-After"] == str(body["detail"]["retry_after"])
            # rev-r11: the window is consulted only after a FAILED check — behind nginx /
            # the NLB every caller shares this peer, so a correct password still signs in
            assert login(client, **ADMIN).status_code == 200
            client.cookies.clear()
            assert login(client, MEMBER["username"], MEMBER["password"]).status_code == 200
            client.cookies.clear()
            # ... while a further wrong password from the tripped peer is a 429, not a 401
            assert login(client, MEMBER["username"], "definitely-wrong-pass").status_code == 429

    def test_successful_sign_ins_never_count(self, monkeypatch, app):
        monkeypatch.setenv("LAUNCHPAD_AUTH_LOGIN_IP_LIMIT", "3")
        get_settings.cache_clear()
        with TestClient(app) as client:
            for _ in range(5):
                assert login(client, **ADMIN).status_code == 200
                client.cookies.clear()

    def test_window_slides(self):
        window = login_guard.SlidingWindow()
        for i in range(3):
            window.record("peer", 3, 300.0, now=1000.0 + i)
        assert window.retry_after("peer", 3, 300.0, now=1003.0) == 297
        assert window.retry_after("peer", 3, 300.0, now=1300.5) == 0  # oldest aged out
        assert window.retry_after("peer", 0, 300.0, now=1003.0) == 0  # limit 0 = disabled

    def test_peer_trip_is_journaled_once(self, caplog):
        settings = Settings(auth_login_ip_limit=3, auth_login_ip_window_seconds=300)
        guard = login_guard.LoginGuard()
        with caplog.at_level(logging.WARNING, logger="launchpad.auth"):
            for i in range(5):
                guard.record_peer_failure("10.44.0.9", settings, now=1000.0 + i)
        trips = [r for r in caplog.records if "login rate limit" in r.getMessage()]
        assert len(trips) == 1
        assert "peer=10.44.0.9" in trips[0].getMessage()


# --- admin unlock --------------------------------------------------------------------


class TestAdminUnlock:
    def test_users_list_shows_the_lock_and_patch_unlock_clears_it(self, admin):
        with TestClient(admin.app) as attacker:
            user_id = register_member(attacker)
            fail(attacker, MEMBER["username"], 5)
            assert login(attacker, MEMBER["username"], MEMBER["password"]).status_code == 429

        listed = admin.get("/api/users").json()["items"]
        row = next(item for item in listed if item["id"] == user_id)
        assert row["failed_logins"] == 5
        locked_until = datetime.fromisoformat(row["locked_until"])
        assert timedelta(seconds=50) < locked_until - datetime.now(UTC) <= timedelta(seconds=60)

        patched = admin.patch(f"/api/users/{user_id}", json={"unlock": True})
        assert patched.status_code == 200, patched.text
        assert patched.json()["locked_until"] is None
        assert patched.json()["failed_logins"] == 0

        with TestClient(admin.app) as member:
            assert login(member, MEMBER["username"], MEMBER["password"]).status_code == 200

    def test_unlock_false_is_a_no_op_and_members_cannot_unlock(self, admin):
        with TestClient(admin.app) as other:
            user_id = register_member(other)
            fail(other, MEMBER["username"], 5)
        assert admin.patch(f"/api/users/{user_id}", json={"unlock": False}).status_code == 200
        assert login_guard.guard.state(MEMBER["username"])[0] == 5
        # the route stays admin-only (route policy); a member session is 403
        admin.patch(f"/api/users/{user_id}", json={"unlock": True})
        with TestClient(admin.app) as member:
            assert login(member, MEMBER["username"], MEMBER["password"]).status_code == 200
            response = member.patch(f"/api/users/{user_id}", json={"unlock": True})
            assert response.status_code == 403

    def test_unlock_is_rejected_when_not_a_boolean(self, admin):
        with TestClient(admin.app) as other:
            user_id = register_member(other)
        assert admin.patch(f"/api/users/{user_id}", json={"unlock": "yes"}).status_code == 422


# --- session TTL + cookie flags ------------------------------------------------------


class TestSessionCookie:
    def test_cookie_flags_and_configurable_absolute_ttl(self, monkeypatch, app):
        monkeypatch.setenv("LAUNCHPAD_AUTH_SESSION_TTL_SECONDS", "900")
        get_settings.cache_clear()
        with TestClient(app) as client:
            response = login(client, **ADMIN)
            assert response.status_code == 200
            header = response.headers["set-cookie"].lower()
            assert f"{auth.COOKIE_NAME}=" in header
            assert "httponly" in header
            assert "samesite=lax" in header
            assert "max-age=900" in header
            assert "secure" not in header  # dev mode over plain HTTP: see cookie_secure()
            assert auth.session_ttl_seconds(get_settings()) == 900

    def test_prod_mode_adds_secure(self, monkeypatch, app):
        monkeypatch.setenv("LAUNCHPAD_RUN_MODE", "prod")
        get_settings.cache_clear()
        with TestClient(app) as client:
            header = login(client, **ADMIN).headers["set-cookie"].lower()
            assert "secure" in header and "httponly" in header and "samesite=lax" in header

    def test_default_ttl_is_twelve_hours(self):
        assert Settings().auth_session_ttl_seconds == auth.SESSION_TTL_SECONDS == 12 * 3600


# --- defaults ------------------------------------------------------------------------


def test_defaults_match_the_documented_contract():
    settings = Settings()
    assert settings.auth_login_lockout_enabled is True
    assert settings.auth_login_lockout_threshold == 5
    assert settings.auth_login_lockout_base_seconds == 60
    assert settings.auth_login_lockout_max_seconds == 1800
    assert settings.auth_login_ip_limit == 20
    assert settings.auth_login_ip_window_seconds == 300
    assert settings.auth_api_key_ip_limit == 20
    assert settings.auth_api_key_ip_window_seconds == 300
    assert settings.auth_password_min_length == 12


def test_guard_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(login_guard, "_MAX_TRACKED", 50)
    settings = Settings()
    guard = login_guard.LoginGuard()
    for i in range(200):
        guard.record_failure(f"name-{i}", "peer", settings, now=1000.0 + i)
        guard.peers.record(f"10.0.0.{i}", 20, 300.0, now=1000.0 + i)
    assert len(guard._accounts) <= 50
    assert len(guard.peers._events) <= 50


# --- session epoch (rev-r11 ruling): a password change signs the other sessions out ------


class TestSessionEpoch:
    def test_admin_password_reset_ends_the_members_other_sessions(self, admin):
        user_id = register_member(admin)
        with TestClient(admin.app) as member_a, TestClient(admin.app) as member_b:
            assert login(member_a, MEMBER["username"], MEMBER["password"]).status_code == 200
            assert login(member_b, MEMBER["username"], MEMBER["password"]).status_code == 200
            assert member_a.get("/api/auth/status").json()["authenticated"] is True

            reset = admin.patch(f"/api/users/{user_id}", json={"password": None})
            assert reset.status_code == 200
            new_password = reset.json()["generated_password"]

            for stale in (member_a, member_b):
                assert stale.get("/api/auth/status").json()["authenticated"] is False
                assert stale.get("/api/agents").status_code == 401
            assert login(member_a, MEMBER["username"], new_password).status_code == 200
            assert member_a.get("/api/auth/status").json()["authenticated"] is True

    def test_unrelated_patches_keep_the_session(self, admin):
        user_id = register_member(admin)
        with TestClient(admin.app) as member:
            assert login(member, MEMBER["username"], MEMBER["password"]).status_code == 200
            for patch in ({"extend_days": 3}, {"unlock": True}, {"permissions": None}):
                assert admin.patch(f"/api/users/{user_id}", json=patch).status_code == 200
            assert member.get("/api/auth/status").json()["authenticated"] is True

    def test_epoch_is_in_the_cookie_and_a_stale_one_is_refused(self, admin):
        user_id = register_member(admin)
        db = SessionLocal()
        try:
            user = users_service.get_user(db, user_id)
            assert user.session_epoch == 0
            stale = auth._issue(user.username, int(time.time()) + 600, user_id=user.id, epoch="0")
            fresh = auth._issue(user.username, int(time.time()) + 600, user_id=user.id, epoch="1")
            user.session_epoch = 1
            db.commit()
        finally:
            db.close()
        with TestClient(admin.app) as client:
            client.cookies.set(auth.COOKIE_NAME, stale)
            assert client.get("/api/agents").status_code == 401
            client.cookies.set(auth.COOKIE_NAME, fresh)
            assert client.get("/api/auth/status").json()["authenticated"] is True
