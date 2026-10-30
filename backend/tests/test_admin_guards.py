"""Last-admin and self-delete guards (r12-g): PATCH / DELETE `/api/users/{id}` never
leave zero active, non-expired administrator rows (the config admin does not count),
and an administrator never deletes their own account."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import create_app
from app.services import users as users_service

CONFIG_ADMIN = {"username": "operator", "password": "s3cret-pass"}
ALICE = {"username": "alice", "email": "alice@acme-corp.com", "password": "alice-long-passphrase"}
BOB = {"username": "bob", "email": "bob@acme-corp.com", "password": "bob-long-passphrase-1"}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", CONFIG_ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", CONFIG_ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def make_admin(account: dict, *, role: str = "admin") -> str:
    db = SessionLocal()
    try:
        user = users_service.register_user(
            db, account["username"], account["email"], account["password"],
            created_by="operator", role=role,
        )
        return user.id
    finally:
        db.close()


def code(response) -> str:
    return response.json()["code"]


def status(client: TestClient, user_id: str, patch: dict) -> int:
    return client.patch(f"/api/users/{user_id}", json=patch).status_code


def login(app, account: dict) -> TestClient:
    client = TestClient(app)
    assert client.post("/api/auth/login", json=account).status_code == 200
    return client


@pytest.fixture
def config_admin(app) -> TestClient:
    return login(app, CONFIG_ADMIN)


class TestLastAdmin:
    @pytest.mark.parametrize(
        "patch",
        [
            {"role": "member"},
            {"status": "disabled"},
            {"status": "pending"},
            {"expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()},
        ],
    )
    def test_the_only_admin_row_cannot_be_demoted_disabled_or_expired(
        self, app, config_admin, patch
    ):
        alice_id = make_admin(ALICE)
        response = config_admin.patch(f"/api/users/{alice_id}", json=patch)
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "users.last_admin"
        assert body["detail"] == {"user_id": alice_id, "username": "alice"}
        # nothing changed — the row still administers
        db = SessionLocal()
        try:
            assert users_service.active_admin_ids(db) == {alice_id}
        finally:
            db.close()

    def test_the_only_admin_row_cannot_be_deleted_even_by_the_config_admin(
        self, app, config_admin
    ):
        alice_id = make_admin(ALICE)
        response = config_admin.delete(f"/api/users/{alice_id}")
        assert response.status_code == 409
        assert response.json()["code"] == "users.last_admin"
        assert config_admin.get("/api/users").json()["total"] == 1

    def test_with_a_second_active_admin_every_change_is_allowed(self, app, config_admin):
        alice_id = make_admin(ALICE)
        make_admin(BOB)
        assert status(config_admin, alice_id, {"role": "member"}) == 200
        assert status(config_admin, alice_id, {"role": "admin"}) == 200
        assert config_admin.delete(f"/api/users/{alice_id}").status_code == 200

    def test_a_disabled_or_expired_admin_does_not_count_as_a_remaining_admin(
        self, app, config_admin
    ):
        alice_id = make_admin(ALICE)
        bob_id = make_admin(BOB)
        assert status(config_admin, bob_id, {"status": "disabled"}) == 200
        demote = config_admin.patch(f"/api/users/{alice_id}", json={"role": "member"})
        assert code(demote) == "users.last_admin"
        assert status(config_admin, bob_id, {"status": "active"}) == 200
        db = SessionLocal()
        try:
            bob = users_service.get_user(db, bob_id)
            bob.expires_at = datetime.now(UTC) - timedelta(days=1)
            db.commit()
        finally:
            db.close()
        demote = config_admin.patch(f"/api/users/{alice_id}", json={"role": "member"})
        assert code(demote) == "users.last_admin"

    def test_changes_that_keep_the_admin_administering_are_untouched(self, app, config_admin):
        alice_id = make_admin(ALICE)
        for patch in (
            {"extend_days": 30},
            {"expires_at": None},
            {"password": None},
            {"permissions": None},
            {"unlock": True},
            {"status": "active"},
            {"role": "admin"},
        ):
            assert status(config_admin, alice_id, patch) == 200, patch

    def test_members_and_pending_rows_are_never_guarded(self, app, config_admin):
        carol_id = make_admin(
            {"username": "carol", "email": "carol@acme-corp.com", "password": "carol-long-pass-1"},
            role="member",
        )
        assert status(config_admin, carol_id, {"status": "disabled"}) == 200
        assert config_admin.delete(f"/api/users/{carol_id}").status_code == 200

    def test_the_config_admin_does_not_count_as_a_remaining_admin(self, app, config_admin):
        """Zero admin rows is a legal starting state; once one exists it is pinned until a
        second one is promoted — the break-glass config admin is not the fallback."""
        db = SessionLocal()
        try:
            assert users_service.active_admin_ids(db) == set()
        finally:
            db.close()
        alice_id = make_admin(ALICE)
        assert code(config_admin.delete(f"/api/users/{alice_id}")) == "users.last_admin"


class TestSelfGuards:
    def test_an_admin_cannot_delete_their_own_account(self, app):
        alice_id = make_admin(ALICE)
        make_admin(BOB)  # a second admin exists, so only the self rule applies
        alice = login(app, ALICE)
        response = alice.delete(f"/api/users/{alice_id}")
        assert response.status_code == 409
        assert response.json()["code"] == "users.self_delete"
        assert alice.get("/api/users").json()["total"] == 2

    def test_self_demotion_needs_another_active_admin(self, app):
        alice_id = make_admin(ALICE)
        alice = login(app, ALICE)
        for patch in ({"role": "member"}, {"status": "disabled"}):
            assert code(alice.patch(f"/api/users/{alice_id}", json=patch)) == "users.last_admin"
        make_admin(BOB)
        demoted = alice.patch(f"/api/users/{alice_id}", json={"role": "member"})
        assert demoted.status_code == 200 and demoted.json()["role"] == "member"
        # the demotion took effect on the very next request
        assert alice.get("/api/users").status_code == 403

    def test_the_config_admin_has_no_row_so_self_delete_never_triggers(self, app, config_admin):
        make_admin(ALICE)
        bob_id = make_admin(BOB)
        assert config_admin.delete(f"/api/users/{bob_id}").status_code == 200
