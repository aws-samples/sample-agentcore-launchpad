"""Pure selection rules shared by Harness deployment and request overrides."""

import re
from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase
from typing import Any

NATIVE_HARNESS_TOOLS = ("shell", "file_operations")
# HarnessTool.name permits ASCII letters, digits, underscores and hyphens.
# Leave one character for "@" within HarnessAllowedTool's 64-character limit.
_DERIVED_GROUP_NAME = re.compile(r"[A-Za-z0-9_-]{1,63}")


def selected_native_tools(spec: Mapping[str, Any]) -> set[str]:
    """Native capabilities admitted by selection or an explicit expert override."""
    patterns = spec.get("allowed_tools")
    if patterns is None:
        return set(spec.get("native_tools") or []).intersection(NATIVE_HARNESS_TOOLS)
    selected = set()
    for pattern in patterns:
        group, slash, tool = pattern.partition("/")
        if group.startswith("@") or slash:
            if not fnmatchcase("builtin", group.removeprefix("@")):
                continue
            pattern = tool if slash else "*"
        selected.update(name for name in NATIVE_HARNESS_TOOLS if fnmatchcase(name, pattern))
    return selected


def selected_tool_patterns(
    final_configs: Iterable[Mapping[str, Any]],
    has_skills: bool,
    native_tools: Iterable[str],
) -> list[str]:
    """Allow only final configured groups, mounted Skills and selected natives.

    Call after Gateway aliases have been resolved. Native tools already exist in
    Harness and belong in allowedTools, never in the ToolConfig list.
    Reject names that could select the reserved native group or selector syntax.
    """
    patterns = []
    for config in final_configs:
        name = config.get("name")
        if (
            not isinstance(name, str)
            or name.casefold() == "builtin"
            or _DERIVED_GROUP_NAME.fullmatch(name) is None
        ):
            raise ValueError(
                f"cannot derive allowedTools for configured tool name {name!r}: "
                "use 1–63 ASCII letters, digits, underscores or hyphens; "
                "'builtin' is reserved for native Harness tools"
            )
        patterns.append(f"@{name}")
    if has_skills:
        patterns.append("skills")
    for name in native_tools:
        if name not in NATIVE_HARNESS_TOOLS:
            raise ValueError(f"unsupported native Harness tool: {name}")
        patterns.append(name)
    return list(dict.fromkeys(patterns))


def remap_tool_patterns(patterns: Iterable[str], names: Mapping[str, str]) -> list[str]:
    """Remap configured-group selectors without enlarging their tool suffix.

    ``names`` maps actual deployed config names to actual request config names.
    Expand matching group globs to exact names, preserving their selected tool
    suffix; ordinary native/tool-name patterns and explicit "*" remain intact.
    """
    result = []
    for pattern in patterns:
        group, slash, suffix = pattern.partition("/")
        if group.startswith("@") or slash:
            prefix = "@" if group.startswith("@") else ""
            selector = group.removeprefix("@")
            matches = [old for old in names if fnmatchcase(old, selector)]
            if matches and any(names[old] != old for old in matches):
                result.extend(f"{prefix}{names[old]}{slash}{suffix}" for old in matches)
                continue
        result.append(pattern)
    return list(dict.fromkeys(result))
