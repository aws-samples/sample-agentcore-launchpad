"""Shared knowledge-base support for the code-generating deploy methods.

The Strands ZIP fast path and the Claude Agent SDK container both mount managed
KBs through the *direct* channel: a retrieval tool baked into the generated code
that calls ``bedrock-agent-runtime:Retrieve`` with the runtime execution role.
Both templates derive their KB literal, tool description and system-prompt
section from here so the two methods cannot drift apart.

The managed Harness (方式B) uses a different channel — ``launchpad-kb-gw``
attached as an ``agentcore_gateway`` tool — and therefore has its own prompt
builder in ``app/deployer/harness.py::_kb_prompt`` naming the gateway tools.
"""

from app.schemas.agent import AgentSpec

# The tool name is part of two live contracts: the config-bundle A/B experiment
# reads tool descriptions by name, and the container exposes it namespaced as
# ``mcp__{KB_MCP_SERVER}__{KB_TOOL_NAME}``.
KB_TOOL_NAME = "kb_search"
KB_MCP_SERVER = "launchpad_kb"
KB_RESULTS = 8


def mounted_kbs(spec: AgentSpec) -> list[dict[str, str]]:
    """AgentSpec.knowledge_bases → the literal baked into the template.

    Description falls back to the name so the generated tool description and
    prompt section always say something about each KB.
    """
    return [
        {
            "kb_id": kb.kb_id,
            "name": kb.name or kb.kb_id,
            "description": kb.description or kb.name or kb.kb_id,
        }
        for kb in spec.knowledge_bases
    ]


def kb_tool_description(kbs: list[dict[str, str]]) -> str:
    """One-line description for the generated retrieval tool.

    Generated rather than hardcoded in the template so it names the actually
    mounted KBs — and so it lands in ``DEFAULT_TOOL_DESCRIPTIONS`` where the
    config-bundle contract can tune it during an A/B experiment.
    """
    if not kbs:
        return ""
    names = ", ".join(kb["name"] for kb in kbs)
    return (
        "Search the mounted managed knowledge bases and return the matching "
        f"passages with their source. Mounted: {names}. Leave kb_id empty to "
        "search all of them; pass one kb_id to target a single knowledge base."
    )


def kb_prompt_section(kbs: list[dict[str, str]], tool_name: str) -> str:
    """'## Knowledge bases' block appended to the generated system prompt.

    Same intent as the harness section, but names the direct-retrieve tool.
    Returns '' when nothing is mounted so KB-less specs render unchanged.
    """
    if not kbs:
        return ""
    lines = [
        "",
        "## Knowledge bases",
        f"A retrieval tool `{tool_name}` is mounted for you. Call it whenever a "
        "question touches the content below — do not answer from memory.",
        "Mounted knowledge bases:",
    ]
    for kb in kbs:
        lines.append(f"- {kb['name']} (kb_id `{kb['kb_id']}`) — {kb['description']}")
    lines.append(
        "Ground answers on the retrieved passages and cite their sources; say so "
        "explicitly when retrieval returns nothing relevant."
    )
    return "\n".join(lines)
