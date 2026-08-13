"""Minimal MCP (streamable-HTTP) client for the shared gateway.

The gateway's inbound auth is Cognito CUSTOM_JWT, so every call carries a
bearer token obtained with the demo user's password grant. JSON-RPC over a
single POST per call — enough for tools/list and tools/call from the console.
"""

import itertools
import json
import time
from typing import Any

import httpx
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import load_yaml_config
from app.core.errors import AppError
from app.services.workspace import WorkspaceContext

_rpc_id = itertools.count(1)
_token_cache: dict[tuple[str, str, str], Any] = {}


def _gateway_url(workspace: WorkspaceContext) -> str:
    url = workspace.resources.get("gateway_url")
    if not url:
        raise AppError(
            "gateway.not_bootstrapped",
            "gateway_url missing from this workspace's resource map — run its bootstrap",
            status_code=503,
        )
    return str(url)


def get_cognito_token(workspace: WorkspaceContext, username: str = "admin") -> str:
    """Access token for a demo user via USER_PASSWORD_AUTH (cached until expiry).

    Cached per workspace as well as per user: each environment has its own user
    pool, so a token minted in one is rejected by another's gateway.
    """
    cache_key = (workspace.account_id, workspace.region, username)
    cached = _token_cache.get(cache_key)
    if cached and cached["expires_at"] > time.time() + 60:
        return cached["token"]
    config_pw = (
        (load_yaml_config().get("demo_users") or {}).get("passwords", {}).get(username)
    )
    if not config_pw:
        raise AppError(
            "gateway.no_credentials",
            "demo user password missing — run scripts/bootstrap.py",
            status_code=503,
        )
    client = workspace.client("cognito-idp")
    try:
        resp = client.initiate_auth(
            ClientId=workspace.resources["user_pool_client_id"],
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": config_pw},
        )["AuthenticationResult"]
    except ClientError as exc:
        aws_code = exc.response.get("Error", {}).get("Code", "ClientError")
        if aws_code == "NotAuthorizedException":
            raise AppError(
                "gateway.credentials_rejected",
                "configured demo user credentials were rejected by Cognito",
                {"aws_code": aws_code},
                status_code=503,
            ) from exc
        raise AppError(
            "gateway.identity_unavailable",
            "Cognito authentication is unavailable",
            {"aws_code": aws_code},
            status_code=503,
        ) from exc
    except BotoCoreError as exc:
        raise AppError(
            "gateway.identity_unavailable",
            "Cognito authentication is unavailable",
            status_code=503,
        ) from exc
    _token_cache[cache_key] = {
        "token": resp["AccessToken"],
        "expires_at": time.time() + resp.get("ExpiresIn", 3600),
    }
    return resp["AccessToken"]


def _rpc(gateway_url: str, token: str | None, method: str, params: dict | None = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {"jsonrpc": "2.0", "id": next(_rpc_id), "method": method, "params": params or {}}
    response = httpx.post(gateway_url, json=payload, headers=headers, timeout=60)
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


def tools_list(workspace: WorkspaceContext, username: str = "admin") -> list[dict[str, Any]]:
    url = _gateway_url(workspace)
    token = get_cognito_token(workspace, username)
    result = _rpc(url, token, "tools/list")
    return result.get("tools", [])


def tools_call(
    workspace: WorkspaceContext,
    name: str,
    arguments: dict[str, Any],
    username: str = "admin",
) -> dict[str, Any]:
    url = _gateway_url(workspace)
    token = get_cognito_token(workspace, username)
    return _rpc(url, token, "tools/call", {"name": name, "arguments": arguments})
