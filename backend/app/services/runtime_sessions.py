"""The readers' view of the per-principal runtime sessions (per-user policy stream A,
re-review ~21:40Z ruling; contract doc "Stream A — hub runtime sessions").

With the principal carrier on, a conversation's turns run in one derived AgentCore Runtime
session per principal (``lpp-…``, ``principal_mint.runtime_session_id``), so its spans and
its short-term memory live under those ids, not under the caller's session id the console and
the ledger keep. Every reader that looks a conversation up by the caller's id goes through
this module:

* ``runtime_sessions_for`` — the derived ids of one conversation the viewer may read (the
  existing viewer rules: the conversation's ledger row and its agent);
* ``conversation_of`` / ``fold_session_rows`` — span-derived rows (observability lists) shown
  under the conversation they belong to, never as separate ``lpp-`` sessions;
* ``expand_session_ids`` — what a passive evaluation over chat session ids must read.

Nothing here queries anything while no derived session exists; with the switch off the readers
are byte-identical to before.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.ledger import Agent, ChatSession
from app.models.policy import PolicyRuntimeSession
from app.services.visibility import Viewer

PREFIX = "lpp-"


def conversation_of(db: Session, workspace_id: str | None,
                    runtime_session_ids: list[str]) -> dict[str, str]:
    """``{derived id: the caller's session id}`` for the derived ids among
    ``runtime_session_ids`` that belong to this workspace's agents."""
    derived = sorted({s for s in runtime_session_ids if s and s.startswith(PREFIX)})
    if not derived:
        return {}
    rows = (
        db.query(PolicyRuntimeSession.runtime_session_id, PolicyRuntimeSession.session_id)
        .join(Agent, Agent.id == PolicyRuntimeSession.agent_id)
        .filter(Agent.workspace_id == workspace_id,
                PolicyRuntimeSession.runtime_session_id.in_(derived))
        .all()
    )
    return {r[0]: r[1] for r in rows}


def runtime_sessions_for(db: Session, workspace_id: str | None, session_id: str,
                         viewer: Viewer = Viewer.ALL, *,
                         agent_id: str | None = None) -> list[str]:
    """The derived runtime sessions of the conversation ``session_id`` (optionally of one
    agent) that ``viewer`` may read: none when the viewer may not see the conversation's
    ledger row or its agent (workspace isolation), all of them otherwise."""
    if not session_id or session_id.startswith(PREFIX):
        return []
    query = (
        db.query(PolicyRuntimeSession.runtime_session_id, PolicyRuntimeSession.agent_id)
        .join(Agent, Agent.id == PolicyRuntimeSession.agent_id)
        .filter(Agent.workspace_id == workspace_id,
                PolicyRuntimeSession.session_id == session_id)
    )
    if agent_id:
        query = query.filter(PolicyRuntimeSession.agent_id == agent_id)
    rows = query.order_by(PolicyRuntimeSession.created_at).all()
    if not rows:
        return []
    if viewer.filters:
        from app.services import visibility
        from app.services.agent_access import HiddenAgents

        hidden = HiddenAgents(db, viewer, workspace_id)
        owner = (
            db.query(ChatSession.created_by)
            .filter(ChatSession.workspace_id == workspace_id,
                    ChatSession.session_id == session_id)
            .first()
        )
        if owner is not None and not visibility.sees(viewer, db, owner[0]):
            return []
        rows = [r for r in rows if not hidden.hides(r[1])]
    return [r[0] for r in rows]


def source_session_ids(db: Session, workspace_id: str | None, session_id: str,
                       viewer: Viewer = Viewer.ALL) -> list[str]:
    """The complete source set for conversation details and their authorization.

    Keep the original id even for governed chats: pre-carrier spans or colliding
    remote records under that id must not be silently ignored.
    """
    return list(dict.fromkeys([
        session_id, *runtime_sessions_for(db, workspace_id, session_id, viewer),
    ]))


def _merge_tokens(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for key, value in (b or {}).items():
        if isinstance(value, int | float) and isinstance(out.get(key), int | float):
            out[key] = out[key] + value
        elif key not in out:
            out[key] = value
    return out


def fold_session_rows(rows: list[dict[str, Any]], mapping: dict[str, str]) -> list[dict[str, Any]]:
    """Observability session rows with each derived ``lpp-`` row shown under its conversation:
    counts summed into the conversation's row (one per conversation and service), first / last
    widened, ``runtime_session_ids`` listing the derived ids it absorbed (additive key)."""
    if not mapping:
        return rows
    out: list[dict[str, Any]] = []
    index: dict[tuple[str, Any], dict[str, Any]] = {}
    for row in rows:
        sid = row.get("session_id") or ""
        caller = mapping.get(sid)
        key = (caller or sid, row.get("service"))
        target = index.get(key)
        if target is None:
            target = dict(row) if caller is None else {
                **row, "session_id": caller, "platform": True, "runtime_session_ids": [sid]}
            index[key] = target
            out.append(target)
            continue
        for count in ("traces", "llm_calls", "errors"):
            target[count] = int(target.get(count) or 0) + int(row.get(count) or 0)
        target["tokens"] = _merge_tokens(target.get("tokens") or {}, row.get("tokens") or {})
        firsts = [t for t in (target.get("first"), row.get("first")) if t]
        lasts = [t for t in (target.get("last"), row.get("last")) if t]
        target["first"] = min(firsts) if firsts else None
        target["last"] = max(lasts) if lasts else None
        if caller is not None:
            target.setdefault("runtime_session_ids", []).append(sid)
            target["platform"] = True
    return out


# a console message older than a conversation's first derived session by more than this ran
# under the caller's own id (before the carrier was switched on). The 5 s are clock-skew
# slack, nothing more: the user message row is written moments BEFORE the turn records its
# derived session, so without the margin every governed conversation's first message would
# look pre-carrier. The cutoff stays anchored to the first derived session's created_at.
PRE_CARRIER_MARGIN_S = 5.0


def expand_session_ids(db: Session, workspace_id: str | None, session_ids: list[str], *,
                       agent_id: str | None = None) -> list[str]:
    """The sessions a passive evaluation over ``session_ids`` reads, in order, deduped: a
    conversation the carrier ran is read through its derived runtime sessions — its caller
    id is kept only when console messages predate the first derived session (turns from
    before the switch was on), so a fully governed conversation does not yield an empty,
    never-ready caller session. Conversations without derived sessions are unchanged."""
    if not session_ids:
        return list(session_ids)
    query = (
        db.query(PolicyRuntimeSession.session_id, PolicyRuntimeSession.runtime_session_id,
                 PolicyRuntimeSession.created_at)
        .join(Agent, Agent.id == PolicyRuntimeSession.agent_id)
        .filter(Agent.workspace_id == workspace_id,
                PolicyRuntimeSession.session_id.in_(list(session_ids)))
    )
    if agent_id:
        query = query.filter(PolicyRuntimeSession.agent_id == agent_id)
    by_caller: dict[str, list[str]] = {}
    first_at: dict[str, Any] = {}
    for caller, derived, created_at in query.order_by(PolicyRuntimeSession.created_at).all():
        by_caller.setdefault(caller, []).append(derived)
        first_at.setdefault(caller, created_at)
    if not by_caller:
        return list(session_ids)
    from datetime import timedelta

    from app.models.ledger import ChatMessage

    keep_caller: set[str] = set()
    for caller, started in first_at.items():
        if started is None:
            keep_caller.add(caller)
            continue
        earlier = (
            db.query(ChatMessage.id)
            .filter(ChatMessage.workspace_id == workspace_id, ChatMessage.session_id == caller,
                    ChatMessage.created_at < started - timedelta(seconds=PRE_CARRIER_MARGIN_S))
            .first()
        )
        if earlier is not None:
            keep_caller.add(caller)
    out: list[str] = []
    for sid in session_ids:
        derived = by_caller.get(sid)
        items = [sid] if not derived else ([sid] if sid in keep_caller else []) + derived
        for item in items:
            if item not in out:
                out.append(item)
    return out
