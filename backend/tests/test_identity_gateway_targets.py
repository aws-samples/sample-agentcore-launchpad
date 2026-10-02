"""Gateway targets bound to a Connection: request shapes, read-back, guards."""

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import app.routers.identity as identity_router
from app.core.errors import AppError, NotFoundError
from app.services import identity_gateway_targets as gt

VAULT = "arn:aws:bedrock-agentcore:us-west-2:111:token-vault/default"
OAUTH_ARN = f"{VAULT}/oauth2credentialprovider/team-idp"
APIKEY_ARN = f"{VAULT}/apikeycredentialprovider/team-key"
RESOURCES = {"gateway_id": "gw-1"}
OPENAPI = """
openapi: 3.0.1
info: {title: crm, version: "1"}
servers: [{url: "https://crm.example"}]
paths:
  /accounts:
    get: {operationId: listAccounts, responses: {"200": {description: ok}}}
"""


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Op")


def make_control(targets=()):
    control = MagicMock()
    control.list_gateway_targets.return_value = {
        "items": [{"targetId": t["targetId"], "name": t["name"]} for t in targets]
    }
    by_id = {t["targetId"]: t for t in targets}
    control.get_gateway_target.side_effect = lambda **kw: by_id[kw["targetId"]]
    control.get_oauth2_credential_provider.return_value = {"credentialProviderArn": OAUTH_ARN}
    control.get_api_key_credential_provider.return_value = {"credentialProviderArn": APIKEY_ARN}
    control.create_gateway_target.return_value = {"targetId": "NEW1", "status": "CREATING"}
    return control


def _target(target_id, name, credential=None, source="openApiSchema"):
    return {
        "targetId": target_id,
        "name": name,
        "status": "READY",
        "targetConfiguration": {"mcp": {source: {}}},
        "credentialProviderConfigurations": [credential] if credential else [],
    }


OAUTH_CRED = {
    "credentialProviderType": "OAUTH",
    "credentialProvider": {"oauthCredentialProvider": {
        "providerArn": OAUTH_ARN, "scopes": ["crm/read"], "grantType": "CLIENT_CREDENTIALS",
    }},
}
APIKEY_CRED = {
    "credentialProviderType": "API_KEY",
    "credentialProvider": {"apiKeyCredentialProvider": {"providerArn": APIKEY_ARN}},
}


def _create(control, **over):
    kwargs = {
        "name": "crm-api", "description": None, "source": "openapi",
        "openapi_schema": OPENAPI, "mcp_endpoint": None, "connection": "team-idp",
        "kind": "oauth2", "mode": "as_agent", "scopes": ["crm/read"],
    } | over
    return gt.create_target(control, RESOURCES, **kwargs)


def test_create_openapi_oauth_target_request_shape():
    control = make_control()
    out = _create(control, description="CRM")
    call = control.create_gateway_target.call_args.kwargs
    assert call["gatewayIdentifier"] == "gw-1" and call["name"] == "crm-api"
    assert call["description"] == "CRM"
    payload = json.loads(call["targetConfiguration"]["mcp"]["openApiSchema"]["inlinePayload"])
    assert payload["servers"][0]["url"] == "https://crm.example"
    assert call["credentialProviderConfigurations"] == [OAUTH_CRED]
    assert out == {
        "target_id": "NEW1", "name": "crm-api", "description": "CRM", "status": "CREATING",
        "status_reasons": [], "source": "openapi", "system": False,
        "auth": "oauth2", "connection": "team-idp", "mode": "as_agent", "scopes": ["crm/read"],
        "warnings": [],
    }


def test_create_openapi_api_key_target_with_placement():
    control = make_control()
    out = _create(
        control, connection="team-key", kind="api_key", scopes=[],
        api_key_location="QUERY_PARAMETER", api_key_parameter="key", api_key_prefix="Token",
    )
    [cred] = control.create_gateway_target.call_args.kwargs["credentialProviderConfigurations"]
    assert cred == {
        "credentialProviderType": "API_KEY",
        "credentialProvider": {"apiKeyCredentialProvider": {
            "providerArn": APIKEY_ARN, "credentialLocation": "QUERY_PARAMETER",
            "credentialParameterName": "key", "credentialPrefix": "Token",
        }},
    }
    assert out["auth"] == "api_key" and out["connection"] == "team-key"


def test_create_mcp_server_target_takes_oauth_only():
    control = make_control()
    _create(control, source="mcp", openapi_schema=None, mcp_endpoint="https://mcp.example/mcp")
    call = control.create_gateway_target.call_args.kwargs
    assert call["targetConfiguration"] == {
        "mcp": {"mcpServer": {"endpoint": "https://mcp.example/mcp"}}
    }
    with pytest.raises(AppError) as err:
        _create(
            make_control(), source="mcp", openapi_schema=None,
            mcp_endpoint="https://mcp.example/mcp", kind="api_key", scopes=[],
        )
    assert err.value.code == "identity.target_auth_unsupported"


@pytest.mark.parametrize(
    ("over", "code", "status"),
    [
        ({"mode": "bogus"}, "identity.mode_unsupported", 422),
        ({"name": "bad_name"}, "identity.invalid_target_name", 422),
        ({"openapi_schema": "swagger: '2.0'"}, "identity.invalid_openapi", 422),
        ({"openapi_schema": OPENAPI.replace("https://", "http://")},
         "identity.invalid_openapi", 422),
        ({"openapi_schema": "{: not yaml"}, "identity.invalid_openapi", 422),
        ({"source": "mcp", "openapi_schema": None, "mcp_endpoint": "http://x"},
         "identity.invalid_mcp_endpoint", 422),
    ],
)
def test_create_refuses_bad_input_without_calling_aws(over, code, status):
    control = make_control()
    with pytest.raises(AppError) as err:
        _create(control, **over)
    assert (err.value.code, err.value.status_code) == (code, status)
    control.create_gateway_target.assert_not_called()


def test_create_duplicate_name_is_409():
    control = make_control([_target("T1", "crm-api")])
    with pytest.raises(AppError) as err:
        _create(control)
    assert (err.value.code, err.value.status_code) == ("identity.target_exists", 409)
    control.create_gateway_target.assert_not_called()

    racing = make_control()
    racing.create_gateway_target.side_effect = _client_error("ConflictException")
    with pytest.raises(AppError) as err:
        _create(racing)
    assert err.value.code == "identity.target_exists"


def test_create_unknown_connection_is_404_and_no_gateway_is_409():
    control = make_control()
    control.get_oauth2_credential_provider.side_effect = _client_error(
        "ResourceNotFoundException"
    )
    with pytest.raises(NotFoundError):
        _create(control)
    control.create_gateway_target.assert_not_called()
    with pytest.raises(AppError) as err:
        gt.create_target(make_control(), {}, name="x", description=None, source="openapi",
                         openapi_schema=OPENAPI, mcp_endpoint=None, connection="c",
                         kind="oauth2", mode="as_agent", scopes=[])
    assert (err.value.code, err.value.status_code) == ("identity.no_gateway", 409)


def test_list_reads_back_the_binding_per_target():
    control = make_control([
        _target("T1", "crm-api", OAUTH_CRED),
        _target("T2", "office-facts", APIKEY_CRED),
        _target("T3", "hr-database", {"credentialProviderType": "GATEWAY_IAM_ROLE"},
                source="lambda"),
        _target("T4", "remote", OAUTH_CRED, source="mcpServer"),
    ])
    targets = {t["name"]: t for t in gt.list_targets(control, RESOURCES)}
    assert targets["crm-api"]["connection"] == "team-idp"
    assert targets["crm-api"]["mode"] == "as_agent"
    assert targets["office-facts"]["system"] is True
    assert targets["office-facts"]["auth"] == "api_key"
    assert targets["hr-database"]["connection"] is None
    assert targets["hr-database"]["auth"] == "gateway_iam_role"
    assert targets["hr-database"]["source"] == "lambda"
    assert targets["remote"]["source"] == "mcp"
    assert gt.list_targets(control, {}) == []


def test_list_skips_a_target_deleted_between_list_and_get():
    control = make_control()
    control.list_gateway_targets.return_value = {"items": [{"targetId": "gone"}]}
    control.get_gateway_target.side_effect = _client_error("ResourceNotFoundException")
    assert gt.list_targets(control, RESOURCES) == []


def test_delete_guards():
    control = make_control([
        _target("T1", "crm-api", OAUTH_CRED),
        _target("T2", "office-facts", APIKEY_CRED),
        _target("T3", "kb-mount"),
    ])
    gt.delete_target(control, RESOURCES, "T1")
    control.delete_gateway_target.assert_called_once_with(
        gatewayIdentifier="gw-1", targetId="T1"
    )
    for target_id, code in (("T2", "identity.system_target"), ("T3", "identity.target_unbound")):
        with pytest.raises(AppError) as err:
            gt.delete_target(control, RESOURCES, target_id)
        assert (err.value.code, err.value.status_code) == (code, 409)
    control.get_gateway_target.side_effect = _client_error("ResourceNotFoundException")
    with pytest.raises(NotFoundError):
        gt.delete_target(control, RESOURCES, "nope")
    assert control.delete_gateway_target.call_count == 1


# ── router ───────────────────────────────────────────────────────────────────


@pytest.fixture
def control(monkeypatch):
    stub = make_control([_target("T1", "crm-api", OAUTH_CRED)])
    monkeypatch.setattr(identity_router, "control_client", lambda _ctx: stub)
    monkeypatch.setattr(identity_router, "_resources", lambda _ws: dict(RESOURCES))
    return stub


def test_router_gateway_targets_round_trip(client, control):
    listed = client.get("/api/identity/gateway-targets").json()
    assert listed["gateway_id"] == "gw-1"
    assert [t["name"] for t in listed["targets"]] == ["crm-api"]

    created = client.post("/api/identity/gateway-targets", json={
        "name": "facts-api", "source": "openapi", "openapi_schema": OPENAPI,
        "connection": "team-key", "kind": "api_key",
        "api_key": {"location": "HEADER", "parameter_name": "x-api-key"},
    })
    assert created.status_code == 201, created.text
    [cred] = control.create_gateway_target.call_args.kwargs["credentialProviderConfigurations"]
    assert cred["credentialProvider"]["apiKeyCredentialProvider"][
        "credentialParameterName"
    ] == "x-api-key"

    assert client.delete("/api/identity/gateway-targets/T1").json() == {"deleted": True}


def test_router_gateway_target_request_validation(client, control):
    res = client.post("/api/identity/gateway-targets", json={
        "name": "x", "source": "lambda", "connection": "c", "kind": "oauth2",
    })
    assert res.status_code == 422
    control.create_gateway_target.assert_not_called()
