"""Workspace administration: register / rename / delete, grants, and the list
a member is allowed to see. Grants are written through `PATCH /api/users/{id}`
so there is exactly one write path for them.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.routers.workspaces as workspaces_router
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalDataset
from app.main import create_app
from app.models.ledger import Agent, UserWorkspace, Workspace
from app.routers.workspaces import _referencing_rows
from app.services import users as users_service

ADMIN_CREDS = {"username": "operator", "password": "s3cret-pass"}
MEMBER_CREDS = {
    "username": "ws-admin-member",
    "email": "ws-admin-member@acme-corp.com",
    "password": "sufficient-pass",
}
NEW = {"id": "acct-usw1", "name": "West 1", "account_id": "444455556666", "region": "us-west-1"}
CROSS_ACCOUNT = {
    "role_arn": f"arn:aws:iam::{NEW['account_id']}:role/LaunchpadWorkspaceRole",
    "external_id": "launchpad-acct-usw1",
}


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

    def test_register_a_cross_account_workspace(self, admin):
        response = admin.post("/api/workspaces", json={**NEW, **CROSS_ACCOUNT})
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["cross_account"] is True
        # an admin sees the role, because the spoke's stack has to be deployed as
        # it; the external id is a shared secret with that stack's trust policy and
        # never comes back out of the ledger
        assert body["role_arn"] == CROSS_ACCOUNT["role_arn"]
        assert "external_id" not in body
        db = SessionLocal()
        try:
            row = db.get(Workspace, NEW["id"])
            assert row.role_arn == CROSS_ACCOUNT["role_arn"]
            assert row.external_id == CROSS_ACCOUNT["external_id"]
        finally:
            db.close()

    def test_a_same_account_workspace_is_not_cross_account(self, admin):
        assert admin.post("/api/workspaces", json=NEW).json()["cross_account"] is False

    @pytest.mark.parametrize("field", ["role_arn", "external_id"])
    def test_the_role_and_its_external_id_are_inseparable(self, admin, field):
        """A role without an ExternalId can never satisfy the spoke's trust policy,
        and an ExternalId without a role would be silently unused."""
        response = admin.post("/api/workspaces", json={**NEW, field: CROSS_ACCOUNT[field]})
        assert response.status_code == 400
        assert response.json()["code"] == "workspace.role_and_external_id_required"

    @pytest.mark.parametrize(
        "bad_arn",
        [
            "LaunchpadWorkspaceRole",
            "arn:aws:iam::444455556666:user/river",
            "arn:aws:iam::4444:role/x",
            "arn:aws:iam::444455556666:role/",
            # longer than the ledger column, which would truncate it on insert
            "arn:aws:iam::444455556666:role/" + "x" * 300,
        ],
    )
    def test_role_arn_shape_is_validated(self, admin, bad_arn):
        response = admin.post(
            "/api/workspaces", json={**NEW, **CROSS_ACCOUNT, "role_arn": bad_arn}
        )
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "workspace.invalid_role_arn"

    def test_the_role_must_live_in_the_declared_account(self, admin):
        """The operator's most likely typo, and the one that would otherwise only
        surface as an account-mismatch failure halfway into a bootstrap run."""
        response = admin.post(
            "/api/workspaces",
            json={**NEW, **CROSS_ACCOUNT, "role_arn": "arn:aws:iam::999988887777:role/Ws"},
        )
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == "workspace.role_account_mismatch"
        assert body["detail"] == {
            "role_account_id": "999988887777",
            "account_id": NEW["account_id"],
        }

    # STS's ExternalId pattern is ASCII, so a non-ASCII one is refused here rather
    # than at the first AssumeRole.
    @pytest.mark.parametrize(
        "bad_external_id", ["x", "y" * 129, "has space", "quote'd", "跨账户"]
    )
    def test_external_id_shape_is_validated(self, admin, bad_external_id):
        response = admin.post(
            "/api/workspaces",
            json={**NEW, **CROSS_ACCOUNT, "external_id": bad_external_id},
        )
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "workspace.invalid_external_id"

    def test_the_default_id_stays_reserved_even_with_a_role(self, admin):
        response = admin.post(
            "/api/workspaces", json={**NEW, **CROSS_ACCOUNT, "id": DEFAULT_WORKSPACE_ID}
        )
        assert response.status_code == 400
        assert response.json()["code"] == "workspace.reserved_id"

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

    def test_delete_is_blocked_while_any_row_still_names_it(self, admin, member_id):
        """Including a soft-deleted agent: the row still references the workspace,
        and after the workspace row goes nothing can resolve it."""
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
        assert blocked.json()["detail"]["rows"] == {"agents": 1}

        db = SessionLocal()
        try:
            db.get(Agent, agent_id).status = "deleted"
            db.commit()
        finally:
            db.close()

        still_blocked = admin.delete(f"/api/workspaces/{NEW['id']}")
        assert still_blocked.status_code == 409
        assert still_blocked.json()["detail"]["rows"] == {"agents": 1}

        db = SessionLocal()
        try:
            db.delete(db.get(Agent, agent_id))
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

    def test_delete_is_blocked_by_a_row_in_any_scoped_table(self, admin):
        """Not just agents: an evaluation dataset (no agent, no AWS resource) is
        enough, and the 409 names the table so the operator knows what to clear."""
        assert admin.post("/api/workspaces", json=NEW).status_code == 201
        db = SessionLocal()
        try:
            dataset = EvalDataset(
                workspace_id=NEW["id"], name="leftover", items=[{"prompt": "p"}]
            )
            db.add(dataset)
            db.commit()
            dataset_id = dataset.id
        finally:
            db.close()

        blocked = admin.delete(f"/api/workspaces/{NEW['id']}")
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "workspace.in_use"
        assert blocked.json()["detail"]["rows"] == {"eval_datasets": 1}

        db = SessionLocal()
        try:
            db.delete(db.get(EvalDataset, dataset_id))
            db.commit()
        finally:
            db.close()

        assert admin.delete(f"/api/workspaces/{NEW['id']}").status_code == 200

    def test_the_guard_queries_every_scoped_table(self, admin):
        """The guard is only as wide as the table list it walks, and every table on
        that list has to be queryable — a name or column that drifted raises here
        rather than silently skipping a table's rows."""
        assert admin.post("/api/workspaces", json=NEW).status_code == 201
        db = SessionLocal()
        try:
            assert _referencing_rows(db, NEW["id"]) == {}
            assert _referencing_rows(db, DEFAULT_WORKSPACE_ID) == {}
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


class TestHubIdentity:
    """`GET /api/workspaces/hub-identity` — what the operator pastes into the
    spoke stack's `HubRoleArn` parameter."""

    @pytest.fixture(autouse=True)
    def _uncached(self, monkeypatch):
        monkeypatch.setattr(workspaces_router, "_HUB_IDENTITY", None)

    @staticmethod
    def _stub_sts(monkeypatch, arn: str, calls: list[int] | None = None):
        class Sts:
            def get_caller_identity(self):
                if calls is not None:
                    calls.append(1)
                return {"Account": "434444145045", "Arn": arn}

        monkeypatch.setattr(
            workspaces_router,
            "default_workspace_context",
            lambda: SimpleNamespace(client=lambda service: Sts()),
        )

    def test_an_assumed_role_is_reported_as_the_role_a_trust_policy_can_name(
        self, admin, monkeypatch
    ):
        """A hub on EC2 reports `sts:...:assumed-role/<role>/<instance id>`, which
        is not a legal trust-policy principal — the console must show the role."""
        self._stub_sts(
            monkeypatch,
            "arn:aws:sts::434444145045:assumed-role/admin_role_for_workshop/i-07",
        )

        body = admin.get("/api/workspaces/hub-identity").json()

        assert body["account_id"] == "434444145045"
        assert body["role_arn"] == "arn:aws:iam::434444145045:role/admin_role_for_workshop"
        assert body["caller_arn"].startswith("arn:aws:sts::")

    def test_the_identity_is_read_once(self, admin, monkeypatch):
        calls: list[int] = []
        self._stub_sts(monkeypatch, "arn:aws:iam::434444145045:role/hub", calls)

        admin.get("/api/workspaces/hub-identity")
        admin.get("/api/workspaces/hub-identity")

        assert len(calls) == 1

    def test_a_hub_without_credentials_says_so(self, admin, monkeypatch):
        """Rather than a 500 the operator cannot act on: a cross-account workspace
        is unregisterable until the hub has an identity of its own."""

        def broken():
            raise RuntimeError("no credentials")

        monkeypatch.setattr(
            workspaces_router,
            "default_workspace_context",
            lambda: SimpleNamespace(client=lambda service: broken()),
        )

        response = admin.get("/api/workspaces/hub-identity")

        assert response.status_code == 502
        assert response.json()["code"] == "workspace.hub_identity_unavailable"

    @pytest.mark.parametrize(
        ("caller", "expected"),
        [
            (
                "arn:aws:sts::111122223333:assumed-role/launchpad-hub/session",
                "arn:aws:iam::111122223333:role/launchpad-hub",
            ),
            (
                "arn:aws-cn:sts::111122223333:assumed-role/hub/i-1",
                "arn:aws-cn:iam::111122223333:role/hub",
            ),
            # already a principal a trust policy accepts — left alone
            ("arn:aws:iam::111122223333:role/hub", "arn:aws:iam::111122223333:role/hub"),
            ("arn:aws:iam::111122223333:user/river", "arn:aws:iam::111122223333:user/river"),
        ],
    )
    def test_normalization(self, caller, expected):
        assert workspaces_router.hub_role_arn(caller) == expected


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
