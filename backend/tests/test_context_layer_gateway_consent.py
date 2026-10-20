"""Consent through the managed COA gateway (``context_layer_gateway.preflight`` /
``gateway_turn_auth``): the -32042 ask recorded as a ``user_jwt`` pending session on
``gw:<gateway_id>`` + the auth-card notice, a consented member remembered, failures
failing open, the AuthCard's status mapping, revoke leaving gateway grants alone,
completion with the same cached JWT (F8), and disable forgetting it all.

Hermetic: AWS is ``coa_fakes``; the gateway is an ``httpx.MockTransport`` speaking
the live shapes measured on dev (2026-10-20).
"""

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent, OauthPendingSession, UserGrant
from app.routers import identity as identity_router
from app.services import context_layer, oauth_sessions, policy_identity
from app.services import context_layer_gateway as gw
from app.services import context_layer_tokens as tokens
from app.services.workspace import context_for_workspace

from . import coa_fakes as fx
from .conftest import set_default_resources
from .test_context_layer_gateway import RESOURCES, GatewayControl, _member

CONNECTION = "coa-coa-w2"
GATEWAY_ID = "launchpad-coa-gw-k3v9q2m7xa"
GRANT_ID = f"gw:{GATEWAY_ID}"
GATEWAY_URL = f"https://{GATEWAY_ID}.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
REF = {
    "gateway_id": GATEWAY_ID,
    "gateway_arn": f"arn:aws:bedrock-agentcore:us-west-2:{fx.ACCOUNT}:gateway/{GATEWAY_ID}",
    "gateway_url": GATEWAY_URL,
    "target_id": "TGT0000001",
}
USER_JWT = fx.jwt(time.time() + 3600, sub="ada-pool-sub", username="ada")
SESSION_URI = "urn:ietf:params:oauth:request_uri:Zm9vYmFyLXNlc3Npb24tMQ"
AUTH_URL = (
    "https://bedrock-agentcore.us-west-2.amazonaws.com/identities/oauth2/authorize"
    "?request_uri=urn%3Aietf%3Aparams%3Aoauth%3Arequest_uri%3AZm9vYmFyLXNlc3Npb24tMQ"
)
NO_GRANT = f"User 'f811e350-0000-4000-8000-000000000000' has no grants on namespace {fx.NAMESPACE}"
SCOPES = ["openid", "email", "profile"]


class GatewayMcp:
    """The managed gateway's MCP endpoint: initialize (2025-11-25), notifications,
    and ``tools/call coa___list_metrics`` answered -32042 until the member
    consented, then COA's result (or COA's own ``isError`` refusal)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.consented = False
        self.coa_grant = True
        self.status = 200
        self.exc: Exception | None = None
        self.version = "2025-11-25"
        self.sse = False
        self.call_error: dict[str, Any] | None = None
        self.elicitations: list[dict[str, Any]] = [{
            "mode": "url", "elicitationId": "el-1", "url": AUTH_URL,
            "message": "Authorize the COA context layer",
        }]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "gateway refused"})
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        rpc: dict[str, Any] = {"jsonrpc": "2.0", "id": body["id"]}
        if body["method"] == "initialize":
            rpc["result"] = {
                "protocolVersion": self.version, "capabilities": {"tools": {}},
                "serverInfo": {"name": "launchpad-coa-gw", "version": "1"},
            }
        elif body["method"] == "tools/call":
            if self.call_error is not None:
                rpc["error"] = self.call_error
            elif not self.consented:
                rpc["error"] = {
                    "code": -32042, "message": "URL elicitation required",
                    "data": {"elicitations": self.elicitations},
                }
            elif not self.coa_grant:
                rpc["result"] = {"content": [{"type": "text", "text": NO_GRANT}], "isError": True}
            else:
                metrics = {"metrics": [{"name": "available_annual_leave_days"}]}
                rpc["result"] = {
                    "content": [{"type": "text", "text": json.dumps(metrics)}], "isError": False,
                }
        else:
            raise AssertionError(body["method"])
        headers = {"mcp-session-id": "gw-session-1"}
        if not self.sse:
            return httpx.Response(200, headers=headers, json=rpc)
        return httpx.Response(
            200, headers={**headers, "content-type": "text/event-stream"},
            content=f"event: message\ndata: {json.dumps(rpc)}\n\n".encode(),
        )

    def methods(self) -> list[str]:
        return [json.loads(r.content).get("method") for r in self.requests]

    def bodies(self, method: str) -> list[dict[str, Any]]:
        return [b for r in self.requests if (b := json.loads(r.content)).get("method") == method]


@pytest.fixture
def gateway(monkeypatch):
    mcp = GatewayMcp()
    monkeypatch.setattr(gw, "_transport", lambda: httpx.MockTransport(mcp.handler))
    monkeypatch.setattr(gw, "_sleep", lambda s: None)
    gw.forget_consents()
    yield mcp
    gw.forget_consents()


@pytest.fixture
def minted(monkeypatch):
    """``policy_identity.gateway_user_token`` stubbed: the member's pool JWT."""
    calls: list[tuple] = []

    def mint(workspace, username, role, email=None, *, min_valid_s=60):
        calls.append((username, role, email, min_valid_s))
        return USER_JWT

    monkeypatch.setattr(policy_identity, "gateway_user_token", mint)
    return calls


@pytest.fixture
def account(monkeypatch, client, gateway):
    """A connected layer whose ledger names an enabled managed gateway."""
    account = fx.Account()
    account.control = GatewayControl()
    fx.install(monkeypatch, account)
    fx.connect_default(account)
    set_default_resources(RESOURCES)
    _set_ref(REF)
    account.control.calls.clear()
    account.data.calls.clear()
    return account


def _ws():
    return context_for_workspace("default")


def _set_ref(ref: dict | None) -> None:
    db = SessionLocal()
    try:
        gw._save_ref(db, "default", ref)
    finally:
        db.close()


def _layer() -> context_layer.ContextLayer:
    return context_layer.require_connected("default")


def _agent(via: str | None = "gateway", method: str = "harness", name: str = "hr-gw") -> Agent:
    ref: dict[str, Any] = {"namespace_id": fx.NAMESPACE, "namespace_name": "AnyCompany HR"}
    if via:
        ref["via"] = via
    db = SessionLocal()
    try:
        row = Agent(workspace_id="default", name=name, method=method, status="active",
                    spec={"name": name, "method": method, "context_layer": ref})
        db.add(row)
        db.commit()
        db.refresh(row)
        db.expunge(row)
        return row
    finally:
        db.close()


def _gate(agent: Agent, username: str | None = "ada", **kwargs):
    return gw.gateway_turn_auth(
        agent, _ws(), username=username, role="member", email="ada@acme-corp.com",
        session_id="chat-session-1", **kwargs,
    )


def _pending() -> list[OauthPendingSession]:
    db = SessionLocal()
    try:
        return db.query(OauthPendingSession).all()
    finally:
        db.close()


def _grants() -> dict[tuple[str, str], UserGrant]:
    db = SessionLocal()
    try:
        return {(g.user_id, g.agent_id): g for g in db.query(UserGrant).all()}
    finally:
        db.close()


def _add_grant(user: str, agent_id: str, status: str = "authorized", pending: bool = False):
    db = SessionLocal()
    try:
        db.add(UserGrant(workspace_id="default", user_id=user, provider=CONNECTION,
                         agent_id=agent_id, tool="context_layer", status=status,
                         authorized_at=datetime.now(UTC) if status == "authorized" else None))
        if pending:
            db.add(OauthPendingSession(
                workspace_id="default", session_uri=f"urn:pending:{agent_id or 'console'}",
                provider=CONNECTION, user_id=user, agent_id=agent_id, tool="context_layer",
                caller_kind="user_jwt" if agent_id.startswith("gw:") else "iam",
            ))
        db.commit()
    finally:
        db.close()


# ── the grant key ────────────────────────────────────────────────────────────


def test_the_gateway_grant_id_fits_the_ledger_columns():
    assert gw.gateway_grant_id(GATEWAY_ID) == GRANT_ID
    width = UserGrant.__table__.c.agent_id.type.length
    assert width == OauthPendingSession.__table__.c.agent_id.type.length == 32
    # the gateway's name is fixed: AWS appends "-" + 10 characters to it
    assert len(gw.gateway_grant_id(f"{gw.GATEWAY_NAME}-{'x' * 10}")) <= width
    assert oauth_sessions.is_gateway_grant(GRANT_ID)
    assert not oauth_sessions.is_gateway_grant("")
    assert not oauth_sessions.is_gateway_grant("a1b2c3")


# ── preflight: no consent → the auth card ────────────────────────────────────


def test_a_missing_consent_records_a_user_jwt_ask_and_ends_at_the_card(
    account, gateway, minted
):
    agent = _agent()
    out = _gate(agent)
    notice = {
        "provider": CONNECTION, "tool": "context_layer", "url": AUTH_URL,
        "scopes": SCOPES, "agent_id": agent.id,
    }
    assert out == {"auth_required": notice}
    # the direct-mode notice shape, so the Chat card / demo offer / return page agree
    direct = tokens.auth_notice(tokens.AuthNeeded(url=AUTH_URL, layer=_layer()), agent.id)
    assert notice == direct
    (row,) = _pending()
    assert (row.session_uri, row.provider, row.user_id, row.agent_id, row.tool,
            row.caller_kind) == (SESSION_URI, CONNECTION, "ada", GRANT_ID, "context_layer",
                                 "user_jwt")
    grant = _grants()[("ada", GRANT_ID)]
    assert grant.status == "pending" and grant.scopes == SCOPES
    # the member's JWT was minted for exactly this member, and outlives the consent
    # session (the completion must find this very token in the cache, F8)
    assert minted == [("ada", "member", "ada@acme-corp.com", gw.CONSENT_JWT_MIN_VALID_S)]
    assert gw.CONSENT_JWT_MIN_VALID_S > oauth_sessions.SESSION_TTL_S
    # gateway mode: the backend fetched no COA token (no vault call at all)
    assert account.data.calls == []


def test_the_preflight_speaks_mcp_2025_11_25_with_the_members_jwt(account, gateway, minted):
    _gate(_agent())
    assert gateway.methods() == ["initialize", "notifications/initialized", "tools/call"]
    for request in gateway.requests:
        assert str(request.url) == GATEWAY_URL
        assert request.headers["authorization"] == f"Bearer {USER_JWT}"
        assert request.headers["mcp-protocol-version"] == "2025-11-25"
    (init,) = gateway.bodies("initialize")
    assert init["params"]["protocolVersion"] == "2025-11-25"
    assert init["params"]["capabilities"] == {"elicitation": {"url": {}}}
    (call,) = gateway.bodies("tools/call")
    assert call["params"] == {
        "name": "coa___list_metrics", "arguments": {"namespaceId": fx.NAMESPACE},
    }
    assert "mcp-session-id" not in gateway.requests[0].headers
    assert {r.headers["mcp-session-id"] for r in gateway.requests[1:]} == {"gw-session-1"}


def test_an_unconsented_member_is_asked_again_on_the_next_turn(account, gateway, minted):
    agent = _agent()
    assert _gate(agent) is not None
    assert _gate(agent) is not None  # never cached
    assert gateway.methods().count("tools/call") == 2
    (row,) = _pending()  # the same session is recorded once
    assert row.session_uri == SESSION_URI


def test_an_sse_answer_is_read_too(account, gateway, minted):
    gateway.sse = True
    assert _gate(_agent())["auth_required"]["url"] == AUTH_URL


# ── preflight: consented → the turn runs ─────────────────────────────────────


@pytest.mark.parametrize("coa_grant", [True, False], ids=["data", "coa-refusal"])
def test_a_consented_member_runs_and_is_remembered(account, gateway, minted, coa_grant):
    """COA's own refusal (isError: no grant on the namespace) still means the gateway
    holds the consent: the agent answers it, not the card."""
    gateway.consented, gateway.coa_grant = True, coa_grant
    agent = _agent()
    assert _gate(agent) is None
    assert _gate(agent) is None
    assert gateway.methods().count("tools/call") == 1  # the second turn asked nobody
    assert _pending() == [] and _grants() == {}


def test_the_remembered_consent_expires(account, gateway, minted, monkeypatch):
    gateway.consented = True
    now = [1000.0]
    monkeypatch.setattr(gw, "_clock", lambda: now[0])
    agent = _agent()
    assert _gate(agent) is None
    now[0] += gw.CONSENT_TTL_S - 1
    assert _gate(agent) is None
    assert gateway.methods().count("tools/call") == 1
    now[0] += 2
    gateway.consented = False  # e.g. COA's refresh token expired meanwhile
    assert _gate(agent) is not None
    assert gateway.methods().count("tools/call") == 2


def test_the_remembered_consent_is_per_member(account, gateway, minted):
    gateway.consented = True
    agent = _agent()
    assert _gate(agent, "ada") is None
    assert _gate(agent, "bob") is None
    assert gateway.methods().count("tools/call") == 2


# ── preflight: anything else fails open ──────────────────────────────────────


FAIL_OPEN = {
    "connect-error": {"exc": httpx.ConnectError("refused")},
    "timeout": {"exc": httpx.ReadTimeout("slow")},
    "http-502": {"status": 502},
    "http-503": {"status": 503},
    "http-401": {"status": 401},
    "http-403": {"status": 403},
    "rpc-error": {"call_error": {"code": -32603, "message": "internal error"}},
    "old-protocol": {"version": "2025-03-26"},
    "no-url": {"elicitations": [{"mode": "form", "message": "x"}]},
    "not-https": {"elicitations": [{"mode": "url", "url": "http://x.example/?request_uri=u"}]},
    "no-request-uri": {"elicitations": [{"mode": "url", "url": "https://x.example/authorize"}]},
}


@pytest.mark.parametrize("failure", FAIL_OPEN.values(), ids=FAIL_OPEN.keys())
def test_a_failed_preflight_fails_open_without_caching(
    account, gateway, minted, caplog, failure
):
    for key, value in failure.items():
        setattr(gateway, key, value)
    agent = _agent()
    with caplog.at_level(logging.WARNING, logger="launchpad.context_layer.gateway"):
        assert _gate(agent) is None
    assert any("failed open" in r.getMessage() for r in caplog.records)
    assert _pending() == [] and _grants() == {}
    calls_before = len(gateway.requests)
    assert _gate(agent) is None
    assert len(gateway.requests) > calls_before  # not remembered as consented


def test_no_jwt_or_authorization_url_reaches_a_log_record(account, gateway, minted, caplog):
    agent = _agent()
    with caplog.at_level(logging.DEBUG):
        _gate(agent)  # the ask
        gateway.status = 503
        _gate(agent)  # fail open
        gateway.status, gateway.consented = 200, True
        _gate(agent)  # consented
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert USER_JWT not in logged
    assert "request_uri" not in logged and SESSION_URI not in logged


# ── the gate around it ───────────────────────────────────────────────────────


def test_a_direct_agent_is_not_gated_here(account, gateway, minted):
    assert _gate(_agent(via=None)) is None
    assert minted == [] and gateway.requests == []


@pytest.mark.parametrize("kwargs", [
    {"username": None}, {"username": ""}, {"console_user": False},
], ids=["no-user", "empty-user", "v1"])
def test_no_console_user_is_refused_before_any_call(account, gateway, minted, kwargs):
    agent = _agent()
    username = kwargs.pop("username", "ada")
    with pytest.raises(AppError) as exc:
        _gate(agent, username, **kwargs)
    assert (exc.value.code, exc.value.status_code) == ("identity.as_user_requires_console", 409)
    assert exc.value.detail == {
        "provider": CONNECTION, "tool": "context_layer", "agent_id": agent.id,
    }
    assert minted == [] and gateway.requests == [] and _pending() == []


def test_the_console_sign_in_off_is_refused_like_direct_mode(account, gateway, monkeypatch):
    """No password configured ⇒ no pool JWT: nobody the gateway could ask."""
    monkeypatch.setattr(policy_identity, "gateway_user_token", lambda *a, **k: None)
    with pytest.raises(AppError) as exc:
        _gate(_agent())
    assert exc.value.code == "identity.as_user_requires_console"
    assert gateway.requests == []


def test_no_managed_gateway_is_refused_by_name(account, gateway, minted):
    _set_ref(None)
    with pytest.raises(AppError) as exc:
        _gate(_agent())
    assert (exc.value.code, exc.value.status_code) == ("context_layer.gateway_not_enabled", 409)
    assert minted == [] and gateway.requests == []


def test_a_non_https_gateway_url_is_never_sent_the_jwt(account, gateway, minted):
    _set_ref({**REF, "gateway_url": "http://plain.example/mcp"})
    with pytest.raises(AppError) as exc:
        _gate(_agent())
    assert exc.value.code == "context_layer.gateway_not_enabled"
    assert gateway.requests == []


# ── the AuthCard's poll: grant_agent_id ──────────────────────────────────────


def _grant_agent(agent_id: str, provider: str = CONNECTION, workspace: str = "default") -> str:
    db = SessionLocal()
    try:
        return context_layer.grant_agent_id(db, workspace, agent_id, provider)
    finally:
        db.close()


def test_gateway_mode_agents_poll_the_gateway_grant(account):
    harness_gw = _agent("gateway", "harness", "h-gw")
    strands_gw = _agent("gateway", "zip_runtime", "s-gw")
    harness_direct = _agent(None, "harness", "h-direct")
    strands_direct = _agent(None, "zip_runtime", "s-direct")
    assert _grant_agent(harness_gw.id) == GRANT_ID
    assert _grant_agent(strands_gw.id) == GRANT_ID
    # direct mode is unchanged: the console grant for a Harness, its own for Strands
    assert _grant_agent(harness_direct.id) == ""
    assert _grant_agent(strands_direct.id) == strands_direct.id
    # another Connection, another workspace, no agent: what they say
    assert _grant_agent(harness_gw.id, provider="other-idp") == harness_gw.id
    assert _grant_agent(harness_gw.id, workspace="other") == harness_gw.id
    assert _grant_agent("") == ""
    # no managed gateway: nothing was asked for it
    _set_ref(None)
    assert _grant_agent(harness_gw.id) == harness_gw.id


def test_the_status_route_reports_the_gateway_grant(client, account, gateway, minted):
    operator = get_settings().auth_username
    agent = _agent()
    _gate(agent, operator)
    status = client.get(f"/api/identity/grants/{CONNECTION}/status?agent_id={agent.id}")
    assert status.status_code == 200
    assert status.json() == {
        "connection": CONNECTION, "agent_id": agent.id, "status": "pending",
        "force_reauth": False, "authorized_at": None,
    }


# ── revoke leaves gateway grants alone ───────────────────────────────────────


def test_revoke_skips_gateway_grants_and_their_pending_sessions(account):
    _add_grant("ada", "", pending=True)            # the console grant (direct)
    _add_grant("ada", "strands-agent-1")           # a direct Strands agent
    _add_grant("ada", GRANT_ID, pending=True)       # through the managed gateway
    db = SessionLocal()
    try:
        out = oauth_sessions.revoke(db, "default", user_id="ada", provider=CONNECTION)
        listed = {g["agent_id"]: g for g in oauth_sessions.list_grants(
            db, "default", user_id="ada"
        )}
        polled = oauth_sessions.grant_status(
            db, "default", user_id="ada", provider=CONNECTION, agent_id=GRANT_ID
        )
    finally:
        db.close()
    assert out == {"revoked": True, "provider": CONNECTION, "agents": 2}
    grants = _grants()
    assert grants[("ada", "")].status == "revoked"
    assert grants[("ada", "strands-agent-1")].status == "revoked"
    assert grants[("ada", GRANT_ID)].status == "authorized"
    assert grants[("ada", GRANT_ID)].revoked_at is None
    assert [s.agent_id for s in _pending()] == [GRANT_ID]
    # the revocation forces the direct paths only
    assert listed[""]["force_reauth"] is True
    assert listed["strands-agent-1"]["force_reauth"] is True
    assert listed[GRANT_ID]["force_reauth"] is False
    assert polled["status"] == "authorized" and polled["force_reauth"] is False


def test_an_unasked_gateway_grant_is_never_reported_as_owing_re_auth(account):
    db = SessionLocal()
    try:
        oauth_sessions.revoke(db, "default", user_id="ada", provider=CONNECTION)
        polled = oauth_sessions.grant_status(
            db, "default", user_id="ada", provider=CONNECTION, agent_id=GRANT_ID
        )
        console = oauth_sessions.grant_status(
            db, "default", user_id="ada", provider=CONNECTION, agent_id=""
        )
    finally:
        db.close()
    assert polled["status"] == "none" and polled["force_reauth"] is False
    assert console["force_reauth"] is True


@pytest.mark.parametrize("route", ["identity", "context-layer"])
def test_both_revoke_routes_skip_gateway_grants(client, account, route):
    operator = get_settings().auth_username
    _add_grant(operator, "")
    _add_grant(operator, GRANT_ID)
    response = (
        client.delete(f"/api/identity/grants/{CONNECTION}") if route == "identity"
        else client.post("/api/context-layer/revoke")
    )
    assert response.status_code == 200, response.text
    grants = _grants()
    assert grants[(operator, "")].status == "revoked"
    assert grants[(operator, GRANT_ID)].status == "authorized"
    listed = client.get("/api/identity/grants").json()["grants"]
    row = next(g for g in listed if g["agent_id"] == GRANT_ID)
    assert row["agent_name"] is None and row["status"] == "authorized"
    assert row["force_reauth"] is False


# ── completion: the same JWT the gateway saw ─────────────────────────────────


def test_completion_binds_the_gateway_consent_with_the_cached_jwt(
    client, account, gateway, minted, monkeypatch
):
    data = MagicMock()
    monkeypatch.setattr(identity_router, "data_client", lambda _ctx: data)
    operator = get_settings().auth_username
    agent = _agent()
    assert _gate(agent, operator) is not None
    done = client.post("/api/identity/oauth/complete", json={"session_id": SESSION_URI})
    assert done.status_code == 200, done.text
    assert done.json() == {
        "completed": True, "provider": CONNECTION, "agent_id": GRANT_ID,
        "tool": "context_layer", "agent_name": None,
    }
    data.complete_resource_token_auth.assert_called_once_with(
        userIdentifier={"userToken": USER_JWT}, sessionUri=SESSION_URI
    )
    grant = _grants()[(operator, GRANT_ID)]
    assert grant.status == "authorized" and grant.authorized_at is not None
    assert _pending() == []
    status = client.get(f"/api/identity/grants/{CONNECTION}/status?agent_id={agent.id}")
    assert status.json()["status"] == "authorized"
    assert status.json()["force_reauth"] is False
    # the member retries: the gateway now holds the consent and the turn runs
    gateway.consented = True
    assert _gate(agent, operator) is None


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", "operator")
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "s3cret-pass")
    get_settings.cache_clear()
    monkeypatch.setattr(policy_identity, "_token_cache", {})
    yield
    get_settings.cache_clear()


def test_the_turn_and_the_return_page_present_one_and_the_same_jwt(
    gated, account, gateway, monkeypatch
):
    """F8 end to end in one process: the gate mints the member's JWT through
    ``policy_identity``'s cache, and ``/auth/return``'s completion reads the same
    token back from it (a fresh token of the same subject would be refused)."""
    from app.main import create_app

    app = create_app()
    set_default_resources(RESOURCES)  # init_db mirrored settings onto the row
    _set_ref(REF)
    member = _member(app)  # "ada", role member, signed in
    ws = _ws()
    seeded = fx.jwt(time.time() + 3600, sub="ada-pool-sub", username="ada")
    policy_identity._token_cache[(ws.account_id, ws.region, "ada", "member")] = {
        "token": seeded, "expires_at": time.time() + 3600,
    }
    data = MagicMock()
    monkeypatch.setattr(identity_router, "data_client", lambda _ctx: data)
    agent = _agent()
    assert _gate(agent, "ada") is not None
    assert {r.headers["authorization"] for r in gateway.requests} == {f"Bearer {seeded}"}
    done = member.post("/api/identity/oauth/complete", json={"session_id": SESSION_URI})
    assert done.status_code == 200, done.text
    data.complete_resource_token_auth.assert_called_once_with(
        userIdentifier={"userToken": seeded}, sessionUri=SESSION_URI
    )
    assert _grants()[("ada", GRANT_ID)].status == "authorized"


def test_another_member_cannot_complete_the_ask(client, account, gateway, minted, monkeypatch):
    data = MagicMock()
    monkeypatch.setattr(identity_router, "data_client", lambda _ctx: data)
    _gate(_agent(), "someone-else")
    response = client.post("/api/identity/oauth/complete", json={"session_id": SESSION_URI})
    assert response.status_code == 403
    assert response.json()["code"] == "identity.session_user_mismatch"
    data.complete_resource_token_auth.assert_not_called()


# ── disable forgets it all ───────────────────────────────────────────────────


def test_disable_drops_the_gateway_consents_and_forgets_the_remembered_ones(
    account, gateway, minted
):
    account.control.add_gateway(GATEWAY_ID)
    gateway.consented = True
    assert _gate(_agent(name="first"), "ada") is None  # remembered
    _add_grant("ada", GRANT_ID, pending=True)
    _add_grant("ada", "")  # the console grant stays
    db = SessionLocal()
    try:
        db.query(Agent).delete()  # disable is refused while an agent uses gateway mode
        db.commit()
    finally:
        db.close()
    db = SessionLocal()
    try:
        out = gw.disable(db, _ws())
    finally:
        db.close()
    assert out == {"disabled": True, "gateway_id": GATEWAY_ID, "grants_removed": 1}
    assert list(_grants()) == [("ada", "")]
    assert _pending() == []
    assert gw._CONSENTED == {}
