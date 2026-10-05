"""Connections (credential providers): service merge/reconcile, secret
non-persistence, 409/404 guards, and the /api/identity/connections router."""

import json
import logging
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import app.routers.identity as identity_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent, IdentityProvider
from app.services import identity_providers as ip

VAULT = "arn:aws:bedrock-agentcore:us-west-2:111:token-vault/default"
OAUTH_ARN = f"{VAULT}/oauth2credentialprovider/team-idp"
APIKEY_ARN = f"{VAULT}/apikeycredentialprovider/team-key"
CALLBACK = "https://bedrock-agentcore.us-west-2.amazonaws.com/identities/oauth2/callback/abc"
SECRET = "s3cr3t-never-stored"
API_KEY = "ak-never-stored"


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Op")


def make_control(oauth=(), api=()):
    control = MagicMock()
    control.list_oauth2_credential_providers.return_value = {
        "credentialProviders": list(oauth)
    }
    control.list_api_key_credential_providers.return_value = {
        "credentialProviders": list(api)
    }
    control.create_oauth2_credential_provider.return_value = {
        "credentialProviderArn": OAUTH_ARN,
        "callbackUrl": CALLBACK,
        "name": "team-idp",
    }
    control.create_api_key_credential_provider.return_value = {
        "credentialProviderArn": APIKEY_ARN,
        "name": "team-key",
    }
    control.list_gateway_targets.return_value = {"items": []}
    return control


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def _agent_with_auth(db, connection: str, *, legacy: bool = False) -> Agent:
    auth = (
        {"provider": connection, "flow": "M2M"}
        if legacy
        else {"connection": connection, "kind": "oauth2", "mode": "as_agent"}
    )
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID,
        name=f"consumer-{connection}",
        method="zip_runtime",
        status="active",
        spec={
            "name": "consumer",
            "tools": [
                {"type": "rest", "name": "crm", "config": {"url": "https://crm.example"},
                 "auth": auth},
            ],
        },
    )
    db.add(agent)
    db.commit()
    return agent


def _create_oauth(control, db, **over):
    kwargs = {
        "name": "team-idp",
        "vendor": "CustomOauth2",
        "client_id": "cid",
        "client_secret": SECRET,
        "discovery_url": "https://idp.example/.well-known/openid-configuration",
        "scopes": ["crm/read"],
        "template": "custom_oidc",
        "created_by": "admin",
    } | over
    return ip.create_oauth2_connection(control, db, DEFAULT_WORKSPACE_ID, **kwargs)


# ── service: list / reconcile ────────────────────────────────────────────────


def test_list_merges_both_kinds_flags_system_external_and_missing(db):
    db.add(IdentityProvider(
        workspace_id=DEFAULT_WORKSPACE_ID, name="gone", kind="api_key", vendor="",
        arn=f"{VAULT}/apikeycredentialprovider/gone", created_by="admin",
    ))
    db.commit()
    control = make_control(
        oauth=[
            {"name": "launchpad-gw-m2m", "credentialProviderVendor": "CustomOauth2",
             "credentialProviderArn": f"{VAULT}/oauth2credentialprovider/launchpad-gw-m2m"},
            {"name": "outside", "credentialProviderVendor": "GithubOauth2",
             "credentialProviderArn": f"{VAULT}/oauth2credentialprovider/outside"},
        ],
        api=[{"name": "team-key", "credentialProviderArn": APIKEY_ARN}],
    )
    items = {(i["kind"], i["name"]): i for i in ip.list_connections(
        control, db, DEFAULT_WORKSPACE_ID
    )}
    assert items[("oauth2", "launchpad-gw-m2m")]["system"] is True
    assert items[("oauth2", "launchpad-gw-m2m")]["source"] == "system"
    assert items[("oauth2", "outside")]["source"] == "external"
    assert items[("oauth2", "outside")]["vendor"] == "GithubOauth2"
    assert items[("api_key", "team-key")]["status"] == "ready"
    gone = items[("api_key", "gone")]
    assert gone["status"] == "missing" and gone["source"] == "launchpad"


def test_list_paginates_at_20(db):
    control = make_control()
    control.list_oauth2_credential_providers.side_effect = [
        {"credentialProviders": [{"name": "a"}], "nextToken": "t1"},
        {"credentialProviders": [{"name": "b"}]},
    ]
    names = [i["name"] for i in ip.list_connections(control, db, DEFAULT_WORKSPACE_ID)]
    assert names == ["a", "b"]
    calls = control.list_oauth2_credential_providers.call_args_list
    assert calls[0].kwargs == {"maxResults": 20}
    assert calls[1].kwargs == {"maxResults": 20, "nextToken": "t1"}


def test_list_reports_agent_and_gateway_target_references(db):
    agent = _agent_with_auth(db, "team-idp", legacy=True)
    control = make_control(oauth=[{"name": "team-idp", "credentialProviderArn": OAUTH_ARN}])
    bound = [{"target_id": "T1", "name": "crm-api", "connection": "team-idp"}]
    [item] = ip.list_connections(control, db, DEFAULT_WORKSPACE_ID, bound_targets=bound)
    assert item["referenced_by"] == [
        {"type": "agent", "id": agent.id, "name": agent.name},
        {"type": "gateway_target", "id": "T1", "name": "crm-api"},
    ]


# ── service: create ──────────────────────────────────────────────────────────


def test_create_oauth2_passes_secret_through_and_never_persists_it(db, caplog):
    control = make_control()
    with caplog.at_level(logging.DEBUG):
        out = _create_oauth(control, db)
    control.create_oauth2_credential_provider.assert_called_once_with(
        name="team-idp",
        credentialProviderVendor="CustomOauth2",
        oauth2ProviderConfigInput={
            "customOauth2ProviderConfig": {
                "oauthDiscovery": {
                    "discoveryUrl": "https://idp.example/.well-known/openid-configuration"
                },
                "clientId": "cid",
                "clientSecret": SECRET,
            }
        },
    )
    assert out["callback_url"] == CALLBACK
    assert out["arn"] == OAUTH_ARN and out["source"] == "launchpad"
    assert SECRET not in json.dumps(out)
    assert SECRET not in caplog.text
    row = db.query(IdentityProvider).filter_by(name="team-idp").one()
    row_values = json.dumps({c.name: str(getattr(row, c.name)) for c in row.__table__.columns})
    assert SECRET not in row_values
    assert row.callback_url == CALLBACK and row.client_id == "cid"
    assert row.scopes == ["crm/read"] and row.template == "custom_oidc"


def test_create_oauth2_explicit_endpoints_and_vendor_shapes(db):
    control = make_control()
    _create_oauth(
        control, db, discovery_url=None, issuer="https://idp.example",
        authorization_endpoint="https://idp.example/authorize",
        token_endpoint="https://idp.example/token",
    )
    config = control.create_oauth2_credential_provider.call_args.kwargs[
        "oauth2ProviderConfigInput"
    ]["customOauth2ProviderConfig"]
    assert config["oauthDiscovery"] == {"authorizationServerMetadata": {
        "issuer": "https://idp.example",
        "authorizationEndpoint": "https://idp.example/authorize",
        "tokenEndpoint": "https://idp.example/token",
    }}

    github = make_control()
    _create_oauth(github, db, name="gh", vendor="GithubOauth2", discovery_url=None)
    assert github.create_oauth2_credential_provider.call_args.kwargs[
        "oauth2ProviderConfigInput"
    ] == {"githubOauth2ProviderConfig": {"clientId": "cid", "clientSecret": SECRET}}


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"vendor": "OktaOauth2"}, "identity.unsupported_vendor"),
        ({"discovery_url": None}, "identity.missing_endpoints"),
        ({"vendor": "GoogleOauth2"}, "identity.endpoints_custom_only"),
    ],
)
def test_create_oauth2_shape_errors_are_422_before_any_aws_call(db, over, code):
    control = make_control()
    with pytest.raises(AppError) as err:
        _create_oauth(control, db, **over)
    assert err.value.code == code and err.value.status_code == 422
    control.create_oauth2_credential_provider.assert_not_called()


def test_duplicate_name_is_409_before_the_secret_leaves(db):
    control = make_control(oauth=[{"name": "team-idp"}])
    with pytest.raises(AppError) as err:
        _create_oauth(control, db)
    assert err.value.code == "identity.connection_exists"
    assert err.value.status_code == 409
    control.create_oauth2_credential_provider.assert_not_called()


def test_aws_conflict_maps_to_connection_exists(db):
    control = make_control()
    control.create_api_key_credential_provider.side_effect = _client_error("ConflictException")
    with pytest.raises(AppError) as err:
        ip.create_api_key_connection(control, db, DEFAULT_WORKSPACE_ID, name="k", api_key=API_KEY)
    assert (err.value.code, err.value.status_code) == ("identity.connection_exists", 409)
    assert db.query(IdentityProvider).filter_by(name="k").first() is None


def test_stale_audit_row_is_replaced_on_recreate(db):
    db.add(IdentityProvider(
        workspace_id=DEFAULT_WORKSPACE_ID, name="team-key", kind="api_key", vendor="",
        arn="arn:old", created_by="someone",
    ))
    db.commit()
    control = make_control()
    ip.create_api_key_connection(
        control, db, DEFAULT_WORKSPACE_ID, name="team-key", api_key=API_KEY, created_by="admin"
    )
    rows = db.query(IdentityProvider).filter_by(name="team-key").all()
    assert len(rows) == 1 and rows[0].arn == APIKEY_ARN and rows[0].created_by == "admin"


# ── service: get / delete ────────────────────────────────────────────────────


def test_get_rereads_callback_url_from_aws(db):
    control = make_control()
    _create_oauth(control, db)
    control.get_oauth2_credential_provider.return_value = {
        "name": "team-idp", "credentialProviderArn": OAUTH_ARN,
        "credentialProviderVendor": "CustomOauth2", "callbackUrl": CALLBACK + "-fresh",
    }
    out = ip.get_connection(control, db, DEFAULT_WORKSPACE_ID, "oauth2", "team-idp")
    assert out["callback_url"] == CALLBACK + "-fresh"
    assert out["client_id"] == "cid"


def test_get_unknown_is_404(db):
    control = make_control()
    control.get_api_key_credential_provider.side_effect = _client_error(
        "ResourceNotFoundException"
    )
    with pytest.raises(NotFoundError) as err:
        ip.get_connection(control, db, DEFAULT_WORKSPACE_ID, "api_key", "nope")
    assert err.value.code == "identity.connection_not_found"


def test_delete_removes_provider_and_audit_row(db):
    control = make_control()
    _create_oauth(control, db)
    control.list_oauth2_credential_providers.return_value = {
        "credentialProviders": [{"name": "team-idp"}]
    }
    ip.delete_connection(control, db, DEFAULT_WORKSPACE_ID, "oauth2", "team-idp")
    control.delete_oauth2_credential_provider.assert_called_once_with(name="team-idp")
    assert db.query(IdentityProvider).filter_by(name="team-idp").first() is None


def test_delete_refuses_system_connections(db):
    control = make_control(oauth=[{"name": "launchpad-gw-m2m"}])
    with pytest.raises(AppError) as err:
        ip.delete_connection(control, db, DEFAULT_WORKSPACE_ID, "oauth2", "launchpad-gw-m2m")
    assert (err.value.code, err.value.status_code) == ("identity.system_connection", 409)
    control.delete_oauth2_credential_provider.assert_not_called()


def test_delete_refuses_referenced_by_agent_or_target(db):
    agent = _agent_with_auth(db, "team-idp")
    control = make_control(oauth=[{"name": "team-idp"}])
    with pytest.raises(AppError) as err:
        ip.delete_connection(control, db, DEFAULT_WORKSPACE_ID, "oauth2", "team-idp")
    assert err.value.status_code == 409
    assert err.value.detail["referenced_by"][0]["id"] == agent.id

    with pytest.raises(AppError) as err:
        ip.delete_connection(
            control, db, DEFAULT_WORKSPACE_ID, "oauth2", "other",
            bound_targets=[{"target_id": "T", "name": "t", "connection": "other"}],
        )
    assert err.value.code == "identity.connection_referenced"
    control.delete_oauth2_credential_provider.assert_not_called()


def test_deleted_agents_do_not_hold_references(db):
    agent = _agent_with_auth(db, "team-idp")
    agent.status = "deleted"
    db.commit()
    control = make_control()
    _create_oauth(control, db)
    control.list_oauth2_credential_providers.return_value = {
        "credentialProviders": [{"name": "team-idp"}]
    }
    ip.delete_connection(control, db, DEFAULT_WORKSPACE_ID, "oauth2", "team-idp")
    control.delete_oauth2_credential_provider.assert_called_once()


@pytest.mark.parametrize("kind", ["oauth2", "api_key"])
def test_delete_refuses_an_external_provider_and_leaves_the_vault_alone(db, kind):
    # in the vault, but no ledger row in this workspace: created outside Launchpad
    control = make_control(oauth=[{"name": "theirs"}], api=[{"name": "theirs"}])
    with pytest.raises(AppError) as err:
        ip.delete_connection(control, db, DEFAULT_WORKSPACE_ID, kind, "theirs")
    assert (err.value.code, err.value.status_code) == ("identity.external_connection", 409)
    assert err.value.detail == {"kind": kind, "name": "theirs"}
    control.delete_oauth2_credential_provider.assert_not_called()
    control.delete_api_key_credential_provider.assert_not_called()


def test_delete_refuses_a_provider_recorded_only_by_another_workspace(db):
    control = make_control()
    ip.create_oauth2_connection(
        control, db, "acct-usw1", name="team-idp", vendor="CustomOauth2",
        client_id="cid", client_secret=SECRET,
        discovery_url="https://idp.example/.well-known/openid-configuration",
    )
    control.list_oauth2_credential_providers.return_value = {
        "credentialProviders": [{"name": "team-idp"}]
    }
    with pytest.raises(AppError) as err:
        ip.delete_connection(control, db, DEFAULT_WORKSPACE_ID, "oauth2", "team-idp")
    assert err.value.code == "identity.external_connection"
    control.delete_oauth2_credential_provider.assert_not_called()


def test_delete_cleans_up_a_ledger_row_whose_provider_is_gone(db):
    control = make_control()
    _create_oauth(control, db)  # recorded, then deleted from the vault out-of-band
    ip.delete_connection(control, db, DEFAULT_WORKSPACE_ID, "oauth2", "team-idp")
    control.delete_oauth2_credential_provider.assert_not_called()
    assert db.query(IdentityProvider).filter_by(name="team-idp").first() is None


def test_delete_unknown_is_404(db):
    with pytest.raises(NotFoundError):
        ip.delete_connection(make_control(), db, DEFAULT_WORKSPACE_ID, "api_key", "nope")


# ── router ───────────────────────────────────────────────────────────────────


@pytest.fixture
def control(monkeypatch):
    stub = make_control()
    monkeypatch.setattr(identity_router, "control_client", lambda _ctx: stub)
    return stub


def test_router_create_list_get_delete_round_trip(client, control):
    created = client.post("/api/identity/connections/oauth2", json={
        "name": "team-idp", "vendor": "CustomOauth2", "template": "cognito",
        "client_id": "cid", "client_secret": SECRET,
        "discovery_url": "https://idp.example/.well-known/openid-configuration",
        "scopes": [" crm/read ", ""],
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["callback_url"] == CALLBACK and body["scopes"] == ["crm/read"]
    assert body["template"] == "cognito"
    assert SECRET not in created.text

    key = client.post("/api/identity/connections/api-key", json={
        "name": "team-key", "api_key": API_KEY,
    })
    assert key.status_code == 201 and API_KEY not in key.text

    control.list_oauth2_credential_providers.return_value = {
        "credentialProviders": [{"name": "team-idp", "credentialProviderArn": OAUTH_ARN}]
    }
    control.list_api_key_credential_providers.return_value = {
        "credentialProviders": [{"name": "team-key", "credentialProviderArn": APIKEY_ARN}]
    }
    listed = client.get("/api/identity/connections").json()["connections"]
    assert {(c["kind"], c["name"]) for c in listed} == {
        ("oauth2", "team-idp"), ("api_key", "team-key"),
    }
    assert SECRET not in json.dumps(listed) and API_KEY not in json.dumps(listed)

    control.get_oauth2_credential_provider.return_value = {
        "name": "team-idp", "credentialProviderArn": OAUTH_ARN, "callbackUrl": CALLBACK,
    }
    got = client.get("/api/identity/connections/oauth2/team-idp")
    assert got.status_code == 200 and got.json()["callback_url"] == CALLBACK

    dup = client.post("/api/identity/connections/api-key", json={
        "name": "team-key", "api_key": API_KEY,
    })
    assert dup.status_code == 409 and dup.json()["code"] == "identity.connection_exists"

    deleted = client.delete("/api/identity/connections/api_key/team-key")
    assert deleted.status_code == 200 and deleted.json() == {"deleted": True}
    control.delete_api_key_credential_provider.assert_called_once_with(name="team-key")


def test_router_templates_cover_every_offered_vendor(client, control):
    templates = client.get("/api/identity/connections/templates").json()["templates"]
    ids = {t["id"] for t in templates}
    assert {"cognito", "custom_oidc", "custom_endpoints", "github", "api_key"} <= ids
    vendors = {t["vendor"] for t in templates if t["kind"] == "oauth2"}
    assert vendors <= set(ip.VENDOR_CONFIG_KEYS)


def test_router_unknown_template_is_not_recorded(client, control):
    res = client.post("/api/identity/connections/oauth2", json={
        "name": "x", "template": "bogus", "client_id": "c", "client_secret": "s",
        "discovery_url": "https://idp.example/.well-known/openid-configuration",
    })
    assert res.status_code == 201 and res.json()["template"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"name": "bad name!", "client_id": "c", "client_secret": "s"},
        {"name": "ok", "client_id": "c"},  # secret required
        {"name": "ok", "vendor": "NotAVendor", "client_id": "c", "client_secret": "s"},
    ],
)
def test_router_oauth2_request_validation(client, control, body):
    assert client.post("/api/identity/connections/oauth2", json=body).status_code == 422
    control.create_oauth2_credential_provider.assert_not_called()


def test_router_delete_refuses_when_a_gateway_target_references_it(client, control, monkeypatch):
    monkeypatch.setattr(identity_router, "_resources", lambda _ws: {"gateway_id": "gw-1"})
    control.list_api_key_credential_providers.return_value = {
        "credentialProviders": [{"name": "team-key"}]
    }
    control.list_gateway_targets.return_value = {"items": [{"targetId": "T1"}]}
    control.get_gateway_target.return_value = {
        "targetId": "T1", "name": "facts",
        "targetConfiguration": {"mcp": {"openApiSchema": {"inlinePayload": "{}"}}},
        "credentialProviderConfigurations": [{
            "credentialProviderType": "API_KEY",
            "credentialProvider": {"apiKeyCredentialProvider": {"providerArn": APIKEY_ARN}},
        }],
    }
    res = client.delete("/api/identity/connections/api_key/team-key")
    assert res.status_code == 409
    assert res.json()["detail"]["referenced_by"] == [
        {"type": "gateway_target", "id": "T1", "name": "facts"}
    ]


def test_router_list_survives_a_gateway_read_failure(client, control, monkeypatch):
    monkeypatch.setattr(identity_router, "_resources", lambda _ws: {"gateway_id": "gw-1"})
    control.list_gateway_targets.side_effect = _client_error("AccessDeniedException")
    assert client.get("/api/identity/connections").status_code == 200
    # ...but the delete path refuses instead of skipping the reference check
    assert client.delete("/api/identity/connections/api_key/x").status_code >= 400
    control.delete_api_key_credential_provider.assert_not_called()


# ── 422 responses never echo a secret ────────────────────────────────────────

LEAKY = "S3CRET-DO-NOT-ECHO"


@pytest.mark.parametrize(
    "body",
    [
        # missing field: Pydantic's input is the whole body
        {"name": "ok", "client_secret": LEAKY},
        # too long: the input is the value itself
        {"name": "ok", "client_id": "c", "client_secret": LEAKY + "x" * 3000},
        # an unrelated bad field next to the secret
        {"name": "bad name!", "client_id": "c", "client_secret": LEAKY},
    ],
    ids=["missing-client-id", "over-long-secret", "bad-name"],
)
def test_oauth2_validation_422_does_not_echo_client_secret(client, control, body):
    response = client.post("/api/identity/connections/oauth2", json=body)
    assert response.status_code == 422
    assert LEAKY not in response.text
    assert all("input" not in error for error in response.json()["detail"])
    control.create_oauth2_credential_provider.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {"api_key": LEAKY},  # missing name
        {"name": "ok", "api_key": LEAKY + "x" * 65536},  # over-long key
    ],
    ids=["missing-name", "over-long-key"],
)
def test_api_key_validation_422_does_not_echo_the_key(client, control, body):
    response = client.post("/api/identity/connections/api-key", json=body)
    assert response.status_code == 422
    assert LEAKY not in response.text
    control.create_api_key_credential_provider.assert_not_called()


def test_validation_422_redacts_nested_secrets_and_keeps_plain_limits():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel, Field

    from app.core.errors import register_error_handlers

    class Inner(BaseModel):
        refreshToken: str = Field(max_length=4)  # noqa: N815 — camelCase on purpose
        Authorization: str | None = None
        size: int

    class Outer(BaseModel):
        name: str = Field(max_length=3)
        config: Inner

    probe = FastAPI()
    register_error_handlers(probe)

    @probe.post("/probe")
    def _probe(body: Outer) -> dict:
        return {}

    response = TestClient(probe).post("/probe", json={
        "name": "toolong",
        "config": {"refreshToken": LEAKY, "Authorization": f"Bearer {LEAKY}", "size": "x"},
    })
    assert response.status_code == 422
    assert LEAKY not in response.text
    errors = {tuple(e["loc"]): e for e in response.json()["detail"]}
    # a secret-located error keeps its loc/msg but loses input and ctx
    assert "ctx" not in errors[("body", "config", "refreshToken")]
    # a plain field keeps the numeric limit the console shows
    assert errors[("body", "name")]["ctx"] == {"max_length": 3}


@pytest.mark.parametrize(("name", "sensitive"), [
    ("client_secret", True), ("apiKey", True), ("API_KEY", True), ("access_token", True),
    ("refresh_token", True), ("Authorization", True), ("private-key", True),
    ("password", True), ("name", False), ("client_id", False), ("scopes", False), (0, False),
])
def test_sensitive_field_names(name, sensitive):
    from app.core.errors import is_sensitive_field

    assert is_sensitive_field(name) is sensitive
