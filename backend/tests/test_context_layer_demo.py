"""The shared COA demo account (``context_layer_demo``): the ``/<prefix>/launchpad/``
scope guard, the parameter checks, the cache, the status fields (no password,
identity, sign-out link), the reveal and the account choice on ``authorize``.

Hermetic: AWS is ``coa_fakes``; the demo account is a fake SSM SecureString.
"""

import json
import logging
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.main import create_app
from app.models.ledger import ContextLayer as ContextLayerRow
from app.models.ledger import UserGrant
from app.services import context_layer, context_layer_demo, oauth_sessions
from app.services import context_layer_tokens as tokens
from app.services import users as users_service
from app.services.workspace import context_for_workspace

from . import coa_fakes as fx

SIGN_OUT = (
    f"{fx.HOSTED_UI}/logout?client_id={fx.WEB_CLIENT}"
    "&logout_uri=https%3A%2F%2Fd3q37s5vts6pte.cloudfront.net%2F"
)


@pytest.fixture
def account(monkeypatch):
    account = fx.install(monkeypatch)
    account.ssm.put_demo_account()
    return account


@pytest.fixture
def connected(account):
    fx.connect_default(account, demo_account_param=fx.DEMO_PARAM)
    account.ssm.calls.clear()
    account.data.calls.clear()
    return account


def _ws():
    return context_for_workspace("default")


def _layer():
    db = SessionLocal()
    try:
        return context_layer.require(db, "default")
    finally:
        db.close()


def _status(username="alice", is_admin=False):
    db = SessionLocal()
    try:
        return context_layer.status(db, _ws(), username=username, is_admin=is_admin)
    finally:
        db.close()


def _authorize_ledger(username="alice"):
    """The console grant reads ``authorized`` (a completed consent)."""
    db = SessionLocal()
    try:
        oauth_sessions.record_pending(
            db, "default", session_uri=f"urn:s:{username}", provider="coa-coa-w2",
            user_id=username, agent_id="", tool="context_layer",
        )
        grant = db.query(UserGrant).filter(UserGrant.user_id == username).one()
        grant.status = "authorized"
        grant.authorized_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()


def _demo_reads(account) -> int:
    return len([c for c in account.ssm.ops("get_parameter") if c["Name"] == fx.DEMO_PARAM])


# ── the scope guard and the parameter checks (connect) ───────────────────────


@pytest.mark.parametrize("name", [
    "/coa-w2/launchpad/demo-account",
    "/coa-w2/launchpad/team/demo",
])
def test_a_parameter_under_the_prefix_is_in_scope(name):
    assert context_layer_demo.check_scope("coa-w2", name) == name


@pytest.mark.parametrize("name", [
    "/coa-w2/launchpad/",              # the branch itself
    "/coa-w2/launchpad",
    "/coa-w2/userpool-id",             # COA's own tree, outside Launchpad's branch
    "/other/launchpad/demo-account",   # another deployment's
    "/coa-w2/launchpad/../agent-oauth-client-id",
    "/coa-w2/launchpad/a/../../agent-oauth-client-id",
    "/coa-w2/launchpad/./demo",
    "/coa-w2/launchpad//demo",
    "/coa-w2/launchpad/demo/",
    "//coa-w2/launchpad/demo",
    "/coa-w2/launchpad/demo account",
    "/coa-w2-x/launchpad/demo",
    "/COA-W2/launchpad/demo",          # SSM names are case-sensitive: another tree
    "/coa-w2/Launchpad/demo",
    "coa-w2/launchpad/demo",
    "arn:aws:ssm:us-west-2:123456789012:parameter/coa-w2/launchpad/demo",
])
def test_a_parameter_outside_the_prefix_is_refused_before_any_aws_call(account, name):
    with pytest.raises(AppError) as exc:
        context_layer_demo.check_scope("coa-w2", name)
    assert exc.value.code == "context_layer.demo_param_scope"
    assert exc.value.status_code == 422
    account.regions.clear()
    db = SessionLocal()
    try:
        with pytest.raises(AppError) as exc:
            context_layer.connect(
                db, _ws(), prefix=fx.PREFIX, env=fx.ENV, region=fx.COA_REGION,
                connection=None, return_url="http://x/auth/return", demo_account_param=name,
            )
    finally:
        db.close()
    assert exc.value.code == "context_layer.demo_param_scope"
    assert account.regions == []


@pytest.mark.parametrize("setup,problem", [
    (lambda ssm: ssm.secure.clear(), "not_found"),
    (lambda ssm: (ssm.secure.clear(), ssm.params.__setitem__(fx.DEMO_PARAM, "plain")),
     "not_secure_string"),
    (lambda ssm: ssm.secure.__setitem__(fx.DEMO_PARAM, "not json " + fx.DEMO_PASSWORD),
     "bad_shape"),
    (lambda ssm: ssm.secure.__setitem__(fx.DEMO_PARAM, json.dumps(
        {"username": fx.DEMO_USER, "password": ""})), "bad_shape"),
    (lambda ssm: ssm.secure.__setitem__(fx.DEMO_PARAM, json.dumps(
        {"username": "", "password": fx.DEMO_PASSWORD})), "bad_shape"),
    (lambda ssm: ssm.secure.__setitem__(fx.DEMO_PARAM, json.dumps(
        [fx.DEMO_USER, fx.DEMO_PASSWORD])), "bad_shape"),
])
def test_an_invalid_parameter_is_refused_by_problem_never_by_value(account, setup, problem):
    setup(account.ssm)
    with pytest.raises(AppError) as exc:
        fx.connect_default(account, demo_account_param=fx.DEMO_PARAM)
    assert exc.value.code == "context_layer.demo_param_invalid"
    assert exc.value.status_code == 409
    assert exc.value.detail == {"parameter": fx.DEMO_PARAM, "problem": problem}
    assert fx.DEMO_PASSWORD not in str(exc.value) + json.dumps(exc.value.detail)
    # no chained exception (an SSM or JSON error) rides along into a traceback
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None or exc.value.__suppress_context__
    # refused before anything was written
    assert account.control.ops("create_oauth2_credential_provider") == []
    db = SessionLocal()
    try:
        assert db.query(ContextLayerRow).count() == 0
    finally:
        db.close()


def test_the_parameter_is_read_decrypted_in_the_coa_region(account):
    fx.connect_default(account, demo_account_param=fx.DEMO_PARAM)
    assert account.ssm.ops("get_parameter") == [
        {"Name": fx.DEMO_PARAM, "WithDecryption": True}
    ]
    assert account.regions_of("ssm") == {fx.COA_REGION}


def test_the_row_keeps_the_name_only(connected):
    db = SessionLocal()
    try:
        row = db.query(ContextLayerRow).one()
    finally:
        db.close()
    assert row.resources[context_layer_demo.PARAM_KEY] == fx.DEMO_PARAM
    assert row.resources["web_client_id"] == fx.WEB_CLIENT
    columns = ContextLayerRow.__table__.columns
    stored = json.dumps({c.name: str(getattr(row, c.name)) for c in columns})
    assert fx.DEMO_PASSWORD not in stored and fx.DEMO_USER not in stored


def test_reconnecting_keeps_clears_or_replaces_the_parameter(connected):
    fx.connect_default(connected)  # omitted: kept
    assert context_layer_demo.param_of(_layer()) == fx.DEMO_PARAM
    fx.connect_default(connected, demo_account_param="")  # "": cleared
    assert context_layer_demo.param_of(_layer()) == ""
    other = f"/{fx.PREFIX}/launchpad/other"
    connected.ssm.put_demo_account(name=other)
    fx.connect_default(connected, demo_account_param=other)
    assert context_layer_demo.param_of(_layer()) == other


def test_a_kept_parameter_outside_a_new_prefix_is_dropped():
    assert context_layer_demo.in_scope("coa-w2", fx.DEMO_PARAM)
    assert not context_layer_demo.in_scope("coa-x9", fx.DEMO_PARAM)


# ── load, cache, public view ─────────────────────────────────────────────────


def test_the_account_is_cached_for_five_minutes(connected):
    now = [100.0]
    layer = _layer()
    first = context_layer_demo.load(_ws(), layer, now=lambda: now[0])
    assert first == context_layer_demo.DemoAccount(fx.DEMO_USER, fx.DEMO_PASSWORD, fx.DEMO_SUB)
    context_layer_demo.load(_ws(), layer, now=lambda: now[0])
    assert _demo_reads(connected) == 1
    now[0] += context_layer_demo.CACHE_TTL_S + 1
    context_layer_demo.load(_ws(), layer, now=lambda: now[0])
    assert _demo_reads(connected) == 2


def test_a_failed_read_is_not_cached_and_is_a_probe_error(connected):
    connected.ssm.secure.clear()
    errors: list[dict[str, str]] = []
    assert context_layer_demo.load(_ws(), _layer(), errors) is None
    assert errors == [{"probe": "demo_account", "code": "context_layer.demo_param_invalid"}]
    connected.ssm.put_demo_account()
    assert context_layer_demo.load(_ws(), _layer()).username == fx.DEMO_USER


def test_connect_and_disconnect_clear_the_cache(connected):
    context_layer_demo.load(_ws(), _layer())
    assert context_layer_demo._CACHE
    fx.connect_default(connected)
    assert not context_layer_demo._CACHE
    context_layer_demo.load(_ws(), _layer())
    db = SessionLocal()
    try:
        context_layer.disconnect(db, _ws())
    finally:
        db.close()
    assert not context_layer_demo._CACHE


def test_the_password_never_shows_in_a_repr_or_the_public_view(connected):
    demo = context_layer_demo.load(_ws(), _layer())
    assert fx.DEMO_PASSWORD not in repr(demo)
    assert context_layer_demo.public_view(demo) == {"available": True, "username": fx.DEMO_USER}
    assert context_layer_demo.public_view(None) == {"available": False, "username": None}


# ── identity ─────────────────────────────────────────────────────────────────


def _demo(sub: str = fx.DEMO_SUB):
    return context_layer_demo.DemoAccount(fx.DEMO_USER, fx.DEMO_PASSWORD, sub)


@pytest.mark.parametrize("claims,demo,expected", [
    ({"sub": fx.DEMO_SUB, "username": fx.DEMO_SUB}, _demo(), "demo"),
    ({"sub": "another-sub", "username": "another-sub"}, _demo(), "own"),
    ({"sub": fx.DEMO_SUB}, None, None),
    # no stored sub: a username match is conclusive, a mismatch is not
    ({"sub": "x", "username": fx.DEMO_USER.upper()}, _demo(""), "demo"),
    ({"sub": "x", "username": "x"}, _demo(""), None),
    ({}, _demo(), None),
])
def test_identity_compares_the_token_with_the_account(claims, demo, expected):
    token = fx.jwt(time.time() + 3600, **claims)
    assert context_layer_demo.identity_of(token, demo) == expected


def test_identity_of_a_token_that_is_not_a_jwt_is_unknown():
    assert context_layer_demo.identity_of("opaque", _demo()) is None


# ── status ───────────────────────────────────────────────────────────────────


def test_status_names_the_account_and_the_sign_out_link_but_never_the_password(connected):
    member = _status()
    admin = _status("root", is_admin=True)
    for payload in (member, admin):
        assert payload["demo_account"] == {"available": True, "username": fx.DEMO_USER}
        assert payload["sign_out_url"] == SIGN_OUT
        assert fx.DEMO_PASSWORD not in json.dumps(payload)
    assert "demo_account_param" not in member
    assert admin["demo_account_param"] == fx.DEMO_PARAM
    # not authorized: no identity, and the vault is not asked
    assert "identity" not in member["authorization"]
    assert connected.data.calls == []


def test_status_without_a_demo_account(account):
    fx.connect_default(account)
    out = _status()
    assert out["demo_account"] == {"available": False, "username": None}
    assert account.ssm.ops("get_parameter") == []


def test_status_without_a_web_client_has_no_sign_out_link(account):
    account.ssm.params.pop(f"/{fx.PREFIX}/userpool-client-id")
    fx.connect_default(account, demo_account_param=fx.DEMO_PARAM)
    assert "web_client_id" not in _layer().resources
    assert _status()["sign_out_url"] is None


def test_an_unreadable_parameter_degrades_to_unavailable(connected):
    connected.ssm.secure.clear()
    out = _status()
    assert out["demo_account"] == {"available": False, "username": None}
    assert {"probe": "demo_account", "code": "context_layer.demo_param_invalid"} in out[
        "probe_errors"
    ]


@pytest.mark.parametrize("sub,identity", [(fx.DEMO_SUB, "demo"), ("chuanxie-sub", "own")])
def test_an_authorized_caller_learns_which_account_consented(connected, sub, identity):
    connected.data.token = fx.jwt(time.time() + 3600, sub=sub, username=sub)
    _authorize_ledger()
    out = _status()
    assert out["authorization"]["status"] == "authorized"
    assert out["authorization"]["identity"] == identity
    assert connected.data.token not in json.dumps(out)
    _status()  # the second read is the broker's cache
    assert len(connected.data.ops("get_resource_oauth2_token")) == 1


def test_the_identity_check_never_starts_a_consent(connected):
    _authorize_ledger()  # the ledger says authorized, the vault holds nothing
    out = _status()
    assert out["authorization"]["identity"] is None
    db = SessionLocal()
    try:
        grant = db.query(UserGrant).filter(UserGrant.user_id == "alice").one()
    finally:
        db.close()
    assert grant.status == "authorized"  # not flipped to pending


def test_without_a_demo_account_the_status_never_reads_the_vault(account):
    fx.connect_default(account)
    account.data.calls.clear()
    _authorize_ledger()
    assert _status()["authorization"]["identity"] is None
    assert account.data.calls == []


# ── routes ───────────────────────────────────────────────────────────────────


def test_route_connect_refuses_an_out_of_scope_parameter(client, account):
    body = {"prefix": fx.PREFIX, "env": fx.ENV, "region": fx.COA_REGION,
            "demo_account_param": "/coa-w2/agent-oauth-client-id"}
    response = client.put("/api/context-layer", json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "context_layer.demo_param_scope"
    assert account.regions == []
    malformed = client.put("/api/context-layer", json={**body, "demo_account_param": "x y"})
    assert malformed.status_code == 422 and account.regions == []


def test_route_connect_with_the_parameter_shows_the_username(client, account):
    body = {"prefix": fx.PREFIX, "env": fx.ENV, "region": fx.COA_REGION,
            "demo_account_param": fx.DEMO_PARAM}
    response = client.put("/api/context-layer", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["demo_account"] == {"available": True, "username": fx.DEMO_USER}
    assert fx.DEMO_PASSWORD not in response.text


def test_the_light_read_names_the_connection_and_never_the_password(client, connected):
    connected.regions.clear()
    response = client.get("/api/context-layer/demo-account")
    assert response.status_code == 200
    assert response.json() == {
        "available": True, "username": fx.DEMO_USER, "connection": "coa-coa-w2",
    }
    assert fx.DEMO_PASSWORD not in response.text
    # the ledger and the cached parameter only: no other AWS call
    assert {svc for svc, _ in connected.regions} <= {"ssm"}


def test_the_light_read_when_nothing_is_connected(client, account):
    assert client.get("/api/context-layer/demo-account").json() == {
        "available": False, "username": None, "connection": None,
    }


def test_reveal_answers_the_credentials_uncached_and_logs_the_member(client, connected, caplog):
    caplog.set_level(logging.DEBUG)
    response = client.post("/api/context-layer/demo-account/reveal")
    assert response.status_code == 200
    assert response.json() == {"username": fx.DEMO_USER, "password": fx.DEMO_PASSWORD}
    assert response.headers["cache-control"] == "no-store"
    revealed = [r for r in caplog.records if "revealed the COA demo account" in r.getMessage()]
    assert len(revealed) == 1 and revealed[0].getMessage().startswith("admin ")
    # WARNING so the record survives the default (unconfigured) logging level
    assert revealed[0].levelno == logging.WARNING
    for record in caplog.records:
        assert fx.DEMO_PASSWORD not in record.getMessage()
        assert fx.DEMO_PASSWORD not in repr(record.args)


def test_reveal_without_a_demo_account_is_404(client, account):
    fx.connect_default(account)
    response = client.post("/api/context-layer/demo-account/reveal")
    assert response.status_code == 404
    assert response.json()["code"] == "context_layer.demo_unavailable"


def test_reveal_when_nothing_is_connected_is_409(client, account):
    response = client.post("/api/context-layer/demo-account/reveal")
    assert response.status_code == 409
    assert response.json()["code"] == "context_layer.not_connected"


def test_authorize_as_demo_is_logged_and_changes_nothing_in_the_consent(client, connected, caplog):
    caplog.set_level(logging.INFO)
    response = client.post("/api/context-layer/authorize", json={"account": "demo"})
    assert response.status_code == 200, response.text
    assert response.json()["authorized"] is False
    # the vault keys a token by customParameters: a consent made with any would land
    # in a slot the broker, Harness, the playground and Strands never read
    request = connected.data.ops("get_resource_oauth2_token")[-1]
    assert "customParameters" not in request
    assert any("consent as the demo account" in r.getMessage() for r in caplog.records)
    assert fx.DEMO_PASSWORD not in response.text + json.dumps(request)
    for record in caplog.records:
        assert fx.DEMO_PASSWORD not in record.getMessage()


@pytest.mark.parametrize("body", [
    {"json": {"account": "demo"}},
    {"json": {"account": "own"}},
    {"json": {"account": None}},
    {"json": {}},
    {},
])
def test_authorize_never_sends_custom_parameters(client, connected, body):
    response = client.post("/api/context-layer/authorize", **body)
    assert response.status_code == 200, response.text
    requests = connected.data.ops("get_resource_oauth2_token")
    assert requests and all("customParameters" not in r for r in requests)


def test_the_broker_has_no_custom_parameters_passthrough():
    import inspect

    assert "custom_parameters" not in inspect.signature(tokens.user_token).parameters


def test_authorize_as_demo_without_one_is_refused_before_the_vault(client, account):
    fx.connect_default(account)
    account.data.calls.clear()
    response = client.post("/api/context-layer/authorize", json={"account": "demo"})
    assert response.status_code == 404
    assert response.json()["code"] == "context_layer.demo_unavailable"
    assert account.data.calls == []
    bad = client.post("/api/context-layer/authorize", json={"account": "someone"})
    assert bad.status_code == 422


def test_the_identity_check_never_forces_authentication(connected):
    """A COA 401 (``tokens.invalidate``) leaves the ledger ``authorized``; the status's
    identity read must not ask the vault then — that ask carries
    ``forceAuthentication`` and starts a fresh consent."""
    connected.data.token = fx.jwt(time.time() + 3600, sub=fx.DEMO_SUB)
    _authorize_ledger()
    tokens.invalidate("default", "alice")
    connected.data.calls.clear()
    out = _status()
    assert out["authorization"]["status"] == "authorized"
    assert out["authorization"]["identity"] is None
    assert connected.data.ops("get_resource_oauth2_token") == []


def test_a_vault_failure_degrades_the_identity_to_unknown(connected, monkeypatch):
    _authorize_ledger()

    def broken(*_a, **_k):
        raise fx.client_error("AccessDeniedException", "no", "GetWorkloadAccessTokenForUserId")

    monkeypatch.setattr(connected.data, "get_workload_access_token_for_user_id", broken)
    out = _status()
    assert out["connected"] is True
    assert out["authorization"]["identity"] is None
    assert {"probe": "identity", "code": "context_layer.identity_failed"} in out["probe_errors"]


# ── a member (auth-enabled console) ──────────────────────────────────────────

ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def _member(app) -> TestClient:
    client = TestClient(app, client=PEER)
    creds = {"username": "ada", "password": "sufficient-pass"}
    registered = client.post(
        "/api/auth/register", json={**creds, "email": "ada@acme-corp.com"}
    )
    assert registered.status_code == 201, registered.text
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, "ada")
        user.status = users_service.STATUS_ACTIVE
        user.role = "member"
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
        db.commit()
    finally:
        db.close()
    assert client.post("/api/auth/login", json=creds).status_code == 200
    return client


def test_a_member_reveals_and_chooses_the_demo_account_but_cannot_set_it(gated, connected):
    member = _member(gated)
    status = member.get("/api/context-layer").json()
    assert status["demo_account"] == {"available": True, "username": fx.DEMO_USER}
    assert "demo_account_param" not in status
    revealed = member.post("/api/context-layer/demo-account/reveal")
    assert revealed.status_code == 200 and revealed.json()["password"] == fx.DEMO_PASSWORD
    assert member.post("/api/context-layer/authorize", json={"account": "demo"}).status_code == 200
    assert connected.data.ops("get_workload_access_token_for_user_id")[-1]["userId"] == "ada"
    body = {"prefix": fx.PREFIX, "env": fx.ENV, "region": fx.COA_REGION,
            "demo_account_param": ""}
    assert member.put("/api/context-layer", json=body).status_code == 403
