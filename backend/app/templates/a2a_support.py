"""Shared A2A *consumer* support for the code-generating deploy methods (N11 r3-C,
N10v2-bug5).

An agent mounts other Launchpad agents by their APPROVED A2A registry records
(``ToolRef.type="a2a"``, ``config.record_id`` + ``config.runtime_arn`` resolved by the
wizard from ``GET /api/registry/attachables`` → ``agents``). Both the generated Strands
runtime and the self-built Harness bundle render ``a2a_tools.py.tmpl`` from here, so the
two methods cannot drift: one ``ask_<agent>`` tool per peer, calling the peer's Runtime
through ``bedrock-agentcore:InvokeAgentRuntime`` (SigV4 with the consumer's execution
role — ``agent_iam`` grants exactly those ARNs) with the standard A2A JSON-RPC
``message/send`` envelope.

Why not ``strands_tools.a2a_client``: the pinned ``strands-agents-tools==0.8.3`` does ship
``A2AClientToolProvider``, but it resolves the card and sends messages over plain httpx
to a URL. An AgentCore Runtime peer is AWS_IAM-inbound (D9) and only reachable through the
signed data-plane API, so that provider cannot talk to it; the minimal client in the
template is the whole integration. Contract: docs/cn/interfaces/registry-backend.md and
harness-template.md (§A2A consumer).
"""

import re
from pathlib import Path

from app.schemas.agent import AgentSpec

A2A_TEMPLATE = Path(__file__).with_name("a2a_tools.py.tmpl")
# Harness graft markers (shape of the KB / builtin grafts in harness_convert).
A2A_GRAFT_START = "# <launchpad-a2a-tools:v1>"
A2A_GRAFT_END = "# </launchpad-a2a-tools:v1>"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def a2a_tool_name(name: str) -> str:
    """``ask_<slug>`` — the model-facing function name for one mounted agent. The ref
    name is schema-validated to ``^[a-z][a-z0-9-]{0,47}$`` so the result is always a
    valid identifier."""
    return "ask_" + _SLUG_RE.sub("_", name.lower()).strip("_")


def mounted_agents(spec: AgentSpec) -> list[dict[str, str]]:
    """AgentSpec a2a ToolRefs -> the literal baked into runtime source."""
    out: list[dict[str, str]] = []
    for tool in spec.tools:
        if tool.type != "a2a":
            continue
        config = tool.config or {}
        out.append({
            "name": tool.name,
            "tool_name": a2a_tool_name(tool.name),
            "runtime_arn": str(config.get("runtime_arn") or ""),
            "record_id": str(config.get("record_id") or ""),
            "description": str(config.get("description") or "").strip()
            or f"Launchpad agent {tool.name}",
        })
    return out


def a2a_tool_description(peer: dict[str, str]) -> str:
    """Generated (not hardcoded) so it names the peer — and lands in
    ``DEFAULT_TOOL_DESCRIPTIONS`` where the config-bundle contract can tune it."""
    return (
        f"Ask the agent '{peer['name']}' a question and return its answer. "
        f"{peer['description']} Pass a complete, self-contained message: the other agent "
        "keeps its own conversation per session but sees nothing of this one."
    )


def a2a_tool_descriptions(peers: list[dict[str, str]]) -> dict[str, str]:
    return {peer["tool_name"]: a2a_tool_description(peer) for peer in peers}


def a2a_prompt_section(peers: list[dict[str, str]]) -> str:
    """'## Other agents' block appended to the generated system prompt; '' when none."""
    if not peers:
        return ""
    lines = [
        "",
        "## Other agents",
        "You can delegate to these agents through their `ask_*` tools. Delegate when a "
        "question falls in an agent's area, pass it a complete question, and relay its "
        "answer (say which agent answered). Fall back to answering yourself when the tool "
        "reports an error.",
    ]
    for peer in peers:
        lines.append(f"- `{peer['tool_name']}` → {peer['name']}: {peer['description']}")
    return "\n".join(lines)


def _tool_functions(peers: list[dict[str, str]]) -> str:
    """One plain ``@tool`` function per peer. The docstring is assigned from the baked
    literal (never spliced into source text) so a record description can carry any
    characters; the function name is the schema-validated slug."""
    blocks = []
    for index, peer in enumerate(peers):
        fn = peer["tool_name"]
        blocks.append(
            f"def {fn}(message: str) -> str:\n"
            f"    return a2a_ask(MOUNTED_AGENTS[{index}], message)\n"
            f"\n"
            f"\n"
            f"{fn}.__doc__ = _A2A_DESCRIPTIONS[{fn!r}]\n"
            f"{fn} = tool({fn})\n"
        )
    return "\n\n".join(blocks)


def render_a2a_tools_source(peers: list[dict[str, str]]) -> str:
    """The client source: inlined by generated Strands agents, written as
    ``launchpad_a2a_tools.py`` into Harness bundles. With no peers the Strands template
    still needs the two names it references, so render the inert stub instead."""
    if not peers:
        return (
            "A2A_TOOLS: list = []\n"
            "\n"
            "\n"
            "def a2a_bind_session(_session_id: str, _actor_id: str = \"\") -> None:\n"
            "    return None\n"
        )
    functions = (
        f"_A2A_DESCRIPTIONS: dict[str, str] = {a2a_tool_descriptions(peers)!r}\n\n\n"
        + _tool_functions(peers)
    )
    return (
        A2A_TEMPLATE.read_text(encoding="utf-8")
        .replace("__LAUNCHPAD_MOUNTED_AGENTS__", repr(peers))
        .replace("__LAUNCHPAD_A2A_TOOL_FUNCTIONS__", functions)
        .replace("__LAUNCHPAD_A2A_TOOL_NAMES__", ", ".join(p["tool_name"] for p in peers))
    )
