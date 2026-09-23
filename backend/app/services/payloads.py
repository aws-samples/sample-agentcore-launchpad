"""Capability guard and ledger summary for the structured invoke payload.

`/v1` is the managed equivalent of InvokeAgentRuntime: Launchpad adds sessions,
actor isolation and keys, but must not remove the Runtime contract — which
accepts an arbitrary JSON body. `AttachmentRequest.payload` restores that: the
caller's keys are merged flat next to `prompt`/`actor_id`, so the agent's
entrypoint sees the same shape as a direct InvokeAgentRuntime call. Bounds and
reserved-key rejection live in `schemas.attachments`; this module owns *which
agents can receive it* and what the chat ledger keeps of it.
"""

import json
from typing import Any

from app.core.errors import AppError
from app.models.ledger import Agent
from app.schemas.attachments import MAX_PAYLOAD_BYTES, RESERVED_PAYLOAD_KEYS
from app.services.runtime_discovery import is_discovered_harness

# The ledger keeps a compact summary, never the raw payload (parity with
# attachments: history shows what was sent without persisting the bytes).
SUMMARY_JSON_CHARS = 2048


def payload_capability(agent: Agent) -> dict[str, Any]:
    """Whether this agent's invoke path can carry caller payload keys.

    The managed Harness is the one method with no passthrough: InvokeHarness
    takes messages, not an open JSON body. Every Runtime-backed method forwards
    — generated templates simply ignore keys they don't read today.
    """
    harness = agent.method == "harness" or is_discovered_harness(agent)
    return {
        "supported": not harness,
        "reason_code": "harness" if harness else None,
        "max_bytes": MAX_PAYLOAD_BYTES,
        "reserved_keys": sorted(RESERVED_PAYLOAD_KEYS),
    }


def check_payload(agent: Agent, payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Refuse a payload the invoke path cannot deliver — before any AWS call.

    Mirrors `prepare_attachments`: capability errors fail the request at the
    entrance instead of silently dropping caller data downstream. The canary
    gateway forwards only `{prompt, sessionId}`, so payload keys would vanish
    mid-experiment — refused like native attachments are.
    """
    if not payload:
        return None
    capability = payload_capability(agent)
    if not capability["supported"]:
        raise AppError(
            "invoke.payload_unsupported",
            "This agent runs on the managed Harness, which accepts messages only; "
            "structured payload fields cannot be forwarded. Use a Runtime-backed agent.",
            {"reason_code": capability["reason_code"]},
            status_code=422,
        )
    from app.optimization import canary_service

    if canary_service.active_canary_route(agent.id):
        raise AppError(
            "invoke.payload_canary_unsupported",
            "Structured payload is unavailable while this agent has an active canary.",
            status_code=409,
        )
    return payload


def payload_summary(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Compact ledger/display projection: sorted keys + truncated JSON."""
    if not payload:
        return None
    dumped = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    truncated = len(dumped) > SUMMARY_JSON_CHARS
    return {
        "keys": sorted(payload),
        "json": dumped[:SUMMARY_JSON_CHARS] + ("…" if truncated else ""),
        "truncated": truncated,
    }
