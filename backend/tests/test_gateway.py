"""Gateway bootstrap idempotency, MCP client parsing, harness gateway mapping."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from botocore.exceptions import ClientError

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.deployer.harness import build_create_params
from app.models.ledger import Workspace
from app.routers import tools as tools_router
from app.routers.workspaces import WORKSPACE_HEADER
from app.schemas.agent import AgentSpec
from app.services import aws_clients, mcp_client
from app.services import gateway_bootstrap as gb
from app.services.agentcore.harness import user_authenticated_tools
from tests.conftest import set_default_resources, ws_ctx

GW_ARN = "arn:aws:bedrock-agentcore:us-west-2:111:gateway/launchpad-gw-abc"
OAUTH_ARN = (
    "arn:aws:bedrock-agentcore:us-west-2:111:token-vault/default"
    "/oauth2credentialprovider/launchpad-gw-m2m"
)


def make_control(gateways=(), targets=(), api_providers=(), oauth_providers=()):
    control = MagicMock()
    control.list_gateways.return_value = {"items": list(gateways)}
    control.list_gateway_targets.return_value = {"items": list(targets)}
    control.list_api_key_credential_providers.return_value = {
        "credentialProviders": list(api_providers)
    }
    control.list_oauth2_credential_providers.return_value = {
        "credentialProviders": list(oauth_providers)
    }
    control.get_gateway.return_value = {
        "gatewayId": "launchpad-gw-abc",
        "gatewayArn": GW_ARN,
        "gatewayUrl": "https://gw.example/mcp",
        "status": "READY",
        "name": "launchpad-gw",
        "roleArn": "arn:role",
        "protocolType": "MCP",
        "authorizerType": "CUSTOM_JWT",
        "authorizerConfiguration": {
            "customJWTAuthorizer": {"discoveryUrl": "https://x", "allowedClients": ["console"]}
        },
    }
    control.create_gateway.return_value = {"gatewayId": "launchpad-gw-abc"}
    control.create_gateway_target.return_value = {"targetId": "T1"}
    control.get_gateway_target.return_value = {"status": "READY"}
    control.create_api_key_credential_provider.return_value = {
        "credentialProviderArn": "arn:apikey"
    }
    control.create_oauth2_credential_provider.return_value = {
        "credentialProviderArn": OAUTH_ARN
    }
    return control


def test_ensure_gateway_reuses_existing():
    control = make_control(gateways=[{"name": "launchpad-gw", "gatewayId": "launchpad-gw-abc"}])
    gw, created = gb.ensure_gateway(
        control, role_arn="arn:role", user_pool_id="p", client_id="c", region="us-west-2"
    )
    assert created is False and gw["arn"] == GW_ARN
    control.create_gateway.assert_not_called()


def test_ensure_gateway_creates_with_jwt_auth():
    control = make_control()
    gw, created = gb.ensure_gateway(
        control, role_arn="arn:role", user_pool_id="us-west-2_ABC", client_id="cid",
        region="us-west-2",
    )
    assert created is True
    kwargs = control.create_gateway.call_args.kwargs
    assert kwargs["authorizerType"] == "CUSTOM_JWT"
    jwt = kwargs["authorizerConfiguration"]["customJWTAuthorizer"]
    assert "us-west-2_ABC/.well-known/openid-configuration" in jwt["discoveryUrl"]
    assert jwt["allowedClients"] == ["cid"]


def test_ensure_targets_idempotent():
    control = make_control(
        targets=[{"name": "hr-database", "targetId": "T-hr"},
                 {"name": "office-facts", "targetId": "T-of"}]
    )
    tid, created = gb.ensure_lambda_target(control, "gw", "arn:lambda")
    assert (tid, created) == ("T-hr", False)
    tid2, created2 = gb.ensure_openapi_target(control, "gw", "https://api/prod", "arn:apikey")
    assert (tid2, created2) == ("T-of", False)
    control.create_gateway_target.assert_not_called()


def test_openapi_target_injects_server_and_api_key():
    control = make_control()
    gb.ensure_openapi_target(control, "gw", "https://abc.execute-api/prod/", "arn:apikey")
    kwargs = control.create_gateway_target.call_args.kwargs
    spec = json.loads(kwargs["targetConfiguration"]["mcp"]["openApiSchema"]["inlinePayload"])
    assert spec["servers"] == [{"url": "https://abc.execute-api/prod"}]
    cred = kwargs["credentialProviderConfigurations"][0]
    assert cred["credentialProviderType"] == "API_KEY"
    akp = cred["credentialProvider"]["apiKeyCredentialProvider"]
    assert akp["credentialParameterName"] == "x-api-key"
    assert akp["credentialLocation"] == "HEADER"


def test_ensure_gateway_allows_client_appends_once():
    control = make_control()
    changed = gb.ensure_gateway_allows_client(control, "gw", "m2m-id")
    assert changed is True
    updated = control.update_gateway.call_args.kwargs
    assert updated["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"] == [
        "console", "m2m-id",
    ]
    # second call: already present
    control2 = make_control()
    control2.get_gateway.return_value["authorizerConfiguration"]["customJWTAuthorizer"][
        "allowedClients"
    ] = ["console", "m2m-id"]
    assert gb.ensure_gateway_allows_client(control2, "gw", "m2m-id") is False
    control2.update_gateway.assert_not_called()


def test_harness_gateway_tool_mapping():
    spec = AgentSpec(
        name="gw-agent",
        method="harness",
        system_prompt="x",
        tools=[{"type": "gateway", "name": "hr-database"}],
    )
    params = build_create_params(
        spec, "arn:role", None,
        gateway={"arn": GW_ARN, "oauth_provider_arn": OAUTH_ARN},
    )
    tool = params["tools"][0]
    assert tool["type"] == "agentcore_gateway"
    cfg = tool["config"]["agentCoreGateway"]
    assert cfg["gatewayArn"] == GW_ARN
    assert cfg["outboundAuth"]["oauth"] == {
        "providerArn": OAUTH_ARN,
        "grantType": "CLIENT_CREDENTIALS",
        "scopes": ["launchpad-gw/invoke"],
    }


def test_harness_gateway_ignored_without_config():
    spec = AgentSpec(
        name="gw-agent", method="harness", system_prompt="x",
        tools=[{"type": "gateway", "name": "hr-database"}],
    )
    params = build_create_params(spec, "arn:role", None, gateway=None)
    assert "tools" not in params


def test_harness_user_gateway_override_preserves_other_tools():
    tools = user_authenticated_tools(
        {
            "tools": [
                {"type": "builtin", "name": "browser", "config": {}},
                {
                    "type": "mcp",
                    "name": "docs",
                    "config": {"url": "https://mcp.example/docs"},
                },
                {
                    "type": "gateway",
                    "name": "hr-database",
                    "config": {"gateway_id": "launchpad-gw-abc"},
                },
            ],
            "knowledge_bases": [{"kb_id": "KB1"}],
        },
        {
            "gateway_id": "launchpad-gw-abc",
            "gateway_url": "https://gw.example/mcp",
            "kb_gateway_arn": "arn:kb-gw",
            "oauth_provider_arn": OAUTH_ARN,
        },
        "trusted-user-jwt",
    )
    assert [tool["type"] for tool in tools] == [
        "agentcore_browser",
        "remote_mcp",
        "remote_mcp",
        "agentcore_gateway",
    ]
    user_gateway = tools[2]["config"]["remoteMcp"]
    assert user_gateway == {
        "url": "https://gw.example/mcp",
        "headers": {"Authorization": "Bearer trusted-user-jwt"},
    }


def test_mcp_client_parses_sse_and_json(monkeypatch):
    sse = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text='event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"a"}]}}\n\n',
    )
    assert mcp_client._parse_jsonrpc_body(sse)["result"]["tools"] == [{"name": "a"}]
    plain = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        text='{"jsonrpc":"2.0","id":2,"result":{"ok":true}}',
    )
    assert mcp_client._parse_jsonrpc_body(plain)["result"] == {"ok": True}


def test_mcp_rpc_raises_envelope_on_401(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return httpx.Response(401, text="Unauthorized", request=httpx.Request("POST", url))

    monkeypatch.setattr(mcp_client.httpx, "post", fake_post)
    with pytest.raises(AppError) as err:
        mcp_client._rpc("https://gw/mcp", "tok", "tools/list")
    assert err.value.code == "gateway.unauthorized"


def test_mcp_cognito_auth_rejection_becomes_app_error(monkeypatch):
    cognito = MagicMock()
    cognito.initiate_auth.side_effect = ClientError(
        {
            "Error": {
                "Code": "NotAuthorizedException",
                "Message": "Incorrect username or password.",
            }
        },
        "InitiateAuth",
    )
    set_default_resources(
        {"user_pool_client_id": "client-id", "gateway_url": "https://gw.example/mcp"}
    )
    monkeypatch.setattr(
        mcp_client,
        "load_yaml_config",
        lambda: {"demo_users": {"passwords": {"admin": "stale-password"}}},
    )
    monkeypatch.setattr(aws_clients, "client", lambda *args, **kwargs: cognito)
    mcp_client._token_cache.clear()

    with pytest.raises(AppError) as err:
        mcp_client.get_cognito_token(ws_ctx({"user_pool_client_id": "client-id"}))

    assert err.value.code == "gateway.credentials_rejected"
    assert err.value.status_code == 503
    assert err.value.detail == {"aws_code": "NotAuthorizedException"}


def test_tool_catalog_degrades_when_gateway_credentials_are_rejected(client, monkeypatch):
    def rejected(_ws):
        raise AppError(
            "gateway.credentials_rejected",
            "demo user credentials were rejected",
            status_code=503,
        )

    monkeypatch.setattr(mcp_client, "tools_list", rejected)
    tools_router._cache.clear()

    response = client.get("/api/tools?refresh=true")

    assert response.status_code == 200
    body = response.json()
    assert body["gateway_error"] == "gateway.credentials_rejected"
    assert {tool["name"] for tool in body["tools"]} == {"code-interpreter", "browser"}


def test_browser_demo_retains_live_session_until_stopped(client, monkeypatch):
    class FakeBrowserClient:
        instances = []

        def __init__(self, region):
            self.region = region
            self.session_id = None
            self.started_with = None
            self.stopped = False
            self.instances.append(self)

        def start(self, **kwargs):
            self.started_with = kwargs
            self.session_id = "01KXNH955ZJWTEVR5PFHGE827F"

        def generate_ws_headers(self):
            return "wss://browser.example/automation", {"Authorization": "signed"}

        def generate_live_view_url(self, expires):
            assert expires == tools_router.BROWSER_DEMO_SESSION_SECONDS
            return "https://browser.example/live-view?signed=true"

        def stop(self):
            self.stopped = True

    page = MagicMock()
    page.title.return_value = "Example Domain"
    context = SimpleNamespace(pages=[page])
    remote_browser = SimpleNamespace(contexts=[context])
    chromium = MagicMock()
    chromium.connect_over_cdp.return_value = remote_browser
    playwright = SimpleNamespace(chromium=chromium)

    monkeypatch.setattr("bedrock_agentcore.tools.BrowserClient", FakeBrowserClient)
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: nullcontext(playwright),
    )

    response = client.post("/api/demos/browser", json={"url": "https://example.com"})

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Example Domain"
    assert body["live_view_url"] == "https://browser.example/live-view?signed=true"
    assert body["live_view_expires_in"] == 300
    assert body["viewport"] == {"width": 1280, "height": 720}
    browser_client = FakeBrowserClient.instances[0]
    assert browser_client.started_with == {
        "identifier": "aws.browser.v1",
        "session_timeout_seconds": 300,
        "viewport": {"width": 1280, "height": 720},
    }
    assert browser_client.stopped is False
    page.goto.assert_called_once_with(
        "https://example.com",
        wait_until="domcontentloaded",
        timeout=30000,
    )

    stopped = client.delete(f"/api/demos/browser/{body['session_id']}")

    assert stopped.status_code == 200
    assert stopped.json()["stopped"] is True
    assert stopped.json()["profile_saved"] is None
    assert browser_client.stopped is True


def test_a_browser_demo_session_is_only_stoppable_by_the_workspace_that_started_it(client):
    """The live-session dict is process-global, but each entry names the workspace
    whose account/region the browser runs in. Another workspace's DELETE answers
    exactly like an expired session — the console stops a possibly-expired previous
    session before starting the next, so "already gone" has to stay a success, and a
    404 would make a foreign id distinguishable from a missing one.
    """
    class FakeBrowserClient:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    other = "acct-usw1"
    db = SessionLocal()
    try:
        db.add(
            Workspace(
                id=other, name=other, account_id="444455556666", region="us-west-1",
                bootstrap_status="ready", resources={},
            )
        )
        db.commit()
    finally:
        db.close()
    browser_client = FakeBrowserClient()
    session_id = "01KXNH955ZJWTEVR5PFHGE827F"
    tools_router._browser_demo_sessions[session_id] = tools_router._BrowserDemoSession(
        client=browser_client,
        browser_identifier="aws.browser.v1",
        profile_identifier=None,
        save_profile=False,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    try:
        foreign = client.delete(
            f"/api/demos/browser/{session_id}", headers={WORKSPACE_HEADER: other}
        )

        assert foreign.status_code == 200
        assert foreign.json()["stopped"] is False
        assert browser_client.stopped is False  # still running, still tracked

        owner = client.delete(f"/api/demos/browser/{session_id}")

        assert owner.json()["stopped"] is True
        assert browser_client.stopped is True
    finally:
        tools_router._browser_demo_sessions.pop(session_id, None)


def test_the_browser_demo_refuses_a_cross_account_workspace(client, monkeypatch):
    """`BrowserClient` builds its clients off ambient credentials, so a spoke
    workspace's demo would silently run in the hub account. Refused up front —
    before the SDK is even imported — rather than mislabelled."""
    constructed: list[str] = []
    monkeypatch.setattr(
        "bedrock_agentcore.tools.BrowserClient",
        lambda **kwargs: constructed.append(kwargs) or SimpleNamespace(),
    )
    spoke = "spoke-usw1"
    db = SessionLocal()
    try:
        db.add(
            Workspace(
                id=spoke, name=spoke, account_id="444455556666", region="us-west-1",
                role_arn="arn:aws:iam::444455556666:role/LaunchpadWorkspaceRole",
                external_id="launchpad-spoke", bootstrap_status="ready", resources={},
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.post(
        "/api/demos/browser",
        json={"url": "https://example.com"},
        headers={WORKSPACE_HEADER: spoke},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "workspace.cross_account_tool_unavailable"
    assert constructed == []


def test_the_code_interpreter_demo_targets_the_workspace_it_runs_in(client, monkeypatch):
    """The SDK takes a session, so this demo needs no gate — but it only lands in
    the right account if that session is passed."""
    seen: dict = {}

    class FakeInterpreter:
        def __init__(self, region, session=None):
            seen["region"], seen["session"] = region, session
            self.session_id = "ci-1"

        def start(self, **kwargs):
            seen["timeout"] = kwargs["session_timeout_seconds"]

        def invoke(self, method, params):
            seen["code"] = params["code"]
            return {"stream": [{"result": {"content": [{"type": "text", "text": "42"}]}}]}

        def stop(self):
            seen["stopped"] = True

    monkeypatch.setattr("bedrock_agentcore.tools.CodeInterpreter", FakeInterpreter)

    response = client.post("/api/demos/code-interpreter", json={"code": "print(42)"})

    assert response.status_code == 200
    assert response.json()["stdout"] == "42"
    assert seen["region"] == "us-west-2"
    assert isinstance(seen["session"], aws_clients.FunnelSession)
    assert seen["stopped"] is True


def test_browser_demo_options_lists_web_bot_auth_browsers_and_profiles(
    client,
    monkeypatch,
):
    class FakePaginator:
        def __init__(self, page):
            self.page = page

        def paginate(self):
            yield self.page

    class FakeControlClient:
        def get_paginator(self, operation):
            if operation == "list_browsers":
                return FakePaginator(
                    {
                        "browserSummaries": [
                            {
                                "browserId": "signed-browser-123",
                                "name": "signed-browser",
                                "description": "Web Bot Auth demo",
                                "status": "READY",
                            }
                        ]
                    }
                )
            return FakePaginator(
                {
                    "profileSummaries": [
                        {
                            "profileId": "demo-profile-abcdefghij",
                            "name": "demo-profile",
                            "description": "Persistent demo state",
                            "status": "READY",
                            "lastSavedAt": "2026-07-16T12:00:00+00:00",
                            "lastSavedBrowserId": "signed-browser-123",
                        }
                    ]
                }
            )

        def get_browser(self, browserId):
            assert browserId == "signed-browser-123"
            return {"status": "READY", "browserSigning": {"enabled": True}}

    monkeypatch.setattr(tools_router, "control_client", lambda _ws=None: FakeControlClient())

    response = client.get("/api/demos/browser/options")

    assert response.status_code == 200
    assert response.json() == {
        "browsers": [
            {
                "identifier": "signed-browser-123",
                "name": "signed-browser",
                "description": "Web Bot Auth demo",
                "status": "READY",
                "web_bot_auth": True,
            }
        ],
        "profiles": [
            {
                "identifier": "demo-profile-abcdefghij",
                "name": "demo-profile",
                "description": "Persistent demo state",
                "status": "READY",
                "last_saved_at": "2026-07-16T12:00:00+00:00",
                "last_saved_browser_identifier": "signed-browser-123",
            }
        ],
    }


def test_browser_demo_uses_web_bot_auth_browser_and_saves_profile(
    client,
    monkeypatch,
):
    class FakeControlClient:
        def get_browser(self, browserId):
            assert browserId == "signed-browser-123"
            return {"status": "READY", "browserSigning": {"enabled": True}}

        def get_browser_profile(self, profileId):
            assert profileId == "demo-profile-abcdefghij"
            return {"status": "READY"}

    class FakeDataPlaneClient:
        def __init__(self):
            self.saved = []

        def save_browser_session_profile(self, **kwargs):
            self.saved.append(kwargs)

    class FakeBrowserClient:
        instances = []

        def __init__(self, region):
            self.region = region
            self.session_id = None
            self.started_with = None
            self.stopped = False
            self.data_plane_client = FakeDataPlaneClient()
            self.instances.append(self)

        def start(self, **kwargs):
            self.started_with = kwargs
            self.session_id = "01KXNH955ZJWTEVR5PFHGE827F"

        def generate_ws_headers(self):
            return "wss://browser.example/automation", {"Authorization": "signed"}

        def generate_live_view_url(self, expires):
            assert expires == tools_router.BROWSER_DEMO_SESSION_SECONDS
            return "https://browser.example/live-view?signed=true"

        def stop(self):
            self.stopped = True

    page = MagicMock()
    page.title.return_value = "Example Domain"
    context = SimpleNamespace(pages=[page])
    remote_browser = SimpleNamespace(contexts=[context])
    chromium = MagicMock()
    chromium.connect_over_cdp.return_value = remote_browser
    playwright = SimpleNamespace(chromium=chromium)

    monkeypatch.setattr(tools_router, "control_client", lambda _ws=None: FakeControlClient())
    monkeypatch.setattr("bedrock_agentcore.tools.BrowserClient", FakeBrowserClient)
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: nullcontext(playwright),
    )

    response = client.post(
        "/api/demos/browser",
        json={
            "url": "https://example.com/profile-demo",
            "web_bot_auth": True,
            "browser_identifier": "signed-browser-123",
            "profile_identifier": "demo-profile-abcdefghij",
            "save_profile": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["browser_identifier"] == "signed-browser-123"
    assert body["web_bot_auth"] is True
    assert body["profile_identifier"] == "demo-profile-abcdefghij"
    assert body["save_profile"] is True
    browser_client = FakeBrowserClient.instances[0]
    assert browser_client.started_with == {
        "identifier": "signed-browser-123",
        "session_timeout_seconds": 300,
        "viewport": {"width": 1280, "height": 720},
        "profile_configuration": {
            "profileIdentifier": "demo-profile-abcdefghij"
        },
    }

    stopped = client.delete(f"/api/demos/browser/{body['session_id']}")

    assert stopped.status_code == 200
    assert stopped.json()["stopped"] is True
    assert stopped.json()["profile_saved"] is True
    assert browser_client.data_plane_client.saved == [
        {
            "browserIdentifier": "signed-browser-123",
            "sessionId": body["session_id"],
            "profileIdentifier": "demo-profile-abcdefghij",
        }
    ]
    assert browser_client.stopped is True
