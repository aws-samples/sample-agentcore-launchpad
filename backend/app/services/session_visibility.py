"""Chat-session visibility — the CN member rule as ONE predicate (Family B step 1 + 2).

Step 1 (user decision 2026-10-16): a member lists only the chat sessions THEY opened —
`chat_sessions.created_by` = their `users.id`, or, for a row older than the column, one
whose first actor is their username — an administrator every session.

Chat sessions are PERSONAL in BOTH visibility modes (users-and-ownership §4 / §7, E04
NC-E04-06; N16-obs70 / N16-obs71, 2026-10-19): every by-id chat route — history, memory
rail, stop, reattach, and a `POST /api/chat/{agent}` that names an existing `session_id` —
resolves the row through `require_own` and answers `404 chat.session_not_found` for a
session the caller could not list, under `shared` as under `isolated`. (The 2026-10-16
ruling that by-id access "stays open under shared" let a member end an administrator's live
session, read its memory rail and append turns into it.) A session with no ledger row
(never written by the console or `/v1`, or the first turn still running) has an unknown
owner and stays open to whoever holds the id.

Step 2 (`isolated` only) additionally applies the same predicate to the observability
`traces` / `sessions` lists and details (`hidden_session_ids` / `mentions`): under `shared`
those stay workspace-wide by accepted design (E04 §7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.ledger import ChatSession

if TYPE_CHECKING:
    from app.routers.auth import Identity
    from app.services.visibility import Viewer


def own_clause(identity: Identity, human_actor: str) -> Any:
    """The sessions `identity` may list, as a SQL predicate — or None (no filter) for an
    administrator."""
    if identity.is_admin:
        return None
    legacy = and_(ChatSession.created_by.is_(None), ChatSession.actor_id == human_actor)
    if identity.user_id is None:
        return legacy
    return or_(ChatSession.created_by == identity.user_id, legacy)


def may_list(identity: Identity, human_actor: str, session: ChatSession | None) -> bool:
    """The per-row form of `own_clause`. A missing row (no ledger session yet) is open."""
    if session is None or identity.is_admin:
        return True
    mine = session.created_by is not None and session.created_by == identity.user_id
    legacy_mine = session.created_by is None and session.actor_id == human_actor
    return mine or legacy_mine


def require_own(identity: Identity, human_actor: str, session: ChatSession | None,
                ) -> ChatSession | None:
    """THE choke point of every by-id chat route (N16-obs70/71): the row the caller may act
    on — their own (`may_list`), or any as an administrator — in BOTH visibility modes; a
    missing row (no ledger session yet) passes through as None for the caller to treat as
    open or as the route's 404. Anything else is `404 chat.session_not_found`, never 403:
    a session id is not probeable, and the answer is the one an unknown id gets."""
    if not may_list(identity, human_actor, session):
        raise NotFoundError("chat.session_not_found", "chat session not found")
    return session


def hidden_session_ids(
    db: Session, workspace_id: str | None, identity: Identity, human_actor: str, viewer: Viewer
) -> frozenset[str]:
    """Under isolation: the ledger session ids the member could not list. Empty, without a
    query, for every viewer who does not filter (shared workspace, administrators)."""
    if not viewer.filters:
        return frozenset()
    own = own_clause(identity, human_actor)
    if own is None:
        return frozenset()
    rows = db.query(ChatSession.session_id).filter(
        ChatSession.workspace_id == workspace_id, ~own
    )
    hidden = {sid for (sid,) in rows if sid}
    # per-user policy: a hidden conversation's per-principal runtime sessions (`lpp-…`,
    # principal_mint) carry its spans and memory — hidden with it
    from app.services import principal_mint

    hidden |= principal_mint.derived_session_ids(db, session_ids=hidden)
    return frozenset(hidden)


def mentions(payload: Any, hidden: frozenset[str], depth: int = 0) -> bool:
    """True when an observability payload carries a hidden session id anywhere."""
    if not hidden or depth > 12:
        return False
    if isinstance(payload, str):
        return payload in hidden
    if isinstance(payload, dict):
        return any(mentions(v, hidden, depth + 1) for v in payload.values())
    if isinstance(payload, list | tuple):
        return any(mentions(v, hidden, depth + 1) for v in payload)
    return False
