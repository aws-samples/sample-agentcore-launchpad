"""Gateway bootstrap idempotency, MCP client parsing.

``app.deployer.harness.build_create_params``/``app.services.agentcore.harness``
(the harness gateway-tool-mapping and user-gateway-override coverage that used to
live here) were removed with the AWS Harness deploy/invoke paths (N1 Task 2) —
there is no Harness CreateHarness payload builder in this build. Everything above
is independent of that and still runs.
"""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.models.ledger import Workspace
from app.routers import tools as tools_router
from app.routers.workspaces import WORKSPACE_HEADER
from app.services import aws_clients, mcp_client
from app.services import gateway_bootstrap as gb
from app.services.workspace import WorkspaceContext

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
    gw, created = gb.ensure_gateway(control, role_arn="arn:role", region="us-west-2")
    assert created is False and gw["arn"] == GW_ARN
    control.create_gateway.assert_not_called()


def test_ensure_gateway_creates_an_iam_authorized_gateway():
    control = make_control()
    gw, created = gb.ensure_gateway(control, role_arn="arn:role", region="us-west-2")
    assert created is True
    kwargs = control.create_gateway.call_args.kwargs
    assert kwargs["authorizerType"] == "AWS_IAM"
    assert "authorizerConfiguration" not in kwargs


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


def test_ensure_gateway_still_creates_an_iam_authorized_gateway():
    control = make_control()
    gw, created = gb.ensure_gateway(
        control, role_arn="arn:aws-cn:iam::1:role/gw", region="cn-northwest-1",
    )
    assert created
    kwargs = control.create_gateway.call_args.kwargs
    assert kwargs["authorizerType"] == "AWS_IAM"
    assert "authorizerConfiguration" not in kwargs


def test_lambda_target_uses_gateway_iam_role_credential_provider(tmp_path, monkeypatch):
    control = make_control()
    gb.ensure_lambda_target(control, "gw-1", "arn:aws-cn:lambda:cn-northwest-1:1:function:hr")
    kwargs = control.create_gateway_target.call_args.kwargs
    assert kwargs["credentialProviderConfigurations"] == [
        {"credentialProviderType": "GATEWAY_IAM_ROLE"}
    ]


def test_openapi_target_still_uses_api_key_credential_provider():
    control = make_control()
    gb.ensure_openapi_target(
        control,
        "gw-1",
        "https://facts.example",
        "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:token-vault/default/apikeycredentialprovider/x",
    )
    kwargs = control.create_gateway_target.call_args.kwargs
    cp = kwargs["credentialProviderConfigurations"][0]
    assert cp["credentialProviderType"] == "API_KEY"


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
    ws = WorkspaceContext(account_id="1", region="cn-northwest-1")
    ws.resources["gateway_url"] = "https://gw/mcp"
    monkeypatch.setattr(
        WorkspaceContext,
        "credentials",
        lambda self: SimpleNamespace(access_key="AK", secret_key="SK", token=None),
    )

    def fake_post(url, *, headers=None, content=None, timeout=None):
        return httpx.Response(401, text="Unauthorized", request=httpx.Request("POST", url))

    monkeypatch.setattr(mcp_client.httpx, "post", fake_post)
    with pytest.raises(AppError) as err:
        mcp_client._rpc(ws, "tools/list")
    assert err.value.code == "gateway.unauthorized"


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


# N15-obs17: the data-plane endpoint botocore resolved for an aws-cn region — the ONLY
# host the demo may sign its stream / live-view URLs against (SDK 1.17.0 builds a Global one)
CN_DATA_PLANE = "https://bedrock-agentcore.cn-northwest-1.amazonaws.com.cn"
DEMO_SESSION = "01KXNH955ZJWTEVR5PFHGE827F"


def _fake_browser_client(data_plane_client=None):
    """A `BrowserClient` double with the attributes the partition-aware signing reads
    (`region`, `identifier`, `session_id`, `data_plane_client.meta.endpoint_url`). The SDK's
    own URL builders are present but trip: the demo must never call them (N15-obs17)."""

    class FakeBrowserClient:
        instances = []

        def __init__(self, region):
            self.region = region
            self.identifier = None
            self.session_id = None
            self.started_with = None
            self.stopped = False
            self.data_plane_client = data_plane_client or SimpleNamespace()
            self.data_plane_client.meta = SimpleNamespace(endpoint_url=CN_DATA_PLANE)
            self.instances.append(self)

        def start(self, **kwargs):
            self.started_with = kwargs
            self.identifier = kwargs["identifier"]
            self.session_id = DEMO_SESSION

        def generate_ws_headers(self):  # pragma: no cover — must not be reached
            raise AssertionError("SDK generate_ws_headers builds a Global host (N15-obs17)")

        def generate_live_view_url(self, expires):  # pragma: no cover — must not be reached
            raise AssertionError("SDK generate_live_view_url builds a Global host (N15-obs17)")

        def stop(self):
            self.stopped = True

    return FakeBrowserClient


def _fake_hub_credentials(monkeypatch):
    """The hub session the demo signs with: static fake keys, never resolved from the host."""
    from botocore.credentials import Credentials

    creds = Credentials("AKIAFAKEFAKEFAKEFAKE", "fake-secret-not-real", "fake-token")
    monkeypatch.setattr(tools_router, "_demo_signing_credentials", lambda workspace: creds)
    return creds


def _assert_cn_signed_stream(chromium, session_id: str, identifier: str = "aws.browser.v1"):
    """The CDP dial went to the aws-cn data-plane host with a SigV4 header set scoped to
    cn-northwest-1 / bedrock-agentcore and the session token — never the secret."""
    call = chromium.connect_over_cdp.call_args
    ws_url, kwargs = call.args[0], call.kwargs
    assert ws_url == (
        "wss://bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/browser-streams/"
        f"{identifier}/sessions/{session_id}/automation"
    )
    headers = kwargs["headers"]
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAFAKEFAKEFAKEFAKE/")
    assert "cn-northwest-1/bedrock-agentcore/aws4_request" in headers["Authorization"]
    assert headers["X-Amz-Security-Token"] == "fake-token" and "X-Amz-Date" in headers
    assert "fake-secret-not-real" not in json.dumps(headers)


def test_browser_demo_retains_live_session_until_stopped(client, monkeypatch):
    FakeBrowserClient = _fake_browser_client()
    _fake_hub_credentials(monkeypatch)

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
    # the live view is pre-signed (SigV4 query) on the same aws-cn data-plane host
    assert body["live_view_url"].startswith(
        "https://bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/browser-streams/aws.browser.v1"
        f"/sessions/{DEMO_SESSION}/live-view?"
    )
    assert "X-Amz-Expires=300" in body["live_view_url"]
    assert "X-Amz-Signature=" in body["live_view_url"]
    assert "fake-secret-not-real" not in body["live_view_url"]
    assert body["live_view_expires_in"] == 300
    _assert_cn_signed_stream(chromium, DEMO_SESSION)
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


def test_browser_demo_failure_is_a_502_envelope_and_the_session_is_stopped(
    client, monkeypatch
):
    """N15-obs17 (secondary): the live failure escaped as a plain-text 500. Any failure
    after the session started is one `demo.browser_failed` envelope with a bounded reason,
    the SDK session is stopped and nothing is retained."""
    FakeBrowserClient = _fake_browser_client()
    _fake_hub_credentials(monkeypatch)
    chromium = MagicMock()
    chromium.connect_over_cdp.side_effect = RuntimeError(
        "WebSocket error: getaddrinfo ENOTFOUND " + "x" * 600
    )
    playwright = SimpleNamespace(chromium=chromium)
    monkeypatch.setattr("bedrock_agentcore.tools.BrowserClient", FakeBrowserClient)
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright", lambda: nullcontext(playwright),
    )

    response = client.post("/api/demos/browser", json={"url": "https://example.com"})

    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "demo.browser_failed"
    reason = body["detail"]["reason"]
    assert reason.startswith("RuntimeError: WebSocket error: getaddrinfo ENOTFOUND")
    assert len(reason) <= tools_router.DEMO_FAILURE_REASON_CHARS
    assert "Traceback" not in response.text
    (browser_client,) = FakeBrowserClient.instances
    assert browser_client.stopped is True
    assert DEMO_SESSION not in tools_router._browser_demo_sessions
    _assert_cn_signed_stream(chromium, DEMO_SESSION)


def test_browser_demo_signs_against_the_clients_resolved_endpoint_not_a_literal(
    client, monkeypatch
):
    """The host comes from the data-plane client the SDK built for the region: a client
    whose resolver answered another host is followed there (no partition table, no literal)."""
    FakeBrowserClient = _fake_browser_client()
    _fake_hub_credentials(monkeypatch)
    page = MagicMock()
    page.title.return_value = "Example Domain"
    chromium = MagicMock()
    chromium.connect_over_cdp.return_value = SimpleNamespace(
        contexts=[SimpleNamespace(pages=[page])]
    )
    monkeypatch.setattr("bedrock_agentcore.tools.BrowserClient", FakeBrowserClient)
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: nullcontext(SimpleNamespace(chromium=chromium)),
    )
    original_init = FakeBrowserClient.__init__

    def init(self, region):
        original_init(self, region)
        self.data_plane_client.meta.endpoint_url = "https://dp.example.test:8443/"

    monkeypatch.setattr(FakeBrowserClient, "__init__", init)

    response = client.post("/api/demos/browser", json={"url": "https://example.com"})

    assert response.status_code == 200, response.text
    ws_url = chromium.connect_over_cdp.call_args.args[0]
    assert ws_url == (
        f"wss://dp.example.test:8443/browser-streams/aws.browser.v1/sessions/{DEMO_SESSION}"
        "/automation"
    )
    assert response.json()["live_view_url"].startswith(
        f"https://dp.example.test:8443/browser-streams/aws.browser.v1/sessions/{DEMO_SESSION}"
        "/live-view?"
    )
    tools_router._stop_browser_demo_session(DEMO_SESSION)


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
    assert seen["region"] == "cn-northwest-1"  # the default workspace's region
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
                "last_saved_at": "2026-07-16T20:00:00+08:00",  # N17-obs463
                "last_saved_browser_identifier": "signed-browser-123",
            }
        ],
    }


def _commercial_workspace(workspace_id: str = "commercial-usw2") -> str:
    """A same-account workspace in a partition that offers Web Bot Auth (`aws`)."""
    db = SessionLocal()
    try:
        if db.get(Workspace, workspace_id) is None:
            db.add(
                Workspace(
                    id=workspace_id, name=workspace_id, account_id="444455556666",
                    region="us-west-2", bootstrap_status="ready", resources={},
                )
            )
            db.commit()
    finally:
        db.close()
    return workspace_id


def test_browser_demo_uses_web_bot_auth_browser_and_saves_profile_outside_china(
    client,
    monkeypatch,
):
    """Upstream behaviour kept for a partition that has browser signing (N17-obs258 gates
    it on the partition, it does not delete it)."""
    workspace_id = _commercial_workspace()
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

    FakeBrowserClient = _fake_browser_client(FakeDataPlaneClient())
    _fake_hub_credentials(monkeypatch)

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
        headers={WORKSPACE_HEADER: workspace_id},
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

    stopped = client.delete(
        f"/api/demos/browser/{body['session_id']}", headers={WORKSPACE_HEADER: workspace_id}
    )

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


def test_browser_demo_refuses_web_bot_auth_in_the_china_partition(client, monkeypatch):
    """N17-obs258: aws-cn has no Web Bot Auth signer (`CreateBrowser` with
    `browserSigning.enabled=true` → ValidationException "Browser signing configuration is
    not enabled in this environment"). A client that still asks is refused with 409 before
    any AgentCore call or browser session."""
    constructed: list = []
    monkeypatch.setattr(
        "bedrock_agentcore.tools.BrowserClient",
        lambda **kwargs: constructed.append(kwargs) or SimpleNamespace(),
    )

    def no_control_client(_ws=None):  # pragma: no cover — must not be reached
        raise AssertionError("the signing refusal must precede every AgentCore call")

    monkeypatch.setattr(tools_router, "control_client", no_control_client)

    response = client.post(
        "/api/demos/browser",
        json={
            "url": "https://example.com",
            "web_bot_auth": True,
            "browser_identifier": "signed-browser-123",
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "browser.signing_unsupported_in_partition"
    assert response.json()["detail"] == {"partition": "aws-cn"}
    assert constructed == []


def test_browser_demo_in_china_runs_the_managed_browser_with_a_profile(client, monkeypatch):
    """N17-obs258: the ordinary demo stays in aws-cn — managed `aws.browser.v1`, an
    optional READY profile — and nothing it sends carries a signing configuration."""
    class FakeControlClient:
        def get_browser(self, browserId):  # pragma: no cover — must not be reached
            raise AssertionError("no custom (signed) browser is looked up in aws-cn")

        def get_browser_profile(self, profileId):
            return {"status": "READY"}

    FakeBrowserClient = _fake_browser_client()
    _fake_hub_credentials(monkeypatch)
    page = MagicMock()
    page.title.return_value = "Example Domain"
    chromium = MagicMock()
    chromium.connect_over_cdp.return_value = SimpleNamespace(
        contexts=[SimpleNamespace(pages=[page])]
    )
    monkeypatch.setattr(tools_router, "control_client", lambda _ws=None: FakeControlClient())
    monkeypatch.setattr("bedrock_agentcore.tools.BrowserClient", FakeBrowserClient)
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: nullcontext(SimpleNamespace(chromium=chromium)),
    )

    response = client.post(
        "/api/demos/browser",
        json={"url": "https://example.com", "profile_identifier": "demo-profile-abcdefghij"},
    )

    try:
        assert response.status_code == 200
        body = response.json()
        assert body["browser_identifier"] == "aws.browser.v1"
        assert body["web_bot_auth"] is False
        started = FakeBrowserClient.instances[0].started_with
        assert started["identifier"] == "aws.browser.v1"
        assert "browserSigning" not in json.dumps(started)
    finally:
        tools_router._browser_demo_sessions.pop(body["session_id"], None)


def test_the_backend_never_creates_a_signed_browser():
    """N17-obs258: no control-plane write that could carry `browserSigning` exists — the
    only `browserSigning` the backend touches is the READ of an existing browser."""
    from pathlib import Path

    app_root = Path(tools_router.__file__).resolve().parents[1]
    offenders = []
    for path in app_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "create_browser(" in text or "update_browser(" in text:
            offenders.append(str(path.relative_to(app_root)))
        if "browserSigning" in text and path.name != "tools.py":
            offenders.append(str(path.relative_to(app_root)))
    assert offenders == []


def test_browser_signing_support_is_partition_derived():
    """One answer for the capability and the refusal: false in aws-cn, true elsewhere."""
    assert tools_router.browser_signing_supported(
        WorkspaceContext(account_id="1", region="cn-northwest-1")
    ) is False
    assert tools_router.browser_signing_supported(
        WorkspaceContext(account_id="1", region="cn-north-1")
    ) is False
    assert tools_router.browser_signing_supported(
        WorkspaceContext(account_id="1", region="us-west-2")
    ) is True
