"""The managed COA gateway (``context_layer_gateway``): enable (exact create shapes,
idempotent, resumable), the return-URL union, refresh, status read-back and drift,
disable (refused while referenced, ordered delete, grant cleanup), the disconnect
guard, the routes and their classification.

Hermetic: AWS is ``coa_fakes`` plus an in-memory gateway control plane below; COA's
MCP runtime is an ``httpx.MockTransport``.
"""

import copy
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.route_policy import ADMIN, MEMBER, ROUTE_POLICY
from app.models.ledger import Agent, OauthPendingSession, UserGrant
from app.models.ledger import ContextLayer as ContextLayerRow
from app.services import context_layer, user_gateways
from app.services import context_layer_gateway as gw
from app.services import context_layer_tokens as tokens
from app.services import users as users_service
from app.services.workspace import context_for_workspace

from . import coa_fakes as fx
from .conftest import client_error, set_default_resources

RETURN_URL = "http://localhost:5173/auth/return"
WS_POOL = "us-west-2_WsPool123"
CONSOLE_CLIENT = "consoleclient1"
M2M_CLIENT = "m2mclient1"
ROLE = f"arn:aws:iam::{fx.ACCOUNT}:role/launchpad-gateway-role"
RESOURCES = {
    "gateway_role_arn": ROLE,
    "user_pool_id": WS_POOL,
    "user_pool_client_id": CONSOLE_CLIENT,
    "m2m_client_id": M2M_CLIENT,
    "oauth_provider_arn": (
        f"arn:aws:bedrock-agentcore:us-west-2:{fx.ACCOUNT}:token-vault/default/"
        "oauth2credentialprovider/launchpad-gw-m2m"
    ),
    "gateway_id": "launchpad-gw-shared01",
}
DISCOVERY = (
    f"https://cognito-idp.us-west-2.amazonaws.com/{WS_POOL}/.well-known/openid-configuration"
)
ADMIN_TOKEN = fx.jwt(time.time() + 3600, sub="admin-sub", username="admin")
PROVIDER_ARN = "arn:provider/coa-coa-w2"
MCP_URL = (
    "https://bedrock-agentcore.us-east-2.amazonaws.com/runtimes/"
    "arn%3Aaws%3Abedrock-agentcore%3Aus-east-2%3A111122223333%3Aruntime%2F"
    "coa_w2_dev_mcp_server-1cRyvG5BcK/invocations?qualifier=DEFAULT"
)

# COA's own tools/list (trimmed); ``annotations`` / ``title`` are dropped by the projection
COA_TOOLS = [
    {"name": name, "title": f"{name} title", "description": f"COA {name}.",
     "inputSchema": {"type": "object", "properties": {"namespaceId": {"type": "string"}},
                     "required": ["namespaceId"]},
     "annotations": {"readOnlyHint": True}}
    for name in ("describe_schema", "graph_traversal", "list_metrics", "query",
                 "rag_retrieval", "translate_sparql")
]
PROJECTED = [
    {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
    for t in COA_TOOLS
]
TOOL_NAMES = [t["name"] for t in COA_TOOLS]


# ── the gateway control plane ────────────────────────────────────────────────


class GatewayControl(fx.FakeControl):
    """bedrock-agentcore-control gateways + targets. Gateways and targets settle
    (CREATING/UPDATING → READY, or ``gateway_final`` / ``target_final``) on the next
    read."""

    def __init__(self) -> None:
        super().__init__()
        self.gateways: dict[str, dict[str, Any]] = {}
        self.targets: dict[str, dict[str, dict[str, Any]]] = {}
        self.gateway_final = "READY"
        self.target_final = "READY"
        self.target_reasons: list[str] = []
        self.fail: dict[str, list[Exception]] = {}
        self._n = 0

    def _maybe_fail(self, op: str) -> None:
        if self.fail.get(op):
            raise self.fail[op].pop(0)

    def _gateway(self, gateway_id: str) -> dict[str, Any]:
        if gateway_id not in self.gateways:
            raise client_error("ResourceNotFoundException", "no gateway", "GetGateway")
        return self.gateways[gateway_id]

    def list_gateways(self, **kwargs):
        self.calls.append(("list_gateways", kwargs))
        return {"items": [
            {"gatewayId": g["gatewayId"], "name": g["name"], "status": g["status"]}
            for g in self.gateways.values()
        ]}

    def get_gateway(self, gatewayIdentifier):
        self.calls.append(("get_gateway", {"gatewayIdentifier": gatewayIdentifier}))
        gateway = self._gateway(gatewayIdentifier)
        out = copy.deepcopy(gateway)
        if gateway["status"] in ("CREATING", "UPDATING"):
            gateway["status"] = self.gateway_final
        return out

    def create_gateway(self, **kwargs):
        self.calls.append(("create_gateway", copy.deepcopy(kwargs)))
        self._maybe_fail("create_gateway")
        self._n += 1
        gateway_id = f"{kwargs['name']}-abc{self._n:07d}"
        self.add_gateway(gateway_id, status="CREATING", **kwargs)
        return {"gatewayId": gateway_id, "status": "CREATING"}

    def add_gateway(self, gateway_id: str, *, status: str = "READY", **config: Any) -> None:
        arn = f"arn:aws:bedrock-agentcore:us-west-2:{fx.ACCOUNT}:gateway/{gateway_id}"
        self.gateways[gateway_id] = {
            **copy.deepcopy(config),
            "gatewayId": gateway_id,
            "gatewayArn": arn,
            "gatewayUrl": f"https://{gateway_id}.gateway.bedrock-agentcore.us-west-2"
                          ".amazonaws.com/mcp",
            "status": status,
            "workloadIdentityDetails": {"workloadIdentityArn": (
                f"arn:aws:bedrock-agentcore:us-west-2:{fx.ACCOUNT}:workload-identity-directory/"
                f"default/workload-identity/{gateway_id}"
            )},
        }
        self.targets.setdefault(gateway_id, {})
        self.workloads.setdefault(gateway_id, [])

    def update_gateway(self, **kwargs):
        self.calls.append(("update_gateway", copy.deepcopy(kwargs)))
        gateway = self._gateway(kwargs["gatewayIdentifier"])
        gateway.update({k: copy.deepcopy(v) for k, v in kwargs.items() if k != "gatewayIdentifier"})
        gateway["status"] = "UPDATING"
        return {"gatewayId": gateway["gatewayId"], "status": "UPDATING"}

    def delete_gateway(self, gatewayIdentifier):
        self.calls.append(("delete_gateway", {"gatewayIdentifier": gatewayIdentifier}))
        self._maybe_fail("delete_gateway")
        self._gateway(gatewayIdentifier)
        if self.targets.get(gatewayIdentifier):
            raise client_error("ConflictException", "gateway has targets", "DeleteGateway")
        self.gateways.pop(gatewayIdentifier)
        return {"gatewayId": gatewayIdentifier, "status": "DELETING"}

    def list_gateway_targets(self, gatewayIdentifier, **kwargs):
        self.calls.append(("list_gateway_targets", {"gatewayIdentifier": gatewayIdentifier}))
        self._gateway(gatewayIdentifier)
        return {"items": [
            {"targetId": t["targetId"], "name": t["name"], "status": t["status"]}
            for t in self.targets[gatewayIdentifier].values()
        ]}

    def get_gateway_target(self, gatewayIdentifier, targetId):
        self.calls.append(("get_gateway_target", {"targetId": targetId}))
        target = self.targets[gatewayIdentifier].get(targetId)
        if target is None:
            raise client_error("ResourceNotFoundException", "no target", "GetGatewayTarget")
        out = copy.deepcopy(target)
        if target["status"] in ("CREATING", "UPDATING"):
            target["status"] = self.target_final
            target["statusReasons"] = list(self.target_reasons)
        return out

    def create_gateway_target(self, gatewayIdentifier, **kwargs):
        self.calls.append(("create_gateway_target", copy.deepcopy(kwargs)))
        self._maybe_fail("create_gateway_target")
        self._n += 1
        target_id = f"TGT{self._n:07d}"
        self.add_target(gatewayIdentifier, target_id, status="CREATING", **kwargs)
        return {"targetId": target_id, "status": "CREATING"}

    def add_target(self, gateway_id: str, target_id: str, *, status: str = "READY", **config):
        self.targets[gateway_id][target_id] = {
            **copy.deepcopy(config), "targetId": target_id, "status": status,
            "gatewayArn": self.gateways[gateway_id]["gatewayArn"],
        }

    def update_gateway_target(self, gatewayIdentifier, targetId, **kwargs):
        self.calls.append(("update_gateway_target", copy.deepcopy(kwargs)))
        target = self.targets[gatewayIdentifier][targetId]
        target.update(copy.deepcopy(kwargs))
        target["status"] = "UPDATING"
        return {"targetId": targetId, "status": "UPDATING"}

    def delete_gateway_target(self, gatewayIdentifier, targetId):
        self.calls.append(("delete_gateway_target", {"targetId": targetId}))
        self.targets[gatewayIdentifier].pop(targetId, None)
        return {"targetId": targetId, "status": "DELETING"}

    def ops_names(self) -> list[str]:
        return [op for op, _ in self.calls]


class CoaMcp:
    """COA's MCP runtime: initialize (hands out a session id), notifications, and a
    two-page tools/list, answered as SSE."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.tools: list[dict[str, Any]] = copy.deepcopy(COA_TOOLS)
        self.status = 200
        self.sse = True
        self.rpc_error: dict[str, Any] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "refused by COA"})
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                "serverInfo": {"name": "coa", "version": "1"},
            }
        elif body["method"] == "tools/list":
            if (body.get("params") or {}).get("cursor") == "page-2":
                result = {"tools": self.tools[4:]}
            else:
                result = {"tools": self.tools[:4], "nextCursor": "page-2"}
        else:
            raise AssertionError(body["method"])
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if self.rpc_error is not None:
            message = {"jsonrpc": "2.0", "id": body["id"], "error": self.rpc_error}
        headers = {"mcp-session-id": "mcp-session-1"}
        if not self.sse:
            return httpx.Response(200, headers=headers, json=message)
        return httpx.Response(
            200, headers={**headers, "content-type": "text/event-stream"},
            content=f"event: message\ndata: {json.dumps(message)}\n\n".encode(),
        )

    def methods(self) -> list[str]:
        return [json.loads(r.content).get("method") for r in self.requests]


@pytest.fixture
def coa(monkeypatch):
    mcp = CoaMcp()
    monkeypatch.setattr(gw, "_transport", lambda: httpx.MockTransport(mcp.handler))
    monkeypatch.setattr(gw, "_sleep", lambda s: None)
    return mcp


@pytest.fixture
def account(monkeypatch, client, coa):
    """A connected layer, a bootstrapped workspace, and the admin's COA consent."""
    account = fx.Account()
    account.control = GatewayControl()
    fx.install(monkeypatch, account)
    fx.connect_default(account)
    set_default_resources(RESOURCES)
    account.data.token = ADMIN_TOKEN
    account.control.calls.clear()
    account.data.calls.clear()
    return account


def _ws():
    return context_for_workspace("default")


def _run(fn, *args, **kwargs):
    db = SessionLocal()
    try:
        return fn(db, _ws(), *args, **kwargs)
    finally:
        db.close()


def _enable(username="admin"):
    return _run(gw.enable, username=username)


def _ref() -> dict | None:
    db = SessionLocal()
    try:
        row = db.query(ContextLayerRow).one()
        return (row.resources or {}).get("gateway")
    finally:
        db.close()


def _agent(via: str | None = "gateway", name: str = "gw-agent", status: str = "active") -> str:
    ref: dict[str, Any] = {"namespace_id": fx.NAMESPACE, "namespace_name": "AnyCompany HR"}
    if via:
        ref["via"] = via
    db = SessionLocal()
    try:
        row = Agent(workspace_id="default", name=name, method="harness", status=status,
                    spec={"name": name, "method": "harness", "context_layer": ref})
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _gateway_id(account) -> str:
    (gateway_id,) = account.control.gateways
    return gateway_id


# ── enable ───────────────────────────────────────────────────────────────────


def test_enable_creates_the_gateway_and_target_with_exactly_these_shapes(account, coa):
    out = _enable()
    control = account.control
    (create,) = control.ops("create_gateway")
    assert create == {
        "name": "launchpad-coa-gw",
        "description": gw.GATEWAY_DESCRIPTION,
        "roleArn": ROLE,
        "protocolType": "MCP",
        "protocolConfiguration": {"mcp": {"supportedVersions": ["2025-11-25", "2025-03-26"]}},
        "authorizerType": "CUSTOM_JWT",
        "authorizerConfiguration": {"customJWTAuthorizer": {
            "discoveryUrl": DISCOVERY, "allowedClients": [CONSOLE_CLIENT, M2M_CLIENT],
        }},
        "exceptionLevel": "DEBUG",
    }
    (target,) = control.ops("create_gateway_target")
    payload = target["targetConfiguration"]["mcp"]["mcpServer"]["mcpToolSchema"].pop(
        "inlinePayload"
    )
    assert json.loads(payload) == {"tools": PROJECTED}
    assert target == {
        "name": "coa",
        "description": gw.TARGET_DESCRIPTION,
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": MCP_URL, "mcpToolSchema": {}}}},
        "credentialProviderConfigurations": [{
            "credentialProviderType": "OAUTH",
            "credentialProvider": {"oauthCredentialProvider": {
                "providerArn": PROVIDER_ARN,
                "grantType": "AUTHORIZATION_CODE",
                "defaultReturnUrl": RETURN_URL,
                "scopes": ["openid", "email", "profile"],
            }},
        }],
    }
    gateway_id = _gateway_id(account)
    # the gateway's own workload identity allow-lists the console's return URL
    assert control.workloads[gateway_id] == [RETURN_URL]
    # identifiers only in the ledger
    (target_id,) = control.targets[gateway_id]
    assert _ref() == {
        "gateway_id": gateway_id,
        "gateway_arn": f"arn:aws:bedrock-agentcore:us-west-2:{fx.ACCOUNT}:gateway/{gateway_id}",
        "gateway_url": f"https://{gateway_id}.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
        "target_id": target_id,
    }
    # it is a gateway the workspace's member tokens may be sent to
    assert user_gateways.trusts_workspace_users(control.gateways[gateway_id], RESOURCES)
    assert out["state"] == "ready" and out["usable"] is True
    assert out["tools"] == TOOL_NAMES
    assert out["protocol_versions"] == ["2025-11-25", "2025-03-26"]
    assert out["return_url_ok"] is True and out["trusts_workspace_users"] is True
    assert out["issues"] == [] and out["agents"] == [] and out["probe_errors"] == []


def test_the_tool_schema_is_read_from_coa_as_the_admin(account, coa):
    _enable()
    assert coa.methods() == ["initialize", "notifications/initialized", "tools/list", "tools/list"]
    for request in coa.requests:
        assert str(request.url) == MCP_URL
        assert request.headers["authorization"] == f"Bearer {ADMIN_TOKEN}"
    # the session id and the negotiated version ride every call after initialize
    assert "mcp-session-id" not in coa.requests[0].headers
    assert {r.headers["mcp-session-id"] for r in coa.requests[1:]} == {"mcp-session-1"}
    assert {r.headers["mcp-protocol-version"] for r in coa.requests[1:]} == {"2025-03-26"}
    # the admin's console grant fetched it: the console workload, not the gateway's
    (fetch,) = account.data.ops("get_workload_access_token_for_user_id")
    assert fetch == {"workloadName": "launchpad-console-default", "userId": "admin"}


def test_a_second_enable_reuses_everything(account, coa):
    _enable()
    account.control.calls.clear()
    out = _enable()
    writes = {"create_gateway", "update_gateway", "create_gateway_target",
              "update_gateway_target", "update_workload_identity"}
    assert writes.isdisjoint(account.control.ops_names())
    assert len(account.control.gateways) == 1 and out["usable"] is True


def test_enable_without_the_admins_consent_creates_nothing(account, coa):
    account.data.token = None
    with pytest.raises(AppError) as exc:
        _enable()
    assert exc.value.code == "context_layer.authorization_required"
    assert exc.value.status_code == 409
    assert account.control.calls == [] and coa.requests == []
    assert _ref() is None


def test_enable_refuses_a_workspace_without_the_bootstrap_resources(account, coa):
    set_default_resources({k: v for k, v in RESOURCES.items() if k != "m2m_client_id"})
    with pytest.raises(AppError) as exc:
        _enable()
    assert exc.value.code == "context_layer.gateway_prerequisites"
    assert exc.value.detail == {"missing": ["m2m_client_id"]}
    assert account.control.calls == [] and coa.requests == []


def test_enable_resumes_after_the_target_failed(account, coa):
    account.control.fail["create_gateway_target"] = [client_error("ThrottlingException")]
    with pytest.raises(AppError) as exc:
        _enable()
    assert exc.value.code == "context_layer.gateway_failed"
    assert exc.value.detail == {"step": "target", "code": "ThrottlingException"}
    gateway_id = _gateway_id(account)
    # the gateway is already the layer's: in the ledger, so deletable and guarded
    assert _ref() == {
        "gateway_id": gateway_id,
        "gateway_arn": account.control.gateways[gateway_id]["gatewayArn"],
        "gateway_url": account.control.gateways[gateway_id]["gatewayUrl"],
        "target_id": "",
    }
    with pytest.raises(AppError) as refused:
        _run(context_layer.disconnect)
    assert refused.value.code == "context_layer.gateway_enabled"
    out = _enable()
    assert len(account.control.ops("create_gateway")) == 1  # resumed, not re-created
    assert out["state"] == "ready" and _ref()["target_id"]


def test_a_gateway_that_failed_to_settle_is_in_the_ledger_and_deletable(account, coa):
    """Recorded as soon as it exists: a gateway AWS then leaves FAILED is still the
    layer's, so the card shows it failed and can delete it."""
    account.control.gateway_final = "FAILED"
    with pytest.raises(AppError) as exc:
        _enable()
    assert exc.value.detail == {"step": "gateway", "status": "FAILED", "reasons": []}
    gateway_id = _gateway_id(account)
    assert _ref()["gateway_id"] == gateway_id
    assert account.control.ops("create_gateway_target") == []
    out = _run(gw.status)
    assert out["enabled"] is True and out["state"] == "failed"
    assert _run(gw.disable)["gateway_id"] == gateway_id
    assert account.control.gateways == {} and _ref() is None


def test_a_re_enable_re_applies_a_target_aws_left_failed(account, coa):
    account.control.target_final = "FAILED"
    with pytest.raises(AppError):
        _enable()
    account.control.target_final = "READY"
    account.control.calls.clear()
    out = _enable()
    # its configuration was already current: re-applying it is the repair
    assert len(account.control.ops("update_gateway_target")) == 1
    assert account.control.ops("create_gateway_target") == []
    assert out["state"] == "ready" and out["usable"] is True


def test_enable_adopts_and_repairs_a_gateway_and_target_found_by_name(account, coa):
    """The ledger lost them (or never got them): the gateway named launchpad-coa-gw is
    reconciled (MCP 2025-11-25 added, the clients set) and the coa target re-pointed."""
    control = account.control
    control.add_gateway(
        "launchpad-coa-gw-old0000001", name="launchpad-coa-gw", roleArn=ROLE,
        protocolType="MCP", authorizerType="CUSTOM_JWT", exceptionLevel="DEBUG",
        protocolConfiguration={"mcp": {"supportedVersions": ["2025-03-26"]}},
        authorizerConfiguration={"customJWTAuthorizer": {
            "discoveryUrl": DISCOVERY, "allowedClients": [CONSOLE_CLIENT]}},
        policyEngineConfiguration={"arn": "arn:engine", "mode": "ENFORCE"},
    )
    control.add_target(
        "launchpad-coa-gw-old0000001", "OLDTARGET1", name="coa",
        targetConfiguration={"mcp": {"mcpServer": {
            "endpoint": "https://old.example/mcp",
            "mcpToolSchema": {"inlinePayload": json.dumps({"tools": PROJECTED[:2]})},
        }}},
        credentialProviderConfigurations=gw._credentials(PROVIDER_ARN, RETURN_URL),
    )
    out = _enable()
    assert control.ops("create_gateway") == [] and control.ops("create_gateway_target") == []
    (update,) = control.ops("update_gateway")
    assert update["protocolConfiguration"] == {
        "mcp": {"supportedVersions": ["2025-11-25", "2025-03-26"]}
    }
    assert update["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"] == [
        CONSOLE_CLIENT, M2M_CLIENT,
    ]
    # replace semantics: what Launchpad does not manage is echoed back
    assert update["policyEngineConfiguration"] == {"arn": "arn:engine", "mode": "ENFORCE"}
    (target,) = control.ops("update_gateway_target")
    assert target["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] == MCP_URL
    assert _ref()["gateway_id"] == "launchpad-coa-gw-old0000001"
    assert _ref()["target_id"] == "OLDTARGET1"
    assert out["usable"] is True and out["tools"] == TOOL_NAMES


def test_the_return_url_is_added_and_nothing_removed(account, coa):
    account.control.add_gateway(
        "launchpad-coa-gw-old0000001", name="launchpad-coa-gw", roleArn=ROLE,
        protocolType="MCP", authorizerType="CUSTOM_JWT",
        protocolConfiguration=gw._protocol(),
        authorizerConfiguration=gw._authorizer({
            "discovery_url": DISCOVERY, "console_client": CONSOLE_CLIENT,
            "m2m_client": M2M_CLIENT,
        }),
    )
    account.control.workloads["launchpad-coa-gw-old0000001"] = ["https://other.example/auth/return"]
    _enable()
    assert account.control.workloads["launchpad-coa-gw-old0000001"] == sorted(
        ["https://other.example/auth/return", RETURN_URL]
    )
    assert account.control.ops("update_gateway") == []  # it already matched


def test_a_pending_auth_target_is_a_named_failure(account, coa):
    account.control.target_final = "CREATE_PENDING_AUTH"
    account.control.target_reasons = ["Waiting for user consent to sync tools"]
    with pytest.raises(AppError) as exc:
        _enable()
    assert exc.value.code == "context_layer.gateway_failed"
    assert exc.value.detail == {
        "step": "target", "status": "CREATE_PENDING_AUTH",
        "reasons": ["Waiting for user consent to sync tools"],
    }
    out = _run(gw.status)
    assert out["state"] == "failed" and out["usable"] is False
    assert out["status_reasons"] == ["Waiting for user consent to sync tools"]


def test_no_coa_token_is_stored_or_logged(account, coa, caplog):
    caplog.set_level("DEBUG")
    out = _enable()
    db = SessionLocal()
    try:
        row = db.query(ContextLayerRow).one()
        ledger = json.dumps(row.resources)
    finally:
        db.close()
    for text in (ledger, caplog.text, json.dumps(out), json.dumps(account.control.calls)):
        assert ADMIN_TOKEN not in text


# ── COA's tools/list ─────────────────────────────────────────────────────────


def test_a_json_answer_is_read_as_well_as_sse(account, coa):
    coa.sse = False
    assert _enable()["tools"] == TOOL_NAMES


def test_a_coa_401_forces_re_consent(account, coa):
    coa.status = 401
    with pytest.raises(AppError) as exc:
        _enable()
    assert exc.value.code == "context_layer.reauthorize"
    assert exc.value.status_code == 409
    assert ("default", "admin") in tokens._FORCE
    assert account.control.calls == []


@pytest.mark.parametrize("status,code,http", [
    (403, "context_layer.forbidden", 403), (500, "context_layer.upstream", 502),
])
def test_coa_refusals_create_nothing(account, coa, status, code, http):
    coa.status = status
    with pytest.raises(AppError) as exc:
        _enable()
    assert (exc.value.code, exc.value.status_code) == (code, http)
    assert account.control.calls == []


def test_a_json_rpc_error_or_no_tools_is_upstream(account, coa):
    coa.rpc_error = {"code": -32603, "message": "internal"}
    with pytest.raises(AppError) as exc:
        _enable()
    assert exc.value.code == "context_layer.upstream"
    assert exc.value.detail["rpc_code"] == -32603
    coa.rpc_error = None
    coa.tools = []
    with pytest.raises(AppError) as empty:
        _enable()
    assert empty.value.code == "context_layer.upstream"
    assert account.control.ops("create_gateway") == []


# ── refresh ──────────────────────────────────────────────────────────────────


def test_refresh_updates_the_inline_schema_only_when_coa_changed(account, coa):
    _enable()
    account.control.calls.clear()
    _run(gw.refresh_tools, username="admin")
    assert account.control.ops("update_gateway_target") == []
    coa.tools.append({"name": "new_tool", "description": "New.", "inputSchema": {"type": "object"}})
    out = _run(gw.refresh_tools, username="admin")
    (update,) = account.control.ops("update_gateway_target")
    payload = update["targetConfiguration"]["mcp"]["mcpServer"]["mcpToolSchema"]["inlinePayload"]
    assert [t["name"] for t in json.loads(payload)["tools"]] == [*TOOL_NAMES, "new_tool"]
    assert out["tools"] == [*TOOL_NAMES, "new_tool"]
    assert account.control.ops("create_gateway") == []


def test_refresh_needs_an_enabled_gateway_and_consent(account, coa):
    with pytest.raises(AppError) as exc:
        _run(gw.refresh_tools, username="admin")
    assert exc.value.code == "context_layer.gateway_not_enabled"
    _enable()
    account.data.token = None
    tokens.forget("default")
    with pytest.raises(AppError) as consent:
        _run(gw.refresh_tools, username="admin")
    assert consent.value.code == "context_layer.authorization_required"


# ── status ───────────────────────────────────────────────────────────────────


def test_status_when_not_enabled(account, coa):
    out = _run(gw.status)
    assert out["enabled"] is False and out["state"] == "disabled"
    assert out["gateway_id"] is None and out["tools"] == []
    assert account.control.calls == []


def test_status_reads_drift_back_from_aws(account, coa):
    _enable()
    gateway_id = _gateway_id(account)
    (target_id,) = account.control.targets[gateway_id]
    account.control.workloads[gateway_id] = []
    account.control.targets[gateway_id][target_id]["targetConfiguration"]["mcp"]["mcpServer"][
        "endpoint"
    ] = "https://elsewhere.example/mcp"
    out = _run(gw.status)
    assert out["state"] == "ready" and out["usable"] is False
    assert out["issues"] == ["return_url", "endpoint"]
    assert out["return_url_ok"] is False


def test_status_says_missing_when_aws_lost_the_gateway(account, coa):
    _enable()
    account.control.gateways.clear()
    out = _run(gw.status)
    assert out["enabled"] is True and out["state"] == "missing"


def test_status_says_unknown_when_aws_cannot_be_read(account, coa):
    _enable()

    def denied(**kwargs):
        raise client_error("AccessDeniedException")

    account.control.get_gateway = denied
    out = _run(gw.status)
    assert out["state"] == "unknown"
    assert out["probe_errors"] == [{"probe": "gateway", "code": "AccessDeniedException"}]


def test_status_lists_the_agents_using_gateway_mode_only(account, coa):
    gateway_agent = _agent("gateway", "hr-gw")
    _agent(None, "hr-direct")
    _agent("gateway", "gone", status="deleted")
    out = _run(gw.status)
    assert out["agents"] == [{"id": gateway_agent, "name": "hr-gw", "method": "harness"}]


def test_the_layer_status_carries_the_ledger_summary(account, coa):
    db = SessionLocal()
    try:
        before = context_layer.status(db, _ws(), username="admin", is_admin=True)
    finally:
        db.close()
    assert before["gateway"] == {"enabled": False, "gateway_id": None}
    _enable()
    account.control.calls.clear()
    db = SessionLocal()
    try:
        after = context_layer.status(db, _ws(), username="alice", is_admin=False)
    finally:
        db.close()
    assert after["gateway"] == {"enabled": True, "gateway_id": _gateway_id(account)}
    # ledger only: the layer's status never reads the gateway
    assert "get_gateway" not in account.control.ops_names()


# ── disable ──────────────────────────────────────────────────────────────────


def _grant(agent_id: str, user: str = "ada") -> None:
    db = SessionLocal()
    try:
        db.add(UserGrant(workspace_id="default", user_id=user, provider="coa-coa-w2",
                         agent_id=agent_id, tool="context_layer", status="authorized"))
        db.add(OauthPendingSession(workspace_id="default", session_uri=f"urn:{agent_id}:{user}",
                                   provider="coa-coa-w2", user_id=user, agent_id=agent_id,
                                   caller_kind="user_jwt"))
        db.commit()
    finally:
        db.close()


def test_disable_is_refused_while_an_agent_uses_gateway_mode(account, coa):
    _enable()
    agent_id = _agent("gateway", "hr-gw")
    _agent(None, "hr-direct")
    account.control.calls.clear()
    with pytest.raises(AppError) as exc:
        _run(gw.disable)
    assert exc.value.code == "context_layer.gateway_referenced"
    assert exc.value.status_code == 409
    assert exc.value.detail == {
        "referenced_by": [{"id": agent_id, "name": "hr-gw", "method": "harness"}]
    }
    assert account.control.calls == []
    assert _ref() is not None


def test_disable_deletes_the_target_then_the_gateway_and_drops_its_grants(account, coa):
    _enable()
    gateway_id = _gateway_id(account)
    _grant(gw.gateway_grant_id(gateway_id), "ada")
    _grant(gw.gateway_grant_id(gateway_id), "bob")
    _grant("", "ada")  # the console (direct) grant stays
    account.control.fail["delete_gateway"] = [client_error("ConflictException", "targets")]
    account.control.calls.clear()
    out = _run(gw.disable)
    assert out == {"disabled": True, "gateway_id": gateway_id, "grants_removed": 2}
    names = account.control.ops_names()
    assert names.index("delete_gateway_target") < names.index("delete_gateway")
    assert len(account.control.ops("delete_gateway")) == 2  # retried after the conflict
    assert account.control.gateways == {}
    assert _ref() is None
    db = SessionLocal()
    try:
        assert [(g.user_id, g.agent_id) for g in db.query(UserGrant).all()] == [("ada", "")]
        assert [s.agent_id for s in db.query(OauthPendingSession).all()] == [""]
    finally:
        db.close()
    assert _run(gw.status)["state"] == "disabled"
    _run(context_layer.disconnect)  # nothing left in the way


def test_disable_clears_the_ledger_when_aws_already_lost_the_gateway(account, coa):
    _enable()
    account.control.gateways.clear()
    out = _run(gw.disable)
    assert out["disabled"] is True and _ref() is None
    assert account.control.ops("delete_gateway") == []


def test_disable_without_a_gateway_is_refused(account, coa):
    with pytest.raises(AppError) as exc:
        _run(gw.disable)
    assert exc.value.code == "context_layer.gateway_not_enabled"


# ── the layer around it ──────────────────────────────────────────────────────


def test_reconnecting_keeps_the_gateway(account, coa):
    _enable()
    ref = _ref()
    fx.connect_default(account)
    assert _ref() == ref


def test_the_ledger_write_keeps_the_layers_updated_at(account, coa):
    db = SessionLocal()
    try:
        before = db.query(ContextLayerRow).one().updated_at
    finally:
        db.close()
    _enable()
    db = SessionLocal()
    try:
        assert db.query(ContextLayerRow).one().updated_at == before
    finally:
        db.close()


# ── HTTP ─────────────────────────────────────────────────────────────────────


def test_routes_enable_status_refresh_and_delete(client, account, coa):
    assert client.get("/api/context-layer").json()["gateway"] == {
        "enabled": False, "gateway_id": None,
    }
    enabled = client.put("/api/context-layer/gateway")
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["state"] == "ready"
    status = client.get("/api/context-layer/gateway").json()
    assert status["tools"] == TOOL_NAMES and status["return_url_ok"] is True
    assert client.get("/api/context-layer").json()["gateway"]["enabled"] is True
    refused = client.delete("/api/context-layer")
    assert refused.status_code == 409
    assert refused.json()["code"] == "context_layer.gateway_enabled"
    assert client.post("/api/context-layer/gateway/refresh").status_code == 200
    _agent("gateway", "hr-gw")
    blocked = client.delete("/api/context-layer/gateway")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "context_layer.gateway_referenced"
    assert blocked.json()["detail"]["referenced_by"][0]["name"] == "hr-gw"
    db = SessionLocal()
    try:
        db.query(Agent).delete()
        db.commit()
    finally:
        db.close()
    gone = client.delete("/api/context-layer/gateway")
    assert gone.status_code == 200 and gone.json()["disabled"] is True
    assert ADMIN_TOKEN not in enabled.text + json.dumps(status)


def test_route_enable_without_consent_answers_409(client, account, coa):
    account.data.token = None
    response = client.put("/api/context-layer/gateway")
    assert response.status_code == 409
    assert response.json()["code"] == "context_layer.authorization_required"


def test_the_routes_are_classified():
    assert ROUTE_POLICY[("GET", "/api/context-layer/gateway")] == MEMBER
    for method, path in (
        ("PUT", "/api/context-layer/gateway"),
        ("POST", "/api/context-layer/gateway/refresh"),
        ("DELETE", "/api/context-layer/gateway"),
    ):
        assert ROUTE_POLICY[(method, path)] == ADMIN


AUTH_ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", AUTH_ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", AUTH_ADMIN["password"])
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _member(app) -> TestClient:
    client = TestClient(app, client=PEER)
    creds = {"username": "ada", "password": "sufficient-pass"}
    registered = client.post("/api/auth/register", json={**creds, "email": "ada@acme-corp.com"})
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


def test_a_member_reads_the_status_but_cannot_change_the_gateway(gated, account, coa):
    from app.main import create_app

    _enable()
    app = create_app()
    set_default_resources(RESOURCES)  # init_db mirrored settings onto the row
    member = _member(app)
    status = member.get("/api/context-layer/gateway")
    assert status.status_code == 200 and status.json()["state"] == "ready"
    account.control.calls.clear()
    assert member.put("/api/context-layer/gateway").status_code == 403
    assert member.post("/api/context-layer/gateway/refresh").status_code == 403
    assert member.delete("/api/context-layer/gateway").status_code == 403
    assert account.control.calls == []
