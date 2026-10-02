"""On-behalf-of token exchange (identity P3): the Connection's
onBehalfOfTokenExchangeConfig, the IdP capability refusal, and gateway
targets in `obo` mode (grantType TOKEN_EXCHANGE)."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import app.routers.identity as identity_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.main import create_app
from app.services import identity_gateway_targets as gt
from app.services import identity_providers as ip
from tests.test_identity_connections import SECRET
from tests.test_identity_connections import make_control as make_vault
from tests.test_identity_gateway_targets import OAUTH_ARN, RESOURCES
from tests.test_identity_gateway_targets import _create as create_target
from tests.test_identity_gateway_targets import make_control as make_gateway

IDP = "https://idp.example/.well-known/openid-configuration"
COGNITO = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_x/.well-known/openid-configuration"
EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
OBO_OUT = {"customOauth2ProviderConfig": {"onBehalfOfTokenExchangeConfig": {
    "grantType": "TOKEN_EXCHANGE",
    "tokenExchangeGrantTypeConfig": {"actorTokenContent": "M2M", "actorTokenScopes": ["a"]},
}}}


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def _declares(*grants):
    return lambda _url: {"jwks_uri": "https://idp.example/jwks",
                         "grant_types_supported": list(grants)}


DECLARES_EXCHANGE = _declares(EXCHANGE)


def _create(control, db, obo, *, probe=DECLARES_EXCHANGE, **over):
    kwargs = {"name": "obo-idp", "vendor": "CustomOauth2", "client_id": "cid",
              "client_secret": SECRET, "discovery_url": IDP} | over
    return ip.create_oauth2_connection(
        control, db, DEFAULT_WORKSPACE_ID, obo=obo, probe=probe, **kwargs)


# ── Connection ──────────────────────────────────────────────────────────────


def test_obo_connection_request_shape_and_echo(db):
    control = make_vault()
    out = _create(control, db, {"grant_type": "TOKEN_EXCHANGE", "actor_token_content": "M2M",
                                "actor_token_scopes": ["a"]})
    config = control.create_oauth2_credential_provider.call_args.kwargs[
        "oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
    assert config["onBehalfOfTokenExchangeConfig"] == (
        OBO_OUT["customOauth2ProviderConfig"]["onBehalfOfTokenExchangeConfig"])
    assert out["obo"] == {"grant_type": "TOKEN_EXCHANGE", "actor_token_content": "M2M",
                          "actor_token_scopes": ["a"]}


def test_jwt_authorization_grant_carries_no_actor_config(db):
    control = make_vault()
    _create(control, db, {"grant_type": "JWT_AUTHORIZATION_GRANT"},
            probe=_declares("urn:ietf:params:oauth:grant-type:jwt-bearer"))
    config = control.create_oauth2_credential_provider.call_args.kwargs[
        "oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
    assert config["onBehalfOfTokenExchangeConfig"] == {"grantType": "JWT_AUTHORIZATION_GRANT"}


@pytest.mark.parametrize(("over", "probe", "code"), [
    ({"discovery_url": COGNITO}, None, "identity.obo_unsupported"),
    ({}, _declares("authorization_code", "client_credentials"), "identity.obo_unsupported"),
    ({"vendor": "GithubOauth2", "discovery_url": None}, None, "identity.obo_vendor_unsupported"),
])
def test_obo_is_refused_before_the_secret_leaves(db, over, probe, code):
    control = make_vault()
    with pytest.raises(AppError) as err:
        _create(control, db, {"grant_type": "TOKEN_EXCHANGE"}, probe=probe, **over)
    assert (err.value.code, err.value.status_code) == (code, 422)
    control.create_oauth2_credential_provider.assert_not_called()


def test_actor_scopes_need_m2m_actor(db):
    with pytest.raises(AppError) as err:
        _create(make_vault(), db, {"grant_type": "TOKEN_EXCHANGE",
                                   "actor_token_content": "NONE", "actor_token_scopes": ["a"]})
    assert err.value.code == "identity.obo_invalid"


def test_an_idp_that_declares_nothing_is_let_through(db):
    control = make_vault()
    _create(control, db, {"grant_type": "TOKEN_EXCHANGE"},
            probe=lambda _u: {"jwks_uri": "https://idp.example/jwks"})
    control.create_oauth2_credential_provider.assert_called_once()


def test_router_passes_the_obo_block(monkeypatch):
    captured = {}

    def fake(*_a, **kwargs):
        captured.update(kwargs)
        return {"name": kwargs["name"]}

    monkeypatch.setattr(identity_router.connections, "create_oauth2_connection", fake)
    monkeypatch.setattr(identity_router, "control_client", lambda _ctx: MagicMock())
    with TestClient(create_app()) as client:
        res = client.post("/api/identity/connections/oauth2", json={
            "name": "obo-idp", "client_id": "c", "client_secret": "s", "discovery_url": IDP,
            "obo": {"grant_type": "TOKEN_EXCHANGE"},
        })
    assert res.status_code == 201, res.text
    assert captured["obo"] == {"grant_type": "TOKEN_EXCHANGE", "actor_token_content": "NONE",
                               "actor_token_scopes": []}


# ── gateway target ──────────────────────────────────────────────────────────


def _obo_gateway(authorizer="CUSTOM_JWT", obo=True):
    control = make_gateway()
    control.get_gateway.return_value = {"authorizerType": authorizer}
    control.get_oauth2_credential_provider.return_value = {
        "credentialProviderArn": OAUTH_ARN,
        **({"oauth2ProviderConfigOutput": OBO_OUT} if obo else {}),
    }
    return control


def test_obo_target_asks_the_gateway_for_a_token_exchange():
    control = _obo_gateway()
    out = create_target(control, mode="obo")
    [cred] = control.create_gateway_target.call_args.kwargs["credentialProviderConfigurations"]
    assert cred["credentialProvider"]["oauthCredentialProvider"] == {
        "providerArn": OAUTH_ARN, "scopes": ["crm/read"], "grantType": "TOKEN_EXCHANGE",
    }
    assert out["mode"] == "obo"
    control.get_gateway.assert_called_once_with(gatewayIdentifier=RESOURCES["gateway_id"])


@pytest.mark.parametrize(("control", "code", "status"), [
    (_obo_gateway(authorizer="AWS_IAM"), "identity.obo_needs_jwt_gateway", 409),
    (_obo_gateway(obo=False), "identity.obo_unsupported", 422),
])
def test_obo_target_guards(control, code, status):
    with pytest.raises(AppError) as err:
        create_target(control, mode="obo")
    assert (err.value.code, err.value.status_code) == (code, status)
    control.create_gateway_target.assert_not_called()


def test_obo_target_takes_an_oauth2_connection():
    with pytest.raises(AppError) as err:
        create_target(_obo_gateway(), mode="obo", kind="api_key", connection="team-key", scopes=[])
    assert err.value.code == "identity.target_auth_unsupported"


def test_read_back_maps_token_exchange_to_obo():
    detail = {"credentialProviderConfigurations": [{
        "credentialProviderType": "OAUTH",
        "credentialProvider": {"oauthCredentialProvider": {
            "providerArn": OAUTH_ARN, "scopes": [], "grantType": "TOKEN_EXCHANGE"}},
    }]}
    assert gt._binding(detail)["mode"] == "obo"


# ── obo issuer consistency hint ─────────────────────────────────────────────

GATEWAY_POOL = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_gw"


def _obo_with_issuers(connection_discovery):
    control = _obo_gateway()
    control.get_gateway.return_value = {
        "authorizerType": "CUSTOM_JWT",
        "authorizerConfiguration": {"customJWTAuthorizer": {
            "discoveryUrl": f"{GATEWAY_POOL}/.well-known/openid-configuration",
            "allowedClients": ["c"],
        }},
    }
    out = {**OBO_OUT["customOauth2ProviderConfig"], "oauthDiscovery": connection_discovery}
    control.get_oauth2_credential_provider.return_value = {
        "credentialProviderArn": OAUTH_ARN, "credentialProviderVendor": "CustomOauth2",
        "oauth2ProviderConfigOutput": {"customOauth2ProviderConfig": out},
    }
    return control


def test_obo_target_warns_when_the_idp_is_not_the_gateway_issuer():
    control = _obo_with_issuers({"discoveryUrl": IDP})
    out = create_target(control, mode="obo")
    # created regardless: a cross-issuer trust is a legitimate IdP setup
    control.create_gateway_target.assert_called_once()
    [warning] = out["warnings"]
    assert warning["code"] == "identity.obo_issuer_mismatch"
    assert warning["detail"] == {
        "connection": "team-idp",
        "connection_issuer": "https://idp.example",
        "gateway_issuer": GATEWAY_POOL,
    }


@pytest.mark.parametrize("discovery", [
    {"discoveryUrl": f"{GATEWAY_POOL}/.well-known/openid-configuration"},
    {"authorizationServerMetadata": {"issuer": f"{GATEWAY_POOL}/"}},
    {},  # issuer unknown on the Connection side: nothing to compare
])
def test_obo_target_has_no_warning_on_a_matching_or_unknown_issuer(discovery):
    assert create_target(_obo_with_issuers(discovery), mode="obo")["warnings"] == []


def test_non_obo_targets_carry_an_empty_warnings_list():
    assert create_target(make_gateway(), mode="as_agent")["warnings"] == []
