"""Assistant-owned data is private to its principal.

Two guards share one source of truth (``assistant_messages.runtime_session_id``
joined to ``assistant_conversations.owner_principal``):

* ``refuse_assistant_session`` — the generic invoke entrances (console Chat,
  ``POST /api/agents/{id}/invoke``, ``/v1``) answer 404 for an assistant runtime
  session id presented on a system-managed agent, so nobody can continue or
  observe a private turn through a shared path. Consulted only for rows that
  carry ``Agent.system_key`` and only when a session id was supplied.
* ``PrivateSessions`` — the Observability views (session/trace lists, details, the
  cached span payloads and on-demand evaluation) hide every runtime session that
  belongs to another principal's conversation **after** the per-workspace cache,
  so a cached payload built for one caller can never be served to another. The
  owner still sees their own sessions; ordinary agents' sessions are untouched.
"""

from typing import Any

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import NotFoundError
from app.models.assistant import AssistantConversation, AssistantMessage
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


class PrivateSessions:
    """Runtime session ids of a workspace's assistant turns that the caller may NOT
    see: every assistant session whose conversation principal differs from the
    caller's (a NULL principal is visible to nobody)."""

    def __init__(self, db: Session, workspace_id: str, principal: str | None):
        rows = (
            db.query(AssistantMessage.runtime_session_id, AssistantConversation.owner_principal)
            .join(AssistantConversation,
                  AssistantConversation.id == AssistantMessage.conversation_id)
            .filter(
                AssistantMessage.workspace_id == workspace_id,
                AssistantMessage.runtime_session_id.isnot(None),
            )
            .all()
        )
        self.hidden: set[str] = {
            sid for sid, owner in rows
            if sid and (owner is None or principal is None or owner != principal)
        }

    def visible(self, session_id: str | None) -> bool:
        return not session_id or session_id not in self.hidden

    def mentions_hidden(self, payload: Any, depth: int = 0) -> bool:
        """True when any string anywhere in ``payload`` is a hidden session id (span
        attributes, transcripts and message events all carry the id verbatim)."""
        if not self.hidden or depth > 12:
            return False
        if isinstance(payload, str):
            return payload in self.hidden
        if isinstance(payload, dict):
            return any(self.mentions_hidden(v, depth + 1) for v in payload.values())
        if isinstance(payload, list | tuple):
            return any(self.mentions_hidden(v, depth + 1) for v in payload)
        return False

    def require_visible(self, session_id: str | None) -> None:
        if not self.visible(session_id):
            raise NotFoundError("observability.session_not_found", "session not found")
