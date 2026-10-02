"""The agent page's editable switch to JWT: an operator-edited config (their
own IdP, optionally picked from a Connection) is probed like every other save,
and the display-only `source_connection` survives into the identity view."""

from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

import app.routers.agents as agents_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec
from app.services import agent_identity as identity_view
from app.services import inbound_auth as svc

OKTA = "https://acme.okta.com/oauth2/default/.well-known/openid-configuration"
EDITED = {"mode": "jwt", "jwt": {
    "discovery_url": OKTA, "allowed_audience": ["api://agent"],
    "source_connection": "corp-okta",
}}


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def _agent(db) -> str:
    spec = AgentSpec(name="sw-edit", method="zip_runtime", system_prompt="p").model_dump()
    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="sw-edit", method="zip_runtime",
                  status="active", arn="arn:rt-sw", resource_id="rt-sw", spec=spec)
    db.add(agent)
    db.commit()
    return agent.id


def test_an_edited_jwt_is_probed_and_pinned_with_its_source(monkeypatch, db):
    started: list[str] = []
    probed: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", started.append)
    monkeypatch.setattr(svc, "_default_probe", lambda url: probed.append(url) or {
        "issuer": "https://acme.okta.com/oauth2/default", "jwks_uri": "https://acme/keys"})
    agent_id = _agent(db)
    with TestClient(create_app()) as client:
        res = client.post(f"/api/agents/{agent_id}/inbound-auth", json={"inbound_auth": EDITED})
    assert res.status_code == 202, res.text
    assert probed == [OKTA] and len(started) == 1
    db.expire_all()
    pinned = db.get(Agent, agent_id).spec["inbound_auth"]["jwt"]
    assert pinned["discovery_url"] == OKTA
    assert pinned["source_connection"] == "corp-okta"


def test_an_unreachable_discovery_url_fails_the_switch_by_name(monkeypatch, db):
    started: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", started.append)

    def unreachable(url):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(svc, "_default_probe", unreachable)
    agent_id = _agent(db)
    with TestClient(create_app()) as client:
        res = client.post(f"/api/agents/{agent_id}/inbound-auth", json={"inbound_auth": EDITED})
    assert res.status_code == 422
    assert res.json()["code"] == "identity.discovery_unreachable"
    assert started == []
    db.expire_all()
    assert db.get(Agent, agent_id).status == "active"


@pytest.mark.parametrize(("ledger_url", "shown"), [(OKTA, "corp-okta"), ("https://other/x", None)])
def test_identity_view_names_the_source_connection_only_while_it_matches(ledger_url, shown):
    control = MagicMock()
    control.get_agent_runtime.return_value = {"authorizerConfiguration": {"customJWTAuthorizer": {
        "discoveryUrl": OKTA, "allowedAudience": ["api://agent"]}}}
    control.list_oauth2_credential_providers.return_value = {"credentialProviders": []}
    control.list_api_key_credential_providers.return_value = {"credentialProviders": []}
    agent = Agent(id="a1", name="x", method="zip_runtime", status="active", resource_id="rt",
                  arn="arn:aws:bedrock-agentcore:us-west-2:111:runtime/x-abc",
                  spec={"name": "x"}, inbound_auth_mode="jwt",
                  inbound_auth_config={"mode": "jwt", "jwt": {
                      **EDITED["jwt"], "discovery_url": ledger_url}})
    jwt = identity_view.agent_identity(control, agent, {})["inbound"]["jwt"]
    assert jwt["discovery_url"] == OKTA
    assert jwt.get("source_connection") == shown


@pytest.mark.parametrize("body", [
    {},
    {"mode": "jwt", "jwt": EDITED["jwt"]},
    {"inbound_auth": EDITED, "extra": 1},
])
def test_a_mistyped_body_is_refused_without_a_redeploy(monkeypatch, db, body):
    started: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", started.append)
    agent_id = _agent(db)
    with TestClient(create_app()) as client:
        res = client.post(f"/api/agents/{agent_id}/inbound-auth", json=body)
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "validation.invalid_request"
    assert started == []
    db.expire_all()
    assert db.get(Agent, agent_id).status == "active"
