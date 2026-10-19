"""The COA token broker (``context_layer_tokens``) and the member-facing routes it
backs: consent, revocation forcing re-auth, the cache, the /v1 refusal, COA error
mapping, and that no token ever reaches a log record or a response."""

import logging
import time

import httpx
import pytest

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.ledger import OauthPendingSession, UserGrant
from app.services import context_layer_api, oauth_sessions
from app.services import context_layer_tokens as tokens
from app.services.workspace import context_for_workspace

from . import coa_fakes as fx


@pytest.fixture
def account(monkeypatch):
    account = fx.install(monkeypatch)
    fx.connect_default(account)
    account.data.calls.clear()
    return account


def _token(username="alice", **kwargs):
    db = SessionLocal()
    try:
        return tokens.user_token(db, context_for_workspace("default"), username, **kwargs)
    finally:
        db.close()


def _grant(username="alice") -> UserGrant | None:
    db = SessionLocal()
    try:
        return db.query(UserGrant).filter(UserGrant.user_id == username).first()
    finally:
        db.close()


def test_a_vaulted_token_is_returned_and_fetched_for_the_console_user(account):
    account.data.token = fx.jwt(time.time() + 3600, sub="u-1")
    grant = _token()
    assert isinstance(grant, tokens.Token)
    assert grant.value == account.data.token
    assert account.data.ops("get_workload_access_token_for_user_id") == [
        {"workloadName": "launchpad-console-default", "userId": "alice"}
    ]
    request = account.data.ops("get_resource_oauth2_token")[0]
    assert request["oauth2Flow"] == "USER_FEDERATION"
    assert request["resourceCredentialProviderName"] == "coa-coa-w2"
    assert request["scopes"] == ["openid", "email", "profile"]
    assert request["workloadIdentityToken"] == "wat-alice"
    assert "forceAuthentication" not in request
    # the vault lives in the workspace region, never the COA one
    assert account.regions_of("bedrock-agentcore") == {fx.WS_REGION}
    assert account.data.token not in repr(grant)


def test_no_token_records_a_console_consent_and_returns_the_url(account):
    grant = _token()
    assert isinstance(grant, tokens.AuthNeeded)
    assert grant.url.startswith("https://coa-auth.example/")
    db = SessionLocal()
    try:
        pending = db.query(OauthPendingSession).one()
    finally:
        db.close()
    assert (pending.agent_id, pending.tool, pending.user_id, pending.provider) == (
        "", "context_layer", "alice", "coa-coa-w2",
    )
    assert _grant().status == "pending"
    state = account.data.ops("get_resource_oauth2_token")[0]["customState"]
    assert '"kind": "context_layer"' in state


def test_a_chat_started_consent_returns_to_that_chat(account):
    _token(agent_id="agent-1", session_id="s" * 40)
    state = account.data.ops("get_resource_oauth2_token")[0]["customState"]
    assert '"agent_id": "agent-1"' in state and '"session_id": "' + "s" * 40 in state


def test_completing_the_consent_authorizes_the_console_grant(account):
    _token()
    db = SessionLocal()
    try:
        oauth_sessions.complete(db, "default", "urn:session:abc", caller="alice",
                                data_client=account.data)
    finally:
        db.close()
    assert _grant().status == "authorized"
    assert account.data.ops("complete_resource_token_auth")[0]["userIdentifier"] == {
        "userId": "alice"
    }


def test_the_cache_serves_until_two_minutes_before_expiry(account):
    now = [1_000_000.0]
    account.data.token = fx.jwt(now[0] + 600)
    assert isinstance(_token(now=lambda: now[0]), tokens.Token)
    assert isinstance(_token(now=lambda: now[0]), tokens.Token)
    assert len(account.data.ops("get_resource_oauth2_token")) == 1  # second was a hit
    now[0] += 600 - tokens.EXPIRY_MARGIN_S + 1
    _token(now=lambda: now[0])
    assert len(account.data.ops("get_resource_oauth2_token")) == 2


def test_a_revocation_forces_authentication_even_over_a_cached_token(account):
    account.data.token = fx.jwt(time.time() + 3600)
    assert isinstance(_token(), tokens.Token)
    db = SessionLocal()
    try:
        oauth_sessions.revoke(db, "default", user_id="alice", provider="coa-coa-w2")
    finally:
        db.close()
    grant = _token()
    assert isinstance(grant, tokens.AuthNeeded)
    assert account.data.ops("get_resource_oauth2_token")[-1]["forceAuthentication"] is True


def test_a_coa_401_forces_consent_until_the_member_consents_again(account):
    account.data.token = fx.jwt(time.time() + 3600)
    assert isinstance(_token(), tokens.Token)
    tokens.invalidate("default", "alice")
    assert isinstance(_token(), tokens.AuthNeeded)
    assert isinstance(_token(), tokens.AuthNeeded)  # still forced: no new consent yet
    db = SessionLocal()
    try:
        oauth_sessions.complete(db, "default", "urn:session:abc", caller="alice",
                                data_client=account.data)
    finally:
        db.close()
    assert isinstance(_token(), tokens.Token)
    assert "forceAuthentication" not in account.data.ops("get_resource_oauth2_token")[-1]


def test_a_non_console_caller_is_refused_before_any_vault_call(account):
    for kwargs in ({"console_user": False}, {}):
        with pytest.raises(AppError) as exc:
            _token(username=None if not kwargs else "alice", **kwargs)
        assert exc.value.code == oauth_sessions.AS_USER_REQUIRES_CONSOLE
        assert exc.value.status_code == 409
    assert account.data.calls == []


def test_nothing_connected_is_refused(monkeypatch):
    fx.install(monkeypatch)
    with pytest.raises(AppError) as exc:
        _token()
    assert exc.value.code == "context_layer.not_connected"


def test_a_vault_failure_names_the_code_only(account):
    def boom(**kwargs):
        raise fx.client_error("AccessDeniedException", "role x may not")

    account.data.get_workload_access_token_for_user_id = boom
    with pytest.raises(AppError) as exc:
        _token()
    assert exc.value.code == "context_layer.identity_failed"
    assert exc.value.detail == {"step": "workload token", "code": "AccessDeniedException"}


def test_no_token_reaches_a_log_record(account, caplog):
    secret = fx.jwt(time.time() + 3600, sub="secret-sub")
    account.data.token = secret
    caplog.set_level(logging.DEBUG)
    grant = _token()
    tokens.invalidate("default", "alice")
    _token()
    assert isinstance(grant, tokens.Token)
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert secret not in repr(record.args)


# ── routes ───────────────────────────────────────────────────────────────────


def test_authorize_returns_the_url_or_authorized(client, account):
    first = client.post("/api/context-layer/authorize")
    assert first.status_code == 200
    assert first.json() == {
        "authorized": False,
        "authorization_url": "https://coa-auth.example/authorize?state=x",
        "connection": "coa-coa-w2",
    }
    account.data.token = fx.jwt(time.time() + 3600)
    second = client.post("/api/context-layer/authorize")
    assert second.json() == {"authorized": True, "connection": "coa-coa-w2"}
    assert account.data.token not in second.text
    status = client.get("/api/context-layer").json()
    assert status["authorization"]["status"] == "pending"


def test_revoke_route_forces_the_next_fetch_and_clears_the_cache(client, account):
    account.data.token = fx.jwt(time.time() + 3600)
    assert client.post("/api/context-layer/authorize").json()["authorized"] is True
    revoked = client.post("/api/context-layer/revoke")
    assert revoked.status_code == 200 and revoked.json()["revoked"] is True
    again = client.post("/api/context-layer/authorize").json()
    assert again["authorized"] is False
    assert account.data.ops("get_resource_oauth2_token")[-1]["forceAuthentication"] is True


def test_the_my_connections_revoke_also_clears_the_cache(client, account):
    account.data.token = fx.jwt(time.time() + 3600)
    client.post("/api/context-layer/authorize")
    assert ("default", "admin") in tokens._CACHE
    assert client.delete("/api/identity/grants/coa-coa-w2").status_code == 200
    assert ("default", "admin") not in tokens._CACHE


def _coa(monkeypatch, handler):
    monkeypatch.setattr(
        context_layer_api, "_transport", lambda: httpx.MockTransport(handler)
    )


def test_namespaces_are_read_as_the_member(client, account, monkeypatch):
    account.data.token = fx.jwt(time.time() + 3600)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "nextToken" not in request.url.params:
            return httpx.Response(200, json={
                "namespaces": [{"namespaceId": fx.NAMESPACE, "name": "anycompany-hr",
                                "displayName": "AnyCompany HR", "status": "ACTIVE"}],
                "nextToken": "p2",
            })
        return httpx.Response(200, json={"namespaces": []})

    _coa(monkeypatch, handler)
    response = client.get("/api/context-layer/namespaces")
    assert response.status_code == 200, response.text
    assert response.json()["namespaces"][0]["namespace_id"] == fx.NAMESPACE
    assert response.json()["namespaces"][0]["display_name"] == "AnyCompany HR"
    assert str(seen[0].url).startswith(f"{fx.API_ENDPOINT}namespaces")
    assert seen[0].headers["authorization"] == f"Bearer {account.data.token}"
    assert len(seen) == 2


def test_namespaces_need_consent_first(client, account, monkeypatch):
    _coa(monkeypatch, lambda request: pytest.fail("COA must not be called"))
    response = client.get("/api/context-layer/namespaces")
    assert response.status_code == 409
    assert response.json()["code"] == "context_layer.authorization_required"
    assert response.json()["detail"]["authorization_url"].startswith("https://coa-auth.example/")


@pytest.mark.parametrize("status,code,http", [
    (403, "context_layer.forbidden", 403),
    (401, "context_layer.reauthorize", 409),
    (503, "context_layer.upstream", 502),
])
def test_coa_errors_map_to_named_codes(client, account, monkeypatch, status, code, http):
    account.data.token = fx.jwt(time.time() + 3600)
    _coa(monkeypatch, lambda request: httpx.Response(status, json={"message": "nope"}))
    response = client.get("/api/context-layer/namespaces")
    assert response.status_code == http
    assert response.json()["code"] == code
    assert account.data.token not in response.text
    if status == 401:
        # the broker dropped the token: the next fetch forces consent
        assert ("default", "admin") in tokens._FORCE


def test_a_coa_timeout_is_upstream(client, account, monkeypatch):
    account.data.token = fx.jwt(time.time() + 3600)

    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    _coa(monkeypatch, handler)
    response = client.get("/api/context-layer/namespaces")
    assert response.status_code == 504
    assert response.json()["code"] == "context_layer.upstream"
