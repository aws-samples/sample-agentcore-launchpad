"""Chat transcript ledger — the ``ChatSession`` / ``ChatMessage`` rows behind the
console's session list and replayable history.

Both entrances write here: the Chat console (``routers/chat.py``) and the public
``/v1`` API (``routers/public_api.py``). Each row carries a ``source`` so an
operator can tell public-API traffic from console traffic in the session list
(N-SCN-obs1); before that marker existed only the console router persisted rows,
so a ``/v1`` session was never listed.

Writes open their own short-lived session: the SSE generators outlive the
request scope that owned the request's ``db``.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.ledger import ChatMessage, ChatSession
from app.services import creators

SOURCE_CONSOLE = "console"
SOURCE_PUBLIC_API = "public_api"


# A6: the deny card's facts a `policy` history row keeps (additive `policy` key on restore);
# N17-obs526: + the deciding gateway and its engine, so the restored card links where the live
# card did (`restoredPolicyDeny` reads `policy.engine_id`)
POLICY_ROW_FACTS = ("policy_id", "policy_name", "stage", "source", "decision_id", "mode",
                    "gateway_id", "engine_id")


def policy_row_facts(data: dict[str, Any]) -> dict[str, Any] | None:
    facts = {k: data[k] for k in POLICY_ROW_FACTS if data.get(k)}
    return {"policy": facts} if facts else None


def save_message(
    workspace_id: str,
    agent_id: str,
    session_id: str,
    role: str,
    text: str,
    name: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    truncated: bool = False,
    tool_call: dict[str, Any] | None = None,
) -> None:
    """``truncated`` marks an agent row the runtime closed at the model's token limit
    (N-SCN-bug4); the console replays it as an inline notice. ``tool_call`` (N16-obs85) is
    the bounded call summary of a ``tool`` row (``{id}`` at the call, completed by
    ``update_tool_message``)."""
    db = SessionLocal()
    try:
        db.add(ChatMessage(workspace_id=workspace_id, agent_id=agent_id,
                           session_id=session_id,
                           role=role, text=text[:100000], name=name, attachments=attachments,
                           truncated=truncated, tool_call=tool_call))
        db.commit()
    finally:
        db.close()


def tool_call_stamp(data: dict[str, Any]) -> dict[str, Any] | None:
    """The ``tool_call`` a ``tool`` event opens: its id when the runtime gave one."""
    tool_id = str(data.get("id") or "").strip()
    return {"id": tool_id} if tool_id else None


def update_tool_message(
    workspace_id: str, agent_id: str, session_id: str, tool_id: str | None,
    name: str | None = None, **fields: Any,
) -> bool:
    """Complete the ``tool_call`` of the session's matching ``tool`` row (N16-obs85):
    by ``tool_id`` when the runtime gave one, else the newest row of that ``name``, else
    the newest tool row. ``fields`` are the summary keys (``input``, ``result_summary``,
    ``status``) — values are stored as given (the invoke layer already bounded them).
    ``False`` when no row matched (a summary the runtime streamed before its ``tool``
    event is dropped, never invented)."""
    db = SessionLocal()
    try:
        rows = (
            db.query(ChatMessage)
            .filter(
                ChatMessage.workspace_id == workspace_id,
                ChatMessage.agent_id == agent_id,
                ChatMessage.session_id == session_id,
                ChatMessage.role == "tool",
            )
            .order_by(ChatMessage.id.desc())
            .limit(20)
            .all()
        )
        row = None
        if tool_id:
            row = next((r for r in rows if (r.tool_call or {}).get("id") == tool_id), None)
        if row is None and name:
            row = next((r for r in rows if r.name == name), None)
        if row is None and not tool_id and rows:
            row = rows[0]
        if row is None:
            return False
        merged = dict(row.tool_call or {})
        merged.update({k: v for k, v in fields.items() if v is not None})
        row.tool_call = merged
        db.commit()
        return True
    finally:
        db.close()


def track_session(
    workspace_id: str, agent_id: str, session_id: str, actor_id: str,
    runtime_version: str | None = None,
    source: str = SOURCE_CONSOLE,
    creator: creators.Creator | None = None,
) -> None:
    """Upsert the session row and count one more turn. ``source`` and ``creator`` (the
    console principal — or the ``/v1`` key's creator — that opened the session; CN
    extra, Family B step 1) are recorded when the row is created; a continuing session
    keeps its original entrance and its first opener, whoever sends the later turns."""
    db = SessionLocal()
    try:
        row = (
            db.query(ChatSession)
            .filter(
                ChatSession.workspace_id == workspace_id,
                ChatSession.agent_id == agent_id,
                ChatSession.session_id == session_id,
            )
            .first()
        )
        if row is None:
            row = ChatSession(workspace_id=workspace_id, agent_id=agent_id,
                              session_id=session_id, actor_id=actor_id,
                              runtime_version=runtime_version, source=source)
            creators.stamp(row, creator)
            db.add(row)
        row.turns = (row.turns or 0) + 1
        row.last_at = datetime.now(UTC)
        # ``ended_at`` is never cleared here: a turn under an ENDED id is refused by
        # ``require_session_open`` in the shared invoke chain before any entrance reaches
        # this upsert (N16-obs79 console, N16-obs88 ``/v1``), so the marker outlasts
        # every later request on that id.
        db.commit()
    finally:
        db.close()


def session_ended(session_id: str, ended_at: datetime) -> AppError:
    """The one ``409 chat.session_ended {session_id, ended_at}`` envelope both entrances
    answer (N16-obs79 console route, N16-obs88 ``/v1``)."""
    return AppError(
        "chat.session_ended",
        "this chat session was ended; start a new session",
        {"session_id": session_id, "ended_at": ended_at.isoformat()},
        status_code=409,
    )


def require_session_open(workspace_id: str, agent_id: str, session_id: str) -> None:
    """Refuse a turn under an ENDED session id — the shared gate of the invoke chain.

    END SESSION stamps the ledger row ``ended_at`` and terminates the runtime session;
    the next prompt opens a NEW session (AC-E04-08). Until N16-obs88 only the console
    route checked this (``chat_turns.require_session_open``) while ``/v1 …/invoke`` and
    ``/invoke-stream`` re-opened the id (``track_session`` cleared ``ended_at``). The
    check now sits in ``services.invoke`` — ``invoke_agent_text`` / ``invoke_agent_events``,
    before the policy gate and any AWS call — so every entrance refuses identically, and
    the stream routes call it once more before their response opens so the refusal is a
    plain 409, not an ``error`` frame. An id with no row (a new session) passes."""
    db = SessionLocal()
    try:
        row = (
            db.query(ChatSession)
            .filter(
                ChatSession.workspace_id == workspace_id,
                ChatSession.agent_id == agent_id,
                ChatSession.session_id == session_id,
            )
            .first()
        )
        if row is not None and row.ended_at is not None:
            raise session_ended(session_id, row.ended_at)
    finally:
        db.close()


def record_turn(
    workspace_id: str,
    agent_id: str,
    session_id: str,
    actor_id: str,
    prompt: str,
    answer: str,
    *,
    runtime_version: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    source: str = SOURCE_CONSOLE,
    truncated: bool = False,
    creator: creators.Creator | None = None,
) -> None:
    """One buffered turn (the sync ``/v1`` invoke): session row + user and agent items.
    ``truncated`` is the buffered result's max_tokens marker (N-SCN-bug4)."""
    track_session(workspace_id, agent_id, session_id, actor_id,
                  runtime_version=runtime_version, source=source, creator=creator)
    save_message(workspace_id, agent_id, session_id, "user", prompt, attachments=attachments)
    if answer:
        save_message(workspace_id, agent_id, session_id, "agent", answer, truncated=truncated)


def record_stream(
    events: Iterator[dict[str, Any]],
    *,
    workspace_id: str,
    agent_id: str,
    actor_id: str,
    prompt: str,
    runtime_version: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    source: str = SOURCE_PUBLIC_API,
    creator: creators.Creator | None = None,
) -> Iterator[dict[str, Any]]:
    """Pass a ``chat_stream`` event sequence through, persisting the transcript
    in event order (``meta`` → session + user item; ``tool`` splits the answer
    bubble; ``error`` keeps the partial answer; ``done`` closes it). Used by the
    public ``/v1`` stream, which has no console identity or consent flow — an
    ``auth_required`` ask cannot occur there (``console_user=False`` turns it
    into an ``error`` event). The runtime's ``truncated: true`` marker (its own
    ``truncated`` event or ``complete{truncated: true}``) flags the open bubble
    — one flag per bubble, exactly as the console router does (N-SCN-bug4)."""
    session_id: str | None = None
    answer_parts: list[str] = []
    bubble_truncated = False

    def close_bubble() -> None:
        nonlocal bubble_truncated
        if answer_parts and session_id:
            save_message(workspace_id, agent_id, session_id, "agent", "".join(answer_parts),
                         truncated=bubble_truncated)
        answer_parts.clear()
        bubble_truncated = False

    for event in events:
        kind, data = event["event"], event["data"]
        if kind == "meta":
            session_id = data["session_id"]
            track_session(workspace_id, agent_id, session_id, actor_id,
                          runtime_version=runtime_version, source=source, creator=creator)
            save_message(workspace_id, agent_id, session_id, "user", prompt,
                         attachments=attachments)
        elif kind == "tool" and session_id:
            close_bubble()
            save_message(workspace_id, agent_id, session_id, "tool", "", name=data.get("name"),
                         tool_call=tool_call_stamp(data))
        elif kind == "tool_input" and session_id:
            update_tool_message(workspace_id, agent_id, session_id, data.get("id"),
                                data.get("name"), input=data.get("input"))
        elif kind == "tool_result" and session_id:
            update_tool_message(workspace_id, agent_id, session_id, data.get("id"),
                                data.get("name"), result_summary=data.get("summary"),
                                status=data.get("status"))
        elif kind == "policy_denied" and session_id:
            save_message(workspace_id, agent_id, session_id, "policy",
                         data.get("reason", ""), name=data.get("tool"),
                         tool_call=policy_row_facts(data))
        elif kind == "delta":
            answer_parts.append(data.get("text", ""))
        elif kind in ("truncated", "complete") and data.get("truncated"):
            # the notice text arrived as a delta just before; the marker only flags it
            bubble_truncated = True
        elif kind == "error" and session_id:
            close_bubble()
            save_message(workspace_id, agent_id, session_id, "error", data.get("message", ""))
        elif kind == "done" and session_id:
            close_bubble()
        yield event
    if session_id:
        close_bubble()
