"""Inbound JWT (identity P3) on top of the ported model/transport tests in
test_inbound_auth.py: redeploys keep the authorizer, the bearer transport
carries the full payload and the 3LO asks, as_user consents asked over a
JWT authorizer complete with ``userToken``, the Chat "invoke as me" toggle,
the service-model claim patterns and the caller-kind migration."""

import json
from unittest.mock import MagicMock

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

import app.routers.chat as chat_router
from app.core import db as db_module
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.main import create_app
from app.models.ledger import Agent, OauthPendingSession, Workspace
from app.schemas.inbound_auth import CustomClaim, InboundAuth, JwtInboundConfig
from app.services import inbound_auth as svc
from app.services import invoke as invoke_service
from app.services import oauth_sessions
from app.services.agentcore import runtime as rt
from tests.test_inbound_auth import (
    DISCOVERY,
    FakeBearerResponse,
    StubRuntimeControl,
    _opener,
    jwt_auth,
)
from tests.test_inbound_auth import TestDeployerAuthorizer as _Deployer

WS = DEFAULT_WORKSPACE_ID
SESSION = "urn:ietf:params:oauth:request_uri:p3"
AS_USER_SPEC = {
    "name": "jwt-fed-agent",
    "method": "zip_runtime",
    "system_prompt": "p",
    "tools": [{
        "type": "rest", "name": "userinfo",
        "config": {"url": "https://idp.example/oauth2/userInfo"},
        "auth": {"connection": "team-idp", "kind": "oauth2", "mode": "as_user",
                 "scopes": ["openid"]},
    }],
}


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


# ── acceptance ②: a redeploy never resets the authorizer ─────────────────────


class TestRedeployKeepsAuthorizer:
    """UpdateAgentRuntime resets an omitted authorizerConfiguration to IAM, so
    every JWT redeploy must echo the resolved authorizer verbatim."""

    JWT_ROW = {"resource_id": "rt-1", "arn": "arn:rt-1", "version": "3",
               "inbound_auth_mode": "jwt"}

    def test_pinned_jwt_redeploy_echoes_the_same_authorizer(self, monkeypatch, db):
        auth = jwt_auth(allowed_scopes=["launchpad/invoke"])
        stub, row = _Deployer()._deploy(
            monkeypatch, db, spec_auth=auth, mode="update",
            existing={**self.JWT_ROW, "inbound_auth_config": auth.model_dump()},
        )
        assert stub.updated_with["authorizerConfiguration"] == (
            svc.authorizer_configuration(auth)
        )
        assert row.inbound_auth_mode == "jwt"
        assert row.inbound_auth_config == auth.model_dump()

    def test_inherited_jwt_redeploy_echoes_the_workspace_default(self, monkeypatch, db):
        auth = jwt_auth()
        stub, row = _Deployer()._deploy(
            monkeypatch, db, workspace_auth=auth, mode="update",
            existing={**self.JWT_ROW, "inbound_auth_config": auth.model_dump()},
        )
        sent = stub.updated_with["authorizerConfiguration"]["customJWTAuthorizer"]
        assert sent["discoveryUrl"] == DISCOVERY
        assert sent["allowedClients"] == ["client-a"]
        assert row.inbound_auth_mode == "jwt"


@pytest.mark.parametrize("wrapper", ["update_code_runtime", "update_container_runtime"])
def test_update_wrappers_forward_the_authorizer(wrapper):
    stub = StubRuntimeControl()
    authorizer = svc.authorizer_configuration(jwt_auth())
    kwargs = {"runtime_id": "rt-1", "role_arn": "arn:role",
              "authorizer_configuration": authorizer}
    if wrapper == "update_code_runtime":
        kwargs.update(s3_bucket="b", s3_key="k")
    else:
        kwargs.update(container_uri="1.dkr.ecr.us-west-2.amazonaws.com/r:t")
    getattr(rt, wrapper)(stub, **kwargs)
    assert stub.updated_with["authorizerConfiguration"] == authorizer


# ── bearer transport: same payload, same parser as SigV4 ────────────────────


def test_bearer_body_carries_every_payload_field_and_no_user_id_header():
    captured: dict = {}
    response = FakeBearerResponse(
        200, json.dumps({"event": "attachments", "contract": "v1"}).encode(),
    )
    rt.invoke_runtime_text_bearer(
        "us-west-2", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/x", "tok", "hi",
        session_id="s" * 40, actor_id="a__alice",
        gateway_access_token="gw", attachments=[{"name": "f"}],
        force_reauth_providers=["team-idp"],
        http_response=_opener(response, captured),
    )
    body = json.loads(captured["body"])
    assert body == {
        "prompt": "hi", "actor_id": "a__alice", "attachments": [{"name": "f"}],
        "force_reauth_providers": ["team-idp"], "gateway_access_token": "gw",
    }
    assert not any("user-id" in h.lower() for h in captured["headers"])


def test_bearer_refuses_unacknowledged_attachments_like_sigv4():
    response = FakeBearerResponse(200, json.dumps({"result": "ok"}).encode())
    with pytest.raises(AppError) as excinfo:
        rt.invoke_runtime_text_bearer(
            "us-west-2", "arn:rt", "tok", "hi", attachments=[{"name": "f"}],
            http_response=_opener(response, {}),
        )
    assert excinfo.value.code == "chat.attachment_new_session_required"


def test_bearer_stream_relays_auth_required():
    captured: dict = {}
    response = FakeBearerResponse(content_type="text/event-stream", sse_lines=[
        'data: {"event": "auth_required", "provider": "team-idp", "tool": "u", '
        '"url": "https://x", "session_uri": "urn:s"}',
        "",
        'data: {"event": "delta", "text": "hi"}',
        "",
    ])
    result = rt.invoke_runtime_text_bearer(
        "us-west-2", "arn:rt", "tok", "q", http_response=_opener(response, captured),
    )
    assert result["text"] == "hi"
    assert result["auth_required"][0]["provider"] == "team-idp"


# ── 3LO over a JWT authorizer: caller kind recorded, userToken completion ──


def _jwt_fed_agent(db) -> Agent:
    agent = Agent(
        workspace_id=WS, name="jwt-fed-agent", method="zip_runtime", status="active",
        arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/jf", spec=AS_USER_SPEC,
        inbound_auth_mode="jwt", inbound_auth_config=jwt_auth().model_dump(),
    )
    db.add(agent)
    db.commit()
    return agent


def _ask_stream(captured):
    def fake(region, arn, token, prompt, **kwargs):
        captured.update(kwargs, token=token)
        yield {"event": "auth_required", "data": {
            "provider": "team-idp", "tool": "userinfo", "url": "https://x",
            "scopes": ["openid"], "session_uri": SESSION,
        }}
    return fake


def test_user_jwt_ask_records_the_caller_kind(monkeypatch, db):
    agent = _jwt_fed_agent(db)
    oauth_sessions.revoke(db, WS, user_id="alice", provider="team-idp")
    captured: dict = {}
    monkeypatch.setattr(invoke_service.rt, "stream_runtime_events_bearer", _ask_stream(captured))
    events = list(invoke_service.invoke_agent_events(
        agent, "who", session_id="s" * 40, actor_id=f"{agent.id}__alice",
        runtime_user_id="alice", bearer_token="user-jwt",
    ))
    assert captured["token"] == "user-jwt"
    # revocations still force re-auth over the bearer transport
    assert captured["force_reauth_providers"] == ["team-idp"]
    assert "session_uri" not in events[0]["data"]
    assert events[0]["data"]["url"] == "https://x"
    row = db.query(OauthPendingSession).one()
    assert (row.user_id, row.caller_kind) == ("alice", "user_jwt")


def _m2m_bearer(monkeypatch):
    monkeypatch.setattr(
        invoke_service.inbound_auth_service, "m2m_bearer_token", lambda _ws: "m2m-tok")


def test_m2m_ask_is_refused_and_never_completable(monkeypatch, db):
    """An as_user ask over the shared M2M subject: named 409, no consent URL,
    no pending session, no grant — nothing any member could complete."""
    from app.models.ledger import UserGrant

    agent = _jwt_fed_agent(db)
    captured: dict = {}
    _m2m_bearer(monkeypatch)
    monkeypatch.setattr(invoke_service.rt, "stream_runtime_events_bearer", _ask_stream(captured))
    with pytest.raises(AppError) as excinfo:
        list(invoke_service.invoke_agent_events(
            agent, "who", session_id="s" * 40, actor_id=f"{agent.id}__alice",
            runtime_user_id="alice",
        ))
    assert captured["token"] == "m2m-tok"
    err = excinfo.value
    assert (err.code, err.status_code) == ("identity.as_user_requires_user_jwt", 409)
    assert err.detail == {"provider": "team-idp", "tool": "userinfo",
                          "agent_id": agent.id, "caller": "m2m"}
    assert "https://x" not in err.message and SESSION not in json.dumps(err.detail)
    assert db.query(OauthPendingSession).count() == 0
    assert db.query(UserGrant).count() == 0


def test_m2m_ask_over_the_text_path_is_refused(monkeypatch, db):
    """/v1 invoke, /api/agents/{id}/invoke and evaluation runs share this path."""
    agent = _jwt_fed_agent(db)
    _m2m_bearer(monkeypatch)
    monkeypatch.setattr(invoke_service.rt, "invoke_runtime_text_bearer", lambda *_a, **kw: {
        "text": "", "session_id": kw["session_id"],
        "auth_required": [{"provider": "team-idp", "tool": "userinfo", "url": "https://x",
                           "scopes": [], "session_uri": SESSION}],
    })
    with pytest.raises(AppError) as excinfo:
        invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40, actor_id="alice")
    assert excinfo.value.code == "identity.as_user_requires_user_jwt"
    assert "user JWT" in excinfo.value.message
    assert db.query(OauthPendingSession).count() == 0


def test_m2m_ask_in_console_chat_says_turn_on_invoke_as_me(monkeypatch, db):
    agent = _jwt_fed_agent(db)
    _m2m_bearer(monkeypatch)
    monkeypatch.setattr(invoke_service.rt, "stream_runtime_events_bearer", _ask_stream({}))
    with TestClient(create_app()) as client:
        body = client.post(
            f"/api/chat/{agent.id}", json={"prompt": "who", "as_user": False}).text
    assert "event: auth_required" not in body and "https://x" not in body
    error = json.loads(body.split("event: error\ndata: ", 1)[1].split("\n", 1)[0])
    assert error["code"] == "identity.as_user_requires_user_jwt"
    assert "invoke as me" in error["message"]
    assert db.query(OauthPendingSession).count() == 0


def test_record_pending_never_records_an_m2m_ask(db):
    _pending(db, "m2m")
    assert db.query(OauthPendingSession).count() == 0


def _pending(db, kind, user="alice"):
    oauth_sessions.record_pending(
        db, WS, session_uri=SESSION, provider="team-idp", user_id=user,
        agent_id="a1", tool="t", caller_kind=kind,
    )


def _legacy_m2m_row(db, user="alice"):
    """A row written before M2M asks were refused."""
    db.add(OauthPendingSession(
        workspace_id=WS, session_uri=SESSION, provider="team-idp", user_id=user,
        agent_id="a1", tool="t", caller_kind="m2m",
    ))
    db.commit()


@pytest.mark.parametrize(("kind", "identifier"), [
    ("iam", {"userId": "alice"}),
    ("user_jwt", {"userToken": "tok-user_jwt"}),
])
def test_complete_uses_the_identifier_of_the_recorded_caller(db, kind, identifier):
    _pending(db, kind)
    data = MagicMock()
    oauth_sessions.complete(
        db, WS, SESSION, caller="alice", data_client=data,
        user_token=lambda k: f"tok-{k}",
    )
    data.complete_resource_token_auth.assert_called_once_with(
        userIdentifier=identifier, sessionUri=SESSION,
    )


def test_complete_refuses_an_m2m_session_without_calling_aws(db):
    _legacy_m2m_row(db)
    data = MagicMock()
    minted: list[str] = []
    with pytest.raises(AppError) as excinfo:
        oauth_sessions.complete(
            db, WS, SESSION, caller="alice", data_client=data,
            user_token=lambda k: minted.append(k) or f"tok-{k}",
        )
    assert (excinfo.value.code, excinfo.value.status_code) == (
        "identity.as_user_requires_user_jwt", 409)
    data.complete_resource_token_auth.assert_not_called()
    assert minted == []
    # the unusable row is dropped, so it can never be retried into a binding
    assert db.query(OauthPendingSession).count() == 0


def test_router_complete_refuses_an_m2m_session(monkeypatch, db):
    import app.routers.identity as identity_router
    from app.core.config import get_settings

    _legacy_m2m_row(db, user=get_settings().auth_username)
    data = MagicMock()
    monkeypatch.setattr(identity_router, "data_client", lambda _ctx: data)
    monkeypatch.setattr(
        identity_router.inbound_auth, "m2m_bearer_token", lambda _ctx: "m2m-tok")
    with TestClient(create_app()) as client:
        response = client.post("/api/identity/oauth/complete", json={"session_id": SESSION})
    assert response.status_code == 409
    assert response.json()["code"] == "identity.as_user_requires_user_jwt"
    data.complete_resource_token_auth.assert_not_called()


def test_another_user_cannot_complete_a_user_jwt_session(db):
    _pending(db, "user_jwt", user="alice")
    data = MagicMock()
    minted: list[str] = []
    with pytest.raises(AppError) as excinfo:
        oauth_sessions.complete(
            db, WS, SESSION, caller="bob", data_client=data,
            user_token=lambda k: minted.append(k) or f"tok-{k}",
        )
    assert (excinfo.value.code, excinfo.value.status_code) == (
        "identity.session_user_mismatch", 403)
    data.complete_resource_token_auth.assert_not_called()
    assert minted == []  # bob's token is never minted for alice's session
    assert db.query(OauthPendingSession).one().user_id == "alice"


def test_iam_ask_still_records_and_completes_with_the_user_id(monkeypatch, db):
    agent = Agent(
        workspace_id=WS, name="iam-fed-agent", method="zip_runtime", status="active",
        arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/if", spec=AS_USER_SPEC,
    )
    db.add(agent)
    db.commit()

    def fake_stream(_client, _arn, _prompt, **_kwargs):
        yield {"event": "auth_required", "data": {
            "provider": "team-idp", "tool": "userinfo", "url": "https://x",
            "scopes": ["openid"], "session_uri": SESSION,
        }}

    monkeypatch.setattr(invoke_service.rt, "stream_runtime_events", fake_stream)
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws: MagicMock())
    events = list(invoke_service.invoke_agent_events(
        agent, "who", session_id="s" * 40, actor_id=f"{agent.id}__alice",
        runtime_user_id="alice",
    ))
    assert events[0]["data"]["url"] == "https://x"
    assert db.query(OauthPendingSession).one().caller_kind == "iam"
    data = MagicMock()
    oauth_sessions.complete(db, WS, SESSION, caller="alice", data_client=data)
    data.complete_resource_token_auth.assert_called_once_with(
        userIdentifier={"userId": "alice"}, sessionUri=SESSION,
    )


def test_jwt_asked_session_without_a_token_is_409_and_kept(db):
    _pending(db, "user_jwt")
    data = MagicMock()
    with pytest.raises(AppError) as excinfo:
        oauth_sessions.complete(
            db, WS, SESSION, caller="alice", data_client=data, user_token=lambda _k: None,
        )
    assert excinfo.value.code == "identity.session_token_unavailable"
    data.complete_resource_token_auth.assert_not_called()
    assert db.query(OauthPendingSession).count() == 1


def test_unknown_caller_kind_is_stored_as_iam(db):
    _pending(db, "bogus")
    assert db.query(OauthPendingSession).one().caller_kind == "iam"


# ── Chat "invoke as me" ─────────────────────────────────────────────────────


def _capture_chat(monkeypatch, captured):
    def fake_stream(agent, prompt, **kwargs):
        captured.update(kwargs)
        yield {"event": "meta", "data": {"session_id": "s" * 40, "agent": "x", "mode": "stream"}}
        yield {"event": "done", "data": {"latency_ms": 1}}

    monkeypatch.setattr(chat_router, "chat_stream", fake_stream)


def _jwt_agent(db) -> str:
    agent = Agent(
        workspace_id=WS, name="jwt-chat-agent", method="zip_runtime", status="active",
        arn="arn:rt-chat", spec={"name": "jwt-chat-agent"},
        inbound_auth_mode="jwt", inbound_auth_config=jwt_auth().model_dump(),
    )
    db.add(agent)
    db.commit()
    return agent.id


@pytest.mark.parametrize(("as_user", "expected"), [
    (True, "user-jwt"), (None, "user-jwt"), (False, None),
])
def test_chat_toggle_picks_the_bearer(monkeypatch, db, as_user, expected):
    agent_id = _jwt_agent(db)
    captured: dict = {}
    _capture_chat(monkeypatch, captured)
    monkeypatch.setattr(
        chat_router.policy_identity, "gateway_user_token", lambda *_a, **_k: "user-jwt")
    body = {"prompt": "hi"} if as_user is None else {"prompt": "hi", "as_user": as_user}
    with TestClient(create_app()) as client:
        assert client.post(f"/api/chat/{agent_id}", json=body).status_code == 200
    assert captured.get("bearer_token") == expected
    # the Memory actor is the compound scoped actor either way
    assert captured["actor_id"] == f"{agent_id}__river"


def test_chat_explicit_as_user_without_a_pool_user_is_409(monkeypatch, db):
    agent_id = _jwt_agent(db)
    _capture_chat(monkeypatch, {})
    monkeypatch.setattr(
        chat_router.policy_identity, "gateway_user_token", lambda *_a, **_k: None)
    with TestClient(create_app()) as client:
        denied = client.post(f"/api/chat/{agent_id}", json={"prompt": "hi", "as_user": True})
        auto = client.post(f"/api/chat/{agent_id}", json={"prompt": "hi"})
    assert denied.status_code == 409
    assert denied.json()["code"] == "chat.as_user_unavailable"
    assert auto.status_code == 200  # no explicit ask → the M2M token


def test_chat_toggle_is_ignored_for_iam_agents(monkeypatch, db):
    agent = Agent(workspace_id=WS, name="iam-chat-agent", method="zip_runtime",
                  status="active", arn="arn:rt-iam", spec={"name": "iam-chat-agent"})
    db.add(agent)
    db.commit()
    captured: dict = {}
    _capture_chat(monkeypatch, captured)
    with TestClient(create_app()) as client:
        client.post(f"/api/chat/{agent.id}", json={"prompt": "hi", "as_user": True})
    assert "bearer_token" not in captured


def test_chat_meta_names_the_jwt_caller(monkeypatch, db):
    from app.services import chat as chat_service

    agent = db.get(Agent, _jwt_agent(db))
    monkeypatch.setattr(chat_service, "invoke_agent_events", lambda *_a, **_k: iter(()))
    metas = [
        next(chat_service.chat_stream(agent, "hi", bearer_token=tok))["data"]["inbound"]
        for tok in ("user-jwt", None)
    ]
    assert metas == [{"mode": "jwt", "caller": "user_jwt"}, {"mode": "jwt", "caller": "m2m"}]


# ── one-click switch on the agent page ──────────────────────────────────────


def _switchable(db, method="zip_runtime", **spec_over) -> str:
    from app.schemas.agent import AgentSpec

    spec = AgentSpec(name=f"sw-{method}".replace("_", "-"), method=method, system_prompt="p",
                     **spec_over).model_dump()
    agent = Agent(workspace_id=WS, name=spec["name"], method=method, status="active",
                  arn="arn:rt-sw", resource_id="rt-sw", spec=spec)
    db.add(agent)
    db.commit()
    return agent.id


def _no_deploy(monkeypatch):
    import app.routers.agents as agents_router

    started: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", started.append)
    monkeypatch.setattr(svc, "probe_discovery", lambda *_a, **_k: {})
    return started


@pytest.mark.parametrize("target", [
    {"mode": "jwt", "jwt": {"discovery_url": DISCOVERY, "allowed_clients": ["c"]}},
    {"mode": "iam"},
    None,
])
def test_switch_pins_the_spec_and_redeploys_in_place(monkeypatch, db, target):
    started = _no_deploy(monkeypatch)
    agent_id = _switchable(db)
    with TestClient(create_app()) as client:
        res = client.post(f"/api/agents/{agent_id}/inbound-auth",
                          json={"inbound_auth": target})
    assert res.status_code == 202, res.text
    assert len(started) == 1
    db.expire_all()
    row = db.get(Agent, agent_id)
    assert row.status == "deploying"
    assert row.spec["system_prompt"] == "p"  # everything else re-published as stored
    pinned = row.spec.get("inbound_auth")
    assert (pinned or {}).get("mode") == (target or {}).get("mode")
    from app.models.ledger import Job

    assert db.get(Job, res.json()["job_id"]).payload["mode"] == "update"


def test_switch_to_jwt_is_refused_for_a_harness(monkeypatch, db):
    started = _no_deploy(monkeypatch)
    agent_id = _switchable(db, method="harness")
    with TestClient(create_app()) as client:
        res = client.post(f"/api/agents/{agent_id}/inbound-auth", json={"inbound_auth": {
            "mode": "jwt", "jwt": {"discovery_url": DISCOVERY, "allowed_clients": ["c"]}}})
    assert res.status_code == 422
    assert res.json()["code"] == "agent.inbound_auth_unsupported"
    assert started == []


def test_switch_waits_for_an_in_flight_deploy(monkeypatch, db):
    _no_deploy(monkeypatch)
    agent_id = _switchable(db)
    row = db.get(Agent, agent_id)
    row.status = "deploying"
    db.commit()
    with TestClient(create_app()) as client:
        res = client.post(f"/api/agents/{agent_id}/inbound-auth", json={"inbound_auth": None})
    assert res.status_code == 409


def test_identity_view_reports_the_live_jwt_and_the_pin():
    from app.services import agent_identity as identity_view

    control = MagicMock()
    control.get_agent_runtime.return_value = {"authorizerConfiguration": {"customJWTAuthorizer": {
        "discoveryUrl": DISCOVERY, "allowedClients": ["c"],
        "customClaims": [{"inboundTokenClaimName": "cognito:groups"}],
    }}}
    control.list_oauth2_credential_providers.return_value = {"credentialProviders": []}
    control.list_api_key_credential_providers.return_value = {"credentialProviders": []}
    arn = "arn:aws:bedrock-agentcore:us-west-2:111:runtime/x-abc"
    agent = Agent(id="a1", name="x", method="container", status="active", resource_id="rt",
                  arn=arn,
                  spec={"name": "x", "inbound_auth": {"mode": "jwt"}},
                  inbound_auth_mode="jwt")
    inbound = identity_view.agent_identity(control, agent, {})["inbound"]
    assert inbound["jwt"]["allowed_clients"] == ["c"]
    assert inbound["jwt"]["custom_claims"] == ["cognito:groups"]
    assert (inbound["capable"], inbound["pinned"], inbound["source"]) == (True, "jwt", "aws")
    assert inbound["invoke_url"] == rt.bearer_invoke_url("us-west-2", arn)


# ── service-model claim patterns ─────────────────────────────────────────────


@pytest.mark.parametrize("name", ["cognito:groups", "custom.tenant_id", "scp-x"])
def test_claim_names_in_the_service_pattern(name):
    CustomClaim(name=name, match_values=["admins"])


@pytest.mark.parametrize(("name", "value"), [
    ("has space", "v"), ("ok", "has space"), ("ok", "a/b"), ("bad!", "v"),
])
def test_claims_outside_the_service_pattern_are_refused(name, value):
    with pytest.raises(ValueError, match="letters, digits"):
        CustomClaim(name=name, match_values=[value])


def test_workspace_default_round_trips_claims(db):
    auth = InboundAuth(mode="jwt", jwt=JwtInboundConfig(
        discovery_url=DISCOVERY,
        custom_claims=[CustomClaim(name="cognito:groups", value_type="STRING_ARRAY",
                                   match_operator="CONTAINS", match_values=["admins"])],
    ))
    row = db.get(Workspace, WS)
    svc.set_workspace_default(db, row, auth)
    assert svc.workspace_default(db.get(Workspace, WS)) == auth


# ── migration ───────────────────────────────────────────────────────────────


def test_caller_kind_column_is_migrated(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE oauth_pending_sessions (id VARCHAR(32) PRIMARY KEY, "
            "workspace_id VARCHAR(32), session_uri VARCHAR(512), provider VARCHAR(128), "
            "user_id VARCHAR(128), agent_id VARCHAR(32), tool VARCHAR(80), created_at DATETIME)"
        ))
        conn.execute(sa.text(
            "INSERT INTO oauth_pending_sessions (id, session_uri, provider, user_id) "
            "VALUES ('x', 'urn:s', 'p', 'u')"
        ))
    db_module._migrate_oauth_session_columns(engine)
    with engine.begin() as conn:
        kinds = conn.execute(sa.text("SELECT caller_kind FROM oauth_pending_sessions")).all()
    assert kinds == [("iam",)]


# ── JWT inbound vs the SigV4-only side doors ─────────────────────────────────


RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/jwt-AbCdEf1234"


def test_a_jwt_inbound_agent_is_not_canary_eligible():
    from types import SimpleNamespace

    from app.optimization.service import canary_capability

    row = SimpleNamespace(method="zip_runtime", status="active", arn=RUNTIME_ARN, spec={},
                          inbound_auth_mode="jwt")
    cap = canary_capability(row)
    assert (cap["eligible"], cap["reason_code"]) == (False, "jwt-inbound")
    assert "SigV4" in cap["reason"]
    for mode in ("iam", None):
        row.inbound_auth_mode = mode
        assert canary_capability(row)["eligible"] is True


def test_the_a2a_demo_refuses_a_jwt_agent_before_signing_a_request(client, monkeypatch):
    from app.services.agentcore import client as ac_client

    data = MagicMock()
    monkeypatch.setattr(ac_client, "data_client", lambda _ws=None: data)
    with SessionLocal() as session:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="jwt-desk", method="zip_runtime",
                      status="active", arn=RUNTIME_ARN, spec={"system_prompt": "s"},
                      inbound_auth_mode="jwt")
        session.add(agent)
        session.commit()
        agent_id = agent.id
    res = client.post("/api/registry/a2a-demo", json={"agent_id": agent_id, "question": "q"})
    assert res.status_code == 409
    assert res.json()["code"] == "registry.a2a_demo_jwt_unsupported"
    data.invoke_agent_runtime.assert_not_called()


# ── evaluation retry policy over the bearer transport ────────────────────────


def _bearer_failure(status: int):
    from tests.test_inbound_auth import FakeBearerResponse, _opener

    response = FakeBearerResponse(status_code=status, body=b'{"message":"x"}')
    try:
        rt.invoke_runtime_text_bearer(
            "us-west-2", RUNTIME_ARN, "tok", "hi", http_response=_opener(response, {}))
    except Exception as exc:  # noqa: BLE001 — the raised type is the subject
        return exc
    raise AssertionError("the bearer invoke did not fail")


@pytest.mark.parametrize(("status", "transient"), [
    (500, True), (502, True), (503, True), (429, True),
    (400, False), (404, False), (401, False), (403, False),
])
def test_bearer_http_failures_retry_only_when_upstream_is_at_fault(status, transient):
    from app.evaluation.service import transient_invoke_error

    exc = _bearer_failure(status)
    expected = rt.RuntimeBearerAuthError if status in (401, 403) else rt.RuntimeBearerHttpError
    assert isinstance(exc, expected)
    prefix = (
        "the Runtime's JWT authorizer" if status in (401, 403)
        else f"bearer invoke returned HTTP {status}"
    )
    assert str(exc).startswith(prefix)
    assert transient_invoke_error(exc) is transient


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("refused"),
    httpx.ReadTimeout("slow"),
    httpx.RemoteProtocolError("peer closed connection"),
])
def test_bearer_transport_failures_are_transient(exc):
    from app.evaluation.service import transient_invoke_error

    assert transient_invoke_error(exc) is True


# ── the in-place switch's 422 never echoes the stored spec ───────────────────


def test_switch_422_does_not_echo_the_stored_spec(monkeypatch):
    import app.routers.agents as agents_router

    marker = "stored-value-that-must-not-leak"
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda _job: None)
    with SessionLocal() as session:
        agent = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, name="sw-echo", method="zip_runtime",
            status="active", arn=RUNTIME_ARN, resource_id="rt-echo",
            # a stored spec the current schema refuses (env values are strings)
            spec={"name": "sw-echo", "method": "zip_runtime", "system_prompt": "p",
                  "env": {"API_PASS": [marker]}},
        )
        session.add(agent)
        session.commit()
        agent_id = agent.id
    with TestClient(create_app()) as client:
        res = client.post(f"/api/agents/{agent_id}/inbound-auth",
                          json={"inbound_auth": {"mode": "iam"}})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "agent.inbound_auth_invalid"
    assert marker not in res.text
    assert all("input" not in error for error in res.json()["detail"]["errors"])
