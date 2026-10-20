"""The managed COA gateway: the workspace's context layer behind one AgentCore Gateway.

Gateway mode (``context_layer.via = "gateway"``) reaches COA through this gateway
with the asking member's workspace-pool JWT, instead of the backend (Harness) or the
runtime (Strands) fetching the member's COA token. Direct mode stays the default and
is unchanged. There is one gateway per workspace, ``launchpad-coa-gw``, created,
repaired and deleted only by an administrator from the Context Layer page;
``launchpad-gw`` is never touched. Live facts behind every choice here: task
``10-20-context-layer-gateway-spike``, ``research/findings.md`` (F1–F11).

``enable`` ensures, in order, each step reusing what already exists, so a second call
is a no-op and a call after a partial failure resumes:

1. **The COA tool schema.** COA's own ``tools/list`` on its MCP runtime, read with the
   enabling admin's console COA token (``context_layer_api.call_as_member``). Without
   that consent: 409 ``context_layer.authorization_required``, before anything is
   created. A 3LO target lists its tools only from an inline schema; without it the
   target waits in ``CREATE_PENDING_AUTH`` for a bootstrap consent (F1).
2. **The gateway.** MCP with ``supportedVersions`` ⊇ ``2025-11-25`` (the version the
   gateway's URL elicitation needs, F2); CUSTOM_JWT on the workspace pool admitting
   the console and M2M clients, no audience, no interceptor
   (``user_gateways.trusts_workspace_users``); the workspace gateway role, which needs
   ``GetWorkloadAccessTokenForJWT`` (F3). Found by name and reconciled when it
   drifted, or re-applied when AWS left it failed. Its identifiers are written to the
   ledger as soon as it exists (before any wait), so a later failure still leaves it
   visible, deletable and inside the disconnect guard.
3. **The gateway's workload identity** (the gateway creates it, named after itself):
   the console's return URL is added to ``allowedResourceOauth2ReturnUrls``, never
   removing an entry (F7).
4. **The target ``coa``.** An MCP server at the layer's MCP invocations URL with the
   inline schema; outbound ``OAUTH`` / ``AUTHORIZATION_CODE`` on the layer's
   Connection with ``defaultReturnUrl``. Never ``customParameters``: the token vault
   keys a token by them. Updated when its configuration differs or AWS left it failed
   / pending auth; it must reach READY.

The ledger keeps identifiers only (``context_layers.resources["gateway"]``); ``status``
reads everything else back from AWS. A consent made through the gateway lives in the
gateway's own vault slot, which the platform can neither re-ask nor delete (F10):
only deleting the gateway resets it, and ``disable`` also drops the grant rows keyed
``gateway:<gateway_id>``.
"""

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, aws_error_code, cleanup_error, is_not_found
from app.models.ledger import Agent, OauthPendingSession, UserGrant
from app.models.ledger import ContextLayer as ContextLayerRow
from app.schemas.context_layer import text
from app.services import context_layer, context_layer_api, identity_providers, user_gateways
from app.services.agentcore import policy as gateway_api
from app.services.agentcore.client import control_client
from app.services.context_layer import GATEWAY_KEY, ContextLayer
from app.services.workspace import WorkspaceContext
from app.templates import context_layer_support as support

logger = logging.getLogger("launchpad.context_layer.gateway")

GATEWAY_NAME = "launchpad-coa-gw"
TARGET_NAME = support.TOOL_NAME
# 2025-11-25 first: the gateway raises consent by URL elicitation only to clients that
# negotiate it (F2); the older one keeps every other MCP client working.
MCP_VERSIONS = ("2025-11-25", "2025-03-26")
# ``user_grants.agent_id`` / ``oauth_pending_sessions.agent_id`` of a gateway consent
GRANT_PREFIX = "gateway:"
# what the workspace must carry (bootstrap) for the gateway to trust its members
REQUIRED_RESOURCES = (
    "gateway_role_arn", "user_pool_id", "user_pool_client_id", "m2m_client_id",
    "oauth_provider_arn",
)
GATEWAY_DESCRIPTION = "Launchpad managed gateway: the COA context layer, as the member"
TARGET_DESCRIPTION = "COA context layer MCP server, acting as the signed-in member"

STATES = ("ready", "creating", "failed", "missing", "disabled", "unknown")
WAIT_TIMEOUT_S = 300
GONE_TIMEOUT_S = 120
POLL_S = 4
DELETE_ATTEMPTS = 6
_GATEWAY_PENDING = {"CREATING", "UPDATING"}
_TARGET_PENDING = {"CREATING", "UPDATING", "SYNCHRONIZING"}
MAX_REASONS = 5
REASON_MAX = 300

# COA's MCP runtime (tools/list)
MCP_TIMEOUT_S = 60.0
MCP_PROTOCOL = "2025-03-26"
MAX_TOOL_PAGES = 10
MAX_TOOLS = 100
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

_sleep = time.sleep  # test seam: the waits below poll AWS
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(workspace_id: str) -> threading.Lock:
    """One enable / refresh / disable at a time per workspace (a second waits, then
    finds everything in place)."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(workspace_id, threading.Lock())


def gateway_grant_id(gateway_id: str) -> str:
    """The ``agent_id`` a gateway consent's grant and pending session are keyed by."""
    return f"{GRANT_PREFIX}{gateway_id}"


# ── errors ───────────────────────────────────────────────────────────────────


def _reasons(value: Any) -> list[str]:
    """AWS ``statusReasons``, bounded (they echo configuration, never a secret)."""
    items = value if isinstance(value, list) else []
    return [reason for item in items[:MAX_REASONS] if (reason := text(item, REASON_MAX))]


def _failed(step: str, exc: Exception | None = None, **detail: Any) -> AppError:
    code = (
        aws_error_code(exc) if isinstance(exc, ClientError)
        else type(exc).__name__ if exc is not None else None
    )
    if code:
        detail["code"] = code
    what = f"{step}: {code}" if code else step
    if detail.get("status"):
        what = f"{what} is {detail['status']}"
    logger.warning("managed COA gateway: %s failed (%s)", step, code or detail.get("status"))
    return AppError(
        "context_layer.gateway_failed",
        f"the managed COA gateway could not be set up ({what})",
        {"step": step, **detail},
        status_code=502,
    )


def not_enabled() -> AppError:
    return AppError(
        "context_layer.gateway_not_enabled",
        "the managed COA gateway is not enabled in this workspace",
        status_code=409,
    )


def _prerequisites(workspace: WorkspaceContext) -> dict[str, str]:
    resources = workspace.resources
    missing = [key for key in REQUIRED_RESOURCES if not resources.get(key)]
    discovery = user_gateways.workspace_discovery_url(resources)
    if not discovery and "user_pool_id" not in missing:
        missing.append("user_pool_id")
    if missing:
        raise AppError(
            "context_layer.gateway_prerequisites",
            "this workspace has no bootstrapped gateway role, user pool or M2M client to "
            "run the managed COA gateway with",
            {"missing": missing},
            status_code=409,
        )
    return {
        "role_arn": str(resources["gateway_role_arn"]),
        "discovery_url": str(discovery),
        "console_client": str(resources["user_pool_client_id"]),
        "m2m_client": str(resources["m2m_client_id"]),
    }


# ── COA's tool schema (MCP tools/list as the admin) ──────────────────────────


def _transport() -> httpx.BaseTransport | None:
    """Seam for tests (an ``httpx.MockTransport``); None = the real network."""
    return None


def _upstream(
    message: str, status: int | None = None, http_status: int = 502, **detail: Any
) -> AppError:
    return AppError(
        "context_layer.upstream", message, {"status": status, **detail},
        status_code=http_status,
    )


class _Mcp:
    """A minimal streamable-HTTP MCP client for one ``tools/list`` read: JSON-RPC
    POSTs carrying the member's bearer, the session id the server hands out and the
    negotiated protocol version. Redirects are never followed (they would re-send the
    bearer), and errors name the HTTP status only."""

    def __init__(self, client: httpx.Client, url: str, token: str) -> None:
        self.client, self.url, self.token = client, url, token
        self.session_id: str | None = None
        self.version: str | None = None
        self._ids = 0

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.version:
            headers["MCP-Protocol-Version"] = self.version
        try:
            response = self.client.post(self.url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise _upstream("COA's MCP server did not answer in time", http_status=504) from exc
        except httpx.HTTPError as exc:
            raise _upstream(f"COA's MCP server is unreachable ({type(exc).__name__})") from exc
        if response.status_code == 401:
            raise context_layer_api.CoaUnauthorized()
        if response.status_code == 403:
            raise AppError(
                "context_layer.forbidden",
                "COA refused to list its tools to you",
                {"status": 403, "coa_message": context_layer_api.coa_message(response)},
                status_code=403,
            )
        self.session_id = response.headers.get("mcp-session-id") or self.session_id
        return response

    def notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method})  # any 2xx/4xx: best effort

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._ids += 1
        rpc_id = self._ids
        response = self._post({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})
        if response.status_code >= 300:
            raise _upstream(
                f"COA's MCP server answered HTTP {response.status_code}",
                response.status_code,
                coa_message=context_layer_api.coa_message(response),
            )
        for message in _messages(response):
            if not isinstance(message, dict) or message.get("id") != rpc_id:
                continue
            error = message.get("error")
            if isinstance(error, dict):
                raise _upstream(
                    f"COA's MCP server refused {method}", response.status_code,
                    rpc_code=error.get("code"), coa_message=text(error.get("message"), 200),
                )
            result = message.get("result")
            return result if isinstance(result, dict) else {}
        raise _upstream(f"COA's MCP server sent no answer to {method}", response.status_code)


def _messages(response: httpx.Response) -> list[Any]:
    """The JSON-RPC messages of a JSON or an SSE (``data:`` lines) answer."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        out: list[Any] = []
        for line in response.text.splitlines():
            if line.startswith("data:"):
                try:
                    out.append(json.loads(line[5:].strip()))
                except ValueError:
                    continue
        return out
    try:
        body = response.json()
    except ValueError as exc:
        raise _upstream(
            "COA's MCP server answered with a body that is not JSON", response.status_code
        ) from exc
    return body if isinstance(body, list) else [body]


def _tool(item: Any) -> dict[str, Any] | None:
    """One tool as the gateway's inline schema takes it: name, description, input
    schema — COA's own values, nothing else."""
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
        return None
    schema = item.get("inputSchema")
    description = item.get("description")
    return {
        "name": name,
        "description": description if isinstance(description, str) else "",
        "inputSchema": schema if isinstance(schema, dict) else {"type": "object"},
    }


def coa_tools(layer: ContextLayer, token: str) -> list[dict[str, Any]]:
    """COA's ``tools/list`` on its MCP runtime (initialize → initialized → every page)."""
    with httpx.Client(
        timeout=MCP_TIMEOUT_S, follow_redirects=False, transport=_transport()
    ) as client:
        mcp = _Mcp(client, layer.mcp_url, token)
        init = mcp.request("initialize", {
            "protocolVersion": MCP_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "agentcore-launchpad", "version": "1"},
        })
        mcp.version = str(init.get("protocolVersion") or MCP_PROTOCOL)
        mcp.notify("notifications/initialized")
        tools: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            result = mcp.request("tools/list", {"cursor": cursor} if cursor else {})
            for item in result.get("tools") or []:
                tool = _tool(item)
                if tool is not None and tool["name"] not in seen and len(tools) < MAX_TOOLS:
                    seen.add(tool["name"])
                    tools.append(tool)
            next_cursor = result.get("nextCursor")
            cursor = next_cursor if isinstance(next_cursor, str) and next_cursor else None
            if not cursor:
                break
        else:
            raise _upstream("COA's MCP server kept paging its tool list")
    if not tools:
        raise _upstream("COA's MCP server listed no tools")
    return tools


def read_tools(db: Session, workspace: WorkspaceContext, username: str) -> list[dict[str, Any]]:
    """COA's tools, read as ``username`` (the enabling admin): consent first."""
    return context_layer_api.call_as_member(db, workspace, username, coa_tools)


# ── AWS helpers ──────────────────────────────────────────────────────────────


def _wait(
    read: Callable[[], dict[str, Any]], *, step: str, pending: set[str],
    timeout_s: float = WAIT_TIMEOUT_S,
) -> dict[str, Any]:
    """Poll ``read()`` until READY; any status but READY or ``pending`` is a failure."""
    deadline = time.monotonic() + timeout_s
    while True:
        current = read()
        status = str(current.get("status") or "")
        if status == "READY":
            return current
        if status not in pending:
            raise _failed(step, status=status, reasons=_reasons(current.get("statusReasons")))
        if time.monotonic() >= deadline:
            raise AppError(
                "context_layer.gateway_failed",
                f"the managed COA gateway's {step} is still {status} after {int(timeout_s)} s",
                {"step": step, "status": status},
                status_code=504,
            )
        # Deliberate polling interval; the loop above owns the deadline.
        _sleep(POLL_S)  # nosemgrep: arbitrary-sleep


def _get_gateway(control: Any, gateway_id: str) -> dict[str, Any] | None:
    try:
        return gateway_api.get_gateway(control, gateway_id)
    except ClientError as exc:
        if is_not_found(exc):
            return None
        raise


def _find_gateway_id(control: Any) -> str | None:
    for summary in gateway_api.list_gateways(control):
        if summary.get("name") == GATEWAY_NAME:
            return str(summary["gatewayId"])
    return None


def _wait_gateway_gone(control: Any, gateway_id: str) -> None:
    deadline = time.monotonic() + GONE_TIMEOUT_S
    while _get_gateway(control, gateway_id) is not None:
        if time.monotonic() >= deadline:
            raise AppError(
                "context_layer.gateway_failed",
                "the previous managed COA gateway is still being deleted; try again shortly",
                {"step": "gateway", "status": "DELETING"},
                status_code=409,
            )
        _sleep(POLL_S)  # nosemgrep: arbitrary-sleep


def _authorizer(pre: dict[str, str]) -> dict[str, Any]:
    return {"customJWTAuthorizer": {
        "discoveryUrl": pre["discovery_url"],
        "allowedClients": [pre["console_client"], pre["m2m_client"]],
    }}


def _protocol(current: dict[str, Any] | None = None) -> dict[str, Any]:
    mcp = dict(((current or {}).get("protocolConfiguration") or {}).get("mcp") or {})
    extra = [v for v in mcp.get("supportedVersions") or [] if v not in MCP_VERSIONS]
    mcp["supportedVersions"] = [*MCP_VERSIONS, *extra]
    return {"mcp": mcp}


def versions(gateway: dict[str, Any]) -> list[str]:
    mcp = (gateway.get("protocolConfiguration") or {}).get("mcp") or {}
    return [str(v) for v in mcp.get("supportedVersions") or []]


def _drifted(gateway: dict[str, Any], pre: dict[str, str]) -> bool:
    jwt = (gateway.get("authorizerConfiguration") or {}).get("customJWTAuthorizer") or {}
    return (
        gateway.get("protocolType") != "MCP"
        or gateway.get("authorizerType") != "CUSTOM_JWT"
        or gateway.get("roleArn") != pre["role_arn"]
        or jwt.get("discoveryUrl") != pre["discovery_url"]
        or set(jwt.get("allowedClients") or []) != {pre["console_client"], pre["m2m_client"]}
        or bool(jwt.get("allowedAudience"))
        or not set(MCP_VERSIONS) <= set(versions(gateway))
    )


def _ensure_gateway(
    control: Any, pre: dict[str, str], known_id: str | None,
    exists: Callable[[dict[str, Any]], None],
) -> tuple[dict[str, Any], bool]:
    """The READY managed gateway (GetGateway), created or reconciled. True = created.
    The ledger's id first, then the name (a gateway a failed ledger write lost).
    ``exists`` gets the gateway as soon as it exists, before any wait or repair, so a
    gateway that then fails or times out is still in the ledger (visible, deletable)."""
    detail = _get_gateway(control, known_id) if known_id else None
    if detail is None:
        found = _find_gateway_id(control)
        detail = _get_gateway(control, found) if found else None
    if detail is not None and detail.get("status") == "DELETING":
        _wait_gateway_gone(control, str(detail["gatewayId"]))
        detail = None
    if detail is None:
        created = control.create_gateway(
            name=GATEWAY_NAME,
            description=GATEWAY_DESCRIPTION,
            roleArn=pre["role_arn"],
            protocolType="MCP",
            protocolConfiguration=_protocol(),
            authorizerType="CUSTOM_JWT",
            authorizerConfiguration=_authorizer(pre),
            exceptionLevel="DEBUG",
        )
        gateway_id = str(created["gatewayId"])
        exists(created)
        detail = _wait(
            lambda: gateway_api.get_gateway(control, gateway_id), step="gateway",
            pending=_GATEWAY_PENDING,
        )
        return detail, True
    gateway_id = str(detail["gatewayId"])
    exists(detail)
    if str(detail.get("status") or "") in _GATEWAY_PENDING:
        detail = _wait(
            lambda: gateway_api.get_gateway(control, gateway_id), step="gateway",
            pending=_GATEWAY_PENDING,
        )
    # a drifted gateway is reconciled; one AWS left FAILED / UPDATE_UNSUCCESSFUL gets
    # its configuration re-applied (what Repair on the card means)
    if _drifted(detail, pre) or detail.get("status") != "READY":
        control.update_gateway(**gateway_api.gateway_replace_params(
            detail,
            roleArn=pre["role_arn"],
            protocolType="MCP",
            protocolConfiguration=_protocol(detail),
            authorizerType="CUSTOM_JWT",
            authorizerConfiguration=_authorizer(pre),
        ))
        detail = _wait(
            lambda: gateway_api.get_gateway(control, gateway_id), step="gateway",
            pending=_GATEWAY_PENDING,
        )
    return detail, False


def workload_name(gateway: dict[str, Any]) -> str:
    """The gateway's own workload identity: the last segment of its ARN (= the id)."""
    arn = str((gateway.get("workloadIdentityDetails") or {}).get("workloadIdentityArn") or "")
    name = arn.rsplit("/", 1)[-1] if "workload-identity/" in arn else ""
    return name or str(gateway.get("gatewayId") or "")


def _ensure_return_url(control: Any, name: str, return_url: str) -> bool:
    """Add the console's return URL to the gateway workload identity (never shrink).
    The gateway owns that identity, so it is never created here."""
    identity = control.get_workload_identity(name=name)
    current = list(identity.get("allowedResourceOauth2ReturnUrls") or [])
    if return_url in current:
        return False
    control.update_workload_identity(
        name=name, allowedResourceOauth2ReturnUrls=sorted({*current, return_url})
    )
    return True


def _target_configuration(layer: ContextLayer, tools: list[dict[str, Any]]) -> dict[str, Any]:
    return {"mcp": {"mcpServer": {
        "endpoint": layer.mcp_url,
        "mcpToolSchema": {"inlinePayload": json.dumps({"tools": tools}, ensure_ascii=False)},
    }}}


def _credentials(provider_arn: str, return_url: str) -> list[dict[str, Any]]:
    return [{
        "credentialProviderType": "OAUTH",
        "credentialProvider": {"oauthCredentialProvider": {
            "providerArn": provider_arn,
            "grantType": "AUTHORIZATION_CODE",
            "defaultReturnUrl": return_url,
            "scopes": list(support.SCOPES),
        }},
    }]


def _server(target: dict[str, Any]) -> dict[str, Any]:
    return ((target.get("targetConfiguration") or {}).get("mcp") or {}).get("mcpServer") or {}


def _oauth(target: dict[str, Any]) -> dict[str, Any]:
    for config in target.get("credentialProviderConfigurations") or []:
        if isinstance(config, dict) and config.get("credentialProviderType") == "OAUTH":
            return (config.get("credentialProvider") or {}).get("oauthCredentialProvider") or {}
    return {}


def live_tools(target: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The tools in a target's inline schema (None when it has none or it is unreadable)."""
    payload = (_server(target).get("mcpToolSchema") or {}).get("inlinePayload")
    if not isinstance(payload, str):
        return None
    try:
        tools = json.loads(payload).get("tools")
    except (ValueError, AttributeError):
        return None
    return [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else None


def _target_current(
    target: dict[str, Any], layer: ContextLayer, tools: list[dict[str, Any]],
    provider_arn: str, return_url: str,
) -> bool:
    oauth = _oauth(target)
    return (
        _server(target).get("endpoint") == layer.mcp_url
        and live_tools(target) == tools
        and oauth.get("providerArn") == provider_arn
        and oauth.get("grantType") == "AUTHORIZATION_CODE"
        and oauth.get("defaultReturnUrl") == return_url
        and list(oauth.get("scopes") or []) == list(support.SCOPES)
        and not oauth.get("customParameters")
    )


def _find_target(control: Any, gateway_id: str) -> dict[str, Any] | None:
    for summary in gateway_api.list_gateway_targets(control, gateway_id):
        if summary.get("name") == TARGET_NAME:
            return summary
    return None


def _wait_target_gone(control: Any, gateway_id: str) -> None:
    deadline = time.monotonic() + GONE_TIMEOUT_S
    while (found := _find_target(control, gateway_id)) is not None:
        if found.get("status") != "DELETING" or time.monotonic() >= deadline:
            return
        _sleep(POLL_S)  # nosemgrep: arbitrary-sleep


def _ensure_target(
    control: Any, gateway_id: str, layer: ContextLayer, tools: list[dict[str, Any]],
    provider_arn: str, return_url: str,
) -> str:
    """Create, update or reuse the ``coa`` target; READY or a named failure."""
    existing = _find_target(control, gateway_id)
    if existing is not None and existing.get("status") == "DELETING":
        _wait_target_gone(control, gateway_id)
        existing = _find_target(control, gateway_id)
    params = {
        "name": TARGET_NAME,
        "description": TARGET_DESCRIPTION,
        "targetConfiguration": _target_configuration(layer, tools),
        "credentialProviderConfigurations": _credentials(provider_arn, return_url),
    }
    if existing is None:
        try:
            created = control.create_gateway_target(gatewayIdentifier=gateway_id, **params)
            target_id = str(created["targetId"])
        except ClientError as exc:
            if aws_error_code(exc) != "ConflictException":
                raise
            # a concurrent enable won the create race: adopt its target
            existing = _find_target(control, gateway_id)
            if existing is None:
                raise
            target_id = str(existing["targetId"])
    else:
        target_id = str(existing["targetId"])
        detail = gateway_api.get_gateway_target(control, gateway_id, target_id)
        target_status = str(detail.get("status") or "")
        # a stale configuration is updated; so is a current one AWS left failed or
        # waiting for a consent (re-applying it is what Repair on the card means)
        settled_badly = target_status != "READY" and target_status not in _TARGET_PENDING
        if settled_badly or not _target_current(detail, layer, tools, provider_arn, return_url):
            control.update_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target_id, **params
            )
    _wait(
        lambda: gateway_api.get_gateway_target(control, gateway_id, target_id),
        step="target", pending=_TARGET_PENDING,
    )
    return target_id


def _provider_arn(control: Any, layer: ContextLayer) -> str:
    arn = identity_providers.resolve_arn(
        control, identity_providers.KIND_OAUTH2, layer.connection
    )
    if not arn:
        raise _failed("connection", connection=layer.connection)
    return arn


def _save_ref(db: Session, workspace_id: str, ref: dict[str, str] | None) -> None:
    """Write (or clear) ``resources["gateway"]``. A Core UPDATE pinning ``updated_at``:
    the row's timestamp says when the deployment was connected, not this."""
    row = (
        db.query(ContextLayerRow).filter(ContextLayerRow.workspace_id == workspace_id).first()
    )
    if row is None:
        raise context_layer.not_connected()
    resources = {k: v for k, v in dict(row.resources or {}).items() if k != GATEWAY_KEY}
    if ref is not None:
        resources[GATEWAY_KEY] = dict(ref)
    db.execute(
        update(ContextLayerRow)
        .where(ContextLayerRow.id == row.id)
        .values(resources=resources, updated_at=row.updated_at)
    )
    db.commit()
    db.expire(row)


def _aws_step[T](step: str, call: Callable[[], T]) -> T:
    try:
        return call()
    except (ClientError, BotoCoreError) as exc:
        raise _failed(step, exc) from exc


# ── the four operations ──────────────────────────────────────────────────────


def gateway_agents(db: Session, workspace_id: str) -> list[dict[str, Any]]:
    """Live agents whose stored spec reaches COA through the managed gateway."""
    rows = (
        db.query(Agent)
        .filter(Agent.workspace_id == workspace_id, Agent.status != "deleted")
        .all()
    )
    return [
        {"id": agent.id, "name": agent.name, "method": agent.method}
        for agent in rows
        if support.uses_gateway(agent.spec)
    ]


def enable(db: Session, workspace: WorkspaceContext, *, username: str) -> dict[str, Any]:
    """Create (or repair) the managed COA gateway; idempotent and resumable (module
    docstring). Answers the live status."""
    with _lock(workspace.id):
        layer = context_layer.require(db, workspace.id)
        pre = _prerequisites(workspace)
        tools = read_tools(db, workspace, username)  # consent first: nothing is created
        return_url = get_settings().resolved_oauth_return_url()
        control = control_client(workspace)
        provider_arn = _aws_step("connection", lambda: _provider_arn(control, layer))
        known = context_layer.gateway_ref(layer) or {}
        ref: dict[str, str] = dict(known)  # what the ledger holds now

        def remember(found: dict[str, Any]) -> None:
            """The ledger write: from here on the gateway is the layer's."""
            gateway_id = str(found["gatewayId"])
            current = {
                "gateway_id": gateway_id,
                "gateway_arn": str(found.get("gatewayArn") or ""),
                "gateway_url": str(found.get("gatewayUrl") or ""),
                "target_id": (
                    str(known.get("target_id") or "")
                    if known.get("gateway_id") == gateway_id else ""
                ),
            }
            if current != ref:
                ref.clear()
                ref.update(current)
                _save_ref(db, workspace.id, ref)

        gateway, created = _aws_step(
            "gateway",
            lambda: _ensure_gateway(control, pre, known.get("gateway_id"), remember),
        )
        remember(gateway)  # the READY read-back (a create's answer may lack the URL)
        _aws_step(
            "workload_identity",
            lambda: _ensure_return_url(control, workload_name(gateway), return_url),
        )
        ref["target_id"] = _aws_step(
            "target",
            lambda: _ensure_target(
                control, ref["gateway_id"], layer, tools, provider_arn, return_url
            ),
        )
        _save_ref(db, workspace.id, ref)
        logger.info(
            "workspace %s: managed COA gateway %s %s by %s (%d tools)",
            workspace.id, ref["gateway_id"], "created" if created else "ensured", username,
            len(tools),
        )
    return status(db, workspace)


def refresh_tools(db: Session, workspace: WorkspaceContext, *, username: str) -> dict[str, Any]:
    """Re-read COA's tools (as ``username``) and update the target's inline schema
    (also re-pointing it at the layer's current MCP runtime and Connection)."""
    with _lock(workspace.id):
        layer = context_layer.require(db, workspace.id)
        ref = context_layer.gateway_ref(layer)
        if ref is None:
            raise not_enabled()
        tools = read_tools(db, workspace, username)
        return_url = get_settings().resolved_oauth_return_url()
        control = control_client(workspace)
        provider_arn = _aws_step("connection", lambda: _provider_arn(control, layer))
        target_id = _aws_step(
            "target",
            lambda: _ensure_target(
                control, ref["gateway_id"], layer, tools, provider_arn, return_url
            ),
        )
        if target_id != ref.get("target_id"):
            _save_ref(db, workspace.id, {**ref, "target_id": target_id})
        logger.info(
            "workspace %s: managed COA gateway %s tools refreshed by %s (%d tools)",
            workspace.id, ref["gateway_id"], username, len(tools),
        )
    return status(db, workspace)


def _delete_targets(control: Any, gateway_id: str) -> None:
    for target in gateway_api.list_gateway_targets(control, gateway_id):
        try:
            control.delete_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target["targetId"]
            )
        except ClientError as exc:
            if not is_not_found(exc):
                raise
    deadline = time.monotonic() + GONE_TIMEOUT_S
    while gateway_api.list_gateway_targets(control, gateway_id):
        if time.monotonic() >= deadline:
            logger.warning("managed COA gateway %s still lists targets", gateway_id)
            return
        _sleep(POLL_S)  # nosemgrep: arbitrary-sleep


def _delete_gateway(control: Any, gateway_id: str) -> None:
    """DeleteGateway, retried while the just-deleted targets drain (Conflict)."""
    for attempt in range(1, DELETE_ATTEMPTS + 1):
        try:
            control.delete_gateway(gatewayIdentifier=gateway_id)
            return
        except ClientError as exc:
            if is_not_found(exc):
                return
            if aws_error_code(exc) not in ("ConflictException", "ValidationException") or (
                attempt == DELETE_ATTEMPTS
            ):
                raise
            logger.info(
                "managed COA gateway %s busy (%s), retrying", gateway_id, cleanup_error(exc)
            )
        _sleep(POLL_S)  # nosemgrep: arbitrary-sleep


def disable(db: Session, workspace: WorkspaceContext) -> dict[str, Any]:
    """Delete the managed gateway (targets first), clear its ledger key and drop the
    gateway consents' grant rows. Refused (409 ``context_layer.gateway_referenced``)
    while any agent uses gateway mode."""
    with _lock(workspace.id):
        layer = context_layer.require(db, workspace.id)
        refs = gateway_agents(db, workspace.id)
        if refs:
            raise AppError(
                "context_layer.gateway_referenced",
                f"{len(refs)} agent(s) reach the context layer through the managed gateway; "
                "switch them to direct first",
                {"referenced_by": refs},
                status_code=409,
            )
        control = control_client(workspace)
        ref = context_layer.gateway_ref(layer)
        gateway_id = (ref or {}).get("gateway_id") or _aws_step(
            "gateway", lambda: _find_gateway_id(control)
        )
        if not gateway_id:
            raise not_enabled()
        if _aws_step("gateway", lambda: _get_gateway(control, gateway_id)) is not None:
            _aws_step("target", lambda: _delete_targets(control, gateway_id))
            _aws_step("gateway", lambda: _delete_gateway(control, gateway_id))
        _save_ref(db, workspace.id, None)
        key = gateway_grant_id(gateway_id)
        grants = (
            db.query(UserGrant)
            .filter(UserGrant.workspace_id == workspace.id, UserGrant.agent_id == key)
            .delete(synchronize_session=False)
        )
        db.query(OauthPendingSession).filter(
            OauthPendingSession.workspace_id == workspace.id, OauthPendingSession.agent_id == key
        ).delete(synchronize_session=False)
        db.commit()
        logger.info(
            "workspace %s: managed COA gateway %s deleted (%d grants dropped)",
            workspace.id, gateway_id, grants,
        )
    return {"disabled": True, "gateway_id": gateway_id, "grants_removed": grants}


# ── status (live, allow-listed) ──────────────────────────────────────────────


def _lifecycle(status: str, pending: set[str]) -> str:
    if status == "READY":
        return "ready"
    if status in pending:
        return "creating"
    if status == "DELETING":
        return "missing"
    return "failed"


def _probe(errors: list[dict[str, str]], what: str, call: Callable[[], Any]) -> Any:
    try:
        return call()
    except (ClientError, BotoCoreError) as exc:
        code = aws_error_code(exc) if isinstance(exc, ClientError) else type(exc).__name__
        errors.append({"probe": what, "code": str(code)})
        return None


def status(db: Session, workspace: WorkspaceContext) -> dict[str, Any]:
    """``GET /api/context-layer/gateway``: the managed gateway read back from AWS.

    ``state``: ``disabled`` (not enabled), ``ready``, ``creating``, ``failed`` (AWS
    reports a failed or pending-auth state, ``status_reasons``), ``missing`` (the
    ledger names a gateway or target AWS no longer has) or ``unknown`` (AWS could not
    be read, ``probe_errors``). ``issues`` lists drift a re-enable repairs:
    ``protocol``, ``authorizer``, ``endpoint``, ``connection``, ``return_url``,
    ``tools``. ``usable`` = ready with no issue. No secret, no tool schema body."""
    layer = context_layer.require(db, workspace.id)
    ref = context_layer.gateway_ref(layer)
    out: dict[str, Any] = {
        "enabled": ref is not None,
        "state": "disabled",
        "usable": False,
        "gateway_name": GATEWAY_NAME,
        "gateway_id": (ref or {}).get("gateway_id"),
        "gateway_url": (ref or {}).get("gateway_url") or None,
        "gateway_status": None,
        "target_status": None,
        "status_reasons": [],
        "tools": [],
        "protocol_versions": [],
        "return_url_ok": None,
        "trusts_workspace_users": None,
        "issues": [],
        "agents": gateway_agents(db, workspace.id),
        "probe_errors": [],
    }
    if ref is None:
        return out
    errors: list[dict[str, str]] = out["probe_errors"]
    control = control_client(workspace)
    try:
        gateway = _get_gateway(control, ref["gateway_id"])
    except (ClientError, BotoCoreError) as exc:
        code = aws_error_code(exc) if isinstance(exc, ClientError) else type(exc).__name__
        errors.append({"probe": "gateway", "code": str(code)})
        out["state"] = "unknown"
        return out
    if gateway is None:
        out["state"] = "missing"
        return out
    gateway_status = str(gateway.get("status") or "")
    out["gateway_status"] = gateway_status
    out["gateway_url"] = gateway.get("gatewayUrl") or out["gateway_url"]
    out["protocol_versions"] = versions(gateway)
    out["trusts_workspace_users"] = user_gateways.trusts_workspace_users(
        gateway, workspace.resources
    )
    out["state"] = _lifecycle(gateway_status, _GATEWAY_PENDING)
    out["status_reasons"] = _reasons(gateway.get("statusReasons"))
    issues: list[str] = out["issues"]
    if "2025-11-25" not in out["protocol_versions"]:
        issues.append("protocol")
    if not out["trusts_workspace_users"]:
        issues.append("authorizer")
    identity = _probe(
        errors, "workload_identity",
        lambda: control.get_workload_identity(name=workload_name(gateway)),
    )
    if identity is not None:
        allowed = identity.get("allowedResourceOauth2ReturnUrls") or []
        out["return_url_ok"] = get_settings().resolved_oauth_return_url() in allowed
        if not out["return_url_ok"]:
            issues.append("return_url")
    summary = _probe(errors, "target", lambda: _find_target(control, ref["gateway_id"]))
    target = (
        _probe(
            errors, "target",
            lambda: gateway_api.get_gateway_target(control, ref["gateway_id"], summary["targetId"]),
        )
        if summary is not None else None
    )
    if target is None:
        if out["state"] == "ready":
            out["state"] = "unknown" if any(e["probe"] == "target" for e in errors) else "missing"
        return out
    target_status = str(target.get("status") or "")
    out["target_status"] = target_status
    if out["state"] == "ready":
        out["state"] = _lifecycle(target_status, _TARGET_PENDING)
        out["status_reasons"] = _reasons(target.get("statusReasons"))
    out["tools"] = [str(t.get("name")) for t in live_tools(target) or [] if t.get("name")]
    if not out["tools"]:
        issues.append("tools")
    if _server(target).get("endpoint") != layer.mcp_url:
        issues.append("endpoint")
    provider = str(_oauth(target).get("providerArn") or "")
    if provider.rsplit("/", 1)[-1] != layer.connection:
        issues.append("connection")
    out["usable"] = out["state"] == "ready" and not issues
    return out
