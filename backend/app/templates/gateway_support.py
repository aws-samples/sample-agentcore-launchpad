"""Shared Gateway (MCP) support for the code-generating deploy methods.

The managed Harness reaches the shared Gateway declaratively: an
``agentcore_gateway`` tool with an ``outboundAuth`` OAuth block, and the Harness
service performs the token exchange (``app/deployer/harness.py``). A generated
runtime has no such service — it must call the Gateway itself, so the client is
*source*, rendered into the agent from ``gateway_tools.py.tmpl``.

The two halves live apart on purpose:

- **authorization** is already method-agnostic. ``agent_iam._uses_gateway()``
  keys off ``tool.type in ("gateway", "mcp")`` and never reads ``spec.method``,
  so a ``zip_runtime`` agent with a gateway ToolRef has always had the right
  execution role — including ``GetWorkloadAccessToken`` and
  ``GetResourceOauth2Token``. Nothing here changes IAM.
- **transport** is what this module wires: the environment the rendered client
  reads, and the client itself.

No new pip requirement is involved: ``strands-agents`` already depends on
``mcp<2.0.0,>=1.23.0``, so ``mcp`` and ``strands.tools.mcp.MCPClient`` are inside
every generated zip. Naming ``mcp`` explicitly would be actively harmful — see
``app/schemas/requirements.py::resolve_pins`` for the ``mcp==2.0.0`` resolution
the platform can never satisfy.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.schemas.agent import AgentSpec

GATEWAY_TEMPLATE = Path(__file__).with_name("gateway_tools.py.tmpl")

# Env keys the rendered client reads. Named here because two producers must agree:
# `deployer/environment.py` writes them and `gateway_tools.py.tmpl` reads them.
ENV_URL = "LAUNCHPAD_GATEWAY_URL"
ENV_PROVIDER = "LAUNCHPAD_GATEWAY_PROVIDER"
ENV_SCOPE = "LAUNCHPAD_GATEWAY_SCOPE"
ENV_WORKLOAD = "LAUNCHPAD_WORKLOAD_NAME"
# Comma-separated fnmatch patterns of the Gateway MCP tool names the agent may load
# (see ``gateway_tool_patterns``); absent = every tool (runtimes rendered before it).
ENV_TOOLS = "LAUNCHPAD_GATEWAY_TOOLS"


def uses_gateway(spec: AgentSpec) -> bool:
    """Whether this spec should render (and be given the env for) a Gateway client.

    Deliberately narrower than ``agent_iam._uses_gateway()``: that one also fires
    for ``type="mcp"`` (remote MCP servers, whose auth story is unresolved on the
    harness path too) and for harness knowledge bases (which ride the KB gateway).
    This is only the shared-Gateway client.
    """
    return any(tool.type == "gateway" for tool in spec.tools)


def gateway_tool_patterns(spec: AgentSpec) -> list[str]:
    """The Gateway tool names a generated runtime may load, as fnmatch patterns.

    The rendered client reaches the shared launchpad-gw, where each selected Registry
    record is one target named ``<record>___<tool>``: a ref's declared ``config.tools``
    are allowed exactly, a ref without them its ``<record>___*`` target — never the
    Gateway's other targets. Same rule as the Harness ``allowedTools`` derivation
    (``harness_tool_access.gateway_target_selectors``).
    """
    patterns: list[str] = []
    for tool in spec.tools:
        if tool.type != "gateway":
            continue
        declared = tool.config.get("tools")
        patterns.extend(declared if isinstance(declared, list) else [f"{tool.name}___*"])
    return list(dict.fromkeys(patterns))


def runtime_user_id(spec: Mapping[str, Any] | None, actor_id: str = "default") -> str | None:
    """``runtimeUserId`` for an ``InvokeAgentRuntime`` call, or None to omit it.

    Supplying it is what makes the Runtime inject a ``WorkloadAccessToken`` into
    the container request; without it a generated agent has no workload identity
    token to exchange and silently runs without its Gateway tools (measured — see
    the zip-gateway task's ``research/r1-m2m-token-path.md``).

    Returns None unless the spec actually carries a gateway ToolRef. Sending it
    unconditionally would change the invoke call for every existing agent to buy
    nothing, and it is a real identity input to AgentCore Identity, not a tag.

    Takes the stored spec dict rather than an ``AgentSpec`` so discovered/foreign
    runtimes — whose stored spec may not validate — cost nothing here.

    Also sent when any tool carries an ``auth`` block (tool-level outbound auth,
    ``templates/identity_support``): the token exchange those tools perform needs
    the same Runtime-injected workload token the Gateway client uses.
    """
    tools = (spec or {}).get("tools") or []
    needs_token = any(
        isinstance(tool, dict) and (tool.get("type") == "gateway" or tool.get("auth"))
        for tool in tools
    )
    if not needs_token:
        return None
    return (actor_id or "default")[:1024]


def provider_name(resources: Mapping[str, Any]) -> str:
    """OAuth2 credential provider NAME from its ARN.

    ``GetResourceOauth2Token`` takes the name, and the bootstrap config records the
    ARN (``…/oauth2credentialprovider/launchpad-gw-m2m``). Derived rather than
    stored as a second key, so the two can never disagree.
    """
    arn = str(resources.get("oauth_provider_arn") or "")
    return arn.rsplit("/", 1)[-1] if arn else ""


def render_gateway_source(spec: AgentSpec) -> str:
    """The Gateway client source to inline, or '' when the spec has no gateway tool."""
    if not uses_gateway(spec):
        return ""
    return GATEWAY_TEMPLATE.read_text(encoding="utf-8").strip()
