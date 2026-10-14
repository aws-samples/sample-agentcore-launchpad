"""Thumbs feedback on console chat answers.

A thumbs-down is a *bad case* the evaluation flow already knows how to consume:
`list_feedback` returns the down-voted **session ids** of the range, which the V2
"add to dataset" modal posts to `POST /api/eval/datasets/from-sessions` unchanged.
No dataset-building logic lives here.

Visibility: an answer can be rated only when its agent is visible to the caller
(`agent_access.agent_in`), and the list drops every row of an agent hidden from the
caller (`agent_access.hidden_agent_ids`) before counting or limiting. The routes pass
`require_viewer`; nothing internal calls in here.

Ported from the console-v3 branch without the issue box, answer rules, share links,
review links / `correction` and comments.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.ledger import Agent, ChatFeedback, ChatMessage
from app.services import agent_access
from app.services.visibility import Viewer

VERDICTS = ("up", "down")
SOURCE_CONSOLE = "console"
RANGE_HOURS = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}
PREVIEW_CHARS = 500
# what one from-sessions call accepts (pipelines.MAX_SESSIONS_PER_CALL)
MAX_SESSION_IDS = 50


def _iso(value: datetime | None) -> str | None:
    """SQLite drops the zone; every stored time is UTC, so say so on the wire."""
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or UTC).isoformat()


def _answer_in(
    db: Session, workspace_id: str, message_id: int, viewer: Viewer
) -> ChatMessage:
    """The agent answer being rated, or 404 — a message of another workspace, a
    user turn / tool row, and an answer of an agent hidden from `viewer` all read
    exactly like a missing one."""
    row = db.get(ChatMessage, message_id)
    if (
        row is None
        or row.workspace_id != workspace_id
        or row.role != "agent"
        or agent_access.agent_in(
            db, workspace_id, row.agent_id, viewer=viewer, include_deleted=True
        ) is None
    ):
        raise NotFoundError("feedback.message_not_found", "message not found")
    return row


def rate(
    db: Session,
    *,
    workspace_id: str,
    message_id: int,
    verdict: str | None,
    actor: str,
    viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    """Upsert `actor`'s verdict on one answer; `verdict=None` withdraws it (a no-op
    when there is none)."""
    answer = _answer_in(db, workspace_id, message_id, viewer)
    row = (
        db.query(ChatFeedback)
        .filter(ChatFeedback.message_id == message_id, ChatFeedback.actor == actor)
        .first()
    )
    if verdict is None:
        if row is not None:
            db.delete(row)
            db.commit()
        return {"message_id": message_id, "verdict": None}
    if row is None:
        db.add(ChatFeedback(
            workspace_id=workspace_id, agent_id=answer.agent_id,
            session_id=answer.session_id, message_id=message_id, verdict=verdict,
            actor=actor, source=SOURCE_CONSOLE,
        ))
    else:
        row.verdict = verdict
    db.commit()
    return {"message_id": message_id, "verdict": verdict}


def verdicts_for(
    db: Session, workspace_id: str, actor: str, message_ids: list[int]
) -> dict[int, str]:
    """This actor's current verdicts, to restore thumb state on a replayed thread."""
    if not message_ids:
        return {}
    rows = (
        db.query(ChatFeedback.message_id, ChatFeedback.verdict)
        .filter(
            ChatFeedback.workspace_id == workspace_id,
            ChatFeedback.actor == actor,
            ChatFeedback.message_id.in_(message_ids),
        )
        .all()
    )
    return {message_id: verdict for message_id, verdict in rows}


def since_for(range_key: str, now: datetime | None = None) -> datetime:
    """Start of the range, in UTC (ledger times are stored as UTC wall clock)."""
    return (now or datetime.now(UTC)) - timedelta(hours=RANGE_HOURS[range_key])


def list_feedback(
    db: Session,
    workspace_id: str,
    *,
    range_key: str = "24h",
    verdict: str | None = None,
    agent_id: str | None = None,
    limit: int = 50,
    viewer: Viewer = Viewer.ALL,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Feedback given in the range (by `updated_at` — when the verdict was last set).

    `totals` counts both verdicts over the range regardless of `verdict`; `items` are
    the newest `limit` rows (optionally one verdict); `down_session_ids` are the
    distinct sessions with a thumbs-down in the range, newest first, capped at what
    one from-sessions call accepts. Hidden agents are filtered in SQL first."""
    since = since_for(range_key, now)
    base = db.query(ChatFeedback).filter(
        ChatFeedback.workspace_id == workspace_id,
        ChatFeedback.updated_at >= since,
    )
    hidden = agent_access.hidden_agent_ids(db, viewer, workspace_id)
    if hidden:
        base = base.filter(ChatFeedback.agent_id.not_in(hidden))
    if agent_id:
        base = base.filter(ChatFeedback.agent_id == agent_id)

    totals = dict.fromkeys(VERDICTS, 0)
    for found, n in (
        base.with_entities(ChatFeedback.verdict, func.count(ChatFeedback.id))
        .group_by(ChatFeedback.verdict)
        .all()
    ):
        if found in totals:
            totals[found] = int(n)

    query = base
    if verdict:
        query = query.filter(ChatFeedback.verdict == verdict)
    # the question is the closest preceding user turn of the same session
    question_id = (
        select(func.max(ChatMessage.id))
        .where(
            ChatMessage.workspace_id == workspace_id,
            ChatMessage.session_id == ChatFeedback.session_id,
            ChatMessage.role == "user",
            ChatMessage.id < ChatFeedback.message_id,
        )
        .correlate(ChatFeedback)
        .scalar_subquery()
    )
    rows = (
        query.with_entities(ChatFeedback, question_id)
        .order_by(ChatFeedback.updated_at.desc(), ChatFeedback.id)
        .limit(limit)
        .all()
    )

    message_ids = {r.message_id for r, _ in rows} | {q for _, q in rows if q is not None}
    messages = {
        mid: (text or "", created_at)
        for mid, text, created_at in db.query(
            ChatMessage.id, func.substr(ChatMessage.text, 1, PREVIEW_CHARS),
            ChatMessage.created_at,
        ).filter(ChatMessage.id.in_(message_ids or {-1}))
    }
    names = {
        a_id: name
        for a_id, name in db.query(Agent.id, Agent.name).filter(
            Agent.id.in_({r.agent_id for r, _ in rows} or {""})
        )
    }
    items = []
    for r, q_id in rows:
        answer_text, answered_at = messages.get(r.message_id, ("", None))
        items.append({
            "id": r.id,
            "agent_id": r.agent_id,
            "agent_name": names.get(r.agent_id, r.agent_id),
            "session_id": r.session_id,
            "message_id": r.message_id,
            "verdict": r.verdict,
            "actor": r.actor,
            "source": r.source,
            "question": messages.get(q_id, ("", None))[0] if q_id is not None else "",
            "answer": answer_text,
            "answered_at": _iso(answered_at),
            "created_at": _iso(r.created_at),
            "updated_at": _iso(r.updated_at),
        })

    latest = func.max(ChatFeedback.updated_at)
    down_session_ids = [
        sid
        for sid, _at in base.filter(ChatFeedback.verdict == "down")
        .with_entities(ChatFeedback.session_id, latest)
        .group_by(ChatFeedback.session_id)
        .order_by(latest.desc())
        .limit(MAX_SESSION_IDS)
        .all()
    ]
    return {"totals": totals, "items": items, "down_session_ids": down_session_ids}
