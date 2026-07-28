"""Shared knowledge-base support for the code-generating deploy methods.

The Strands ZIP fast path and the Claude Agent SDK container both mount managed
KBs through the *direct* channel: retrieval tools baked into the generated code
that call the ``bedrock-agent-runtime`` data plane with the runtime execution
role. Two tools, deliberately both exposed:

- ``kb_search`` → ``Retrieve``: one similarity search, ~1s, no FM call.
- ``kb_deep_search`` → ``AgenticRetrieveStream``: an FM-driven planning loop that
  decomposes the question, searches every mounted KB (possibly over several
  rounds, possibly expanding to whole documents) and returns a cited answer plus
  its sources. Seconds, and one FM call per planning round.

Both templates derive their KB literal, tool descriptions and system-prompt
section from here so the two methods cannot drift apart.

The managed Harness (方式B) uses a different channel — ``launchpad-kb-gw``
attached as an ``agentcore_gateway`` tool — and therefore has its own prompt
builder in ``app/deployer/harness.py::_kb_prompt`` naming the gateway tools
(``…___Retrieve`` / ``…___AgenticRetrieveStream``).
"""

from app.schemas.agent import AgentSpec

# Tool names are part of two live contracts: the config-bundle A/B experiment
# reads tool descriptions by name, and the container exposes them namespaced as
# ``mcp__{KB_MCP_SERVER}__{tool name}``.
KB_TOOL_NAME = "kb_search"
KB_DEEP_TOOL_NAME = "kb_deep_search"
KB_MCP_SERVER = "launchpad_kb"
KB_RESULTS = 8
# maxAgentIteration for the agentic planner — AWS guidance is 3 for a single KB
# and 4–5 across several (more retrievers → more sub-queries worth planning).
KB_DEEP_ITERATIONS_SINGLE = 3
KB_DEEP_ITERATIONS_MULTI = 5


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


def _kb_names(kbs: list[dict[str, str]]) -> str:
    return ", ".join(kb["name"] for kb in kbs)


def kb_tool_description(kbs: list[dict[str, str]]) -> str:
    """One-line description for the fast single-shot retrieval tool.

    Generated rather than hardcoded in the template so it names the actually
    mounted KBs — and so it lands in ``DEFAULT_TOOL_DESCRIPTIONS`` where the
    config-bundle contract can tune it during an A/B experiment.
    """
    if not kbs:
        return ""
    return (
        "Search the mounted managed knowledge bases with a single similarity "
        f"search and return the matching passages with their source. Mounted: "
        f"{_kb_names(kbs)}. Fast and cheap — prefer it for a single fact you can "
        "name. Leave kb_id empty to search all of them; pass one kb_id to target "
        "a single knowledge base."
    )


def kb_deep_tool_description(kbs: list[dict[str, str]]) -> str:
    """One-line description for the agentic (multi-step) retrieval tool."""
    if not kbs:
        return ""
    return (
        "Deep-search the mounted managed knowledge bases: a planning loop "
        "decomposes the question into sub-queries, searches across the knowledge "
        f"bases over several rounds (pulling whole documents when a passage is "
        f"not enough) and returns a cited answer plus the supporting passages. "
        f"Mounted: {_kb_names(kbs)}. Slower and more expensive than kb_search — "
        "prefer it for comparisons, exhaustive lists, summaries, or when the "
        "evidence is spread across documents. Leave kb_id empty to search all of "
        "them; pass one kb_id to target a single knowledge base."
    )


def kb_prompt_section(kbs: list[dict[str, str]]) -> str:
    """'## Knowledge bases' block appended to the generated system prompt.

    Same intent as the harness section, but names the two direct-retrieve tools
    (and steers between them) instead of the gateway's MCP tools. Returns ''
    when nothing is mounted so KB-less specs render unchanged.
    """
    if not kbs:
        return ""
    lines = [
        "",
        "## Knowledge bases",
        "Two retrieval tools are mounted for you. Never answer from memory when a "
        "question touches the content below.",
        f"- `{KB_DEEP_TOOL_NAME}` — plans sub-queries, searches across every mounted "
        "knowledge base over several rounds, and returns a cited answer with its "
        "sources. Prefer it for comparisons, exhaustive lists, summaries, and "
        "anything whose evidence is spread across documents. Slower, costs more.",
        f"- `{KB_TOOL_NAME}` — one similarity search. Prefer it for a single fact "
        "you can name.",
        "Mounted knowledge bases:",
    ]
    for kb in kbs:
        lines.append(f"- {kb['name']} (kb_id `{kb['kb_id']}`) — {kb['description']}")
    lines.append(
        "Ground answers on the retrieved passages and cite their sources; say so "
        "explicitly when retrieval returns nothing relevant."
    )
    return "\n".join(lines)
