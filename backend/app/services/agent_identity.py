"""Read-only identity view of one agent (`GET /api/agents/{id}/identity`).

Three facts, each read back from AWS where AWS holds it:

- the workload identity the Runtime auto-created (`GetAgentRuntime` →
  `workloadIdentityDetails`, then `GetWorkloadIdentity` for the allowed return
  URLs) — the self-built `harness` method reads this back like every other
  Runtime method;
- the inbound mode (AWS_IAM only in this build — see
  `app.schemas.inbound_auth`);
- every downstream with its acting mode and Connection: tools carrying an auth
  block, the attached gateway (the agent reaches the shared gateway as itself
  over SigV4, no Connection involved), and the Connection-bound targets behind
  that gateway.
"""

from typing import Any

from botocore.exceptions import ClientError

from app.models.ledger import Agent
from app.services import identity_gateway_targets as gateway_targets
from app.services import identity_providers as connections
from app.services.agentcore.runtime import bearer_invoke_url
from app.services.runtime_discovery import DISCOVERED_METHOD

RUNTIME_METHODS = frozenset(
    {"zip_runtime", "container", "studio", "byoc", "harness", DISCOVERED_METHOD}
)


def _code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def _inbound_controls(agent: Agent) -> dict[str, Any]:
    """What the console shows: this build is AWS_IAM-only, so there is nothing
    to switch — kept as a stable shape for the frontend's identity view."""
    return {
        "capable": False,
        "pinned": None,
        "ledger_mode": "iam",
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
    if agent.method not in RUNTIME_METHODS or not agent.resource_id:
        return (
            {"status": "not_deployed", "name": None, "arn": None, "allowed_return_urls": []},
            {"mode": "iam", "source": "ledger", "jwt": None},
        )
    try:
        runtime = control.get_agent_runtime(agentRuntimeId=agent.resource_id)
    except ClientError as exc:
        if _code(exc) != "ResourceNotFoundException":
            raise
        return (
            {"status": "missing", "name": None, "arn": None, "allowed_return_urls": []},
            {"mode": "iam", "source": "ledger", "jwt": None},
        )
    inbound = {"mode": "iam", "source": "aws", "jwt": None}
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
        # The agent reaches the shared gateway as itself over SigV4 (N3 W2) —
        # no Connection, no M2M OAuth2 provider; "n/a" rather than "unbound"/
        # "missing" because there is nothing here for a Connection to bind.
        downstreams.append({
            "type": "gateway",
            "name": tool.get("name", ""),
            "tool_type": "gateway",
            "via": "agent",
            "mode": "as_agent",
            "connection": None,
            "kind": "aws_iam",
            "scopes": [],
            "connection_status": "n/a",
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
