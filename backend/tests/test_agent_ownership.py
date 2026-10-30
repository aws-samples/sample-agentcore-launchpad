"""Agent ownership boundary (N10v2-bug4 / obs9; Family B step 1 re-pointed it).

``Agent.created_by`` is the ``users.id`` of the LOGIN identity that created the row
and ``created_by_name`` its username — never a configured operator name (the pre-#261
``owner`` username column was retired). Within a workspace reads, invoke and chat stay
shared (route_policy: Chat and invoke are deliberately MEMBER), but the mutations that
destroy or re-publish an agent are creator-or-admin: a member acting on another member's
(or the admin's) agent answers 403 ``agent.forbidden`` before any AWS call.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app.routers.agents as agents_router
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import Agent
from app.services import users as users_service
from tests.conftest import settle_deploy_jobs

ADMIN_CREDS = {"username": "operator", "password": "s3cret-pass"}
MEMBER_CREDS = {
    "username": "owner-user",
    "email": "owner-user@acme-corp.com",
    "password": "sufficient-pass",
}
OTHER_CREDS = {
    "username": "other-user",
    "email": "other-user@acme-corp.com",
    "password": "sufficient-pass",
}
SPEC = {"name": "owned-agent", "method": "zip_runtime", "system_prompt": "Be brief."}


@pytest.fixture(autouse=True)
def no_real_aws(monkeypatch):
    launched: list[str] = []
    torn_down: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    monkeypatch.setattr(agents_router.teardown, "cleanup_residue", lambda *a, **k: [])
    monkeypatch.setattr(
        agents_router.zip_method, "delete_agent_resources",
        lambda agent, _ws: torn_down.append(agent.name),
    )
    yield {"launched": launched, "torn_down": torn_down}


@pytest.fixture
def gated_app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN_CREDS["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN_CREDS["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def _activate_member(username: str) -> None:
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, username)
        assert user is not None
        user.status = users_service.STATUS_ACTIVE
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
        db.commit()
    finally:
        db.close()


def _member(app, creds) -> TestClient:
    client = TestClient(app, client=("127.0.0.1", 4321))
    assert client.post("/api/auth/register", json=creds).status_code == 201
    _activate_member(creds["username"])
    login = client.post(
        "/api/auth/login", json={"username": creds["username"], "password": creds["password"]}
    )
    assert login.status_code == 200, login.text
    return client


@pytest.fixture
def sessions(gated_app):
    """(admin, member, other member) — three live sessions on the default workspace."""
    with TestClient(gated_app, client=("127.0.0.1", 4321)) as admin:
        assert admin.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        member = _member(gated_app, MEMBER_CREDS)
        other = _member(gated_app, OTHER_CREDS)
        yield admin, member, other


def _activate_agent(agent_id: str) -> None:
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    agent.status = "active"
    agent.resource_id = "rt-xyz"
    agent.arn = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:runtime/xyz"
    db.commit()
    db.close()


def _create(client, name: str) -> str:
    res = client.post("/api/agents", json={**SPEC, "name": name})
    assert res.status_code == 202, res.text
    return res.json()["agent"]["id"]


# ---- the owner is the signed-in identity ----------------------------------------


def test_owner_is_the_login_identity_not_a_configured_name(sessions):
    admin, member, _ = sessions
    admin_agent = admin.post("/api/agents", json=SPEC).json()["agent"]
    # obs9: was "river"; the config admin has no `users` row → name only
    assert admin_agent["created_by_name"] == ADMIN_CREDS["username"]
    assert admin_agent["created_by"] is None
    member_agent = member.post("/api/agents", json={**SPEC, "name": "member-agent"}).json()
    assert member_agent["agent"]["created_by_name"] == MEMBER_CREDS["username"]
    assert member_agent["agent"]["created_by"] == _user_id(MEMBER_CREDS["username"])
    listed = {a["name"]: a["created_by_name"] for a in admin.get("/api/agents").json()["agents"]}
    assert listed == {"owned-agent": "operator", "member-agent": "owner-user"}


def test_open_console_owner_is_the_built_in_admin(client):
    """Gate disabled: the implicit local operator is the configured admin username."""
    agent = client.post("/api/agents", json=SPEC).json()["agent"]
    assert agent["created_by_name"] == get_settings().auth_username
    assert agent["created_by_name"] != "river" and agent["created_by"] is None


def test_the_creator_is_never_client_settable(client):
    """A payload naming the creator is refused (422) — unlike `system_key`, which is
    silently ignored — so nobody can file an agent under another account."""
    res = client.post("/api/agents", json={**SPEC, "created_by": "u-someone"})
    assert res.status_code == 422, res.text
    res = client.post("/api/agents", json={**SPEC, "created_by_name": "someone"})
    assert res.status_code == 422, res.text
    assert client.get("/api/agents").json()["agents"] == []


def _user_id(username: str) -> str:
    db = SessionLocal()
    try:
        return users_service.find_by_username(db, username).id
    finally:
        db.close()


# ---- mutations are owner-or-admin ----------------------------------------------------


def test_member_cannot_delete_or_redeploy_another_members_agent(sessions, no_real_aws):
    admin, member, other = sessions
    agent_id = _create(member, "members-agent")
    _activate_agent(agent_id)

    denied = other.delete(f"/api/agents/{agent_id}")
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "agent.forbidden"
    assert denied.json()["detail"] == {
        "agent_id": agent_id,
        "created_by": _user_id(MEMBER_CREDS["username"]),
        "created_by_name": MEMBER_CREDS["username"],
    }
    assert no_real_aws["torn_down"] == []  # refused before any AWS call

    denied = other.post(f"/api/agents/{agent_id}/redeploy",
                        json={**SPEC, "name": "members-agent", "system_prompt": "changed"})
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "agent.forbidden"

    db = SessionLocal()
    row = db.get(Agent, agent_id)
    assert row.status == "active" and row.spec["system_prompt"] == "Be brief."
    db.close()


def test_member_cannot_delete_the_admins_agent(sessions, no_real_aws):
    """The N10 v2 case 12 repro: a freshly approved member destroyed the admin's agent."""
    admin, member, _ = sessions
    agent_id = _create(admin, "admins-agent")
    denied = member.delete(f"/api/agents/{agent_id}")
    assert denied.status_code == 403 and denied.json()["code"] == "agent.forbidden"
    assert no_real_aws["torn_down"] == []
    assert [a["id"] for a in admin.get("/api/agents").json()["agents"]] == [agent_id]


def test_owner_can_redeploy_and_delete_their_own_agent(sessions, no_real_aws):
    _, member, _ = sessions
    agent_id = _create(member, "mine")
    _activate_agent(agent_id)
    res = member.post(f"/api/agents/{agent_id}/redeploy",
                      json={**SPEC, "name": "mine", "system_prompt": "v2"})
    assert res.status_code == 202, res.text
    _activate_agent(agent_id)
    settle_deploy_jobs(agent_id)  # N16-obs83: a delete mid-deploy is 409
    res = member.delete(f"/api/agents/{agent_id}")
    assert res.status_code == 200 and res.json()["deleted"] is True
    assert no_real_aws["torn_down"] == ["mine"]


def test_admin_can_mutate_any_members_agent(sessions, no_real_aws):
    admin, member, _ = sessions
    agent_id = _create(member, "members-agent")
    _activate_agent(agent_id)
    res = admin.post(f"/api/agents/{agent_id}/redeploy",
                     json={**SPEC, "name": "members-agent", "system_prompt": "admin edit"})
    assert res.status_code == 202, res.text
    _activate_agent(agent_id)
    settle_deploy_jobs(agent_id)  # N16-obs83: a delete mid-deploy is 409
    assert admin.delete(f"/api/agents/{agent_id}").status_code == 200
    assert no_real_aws["torn_down"] == ["members-agent"]


# ---- reads stay shared inside the workspace -----------------------------------------


def test_members_still_read_each_others_agents(sessions):
    admin, member, other = sessions
    admin_id = _create(admin, "admins-agent")
    member_id = _create(member, "members-agent")
    seen = {a["id"] for a in other.get("/api/agents").json()["agents"]}
    assert seen == {admin_id, member_id}
    for agent_id in (admin_id, member_id):
        res = other.get(f"/api/agents/{agent_id}")
        assert res.status_code == 200, res.text
        assert res.json()["id"] == agent_id


def test_permission_denial_still_wins_over_ownership(sessions):
    """A member whose agents.delete was revoked is refused by the route policy
    (403 auth.permission_required) even on their own agent."""
    admin, member, _ = sessions
    agent_id = _create(member, "mine")
    db = SessionLocal()
    user_id = users_service.find_by_username(db, MEMBER_CREDS["username"]).id
    db.close()
    assert admin.patch(f"/api/users/{user_id}",
                       json={"permissions": {"agents.delete": False}}).status_code == 200
    denied = member.delete(f"/api/agents/{agent_id}")
    assert denied.status_code == 403
    assert denied.json()["code"] == "auth.permission_required"


# ---- every spec-update entry point carries the gate -----------------------------------


def test_republish_agent_itself_enforces_the_owner_gate():
    """The shared re-publish function refuses a non-owner identity even when called
    directly (an AI-recommendation accept, a future PUT/PATCH): the gate cannot be
    skipped by a new caller forgetting the route-level check."""
    from app.core.errors import AppError
    from app.routers.agents import republish_agent
    from app.routers.auth import Identity
    from app.schemas.agent import AgentSpec

    agent = Agent(id="a1", workspace_id=DEFAULT_WORKSPACE_ID, name="owned-agent",
                  method="zip_runtime", status="active", created_by="u-alice",
                  created_by_name="alice", spec=dict(SPEC))
    other = Identity(username="bob", role="member", user_id="u-bob")
    with pytest.raises(AppError) as exc:
        republish_agent(None, agent, AgentSpec(**SPEC), None, identity=other)
    assert exc.value.code == "agent.forbidden" and exc.value.status_code == 403


def test_every_agent_write_route_is_a_known_gated_entry_point():
    """Drift guard: a new non-GET /api/agents/{agent_id}* route (PUT/PATCH spec update,
    convert, …) must be added here only after it calls require_agent_owner — the
    route table cannot grow a write path without the creator gate being considered."""
    from app.core.route_policy import ROUTE_POLICY

    gated = {
        ("DELETE", "/api/agents/{agent_id}"),           # delete_agent → require_agent_creator
        ("POST", "/api/agents/{agent_id}/redeploy"),    # republish_agent → require_agent_creator
    }
    shared = {("POST", "/api/agents/{agent_id}/invoke")}  # member interaction, like Chat
    writes = {
        key for key in ROUTE_POLICY
        if key[0] != "GET" and key[1].startswith("/api/agents/{agent_id}")
    }
    assert writes == gated | shared, sorted(writes - gated - shared)
