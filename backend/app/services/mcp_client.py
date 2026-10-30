"""Minimal MCP (streamable-HTTP) client for the shared gateway.

The gateway's inbound auth is AWS_IAM (SigV4): every call is signed with the
hub's own credentials via ``WorkspaceContext.credentials()`` — the same seam
``services/agentcore/gateway.py``'s wait helpers and the generated-code transport
(``templates/sigv4_gateway_transport.py.tmpl``) use, just invoked from the
console process instead of inside a Runtime container. JSON-RPC over a single
POST per call — enough for tools/list and tools/call from the console.
"""

import itertools
import json
from typing import Any

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from app.core.errors import AppError
from app.services.workspace import WorkspaceContext

_rpc_id = itertools.count(1)


def _gateway_url(workspace: WorkspaceContext) -> str:
    url = workspace.resources.get("gateway_url")
    if not url:
        raise AppError(
            "gateway.not_bootstrapped",
            "gateway_url missing from this workspace's resource map — run its bootstrap",
            status_code=503,
        )
    return str(url)


def _signed_headers(
    workspace: WorkspaceContext, body: bytes, extra: dict[str, str]
) -> dict[str, str]:
    """SigV4-sign (service ``bedrock-agentcore``) a POST of ``body`` to the gateway."""
    aws_request = AWSRequest(method="POST", url=_gateway_url(workspace), data=body, headers=extra)
    SigV4Auth(workspace.credentials(), "bedrock-agentcore", workspace.region).add_auth(aws_request)
    return dict(aws_request.headers)


# stream A2b (contract I2): the governed Gateway methods that carry the principal token
_PRINCIPAL_METHODS = ("tools/list", "tools/call")
_PRINCIPAL_META_KEY = "launchpad/principal"


def _rpc(
    workspace: WorkspaceContext, method: str, params: dict | None = None, *, timeout: float = 60,
    principal: Any | None = None,
) -> dict:
    gateway_url = _gateway_url(workspace)
    params = dict(params or {})
    if method in _PRINCIPAL_METHODS:
        from app.services import principal_mint

        token = principal_mint.mint_for_hub_call(
            workspace.id, principal, gateway_id=str(workspace.resources.get("gateway_id") or ""))
        if token:  # the interceptor on for this workspace: who asked, in params._meta
            params["_meta"] = {**(params.get("_meta") or {}), _PRINCIPAL_META_KEY: token}
    payload = {"jsonrpc": "2.0", "id": next(_rpc_id), "method": method, "params": params}
    body = json.dumps(payload).encode("utf-8")
    headers = _signed_headers(
        workspace,
        body,
        {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    response = httpx.post(gateway_url, content=body, headers=headers, timeout=timeout)
    if response.status_code in (401, 403):
        raise AppError(
            "gateway.unauthorized",
            f"gateway rejected the call ({response.status_code})",
            {"body": response.text[:300]},
            status_code=response.status_code,
        )
    response.raise_for_status()
    result = _parse_jsonrpc_body(response)
    if "error" in result:
        raise AppError("gateway.rpc_error", str(result["error"].get("message")), result["error"])
    return result.get("result", {})


def _unsigned_rpc(
    url: str, token: str | None, method: str, params: dict | None = None,
    *, timeout: float = 60,
) -> dict:
    """Unsigned JSON-RPC call to an arbitrary, member-registered MCP URL.

    Unlike ``_rpc`` (SigV4-signed, shared-gateway-only), this serves
    ``app.assistant.tool_catalog.remote_tool_names``'s probe of a Registry
    ``kind: "mcp"`` record's own ``url`` — a standalone third-party MCP server
    that is not the shared AWS_IAM gateway and in general cannot be SigV4-signed
    (it may not even be AWS-hosted). ``token``, when given, travels as a bearer
    token for a server that wants one.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {"jsonrpc": "2.0", "id": next(_rpc_id), "method": method, "params": params or {}}
    response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    if response.status_code in (401, 403):
        raise AppError(
            "gateway.unauthorized",
            f"gateway rejected the call ({response.status_code})",
            {"body": response.text[:300]},
            status_code=response.status_code,
        )
    response.raise_for_status()
    body = _parse_jsonrpc_body(response)
    if "error" in body:
        raise AppError("gateway.rpc_error", str(body["error"].get("message")), body["error"])
    return body.get("result", {})


def _parse_jsonrpc_body(response: httpx.Response) -> dict:
    text = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AppError("gateway.bad_response", "no data frame in SSE response")
    return json.loads(text) if text.strip() else {}


def tools_list(workspace: WorkspaceContext, principal: Any | None = None, *,
               timeout: float = 60) -> list[dict[str, Any]]:
    """``principal`` (stream A2b): the console user the list is for; none = the hub."""
    result = _rpc(workspace, "tools/list", principal=principal, timeout=timeout)
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list) or any(
        not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]
        for tool in tools
    ):
        raise AppError("gateway.bad_response", "tools/list returned an invalid tools collection")
    return tools


def tools_call(workspace: WorkspaceContext, name: str, arguments: dict[str, Any],
               principal: Any | None = None) -> dict[str, Any]:
    return _rpc(workspace, "tools/call", {"name": name, "arguments": arguments},
                principal=principal)
