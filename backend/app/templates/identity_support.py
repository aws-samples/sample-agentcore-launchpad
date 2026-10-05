"""Tool-level outbound auth (AgentCore Identity) for the code-generating methods.

The shared-Gateway client (``gateway_support`` / ``gateway_tools.py.tmpl``) is
one hard-wired Connection. This module generalises the same pattern to any tool
whose ``ToolRef.auth`` names a workspace Connection: the rendered
``identity_tools.py.tmpl`` block exchanges the Runtime's workload identity
token for an M2M access token (``GetResourceOauth2Token``) or an API key
(``GetResourceApiKey``) per tool, or — for ``as_user`` tools — for the invoking
user's own 3LO token (``USER_FEDERATION``), entirely inside the runtime —
Launchpad never sees the credential.

What is baked into the generated source is identifiers only: Connection
*names*, scopes, URLs, key placement. The secret material lives in the vault.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.schemas.agent import AgentSpec, ToolRef

IDENTITY_TEMPLATE = Path(__file__).with_name("identity_tools.py.tmpl")

_MARKER = "__LAUNCHPAD_IDENTITY_TOOLS__"


def auth_tools(spec: AgentSpec) -> list[ToolRef]:
    """The rest/mcp tools that carry an ``auth`` block, plus open rest tools.

    A ``rest`` tool without auth still needs the generated caller (there is no
    other way it reaches the runtime), so it rides along with empty auth; an
    ``mcp`` tool without auth keeps its existing path.
    """
    return [tool for tool in spec.tools if tool.auth is not None or tool.type == "rest"]


def uses_identity(spec: AgentSpec) -> bool:
    """Whether this spec should render the identity block."""
    return bool(auth_tools(spec))


def uses_workload_identity(spec_dict: Mapping[str, Any] | None) -> bool:
    """Whether the STORED spec carries any tool auth — the ``runtime_user_id``
    gate: the Runtime only injects a WorkloadAccessToken when runtimeUserId is
    sent, and the token exchange needs one. Reads the raw dict so
    discovered/foreign specs cost nothing."""
    tools = (spec_dict or {}).get("tools") or []
    return any(isinstance(tool, dict) and tool.get("auth") for tool in tools)


def referenced_connections(spec: AgentSpec) -> dict[str, str]:
    """{Connection name: kind} for every auth block in the spec — the IAM input."""
    return {tool.auth.connection: tool.auth.kind for tool in spec.tools if tool.auth is not None}


def _baked_tool(tool: ToolRef) -> dict[str, Any]:
    """One entry of the IDENTITY_AUTH_TOOLS literal. Flat, JSON-safe, no secrets."""
    config = tool.config or {}
    auth = tool.auth
    return {
        "name": tool.name,
        "type": tool.type,
        "url": str(config.get("url") or ""),
        "description": str(config.get("description") or ""),
        "method": str(config.get("method") or "GET"),
        "body": str(config.get("body") or ""),
        "headers": {str(k): str(v) for k, v in (config.get("headers") or {}).items()},
        "connection": auth.connection if auth else "",
        "kind": auth.kind if auth else "",
        "mode": auth.mode if auth else "",
        "scopes": list(auth.scopes) if auth else [],
        "audience": (auth.audience or "") if auth else "",
        "key_in": auth.api_key.in_ if auth and auth.api_key else "header",
        "key_name": auth.api_key.name if auth and auth.api_key else "Authorization",
    }


def render_identity_source(spec: AgentSpec) -> str:
    """The outbound-auth block to inline, or '' when no tool needs it."""
    tools = auth_tools(spec)
    if not tools:
        return ""
    source = IDENTITY_TEMPLATE.read_text(encoding="utf-8").strip()
    return source.replace(_MARKER, repr([_baked_tool(tool) for tool in tools]))


def outbound_auth_env(spec: AgentSpec) -> str:
    """LAUNCHPAD_OUTBOUND_AUTH: the auth wiring as JSON, so a BYOC agent's own
    code can perform the same exchange with the Identity SDK (generated agents
    carry it for observability only). Empty when the spec has no auth."""
    entries = [
        {
            "tool": tool.name,
            "type": tool.type,
            "connection": tool.auth.connection,
            "kind": tool.auth.kind,
            "mode": tool.auth.mode,
            "scopes": list(tool.auth.scopes),
            **({"url": str(tool.config["url"])} if tool.config.get("url") else {}),
        }
        for tool in spec.tools
        if tool.auth is not None
    ]
    return json.dumps(entries, ensure_ascii=False) if entries else ""


def as_user_connections(spec: AgentSpec) -> list[str]:
    """Connection names of the spec's ``as_user`` (3LO) tools — what makes the
    deployer allow-list the OAuth return URL on the workload identity."""
    return sorted(
        {
            tool.auth.connection
            for tool in spec.tools
            if tool.auth is not None and tool.auth.mode == "as_user"
        }
    )


def uses_as_user(spec: AgentSpec) -> bool:
    return bool(as_user_connections(spec))


def as_user_connections_stored(spec_dict: Mapping[str, Any] | None) -> list[str]:
    """Raw-dict variant for the invoke hot path (a stored spec may predate the
    current schema; the revocation lookup must still see it). Reads both the
    v2 ``{connection, mode}`` and the earlier fork's ``{provider, flow}`` shape."""
    names: set[str] = set()
    for tool in (spec_dict or {}).get("tools") or []:
        auth = tool.get("auth") if isinstance(tool, dict) else None
        if not isinstance(auth, dict):
            continue
        as_user = auth.get("mode") == "as_user" or auth.get("flow") == "USER_FEDERATION"
        name = auth.get("connection") or auth.get("provider")
        if as_user and name:
            names.add(str(name))
    return sorted(names)
