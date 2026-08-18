"""Validation and normalization for optional SkillEval judge contracts."""
from __future__ import annotations

import math
import re

JUDGE_MODES = {"auto", "agentic", "chat"}
CHECK_FIELDS = {
    "exists": (),
    "opens": (),
    "contains_text": ("text",),
    "xlsx_cell": ("sheet", "cell", "value"),
    "xlsx_formula": ("sheet", "cell", "formula"),
    "page_count": ("value",),
    "slide_count": ("value",),
    "image_dimensions": ("width", "height"),
    "visual": ("rubric",),
}

_RUNTIME_ROOTS = {".agents", ".claude", ".codex", ".git", "task.md"}
_WINDOWS_ROOT_RE = re.compile(r"^[A-Za-z]:")


def _item_label(index: int, item: dict) -> str:
    raw_id = item.get("id")
    if isinstance(raw_id, str) and raw_id:
        return f"item #{index} (id={raw_id!r})"
    return f"item #{index}"


def _safe_rel_path(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value.startswith(("~", "/"))
        or "\\" in value
        or "\x00" in value
        or _WINDOWS_ROOT_RE.match(value)
    ):
        raise ValueError(f"{label} path {value!r} must be a safe POSIX relative path")

    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"{label} path {value!r} must be a safe POSIX relative path")
    if parts[0] in _RUNTIME_ROOTS:
        raise ValueError(f"{label} path {value!r} collides with the evaluation runtime")
    return value


def normalize_judge_contract(
    index: int,
    item: dict,
) -> tuple[str, list[dict], bool]:
    """Validate and normalize a task's optional judge configuration."""
    item_label = _item_label(index, item)
    mode_explicit = "judge_mode" in item
    mode = item.get("judge_mode", "auto")
    if not isinstance(mode, str) or mode not in JUDGE_MODES:
        raise ValueError(
            f"{item_label}: judge_mode must be one of {sorted(JUDGE_MODES)}"
        )

    raw_checks = item.get("artifact_checks", [])
    if not isinstance(raw_checks, list):
        raise ValueError(f"{item_label}: artifact_checks must be a list")

    normalized: list[dict] = []
    seen_ids: set[str] = set()
    for check_index, raw_check in enumerate(raw_checks):
        location = f"{item_label}: artifact_checks[{check_index}]"
        if not isinstance(raw_check, dict):
            raise ValueError(f"{location} must be an object")

        raw_id = raw_check.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise ValueError(f"{location}.id must be a non-empty string")
        check_id = raw_id.strip()
        if check_id in seen_ids:
            raise ValueError(f"{item_label}: duplicate artifact check id {check_id!r}")
        seen_ids.add(check_id)
        check_label = f"{item_label}: artifact check {check_id!r}"

        path = _safe_rel_path(raw_check.get("path"), label=check_label)

        check_type = raw_check.get("type")
        if not isinstance(check_type, str) or check_type not in CHECK_FIELDS:
            raise ValueError(f"{check_label} has unknown artifact check type {check_type!r}")

        spec = raw_check.get("spec")
        if not isinstance(spec, dict):
            raise ValueError(f"{check_label} spec must be an object")
        missing_fields = [field for field in CHECK_FIELDS[check_type] if field not in spec]
        if missing_fields:
            raise ValueError(
                f"{check_label} is missing required spec fields {missing_fields}"
            )

        required = raw_check.get("required", True)
        if not isinstance(required, bool):
            raise ValueError(f"{check_label} required must be boolean")

        weight = raw_check.get("weight", 1.0)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ValueError(f"{check_label} weight must be a finite positive number")
        try:
            normalized_weight = float(weight)
        except OverflowError:
            normalized_weight = math.inf
        if not math.isfinite(normalized_weight) or normalized_weight <= 0:
            raise ValueError(f"{check_label} weight must be a finite positive number")

        normalized.append(
            {
                "id": check_id,
                "path": path,
                "type": check_type,
                "required": required,
                "weight": normalized_weight,
                "spec": dict(spec),
            }
        )

    return mode, normalized, mode_explicit
