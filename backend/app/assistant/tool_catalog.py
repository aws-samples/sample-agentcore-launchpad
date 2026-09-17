"""Runtime callable names for reviewed Harness evaluation rules.

Proposal keys select attachments. They are never literal tool names in traces.
Only approved catalog attachments and the platform's own KB/Skill naming contract
contribute names; observed calls cannot expand this catalog.
"""

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import httpx

from app.core.errors import AppError
from app.harness_tool_access import selected_native_tools
from app.services import kb_gateway, mcp_client

if TYPE_CHECKING:
    from app.assistant.evaluation_plan import Scenario

MAX_TOOL_PAGES = 5
MAX_TOOLS = 250
SELECTOR_PREFIXES = ("mcp:", "gateway:", "builtin:")


def remote_tool_names(name: str, url: str) -> list[str]:
    """Read a bounded, complete tools/list catalog; never invoke a tool."""
    names: list[str] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(MAX_TOOL_PAGES):
        result = mcp_client._rpc(
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
    names = {"skills"} if content.get("skills") else set()
    kbs = content.get("knowledge_bases") or []
    by_id = {kb["kb_id"]: kb for kb in (catalog or {}).get("knowledge_bases") or []}
    for ref in kbs:
        kb = ref if isinstance(ref, dict) else by_id.get(ref)
        if kb and kb.get("kb_id") and kb.get("name"):
            names.add(kb_gateway.retrieve_target_name(kb["kb_id"], kb["name"]) + "___Retrieve")
    if kbs and content.get("name"):
        names.add(kb_gateway.agentic_target_name(content["name"]) + "___AgenticRetrieveStream")
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
    known = support_tool_names(content, catalog)
    if content.get("method", "harness") == "harness":
        known.update(selected_native_tools(content))
    missing: list[str] = []
    by_key = {tool["key"]: tool for tool in catalog.get("tools") or []}
    for key in content.get("tools") or []:
        entry = by_key.get(key) if isinstance(key, str) else None
        tool_names = (entry or {}).get("runtime_tools")
        if not isinstance(tool_names, list):
            missing.append(str(key))
        else:
            known.update(tool_names)
    for ref in content.get("knowledge_bases") or []:
        if isinstance(ref, str) and not any(
            kb.get("kb_id") == ref and kb.get("name")
            for kb in catalog.get("knowledge_bases") or []
        ):
            missing.append(f"knowledge_base:{ref}")
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
            errors.append(
                f"{prefix}: {field} contains tools not in the selected runtime catalog: "
                f"{unknown}; use exact callable names, not attachment keys"
            )
        if omitted:
            errors.append(
                f"{prefix}: allowed omits mounted Skill/knowledge-base support tools: {omitted}"
            )
    return errors
