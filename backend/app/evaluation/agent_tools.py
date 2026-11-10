"""The tool names an agent calls at runtime, from its spec (N17-obs407, eval Round 3 D3-a).

A scenario's ``expected_trajectory`` is matched against the tool names in the agent's spans,
so a name the agent does not have can never match (DIFF-GLOBAL-EVAL B4: a dataset said
``hr-database___get_employee``; the Harness agent calls it
``launchpad_gw_hr-database___get_employee``). The names come from the same sources the
renderers use, per method:

- ``harness``: the selected native tools, ``BUILTIN_RUNTIME_TOOLS`` of each builtin ref,
  ``skills`` when Skills are mounted, the KB retrieval tools, ``ask_<peer>`` per A2A peer, a
  Gateway ref's declared ``config.tools`` behind the ``launchpad_gw_`` alias (an undeclared
  ref: its ``launchpad_gw_<record>___*`` target), and a remote MCP server's ``<server>_*``;
- ``zip_runtime``: the toolkits' functions, the KB retrieval tools, ``ask_<peer>``, a rest
  tool's function name and the Gateway names WITHOUT the alias (the rendered client loads
  ``<record>___<tool>`` as is).

Everything else (a spec without a ``method``, ``studio`` canvas code, ``byoc``, ``container``,
a ``zip_runtime`` spec with its own ``code`` / ``code_bundle`` or an MCP server) defines tools
the platform does not render, so the catalogue is OPEN and nothing is refused.
"""

from __future__ import annotations

import difflib
import fnmatch
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.assistant.tool_catalog import GATEWAY_ALIAS
from app.harness_tool_access import (
    BUILTIN_RUNTIME_TOOLS,
    gateway_tool_names,
    selected_native_tools,
)
from app.templates.a2a_support import a2a_tool_name
from app.templates.kb_support import KB_DEEP_TOOL_NAME, KB_TOOL_NAME
from app.templates.toolkits import toolkit_tool_names

GATEWAY_PREFIX = f"{GATEWAY_ALIAS}_"
SUGGESTIONS = 3


@dataclass(frozen=True)
class AgentTools:
    """``names`` called exactly, ``patterns`` (fnmatch) for tools only known by their
    target / server prefix, ``open`` when the platform cannot list every tool."""

    method: str
    names: frozenset[str]
    patterns: tuple[str, ...] = ()
    open: bool = False

    def knows(self, name: str) -> bool:
        return name in self.names or any(fnmatch.fnmatchcase(name, p) for p in self.patterns)

    def canonical(self, name: str) -> str | None:
        """``name`` as the agent calls it: itself when known; else the one Gateway spelling
        that is known — the alias prefix added (a Harness agent) or dropped (a generated
        runtime). None when neither is."""
        if self.knows(name):
            return name
        other = (name[len(GATEWAY_PREFIX):] if name.startswith(GATEWAY_PREFIX)
                 else GATEWAY_PREFIX + name)
        return other if other and self.knows(other) else None

    def listing(self) -> list[str]:
        return sorted(self.names) + list(self.patterns)

    def suggest(self, name: str) -> list[str]:
        return difflib.get_close_matches(name, sorted(self.names), n=SUGGESTIONS, cutoff=0.5)


def _refs(spec: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    return [t for t in spec.get("tools") or [] if isinstance(t, Mapping) and t.get("type") == kind]


def _gateway(spec: Mapping[str, Any], prefix: str) -> tuple[set[str], list[str]]:
    names: set[str] = set()
    patterns: list[str] = []
    for ref in _refs(spec, "gateway"):
        declared = gateway_tool_names(ref)
        if declared is None:
            patterns.append(f"{prefix}{ref.get('name')}___*")
        else:
            names.update(prefix + name for name in declared)
    return names, patterns


def _common(spec: Mapping[str, Any]) -> set[str]:
    names = {a2a_tool_name(str(ref.get("name"))) for ref in _refs(spec, "a2a") if ref.get("name")}
    if spec.get("knowledge_bases"):
        names.update((KB_TOOL_NAME, KB_DEEP_TOOL_NAME))
    return names


def agent_tools(spec: Mapping[str, Any] | None) -> AgentTools:
    """The agent's runtime tool catalogue (see the module docstring). A spec that does not
    state its ``method`` (an imported / discovered runtime, a pre-spec row) is not one the
    platform rendered, so it is OPEN too."""
    spec = spec or {}
    method = str(spec.get("method") or "")
    if method == "harness":
        names = _common(spec) | selected_native_tools(spec)
        for ref in _refs(spec, "builtin"):
            names.update(BUILTIN_RUNTIME_TOOLS.get(str(ref.get("name")), ()))
        if spec.get("skills"):
            names.add("skills")
        gateway, patterns = _gateway(spec, GATEWAY_PREFIX)
        patterns += [f"{ref.get('name')}_*" for ref in _refs(spec, "mcp")]
        return AgentTools(method, frozenset(names | gateway), tuple(patterns))
    if method == "zip_runtime" and not spec.get("code") and not spec.get("code_bundle") \
            and not _refs(spec, "mcp"):
        names = _common(spec) | set(toolkit_tool_names(list(spec.get("toolkits") or [])))
        names.update(str(ref.get("name")).replace("-", "_") for ref in _refs(spec, "rest"))
        gateway, patterns = _gateway(spec, "")
        return AgentTools(method, frozenset(names | gateway), tuple(patterns))
    return AgentTools(method, frozenset(), (), open=True)


@dataclass
class TrajectoryCheck:
    """What :func:`check_trajectories` found: the items with canonical names, the rewrites
    it made ``[{scenario_id, from, to}]`` and the names no spelling of which the agent has
    ``{scenario_id: [name]}``."""

    items: list[dict[str, Any]]
    rewrites: list[dict[str, str]]
    unknown: dict[str, list[str]]

    def detail(self, tools: AgentTools) -> dict[str, Any]:
        names = sorted({n for ns in self.unknown.values() for n in ns})
        return {"unknown": self.unknown, "agent_tools": tools.listing(),
                "suggestions": {n: tools.suggest(n) for n in names}}


def check_trajectories(items: Iterable[dict[str, Any]], tools: AgentTools) -> TrajectoryCheck:
    """Each scenario's ``expected_trajectory`` against ``tools``: a known name stays, a name
    whose other Gateway spelling is known is rewritten to it (the platform records the
    canonical one), anything else is unknown. An OPEN catalogue checks nothing."""
    out: list[dict[str, Any]] = []
    rewrites: list[dict[str, str]] = []
    unknown: dict[str, list[str]] = {}
    for index, item in enumerate(items, 1):
        trajectory = item.get("expected_trajectory")
        if tools.open or not isinstance(trajectory, list) or not trajectory:
            out.append(item)
            continue
        scenario_id = str(item.get("scenario_id") or f"item_{index}")
        resolved: list[Any] = []
        for name in trajectory:
            canonical = tools.canonical(str(name))
            if canonical is None:
                unknown.setdefault(scenario_id, []).append(str(name))
                resolved.append(name)
                continue
            if canonical != name:
                rewrites.append({"scenario_id": scenario_id, "from": str(name), "to": canonical})
            resolved.append(canonical)
        out.append({**item, "expected_trajectory": resolved} if resolved != trajectory else item)
    return TrajectoryCheck(out, rewrites, unknown)
