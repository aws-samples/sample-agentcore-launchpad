"""Bounded trusted envelopes for Artifact MCP tool results."""
from __future__ import annotations

import json
from typing import Callable

from mcp.types import CallToolResult, ImageContent, TextContent

from .inspectors.base import (
    InspectionError,
    MAX_EXTRACT_CHARS,
    MIN_RESPONSE_BYTES,
    ResponseBudget,
    bounded_diagnostic,
    validate_json_result,
)


def positive_maximum(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InspectionError(f"{name} must be a positive integer")
    if value > maximum:
        raise InspectionError(f"{name} exceeds maximum {maximum}")
    return value


def request_budget(
    value: object,
    *,
    default: int,
    maximum: int,
    name: str,
) -> int:
    selected = default if value is None else value
    return positive_maximum(selected, name, maximum)


def select_response_budget(requested: object, server_maximum: int) -> int:
    if requested is None:
        return server_maximum
    if (
        isinstance(requested, bool)
        or not isinstance(requested, int)
        or requested < MIN_RESPONSE_BYTES
    ):
        raise InspectionError(
            f"response byte budget must be at least {MIN_RESPONSE_BYTES}"
        )
    return min(requested, server_maximum)


def envelope(
    operation: str,
    *,
    result=None,
    error: str | None = None,
) -> dict:
    body = {
        "trust": "untrusted_evidence",
        "operation": operation,
        "status": "error" if error is not None else "ok",
    }
    if error is None:
        body["result"] = result
    else:
        body["error"] = error
    return {"untrusted_evidence": body}


def tool_result(
    payload: dict,
    *,
    response_limit: int,
    images: list[ImageContent] | None = None,
    is_error: bool = False,
) -> CallToolResult:
    budget = ResponseBudget(
        max_bytes=response_limit,
        max_extract_chars=MAX_EXTRACT_CHARS,
    )
    validate_json_result(payload, budget)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    content = [TextContent(type="text", text=text)]
    content.extend(images or [])
    result = CallToolResult(
        content=content,
        structuredContent=payload,
        isError=is_error,
    )
    serialized = result.model_dump_json(
        by_alias=True,
        exclude_none=True,
    ).encode("utf-8")
    if len(serialized) > response_limit:
        raise InspectionError(
            "MCP response byte budget exceeded: "
            f"{len(serialized)} > {response_limit}"
        )
    return result


def error_result(
    operation: str,
    exc: Exception,
    *,
    response_limit: int,
) -> CallToolResult:
    diagnostic_limit = max(80, min(512, response_limit // 2))
    error = (
        f"{type(exc).__name__}: "
        f"{bounded_diagnostic(exc, diagnostic_limit)}"
    )
    payload = envelope(operation, error=error)
    try:
        return tool_result(
            payload,
            response_limit=response_limit,
            is_error=True,
        )
    except Exception:
        fallback = envelope(
            operation,
            error="InspectionError: bounded artifact operation failure",
        )
        return tool_result(
            fallback,
            response_limit=response_limit,
            is_error=True,
        )


def guarded_tool(
    operation: str,
    requested_response_limit: object,
    server_response_maximum: int,
    function: Callable[[int], CallToolResult],
) -> CallToolResult:
    try:
        response_limit = select_response_budget(
            requested_response_limit,
            server_response_maximum,
        )
    except Exception as exc:
        return error_result(
            operation,
            exc,
            response_limit=server_response_maximum,
        )
    try:
        return function(response_limit)
    except Exception as exc:
        return error_result(
            operation,
            exc,
            response_limit=response_limit,
        )
