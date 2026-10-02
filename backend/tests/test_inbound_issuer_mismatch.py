"""A JWT agent whose authorizer trusts an IdP other than the workspace Cognito
pool: every platform invoke path (console Chat both ways, /v1, direct invoke,
evaluation) is refused by name — `agent.inbound_issuer_mismatch` — before a
token is minted, instead of surfacing the authorizer's bare 403."""

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import service as eval_service
from app.models.ledger import Agent
from app.schemas.inbound_auth import InboundAuth, JwtInboundConfig
from app.services import invoke as invoke_service
from app.services.chat import chat_stream
from tests.conftest import ws_ctx

POOL = "us-west-2_ABC123"
POOL_ISSUER = f"https://cognito-idp.us-west-2.amazonaws.com/{POOL}"
OKTA = "https://acme.okta.com/oauth2/default/.well-known/openid-configuration"
WS = ws_ctx({"user_pool_id": POOL, "m2m_client_id": "m2m"})


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def _agent(db, discovery_url: str) -> Agent:
    auth = InboundAuth(mode="jwt", jwt=JwtInboundConfig(
        discovery_url=discovery_url, allowed_audience=["api://agent"]))
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name="okta-agent", method="zip_runtime",
        status="active", resource_id="rt-1",
        arn="arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/okta-1",
        spec={"name": "okta-agent", "protocol": "http"},
        inbound_auth_mode="jwt", inbound_auth_config=auth.model_dump(),
    )
    db.add(agent)
    db.commit()
    return agent


@pytest.fixture
def no_transport(monkeypatch):
    """Minting or sending anything is a failure: the refusal must come first."""
    def forbidden(*_a, **_k):
        raise AssertionError("no token may be minted or sent for an unreachable agent")

    monkeypatch.setattr(invoke_service.inbound_auth_service, "m2m_bearer_token", forbidden)
    monkeypatch.setattr(invoke_service.rt, "invoke_runtime_text_bearer", forbidden)
    monkeypatch.setattr(invoke_service.rt, "stream_runtime_events_bearer", forbidden)


@pytest.mark.parametrize(("bearer", "caller"), [(None, "m2m"), ("user-jwt", "user_jwt")])
def test_text_invoke_is_refused_by_name(db, no_transport, bearer, caller):
    agent = _agent(db, OKTA)
    with pytest.raises(AppError) as excinfo:
        invoke_service.invoke_agent_text(agent, "hi", workspace=WS, bearer_token=bearer)
    err = excinfo.value
    assert err.code == "agent.inbound_issuer_mismatch" and err.status_code == 409
    assert err.detail == {
        "agent_issuer": "https://acme.okta.com/oauth2/default",
        "workspace_issuer": POOL_ISSUER,
        "caller": caller,
    }


def test_stream_invoke_is_refused_by_name(db, no_transport):
    agent = _agent(db, OKTA)
    with pytest.raises(AppError) as excinfo:
        list(invoke_service.invoke_agent_events(agent, "hi", workspace=WS))
    assert excinfo.value.code == "agent.inbound_issuer_mismatch"


def test_console_chat_emits_the_named_error_event(db, no_transport):
    agent = _agent(db, OKTA)
    events = list(chat_stream(agent, "hi", workspace=WS))
    errors = [e["data"] for e in events if e["event"] == "error"]
    assert errors and errors[0]["code"] == "agent.inbound_issuer_mismatch", events


def test_same_issuer_agent_still_invokes(db, monkeypatch):
    agent = _agent(db, f"{POOL_ISSUER}/.well-known/openid-configuration")
    monkeypatch.setattr(
        invoke_service.inbound_auth_service, "m2m_bearer_token", lambda _ws: "m2m-token")
    sent: dict = {}
    monkeypatch.setattr(
        invoke_service.rt, "invoke_runtime_text_bearer",
        lambda region, arn, token, prompt, **kw: sent.update(token=token) or {"text": "ok"})
    assert invoke_service.invoke_agent_text(agent, "hi", workspace=WS)["text"] == "ok"
    assert sent["token"] == "m2m-token"


def test_no_pool_keeps_the_m2m_unavailable_error(db):
    # without a workspace pool there is nothing to compare; the M2M path
    # names its own error, as before
    agent = _agent(db, OKTA)
    with pytest.raises(AppError) as excinfo:
        invoke_service.invoke_agent_text(agent, "hi", workspace=ws_ctx())
    assert excinfo.value.code == "identity.m2m_unavailable"


def test_evaluation_submit_is_refused_before_a_run_row(db, monkeypatch):
    agent = _agent(db, OKTA)
    monkeypatch.setattr(eval_service, "resolve_telemetry", lambda *_a: ("svc", "lg"))
    with pytest.raises(AppError) as excinfo:
        eval_service.submit_run(
            agent=agent, workspace=WS, dataset_items=[{"prompt": "hi"}],
            dataset_id=None, dataset_name="d", evaluators=["Builtin.Helpfulness"],
        )
    assert excinfo.value.code == "agent.inbound_issuer_mismatch"
    assert excinfo.value.detail["caller"] == "m2m"
    assert db.query(eval_service.EvalRun).count() == 0
