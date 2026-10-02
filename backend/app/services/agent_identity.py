"""Read-only identity view of one agent (`GET /api/agents/{id}/identity`).

Three facts, each read back from AWS where AWS holds it:

- the workload identity the Runtime auto-created (`GetAgentRuntime` →
  `workloadIdentityDetails`, then `GetWorkloadIdentity` for the allowed return
  URLs). A harness has a service-managed identity, reported as ``managed``;
- the inbound mode (the runtime's `authorizerConfiguration`: absent = IAM);
- every downstream with its acting mode and Connection: tools carrying an auth
  block, attached gateways (the agent reaches the gateway as itself through the
  platform M2M Connection), and the Connection-bound targets behind that gateway.
"""

from typing import Any

from botocore.exceptions import ClientError

from app.models.ledger import Agent
from app.schemas.inbound_auth import JWT_CAPABLE_METHODS
from app.services import identity_gateway_targets as gateway_targets
from app.services import identity_providers as connections
from app.services.agentcore.runtime import bearer_invoke_url
from app.services.gateway_bootstrap import GATEWAY_M2M_PROVIDER_NAME
from app.services.runtime_discovery import DISCOVERED_METHOD

RUNTIME_METHODS = frozenset({"zip_runtime", "container", "studio", "byoc", DISCOVERED_METHOD})


def _code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def _live_jwt(authorizer: dict[str, Any]) -> dict[str, Any]:
    """The live customJWTAuthorizer in the console's snake_case shape."""
    return {
        "discovery_url": authorizer.get("discoveryUrl", ""),
        "allowed_clients": list(authorizer.get("allowedClients") or []),
        "allowed_audience": list(authorizer.get("allowedAudience") or []),
        "allowed_scopes": list(authorizer.get("allowedScopes") or []),
        "custom_claims": [
            str(claim.get("inboundTokenClaimName") or "")
            for claim in authorizer.get("customClaims") or []
        ],
    }


def _ledger_jwt(agent: Agent) -> dict[str, Any] | None:
    if agent.inbound_auth_mode != "jwt":
        return None
    config = (agent.inbound_auth_config or {}).get("jwt")
    if not config:
        return None
    claims = [c.get("name", "") for c in config.get("custom_claims") or []]
    return {**config, "custom_claims": claims}


def _with_source_connection(live: dict[str, Any], agent: Agent) -> dict[str, Any]:
    """The live authorizer plus the display-only Connection it was picked from.

    AWS never stores the name, so it comes from the deploy-time ledger config —
    and only while that config still names the live discovery URL (an
    out-of-band edit makes the label stale)."""
    ledger = _ledger_jwt(agent) or {}
    source = ledger.get("source_connection")
    if source and ledger.get("discovery_url") == live["discovery_url"]:
        return {**live, "source_connection": source}
    return live


def _inbound_controls(agent: Agent) -> dict[str, Any]:
    """What the console's switch needs: can this agent carry a JWT authorizer,
    and is its mode pinned on the spec (else it inherits the workspace default)."""
    spec = agent.spec or {}
    pinned = (spec.get("inbound_auth") or {}).get("mode")
    return {
        "capable": agent.method in JWT_CAPABLE_METHODS and spec.get("protocol") != "a2a",
        "pinned": pinned if pinned in ("iam", "jwt") else None,
        # the deploy-time snapshot; differs from a live read only after an out-of-band edit
        "ledger_mode": agent.inbound_auth_mode or "iam",
        # the HTTPS URL a bearer caller POSTs to (the console's caller example)
        "invoke_url": _invoke_url(agent.arn),
    }


def _invoke_url(arn: str | None) -> str | None:
    parts = (arn or "").split(":")
    if len(parts) < 6 or parts[2] != "bedrock-agentcore" or "runtime/" not in parts[5]:
        return None
    return bearer_invoke_url(parts[3], arn or "")


def _workload_and_inbound(
    control: Any, agent: Agent
) -> tuple[dict[str, Any], dict[str, Any]]:
    stored_mode = agent.inbound_auth_mode or "iam"
    if agent.method == "harness":
        return (
            {"status": "managed", "name": None, "arn": None, "allowed_return_urls": []},
            {"mode": "iam", "source": "managed", "jwt": None},
        )
    if agent.method not in RUNTIME_METHODS or not agent.resource_id:
        return (
            {"status": "not_deployed", "name": None, "arn": None, "allowed_return_urls": []},
            {"mode": stored_mode, "source": "ledger", "jwt": _ledger_jwt(agent)},
        )
    try:
        runtime = control.get_agent_runtime(agentRuntimeId=agent.resource_id)
    except ClientError as exc:
        if _code(exc) != "ResourceNotFoundException":
            raise
        return (
            {"status": "missing", "name": None, "arn": None, "allowed_return_urls": []},
            {"mode": stored_mode, "source": "ledger", "jwt": _ledger_jwt(agent)},
        )
    jwt = (runtime.get("authorizerConfiguration") or {}).get("customJWTAuthorizer")
    inbound = {
        "mode": "jwt" if jwt else "iam",
        "source": "aws",
        "jwt": _with_source_connection(_live_jwt(jwt), agent) if jwt else None,
    }
    arn = (runtime.get("workloadIdentityDetails") or {}).get("workloadIdentityArn") or ""
    if not arn:
        return (
            {"status": "none", "name": None, "arn": None, "allowed_return_urls": []},
            inbound,
        )
    name = arn.rsplit("/", 1)[-1]
    urls: list[str] = []
    try:
        detail = control.get_workload_identity(name=name)
        urls = list(detail.get("allowedResourceOauth2ReturnUrls") or [])
    except ClientError as exc:
        # a service-linked identity may refuse a direct read; the ARN still stands
        if _code(exc) not in ("ResourceNotFoundException", "AccessDeniedException"):
            raise
    return (
        {"status": "ready", "name": name, "arn": arn, "allowed_return_urls": urls},
        inbound,
    )


def agent_identity(
    control: Any, agent: Agent, resources: dict[str, Any]
) -> dict[str, Any]:
    spec = agent.spec or {}
    tools = [t for t in (spec.get("tools") or []) if isinstance(t, dict)]
    workload, inbound = _workload_and_inbound(control, agent)

    live = {kind: connections.provider_names(control, kind) for kind in connections.KINDS}

    def status_of(kind: str, name: str | None) -> str:
        if not name:
            return "unbound"
        return "ready" if name in live.get(kind, set()) else "missing"

    downstreams: list[dict[str, Any]] = []
    for tool in tools:
        auth = tool.get("auth")
        if not isinstance(auth, dict):
            continue
        name = auth.get("connection") or auth.get("provider")
        kind = auth.get("kind") or (
            "api_key" if auth.get("flow") == "API_KEY" else "oauth2"
        )
        mode = auth.get("mode") or (
            "as_user" if auth.get("flow") == "USER_FEDERATION" else "as_agent"
        )
        downstreams.append({
            "type": "tool",
            "name": tool.get("name", ""),
            "tool_type": tool.get("type", ""),
            "via": "agent",
            "mode": mode,
            "connection": name,
            "kind": kind,
            "scopes": list(auth.get("scopes") or []),
            "connection_status": status_of(kind, name),
        })

    gateway_tools = [t for t in tools if t.get("type") == "gateway"]
    for tool in gateway_tools:
        downstreams.append({
            "type": "gateway",
            "name": tool.get("name", ""),
            "tool_type": "gateway",
            "via": "agent",
            "mode": "as_agent",
            "connection": GATEWAY_M2M_PROVIDER_NAME,
            "kind": "oauth2",
            "scopes": [],
            "connection_status": status_of("oauth2", GATEWAY_M2M_PROVIDER_NAME),
        })
    if gateway_tools:
        for target in gateway_targets.bound_targets_safe(control, resources):
            if not target.get("connection"):
                continue
            kind = target["auth"] if target["auth"] in connections.KINDS else "oauth2"
            downstreams.append({
                "type": "gateway_target",
                "name": target["name"],
                "tool_type": target["source"],
                "via": "gateway",
                "mode": target.get("mode") or "as_agent",
                "connection": target["connection"],
                "kind": kind,
                "scopes": target.get("scopes") or [],
                "connection_status": status_of(kind, target["connection"]),
            })

    return {
        "agent_id": agent.id,
        "name": agent.name,
        "method": agent.method,
        "workload_identity": workload,
        "inbound": {**inbound, **_inbound_controls(agent)},
        "downstreams": downstreams,
    }
