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
SIGV4_TRANSPORT_TEMPLATE = Path(__file__).with_name("sigv4_gateway_transport.py.tmpl")

# Env keys the rendered client reads. Named here because two producers must agree:
# `deployer/environment.py` writes them and `gateway_tools.py.tmpl` reads them.
ENV_URL = "LAUNCHPAD_GATEWAY_URL"
ENV_GATEWAYS = "LAUNCHPAD_GATEWAYS"
# Comma-separated fnmatch patterns of the Gateway MCP tool names the agent may load
# (see ``gateway_tool_patterns``); absent = every tool (runtimes rendered before it).
ENV_TOOLS = "LAUNCHPAD_GATEWAY_TOOLS"

# Module-scope imports the SigV4 transport block needs from its surrounding module
# (see ``sigv4_gateway_transport.py.tmpl``'s header comment — the same contract
# ``harness_convert.py`` satisfies for a converted twin's mcp_client/client.py).
# ``_LaunchpadSigV4Auth(httpx.Auth)`` is a class statement that resolves at module
# **import** time, so these cannot be function-local the way the rest of the
# fail-soft gateway block is: httpx/botocore/mcp are guaranteed present (declared
# dependencies of strands-agents / bedrock_agentcore), so this trades no real
# fail-softness away. ``boto3`` itself is already imported at module scope by
# ``main.py.tmpl``.
_SIGV4_TRANSPORT_IMPORTS = (
    "import httpx\n"
    "from botocore.auth import SigV4Auth\n"
    "from botocore.awsrequest import AWSRequest\n"
    "from mcp.client.streamable_http import streamablehttp_client\n"
)


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


def runtime_user_id(spec: Mapping[str, Any] | None, actor_id: str = "default") -> str:
    """``runtimeUserId`` for every ``InvokeAgentRuntime`` call.

    Supplying it is what makes the Runtime inject a ``WorkloadAccessToken`` into
    the container request. Pre-CN only gateway/auth-tool agents needed that token; in
    the CN build EVERY generated agent exchanges it for the Global Bedrock API key
    (``GetResourceApiKey`` on the workspace's ``model_credential`` provider) before its
    first model call, so the id is always sent. ``spec`` stays in the signature for the
    callers that pass a stored/foreign spec dict; it no longer gates anything. Bounded
    to the API's 1–1024 characters and never empty.
    """
    del spec
    return (actor_id or "default")[:1024]


def render_sigv4_transport(*, principal_carrier: bool = False) -> str:
    """The SigV4 transport block; ``principal_carrier`` (stream A3) adds the line that puts
    the turn's principal token into the request it signs — off, byte-identical to before."""
    from app.templates.principal_support import render_transport

    return render_transport(SIGV4_TRANSPORT_TEMPLATE.read_text(encoding="utf-8"),
                            enabled=principal_carrier)


def render_gateway_source(spec: AgentSpec, *, principal_carrier: bool = False) -> str:
    """The Gateway client source to inline, or '' when the spec has no gateway tool."""
    if not uses_gateway(spec):
        return ""
    transport = render_sigv4_transport(principal_carrier=principal_carrier).strip()
    client = GATEWAY_TEMPLATE.read_text(encoding="utf-8").strip()
    return f"{_SIGV4_TRANSPORT_IMPORTS}\n{transport}\n\n{client}"
