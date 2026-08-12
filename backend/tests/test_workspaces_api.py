"""Workspace administration: register / rename / delete, grants, and the list
a member is allowed to see. Grants are written through `PATCH /api/users/{id}`
so there is exactly one write path for them.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import Agent, UserWorkspace, Workspace
from app.services import users as users_service

ADMIN_CREDS = {"username": "operator", "password": "s3cret-pass"}
MEMBER_CREDS = {
    "username": "ws-admin-member",
    "email": "ws-admin-member@acme-corp.com",
    "password": "sufficient-pass",
}
NEW = {"id": "acct-usw1", "name": "West 1", "account_id": "444455556666", "region": "us-west-1"}


@pytest.fixture
def gated_app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN_CREDS["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN_CREDS["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


@pytest.fixture
def admin(gated_app):
    with TestClient(gated_app, client=("127.0.0.1", 4321)) as client:
        assert client.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        yield client


@pytest.fixture
def member_id(gated_app):
    """A registered, approved member with no workspace grant yet."""
    with TestClient(gated_app, client=("127.0.0.1", 4321)) as client:
        assert client.post("/api/auth/register", json=MEMBER_CREDS).status_code == 201
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, MEMBER_CREDS["username"])
        user.status = users_service.STATUS_ACTIVE
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        db.commit()
        return user.id
    finally:
        db.close()


class TestRegistration:
    def test_register_and_list(self, admin):
        created = admin.post("/api/workspaces", json=NEW)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["bootstrap_status"] == "registered"
        assert body["is_default"] is False
        # the resource map is never disclosed: it carries ARNs and pool ids
        assert "resources" not in body

        listed = admin.get("/api/workspaces").json()
        assert [row["id"] for row in listed["workspaces"]] == [NEW["id"], DEFAULT_WORKSPACE_ID]
        assert listed["all_workspaces"] is True

    def test_the_default_id_is_reserved(self, admin):
        response = admin.post("/api/workspaces", json={**NEW, "id": DEFAULT_WORKSPACE_ID})
        assert response.status_code == 400
        assert response.json()["code"] == "workspace.reserved_id"

    @pytest.mark.parametrize(
        "bad_id", ["a", "Upper", "with_underscore", "-leading", "x" * 33, "sp ace"]
    )
    def test_slug_validation(self, admin, bad_id):
        response = admin.post("/api/workspaces", json={**NEW, "id": bad_id})
        assert response.status_code in (400, 422), response.text
        if response.status_code == 400:
            assert response.json()["code"] == "workspace.invalid_id"

    @pytest.mark.parametrize("bad_region", ["", "west", "us_west_2", "us-west"])
    def test_region_shape_is_validated(self, admin, bad_region):
        response = admin.post("/api/workspaces", json={**NEW, "region": bad_region})
        assert response.status_code in (400, 422)
        if response.status_code == 400:
            assert response.json()["code"] == "workspace.invalid_region"

    def test_account_id_must_be_an_account_id(self, admin):
        response = admin.post("/api/workspaces", json={**NEW, "account_id": "4444"})
        assert response.status_code == 400
        assert response.json()["code"] == "workspace.invalid_account"

    def test_cross_account_is_refused_until_phase_3(self, admin):
        response = admin.post(
            "/api/workspaces", json={**NEW, "role_arn": "arn:aws:iam::4444:role/x"}
        )
        assert response.status_code == 400
        assert response.json()["code"] == "workspace.cross_account_unsupported"

    def test_a_duplicate_id_is_409(self, admin):
        assert admin.post("/api/workspaces", json=NEW).status_code == 201
        response = admin.post("/api/workspaces", json={**NEW, "region": "eu-west-1"})
        assert response.status_code == 409
        assert response.json()["code"] == "workspace.exists"

    def test_one_workspace_per_account_and_region_is_a_409_not_a_500(self, admin):
        """The UNIQUE constraint is a user-visible rule, so it must not surface as
        an IntegrityError."""
        assert admin.post("/api/workspaces", json=NEW).status_code == 201
        response = admin.post("/api/workspaces", json={**NEW, "id": "acct-usw1-again"})
        assert response.status_code == 409
        assert response.json()["code"] == "workspace.environment_taken"

    def test_only_admins_register(self, gated_app, member_id):
        db = SessionLocal()
        try:
            user = users_service.get_user(db, member_id)
            users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
            db.commit()
        finally:
            db.close()
        with TestClient(gated_app, client=("127.0.0.1", 4321)) as member:
            assert member.post(
                "/api/auth/login",
                json={
                    "username": MEMBER_CREDS["username"],
                    "password": MEMBER_CREDS["password"],
                },
            ).status_code == 200
            assert member.post("/api/workspaces", json=NEW).status_code == 403
            # ...but the switcher list is member-reachable, granted rows only
            assert [row["id"] for row in member.get("/api/workspaces").json()["workspaces"]] == [
                DEFAULT_WORKSPACE_ID
            ]


class TestRenameAndDelete:
    def test_rename(self, admin):
        assert admin.post("/api/workspaces", json=NEW).status_code == 201
        renamed = admin.patch(f"/api/workspaces/{NEW['id']}", json={"name": "West One"})
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "West One"
        assert admin.patch("/api/workspaces/nope", json={"name": "x"}).status_code == 404

    def test_default_cannot_be_deleted(self, admin):
        response = admin.delete(f"/api/workspaces/{DEFAULT_WORKSPACE_ID}")
        assert response.status_code == 400
        assert response.json()["code"] == "workspace.reserved_id"

    def test_delete_is_blocked_while_agents_live_in_it(self, admin, member_id):
        assert admin.post("/api/workspaces", json=NEW).status_code == 201
        admin.patch(f"/api/users/{member_id}", json={"workspaces": [NEW["id"]]})
        db = SessionLocal()
        try:
            agent = Agent(
                workspace_id=NEW["id"], name="resident", method="zip_runtime", status="active"
            )
            db.add(agent)
            db.commit()
            agent_id = agent.id
        finally:
            db.close()

        blocked = admin.delete(f"/api/workspaces/{NEW['id']}")
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "workspace.in_use"

        db = SessionLocal()
        try:
            db.get(Agent, agent_id).status = "deleted"
            db.commit()
        finally:
            db.close()

        assert admin.delete(f"/api/workspaces/{NEW['id']}").status_code == 200
        db = SessionLocal()
        try:
            assert db.get(Workspace, NEW["id"]) is None
            # the grants go with it, or a re-registered id would inherit them
            assert (
                db.query(UserWorkspace).filter(UserWorkspace.workspace_id == NEW["id"]).count()
                == 0
            )
        finally:
            db.close()


class TestGrants:
    def test_grants_are_set_replaced_and_cleared_through_the_user_patch(self, admin, member_id):
        assert admin.post("/api/workspaces", json=NEW).status_code == 201

        granted = admin.patch(
            f"/api/users/{member_id}", json={"workspaces": [DEFAULT_WORKSPACE_ID, NEW["id"]]}
        )
        assert granted.status_code == 200, granted.text
        assert granted.json()["workspaces"] == [NEW["id"], DEFAULT_WORKSPACE_ID]

        replaced = admin.patch(f"/api/users/{member_id}", json={"workspaces": [NEW["id"]]})
        assert replaced.json()["workspaces"] == [NEW["id"]]

        cleared = admin.patch(f"/api/users/{member_id}", json={"workspaces": None})
        assert cleared.json()["workspaces"] == []

    def test_an_unknown_workspace_id_is_refused(self, admin, member_id):
        response = admin.patch(f"/api/users/{member_id}", json={"workspaces": ["ghost"]})
        assert response.status_code == 400
        assert response.json()["code"] == "users.invalid_workspaces"
        assert "ghost" in response.json()["message"]

    def test_other_patch_fields_still_leave_grants_alone(self, admin, member_id):
        admin.patch(f"/api/users/{member_id}", json={"workspaces": [DEFAULT_WORKSPACE_ID]})
        patched = admin.patch(f"/api/users/{member_id}", json={"extend_days": 3})
        assert patched.json()["workspaces"] == [DEFAULT_WORKSPACE_ID]

    def test_the_grants_endpoint_lists_the_members(self, admin, member_id):
        admin.patch(f"/api/users/{member_id}", json={"workspaces": [DEFAULT_WORKSPACE_ID]})

        listed = admin.get(f"/api/workspaces/{DEFAULT_WORKSPACE_ID}/grants").json()

        assert listed["workspace_id"] == DEFAULT_WORKSPACE_ID
        assert [user["username"] for user in listed["users"]] == [MEMBER_CREDS["username"]]
        assert admin.get("/api/workspaces/ghost/grants").status_code == 404

    def test_the_user_list_carries_the_grants(self, admin, member_id):
        admin.patch(f"/api/users/{member_id}", json={"workspaces": [DEFAULT_WORKSPACE_ID]})
        row = next(
            item for item in admin.get("/api/users").json()["items"] if item["id"] == member_id
        )
        assert row["workspaces"] == [DEFAULT_WORKSPACE_ID]

    def test_deleting_a_user_takes_its_grants(self, admin, member_id):
        admin.patch(f"/api/users/{member_id}", json={"workspaces": [DEFAULT_WORKSPACE_ID]})
        assert admin.delete(f"/api/users/{member_id}").status_code == 200
        db = SessionLocal()
        try:
            assert db.query(UserWorkspace).count() == 0
        finally:
            db.close()


def test_pre_workspace_accounts_keep_their_access_after_the_upgrade(tmp_path):
    """The migration grants `default` to accounts that predate workspaces —
    otherwise an upgrade would lock every member out of the console. It is a
    one-shot: repeating it would resurrect revoked grants."""
    import sqlalchemy as sa

    from app.core import db as db_module

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    db_module.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM workspaces"))
        conn.execute(
            sa.text(
                "INSERT INTO users (id, username, username_key, email, password_hash,"
                " role, status, created_by, login_count, created_at, updated_at)"
                " VALUES ('u1', 'Legacy', 'legacy', 'legacy@example.com', 'x', 'member',"
                " 'active', 'self', 0, :now, :now)"
            ),
            {"now": "2026-08-01 00:00:00.000000"},
        )

    db_module._seed_default_workspace(engine)

    with engine.begin() as conn:
        assert conn.execute(
            sa.text("SELECT workspace_id FROM user_workspaces WHERE user_id = 'u1'")
        ).scalar_one() == DEFAULT_WORKSPACE_ID
        conn.execute(sa.text("DELETE FROM user_workspaces"))

    db_module._seed_default_workspace(engine)

    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM user_workspaces")).scalar_one() == 0
