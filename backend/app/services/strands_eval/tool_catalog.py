"""The agent's tool catalogue for ``available_tools`` (N17-obs334).

Global AgentCore Evaluations gives the tool-level judges (ToolSelectionAccuracy …) and the
``{available_tools}`` placeholder EVERY tool the agent had, with its description. The
strands-evals CloudWatch mapper lists only the tools a trace CALLED, so a judge asked "was
this the right tool?" never saw the alternatives (ToolSelection inflated). Sources, merged
per session by tool name:

- ``gen_ai.agent.tools`` on the ``invoke_agent`` span — the agent's tool names (Strands
  serialises a JSON list of names; a list of ``{name, description}`` objects is accepted);
- ``gen_ai.tool.definitions`` on the ``invoke_agent`` span (Strands' opt-in
  ``gen_ai_tool_definitions``) — ``[{name, description, inputSchema}]``;
- ``gen_ai.tool.description`` / ``gen_ai.tool.json_schema`` on each ``execute_tool`` span.

Span attributes live in ``aws/spans``: with ``spans_source=runtime_logs`` there are none and
the catalogue is empty — the caller then falls back to the tools the trace called.

Pure library (shipped into the worker zip): imports nothing from ``app.*``.
"""

from __future__ import annotations

import json
from typing import Any

from strands_evals.types.trace import ToolConfig


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _operation(span: dict) -> str:
    attributes = span.get("attributes") or {}
    operation = str(attributes.get("gen_ai.operation.name") or "")
    if operation:
        return operation
    return str(span.get("name") or "").split(" ", 1)[0]


class Catalog:
    """Tool name → ``ToolConfig`` (first non-empty description / schema wins), in the order
    the agent declared its tools, then the order other sources named them."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolConfig] = {}

    def add(self, name: Any, description: Any = None, parameters: Any = None) -> None:
        name = str(name or "").strip()
        if not name:
            return
        tool = self._tools.setdefault(name, ToolConfig(name=name))
        if description and not tool.description:
            tool.description = str(description)
        parameters = _json(parameters)
        if isinstance(parameters, dict) and not tool.parameters:
            tool.parameters = parameters

    def names(self) -> list[str]:
        return list(self._tools)

    def tools(self, names: list[str] | None = None) -> list[ToolConfig]:
        """Copies of the catalogue entries — ``names`` first, in that order, then the rest."""
        order = [n for n in dict.fromkeys(names or []) if n in self._tools]
        order += [n for n in self._tools if n not in set(order)]
        return [self._tools[n].model_copy() for n in order]

    def __bool__(self) -> bool:
        return bool(self._tools)


def declared_names(span: dict) -> list[str]:
    """``gen_ai.agent.tools`` of one ``invoke_agent`` span (names only)."""
    declared = _json((span.get("attributes") or {}).get("gen_ai.agent.tools"))
    names: list[str] = []
    for item in declared if isinstance(declared, list) else []:
        name = item.get("name") if isinstance(item, dict) else item
        if name:
            names.append(str(name))
    return names


def build(spans: list[dict]) -> Catalog:
    """The session's catalogue from its span records (normalised span dicts)."""
    catalog = Catalog()
    for span in spans:
        scope = str((span.get("scope") or {}).get("name") or "")
        if scope and not scope.startswith("strands"):
            continue  # ADOT's MCP client span names the gateway's raw tool (N17-obs372)
        attributes = span.get("attributes") or {}
        operation = _operation(span)
        if operation == "invoke_agent":
            declared = _json(attributes.get("gen_ai.agent.tools"))
            for item in declared if isinstance(declared, list) else []:
                if isinstance(item, dict):
                    catalog.add(item.get("name"), item.get("description"),
                                item.get("inputSchema") or item.get("parameters"))
                else:
                    catalog.add(item)
            definitions = _json(attributes.get("gen_ai.tool.definitions"))
            for item in definitions if isinstance(definitions, list) else []:
                if isinstance(item, dict):
                    # OpenAI-style entries nest the tool under "function"
                    fn = item["function"] if isinstance(item.get("function"), dict) else item
                    catalog.add(fn.get("name"), fn.get("description"),
                                fn.get("inputSchema") or fn.get("parameters"))
        elif operation == "execute_tool":
            catalog.add(attributes.get("gen_ai.tool.name"),
                        attributes.get("gen_ai.tool.description"),
                        attributes.get("gen_ai.tool.json_schema"))
    return catalog
