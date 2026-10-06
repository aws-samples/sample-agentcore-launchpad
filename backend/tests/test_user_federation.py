"""as_user (USER_FEDERATION 3LO) outbound auth: consent sessions, the
CompleteResourceTokenAuth leg, grants, revocation (force re-auth), the
/api/identity/{oauth,grants} routes, the invoke/chat event plumbing, the
return-URL reconcile and the generated non-blocking exchange."""

import ast
import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

import app.routers.chat as chat_router
import app.routers.identity as identity_router
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal, engine, schema_drift
from app.core.errors import AppError, NotFoundError
from app.deployer import return_url
from app.deployer.environment import ENV_AGENT_ID, ENV_OAUTH_RETURN_URL, runtime_environment
from app.main import create_app
from app.models.ledger import (
    Agent,
    ChatMessage,
    OauthPendingSession,
    UserGrant,
    UserTokenRevocation,
)
from app.schemas.agent import AgentSpec
from app.services import identity_providers as ip
from app.services import invoke as invoke_service
from app.services import oauth_sessions
from app.services.agentcore import runtime as rt
from app.templates import identity_support
from app.templates.gateway_support import runtime_user_id
from app.templates.strands_agent import render_main_py

WS = DEFAULT_WORKSPACE_ID
SESSION = "urn:ietf:params:oauth:request_uri:abc123"
AUTH_URL = "https://bedrock-agentcore.us-west-2.amazonaws.com/identities/oauth2/authorize?request_uri=x"

AS_USER_TOOL = {
    "type": "rest",
    "name": "userinfo",
    "config": {"url": "https://idp.example/oauth2/userInfo"},
    "auth": {"connection": "team-idp", "kind": "oauth2", "mode": "as_user",
             "scopes": ["openid", "profile"]},
}
AS_USER_SPEC = {
    "name": "fed-agent",
    "method": "zip_runtime",
    "system_prompt": "p",
    "tools": [AS_USER_TOOL],
}


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def _record(db, *, user="alice", provider="team-idp", agent="a1", session=SESSION, tool="t"):
    oauth_sessions.record_pending(
        db, WS, session_uri=session, provider=provider, user_id=user,
        agent_id=agent, tool=tool, scopes=["openid"],
    )


def _complete(db, *, caller="alice", session=SESSION, data=None):
    return oauth_sessions.complete(
        db, WS, session, caller=caller, data_client=data or MagicMock()
    )


# ── service: sessions and the fourth leg ─────────────────────────────────────


def test_record_then_complete_binds_with_the_recorded_user(db):
    _record(db)
    assert oauth_sessions.grant_status(
        db, WS, user_id="alice", provider="team-idp", agent_id="a1"
    )["status"] == "pending"
    data = MagicMock()
    result = _complete(db, data=data)
    data.complete_resource_token_auth.assert_called_once_with(
        userIdentifier={"userId": "alice"}, sessionUri=SESSION
    )
    assert result == {"completed": True, "provider": "team-idp", "agent_id": "a1", "tool": "t"}
    status = oauth_sessions.grant_status(
        db, WS, user_id="alice", provider="team-idp", agent_id="a1"
    )
    assert status["status"] == "authorized" and status["authorized_at"]
    # single use: the session row is gone
    assert db.query(OauthPendingSession).count() == 0
    with pytest.raises(NotFoundError) as err:
        _complete(db)
    assert err.value.code == "identity.session_unknown"


def test_record_is_idempotent_and_needs_all_three_ids(db):
    _record(db)
    _record(db)
    assert db.query(OauthPendingSession).count() == 1
    assert db.query(UserGrant).count() == 1
    oauth_sessions.record_pending(db, WS, session_uri="", provider="p", user_id="u")
    oauth_sessions.record_pending(db, WS, session_uri="s", provider="p", user_id="")
    assert db.query(OauthPendingSession).count() == 1


def test_complete_refuses_another_signed_in_user_without_calling_aws(db):
    _record(db)
    data = MagicMock()
    with pytest.raises(AppError) as err:
        _complete(db, caller="mallory", data=data)
    assert err.value.code == "identity.session_user_mismatch"
    assert err.value.status_code == 403
    assert "alice" not in err.value.message and "mallory" not in err.value.message
    data.complete_resource_token_auth.assert_not_called()
    # the rightful user can still complete it
    assert _complete(db)["completed"] is True


def test_expired_session_is_dropped_and_409(db):
    _record(db)
    row = db.query(OauthPendingSession).one()
    row.created_at = datetime.now(UTC) - timedelta(seconds=oauth_sessions.SESSION_TTL_S + 5)
    db.commit()
    data = MagicMock()
    with pytest.raises(AppError) as err:
        _complete(db, data=data)
    assert err.value.code == "identity.session_expired" and err.value.status_code == 409
    data.complete_resource_token_auth.assert_not_called()
    assert db.query(OauthPendingSession).count() == 0


def test_aws_failure_is_502_and_keeps_the_session_for_a_retry(db):
    _record(db)
    data = MagicMock()
    data.complete_resource_token_auth.side_effect = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "nope"}}, "Op"
    )
    with pytest.raises(AppError) as err:
        _complete(db, data=data)
    assert err.value.code == "identity.session_completion_failed"
    assert err.value.status_code == 502
    assert db.query(OauthPendingSession).count() == 1
    assert oauth_sessions.grant_status(
        db, WS, user_id="alice", provider="team-idp", agent_id="a1"
    )["status"] == "pending"


def test_sessions_are_workspace_scoped(db):
    _record(db)
    with pytest.raises(NotFoundError):
        oauth_sessions.complete(db, "other-ws", SESSION, caller="alice", data_client=MagicMock())


# ── service: revocation = force re-auth until a NEW consent ──────────────────


def test_revoke_forces_reauth_until_that_agent_reauthorizes(db):
    _record(db, agent="a1", session="s1")
    _record(db, agent="a2", session="s2")
    _complete(db, session="s1")
    _complete(db, session="s2")
    names = ("team-idp",)
    assert oauth_sessions.pending_revocations(
        db, WS, user_id="alice", agent_id="a1", providers=names
    ) == []

    out = oauth_sessions.revoke(db, WS, user_id="alice", provider="team-idp")
    assert out == {"revoked": True, "provider": "team-idp", "agents": 2}
    for agent in ("a1", "a2"):
        assert oauth_sessions.pending_revocations(
            db, WS, user_id="alice", agent_id=agent, providers=names
        ) == ["team-idp"]
    grants = oauth_sessions.list_grants(db, WS, user_id="alice")
    assert {g["status"] for g in grants} == {"revoked"}
    assert all(g["force_reauth"] for g in grants)

    # a turn that re-asks does NOT cancel the revocation; only a new consent does
    _record(db, agent="a1", session="s3")
    assert oauth_sessions.pending_revocations(
        db, WS, user_id="alice", agent_id="a1", providers=names
    ) == ["team-idp"]
    _complete(db, session="s3")
    assert oauth_sessions.pending_revocations(
        db, WS, user_id="alice", agent_id="a1", providers=names
    ) == []
    # re-consenting on a1 leaves a2 still forced
    assert oauth_sessions.pending_revocations(
        db, WS, user_id="alice", agent_id="a2", providers=names
    ) == ["team-idp"]


def test_revoke_is_per_user_and_drops_in_flight_consents(db):
    _record(db, user="alice", session="sa")
    _record(db, user="bob", session="sb")
    oauth_sessions.revoke(db, WS, user_id="alice", provider="team-idp")
    assert oauth_sessions.pending_revocations(
        db, WS, user_id="bob", agent_id="a1", providers=["team-idp"]
    ) == []
    # alice's pending consent cannot bind after the revocation
    with pytest.raises(NotFoundError):
        _complete(db, session="sa")
    assert _complete(db, caller="bob", session="sb")["completed"] is True


def test_revoke_twice_restamps_and_revoking_an_unknown_grant_still_forces(db):
    first = oauth_sessions.revoke(db, WS, user_id="alice", provider="never-used")
    assert first["agents"] == 0
    assert oauth_sessions.pending_revocations(
        db, WS, user_id="alice", agent_id="a9", providers=["never-used"]
    ) == ["never-used"]
    assert oauth_sessions.grant_status(
        db, WS, user_id="alice", provider="never-used", agent_id="a9"
    ) == {"connection": "never-used", "agent_id": "a9", "status": "none",
          "force_reauth": True, "authorized_at": None}


def test_list_grants_is_the_callers_own_only(db):
    _record(db, user="alice", session="sa")
    _record(db, user="bob", session="sb", provider="other")
    mine = oauth_sessions.list_grants(db, WS, user_id="alice")
    assert [(g["connection"], g["agent_id"], g["status"]) for g in mine] == [
        ("team-idp", "a1", "pending")
    ]
    assert oauth_sessions.list_grants(db, WS, user_id="carol") == []


def test_grants_of_a_deleted_agent_are_hidden_and_forgotten_on_delete(db):
    gone = Agent(workspace_id=WS, name="gone-agent", method="zip_runtime", status="deleted")
    live = Agent(workspace_id=WS, name="live-agent", method="zip_runtime", status="active")
    db.add_all([gone, live])
    db.commit()
    _record(db, user="alice", agent=gone.id, session="s-gone")
    _record(db, user="alice", agent=live.id, session="s-live")
    # a stale row (deleted before forget_agent existed) never lists
    assert [g["agent_id"] for g in oauth_sessions.list_grants(db, WS, user_id="alice")] == [
        live.id
    ]
    assert oauth_sessions.forget_agent(db, WS, gone.id) == 1
    db.commit()
    assert db.query(UserGrant).filter_by(agent_id=gone.id).count() == 0
    assert db.query(OauthPendingSession).filter_by(agent_id=gone.id).count() == 0
    assert db.query(UserGrant).filter_by(agent_id=live.id).count() == 1


def test_forget_connection_drops_its_grants_revocations_and_sessions(db):
    _record(db, user="alice", provider="team-idp", session="s1")
    _record(db, user="bob", provider="team-idp", agent="a2", session="s2")
    _record(db, user="alice", provider="other-idp", session="s3")
    oauth_sessions.revoke(db, WS, user_id="alice", provider="team-idp")
    assert oauth_sessions.forget_connection(db, WS, "team-idp") == 2
    db.commit()
    assert db.query(UserGrant).filter_by(provider="team-idp").count() == 0
    assert db.query(UserTokenRevocation).filter_by(provider="team-idp").count() == 0
    assert db.query(OauthPendingSession).filter_by(provider="team-idp").count() == 0
    assert [g["connection"] for g in oauth_sessions.list_grants(db, WS, user_id="alice")] == [
        "other-idp"
    ]


def test_user_grants_table_is_in_the_schema_without_drift():
    assert schema_drift(engine) == {}


# ── router ───────────────────────────────────────────────────────────────────


@pytest.fixture
def data(monkeypatch):
    stub = MagicMock()
    monkeypatch.setattr(identity_router, "data_client", lambda _ctx: stub)
    return stub


def _operator() -> str:
    # auth gate off: the caller is the built-in admin username
    return get_settings().auth_username


def test_router_complete_status_list_revoke_round_trip(client, data, db):
    agent = Agent(workspace_id=WS, name="fed-agent", method="zip_runtime", status="active")
    db.add(agent)
    db.commit()
    _record(db, user=_operator(), agent=agent.id)

    status = client.get(f"/api/identity/grants/team-idp/status?agent_id={agent.id}")
    assert status.status_code == 200 and status.json()["status"] == "pending"

    done = client.post("/api/identity/oauth/complete", json={"session_id": SESSION})
    assert done.status_code == 200, done.text
    assert done.json()["agent_name"] == "fed-agent"
    data.complete_resource_token_auth.assert_called_once()

    flipped = client.get(f"/api/identity/grants/team-idp/status?agent_id={agent.id}").json()
    assert flipped["status"] == "authorized" and flipped["force_reauth"] is False

    listed = client.get("/api/identity/grants").json()["grants"]
    assert [(g["connection"], g["agent_name"], g["status"]) for g in listed] == [
        ("team-idp", "fed-agent", "authorized")
    ]

    revoked = client.delete("/api/identity/grants/team-idp")
    assert revoked.status_code == 200 and revoked.json()["revoked"] is True
    after = client.get("/api/identity/grants").json()["grants"][0]
    assert after["status"] == "revoked" and after["force_reauth"] is True


def test_router_complete_unknown_session_is_404(client, data):
    response = client.post("/api/identity/oauth/complete", json={"session_id": "nope"})
    assert response.status_code == 404
    assert response.json()["code"] == "identity.session_unknown"
    data.complete_resource_token_auth.assert_not_called()


def test_router_complete_another_users_session_is_403(client, data, db):
    _record(db, user="someone-else")
    response = client.post("/api/identity/oauth/complete", json={"session_id": SESSION})
    assert response.status_code == 403
    assert response.json()["code"] == "identity.session_user_mismatch"
    data.complete_resource_token_auth.assert_not_called()


def test_router_rejects_a_bad_connection_name(client):
    assert client.get("/api/identity/grants/bad%20name/status").status_code == 422


# ── invoke plumbing ──────────────────────────────────────────────────────────


def test_runtime_payload_auth_required_event_is_normalized():
    events = list(rt._runtime_payload_events({
        "event": "auth_required", "provider": "team-idp", "tool": "userinfo",
        "url": AUTH_URL, "scopes": ["openid"], "session_uri": SESSION,
    }))
    assert events == [{"event": "auth_required", "data": {
        "provider": "team-idp", "tool": "userinfo", "url": AUTH_URL,
        "scopes": ["openid"], "session_uri": SESSION,
    }}]


def test_invoke_params_carry_force_reauth_only_when_set():
    base = rt._runtime_invoke_params("arn", "hi", "s" * 40, "actor", None)
    assert "force_reauth_providers" not in json.loads(base["payload"])
    forced = rt._runtime_invoke_params(
        "arn", "hi", "s" * 40, "actor", None, force_reauth_providers=["team-idp"]
    )
    assert json.loads(forced["payload"])["force_reauth_providers"] == ["team-idp"]


def _fed_agent(db) -> Agent:
    agent = Agent(
        workspace_id=WS, name="fed-agent", method="zip_runtime", status="active",
        arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/fed", spec=AS_USER_SPEC,
    )
    db.add(agent)
    db.commit()
    return agent


def test_invoke_events_record_the_ask_strip_the_session_and_send_revocations(
    monkeypatch, db
):
    agent = _fed_agent(db)
    oauth_sessions.revoke(db, WS, user_id="alice", provider="team-idp")
    captured: dict = {}

    def fake_stream(_client, _arn, _prompt, **kwargs):
        captured.update(kwargs)
        yield {"event": "auth_required", "data": {
            "provider": "team-idp", "tool": "userinfo", "url": AUTH_URL,
            "scopes": ["openid"], "session_uri": SESSION,
        }}
        yield {"event": "complete", "data": {"text": ""}}

    monkeypatch.setattr(invoke_service.rt, "stream_runtime_events", fake_stream)
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws: MagicMock())
    events = list(invoke_service.invoke_agent_events(
        agent, "who am i", session_id="s" * 40, actor_id=f"{agent.id}__alice",
        runtime_user_id="alice",
    ))
    assert captured["runtime_user_id"] == "alice"
    assert captured["force_reauth_providers"] == ["team-idp"]
    ask = events[0]
    assert ask["event"] == "auth_required"
    assert "session_uri" not in ask["data"]
    assert ask["data"]["agent_id"] == agent.id and ask["data"]["url"] == AUTH_URL
    row = db.query(OauthPendingSession).one()
    assert (row.user_id, row.provider, row.agent_id) == ("alice", "team-idp", agent.id)


def test_invoke_text_relays_asks_without_the_session(monkeypatch, db):
    agent = _fed_agent(db)
    monkeypatch.setattr(
        invoke_service.rt,
        "invoke_runtime_text",
        lambda *_a, **kw: {
            "text": "", "session_id": kw["session_id"],
            "auth_required": [{"provider": "team-idp", "tool": "userinfo",
                               "url": AUTH_URL, "scopes": [], "session_uri": SESSION}],
        },
    )
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws: MagicMock())
    result = invoke_service.invoke_agent_text(
        agent, "hi", session_id="s" * 40, actor_id="alice"
    )
    assert result["auth_required"][0]["agent_id"] == agent.id
    assert "session_uri" not in result["auth_required"][0]
    assert db.query(OauthPendingSession).one().user_id == "alice"


ASK = {"provider": "team-idp", "tool": "userinfo", "url": AUTH_URL, "scopes": [],
       "session_uri": SESSION}


def _stub_runtime(monkeypatch):
    def fake_stream(_client, _arn, _prompt, **_kw):
        yield {"event": "auth_required", "data": dict(ASK)}
        yield {"event": "complete", "data": {"text": ""}}

    monkeypatch.setattr(invoke_service.rt, "stream_runtime_events", fake_stream)
    monkeypatch.setattr(
        invoke_service.rt, "invoke_runtime_text",
        lambda *_a, **kw: {"text": "", "session_id": kw["session_id"],
                           "auth_required": [dict(ASK)]},
    )
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws: MagicMock())


def test_a_non_console_caller_never_records_a_consent_it_cannot_complete(monkeypatch, db):
    """/v1 has no signed-in console user: leg 4 could never complete, so the
    ask is refused by name — on both the sync and the streaming invoke."""
    agent = _fed_agent(db)
    _stub_runtime(monkeypatch)
    with pytest.raises(AppError) as err:
        invoke_service.invoke_agent_text(
            agent, "hi", session_id="s" * 40, actor_id=f"{agent.id}__api",
            console_user=False,
        )
    assert (err.value.code, err.value.status_code) == (
        oauth_sessions.AS_USER_REQUIRES_CONSOLE, 409)
    assert err.value.detail == {"provider": "team-idp", "tool": "userinfo",
                                "agent_id": agent.id}
    assert AUTH_URL not in err.value.message
    with pytest.raises(AppError) as err:
        list(invoke_service.invoke_agent_events(
            agent, "hi", session_id="s" * 40, actor_id=f"{agent.id}__api",
            console_user=False,
        ))
    assert err.value.code == oauth_sessions.AS_USER_REQUIRES_CONSOLE
    assert db.query(OauthPendingSession).count() == 0


def test_v1_answers_an_as_user_ask_with_a_named_409_and_records_nothing(monkeypatch, db):
    from app.models.ledger import ApiKey
    from app.routers.apikeys import hash_key

    agent = _fed_agent(db)
    db.add(ApiKey(workspace_id=WS, name="v1", prefix="test", key_hash=hash_key("v1-key")))
    db.commit()
    _stub_runtime(monkeypatch)
    headers = {"X-Api-Key": "v1-key"}
    with TestClient(create_app()) as client:
        sync = client.post(f"/v1/agents/{agent.id}/invoke", json={"prompt": "hi"},
                           headers=headers)
        stream = client.post(f"/v1/agents/{agent.id}/invoke-stream",
                             json={"prompt": "hi"}, headers=headers)
    assert sync.status_code == 409
    assert sync.json()["code"] == "identity.as_user_requires_console"
    assert AUTH_URL not in sync.text
    assert stream.status_code == 200
    assert "event: error" in stream.text
    assert "identity.as_user_requires_console" in stream.text
    assert "event: auth_required" not in stream.text and AUTH_URL not in stream.text
    assert db.query(OauthPendingSession).count() == 0


@pytest.mark.parametrize("url", [
    "javascript:alert(document.cookie)",
    "data:text/html,<script>alert(1)</script>",
    "http://idp.example/authorize?x=1",
    "//idp.example/authorize",
    "https:///no-host",
    "",
])
def test_auth_required_drops_a_url_that_is_not_https(url):
    (event,) = rt._runtime_payload_events({
        "event": "auth_required", "provider": "team-idp", "tool": "userinfo",
        "url": url, "session_uri": SESSION,
    })
    assert event["data"]["url"] is None
    assert event["data"]["provider"] == "team-idp"  # the ask itself still stands


def test_agents_without_as_user_tools_never_touch_the_ledger(monkeypatch):
    agent = Agent(workspace_id=WS, spec={"tools": [{"type": "rest", "name": "x"}]})
    monkeypatch.setattr(invoke_service, "as_user_connections_stored", lambda _s: [])
    assert invoke_service._pending_force_reauth(agent, "alice") == []
    assert invoke_service._pending_force_reauth(
        Agent(workspace_id=WS, spec=AS_USER_SPEC), None
    ) == []


def test_chat_persists_the_ask_without_the_url(monkeypatch, db):
    agent = _fed_agent(db)
    agent_id = agent.id

    def fake_stream(agent, prompt, **_kwargs):
        yield {"event": "meta", "data": {"session_id": "s" * 40, "agent": "x", "mode": "stream"}}
        yield {"event": "delta", "data": {"text": "checking"}}
        yield {"event": "auth_required", "data": {
            "provider": "team-idp", "tool": "userinfo", "url": AUTH_URL,
            "scopes": ["openid"], "agent_id": agent_id,
        }}
        yield {"event": "done", "data": {"latency_ms": 1}}

    monkeypatch.setattr(chat_router, "chat_stream", fake_stream)
    with TestClient(create_app()) as client:
        body = client.post(f"/api/chat/{agent_id}", json={"prompt": "who am i"}).text
    assert "event: auth_required" in body and AUTH_URL in body
    rows = (
        db.query(ChatMessage).filter_by(agent_id=agent_id).order_by(ChatMessage.id).all()
    )
    assert [(r.role, r.text, r.name) for r in rows] == [
        ("user", "who am i", None),
        ("agent", "checking", None),
        ("auth", "team-idp", "userinfo"),
    ]
    assert all(AUTH_URL not in (r.text or "") for r in rows)


@pytest.mark.parametrize(("events", "expected"), [
    # deltas after the ask stay in the one bubble the user saw; the card follows it
    (["看", "ask", "起来您尚未完成授权", "tool", "ok"],
     [("agent", "看起来您尚未完成授权"), ("auth", "team-idp"), ("tool", ""), ("agent", "ok")]),
    # an ask before any text is saved in place; the answer opens after it
    (["ask", "hi"], [("auth", "team-idp"), ("agent", "hi")]),
])
def test_chat_answer_around_an_ask_is_one_row(monkeypatch, db, events, expected):
    agent_id = _fed_agent(db).id

    def fake_stream(agent, prompt, **_kwargs):
        yield {"event": "meta", "data": {"session_id": "s" * 40, "agent": "x", "mode": "stream"}}
        for item in events:
            if item == "ask":
                yield {"event": "auth_required", "data": {
                    "provider": "team-idp", "tool": "userinfo", "url": AUTH_URL}}
            elif item == "tool":
                yield {"event": "tool", "data": {"name": ""}}
            else:
                yield {"event": "delta", "data": {"text": item}}
        yield {"event": "done", "data": {"latency_ms": 1}}

    monkeypatch.setattr(chat_router, "chat_stream", fake_stream)
    with TestClient(create_app()) as client:
        client.post(f"/api/chat/{agent_id}", json={"prompt": "who am i"})
    rows = (
        db.query(ChatMessage).filter_by(agent_id=agent_id).order_by(ChatMessage.id).all()
    )
    assert [(r.role, r.text) for r in rows[1:]] == expected


# ── spec, env, validation ────────────────────────────────────────────────────


def test_as_user_is_supported_but_obo_stays_a_gateway_target_mode():
    control = MagicMock()
    control.list_oauth2_credential_providers.return_value = {
        "credentialProviders": [{"name": "team-idp"}]
    }
    control.list_api_key_credential_providers.return_value = {"credentialProviders": []}
    ip.validate_tool_auth(control, AgentSpec(**AS_USER_SPEC))
    obo = {**AS_USER_TOOL, "auth": {**AS_USER_TOOL["auth"], "mode": "obo"}}
    with pytest.raises(AppError) as err:
        ip.validate_tool_auth(control, AgentSpec(**{**AS_USER_SPEC, "tools": [obo]}))
    assert err.value.code == "identity.mode_unsupported"
    assert "gateway target" in err.value.message


def test_as_user_connections_read_both_stored_shapes():
    legacy = {"tools": [
        {"type": "rest", "name": "a", "auth": {"provider": "old-idp", "flow": "USER_FEDERATION"}},
        {"type": "rest", "name": "b", "auth": {"provider": "m2m", "flow": "M2M"}},
        {"type": "rest", "name": "c", "auth": {"connection": "team-idp", "mode": "as_user"}},
        "not-a-tool",
    ]}
    assert identity_support.as_user_connections_stored(legacy) == ["old-idp", "team-idp"]
    assert identity_support.as_user_connections_stored(None) == []
    assert identity_support.as_user_connections(AgentSpec(**AS_USER_SPEC)) == ["team-idp"]


def test_runtime_user_id_is_sent_for_as_user_tools():
    assert runtime_user_id(AS_USER_SPEC, "alice") == "alice"


def test_environment_carries_return_url_and_agent_id_only_for_as_user(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_PUBLIC_BASE_URL", "https://console.example/")
    get_settings.cache_clear()
    try:
        env = runtime_environment(AgentSpec(**AS_USER_SPEC), {}, agent_id="a1")
        assert env[ENV_OAUTH_RETURN_URL] == "https://console.example/auth/return"
        assert env[ENV_AGENT_ID] == "a1"
        m2m = {**AS_USER_TOOL, "auth": {**AS_USER_TOOL["auth"], "mode": "as_agent"}}
        plain = runtime_environment(
            AgentSpec(**{**AS_USER_SPEC, "tools": [m2m]}), {}, agent_id="a1"
        )
        assert ENV_OAUTH_RETURN_URL not in plain and ENV_AGENT_ID not in plain
    finally:
        get_settings.cache_clear()


def test_explicit_return_url_overrides_the_derived_one(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_OAUTH_RETURN_URL", "https://proxy.example/lp/auth/return")
    get_settings.cache_clear()
    try:
        assert get_settings().resolved_oauth_return_url() == (
            "https://proxy.example/lp/auth/return"
        )
    finally:
        get_settings.cache_clear()


# ── deployer: return-URL allow-list reconcile ────────────────────────────────


def test_return_url_is_added_preserving_other_entries_and_is_idempotent():
    control = MagicMock()
    control.get_workload_identity.return_value = {
        "allowedResourceOauth2ReturnUrls": ["https://other.example/cb"]
    }
    logs: list[str] = []
    assert return_url.ensure_return_url_allowed(
        control, "rt-1", "https://console.example/auth/return", logs.append
    ) is True
    control.update_workload_identity.assert_called_once_with(
        name="rt-1",
        allowedResourceOauth2ReturnUrls=[
            "https://console.example/auth/return", "https://other.example/cb",
        ],
    )
    control.reset_mock()
    control.get_workload_identity.return_value = {
        "allowedResourceOauth2ReturnUrls": ["https://console.example/auth/return"]
    }
    assert return_url.ensure_return_url_allowed(
        control, "rt-1", "https://console.example/auth/return", logs.append
    ) is False
    control.update_workload_identity.assert_not_called()


def test_return_url_stage_is_a_noop_without_as_user_and_warns_after_retries():
    control = MagicMock()
    m2m = {**AS_USER_TOOL, "auth": {**AS_USER_TOOL["auth"], "mode": "as_agent"}}
    assert return_url.register_return_url_stage(
        control, AgentSpec(**{**AS_USER_SPEC, "tools": [m2m]}), "rt-1", lambda _m: None
    ) is True
    control.get_workload_identity.assert_not_called()

    control.get_workload_identity.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetWorkloadIdentity"
    )
    logs: list[str] = []
    slept: list[float] = []
    # never raises: the runtime is already READY, and a failed stage would make
    # the operator's retry a whole new UpdateAgentRuntime version
    assert return_url.register_return_url_stage(
        control, AgentSpec(**AS_USER_SPEC), "rt-1", logs.append, sleeper=slept.append
    ) is False
    assert control.get_workload_identity.call_count == return_url.RETURN_URL_ATTEMPTS
    assert slept == [2.0, 4.0]
    assert logs[-1].startswith("WARNING: return-URL registration failed")
    assert "AccessDeniedException" in logs[-1] and "redeploy" in logs[-1]


def test_return_url_stage_recovers_from_a_transient_failure():
    control = MagicMock()
    control.get_workload_identity.side_effect = [
        ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}},
                    "GetWorkloadIdentity"),
        {"allowedResourceOauth2ReturnUrls": []},
    ]
    logs: list[str] = []
    assert return_url.register_return_url_stage(
        control, AgentSpec(**AS_USER_SPEC), "rt-1", logs.append, sleeper=lambda _s: None
    ) is True
    control.update_workload_identity.assert_called_once()
    assert "retrying (1/3)" in logs[0]


def test_a_failed_return_url_registration_does_not_fail_the_deploy_stage(monkeypatch, db):
    """READY + a failed allow-list call = a succeeded stage with a warning in
    its detail, so resume / the operator never re-publishes for it."""
    from types import SimpleNamespace

    from app.deployer import zip_runtime as zr
    from app.deployer.pipeline import StageContext
    from tests.conftest import ws_ctx

    agent = Agent(workspace_id=WS, name="fed-agent", method="zip_runtime",
                  status="deploying", spec=AgentSpec(**AS_USER_SPEC).model_dump())
    db.add(agent)
    db.commit()
    control = MagicMock()
    control.create_agent_runtime.return_value = {
        "agentRuntimeId": "rt-9", "agentRuntimeArn": "arn:rt-9", "agentRuntimeVersion": "1"}
    control.get_agent_runtime.return_value = {
        "agentRuntimeId": "rt-9", "agentRuntimeArn": "arn:rt-9",
        "agentRuntimeVersion": "1", "status": "READY"}
    control.get_workload_identity.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetWorkloadIdentity")
    monkeypatch.setattr(zr, "control_client", lambda _ws=None: control)
    monkeypatch.setattr(return_url, "RETURN_URL_BACKOFF_S", 0.0)
    monkeypatch.setattr(zr, "get_settings", lambda: SimpleNamespace(
        account_id="1", region="us-west-2", resources={}))
    logs: list[str] = []
    ctx = StageContext(agent_id=agent.id, deployment_id="d", job_id="j",
                       workspace=ws_ctx({"artifacts_bucket": "b", "execution_role_arn": "r"}))
    ctx.log = logs.append
    result = zr._stage_deploy(ctx, agent)
    assert result.detail == f"READY · arn:rt-9 · {return_url.RETURN_URL_WARNING}"
    assert any(line.startswith("WARNING: return-URL") for line in logs)
    control.create_agent_runtime.assert_called_once()


# ── generated code: the non-blocking USER_FEDERATION exchange ────────────────


class _Ctx:
    token = "workload-token"

    @classmethod
    def get_workload_access_token(cls):
        return cls.token


def _identity_block(monkeypatch, response: dict):
    """Exec the rendered identity block with a stub Identity data plane."""
    monkeypatch.setenv("LAUNCHPAD_OAUTH_RETURN_URL", "https://console.example/auth/return")
    monkeypatch.setenv("LAUNCHPAD_AGENT_ID", "a1")
    source = identity_support.render_identity_source(AgentSpec(**AS_USER_SPEC))
    namespace: dict = {"os": os, "BedrockAgentCoreContext": _Ctx}
    exec(compile(source, "identity_block", "exec"), namespace)  # noqa: S102
    dp = MagicMock()
    dp.get_resource_oauth2_token.return_value = response
    namespace["_id_client"] = lambda: MagicMock(dp_client=dp)
    return namespace, dp


def test_generated_as_user_exchange_queues_a_notice_instead_of_blocking(monkeypatch):
    ns, dp = _identity_block(monkeypatch, {"authorizationUrl": AUTH_URL, "sessionUri": SESSION})
    ns["_IDENTITY_SESSION"]["id"] = "chat-session-1"
    spec = ns["IDENTITY_AUTH_TOOLS"][0]
    assert spec["mode"] == "as_user"

    answer = json.loads(ns["_rest_call"](spec, "", ""))
    assert answer["auth_required"] is True and AUTH_URL not in json.dumps(answer)
    kwargs = dp.get_resource_oauth2_token.call_args.kwargs
    assert kwargs["oauth2Flow"] == "USER_FEDERATION"
    assert kwargs["resourceOauth2ReturnUrl"] == "https://console.example/auth/return"
    assert json.loads(kwargs["customState"]) == {
        "agent_id": "a1", "tool": "userinfo", "session_id": "chat-session-1",
    }
    assert "forceAuthentication" not in kwargs
    assert ns["identity_drain_auth_notices"]() == [{
        "provider": "team-idp", "tool": "userinfo", "url": AUTH_URL,
        "scopes": ["openid", "profile"], "session_uri": SESSION,
    }]
    assert ns["identity_drain_auth_notices"]() == []


def test_generated_as_user_exchange_uses_a_vaulted_token_and_honors_revocation(monkeypatch):
    ns, dp = _identity_block(monkeypatch, {"accessToken": "user-token"})
    ns["identity_tools"](MagicMock(), "sess", ["team-idp"])
    spec = ns["IDENTITY_AUTH_TOOLS"][0]
    assert ns["identity_credential"](spec) == ({"Authorization": "Bearer user-token"}, {})
    assert dp.get_resource_oauth2_token.call_args.kwargs["forceAuthentication"] is True
    assert ns["identity_drain_auth_notices"]() == []


def test_rendered_main_with_an_as_user_tool_compiles_and_drains_notices():
    source = render_main_py(AgentSpec(**AS_USER_SPEC))
    ast.parse(source)
    assert "IDENTITY_AUTH_NOTICES = identity_drain_auth_notices" in source
    assert 'force_reauth = payload.get("force_reauth_providers") or []' in source
    assert "'mode': 'as_user'" in source


class _Recorder:
    """A loopback HTTP server that answers every request with one fixed reply
    and records the Authorization header it saw."""

    def __init__(self, status: int, headers: dict | None = None, body: bytes = b""):
        import http.server
        import threading

        seen: list = []
        self.seen = seen

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — http.server's hook name
                seen.append(self.headers.get("Authorization"))
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_generated_rest_call_refuses_a_redirect_and_never_forwards_the_credential(monkeypatch):
    ns, _dp = _identity_block(monkeypatch, {"accessToken": "user-token"})
    thief = _Recorder(200, body=b"{}")
    api = _Recorder(302, headers={"Location": f"{thief.url}/steal?x=1"})
    try:
        spec = {**ns["IDENTITY_AUTH_TOOLS"][0], "url": f"{api.url}/userinfo"}
        answer = ns["_rest_call"](spec, "", "")
    finally:
        api.close()
        thief.close()
    assert api.seen == ["Bearer user-token"]
    assert thief.seen == []  # the 302 was not followed
    assert answer.startswith("error: the API answered HTTP 302")
    assert "redirects are not followed" in answer
    assert "x=1" not in answer and "user-token" not in answer


def test_generated_mcp_client_refuses_a_cross_origin_redirect(monkeypatch):
    import asyncio

    import httpx

    ns, _dp = _identity_block(monkeypatch, {"accessToken": "user-token"})
    thief = _Recorder(200, body=b"{}")
    api = _Recorder(307, headers={"Location": f"{thief.url}/mcp/"})
    factory = ns["_id_same_origin_mcp_factory"](f"{api.url}/mcp")

    async def call():
        async with factory(headers={"Authorization": "Bearer user-token"}) as client:
            await client.get(f"{api.url}/mcp")

    try:
        with pytest.raises(httpx.RequestError, match="cross-origin redirect"):
            asyncio.run(call())
    finally:
        api.close()
        thief.close()
    assert api.seen == ["Bearer user-token"]
    assert thief.seen == []  # the credential never left the configured origin


def test_generated_mcp_client_still_follows_a_same_origin_redirect(monkeypatch):
    import asyncio
    import http.server
    import threading

    ns, _dp = _identity_block(monkeypatch, {"accessToken": "user-token"})
    seen: list = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server's hook name
            seen.append(self.path)
            if self.path == "/mcp":  # the common `/mcp` → `/mcp/` hop
                self.send_response(307)
                self.send_header("Location", "/mcp/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
    factory = ns["_id_same_origin_mcp_factory"](url)

    async def call():
        async with factory(headers={"Authorization": "Bearer user-token"}) as client:
            return (await client.get(url)).status_code

    try:
        assert asyncio.run(call()) == 200
    finally:
        server.shutdown()
        server.server_close()
    assert seen == ["/mcp", "/mcp/"]


def test_generated_rest_call_still_returns_a_plain_answer(monkeypatch):
    ns, _dp = _identity_block(monkeypatch, {"accessToken": "user-token"})
    api = _Recorder(200, headers={"Content-Type": "application/json"}, body=b'{"sub": "u1"}')
    try:
        spec = {**ns["IDENTITY_AUTH_TOOLS"][0], "url": f"{api.url}/userinfo"}
        answer = ns["_rest_call"](spec, "", "")
    finally:
        api.close()
    assert api.seen == ["Bearer user-token"]
    assert answer.startswith("HTTP 200") and '"sub": "u1"' in answer


@pytest.mark.parametrize(("run_mode", "urls", "warns"), [
    ("prod", {}, True),  # the dev default http://localhost:5173
    ("prod", {"public_base_url": "http://127.0.0.1:8080"}, True),
    ("prod", {"public_base_url": "https://console.example"}, False),
    ("prod", {"oauth_return_url": "https://proxy.example/lp/auth/return"}, False),
    ("dev", {}, False),
])
def test_prod_startup_warns_on_a_localhost_oauth_return_url(run_mode, urls, warns):
    from app.core.config import Settings
    from app.main import _warn_local_return_url

    # init kwargs outrank yaml + env, so the host's own config cannot leak in
    settings = Settings(run_mode=run_mode, **{
        "public_base_url": "http://localhost:5173", "oauth_return_url": "", **urls})
    assert _warn_local_return_url(settings) is warns
