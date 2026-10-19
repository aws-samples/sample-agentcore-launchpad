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

Nothing here touches AWS or the ledger: the prompt text derives from the spec
alone, so ``deployer/harness.build_create_params`` stays pure.
"""

from collections.abc import Mapping
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


def prompt_addendum(namespace_id: str, namespace_name: str = "", *, tool_prefix: str = "") -> str:
    """The system-prompt section an attached agent is deployed with.

    Keep the wording stable: a change only reaches an agent on its next deploy, and
    ``deployer/harness.strip_generated_prompt`` removes exactly this text from a
    prompt read back from AWS. ``tool_prefix`` is how the method names COA's tools
    (``coa_`` on a Harness, none on a Strands runtime).
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
