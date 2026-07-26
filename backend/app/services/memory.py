"""AgentCore Memory helpers — session events (short-term) + records (long-term)."""

from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.services.agentcore.client import data_client


def memory_id_or_none() -> str | None:
    """The bootstrap singleton's memory id, or None before bootstrap has run.

    The console's landing view needs to distinguish "not bootstrapped" from
    "AWS call failed", so it reads the id without raising.
    """
    return get_settings().resources.get("memory_id") or None


def _memory_id() -> str:
    memory_id = memory_id_or_none()
    if not memory_id:
        raise RuntimeError("memory_id missing from config — run scripts/bootstrap.py")
    return memory_id


SCOPE_SEP = "__"


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
    actor_id: str, session_id: str, prompt: str, answer: str
) -> dict[str, Any]:
    """Persist one conversation turn into short-term memory (feeds extraction)."""
    return data_client().create_event(
        memoryId=_memory_id(),
        actorId=actor_id,
        sessionId=session_id,
        eventTimestamp=datetime.now(UTC),
        payload=[
            {"conversational": {"role": "USER", "content": {"text": prompt}}},
            {"conversational": {"role": "ASSISTANT", "content": {"text": answer}}},
        ],
    )


def list_events(actor_id: str, session_id: str, max_results: int = 20) -> list[dict]:
    return data_client().list_events(
        memoryId=_memory_id(),
        actorId=actor_id,
        sessionId=session_id,
        includePayloads=True,
        maxResults=min(max_results, 100),
    ).get("events", [])


def list_records(namespace_prefix: str, max_results: int = 20) -> list[dict]:
    return data_client().list_memory_records(
        memoryId=_memory_id(),
        namespacePath=namespace_prefix,
        maxResults=min(max_results, 100),
    ).get("memoryRecordSummaries", [])


def retrieve_records(namespace: str, query: str, top_k: int = 3) -> list[dict]:
    return data_client().retrieve_memory_records(
        memoryId=_memory_id(),
        namespace=namespace,
        searchCriteria={"searchQuery": query, "topK": top_k},
    ).get("memoryRecordSummaries", [])


def session_memory_summary(actor_id: str, session_id: str) -> dict[str, Any]:
    """Right-rail panel data: event count + long-term records for the actor."""
    events = list_events(actor_id, session_id)
    records: list[dict[str, Any]] = []
    for label in ("/preferences", "/facts"):
        # actor_id is already agent-scoped (see scoped_actor); the display label
        # keeps just the strategy — the actor/agent is implied by the session.
        for record in list_records(f"{label}/{actor_id}", max_results=10):
            content = record.get("content", {})
            records.append(
                {
                    "namespace": label,
                    "text": content.get("text", "")[:200],
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
