"""AgentCore Memory helpers — session events (short-term) + records (long-term)."""

import json
from datetime import UTC, datetime
from typing import Any

from app.services.agentcore.client import data_client
from app.services.workspace import WorkspaceContext


def memory_id_or_none(workspace: WorkspaceContext) -> str | None:
    """The workspace's memory id, or None before its bootstrap has run.

    The console's landing view needs to distinguish "not bootstrapped" from
    "AWS call failed", so it reads the id without raising.
    """
    return workspace.resources.get("memory_id") or None


def _memory_id(workspace: WorkspaceContext) -> str:
    memory_id = memory_id_or_none(workspace)
    if not memory_id:
        raise RuntimeError(
            "memory_id missing from this workspace's resource map — run its bootstrap"
        )
    return memory_id


SCOPE_SEP = "__"

# Strategies do not agree on a record payload shape: SEMANTIC stores prose in
# content.text, while USER_PREFERENCE (and SUMMARIZATION) store a structured
# JSON object there. Showing that object verbatim is unreadable, so pick the
# first present display field — in decreasing specificity — and hand the parsed
# object back so callers can render the rest.
_RECORD_DISPLAY_KEYS = ("preference", "summary", "fact", "context", "text")


def decode_record_text(raw: str) -> tuple[str, dict | None]:
    """Return (display text, parsed object or None) for one memory record."""
    text = (raw or "").strip()
    if not text.startswith("{"):
        return raw, None
    try:
        parsed = json.loads(text)
    except ValueError:
        return raw, None
    if not isinstance(parsed, dict):
        return raw, None
    for key in _RECORD_DISPLAY_KEYS:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value, parsed
    # a structured record with no known display field: keep the raw JSON rather
    # than inventing a summary, but still expose the parsed fields
    return raw, parsed


def scoped_actor(agent_id: str, base_actor: str = "river") -> str:
    """Fold the agent id into the memory actor id so memory partitions per agent.

    AgentCore Memory has no notion of "agent": its long-term namespace templates
    (``/facts/{actorId}``, ``/preferences/{actorId}``) and its short-term events
    are both keyed only on ``actorId`` (plus ``sessionId``). Scoping the actor is
    therefore the single lever that separates BOTH stores by agent — long-term
    records land in ``/facts/<agent>__<actor>`` and short-term events are written
    under the same compound actor, so one agent's memory never bleeds into
    another's for the same human.

    Agent ids are uuid4 hex (``[0-9a-f]{32}``), so the compound id stays within
    the actorId charset and is safe as a namespace path segment.
    """
    return f"{agent_id}{SCOPE_SEP}{base_actor}"


def create_turn_event(
    workspace: WorkspaceContext, actor_id: str, session_id: str, prompt: str, answer: str
) -> dict[str, Any]:
    """Persist one conversation turn into short-term memory (feeds extraction)."""
    return data_client(workspace).create_event(
        memoryId=_memory_id(workspace),
        actorId=actor_id,
        sessionId=session_id,
        eventTimestamp=datetime.now(UTC),
        payload=[
            {"conversational": {"role": "USER", "content": {"text": prompt}}},
            {"conversational": {"role": "ASSISTANT", "content": {"text": answer}}},
        ],
    )


def list_events(
    workspace: WorkspaceContext, actor_id: str, session_id: str, max_results: int = 20
) -> list[dict]:
    return data_client(workspace).list_events(
        memoryId=_memory_id(workspace),
        actorId=actor_id,
        sessionId=session_id,
        includePayloads=True,
        maxResults=min(max_results, 100),
    ).get("events", [])


def list_actor_ids(
    workspace: WorkspaceContext, prefix: str | None = None, max_pages: int = 5
) -> list[str]:
    """Every actor id in the memory store, optionally filtered by prefix.

    ListActors has no server-side filter, so the prefix is applied client-side
    while paging (100/page, capped at `max_pages` — the store holds tens of
    actors, and callers only need a bounded candidate set).
    """
    client = data_client(workspace)
    actor_ids: list[str] = []
    token: str | None = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"memoryId": _memory_id(workspace), "maxResults": 100}
        if token:
            params["nextToken"] = token
        page = client.list_actors(**params)
        actor_ids += [
            summary["actorId"]
            for summary in page.get("actorSummaries", [])
            if summary.get("actorId")
        ]
        token = page.get("nextToken")
        if not token:
            break
    if prefix:
        return [actor_id for actor_id in actor_ids if actor_id.startswith(prefix)]
    return actor_ids


def list_records(
    workspace: WorkspaceContext, namespace_prefix: str, max_results: int = 20
) -> list[dict]:
    return data_client(workspace).list_memory_records(
        memoryId=_memory_id(workspace),
        namespacePath=namespace_prefix,
        maxResults=min(max_results, 100),
    ).get("memoryRecordSummaries", [])


def retrieve_records(
    workspace: WorkspaceContext, namespace: str, query: str, top_k: int = 3
) -> list[dict]:
    return data_client(workspace).retrieve_memory_records(
        memoryId=_memory_id(workspace),
        namespace=namespace,
        searchCriteria={"searchQuery": query, "topK": top_k},
    ).get("memoryRecordSummaries", [])


def session_memory_summary(
    workspace: WorkspaceContext, actor_id: str, session_id: str
) -> dict[str, Any]:
    """Right-rail panel data: event count + long-term records for the actor."""
    events = list_events(workspace, actor_id, session_id)
    records: list[dict[str, Any]] = []
    for label in ("/preferences", "/facts"):
        # actor_id is already agent-scoped (see scoped_actor); the display label
        # keeps just the strategy — the actor/agent is implied by the session.
        for record in list_records(workspace, f"{label}/{actor_id}", max_results=10):
            content = record.get("content", {})
            # /preferences records are structured JSON, /facts records prose —
            # decode so the rail shows a sentence, not a serialized object.
            display, _ = decode_record_text(content.get("text", ""))
            records.append(
                {
                    "namespace": label,
                    "text": display[:200],
                    "record_id": record.get("memoryRecordId"),
                }
            )
    return {
        "event_count": len(events),
        "events": [
            {
                "id": e.get("eventId"),
                "at": str(e.get("eventTimestamp", "")),
                "payload": [
                    {
                        "role": p["conversational"].get("role"),
                        "text": p["conversational"].get("content", {}).get("text", "")[:120],
                    }
                    for p in e.get("payload", [])
                    if "conversational" in p
                ],
            }
            for e in events
        ],
        "records": records,
    }
