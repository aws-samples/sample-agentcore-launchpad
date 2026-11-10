"""Shared knowledge-base support for the code-generating deploy methods.

Knowledge bases are self-hosted on the workspace's OpenSearch domain (N6: one k-NN
index per KB, Titan v2 embeddings — Bedrock Knowledge Bases do not exist in the CN
partition). Generated Strands agents and converted Harness bundles mount them through
the *direct* channel: retrieval tools baked into the generated code that query the KB's
index with the runtime execution role, embedding the question with the same Global
Bedrock API key the agent's model uses. Two tools, deliberately both exposed (ruling
R10 — k-NN only, no FM query planner):

- ``kb_search``: one k-NN similarity search per mounted KB, ``KB_RESULTS`` wide.
- ``kb_deep_search``: a wider multi-query k-NN pass — the whole question plus up to
  three clause-split sub-queries, 2× ``KB_RESULTS``, de-duplicated by
  ``(doc_uri, chunk_index)`` — for comparisons and evidence spread across documents.

Both templates derive their KB literal, tool descriptions and system-prompt section
from here so the two methods cannot drift apart. Contract: docs/cn/interfaces/kb-runtime.md.
"""

from pathlib import Path

from app.schemas.agent import AgentSpec, KnowledgeBaseRef

# Tool names are part of two live contracts: the config-bundle A/B experiment
# reads tool descriptions by name, and the container exposes them namespaced as
# ``mcp__{KB_MCP_SERVER}__{tool name}``.
KB_TOOL_NAME = "kb_search"
KB_DEEP_TOOL_NAME = "kb_deep_search"
KB_MCP_SERVER = "launchpad_kb"
KB_RESULTS = 8
DIRECT_KB_TEMPLATE = Path(__file__).with_name("direct_kb_tools.py.tmpl")


def mounted_kb_refs(refs: list[KnowledgeBaseRef]) -> list[dict[str, str]]:
    """KnowledgeBaseRef values -> the literal baked into runtime source.

    Description falls back to the name so the generated tool description and
    prompt section always say something about each KB.
    """
    return [
        {
            "kb_id": kb.kb_id,
            "name": kb.name or kb.kb_id,
            "description": kb.description or kb.name or kb.kb_id,
        }
        for kb in refs
    ]


def mounted_kbs(spec: AgentSpec) -> list[dict[str, str]]:
    """AgentSpec.knowledge_bases -> the literal baked into the template."""
    return mounted_kb_refs(spec.knowledge_bases)


def render_direct_kb_source(kbs: list[dict[str, str]]) -> str:
    """Render the standalone source also inlined by generated ZIP agents."""
    return (
        DIRECT_KB_TEMPLATE.read_text(encoding="utf-8")
        .replace("__LAUNCHPAD_MOUNTED_KBS__", repr(kbs))
        .replace("__LAUNCHPAD_KB_RESULTS__", repr(KB_RESULTS))
    )


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
        "Search the mounted knowledge bases with one k-NN similarity search over "
        "their indexed passages and return the matches with their source. Mounted: "
        f"{_kb_names(kbs)}. Fast and cheap — prefer it for a single fact you can "
        "name. Leave kb_id empty to search all of them; pass one kb_id to target "
        "a single knowledge base."
    )


def kb_deep_tool_description(kbs: list[dict[str, str]]) -> str:
    """One-line description for the wider multi-query retrieval tool."""
    if not kbs:
        return ""
    return (
        "Deep-search the mounted knowledge bases: a multi-query k-NN pass (the "
        "question plus its clauses, a wider result set, de-duplicated) that returns "
        f"the best-scoring passages with their sources. Mounted: {_kb_names(kbs)}. "
        "Slower than kb_search — prefer it for comparisons, exhaustive lists or "
        "evidence spread across documents. Leave kb_id empty to search all of "
        "them; pass one kb_id to target a single knowledge base."
    )


def kb_prompt_section(kbs: list[dict[str, str]]) -> str:
    """'## Knowledge bases' block appended to the generated system prompt.

    Names the two direct-retrieve tools and steers between them. Returns ''
    when nothing is mounted so KB-less specs render unchanged.
    """
    if not kbs:
        return ""
    lines = [
        "",
        "## Knowledge bases",
        "Two retrieval tools are mounted for you. Never answer from memory when a "
        "question touches the content below.",
        f"- `{KB_DEEP_TOOL_NAME}` — runs several similarity searches (the question "
        "plus its clauses) across every mounted knowledge base and returns the "
        "best-scoring passages with their sources, de-duplicated. Prefer it for "
        "comparisons, exhaustive lists, and anything whose evidence is spread "
        "across documents. Slower.",
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
