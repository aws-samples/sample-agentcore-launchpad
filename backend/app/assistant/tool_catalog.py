"""Runtime callable names of the attachments a proposal selects.

Proposal keys select attachments. They are never literal tool names in traces.
Only approved catalog attachments and the platform's own KB/Skill naming contract
contribute names; observed calls cannot expand this catalog.
"""

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import httpx

from app.core.errors import AppError
from app.harness_tool_access import selected_builtin_tool_names, selected_native_tools
from app.services import mcp_client
from app.templates.kb_support import KB_DEEP_TOOL_NAME, KB_TOOL_NAME

if TYPE_CHECKING:
    from app.assistant.evaluation_plan import Scenario

MAX_TOOL_PAGES = 5
MAX_TOOLS = 250
SELECTOR_PREFIXES = ("mcp:", "gateway:", "builtin:")
# N17-obs375: a Harness mounts the shared Gateway under this alias (`deployer.harness_local.
# GATEWAY_ALIAS`, pinned equal by a test) and strands prefixes each of its tools with it, so the
# declared target tool `<record>___<tool>` is called `launchpad_gw_<record>___<tool>`.
GATEWAY_ALIAS = "launchpad_gw"


def remote_tool_names(name: str, url: str) -> list[str]:
    """Read a bounded, complete tools/list catalog; never invoke a tool."""
    names: list[str] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(MAX_TOOL_PAGES):
        result = mcp_client._unsigned_rpc(
            url, None, "tools/list", {"cursor": cursor} if cursor else {},
            timeout=10,
        )
        if not isinstance(result, dict):
            raise ValueError("tools/list did not return a result object")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise ValueError("tools/list did not return a tools array")
        for tool in tools:
            raw = tool.get("name") if isinstance(tool, dict) else None
            if not isinstance(raw, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", raw):
                raise ValueError("tools/list returned an invalid tool name")
            qualified = f"{name}_{raw}"
            if qualified in names:
                raise ValueError("tools/list returned duplicate tool names")
            names.append(qualified)
        if len(names) > MAX_TOOLS:
            raise ValueError("tools/list exceeded the bounded catalog size")
        nxt = result.get("nextCursor")
        if nxt in (None, ""):
            return names
        if not isinstance(nxt, str) or nxt in seen:
            raise ValueError("tools/list returned an unusable pagination cursor")
        seen.add(nxt)
        cursor = nxt
    raise ValueError("tools/list pagination did not complete")


def enrich_tool(entry: dict[str, Any], warnings: list[str]) -> None:
    """Discovery failure affects rule review, not ordinary attachment eligibility."""
    if not entry.get("attachable", True):
        return
    try:
        if entry["kind"] == "mcp":
            entry["runtime_tools"] = remote_tool_names(entry["name"], entry["url"])
        elif "runtime_tools" not in entry:
            entry["runtime_tools"] = None
        if entry.get("runtime_tools") is None:
            raise ValueError("the approved Gateway record has no complete tool catalog")
    except (AppError, httpx.HTTPError, ValueError) as exc:
        entry["runtime_tools"] = None
        warnings.append(
            f"runtime tools for {entry['key']} unavailable: {str(exc)[:180]}; "
            "refresh the catalog before preparing a literal tool allowlist"
        )


def support_tool_names(
    content: dict[str, Any], catalog: dict[str, Any] | None = None,
) -> set[str]:
    """Names implicitly mounted by Harness for selected Skills and knowledge bases."""
    if content.get("method", "harness") != "harness":
        return set()
    names: set[str] = set()
    if content.get("skills"):
        names.add("skills")
    if content.get("knowledge_bases"):
        # the direct retrieval tools the renderer wires for every mounted KB
        # (``templates/kb_support``) — constant names, no catalog lookup to guess
        names.update((KB_TOOL_NAME, KB_DEEP_TOOL_NAME))
    return names


def rule_catalog_errors(
    evaluators: list[Any], content: dict[str, Any], catalog: dict[str, Any],
    *, scenarios: Iterable["Scenario"] = (),
) -> list[str]:
    """Check positive rules and scenario trajectories against selected callables.

    A mounted MCP can expose both reads and writes: narrow allowlists and named
    prohibitions remain valid. Never broaden rules to all advertised functions.
    Scenarios are the runnable plan inputs; blocked golden tests are excluded.
    A nonempty trajectory is an expectation regardless of which evaluator uses it.
    """
    positive_rules: list[tuple[str, list[str], bool]] = []
    for entry in evaluators:
        if entry.kind != "code":
            continue
        for check in entry.rules.checks:
            names = (
                check.allowed if check.type == "tool_set"
                else check.tools if check.type == "tool_sequence"
                else [check.tool] if check.type == "tool_count" and check.tool
                and (check.min or 0) > 0
                else []
            )
            if names:
                positive_rules.append((
                    f"evaluators.{entry.key}.rules.{check.id}", names, check.type == "tool_set",
                ))
    positive_rules.extend(
        (f"scenarios.{scenario.scenario_id}.expected_trajectory",
         scenario.expected_trajectory, False)
        for scenario in scenarios if scenario.expected_trajectory
    )
    if not positive_rules:
        return []
    known, runtime_of, missing = _selected_callables(content, catalog)
    # a declared Gateway name stays valid (code rules were authored with it); its runtime
    # name is valid too — a trajectory written with it was refused before (N17-obs375)
    known |= set(runtime_of.values())
    errors = []
    for prefix, names, is_allowlist in positive_rules:
        if missing:
            errors.append(
                f"{prefix}: runtime tool catalog unavailable for {', '.join(missing)}; "
                "refresh the conversation catalog and review the exact callable names"
            )
            continue
        unknown = sorted({name for name in names
                          if name not in known or name.startswith(SELECTOR_PREFIXES)})
        omitted = (
            sorted(support_tool_names(content, catalog) - set(names))
            if is_allowlist else []
        )
        if unknown:
            field = "allowed" if is_allowlist else "required tools"
            valid = sorted(set(runtime_of.values()) | (known - set(runtime_of)))
            errors.append(
                f"{prefix}: {field} contains tools not in the selected runtime catalog: "
                f"{unknown}; use exact callable names, not attachment keys "
                f"(valid names: {valid})"
            )
        if omitted:
            errors.append(
                f"{prefix}: allowed omits mounted Skill/knowledge-base support tools: {omitted}"
            )
    return errors


def _selected_callables(
    content: dict[str, Any], catalog: dict[str, Any],
) -> tuple[set[str], dict[str, str], list[str]]:
    """``(known, runtime_of, missing)`` of the proposal's selected attachments: every callable
    name the catalogue gives them (declared names for a Gateway), ``{declared Gateway name:
    the name the agent calls it by}`` and the selections whose catalogue is unavailable."""
    harness = content.get("method", "harness") == "harness"
    known = support_tool_names(content, catalog)
    if harness:
        known.update(selected_native_tools(content))
        known.update(selected_builtin_tool_names(content))
    runtime_of: dict[str, str] = {}
    missing: list[str] = []
    by_key = {tool["key"]: tool for tool in catalog.get("tools") or []}
    narrowed = content.get("tool_functions") or {}
    for key in content.get("tools") or []:
        entry = by_key.get(key) if isinstance(key, str) else None
        tool_names = (entry or {}).get("runtime_tools")
        if isinstance(tool_names, list) and isinstance(narrowed.get(key), list):
            # a narrowed Gateway exposes only the reviewed functions at runtime
            tool_names = [name for name in tool_names if name in narrowed[key]]
        if not isinstance(tool_names, list):
            missing.append(str(key))
            continue
        known.update(tool_names)
        if harness and (entry or {}).get("kind") == "gateway":
            runtime_of.update({name: f"{GATEWAY_ALIAS}_{name}" for name in tool_names})
    for ref in content.get("knowledge_bases") or []:
        if isinstance(ref, str) and not any(
            kb.get("kb_id") == ref and kb.get("name")
            for kb in catalog.get("knowledge_bases") or []
        ):
            missing.append(f"knowledge_base:{ref}")
    return known, runtime_of, missing


def resolve_trajectory(
    names: list[str], content: dict[str, Any], catalog: dict[str, Any],
) -> list[str]:
    """N17-obs375: ``names`` as the agent's runtime callable names — a declared Gateway name of
    a Harness agent gains the ``launchpad_gw_`` alias prefix; every other name (and every name
    of an agent that mounts Gateway tools unprefixed) is kept. Unknown names are left for
    :func:`rule_catalog_errors` to refuse."""
    _, runtime_of, _ = _selected_callables(content, catalog)
    return [runtime_of.get(name, name) for name in names]
