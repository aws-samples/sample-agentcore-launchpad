"""Runtime callable names for reviewed Harness evaluation rules.

Proposal keys select attachments. They are never literal tool names in traces.
Only approved catalog attachments and the platform's own KB/Skill naming contract
contribute names; observed calls cannot expand this catalog.
"""

import re
from typing import Any

import httpx

from app.core.errors import AppError
from app.services import kb_gateway, mcp_client

MAX_TOOL_PAGES = 5
MAX_TOOLS = 250
SELECTOR_PREFIXES = ("mcp:", "gateway:", "builtin:")
# Managed Harness exposes these without an attachment. They are optional in an
# evaluation allowlist, never silently added to an already reviewed rule.
NATIVE_HARNESS_TOOLS = ("shell", "file_operations")


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
) -> list[str]:
    """Check literal positive allowlists against selected catalog capabilities."""
    allowlists = [
        (entry.key, check) for entry in evaluators if entry.kind == "code"
        for check in entry.rules.checks if check.type == "tool_set" and check.allowed
    ]
    if not allowlists:
        return []
    known = support_tool_names(content, catalog)
    if content.get("method", "harness") == "harness":
        known.update(NATIVE_HARNESS_TOOLS)
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
    for key, check in allowlists:
        prefix = f"evaluators.{key}.rules.{check.id}"
        if missing:
            errors.append(
                f"{prefix}: runtime tool catalog unavailable for {', '.join(missing)}; "
                "refresh the conversation catalog and review the exact callable names"
            )
            continue
        unknown = sorted(set(check.allowed) - known)
        omitted = sorted(support_tool_names(content, catalog) - set(check.allowed))
        if unknown:
            errors.append(
                f"{prefix}: allowed contains tools not in the selected runtime catalog: "
                f"{unknown}; use exact callable names, not attachment keys"
            )
        if omitted:
            errors.append(
                f"{prefix}: allowed omits mounted Skill/knowledge-base support tools: {omitted}"
            )
    return errors
