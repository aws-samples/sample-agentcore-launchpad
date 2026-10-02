"""Inbound JWT UX: issuer derivation, the display-only `source_connection`, the
workspace pool issuer on the default API, and the Connection → OIDC discovery
picker (`GET /api/identity/connections/oidc-sources`)."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import app.routers.identity as identity_router
from app.schemas.inbound_auth import (
    InboundAuth,
    JwtInboundConfig,
    authorizer_configuration,
    discovery_url_for_issuer,
    issuer_from_discovery_url,
    normalize_issuer,
)
from app.services import identity_providers as ip
from app.services import inbound_auth as svc
from tests.conftest import set_default_resources, ws_ctx

POOL_ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_ABC123"
POOL_DISCOVERY = f"{POOL_ISSUER}/.well-known/openid-configuration"
OKTA_DISCOVERY = "https://acme.okta.com/oauth2/default/.well-known/openid-configuration"


# ── pure helpers ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "issuer"),
    [
        (POOL_DISCOVERY, POOL_ISSUER),
        (OKTA_DISCOVERY, "https://acme.okta.com/oauth2/default"),
        ("HTTPS://Login.Example.COM/Tenant/v2.0/.well-known/openid-configuration",
         "https://login.example.com/Tenant/v2.0"),
        ("https://idp.example/.well-known/openid-configuration", "https://idp.example"),
        ("https://idp.example/oidc", None),
        ("", None),
        (None, None),
    ],
)
def test_issuer_from_discovery_url(url, issuer):
    assert issuer_from_discovery_url(url) == issuer


def test_normalize_issuer_refuses_non_urls_and_templates():
    assert normalize_issuer("https://idp.example/") == "https://idp.example"
    assert normalize_issuer("idp.example") is None
    assert normalize_issuer("ftp://idp.example") is None
    assert normalize_issuer("https://login.microsoftonline.com/{tenantid}/v2.0") is None
    assert discovery_url_for_issuer("https://idp.example/") == (
        "https://idp.example/.well-known/openid-configuration"
    )
    assert discovery_url_for_issuer("") is None


def test_source_connection_is_display_only():
    auth = InboundAuth(mode="jwt", jwt=JwtInboundConfig(
        discovery_url=OKTA_DISCOVERY, allowed_audience=["api://agent"],
        source_connection="corp-okta",
    ))
    assert auth.model_dump()["jwt"]["source_connection"] == "corp-okta"
    # the authorizer build never sees it
    assert authorizer_configuration(auth) == {"customJWTAuthorizer": {
        "discoveryUrl": OKTA_DISCOVERY, "allowedAudience": ["api://agent"],
    }}
    # additive: a config without it still parses (every stored blob)
    assert JwtInboundConfig(discovery_url=OKTA_DISCOVERY, allowed_clients=["c"]
                            ).source_connection is None
    with pytest.raises(ValueError):
        JwtInboundConfig(discovery_url=OKTA_DISCOVERY, allowed_clients=["c"],
                         source_connection="bad name")


def test_workspace_cognito_issuer_and_mismatch():
    ws = ws_ctx({"user_pool_id": "us-west-2_ABC123"})
    assert svc.workspace_cognito_issuer(ws) == POOL_ISSUER
    assert svc.workspace_cognito_issuer(ws_ctx()) is None

    def jwt(url):
        return InboundAuth(mode="jwt", jwt=JwtInboundConfig(
            discovery_url=url, allowed_clients=["c"]))

    assert svc.issuer_mismatch(jwt(POOL_DISCOVERY), ws) is None
    assert svc.issuer_mismatch(jwt(OKTA_DISCOVERY), ws) == {
        "agent_issuer": "https://acme.okta.com/oauth2/default",
        "workspace_issuer": POOL_ISSUER,
    }
    assert svc.issuer_mismatch(InboundAuth(mode="iam"), ws) is None
    # no pool: nothing to compare against (the M2M path names its own error)
    assert svc.issuer_mismatch(jwt(OKTA_DISCOVERY), ws_ctx()) is None


def test_default_api_reports_the_pool_issuer(client):
    assert client.get("/api/identity/inbound-auth/default").json()["cognito_issuer"] is None
    set_default_resources({"user_pool_id": "us-west-2_ABC123", "user_pool_client_id": "web"})
    body = client.get("/api/identity/inbound-auth/default").json()
    assert body["cognito_issuer"] == POOL_ISSUER
    assert body["cognito"]["discovery_url"] == POOL_DISCOVERY


# ── Connection → discovery derivation ────────────────────────────────────────


def _provider(name, vendor, discovery):
    key = ip.VENDOR_CONFIG_KEYS.get(vendor, "includedOauth2ProviderConfig")
    return {
        "name": name,
        "credentialProviderVendor": vendor,
        "credentialProviderArn": f"arn:aws:x:::token-vault/default/oauth2credentialprovider/{name}",
        "oauth2ProviderConfigOutput": {key: {"clientId": "outbound-cid",
                                             "oauthDiscovery": discovery}},
    }


def test_oidc_discovery_from_discovery_url():
    got = ip.oidc_discovery(_provider("okta", "CustomOauth2", {"discoveryUrl": OKTA_DISCOVERY}))
    assert got == {"discovery_url": OKTA_DISCOVERY,
                   "issuer": "https://acme.okta.com/oauth2/default",
                   "derived_from": "discovery_url"}


def test_oidc_discovery_from_issuer_metadata():
    got = ip.oidc_discovery(_provider("kc", "CustomOauth2", {"authorizationServerMetadata": {
        "issuer": "https://kc.example/realms/corp/",
        "authorizationEndpoint": "https://kc.example/auth",
        "tokenEndpoint": "https://kc.example/token",
    }}))
    assert got == {
        "discovery_url": "https://kc.example/realms/corp/.well-known/openid-configuration",
        "issuer": "https://kc.example/realms/corp",
        "derived_from": "issuer",
    }


def test_oidc_discovery_uses_a_builtin_vendor_only_when_its_config_names_one():
    # a built-in vendor whose stored config names an issuer (the live shape)
    ms = _provider("entra", "MicrosoftOauth2", {"authorizationServerMetadata": {
        "issuer": "https://login.microsoftonline.com/tenant-1/v2.0"}})
    assert ip.oidc_discovery(ms)["discovery_url"] == (
        "https://login.microsoftonline.com/tenant-1/v2.0/.well-known/openid-configuration"
    )
    # templated multi-tenant issuer, nothing stored, or no output: not derivable
    templated = _provider("entra2", "MicrosoftOauth2", {"authorizationServerMetadata": {
        "issuer": "https://login.microsoftonline.com/{tenantid}/v2.0"}})
    assert ip.oidc_discovery(templated) is None
    assert ip.oidc_discovery(_provider("g", "GoogleOauth2", {})) is None
    assert ip.oidc_discovery({"credentialProviderVendor": "GoogleOauth2"}) is None
    assert ip.oidc_discovery(None) is None


def test_github_reported_issuer_is_not_offered():
    # the live GetOauth2CredentialProvider shape (2026-09-29): GitHub reports the
    # Actions OIDC issuer, which does not sign OAuth-app user tokens
    gh = _provider("gh", "GithubOauth2", {"authorizationServerMetadata": {
        "issuer": "https://token.actions.githubusercontent.com",
        "authorizationEndpoint": "https://github.com/login/oauth/authorize",
        "tokenEndpoint": "https://github.com/login/oauth/access_token",
    }})
    assert ip.oidc_discovery(gh) is None


def _control(providers):
    control = MagicMock()
    control.list_oauth2_credential_providers.return_value = {
        "credentialProviders": [{"name": p["name"]} for p in providers]
    }
    by_name = {p["name"]: p for p in providers}

    def get(name):
        if name not in by_name:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "Get")
        return by_name[name]

    control.get_oauth2_credential_provider.side_effect = get
    return control


def test_list_oidc_sources_filters_and_skips_unreadable():
    providers = [
        _provider("zeta-okta", "CustomOauth2", {"discoveryUrl": OKTA_DISCOVERY}),
        _provider("launchpad-gw-m2m", "CustomOauth2", {"discoveryUrl": POOL_DISCOVERY}),
        _provider("gh", "GithubOauth2", {"authorizationServerMetadata": {
            "issuer": "https://token.actions.githubusercontent.com"}}),
        _provider("alpha-kc", "CustomOauth2", {"authorizationServerMetadata": {
            "issuer": "https://kc.example/realms/corp"}}),
    ]
    control = _control(providers)
    control.list_oauth2_credential_providers.return_value["credentialProviders"].append(
        {"name": "flaky"})
    base_get = control.get_oauth2_credential_provider.side_effect

    def get(name):
        if name == "flaky":
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "Get")
        return base_get(name)

    control.get_oauth2_credential_provider.side_effect = get
    sources = ip.list_oidc_sources(control)
    assert [s["name"] for s in sources] == ["alpha-kc", "zeta-okta"]
    assert sources[1]["vendor"] == "CustomOauth2"
    # never the Connection's own (outbound) client id
    assert all("client_id" not in s and "outbound-cid" not in str(s) for s in sources)
    # the system provider is never read, let alone offered
    asked = [c.kwargs["name"] for c in control.get_oauth2_credential_provider.call_args_list]
    assert "launchpad-gw-m2m" not in asked


def test_router_oidc_sources(client, monkeypatch):
    control = _control([_provider("corp-okta", "CustomOauth2", {"discoveryUrl": OKTA_DISCOVERY})])
    monkeypatch.setattr(identity_router, "control_client", lambda _ctx: control)
    response = client.get("/api/identity/connections/oidc-sources")
    assert response.status_code == 200, response.text
    assert response.json() == {"sources": [{
        "name": "corp-okta", "vendor": "CustomOauth2", "discovery_url": OKTA_DISCOVERY,
        "issuer": "https://acme.okta.com/oauth2/default", "derived_from": "discovery_url",
    }]}
