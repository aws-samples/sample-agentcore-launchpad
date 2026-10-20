"""COA context layer — the pure half shared by the deployers and the invoke chain.

An agent whose spec carries ``context_layer`` (``{namespace_id, namespace_name}``)
reads governed context from the workspace's connected COA (Context Ontology
Accelerator) deployment through COA's MCP runtime, **as the invoking member**.
The two methods get there differently (``.trellis/spec/launchpad/context-layer.md``):

- **Harness** — nothing COA-related is deployed except the prompt addendum. Every
  console turn adds one ``remote_mcp`` tool named ``coa`` to ``InvokeHarness.tools``,
  carrying the member's COA token in its ``Authorization`` header, and the
  ``@coa`` selector to ``allowedTools`` (live spike 2026-10-19: bare
  ``coa_<tool>`` selectors select nothing; the model sees ``coa_<tool>``).
- **Strands (zip_runtime)** — the deploy expands ``context_layer`` into one
  ``as_user`` MCP tool named ``coa`` bound to the layer's Connection, so the
  generated identity client fetches the member's token itself.

Gateway mode (``context_layer.via = "gateway"``, opt-in per agent) reaches COA
through the workspace's managed COA gateway instead (``services/context_layer_gateway``).
A Harness is deployed with that gateway attached as ``GATEWAY_ALIAS``; each console
turn swaps it for a ``remote_mcp`` server carrying the member's pool JWT, whose tools
the model sees as ``<alias>_coa___<tool>`` (spike F5), so the addendum names them
with ``GATEWAY_TOOL_PREFIX``. A Strands runtime is rendered with the gateway's URL
baked in (``CONTEXT_GATEWAY``, ``context_gateway_tools.py.tmpl``) and no COA tool; it
loads the gateway's ``coa___<tool>`` tools per turn with the member's pool JWT from the
payload, so the addendum names them with ``STRANDS_GATEWAY_TOOL_PREFIX``.

Nothing here touches AWS or the ledger: the prompt text derives from the spec
alone, so ``deployer/harness.build_create_params`` stays pure.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

# The MCP server / tool name both methods mount. A spec tool may not take it.
TOOL_NAME = "coa"
# ``user_grants.tool`` / ``oauth_pending_sessions.tool`` and the ``auth_required``
# event's ``tool`` for a consent asked on the console workload identity.
GRANT_TOOL = "context_layer"
# What the COA agent OAuth client is configured for (COA infra/lib/utils/agent-oauth.ts).
SCOPES = ("openid", "email", "profile")
# COA's MCP tools, every one of which takes ``namespaceId`` (all six are exposed).
COA_TOOLS = (
    "list_metrics",
    "describe_schema",
    "query",
    "translate_sparql",
    "rag_retrieval",
    "graph_traversal",
)
# Harness: one selector exposes the whole server; tools are named ``coa_<tool>``.
HARNESS_SELECTOR = f"@{TOOL_NAME}"
HARNESS_TOOL_PREFIX = f"{TOOL_NAME}_"
SUPPORTED_METHODS = ("harness", "zip_runtime")
# ``context_layer.via``: how an agent reaches COA. Direct (the default, omitted) is the
# backend / runtime fetching the member's COA token; "gateway" is the workspace's
# managed COA gateway (``services/context_layer_gateway``) with the member's pool JWT.
VIA_GATEWAY = "gateway"
VIA_DIRECT = "direct"
# Gateway mode on a Harness: the managed gateway's tool alias (one constant for the
# deploy, the per-turn swap and the prompt), its selector, and the names the model sees
# after the user-JWT swap: ``<alias>_<target>___<tool>`` (the gateway's target is ``coa``).
GATEWAY_ALIAS = "launchpad_coa_gw"
GATEWAY_SELECTOR = f"@{GATEWAY_ALIAS}"
GATEWAY_TOOL_PREFIX = f"{GATEWAY_ALIAS}_{TOOL_NAME}___"
# Gateway mode on a Strands runtime: the gateway names its target's tools
# ``<target>___<tool>`` (the target is ``coa``), and the runtime's MCP client keeps those
# names, so the model sees ``coa___<tool>``; the rendered client loads only these.
STRANDS_GATEWAY_TOOL_PREFIX = f"{TOOL_NAME}___"
STRANDS_GATEWAY_TOOLS = f"{STRANDS_GATEWAY_TOOL_PREFIX}*"
CONTEXT_GATEWAY_TEMPLATE = Path(__file__).with_name("context_gateway_tools.py.tmpl")
_CONTEXT_GATEWAY_MARKER = "__LAUNCHPAD_CONTEXT_GATEWAY__"
ADDENDUM_HEADING = "## Context layer (COA)"


def invocations_url(region: str, runtime_arn: str) -> str:
    """The data-plane URL of an AgentCore Runtime (MCP or HTTP), for bearer callers.

    The host is fixed per region; the ARN is one percent-encoded path segment.
    """
    return (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{quote(runtime_arn, safe='')}/invocations?qualifier=DEFAULT"
    )


def namespace_ref(spec: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The stored ``context_layer`` ref of a spec dict, or None. Reads the raw dict so
    discovered/foreign specs (which may not validate) cost nothing."""
    ref = (spec or {}).get("context_layer")
    if isinstance(ref, Mapping) and ref.get("namespace_id"):
        return ref
    return None


def attached(spec: Mapping[str, Any] | None) -> bool:
    """Whether the stored spec mounts the workspace's COA context layer."""
    return namespace_ref(spec) is not None


def uses_gateway(spec: Mapping[str, Any] | None) -> bool:
    """Whether the stored spec reaches the layer through the managed COA gateway
    (``context_layer.via == "gateway"``). Raw-dict read, like ``attached``: a direct
    ref is stored without ``via`` (``schemas.agent.ContextLayerRef``)."""
    ref = namespace_ref(spec)
    return ref is not None and ref.get("via") == VIA_GATEWAY


def via_gateway(spec: Mapping[str, Any] | Any) -> bool:
    """``uses_gateway`` for an ``AgentSpec`` or its stored dict."""
    if isinstance(spec, Mapping):
        return uses_gateway(spec)
    ref = getattr(spec, "context_layer", None)
    return ref is not None and bool(ref.namespace_id) and ref.via == VIA_GATEWAY


def harness_tool_prefix(spec: Mapping[str, Any] | Any) -> str:
    """How a Harness's model names COA's tools: ``coa_`` (direct, the per-turn
    ``remote_mcp`` server ``coa``) or ``launchpad_coa_gw_coa___`` (gateway mode)."""
    return GATEWAY_TOOL_PREFIX if via_gateway(spec) else HARNESS_TOOL_PREFIX


def strands_tool_prefix(spec: Mapping[str, Any] | Any) -> str:
    """How a Strands runtime's model names COA's tools: bare (direct, the as_user MCP
    tool ``coa``) or ``coa___`` (gateway mode, the managed gateway's target names)."""
    return STRANDS_GATEWAY_TOOL_PREFIX if via_gateway(spec) else ""


def bind_gateway_url(ref: Any, url: str) -> Any:
    """A copy of an ``AgentSpec.context_layer`` carrying the managed gateway's MCP URL
    for the Strands renderer (``context_layer.expanded_spec``, gateway mode only).

    A private attribute (``ContextLayerRef._gateway_url``): never serialized, so the
    stored spec, its snapshots and every API answer keep only the ref, and never
    settable by a client, so the URL the member's JWT goes to is always the ledger's."""
    bound = ref.model_copy()
    bound._gateway_url = url
    return bound


def bound_gateway_url(spec: Any) -> str:
    """The URL ``bind_gateway_url`` put on a spec's ref, or '' (not expanded / direct)."""
    ref = getattr(spec, "context_layer", None)
    return str(getattr(ref, "_gateway_url", "") or "")


def render_context_gateway_source(spec: Any) -> str:
    """The context-gateway client to inline into a Strands runtime, or '' unless the
    spec is in gateway mode. Its only baked values are identifiers:
    ``CONTEXT_GATEWAY = {"url": <the managed gateway's MCP URL>, "tools": "coa___*"}``
    (visible in the code view); the bearer is the member's JWT from each turn's payload.

    A gateway-mode spec renders only from its deploy expansion, which binds the URL: a
    bare stored spec raising here is a wiring bug, never an agent deployed without COA.
    """
    if not via_gateway(spec):
        return ""
    url = bound_gateway_url(spec)
    if not url:
        raise ValueError(
            "a gateway-mode context layer renders only from its deploy expansion "
            "(context_layer.deploy_spec), which binds the managed COA gateway's URL"
        )
    baked = {"url": url, "tools": STRANDS_GATEWAY_TOOLS}
    source = CONTEXT_GATEWAY_TEMPLATE.read_text(encoding="utf-8").strip()
    return source.replace(_CONTEXT_GATEWAY_MARKER, repr(baked))


def prompt_addendum(namespace_id: str, namespace_name: str = "", *, tool_prefix: str = "") -> str:
    """The system-prompt section an attached agent is deployed with.

    Keep the wording stable: a change only reaches an agent on its next deploy, and
    ``deployer/harness.strip_generated_prompt`` removes exactly this text from a
    prompt read back from AWS. ``tool_prefix`` is how the method names COA's tools
    (``harness_tool_prefix`` / ``strands_tool_prefix``: ``coa_`` on a Harness, none on
    a Strands runtime, and the gateway's names in gateway mode).
    """
    def tool(name: str) -> str:
        return f"`{tool_prefix}{name}`"

    label = f'"{namespace_name}" ' if namespace_name else ""
    listed = ", ".join(tool(name) for name in COA_TOOLS)
    return "\n".join([
        "",
        "",
        ADDENDUM_HEADING,
        f"Governed business context comes from the COA context layer, namespace {label}"
        f"(namespaceId `{namespace_id}`), through the tools {listed}.",
        f"- Always pass namespaceId `{namespace_id}` to every COA tool.",
        f"- For a numeric or metric question, call {tool('list_metrics')} first and prefer "
        f"a governed metric over your own calculation, then answer it with {tool('query')}.",
        f"- Use {tool('rag_retrieval')} for policy or document text, and "
        f"{tool('describe_schema')} to see which entities and properties exist.",
        "- COA answers as the signed-in user, with that user's COA permissions. If the COA "
        "tools are missing, or a COA tool answers with an authorization or access error "
        "(401, 403, forbidden, no grant), tell the user plainly that they have no access "
        "to this data in COA. Never guess or invent the answer instead.",
    ])


def addendum_for(spec: Mapping[str, Any] | Any, *, tool_prefix: str) -> str:
    """``prompt_addendum`` for a spec (an ``AgentSpec`` or its stored dict), or ''."""
    ref = spec.get("context_layer") if isinstance(spec, Mapping) else getattr(
        spec, "context_layer", None
    )
    if ref is None:
        return ""
    if isinstance(ref, Mapping):
        namespace_id = str(ref.get("namespace_id") or "")
        name = str(ref.get("namespace_name") or "")
    else:
        namespace_id, name = ref.namespace_id, ref.namespace_name
    if not namespace_id:
        return ""
    return prompt_addendum(namespace_id, name, tool_prefix=tool_prefix)


def harness_tool(mcp_url: str, access_token: str) -> dict[str, Any]:
    """The per-request ``InvokeHarness.tools`` entry carrying the member's COA token.

    Built per turn and never stored: the token rides only this request."""
    return {
        "type": "remote_mcp",
        "name": TOOL_NAME,
        "config": {
            "remoteMcp": {
                "url": mcp_url,
                "headers": {"Authorization": f"Bearer {access_token}"},
            }
        },
    }


def strands_tool_ref(mcp_url: str, connection: str) -> dict[str, Any]:
    """The ``as_user`` MCP ToolRef a Strands deploy expands ``context_layer`` into.

    Identifiers only: the generated identity client exchanges the runtime's workload
    token for the member's COA token at call time (``identity_tools.py.tmpl``)."""
    return {
        "type": "mcp",
        "name": TOOL_NAME,
        "config": {"url": mcp_url},
        "auth": {
            "connection": connection,
            "kind": "oauth2",
            "mode": "as_user",
            "scopes": list(SCOPES),
        },
    }
