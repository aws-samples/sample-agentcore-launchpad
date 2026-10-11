"""SE-054 — /v1 API keys record their creator and last use, and an offboarded
creator's keys stop working (disabled, never deleted, not re-enableable).

Offboarding paths: admin disable, delete, expiry (no write event — caught at use),
per-user grant replacement and the workspace-side bulk revoke.
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.core import db as db_module
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal, assert_no_schema_drift
from app.main import create_app
from app.models.ledger import ApiKey, Workspace
from app.routers.apikeys import hash_key
from app.services import api_keys as api_keys_service
from app.services import users as users_service

ADMIN = {"username": "operator", "password": "s3cret-pass"}
WEST = "west"
PEER = ("127.0.0.1", 4321)


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


@pytest.fixture
def west(app):
    db = SessionLocal()
    try:
        db.add(
            Workspace(
                id=WEST, name="West", account_id="444455556666", region="us-west-1",
                bootstrap_status="ready",
            )
        )
        db.commit()
    finally:
        db.close()
    return WEST


def _member(app, username: str, workspaces: list[str]) -> tuple[str, TestClient]:
    """An approved member holding `workspaces`, and a logged-in client for it."""
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
        users_service.set_workspace_grants(db, user, workspaces)
        db.commit()
        user_id = user.id
    finally:
        db.close()
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return user_id, client


def _mint(client: TestClient, workspace: str = DEFAULT_WORKSPACE_ID, name: str = "k") -> dict:
    response = client.post(
        "/api/apikeys", json={"name": name}, headers={"X-Workspace": workspace}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _legacy_key(workspace: str = DEFAULT_WORKSPACE_ID) -> tuple[str, str]:
    """A key as minted before creators were recorded (all lifecycle columns NULL)."""
    raw = "lp_live_legacy0000"
    db = SessionLocal()
    try:
        key = ApiKey(workspace_id=workspace, name="legacy", prefix="lp_live_lega…",
                     key_hash=hash_key(raw))
        db.add(key)
        db.commit()
        return key.id, raw
    finally:
        db.close()


def _v1(app, raw: str) -> int:
    with TestClient(app, client=PEER) as client:
        return client.get("/v1/agents", headers={"X-Api-Key": raw}).status_code


def _row(key_id: str) -> ApiKey:
    db = SessionLocal()
    try:
        row = db.get(ApiKey, key_id)
        db.expunge(row)
        return row
    finally:
        db.close()


def _listed(client: TestClient, key_id: str, workspace: str = DEFAULT_WORKSPACE_ID) -> dict:
    keys = client.get("/api/apikeys", headers={"X-Workspace": workspace}).json()["keys"]
    return next(k for k in keys if k["id"] == key_id)


def _set_user(user_id: str, **fields) -> None:
    db = SessionLocal()
    try:
        user = users_service.get_user(db, user_id)
        for name, value in fields.items():
            setattr(user, name, value)
        db.commit()
    finally:
        db.close()


class TestCreatorAndLastUse:
    def test_a_member_minted_key_records_its_creator(self, app, admin):
        user_id, ada = _member(app, "ada", [DEFAULT_WORKSPACE_ID])
        created = _mint(ada)

        row = _row(created["id"])
        assert row.created_by == user_id and row.created_by_name == "ada"
        listed = _listed(admin, created["id"])
        assert listed["created_by"] == user_id
        assert listed["created_by_name"] == "ada"
        assert listed["last_used_at"] is None
        assert listed["creator_offboarded"] is None

    def test_the_config_admin_has_no_users_row_and_is_named_only(self, admin):
        created = _mint(admin)
        row = _row(created["id"])
        assert row.created_by is None and row.created_by_name == ADMIN["username"]

    def test_a_legacy_key_lists_an_unknown_creator(self, admin):
        key_id, _ = _legacy_key()
        listed = _listed(admin, key_id)
        assert listed["created_by"] is None and listed["created_by_name"] is None
        assert listed["creator_offboarded"] is None

    def test_v1_stamps_last_used_at_most_once_per_window(self, app, admin):
        created = _mint(admin)
        raw, first_id = created["key"], created["id"]
        key_id = _mint(admin, name="other")["id"]  # untouched control

        assert _v1(app, raw) == 200
        stamped = _row(first_id).last_used_at
        assert stamped is not None

        assert _v1(app, raw) == 200
        assert _row(first_id).last_used_at == stamped  # inside the window: no write
        assert _row(key_id).last_used_at is None

        db = SessionLocal()
        try:
            db.get(ApiKey, first_id).last_used_at = datetime.now(UTC) - timedelta(minutes=2)
            db.commit()
        finally:
            db.close()
        assert _v1(app, raw) == 200
        later = users_service.as_utc(_row(first_id).last_used_at)
        assert later > datetime.now(UTC) - timedelta(seconds=30)
        assert _listed(admin, first_id)["last_used_at"] is not None

    def test_touch_last_used_is_throttled(self):
        key = ApiKey(name="k", prefix="p", key_hash="h")
        now = datetime.now(UTC)
        assert api_keys_service.touch_last_used(key, now) is True
        assert api_keys_service.touch_last_used(key, now + timedelta(seconds=59)) is False
        assert api_keys_service.touch_last_used(key, now + timedelta(seconds=61)) is True

    def test_a_refused_key_is_not_stamped(self, app, admin):
        created = _mint(admin)
        admin.post(f"/api/apikeys/{created['id']}/disable")
        assert _v1(app, created["key"]) == 401
        assert _row(created["id"]).last_used_at is None


class TestOffboarding:
    def test_deactivating_a_member_disables_only_their_keys(self, app, admin):
        ada_id, ada = _member(app, "ada", [DEFAULT_WORKSPACE_ID])
        _, bruno = _member(app, "bruno", [DEFAULT_WORKSPACE_ID])
        ada_key = _mint(ada)
        bruno_key = _mint(bruno)
        admin_key = _mint(admin)
        legacy_id, legacy_raw = _legacy_key()
        assert _v1(app, ada_key["key"]) == 200

        response = admin.patch(f"/api/users/{ada_id}", json={"status": "disabled"})
        assert response.status_code == 200, response.text

        assert _row(ada_key["id"]).enabled is False  # eagerly, before any /v1 call
        with TestClient(app, client=PEER) as client:
            refused = client.get("/v1/agents", headers={"X-Api-Key": ada_key["key"]})
        assert refused.status_code == 401
        assert refused.json()["code"] == "auth.invalid_api_key"  # never says why
        assert _v1(app, bruno_key["key"]) == 200
        assert _v1(app, admin_key["key"]) == 200
        assert _v1(app, legacy_raw) == 200
        assert _row(bruno_key["id"]).enabled and _row(legacy_id).enabled
        listed = _listed(admin, ada_key["id"])  # still listed, for audit
        assert listed["enabled"] is False and listed["creator_offboarded"] == "disabled"

    def test_deleting_a_member_disables_and_keeps_their_keys(self, app, admin):
        ada_id, ada = _member(app, "ada", [DEFAULT_WORKSPACE_ID])
        ada_key = _mint(ada)
        _, legacy_raw = _legacy_key()

        assert admin.delete(f"/api/users/{ada_id}").status_code == 200

        row = _row(ada_key["id"])
        assert row.enabled is False and row.created_by == ada_id
        assert _v1(app, ada_key["key"]) == 401
        assert _v1(app, legacy_raw) == 200
        listed = _listed(admin, ada_key["id"])
        assert listed["created_by_name"] == "ada"
        assert listed["creator_offboarded"] == "deleted"

    def test_expiry_has_no_write_event_so_v1_disables_at_use(self, app, admin):
        ada_id, ada = _member(app, "ada", [DEFAULT_WORKSPACE_ID])
        ada_key = _mint(ada)
        _set_user(ada_id, expires_at=datetime.now(UTC) - timedelta(minutes=1))
        assert _row(ada_key["id"]).enabled is True
        assert _listed(admin, ada_key["id"])["creator_offboarded"] == "expired"

        assert _v1(app, ada_key["key"]) == 401
        assert _row(ada_key["id"]).enabled is False

    def test_bulk_grant_revoke_disables_only_that_workspace_keys(self, app, admin, west):
        ada_id, ada = _member(app, "ada", [DEFAULT_WORKSPACE_ID, west])
        _, bruno = _member(app, "bruno", [west])
        home = _mint(ada, DEFAULT_WORKSPACE_ID)
        away = _mint(ada, west)
        bruno_away = _mint(bruno, west)
        _, legacy_raw = _legacy_key(west)

        response = admin.put(f"/api/workspaces/{west}/grants", json={"revoke": [ada_id]})
        assert response.status_code == 200, response.text

        assert _row(away["id"]).enabled is False
        assert _row(home["id"]).enabled is True
        assert _v1(app, away["key"]) == 401
        assert _v1(app, home["key"]) == 200
        assert _v1(app, bruno_away["key"]) == 200
        assert _v1(app, legacy_raw) == 200
        assert _listed(admin, away["id"], west)["creator_offboarded"] == "no_workspace_grant"

    def test_per_user_grant_replacement_disables_the_dropped_workspace_keys(
        self, app, admin, west
    ):
        ada_id, ada = _member(app, "ada", [DEFAULT_WORKSPACE_ID, west])
        home = _mint(ada, DEFAULT_WORKSPACE_ID)
        away = _mint(ada, west)

        response = admin.patch(
            f"/api/users/{ada_id}", json={"workspaces": [DEFAULT_WORKSPACE_ID]}
        )
        assert response.status_code == 200, response.text

        assert _row(away["id"]).enabled is False
        assert _row(home["id"]).enabled is True

    def test_an_unrelated_patch_leaves_an_active_member_keys_alone(self, app, admin):
        ada_id, ada = _member(app, "ada", [DEFAULT_WORKSPACE_ID])
        ada_key = _mint(ada)
        assert admin.patch(f"/api/users/{ada_id}", json={"extend_days": 3}).status_code == 200
        assert _row(ada_key["id"]).enabled is True

    def test_an_admin_role_creator_needs_no_grant(self, app, admin, west):
        ada_id, ada = _member(app, "ada", [west])
        away = _mint(ada, west)
        assert admin.patch(
            f"/api/users/{ada_id}", json={"role": "admin", "workspaces": None}
        ).status_code == 200
        assert _row(away["id"]).enabled is True
        assert _v1(app, away["key"]) == 200

        # demoted with no grant: offboarded from that workspace
        assert admin.patch(f"/api/users/{ada_id}", json={"role": "member"}).status_code == 200
        assert _row(away["id"]).enabled is False


class TestReEnable:
    def _refused(self, client: TestClient, key_id: str, workspace=DEFAULT_WORKSPACE_ID):
        response = client.post(
            f"/api/apikeys/{key_id}/enable", headers={"X-Workspace": workspace}
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "apikey.creator_offboarded"
        assert _row(key_id).enabled is False
        return response.json()

    def test_a_deactivated_creator_key_cannot_be_re_enabled(self, app, admin):
        ada_id, ada = _member(app, "ada", [DEFAULT_WORKSPACE_ID])
        _, bruno = _member(app, "bruno", [DEFAULT_WORKSPACE_ID])
        key = _mint(ada)
        admin.patch(f"/api/users/{ada_id}", json={"status": "disabled"})

        body = self._refused(admin, key["id"])
        assert body["detail"] == {"reason": "disabled"}
        self._refused(bruno, key["id"])  # any member is refused alike
        assert _v1(app, key["key"]) == 401

    def test_a_deleted_creator_key_cannot_be_re_enabled(self, app, admin):
        ada_id, ada = _member(app, "ada", [DEFAULT_WORKSPACE_ID])
        key = _mint(ada)
        admin.delete(f"/api/users/{ada_id}")
        assert self._refused(admin, key["id"])["detail"] == {"reason": "deleted"}

    def test_a_creator_without_a_grant_cannot_re_enable(self, app, admin, west):
        ada_id, ada = _member(app, "ada", [DEFAULT_WORKSPACE_ID, west])
        key = _mint(ada, west)
        admin.put(f"/api/workspaces/{west}/grants", json={"revoke": [ada_id]})
        body = self._refused(admin, key["id"], west)
        assert body["detail"] == {"reason": "no_workspace_grant"}

    def test_a_legacy_key_can_be_re_enabled(self, app, admin):
        key_id, raw = _legacy_key()
        admin.post(f"/api/apikeys/{key_id}/disable")
        response = admin.post(f"/api/apikeys/{key_id}/enable")
        assert response.status_code == 200 and response.json()["enabled"] is True
        assert _v1(app, raw) == 200

    def test_an_active_creator_key_can_be_re_enabled(self, app, admin):
        _, ada = _member(app, "ada", [DEFAULT_WORKSPACE_ID])
        _, bruno = _member(app, "bruno", [DEFAULT_WORKSPACE_ID])
        key = _mint(ada)
        bruno.post(f"/api/apikeys/{key['id']}/disable")  # any member may toggle
        assert _v1(app, key["key"]) == 401
        response = bruno.post(f"/api/apikeys/{key['id']}/enable")
        assert response.status_code == 200 and response.json()["enabled"] is True
        assert _v1(app, key["key"]) == 200


def test_auth_disabled_operator_keys_record_a_name_only(client):
    created = client.post("/api/apikeys", json={"name": "local"}).json()
    row = _row(created["id"])
    assert row.created_by is None and row.created_by_name == get_settings().auth_username


def test_a_pre_change_ledger_gains_the_lifecycle_columns(tmp_path):
    """A deployed `api_keys` table predating SE-054, with a key in it: startup
    adds the columns, the key keeps NULLs (an unknown, never-offboarded creator)."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    db_module.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP INDEX ix_api_keys_created_by"))
        for column in ("created_by", "created_by_name", "last_used_at"):
            conn.execute(sa.text(f"ALTER TABLE api_keys DROP COLUMN {column}"))
        conn.execute(
            sa.text(
                "INSERT INTO api_keys (id, workspace_id, name, prefix, key_hash, enabled,"
                " created_at) VALUES ('k1', 'default', 'old', 'lp_live_old…', 'h', 1,"
                " '2026-01-01 00:00:00')"
            )
        )

    db_module._migrate(engine)

    assert_no_schema_drift(engine)
    with engine.begin() as conn:
        row = conn.execute(
            sa.text("SELECT created_by, created_by_name, last_used_at FROM api_keys")
        ).one()
    assert tuple(row) == (None, None, None)
    indexes = {index["name"] for index in sa.inspect(engine).get_indexes("api_keys")}
    assert "ix_api_keys_created_by" in indexes
