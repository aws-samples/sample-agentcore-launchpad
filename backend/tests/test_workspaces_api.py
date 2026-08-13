"""Workspace administration: register / rename / detach / purge, the access
probe, grants, and the list a member is allowed to see. Grants have two write
shapes onto one table (`user_workspaces`): the per-user replacement
`PATCH /api/users/{id}` and the workspace-side bulk `PUT /{id}/grants`.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.routers.workspaces as workspaces_router
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalDataset
from app.main import create_app
from app.models.ledger import Agent, Job, PolicyChange, UserWorkspace, Workspace
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


class TestPreflight:
    """`POST /preflight` — "can this hub assume that role?", asked before the
    registration exists.

    It closes the trap that motivated the purge feature: a wrong ExternalId is
    otherwise invisible until the bootstrap job signs its first request, minutes
    later, leaving a failed registration behind.
    """

    BODY = {**{k: NEW[k] for k in ("account_id", "region")}, **CROSS_ACCOUNT}

    @staticmethod
    def _probe(monkeypatch, result=None, error=None) -> list[tuple]:
        """Record the probe's arguments; STS itself never runs in a unit test."""
        calls: list[tuple] = []

        def fake(account_id, region, role_arn, external_id):
            calls.append((account_id, region, role_arn, external_id))
            if error is not None:
                raise error
            return result

        monkeypatch.setattr(workspaces_router.aws_clients, "probe_caller_identity", fake)
        return calls

    @staticmethod
    def _ledger_counts() -> tuple[int, int]:
        db = SessionLocal()
        try:
            return db.query(Workspace).count(), db.query(Job).count()
        finally:
            db.close()

    def test_a_reachable_role_reports_the_account_it_reached(self, admin, monkeypatch):
        calls = self._probe(monkeypatch, result=NEW["account_id"])
        before = self._ledger_counts()

        response = admin.post("/api/workspaces/preflight", json=self.BODY)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "ok": True,
            "caller_account": NEW["account_id"],
            "diagnostic": None,
        }
        assert calls == [
            (
                NEW["account_id"],
                NEW["region"],
                CROSS_ACCOUNT["role_arn"],
                CROSS_ACCOUNT["external_id"],
            )
        ]
        # a probe records nothing: the operator may still be editing the form
        assert self._ledger_counts() == before

    def test_a_denied_assume_role_is_a_result_not_an_error(self, admin, monkeypatch):
        """200 with `ok: false`: the operator asked a question and got its answer.
        A 502 would read as "the console is broken" for the case this exists for."""
        self._probe(
            monkeypatch,
            error=ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "not authorized to perform"}},
                "AssumeRole",
            ),
        )
        before = self._ledger_counts()

        response = admin.post("/api/workspaces/preflight", json=self.BODY)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is False and body["caller_account"] is None
        # the shared diagnostic, so the console says the same thing here as it
        # does when a bootstrap stage or a console request hits the same wall
        assert "trust policy" in body["diagnostic"] and "ExternalId" in body["diagnostic"]
        assert "not authorized to perform" in body["diagnostic"]
        assert self._ledger_counts() == before

    def test_any_other_client_error_is_not_dressed_up_as_a_verdict(
        self, admin, monkeypatch
    ):
        """Reporting an unrelated AWS failure as `ok: false` would send the
        operator after a trust policy that is fine."""
        self._probe(
            monkeypatch,
            error=ClientError({"Error": {"Code": "AccessDenied"}}, "GetCallerIdentity"),
        )

        with pytest.raises(ClientError):
            admin.post("/api/workspaces/preflight", json=self.BODY)

    @pytest.mark.parametrize(
        ("patch", "code"),
        [
            ({"account_id": "4444"}, "workspace.invalid_account"),
            ({"region": "us_west_2"}, "workspace.invalid_region"),
            ({"role_arn": "LaunchpadWorkspaceRole"}, "workspace.invalid_role_arn"),
            ({"external_id": "has space"}, "workspace.invalid_external_id"),
            (
                {"role_arn": "arn:aws:iam::999988887777:role/Ws"},
                "workspace.role_account_mismatch",
            ),
            ({"external_id": None}, "workspace.role_and_external_id_required"),
            # nothing to assume: the hub's own account needs no preflight
            (
                {"role_arn": None, "external_id": None},
                "workspace.role_and_external_id_required",
            ),
        ],
    )
    def test_it_validates_exactly_what_registration_validates(
        self, admin, monkeypatch, patch, code
    ):
        """Same validators, same codes — a preflight that passed values
        `POST /api/workspaces` then rejected would be worse than none."""
        calls = self._probe(monkeypatch, result=NEW["account_id"])

        response = admin.post("/api/workspaces/preflight", json={**self.BODY, **patch})

        assert response.status_code == 400, response.text
        assert response.json()["code"] == code
        assert calls == []  # refused before anything reached AWS

    def test_only_admins_probe(self, gated_app, member_id, monkeypatch):
        """It signs a request with the hub's credentials, so it is admin-only like
        the rest of workspace administration."""
        calls = self._probe(monkeypatch, result=NEW["account_id"])
        with TestClient(gated_app, client=("127.0.0.1", 4321)) as member:
            assert member.post(
                "/api/auth/login",
                json={
                    "username": MEMBER_CREDS["username"],
                    "password": MEMBER_CREDS["password"],
                },
            ).status_code == 200

            response = member.post("/api/workspaces/preflight", json=self.BODY)

        assert response.status_code == 403
        assert calls == []


class TestPurge:
    """`POST /{id}/purge` — the escape hatch from the detach guard, for a
    registration that never became a usable environment.

    Its whole reason to exist is the state the detach guard cannot clear: a
    bootstrap that failed leaves one FAILED job row, which blocks DELETE *and*
    keeps the UNIQUE(account, region) slot, so the operator cannot re-register
    the environment they were fixing.
    """

    @staticmethod
    def _failed_registration(admin, **columns) -> None:
        assert admin.post("/api/workspaces", json=NEW).status_code == 201
        db = SessionLocal()
        try:
            row = db.get(Workspace, NEW["id"])
            row.bootstrap_status = "failed"
            row.resources = {"gateway_id": "gw-abc", "memory_id": "mem-abc"}
            for column, value in columns.items():
                setattr(row, column, value)
            db.add(Job(workspace_id=NEW["id"], type="workspace_bootstrap", status="failed"))
            db.commit()
        finally:
            db.close()

    def test_a_failed_registration_purges_with_its_rows_and_grants(self, admin, member_id):
        self._failed_registration(admin)
        admin.patch(f"/api/users/{member_id}", json={"workspaces": [NEW["id"]]})
        assert admin.delete(f"/api/workspaces/{NEW['id']}").status_code == 409  # the guard

        purged = admin.post(f"/api/workspaces/{NEW['id']}/purge")

        assert purged.status_code == 200, purged.text
        body = purged.json()
        assert body["purged"] is True and body["dry_run"] is False
        assert body["rows"] == {"jobs": 1}
        # names only: what a failed run had already provisioned in AWS and purge
        # does not remove
        assert body["resource_keys"] == ["gateway_id", "memory_id"]

        db = SessionLocal()
        try:
            assert db.get(Workspace, NEW["id"]) is None
            assert db.query(Job).filter(Job.workspace_id == NEW["id"]).count() == 0
            assert (
                db.query(UserWorkspace).filter(UserWorkspace.workspace_id == NEW["id"]).count()
                == 0
            )
        finally:
            db.close()

    def test_the_account_and_region_slot_is_free_again(self, admin):
        """The point of the feature: re-registering the environment that was being
        fixed must not need ledger surgery."""
        self._failed_registration(admin)
        taken = admin.post("/api/workspaces", json={**NEW, "id": "acct-usw1-retry"})
        assert taken.status_code == 409 and taken.json()["code"] == "workspace.environment_taken"

        assert admin.post(f"/api/workspaces/{NEW['id']}/purge").status_code == 200

        again = admin.post("/api/workspaces", json={**NEW, "id": "acct-usw1-retry"})
        assert again.status_code == 201, again.text

    def test_a_registered_workspace_purges_too(self, admin):
        """An abandoned registration that was never bootstrapped is residue as
        much as a failed one."""
        assert admin.post("/api/workspaces", json=NEW).status_code == 201
        purged = admin.post(f"/api/workspaces/{NEW['id']}/purge")
        assert purged.status_code == 200
        assert purged.json()["rows"] == {} and purged.json()["resource_keys"] == []

    def test_the_default_workspace_is_never_purged(self, admin):
        response = admin.post(f"/api/workspaces/{DEFAULT_WORKSPACE_ID}/purge")
        assert response.status_code == 400
        assert response.json()["code"] == "workspace.purge_refused"
        assert response.json()["detail"]["reason"] == "default"

    @pytest.mark.parametrize("status", ["ready", "bootstrapping"])
    def test_a_usable_or_running_workspace_is_refused(self, admin, status):
        """`ready` is a working environment (retiring it means deleting AWS
        resources, which this does not do); `bootstrapping` has a live run whose
        writes would land on rows this transaction just deleted."""
        self._failed_registration(admin, bootstrap_status=status)

        response = admin.post(f"/api/workspaces/{NEW['id']}/purge")

        assert response.status_code == 409
        assert response.json()["code"] == "workspace.purge_refused"
        assert response.json()["detail"]["reason"] == status
        db = SessionLocal()
        try:
            assert db.get(Workspace, NEW["id"]) is not None
        finally:
            db.close()

    def test_a_live_agent_refuses_but_a_soft_deleted_one_does_not(self, admin):
        """A live agent means real usage — purging it would orphan its AgentCore
        runtime with nothing left naming it. A soft-deleted agent's runtime is
        already gone on the AWS side, so its row is residue to clean."""
        self._failed_registration(admin)
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

        refused = admin.post(f"/api/workspaces/{NEW['id']}/purge")
        assert refused.status_code == 409
        assert refused.json()["detail"]["reason"] == "agents"

        db = SessionLocal()
        try:
            db.get(Agent, agent_id).status = "deleted"
            db.commit()
        finally:
            db.close()

        purged = admin.post(f"/api/workspaces/{NEW['id']}/purge")
        assert purged.status_code == 200, purged.text
        assert purged.json()["rows"] == {"agents": 1, "jobs": 1}
        db = SessionLocal()
        try:
            assert db.get(Agent, agent_id) is None
        finally:
            db.close()

    def test_an_audit_row_purges_although_the_orm_refuses_to_delete_one(self, admin):
        """`PolicyChange` carries an immutability listener, so the raw per-table
        DELETE is what makes the audit trail purgeable at all — an ORM delete
        would raise and leave the workspace undetachable forever."""
        self._failed_registration(admin)
        db = SessionLocal()
        try:
            change = PolicyChange(
                workspace_id=NEW["id"],
                gateway_id="gw-abc",
                gateway_arn="arn:aws:bedrock-agentcore:us-west-1:1:gateway/gw-abc",
                gateway_name="left-behind-gw",
                operation="engine_attach",
                status="succeeded",
                before={},
                requested={"mode": "ENFORCE"},
                operator="operator",
            )
            db.add(change)
            db.commit()
        finally:
            db.close()

        purged = admin.post(f"/api/workspaces/{NEW['id']}/purge")

        assert purged.status_code == 200, purged.text
        assert purged.json()["rows"] == {"policy_changes": 1, "jobs": 1}
        db = SessionLocal()
        try:
            left = db.query(PolicyChange).filter(PolicyChange.workspace_id == NEW["id"]).count()
            assert left == 0
        finally:
            db.close()

    def test_a_dry_run_reports_what_would_go_and_deletes_nothing(self, admin):
        """What the confirm dialog calls when it opens, so the operator reads the
        counts before committing rather than after."""
        self._failed_registration(admin)

        preview = admin.post(f"/api/workspaces/{NEW['id']}/purge?dry_run=true")

        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["purged"] is False and body["dry_run"] is True
        assert body["rows"] == {"jobs": 1}
        assert body["resource_keys"] == ["gateway_id", "memory_id"]
        db = SessionLocal()
        try:
            assert db.get(Workspace, NEW["id"]) is not None
            assert db.query(Job).filter(Job.workspace_id == NEW["id"]).count() == 1
        finally:
            db.close()

    def test_a_dry_run_is_a_real_preflight(self, admin):
        """The guardrails run before it answers, so a dialog that opens on a
        workspace that turned READY meanwhile says so instead of promising a purge
        the confirm would then refuse."""
        self._failed_registration(admin, bootstrap_status="ready")
        response = admin.post(f"/api/workspaces/{NEW['id']}/purge?dry_run=true")
        assert response.status_code == 409
        assert response.json()["detail"]["reason"] == "ready"

    def test_it_takes_nothing_from_another_workspace(self, admin, member_id):
        """Every DELETE is predicated on the target id, so a neighbour sharing the
        same tables — including the `default` workspace the console runs on — keeps
        its rows, its grants and its registration."""
        self._failed_registration(admin)
        neighbour = {**NEW, "id": "acct-usw2", "region": "us-west-2"}
        assert admin.post("/api/workspaces", json=neighbour).status_code == 201
        admin.patch(f"/api/users/{member_id}", json={"workspaces": [neighbour["id"]]})
        db = SessionLocal()
        try:
            db.add(Job(workspace_id=neighbour["id"], type="workspace_bootstrap", status="failed"))
            db.add(Job(workspace_id=DEFAULT_WORKSPACE_ID, type="deploy", status="succeeded"))
            db.add(Agent(workspace_id=neighbour["id"], name="theirs", method="zip_runtime"))
            db.commit()
        finally:
            db.close()

        assert admin.post(f"/api/workspaces/{NEW['id']}/purge").status_code == 200

        db = SessionLocal()
        try:
            assert db.get(Workspace, neighbour["id"]) is not None
            assert _referencing_rows(db, neighbour["id"]) == {"agents": 1, "jobs": 1}
            assert _referencing_rows(db, DEFAULT_WORKSPACE_ID) == {"jobs": 1}
            assert (
                db.query(UserWorkspace)
                .filter(UserWorkspace.workspace_id == neighbour["id"])
                .count()
                == 1
            )
        finally:
            db.close()

    def test_a_run_claiming_the_row_first_wins_and_nothing_is_deleted(
        self, admin, monkeypatch
    ):
        """The guardrails are a read and handlers run on a threadpool, so a
        bootstrap POST can claim the row between the check and the deletes. The
        workspace row goes with a conditional DELETE for exactly that: the loser
        rolls back whole, rather than deleting the rows a live run is about to
        record its AWS resources on."""
        self._failed_registration(admin)

        def claim_it(db, row):
            """Stand in for the racing bootstrap: same conditional UPDATE, applied
            after this request's guardrails have already passed."""
            counts = _referencing_rows(db, row.id)
            db.execute(
                Workspace.__table__.update()
                .where(Workspace.id == row.id)
                .values(bootstrap_status="bootstrapping")
            )
            db.commit()
            return counts, []

        monkeypatch.setattr(workspaces_router, "_assert_purgeable", claim_it)

        response = admin.post(f"/api/workspaces/{NEW['id']}/purge")

        assert response.status_code == 409
        assert response.json()["code"] == "workspace.purge_refused"
        assert response.json()["detail"]["reason"] == "bootstrapping"
        db = SessionLocal()
        try:
            assert db.get(Workspace, NEW["id"]) is not None
            assert db.query(Job).filter(Job.workspace_id == NEW["id"]).count() == 1
        finally:
            db.close()

    def test_an_unknown_workspace_is_404(self, admin):
        assert admin.post("/api/workspaces/ghost/purge").status_code == 404

    def test_only_admins_purge(self, gated_app, admin, member_id):
        self._failed_registration(admin)
        admin.patch(f"/api/users/{member_id}", json={"workspaces": [NEW["id"]]})
        with TestClient(gated_app, client=("127.0.0.1", 4321)) as member:
            assert member.post(
                "/api/auth/login",
                json={
                    "username": MEMBER_CREDS["username"],
                    "password": MEMBER_CREDS["password"],
                },
            ).status_code == 200

            response = member.post(f"/api/workspaces/{NEW['id']}/purge")

        assert response.status_code == 403
        db = SessionLocal()
        try:
            assert db.get(Workspace, NEW["id"]) is not None
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

    def test_the_grants_endpoint_lists_every_member_and_who_holds_it(self, admin, member_id):
        """Since R8 the endpoint answers "who *could* hold this workspace, and who
        does" — the console needs both to offer grant and revoke in one table."""
        admin.patch(f"/api/users/{member_id}", json={"workspaces": [DEFAULT_WORKSPACE_ID]})

        listed = admin.get(f"/api/workspaces/{DEFAULT_WORKSPACE_ID}/grants").json()

        assert listed["workspace_id"] == DEFAULT_WORKSPACE_ID
        assert [user["username"] for user in listed["users"]] == [MEMBER_CREDS["username"]]
        assert listed["users"][0]["granted"] is True
        assert listed["users"][0]["status"] == "active"
        assert listed["total"] == 1 and listed["granted_total"] == 1
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


@pytest.fixture
def members(gated_app):
    """Six members with predictable names/emails, one admin, one disabled account.

    Registered through the real endpoint so password rules and the `username_key`
    lowercase form are the app's own, then activated directly — the fixture is
    about the grants table, not about approval.
    """
    created: list[dict[str, str]] = []
    with TestClient(gated_app, client=("127.0.0.1", 4321)) as client:
        for index, (username, email) in enumerate(
            [
                ("ada", "ada@northwind.example"),
                ("bruno", "bruno@northwind.example"),
                ("carla", "carla@acme-corp.com"),
                ("dan", "dan@acme-corp.com"),
                ("eve", "eve@northwind.example"),
                ("frank", "frank@acme-corp.com"),
            ]
        ):
            assert client.post(
                "/api/auth/register",
                json={"username": username, "email": email, "password": "sufficient-pass"},
            ).status_code == 201
            created.append({"username": username, "email": email, "index": index})
    db = SessionLocal()
    try:
        rows = {}
        for entry in created:
            user = users_service.find_by_username(db, entry["username"])
            user.status = users_service.STATUS_ACTIVE
            user.expires_at = datetime.now(UTC) + timedelta(days=7)
            rows[entry["username"]] = user.id
        # one promoted account: an admin must never appear as a grantable row,
        # and a stale grant row it kept must not count towards granted_total
        promoted = users_service.find_by_username(db, "frank")
        promoted.role = "admin"
        db.add(UserWorkspace(user_id=promoted.id, workspace_id=DEFAULT_WORKSPACE_ID))
        # one deactivated member: still grantable, but the table says `disabled`
        users_service.find_by_username(db, "eve").status = users_service.STATUS_DISABLED
        db.commit()
        return rows
    finally:
        db.close()


class TestGrantsListing:
    """`GET /{id}/grants` — the members table behind the detail view."""

    WS = DEFAULT_WORKSPACE_ID

    def _get(self, admin, **params) -> dict:
        response = admin.get(f"/api/workspaces/{self.WS}/grants", params=params)
        assert response.status_code == 200, response.text
        return response.json()

    def test_every_member_is_listed_with_its_grant_flag(self, admin, members):
        body = self._get(admin)

        # alphabetical by the stored lowercase key, and `frank` is absent: he was
        # promoted to admin, so he reaches every workspace by role
        assert [user["username"] for user in body["users"]] == [
            "ada",
            "bruno",
            "carla",
            "dan",
            "eve",
        ]
        assert all(user["granted"] is False for user in body["users"])
        assert body["total"] == 5
        # frank's leftover grant row does not count: it is not a revocable grant
        assert body["granted_total"] == 0

    def test_an_account_holding_another_workspace_is_still_listed_as_ungranted(
        self, admin, members
    ):
        """The workspace predicate has to live in the JOIN: as a WHERE it would
        hide every account that holds some other workspace."""
        assert admin.post("/api/workspaces", json=NEW).status_code == 201
        admin.put(f"/api/workspaces/{NEW['id']}/grants", json={"grant": [members["ada"]]})

        body = self._get(admin)

        ada = next(user for user in body["users"] if user["username"] == "ada")
        assert ada["granted"] is False
        assert body["total"] == 5 and body["granted_total"] == 0

    @pytest.mark.parametrize(
        ("q", "expected"),
        [
            ("ada", ["ada"]),
            ("AD", ["ada"]),  # case-insensitive on the username
            ("acme-corp", ["carla", "dan"]),  # matched on the email
            ("NORTHWIND", ["ada", "bruno", "eve"]),  # case-insensitive on the email
            ("nobody", []),
        ],
    )
    def test_search_covers_username_and_email(self, admin, members, q, expected):
        body = self._get(admin, q=q)
        assert [user["username"] for user in body["users"]] == expected
        assert body["total"] == len(expected)

    def test_the_granted_filter(self, admin, members):
        admin.put(
            f"/api/workspaces/{self.WS}/grants",
            json={"grant": [members["ada"], members["dan"]]},
        )

        granted = self._get(admin, granted="granted")
        ungranted = self._get(admin, granted="ungranted")

        assert [user["username"] for user in granted["users"]] == ["ada", "dan"]
        assert [user["username"] for user in ungranted["users"]] == ["bruno", "carla", "eve"]
        assert granted["total"] == 2 and ungranted["total"] == 3
        # a property of the workspace, so it does not follow the filter
        assert granted["granted_total"] == ungranted["granted_total"] == 2

    def test_search_and_filter_compose(self, admin, members):
        admin.put(f"/api/workspaces/{self.WS}/grants", json={"grant": [members["carla"]]})

        body = self._get(admin, q="acme-corp", granted="ungranted")

        assert [user["username"] for user in body["users"]] == ["dan"]
        assert body["total"] == 1 and body["granted_total"] == 1

    def test_pagination_reports_the_full_total(self, admin, members):
        first = self._get(admin, limit=2)
        second = self._get(admin, limit=2, offset=2)
        past_the_end = self._get(admin, limit=2, offset=99)

        assert [user["username"] for user in first["users"]] == ["ada", "bruno"]
        assert [user["username"] for user in second["users"]] == ["carla", "dan"]
        assert past_the_end["users"] == []
        # the count is of matching rows, not of the returned window
        assert first["total"] == second["total"] == 5

    def test_the_limit_is_capped_and_reported(self, admin, members):
        """An uncapped limit would let one request page the whole account table.

        The cap has to be at least the console's largest page size: the pager
        computes its offsets from the size it asked for, so a lower cap would make
        the rows between the cap and that size unreachable.
        """
        body = self._get(admin, limit=5000)
        assert body["limit"] == workspaces_router._GRANTS_LIMIT_MAX == 200
        assert self._get(admin, limit=0)["limit"] == 1
        assert self._get(admin, offset=-5)["offset"] == 0

    def test_a_wildcard_in_the_search_is_a_literal(self, admin, members):
        """`_` is legal in a username and an email, so an unescaped needle would
        make `ada_b` match `adaXb` — a filter that quietly over-matches."""
        assert self._get(admin, q="ad_")["users"] == []
        assert [user["username"] for user in self._get(admin, q="ad")["users"]] == ["ada"]
        assert self._get(admin, q="%")["users"] == []

    def test_an_unknown_filter_is_refused(self, admin, members):
        response = admin.get(f"/api/workspaces/{self.WS}/grants", params={"granted": "maybe"})
        assert response.status_code == 400
        assert response.json()["code"] == "workspace.invalid_grant_filter"

    def test_only_admins_read_the_table(self, gated_app, admin, members):
        with TestClient(gated_app, client=("127.0.0.1", 4321)) as member:
            assert member.post(
                "/api/auth/login", json={"username": "ada", "password": "sufficient-pass"}
            ).status_code == 200
            assert member.get(f"/api/workspaces/{self.WS}/grants").status_code == 403


class TestGrantsBatchWrite:
    """`PUT /{id}/grants` — the workspace-side bulk complement to
    `PATCH /api/users/{id}` (which stays a per-user full replacement)."""

    WS = DEFAULT_WORKSPACE_ID

    def _held(self) -> set[str]:
        db = SessionLocal()
        try:
            return set(
                db.scalars(
                    select(UserWorkspace.user_id).where(
                        UserWorkspace.workspace_id == self.WS
                    )
                )
            )
        finally:
            db.close()

    def test_grant_and_revoke_in_one_call(self, admin, members):
        admin.put(f"/api/workspaces/{self.WS}/grants", json={"grant": [members["ada"]]})

        response = admin.put(
            f"/api/workspaces/{self.WS}/grants",
            json={"grant": [members["bruno"], members["carla"]], "revoke": [members["ada"]]},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["added"] == 2 and body["removed"] == 1
        assert body["granted_total"] == 2
        assert self._held() == {members["bruno"], members["carla"], members["frank"]}

    def test_regranting_is_a_no_op_rather_than_a_conflict(self, admin, members):
        """The composite primary key would raise on a duplicate insert, and two
        admins acting on overlapping selections is the normal case."""
        admin.put(f"/api/workspaces/{self.WS}/grants", json={"grant": [members["ada"]]})

        again = admin.put(
            f"/api/workspaces/{self.WS}/grants",
            json={"grant": [members["ada"], members["bruno"]]},
        )

        assert again.status_code == 200, again.text
        assert again.json()["added"] == 1  # only bruno actually changed
        assert again.json()["granted_total"] == 2

    def test_revoking_what_was_never_granted_is_a_no_op(self, admin, members):
        response = admin.put(
            f"/api/workspaces/{self.WS}/grants", json={"revoke": [members["ada"]]}
        )
        assert response.status_code == 200
        assert response.json() == {
            "workspace_id": self.WS,
            "added": 0,
            "removed": 0,
            "granted_total": 0,
        }

    def test_an_empty_call_changes_nothing(self, admin, members):
        before = self._held()
        response = admin.put(f"/api/workspaces/{self.WS}/grants", json={})
        assert response.status_code == 200
        assert self._held() == before

    def test_the_same_account_cannot_be_granted_and_revoked(self, admin, members):
        """Silently letting one win would make the result depend on the order the
        endpoint happens to apply them in."""
        response = admin.put(
            f"/api/workspaces/{self.WS}/grants",
            json={"grant": [members["ada"]], "revoke": [members["ada"]]},
        )

        assert response.status_code == 400
        assert response.json()["code"] == "workspace.grant_conflict"
        assert response.json()["detail"]["user_ids"] == [members["ada"]]
        assert self._held() == {members["frank"]}

    def test_an_unknown_account_is_refused_and_nothing_is_written(self, admin, members):
        response = admin.put(
            f"/api/workspaces/{self.WS}/grants",
            json={"grant": [members["ada"], "ghost-user"]},
        )

        assert response.status_code == 400
        assert response.json()["code"] == "workspace.invalid_grant_targets"
        assert response.json()["detail"]["user_ids"] == ["ghost-user"]
        # the valid half of the batch is not applied either
        assert self._held() == {members["frank"]}

    def test_a_blank_id_is_refused_like_any_unknown_target(self, admin, members):
        """Live-found: blank ids were silently filtered before validation, so the
        same garbage input succeeded or 400ed depending on whitespace alone."""
        response = admin.put(
            f"/api/workspaces/{self.WS}/grants", json={"grant": ["  ", members["ada"]]}
        )

        assert response.status_code == 400
        assert response.json()["code"] == "workspace.invalid_grant_targets"
        assert response.json()["detail"]["user_ids"] == [""]
        assert self._held() == {members["frank"]}

    def test_an_administrator_cannot_be_granted(self, admin, members):
        """An admin reaches every workspace by role, so a grant would be a lie the
        console then offers to revoke."""
        response = admin.put(
            f"/api/workspaces/{self.WS}/grants", json={"grant": [members["frank"]]}
        )

        assert response.status_code == 400
        assert response.json()["code"] == "workspace.invalid_grant_targets"
        assert response.json()["detail"]["user_ids"] == [members["frank"]]

    def test_a_disabled_member_can_still_be_granted(self, admin, members):
        """Access is granted to an account, not to a session: re-enabling it must
        not need the grants re-done."""
        response = admin.put(
            f"/api/workspaces/{self.WS}/grants", json={"grant": [members["eve"]]}
        )
        assert response.status_code == 200
        assert members["eve"] in self._held()

    def test_a_workspace_awaiting_bootstrap_can_be_granted(self, admin, members):
        """Handing an environment over before it is READY is legitimate; the
        readiness gate lives on mutating traffic, not on grants."""
        assert admin.post("/api/workspaces", json=NEW).status_code == 201

        response = admin.put(
            f"/api/workspaces/{NEW['id']}/grants", json={"grant": [members["ada"]]}
        )

        assert response.status_code == 200, response.text
        assert response.json()["granted_total"] == 1

    def test_an_unknown_workspace_is_404(self, admin, members):
        response = admin.put("/api/workspaces/ghost/grants", json={"grant": [members["ada"]]})
        assert response.status_code == 404

    def test_only_admins_write(self, gated_app, admin, members):
        with TestClient(gated_app, client=("127.0.0.1", 4321)) as member:
            assert member.post(
                "/api/auth/login", json={"username": "ada", "password": "sufficient-pass"}
            ).status_code == 200

            response = member.put(
                f"/api/workspaces/{self.WS}/grants", json={"grant": [members["bruno"]]}
            )

        assert response.status_code == 403
        assert self._held() == {members["frank"]}

    def test_the_user_patch_path_still_works_alongside_it(self, admin, members):
        """Both write only `user_workspaces`, so the per-user replacement must see
        what the bulk endpoint did and vice versa."""
        admin.put(f"/api/workspaces/{self.WS}/grants", json={"grant": [members["ada"]]})

        listed = admin.get("/api/users", params={"q": "ada"}).json()["items"]
        assert listed[0]["workspaces"] == [self.WS]

        admin.patch(f"/api/users/{members['ada']}", json={"workspaces": []})
        assert members["ada"] not in self._held()


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
