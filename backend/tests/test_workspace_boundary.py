"""The request boundary: which workspace a call lands in, and who may say so.

Everything here is about the *refusals*, because they are the security
properties: a member reaches only granted workspaces, another workspace's
resource ids read as missing, and a workspace whose bootstrap has not finished
serves reads only.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app.routers.agents as agents_router
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import Agent, ApiKey, ChatMessage, ChatSession, Job, Workspace
from app.routers.workspaces import WORKSPACE_HEADER
from app.services import users as users_service

ADMIN_CREDS = {"username": "operator", "password": "s3cret-pass"}
MEMBER_CREDS = {
    "username": "ws-member",
    "email": "ws-member@acme-corp.com",
    "password": "sufficient-pass",
}
OTHER = "acct-usw1"


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


def _member_client(app, workspaces: list[str]) -> TestClient:
    """A live member session granted exactly `workspaces` (what an approval does)."""
    client = TestClient(app, client=("127.0.0.1", 4321))
    assert client.post("/api/auth/register", json=MEMBER_CREDS).status_code == 201
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, MEMBER_CREDS["username"])
        assert user is not None
        user.status = users_service.STATUS_ACTIVE
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        users_service.set_workspace_grants(db, user, workspaces)
        db.commit()
    finally:
        db.close()
    login = client.post(
        "/api/auth/login",
        json={
            "username": MEMBER_CREDS["username"],
            "password": MEMBER_CREDS["password"],
        },
    )
    assert login.status_code == 200, login.text
    return client


def _workspace(workspace_id: str = OTHER, region: str = "us-west-1", **columns) -> str:
    db = SessionLocal()
    try:
        db.add(
            Workspace(
                id=workspace_id,
                name=workspace_id,
                account_id="444455556666",
                region=region,
                bootstrap_status=columns.pop("bootstrap_status", "ready"),
                resources=columns.pop("resources", {"gateway_id": "gw-2"}),
                **columns,
            )
        )
        db.commit()
    finally:
        db.close()
    return workspace_id


def _agent(workspace_id: str, name: str = "boundary-agent") -> str:
    db = SessionLocal()
    try:
        agent = Agent(
            workspace_id=workspace_id,
            name=name,
            method="zip_runtime",
            status="active",
            arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/x",
            spec={"name": name},
        )
        db.add(agent)
        db.commit()
        return agent.id
    finally:
        db.close()


class TestHeaderResolution:
    def test_an_admin_without_a_header_lands_on_default(self, admin):
        agent_id = _agent(DEFAULT_WORKSPACE_ID)
        listed = admin.get("/api/agents").json()["agents"]
        assert [row["id"] for row in listed] == [agent_id]

    def test_an_admin_reaches_any_workspace_without_a_grant(self, admin):
        _workspace()
        foreign = _agent(OTHER, name="foreign-agent")
        listed = admin.get("/api/agents", headers={WORKSPACE_HEADER: OTHER}).json()
        assert [row["id"] for row in listed["agents"]] == [foreign]

    def test_an_unknown_workspace_is_404(self, admin):
        response = admin.get("/api/agents", headers={WORKSPACE_HEADER: "nope"})
        assert response.status_code == 404
        assert response.json()["code"] == "workspace.not_found"

    def test_a_member_with_one_grant_needs_no_header(self, gated_app):
        _workspace()
        foreign = _agent(OTHER, name="member-agent")
        _agent(DEFAULT_WORKSPACE_ID, name="hub-agent")
        member = _member_client(gated_app, [OTHER])

        listed = member.get("/api/agents").json()["agents"]

        assert [row["id"] for row in listed] == [foreign]

    def test_a_member_with_several_grants_must_name_one(self, gated_app):
        _workspace()
        member = _member_client(gated_app, [DEFAULT_WORKSPACE_ID, OTHER])

        response = member.get("/api/agents")

        assert response.status_code == 400
        assert response.json()["code"] == "workspace.header_required"
        assert response.json()["detail"]["available"] == [OTHER, DEFAULT_WORKSPACE_ID]
        assert member.get("/api/agents", headers={WORKSPACE_HEADER: OTHER}).status_code == 200

    def test_a_member_without_a_grant_reaches_nothing(self, gated_app):
        member = _member_client(gated_app, [])

        blind = member.get("/api/agents")
        named = member.get("/api/agents", headers={WORKSPACE_HEADER: DEFAULT_WORKSPACE_ID})

        assert blind.status_code == 403 and blind.json()["code"] == "workspace.forbidden"
        assert named.status_code == 403 and named.json()["code"] == "workspace.forbidden"
        # ...and the switcher has nothing to offer
        assert member.get("/api/workspaces").json() == {
            "workspaces": [],
            "all_workspaces": False,
        }

    def test_a_member_is_refused_a_workspace_it_was_not_granted(self, gated_app):
        _workspace()
        member = _member_client(gated_app, [DEFAULT_WORKSPACE_ID])

        response = member.get("/api/agents", headers={WORKSPACE_HEADER: OTHER})

        assert response.status_code == 403
        assert response.json()["code"] == "workspace.forbidden"
        assert response.json()["detail"]["workspace_id"] == OTHER

    def test_the_open_console_resolves_the_default_workspace(self, client):
        """No password configured: the local operator is the built-in admin, so a
        console with no login keeps working without ever sending the header."""
        assert client.get("/api/agents").status_code == 200
        assert client.get("/api/apikeys").status_code == 200


class TestForeignIdsAreInvisible:
    """A resource id from another workspace must read exactly like a missing one."""

    def test_agents_jobs_chats_and_keys_are_404(self, admin):
        _workspace()
        foreign_agent = _agent(OTHER, name="hidden")
        db = SessionLocal()
        try:
            job = Job(workspace_id=OTHER, type="deploy_agent", payload={})
            key = ApiKey(
                workspace_id=OTHER, name="foreign", prefix="lp_live_x…", key_hash="deadbeef"
            )
            db.add_all(
                [
                    job,
                    key,
                    ChatSession(
                        workspace_id=OTHER,
                        agent_id=foreign_agent,
                        session_id="f" * 40,
                        actor_id="river",
                    ),
                    ChatMessage(
                        workspace_id=OTHER,
                        agent_id=foreign_agent,
                        session_id="f" * 40,
                        role="user",
                        text="secret",
                    ),
                ]
            )
            db.commit()
            job_id, key_id = job.id, key.id
        finally:
            db.close()

        # every probe runs as an admin on `default`: authorization passes, the
        # workspace predicate is what refuses
        assert admin.get(f"/api/agents/{foreign_agent}").status_code == 404
        assert admin.get(f"/api/jobs/{job_id}").status_code == 404
        assert admin.post(f"/api/apikeys/{key_id}/disable").status_code == 404
        assert admin.delete(f"/api/agents/{foreign_agent}").status_code == 404
        assert (
            admin.get(f"/api/chat/{foreign_agent}/history?session_id={'f' * 40}").status_code
            == 404
        )
        assert admin.get(f"/api/chat/{foreign_agent}/sessions").status_code == 404
        assert admin.get("/api/apikeys").json()["keys"] == []

    def test_a_name_is_free_again_in_another_workspace(self, admin, monkeypatch):
        """Agent names are unique per workspace: each environment owns its own
        AgentCore resource namespace."""
        monkeypatch.setattr(agents_router, "start_deploy_async", lambda job_id: None)
        _workspace()
        _agent(DEFAULT_WORKSPACE_ID, name="shared-name")
        spec = {
            "name": "shared-name",
            "method": "harness",
            "system_prompt": "Answer concisely.",
        }

        taken = admin.post("/api/agents", json=spec)
        free = admin.post("/api/agents", headers={WORKSPACE_HEADER: OTHER}, json=spec)

        assert taken.status_code == 409 and taken.json()["code"] == "agent.name_exists"
        assert free.status_code == 202, free.text
        assert free.json()["agent"]["name"] == "shared-name"


class TestReadinessGate:
    def test_a_registered_workspace_serves_reads_only(self, admin):
        _workspace(bootstrap_status="registered", resources={})
        headers = {WORKSPACE_HEADER: OTHER}

        assert admin.get("/api/agents", headers=headers).status_code == 200
        blocked = admin.post("/api/apikeys", headers=headers, json={"name": "early"})
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "workspace.not_ready"
        assert blocked.json()["detail"]["bootstrap_status"] == "registered"

    def test_a_bootstrapping_workspace_is_also_read_only(self, admin):
        _workspace(bootstrap_status="bootstrapping")
        response = admin.post(
            "/api/apikeys", headers={WORKSPACE_HEADER: OTHER}, json={"name": "early"}
        )
        assert response.status_code == 409

    def test_the_default_workspace_is_never_gated(self, admin):
        """It mirrors settings, so it reads "registered" wherever `make bootstrap`
        has not run — and the AWS-free local flows must keep working there."""
        db = SessionLocal()
        try:
            db.get(Workspace, DEFAULT_WORKSPACE_ID).bootstrap_status = "registered"
            db.commit()
        finally:
            db.close()

        assert admin.post("/api/apikeys", json={"name": "local"}).status_code == 201


class TestStamping:
    def test_new_rows_carry_the_calling_workspace(self, admin):
        _workspace()
        created = admin.post(
            "/api/apikeys", headers={WORKSPACE_HEADER: OTHER}, json={"name": "scoped"}
        )
        assert created.status_code == 201
        db = SessionLocal()
        try:
            assert db.get(ApiKey, created.json()["id"]).workspace_id == OTHER
        finally:
            db.close()
        # ...and it is invisible from the other workspace
        assert admin.get("/api/apikeys").json()["keys"] == []
        assert [k["name"] for k in
                admin.get("/api/apikeys", headers={WORKSPACE_HEADER: OTHER}).json()["keys"]
                ] == ["scoped"]

    def test_a_deploy_job_inherits_the_agents_workspace(self):
        """The pipeline row must name the environment of the agent it deploys, not
        whatever a later request happens to target."""
        from app.deployer.pipeline import create_deployment

        _workspace()
        db = SessionLocal()
        try:
            agent = db.get(Agent, _agent(OTHER, name="job-owner"))
            deployment, job = create_deployment(db, agent)
            assert (deployment.workspace_id, job.workspace_id) == (OTHER, OTHER)
        finally:
            db.close()
