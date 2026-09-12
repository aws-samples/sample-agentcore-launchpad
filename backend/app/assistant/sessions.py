"""Assistant-owned runtime sessions are private to the assistant.

Every assistant turn mints its own AgentCore runtime session id (64 hex chars) and
records it on the transcript row. The generic entrances — console Chat, ``POST
/api/agents/{id}/invoke`` and the public ``/v1`` — accept caller-supplied session
ids for any agent a member may reach, including the preset itself; this guard
makes an assistant session id indistinguishable from an unknown one on those
paths. It is consulted only for rows that carry ``Agent.system_key`` and only when
a session id was supplied, so ordinary agents pay nothing.
"""

from app.core.db import SessionLocal
from app.core.errors import NotFoundError
from app.models.assistant import AssistantMessage
from app.models.ledger import Agent


def is_assistant_session(session_id: str | None) -> bool:
    if not session_id:
        return False
    db = SessionLocal()
    try:
        return (
            db.query(AssistantMessage.id)
            .filter(AssistantMessage.runtime_session_id == session_id)
            .first()
            is not None
        )
    finally:
        db.close()


def refuse_assistant_session(agent: Agent, session_id: str | None) -> None:
    """404 (never 403 — the id must not be confirmed to exist) for an assistant
    session presented to a generic entrance of a system-managed agent."""
    if not session_id or not getattr(agent, "system_key", None):
        return
    if is_assistant_session(session_id):
        raise NotFoundError("chat.session_not_found", "chat session not found")
