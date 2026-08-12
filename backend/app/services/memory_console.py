"""Read-only AgentCore Memory projections for the Memory console page.

Deliberately separate from ``app.services.memory``: that module sits on the chat
invoke hot path and stays tiny, while everything here is console-only (control
plane reads, actor decoding, namespace resolution, pagination plumbing). The
scoping contract itself is *not* duplicated — ``SCOPE_SEP`` and ``_memory_id``
are imported from ``memory`` so there is a single source of truth for how the
agent id is folded into ``actorId``.

Every function in this module is a **read**. There is intentionally no wrapper
for ``CreateEvent``/``DeleteEvent``/``DeleteMemoryRecord``/``Batch*MemoryRecords``/
``StartMemoryExtractionJob``/``CreateMemory``/``UpdateMemory``/``DeleteMemory`` —
the console cannot mutate memory because the code path does not exist.
"""

import json
import re
from datetime import datetime
from typing import Any

from app.core.errors import AppError
from app.services.agentcore.client import control_client, data_client
from app.services.memory import SCOPE_SEP, decode_record_text, memory_id_or_none
from app.services.workspace import WorkspaceContext

# AWS caps most memory list operations at 100 items per page — but
# ListMemoryExtractionJobs rejects anything above 50 with a ValidationException,
# and the botocore service model does not declare that bound, so it is pinned
# here (found by calling the real API; see tests for the regression).
PAGE_MAX = 100
EXTRACTION_PAGE_MAX = 50
# ListMemoryExtractionJobs' status filter accepts exactly one value. Confirmed
# both in the botocore enum and by the live API, which answers anything else with
# `Member must satisfy enum value set: [FAILED]`. Jobs of other statuses are
# still listed — you just cannot filter *for* them.
EXTRACTION_STATUS_FILTERS = ("FAILED",)
# Namespace templates carry placeholders like ``{actorId}``; anything left after
# substitution means we cannot build a concrete namespace for this actor.
_PLACEHOLDER = re.compile(r"\{[^}]+\}")


def _iso(value: Any) -> str | None:
    """botocore hands back datetimes; the API contract is ISO-8601 strings."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _page_size(requested: int | None, cap: int = PAGE_MAX) -> int:
    return min(requested or cap, cap)


def _token(token: str | None) -> dict[str, str]:
    return {"nextToken": token} if token else {}


def require_memory_id(workspace: WorkspaceContext) -> str:
    """Memory id or a typed 409 — every endpoint except /overview needs a real id."""
    mem_id = memory_id_or_none(workspace)
    if not mem_id:
        raise AppError(
            "memory.not_configured",
            "No AgentCore Memory resource is configured — run `make bootstrap`.",
            status_code=409,
        )
    return mem_id


# --------------------------------------------------------------------------- #
# Overview — memory resource + long-term strategies
# --------------------------------------------------------------------------- #


def _strategy(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "strategy_id": raw.get("strategyId"),
        "name": raw.get("name"),
        "description": raw.get("description"),
        "type": raw.get("type"),
        "status": raw.get("status"),
        "namespaces": list(raw.get("namespaces") or []),
        "namespace_templates": list(raw.get("namespaceTemplates") or []),
        "created_at": _iso(raw.get("createdAt")),
        "updated_at": _iso(raw.get("updatedAt")),
    }


def memory_overview(workspace: WorkspaceContext) -> dict[str, Any]:
    """Resource config + strategies + a bounded actor count + sibling memories.

    Returns ``configured: False`` (instead of raising) when bootstrap has not run
    — the landing view needs to render a "run make bootstrap" state, while every
    other endpoint treats a missing memory id as an error.
    """
    mem_id = memory_id_or_none(workspace)
    if not mem_id:
        return {
            "configured": False,
            "memory": None,
            "strategies": [],
            "actor_count": 0,
            "actor_count_truncated": False,
            "other_memories": [],
        }

    control = control_client(workspace)
    raw = control.get_memory(memoryId=mem_id).get("memory", {})

    # One page only: a bounded cost with the bound made visible below, rather
    # than paginating an unbounded actor list to fill a stat tile.
    actors = data_client(workspace).list_actors(memoryId=mem_id, maxResults=PAGE_MAX)

    others: list[dict[str, Any]] = []
    for mem in control.list_memories(maxResults=PAGE_MAX).get("memories", []):
        ident = mem.get("id") or mem.get("memoryId")
        others.append(
            {
                "id": ident,
                "arn": mem.get("arn"),
                "status": mem.get("status"),
                "created_at": _iso(mem.get("createdAt")),
                "updated_at": _iso(mem.get("updatedAt")),
                "is_platform": ident == mem_id,
            }
        )

    return {
        "configured": True,
        "memory": {
            "id": raw.get("id") or mem_id,
            "arn": raw.get("arn"),
            "name": raw.get("name"),
            "description": raw.get("description"),
            "status": raw.get("status"),
            "failure_reason": raw.get("failureReason"),
            "event_expiry_days": raw.get("eventExpiryDuration"),
            "encryption_key_arn": raw.get("encryptionKeyArn"),
            "execution_role_arn": raw.get("memoryExecutionRoleArn"),
            "created_at": _iso(raw.get("createdAt")),
            "updated_at": _iso(raw.get("updatedAt")),
        },
        "strategies": [_strategy(s) for s in raw.get("strategies") or []],
        "actor_count": len(actors.get("actorSummaries") or []),
        "actor_count_truncated": bool(actors.get("nextToken")),
        "other_memories": others,
    }


def get_strategies(workspace: WorkspaceContext) -> list[dict[str, Any]]:
    """Strategies alone — used by namespace resolution without a full overview."""
    raw = (
        control_client(workspace)
        .get_memory(memoryId=require_memory_id(workspace))
        .get("memory", {})
    )
    return [_strategy(s) for s in raw.get("strategies") or []]


# --------------------------------------------------------------------------- #
# Short-term — actors, sessions, events
# --------------------------------------------------------------------------- #


def decode_actor(actor_id: str) -> dict[str, Any]:
    """Split a platform-scoped actor id back into (agent id, human actor).

    ``scoped_actor()`` builds ``<agent_id>__<human>``. The human part may itself
    contain the separator (actor ids are free-form), so split on the *first*
    occurrence only. An actor id written by something other than this platform
    has no separator — report it as unscoped rather than inventing an agent id.
    """
    agent_id, sep, human = actor_id.partition(SCOPE_SEP)
    if not sep:
        return {
            "actor_id": actor_id,
            "agent_id": None,
            "human_actor": actor_id,
            "scoped": False,
        }
    return {
        "actor_id": actor_id,
        "agent_id": agent_id,
        "human_actor": human,
        "scoped": True,
    }


def list_actors(
    workspace: WorkspaceContext,
    next_token: str | None = None,
    max_results: int | None = None,
) -> dict[str, Any]:
    mem_id = require_memory_id(workspace)
    page = data_client(workspace).list_actors(
        memoryId=mem_id, maxResults=_page_size(max_results), **_token(next_token)
    )
    return {
        "items": [
            decode_actor(a.get("actorId", "")) for a in page.get("actorSummaries") or []
        ],
        "next_token": page.get("nextToken"),
    }


def list_sessions(
    workspace: WorkspaceContext,
    actor_id: str,
    next_token: str | None = None,
    max_results: int | None = None,
) -> dict[str, Any]:
    mem_id = require_memory_id(workspace)
    page = data_client(workspace).list_sessions(
        memoryId=mem_id,
        actorId=actor_id,
        maxResults=_page_size(max_results),
        **_token(next_token),
    )
    return {
        "items": [
            {
                "session_id": s.get("sessionId"),
                "actor_id": s.get("actorId", actor_id),
                "created_at": _iso(s.get("createdAt")),
            }
            for s in page.get("sessionSummaries") or []
        ],
        "next_token": page.get("nextToken"),
    }


def _decode_turn(raw: str) -> tuple[str, list[str]]:
    """Return (display text, envelope part kinds) for one conversational turn.

    Harness agents persist a whole message envelope as the event text
    (``{"message": {"role", "content": [{"text"|"toolUse"|"toolResult"…}]}}``),
    while platform-written events store plain text. Unlike the Observability
    transcript (``observability._turn_text``), which drops tool-only turns, a
    memory inspector must never hide a payload that exists: the part kinds are
    returned alongside the text so a tool-only turn still renders as itself
    instead of as an empty bubble or a wall of raw JSON.
    """
    text = raw.strip()
    if not text.startswith("{"):
        return raw, []
    try:
        envelope = json.loads(text)
    except ValueError:
        return raw, []
    content = (envelope.get("message") or {}).get("content")
    if not isinstance(content, list):
        return raw, []
    kinds: list[str] = []
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        for key, value in part.items():
            kinds.append(key)
            if key == "text" and value:
                parts.append(str(value))
    return "\n".join(parts), kinds


def _payload_entry(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one event payload entry.

    Conversational text is passed through in full (the UI clamps it); blob
    payloads report only a byte count so binary agent state never reaches the
    browser. Unknown payload kinds are dropped — the preview SDK may add more.
    """
    if "conversational" in raw:
        conv = raw["conversational"] or {}
        text, kinds = _decode_turn((conv.get("content") or {}).get("text", "") or "")
        return {
            "kind": "conversational",
            "role": conv.get("role"),
            "text": text,
            "parts": kinds,
            "blob_bytes": None,
        }
    if "blob" in raw:
        blob = raw["blob"]
        try:
            size = len(blob) if blob is not None else 0
        except TypeError:  # non-sized blob representation
            size = 0
        return {
            "kind": "blob",
            "role": None,
            "text": None,
            "parts": [],
            "blob_bytes": size,
        }
    return None


def list_events(
    workspace: WorkspaceContext,
    actor_id: str,
    session_id: str,
    include_payloads: bool = True,
    next_token: str | None = None,
    max_results: int | None = None,
) -> dict[str, Any]:
    mem_id = require_memory_id(workspace)
    page = data_client(workspace).list_events(
        memoryId=mem_id,
        actorId=actor_id,
        sessionId=session_id,
        includePayloads=include_payloads,
        maxResults=_page_size(max_results),
        **_token(next_token),
    )
    items: list[dict[str, Any]] = []
    for event in page.get("events") or []:
        payload = [
            entry
            for entry in (_payload_entry(p) for p in event.get("payload") or [])
            if entry is not None
        ]
        branch = event.get("branch") or None
        items.append(
            {
                "event_id": event.get("eventId"),
                "at": _iso(event.get("eventTimestamp")),
                "branch": (
                    {"name": branch.get("name"), "root_event_id": branch.get("rootEventId")}
                    if branch
                    else None
                ),
                "metadata": dict(event.get("metadata") or {}),
                "payload": payload,
            }
        )
    return {"items": items, "next_token": page.get("nextToken")}


# --------------------------------------------------------------------------- #
# Long-term — namespaces, records, semantic retrieval
# --------------------------------------------------------------------------- #


def resolve_namespaces(
    workspace: WorkspaceContext,
    actor_id: str,
    strategies: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Turn each strategy's namespace template into a concrete namespace.

    Substitution happens server-side so the ``{actorId}`` contract lives next to
    ``scoped_actor`` instead of being re-implemented in TypeScript. Templates
    with any other placeholder are returned as ``resolvable: False`` so the UI
    can disable them rather than sending a broken namespace to AWS.
    """
    resolved: list[dict[str, Any]] = []
    for strategy in strategies if strategies is not None else get_strategies(workspace):
        templates = strategy["namespace_templates"] or strategy["namespaces"]
        for template in templates:
            namespace = template.replace("{actorId}", actor_id)
            resolved.append(
                {
                    "strategy_id": strategy["strategy_id"],
                    "strategy_name": strategy["name"],
                    "strategy_type": strategy["type"],
                    "template": template,
                    "namespace": namespace,
                    "resolvable": not _PLACEHOLDER.search(namespace),
                }
            )
    return resolved


def _record(raw: dict[str, Any]) -> dict[str, Any]:
    # Strategies disagree on the payload shape (see memory.decode_record_text):
    # `text` is always something a human can read, `structured` carries the
    # parsed object for record detail, and `raw_text` never loses the original.
    stored = (raw.get("content") or {}).get("text", "")
    display, structured = decode_record_text(stored)
    return {
        "record_id": raw.get("memoryRecordId"),
        "text": display,
        "structured": structured,
        "raw_text": stored,
        "strategy_id": raw.get("memoryStrategyId"),
        "namespaces": list(raw.get("namespaces") or []),
        "created_at": _iso(raw.get("createdAt")),
        "score": raw.get("score"),
        "metadata": dict(raw.get("metadata") or {}),
    }


def list_records(
    workspace: WorkspaceContext,
    namespace: str,
    strategy_id: str | None = None,
    next_token: str | None = None,
    max_results: int | None = None,
) -> dict[str, Any]:
    mem_id = require_memory_id(workspace)
    kwargs: dict[str, Any] = {
        "memoryId": mem_id,
        "namespacePath": namespace,
        "maxResults": _page_size(max_results),
        **_token(next_token),
    }
    if strategy_id:
        kwargs["memoryStrategyId"] = strategy_id
    page = data_client(workspace).list_memory_records(**kwargs)
    return {
        "namespace": namespace,
        "items": [_record(r) for r in page.get("memoryRecordSummaries") or []],
        "next_token": page.get("nextToken"),
    }


def search_records(
    workspace: WorkspaceContext,
    namespace: str,
    query: str,
    top_k: int = 5,
    strategy_id: str | None = None,
) -> dict[str, Any]:
    mem_id = require_memory_id(workspace)
    criteria: dict[str, Any] = {"searchQuery": query, "topK": top_k}
    if strategy_id:
        criteria["memoryStrategyId"] = strategy_id
    page = data_client(workspace).retrieve_memory_records(
        memoryId=mem_id, namespace=namespace, searchCriteria=criteria
    )
    return {
        "namespace": namespace,
        "query": query,
        "items": [_record(r) for r in page.get("memoryRecordSummaries") or []],
        "next_token": page.get("nextToken"),
    }


# --------------------------------------------------------------------------- #
# Extraction pipeline — short-term events → long-term records
# --------------------------------------------------------------------------- #


def _job_messages(raw: Any) -> list[str]:
    """``messages.messagesList`` is preview-volatile; degrade instead of raising."""
    if not isinstance(raw, dict):
        return []
    listed = raw.get("messagesList")
    if not isinstance(listed, list):
        return []
    return [m if isinstance(m, str) else str(m) for m in listed]


def list_extraction_jobs(
    workspace: WorkspaceContext,
    actor_id: str | None = None,
    session_id: str | None = None,
    strategy_id: str | None = None,
    status: str | None = None,
    next_token: str | None = None,
    max_results: int | None = None,
) -> dict[str, Any]:
    mem_id = require_memory_id(workspace)
    if status and status not in EXTRACTION_STATUS_FILTERS:
        # Fail as a typed 400 rather than letting AWS answer with a 502-shaped
        # ValidationException the caller cannot act on.
        raise AppError(
            "memory.invalid_status_filter",
            f"status filter must be one of {', '.join(EXTRACTION_STATUS_FILTERS)}.",
            detail={"allowed": list(EXTRACTION_STATUS_FILTERS)},
            status_code=400,
        )
    # Empty strings are rejected inside the preview API's filter shape, so only
    # non-empty filters are sent at all.
    job_filter = {
        key: value
        for key, value in (
            ("actorId", actor_id),
            ("sessionId", session_id),
            ("strategyId", strategy_id),
            ("status", status),
        )
        if value
    }
    kwargs: dict[str, Any] = {
        "memoryId": mem_id,
        "maxResults": _page_size(max_results, EXTRACTION_PAGE_MAX),
        **_token(next_token),
    }
    if job_filter:
        kwargs["filter"] = job_filter
    page = data_client(workspace).list_memory_extraction_jobs(**kwargs)
    return {
        "items": [
            {
                "job_id": job.get("jobID") or job.get("jobId"),
                "status": job.get("status"),
                "failure_reason": job.get("failureReason"),
                "strategy_id": job.get("strategyId"),
                "actor_id": job.get("actorId"),
                "session_id": job.get("sessionId"),
                "messages": _job_messages(job.get("messages")),
            }
            for job in page.get("jobs") or []
        ],
        "next_token": page.get("nextToken"),
    }
