"""Chat-ledger references for the extraction reconciliation (eval accuracy item 1).

A session-scope run, SCORE NOW and an online sample evaluate sessions the platform did not
replay itself, so the worker has no answer of its own to compare the extraction with. When
the session is a platform chat session (console Chat or ``/v1``), the chat ledger holds what
the user was shown: ``chat_messages`` rows in order, one ``user`` row per turn and the
turn's answer bubbles as ``agent`` rows (a ``tool`` event closes a bubble — the last bubble
is the answer, ``AgentResult`` semantics). The reference is the LAST ``agent`` row of each
user turn (``""`` when the turn showed none — an error before any text); a bubble the
runtime cut at its token limit carries the platform's notice and is not compared (``None``).
A session with no ledger rows gets no reference — its check is ``unverifiable``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.ledger import ChatMessage
from app.services.strands_eval import extraction_check

CHUNK = 500  # session ids per IN query (SQLite's bound-parameter limit)


def ledger_answers(rows: list[ChatMessage]) -> list[str | None]:
    """Per user turn, the last ``agent`` row's text (``None`` when truncated, ``""`` when the
    turn has no agent row). ``rows`` are one session's ``user`` / ``agent`` rows by id."""
    answers: list[str | None] = []
    for row in rows:
        if row.role == "user":
            answers.append("")
        elif row.role == "agent" and answers:
            answers[-1] = None if row.truncated else str(row.text or "")
    return answers


def ledger_references(
    db: Session, workspace_id: str | None, session_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """``{session_id: extraction_check.reference("chat_ledger", answers)}`` for the sessions
    the workspace's chat ledger knows; the others are absent (``unverifiable``)."""
    wanted = [str(s) for s in dict.fromkeys(session_ids or []) if s]
    by_session: dict[str, list[ChatMessage]] = {}
    for start in range(0, len(wanted), CHUNK):
        rows = (
            db.query(ChatMessage)
            .filter(ChatMessage.workspace_id == workspace_id,
                    ChatMessage.session_id.in_(wanted[start:start + CHUNK]),
                    ChatMessage.role.in_(("user", "agent")))
            .order_by(ChatMessage.id.asc())
            .all()
        )
        for row in rows:
            by_session.setdefault(row.session_id, []).append(row)
    return {sid: extraction_check.reference(extraction_check.SOURCE_CHAT_LEDGER,
                                            ledger_answers(found))
            for sid, found in by_session.items() if any(r.role == "user" for r in found)}


def references_for(workspace_id: str | None, session_ids: list[str]) -> dict[str, dict[str, Any]]:
    """:func:`ledger_references` on its own ledger session (planners outside a request);
    a ledger read failure degrades to no references — the checks read ``unverifiable``."""
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        return ledger_references(db, workspace_id, session_ids)
    except Exception:  # noqa: BLE001 — the reconciliation must never fail a run
        return {}
    finally:
        db.close()
