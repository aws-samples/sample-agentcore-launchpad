"""A second CUSTOM_JWT Gateway that trusts the workspace pool can carry the user token."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services import governance, invoke, registry_console, user_gateways
from app.services.agentcore import harness as hc

from .conftest import client_error, ws_ctx

POOL = "us-west-2_AbCdEf"
DISCOVERY = f"https://cognito-idp.us-west-2.amazonaws.com/{POOL}/.well-known/openid-configuration"
OAUTH = "arn:aws:bedrock-agentcore:us-west-2:111:token-vault/default/oauth2credentialprovider/m2m"
LP_ARN = "arn:aws:bedrock-agentcore:us-west-2:111:gateway/launchpad-gw-abc"
EXT_ARN = "arn:aws:bedrock-agentcore:us-west-2:111:gateway/team-gw-xyz"
EXT_URL = "https://team-gw-xyz.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"

RESOURCES = {
    "gateway_id": "launchpad-gw-abc",
    "gateway_arn": LP_ARN,
    "gateway_url": "https://launchpad-gw-abc.example/mcp",
    "oauth_provider_arn": OAUTH,
    "user_pool_id": POOL,
    "user_pool_client_id": "console-client",
    "m2m_client_id": "m2m-client",
}


def _gateway(**overrides):
    gateway = {
        "gatewayId": "team-gw-xyz",
        "gatewayArn": EXT_ARN,
        "gatewayUrl": EXT_URL,
        "name": "team-gw",
        "protocolType": "MCP",
        "authorizerType": "CUSTOM_JWT",
        "authorizerConfiguration": {"customJWTAuthorizer": {
            "discoveryUrl": DISCOVERY,
            "allowedClients": ["console-client", "m2m-client"],
        }},
    }
    gateway.update(overrides)
    return gateway


def _jwt(**fields):
    return {"customJWTAuthorizer": {"discoveryUrl": DISCOVERY, **fields}}


def test_trusted_when_pool_and_both_clients_match():
    assert user_gateways.trusts_workspace_users(_gateway(), RESOURCES) is True


@pytest.mark.parametrize("gateway", [
    _gateway(authorizerType="AWS_IAM"),
    _gateway(gatewayId="launchpad-gw-abc"),
    _gateway(authorizerConfiguration={"customJWTAuthorizer": {
        "discoveryUrl": DISCOVERY.replace(POOL, "us-west-2_Other"),
        "allowedClients": ["console-client", "m2m-client"]}}),
    _gateway(authorizerConfiguration=_jwt(allowedClients=["console-client"])),
    _gateway(authorizerConfiguration=_jwt(allowedClients=["m2m-client"])),
    _gateway(authorizerConfiguration=_jwt(
        allowedClients=["console-client", "m2m-client"], allowedAudience=["x"])),
    _gateway(interceptorConfigurations=[{
        "interceptor": {"lambda": {"arn": "arn:aws:lambda:us-west-2:111:function:f"}},
        "interceptionPoints": ["REQUEST"],
        "inputConfiguration": {"passRequestHeaders": True},
    }]),
], ids=["iam", "launchpad-gw", "other-pool", "no-m2m", "no-console", "audience",
        "header-interceptor"])
def test_untrusted_shapes(gateway):
    assert user_gateways.trusts_workspace_users(gateway, RESOURCES) is False


def test_interceptor_without_headers_is_still_trusted():
    gateway = _gateway(interceptorConfigurations=[{
        "interceptor": {"lambda": {"arn": "arn:aws:lambda:us-west-2:111:function:f"}},
        "interceptionPoints": ["RESPONSE"],
    }])
    assert user_gateways.trusts_workspace_users(gateway, RESOURCES) is True


@pytest.mark.parametrize("missing", ["user_pool_id", "m2m_client_id", "oauth_provider_arn"])
def test_untrusted_without_workspace_identity(missing):
    resources = {k: v for k, v in RESOURCES.items() if k != missing}
    assert user_gateways.trusts_workspace_users(_gateway(), resources) is False


def test_registry_attachment_maps_trusted_gateway_to_m2m():
    attachment = registry_console._gateway_attachment(_gateway(), RESOURCES)
    assert attachment["attachable"] is True
    assert attachment["outbound_auth"] == {"oauth": {
        "providerArn": OAUTH, "grantType": "CLIENT_CREDENTIALS",
        "scopes": [registry_console.GATEWAY_SCOPE],
    }}
    other = registry_console._gateway_attachment(
        _gateway(authorizerConfiguration=_jwt(allowedClients=["console-client"])), RESOURCES)
    assert other["attachable"] is False


def test_governance_view_marks_trusted_gateway_attachable():
    view = governance._attachability(_gateway(), ws_ctx(RESOURCES))
    assert view == {"attachable": True, "reason": None, "auth_type": "oauth"}


def _configured():
    m2m = {"oauth": {"providerArn": OAUTH, "grantType": "CLIENT_CREDENTIALS",
                     "scopes": ["launchpad-gw/invoke"]}}
    return [
        {"type": "agentcore_gateway", "name": "launchpad_gw",
         "config": {"agentCoreGateway": {"gatewayArn": LP_ARN, "outboundAuth": m2m}}},
        {"type": "agentcore_gateway", "name": "team_gw",
         "config": {"agentCoreGateway": {"gatewayArn": EXT_ARN, "outboundAuth": m2m}}},
        {"type": "agentcore_gateway", "name": "iam_gw",
         "config": {"agentCoreGateway": {
             "gatewayArn": "arn:aws:bedrock-agentcore:us-west-2:111:gateway/iam-gw",
             "outboundAuth": {"awsIam": {}}}}},
    ]


def test_user_tools_swap_listed_gateway_and_keep_its_alias():
    tools = hc.user_authenticated_tools(
        {}, RESOURCES, "user-jwt", configured_tools=_configured(),
        user_gateways={EXT_ARN: EXT_URL},
    )
    assert [t["name"] for t in tools] == [hc.USER_GATEWAY_ALIAS, "team_gw", "iam_gw"]
    assert tools[1] == {"type": "remote_mcp", "name": "team_gw", "config": {"remoteMcp": {
        "url": EXT_URL, "headers": {"Authorization": "Bearer user-jwt"}}}}
    assert tools[2]["type"] == "agentcore_gateway"


def test_user_tools_leave_unlisted_gateway_on_m2m():
    tools = hc.user_authenticated_tools({}, RESOURCES, "user-jwt", configured_tools=_configured())
    assert tools[1]["type"] == "agentcore_gateway"


def _workspace(monkeypatch, gateway_or_error):
    control = MagicMock()
    if isinstance(gateway_or_error, Exception):
        control.get_gateway.side_effect = gateway_or_error
    else:
        control.get_gateway.return_value = gateway_or_error
    monkeypatch.setattr(invoke, "control_client", lambda ctx: control)
    return ws_ctx(RESOURCES), control


def test_trusting_gateways_rereads_only_m2m_candidates(monkeypatch):
    workspace, control = _workspace(monkeypatch, _gateway())
    assert invoke._user_trusting_gateways(_configured(), workspace) == {EXT_ARN: EXT_URL}
    control.get_gateway.assert_called_once_with(gatewayIdentifier="team-gw-xyz")


def test_trusting_gateways_drop_a_gateway_that_changed_since_deploy(monkeypatch):
    changed = _gateway(authorizerConfiguration=_jwt(allowedClients=["m2m-client"]))
    workspace, _ = _workspace(monkeypatch, changed)
    assert invoke._user_trusting_gateways(_configured(), workspace) == {}


def test_trusting_gateways_fail_closed_on_read_error(monkeypatch):
    workspace, _ = _workspace(monkeypatch, client_error("AccessDeniedException"))
    assert invoke._user_trusting_gateways(_configured(), workspace) == {}


def test_overrides_route_the_user_token_to_the_trusted_gateway(monkeypatch):
    workspace, _ = _workspace(monkeypatch, _gateway())
    monkeypatch.setattr(hc, "get_harness", lambda client, hid: {
        "tools": _configured(), "allowedTools": ["@team_gw/*", "@launchpad_gw/*"]})
    agent = SimpleNamespace(
        spec={"tools": [{"type": "gateway", "name": "team"}]}, resource_id="h-1")
    overrides = invoke.harness_user_overrides(agent, workspace, "user-jwt")
    team = next(t for t in overrides["tools"] if t["name"] == "team_gw")
    assert team["type"] == "remote_mcp"
    assert team["config"]["remoteMcp"]["headers"] == {"Authorization": "Bearer user-jwt"}
    assert "@team_gw/*" in overrides["allowedTools"]
