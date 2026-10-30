"""Chat playground turns as detached server-side work — N-ARCH-obs15 v2 (chat parity).

Before this module a console turn (``POST /api/chat/{agent_id}``) lived and died with its
SSE response: the generator that drove ``chat_stream`` *was* the response body, so a
closed connection (phone lock, app background, reload) got ``GeneratorExit``, the
partial answer was persisted as whatever had streamed and the member had to resend.

Now a turn is a unit of work the process owns, exactly like an architect turn
(``app/assistant/service.py``): ``start_turn`` runs the unchanged ``chat_stream`` on a
driver thread, every event gets a monotonic ``seq`` (published to the in-memory buffer
every subscriber tails and appended to ``chat_turn_events``), the ``ChatMessage`` rows
are written by the same reducer the response used to run (``TranscriptRecorder`` — rows
land BEFORE the event is logged, so "event in the log ⇒ its rows exist"), and the
``POST`` response is merely the first subscriber. The policy gate is untouched: it still
runs inside ``invoke_agent_events`` on the driver thread, before any AWS call, and its
refusal is still the first ``error`` after ``meta``; the p2-a ``policy_denied`` /
``policy_decision`` frames flow through the log unchanged.

Durable claim: ``chat_sessions.active_turn / active_turn_started_at / active_turn_token``,
taken at ``meta`` (when ``track_session`` has the row; the in-process registry refuses a
second start before that), released after the terminal event is durable. A claim found
at startup belongs to a dead process: the sweep persists the open bubble as an
interrupted turn from the event log and appends a terminal ``error`` — never resumes.

C4: the log stores only what the wire carried — minus the single-use as_user
authorization ``url`` of an ``auth_required`` ask, which the history row drops as well.
Contract: ``docs/cn/interfaces/assistant-turns.md`` §8.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.models.ledger import ChatSession, ChatTurnEvent
from app.services import live_turns
from app.services.chat_ledger import (
    policy_row_facts,
    save_message,
    session_ended,
    tool_call_stamp,
    track_session,
    update_tool_message,
)

logger = logging.getLogger(__name__)

# Same budget as the architect's claim: past it a claim nobody in this process holds is
# an orphan (the frontend link gives up reconnecting at the same mark).
TURN_CLAIM_TTL_S = 1800
# The ``error`` row the startup sweep writes for a turn a dead process left open; keeps
# the ``interrupted`` prefix every reader keys on (parity with the assistant's text).
INTERRUPTED_TEXT = ("interrupted: the turn stopped before the reply finished — what had "
                    "streamed is kept")
# Rows that end a turn's log; a stream ends after one of them.
TERMINAL_EVENTS = live_turns.TERMINAL_EVENTS
# Events that close the open answer bubble live (the reducer mirrors them) — the sweep
# rebuilds the still-open bubble from what followed the last of them.
_BUBBLE_CLOSERS = ("tool", "error", "done")


def turn_key(agent_id: str, session_id: str) -> str:
    return f"{agent_id}:{session_id}"


class ChatLiveTurn(live_turns.LiveTurn):
    """One running console turn of one chat session."""

    def __init__(self, agent_id: str, session_id: str, workspace_id: str, actor_id: str,
                 username: str) -> None:
        super().__init__(turn_key(agent_id, session_id), workspace_id, username)
        self.agent_id = agent_id
        self.session_id = session_id
        # the bare human actor the session row records (display + "whose turn is it")
        self.actor_id = actor_id
        self.token = uuid.uuid4().hex


_LIVE_RUNS = live_turns.LiveTurnRegistry()


def live_run(agent_id: str, session_id: str, turn: int | None = None) -> ChatLiveTurn | None:
    live = _LIVE_RUNS.get(turn_key(agent_id, session_id), turn)
    assert live is None or isinstance(live, ChatLiveTurn)
    return live


# ---------------------------------------------------------------------------
# the transcript reducer (formerly routers/chat.py::generate)
# ---------------------------------------------------------------------------


class TranscriptRecorder:
    """The ``ChatMessage`` persistence of one console turn, in event order (int pk =
    replay order): ``meta`` → session row + user item; ``tool`` splits the answer bubble;
    an ``auth_required`` ask asked mid-answer is saved after that bubble's text;
    ``policy_denied`` follows its tool row; the runtime's ``truncated: true`` marker flags
    the open bubble (N-SCN-bug4); ``error`` keeps the partial answer; ``done`` closes it.

    ``persist=False`` replays a log without writing (the startup sweep rebuilds the state
    of a dead turn, then switches persistence on for the rows that are still missing)."""

    def __init__(
        self, workspace_id: str, agent_id: str, *, prompt: str, actor_id: str,
        attachments: list[dict[str, Any]] | None, runtime_version: str | None,
        save: Callable[..., None] = save_message, track: Callable[..., None] = track_session,
        persist: bool = True, creator: Any = None,
        update_tool: Callable[..., Any] = update_tool_message,
    ) -> None:
        self.workspace_id = workspace_id
        self.agent_id = agent_id
        self.prompt = prompt
        self.actor_id = actor_id
        # the console principal opening the session (`services.creators.Creator`), stamped
        # on the row the first turn creates; None on a replayed log (the row exists)
        self.creator = creator
        self.attachments = attachments
        self.runtime_version = runtime_version
        self._save = save
        self._track = track
        self._update_tool = update_tool
        self.persist = persist
        self.session_id: str | None = None
        self.answer_parts: list[str] = []
        self.pending_asks: list[tuple[str, str | None]] = []
        # one flag per bubble; a tool call opens a new one
        self.bubble_truncated = False

    def save(self, role: str, text: str, **kwargs: Any) -> None:
        if self.persist and self.session_id:
            self._save(self.workspace_id, self.agent_id, self.session_id, role, text, **kwargs)

    def update_tool(self, tool_id: Any, name: Any, **fields: Any) -> None:
        if self.persist and self.session_id:
            self._update_tool(self.workspace_id, self.agent_id, self.session_id,
                              str(tool_id or "") or None, str(name or "") or None, **fields)

    def close_bubble(self) -> None:
        if self.answer_parts:
            self.save("agent", "".join(self.answer_parts), truncated=self.bubble_truncated)
        self.answer_parts.clear()
        self.bubble_truncated = False
        for provider, tool in self.pending_asks:
            self.save("auth", provider, name=tool)
        self.pending_asks.clear()

    def feed(self, event: dict[str, Any]) -> None:
        kind, data = event["event"], event["data"]
        if kind == "meta":
            self.session_id = data["session_id"]
            if self.persist:
                self._track(self.workspace_id, self.agent_id, self.session_id, self.actor_id,
                            runtime_version=self.runtime_version, creator=self.creator)
            self.save("user", self.prompt, attachments=self.attachments)
        elif kind == "tool":
            self.close_bubble()  # a tool call splits the answer bubble live — mirror it
            self.save("tool", "", name=data.get("name"), tool_call=tool_call_stamp(data))
        elif kind == "tool_input":
            # N16-obs85: the bounded arguments complete the tool row the call opened
            self.update_tool(data.get("id"), data.get("name"), input=data.get("input"))
        elif kind == "tool_result":
            self.update_tool(data.get("id"), data.get("name"),
                             result_summary=data.get("summary"), status=data.get("status"))
        elif kind == "auth_required":
            # as_user consent ask: the Connection name and tool only — the authorization
            # URL is single-use and is never persisted, so a restored card re-asks by
            # retrying the previous user message. The card does not close an open answer
            # bubble live, so it is saved after that bubble's text, not between its halves.
            ask = (str(data.get("provider") or ""), data.get("tool"))
            if self.answer_parts:
                self.pending_asks.append(ask)
            else:
                self.save("auth", ask[0], name=ask[1])
        elif kind == "policy_denied":
            # the tool event already closed the open bubble: the card follows its tool
            # row; the reason carries the policy id for the restore
            self.save("policy", data.get("reason", ""), name=data.get("tool"),
                      tool_call=policy_row_facts(data))
        elif kind == "delta":
            self.answer_parts.append(data.get("text", ""))
        elif kind in ("truncated", "complete") and data.get("truncated"):
            # the notice text arrived as a delta just before; the marker only flags it
            self.bubble_truncated = True
        elif kind == "error":
            self.close_bubble()  # keep the partial answer the user saw
            self.save("error", data.get("message", ""))
        elif kind == "done":
            self.close_bubble()

    def finish(self) -> None:
        """The stream ended without a terminal event: the open bubble and its asks are
        still kept (exactly what the response generator did on exhaustion)."""
        self.close_bubble()

    def interrupted(self) -> None:
        """A dead process left the turn open: keep what had streamed, then say so."""
        self.close_bubble()
        self.save("error", INTERRUPTED_TEXT)


# ---------------------------------------------------------------------------
# the detached producer
# ---------------------------------------------------------------------------


def start_turn(
    agent_id: str,
    workspace_id: str,
    session_id: str,
    *,
    actor_id: str,
    username: str,
    stream: Callable[[str], Iterator[dict[str, Any]]],
    recorder: TranscriptRecorder,
) -> ChatLiveTurn:
    """Start one console turn as a server-side unit of work and return its live handle.
    ``stream(session_id)`` opens the producer (``chat_stream`` with the caller's
    arguments); the caller has already run the pre-stream refusals
    (``require_turn_capacity``). A second start for a session whose turn is still live
    here is refused (``409 chat.turn_in_progress``)."""
    live = ChatLiveTurn(agent_id, session_id, workspace_id, actor_id, username)

    def conflict(current: live_turns.LiveTurn) -> AppError:
        return _in_progress(current.turn)

    _LIVE_RUNS.start(live, _drive, (live, stream, recorder),
                     name=f"chat-turn-driver-{session_id[:8]}", conflict=conflict)
    return live


def _in_progress(turn: int | None) -> AppError:
    return AppError(
        "chat.turn_in_progress",
        "a turn of this session is still streaming; wait for it to finish",
        {"active_turn": turn}, status_code=409,
    )


def _drive(live: ChatLiveTurn, stream: Callable[[str], Iterator[dict[str, Any]]],
           recorder: TranscriptRecorder) -> None:
    """The producer: iterate ``chat_stream`` to its end whatever any subscriber does. Per
    event: transcript rows first (``recorder``), then publish with its ``seq``, then the
    event log. ``meta`` also takes the durable claim and learns the turn number (it is
    added to the published ``meta`` so a client can reattach); the terminal event is
    durable before the claim is released and before subscribers are handed it."""
    log_db = SessionLocal()
    log = live_turns.EventLogBatcher(
        lambda events: _append_turn_events(log_db, live, events),
        lambda: live.turn is not None,
    )
    events: Iterator[dict[str, Any]] | None = None
    terminal_seen = False
    try:
        try:
            events = stream(live.session_id)
            for event in events:
                kind = event["event"]
                if kind == "heartbeat":
                    log.idle()
                    continue
                if kind == "meta":
                    reported = event["data"]["session_id"]
                    if reported != live.session_id:  # a scripted producer chose its own id
                        _LIVE_RUNS.rekey(live, turn_key(live.agent_id, reported))
                        live.session_id = reported
                    recorder.feed(event)  # session row (+1 turn) and the user row
                    turn = _claim(live)
                    # ``turn`` is what a client reattaches with; ``author`` (the member who
                    # sent THIS turn — a shared session's actor is its first sender) is the
                    # durable ownership record the reattach checks once the turn is no
                    # longer live here (``ChatMessage`` rows carry no author)
                    event = {"event": "meta",
                             "data": {**event["data"], "turn": turn, "author": live.username}}
                    live.turn = turn  # ``publish`` wakes ``live_run`` waiters
                else:
                    if kind in TERMINAL_EVENTS:
                        live.finishing = True
                    recorder.feed(event)
                if kind in TERMINAL_EVENTS:
                    terminal_seen = True
                log.add(live.publish(event))
            if not terminal_seen:
                # the producer ended without ``done``/``error`` (only a scripted one can:
                # ``chat_stream`` never does) — the asks are kept as before, and the log
                # still ends so a reattaching client sees the turn end
                recorder.finish()
                log.add(live.publish({"event": "error", "data": {"message": "interrupted"}}))
        except Exception as exc:
            # ``chat_stream`` reports its own failures as ``error`` events; this is the
            # driver's own path (claim / ledger / log) — the member sees what the error
            # branch would have shown, and the partial answer is kept
            logger.exception("chat: turn driver of %s failed", live.key)
            failure = {"event": "error", "data": {"message": f"{type(exc).__name__}: {exc}"}}
            if not terminal_seen:
                try:
                    recorder.feed(failure)
                except Exception:  # pragma: no cover — the row is best effort here
                    logger.exception("chat: could not persist the failure of turn %s", live.key)
                log.pending.append(live.publish(failure))
        finally:
            try:
                log.flush()
            except Exception:  # pragma: no cover — the transcript rows are already durable
                logger.exception("chat: could not flush the event log of %s", live.key)
    finally:
        close = getattr(events, "close", None)
        if callable(close):
            try:
                close()  # idempotent on an exhausted generator; runs chat_stream's finalizer
            except Exception:  # pragma: no cover — cleanup must not mask the cause
                logger.exception("chat: closing the producer of %s failed", live.key)
        if live.turn is not None:
            try:
                _release(live)
            except Exception:  # pragma: no cover — the sweep clears what is left
                logger.exception("chat: could not release the claim of %s", live.key)
        live.finish()
        _LIVE_RUNS.drop(live)
        log_db.close()


def _loggable(event: dict[str, Any]) -> dict[str, Any]:
    """What the ledger keeps of a frame: everything the wire carried, minus the single-use
    as_user authorization URL (the history row drops it for the same reason)."""
    if event["event"] == "auth_required" and isinstance(event["data"], dict):
        return {**event, "data": {k: v for k, v in event["data"].items() if k != "url"}}
    return event


def _append_turn_events(db: Session, live: ChatLiveTurn, events: list[dict[str, Any]]) -> None:
    db.rollback()
    db.add_all([
        ChatTurnEvent(
            workspace_id=live.workspace_id, agent_id=live.agent_id,
            session_id=live.session_id, turn=live.turn, seq=e["seq"],
            event=e["event"], data=_loggable(e)["data"],
        )
        for e in events
    ])
    db.commit()


def turn_events(db: Session, agent_id: str, session_id: str, turn: int, after: int = 0,
                ) -> list[ChatTurnEvent]:
    return (
        db.query(ChatTurnEvent)
        .filter(ChatTurnEvent.agent_id == agent_id, ChatTurnEvent.session_id == session_id,
                ChatTurnEvent.turn == turn, ChatTurnEvent.seq > after)
        .order_by(ChatTurnEvent.seq.asc())
        .all()
    )


def _event_out(row: ChatTurnEvent) -> dict[str, Any]:
    return {"event": row.event, "data": row.data, "seq": row.seq}


# ---------------------------------------------------------------------------
# the durable claim
# ---------------------------------------------------------------------------


def session_row(db: Session, workspace_id: str, agent_id: str, session_id: str,
                ) -> ChatSession | None:
    return (
        db.query(ChatSession)
        .filter(ChatSession.workspace_id == workspace_id, ChatSession.agent_id == agent_id,
                ChatSession.session_id == session_id)
        .first()
    )


def _claim_is_stale(row: ChatSession, now: datetime) -> bool:
    if row.active_turn is None or row.active_turn_started_at is None:
        return False
    started = row.active_turn_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return now - started > timedelta(seconds=TURN_CLAIM_TTL_S)


def _claim(live: ChatLiveTurn) -> int:
    """Take the durable claim on the session row ``track_session`` just upserted: the
    turn number is the row's ``turns``. The pre-stream refusal and the in-process
    registry already keep a second producer out, so this is a fence, not a race."""
    db = SessionLocal()
    try:
        row = session_row(db, live.workspace_id, live.agent_id, live.session_id)
        if row is None:  # pragma: no cover — track_session always leaves the row
            raise RuntimeError("chat session row missing after meta")
        row.active_turn = row.turns or 1
        row.active_turn_started_at = datetime.now(UTC)
        row.active_turn_token = live.token
        db.commit()
        return row.active_turn
    finally:
        db.close()


def _release(live: ChatLiveTurn) -> None:
    db = SessionLocal()
    try:
        db.execute(
            update(ChatSession)
            .where(ChatSession.workspace_id == live.workspace_id,
                   ChatSession.agent_id == live.agent_id,
                   ChatSession.session_id == live.session_id,
                   ChatSession.active_turn == live.turn,
                   ChatSession.active_turn_token == live.token)
            .values(active_turn=None, active_turn_started_at=None, active_turn_token=None)
        )
        db.commit()
    finally:
        db.close()


def require_turn_capacity(db: Session, workspace_id: str, agent_id: str, session_id: str,
                          ) -> None:
    """409 ``chat.turn_in_progress`` before the stream opens (a refusal inside the stream
    would be a 500): a turn live in this process, or a still-valid claim on the session
    row (another process, or an orphan the next startup sweep will finalize)."""
    live = live_run(agent_id, session_id)
    if live is not None:
        raise _in_progress(live.turn)
    row = session_row(db, workspace_id, agent_id, session_id)
    if (row is not None and row.active_turn is not None
            and not _claim_is_stale(row, datetime.now(UTC))):
        raise _in_progress(row.active_turn)


def require_session_open(db: Session, workspace_id: str, agent_id: str, session_id: str,
                         ) -> None:
    """409 ``chat.session_ended`` for a turn posted under an ENDED session id (N16-obs79).

    END SESSION stamps the ledger row ``ended_at`` and terminates the runtime session;
    the console's next prompt opens a NEW session (AC-E04-08). A plain POST carrying
    the ended id used to re-open it silently (a fresh runtime session under the old id,
    ``ended_at`` cleared) — the marker is now kept and the caller is told to start a new
    session. Checked before the stream opens, so the refusal is a plain 409, not a stream
    error. The same gate runs inside the shared invoke chain for every entrance
    (``chat_ledger.require_session_open``, N16-obs88)."""
    row = session_row(db, workspace_id, agent_id, session_id)
    if row is not None and row.ended_at is not None:
        raise session_ended(session_id, row.ended_at)


# ---------------------------------------------------------------------------
# reattach
# ---------------------------------------------------------------------------


def _logged_author(db: Session, agent_id: str, session_id: str, turn: int) -> str | None:
    """The ``author`` the driver stamped on the turn's logged ``meta`` (``None`` for a log
    written before the stamp existed, or no log at all)."""
    data = db.execute(
        select(ChatTurnEvent.data)
        .where(ChatTurnEvent.agent_id == agent_id, ChatTurnEvent.session_id == session_id,
               ChatTurnEvent.turn == turn, ChatTurnEvent.event == "meta")
        .limit(1)
    ).scalar()
    author = data.get("author") if isinstance(data, dict) else None
    return author if isinstance(author, str) and author else None


def turn_author(db: Session, row: ChatSession, turn: int) -> str | None:
    """The member who SENT a turn — the one a reattach belongs to. The base ``POST`` lets
    any workspace member continue a session while ``chat_sessions.actor_id`` keeps the
    first sender (the Memory owner), so ownership is per turn: the live run's username
    while the turn runs here; the ``author`` of its logged ``meta`` otherwise; the
    session's actor only when neither is known (a log from before the stamp)."""
    live = live_run(row.agent_id, row.session_id)
    if live is not None and live.turn == turn:
        return live.username
    return _logged_author(db, row.agent_id, row.session_id, turn) or row.actor_id or None


def live_turn_view(db: Session, row: ChatSession) -> dict[str, Any] | None:
    """``{turn, seq, started_at, state, author}`` of the turn a client should reattach
    to: the one live in this process, else a still-valid claim; ``None`` otherwise.
    ``author`` is the turn's sender (``turn_author``), which the page compares with the
    signed-in member before reattaching."""
    live = live_run(row.agent_id, row.session_id)
    if live is not None and live.turn is not None:
        return {"turn": live.turn, "seq": live.seq, "started_at": live.started_at.isoformat(),
                "state": live.state, "author": live.username}
    if row.active_turn is None or _claim_is_stale(row, datetime.now(UTC)):
        return None
    last = db.execute(
        select(ChatTurnEvent.seq)
        .where(ChatTurnEvent.agent_id == row.agent_id,
               ChatTurnEvent.session_id == row.session_id,
               ChatTurnEvent.turn == row.active_turn)
        .order_by(ChatTurnEvent.seq.desc()).limit(1)
    ).scalar()
    started = row.active_turn_started_at
    return {"turn": row.active_turn, "seq": last or 0,
            "started_at": started.isoformat() if started else None,
            "state": "running",
            "author": turn_author(db, row, row.active_turn)}


def require_turn(db: Session, row: ChatSession, turn: int, human_actor: str) -> None:
    """404 for a turn number the session never reached; 409 ``chat.turn_not_yours
    {author}`` when another member sent the turn (``turn_author``: the sender, not the
    session's first owner — a member who continued a shared session reattaches to their
    own turn; a viewer who sent neither is refused)."""
    if turn < 1 or turn > (row.turns or 0):
        raise NotFoundError("chat.turn_not_found", f"turn {turn} does not exist")
    author = turn_author(db, row, turn)
    if author and author != human_actor:
        raise AppError("chat.turn_not_yours", f"turn {turn} was sent by another member",
                       {"author": author}, status_code=409)


def stream_turn(workspace_id: str, agent_id: str, session_id: str, turn: int, after: int = 0,
                ) -> Iterator[dict[str, Any]]:
    """Reattach to a turn: the persisted events with ``seq > after``, then the live tail
    (this process's buffer, or the ledger polled while the claim is held elsewhere),
    then the terminal event. A turn with no log and no claim ended before this feature
    or lost its log: the history has it, so a bare ``done`` lets the client settle."""

    def claim_held(db: Session) -> bool:
        row = session_row(db, workspace_id, agent_id, session_id)
        return (row is not None and row.active_turn == turn
                and not _claim_is_stale(row, datetime.now(UTC)))

    yield from live_turns.tail_turn(
        live=lambda: live_run(agent_id, session_id, turn),
        read_events=lambda db, last: [_event_out(r)
                                      for r in turn_events(db, agent_id, session_id, turn, last)],
        claim_held=claim_held,
        ended=lambda db: {"event": "done", "data": {"turn": turn}},
        after=after,
    )


# ---------------------------------------------------------------------------
# startup sweep
# ---------------------------------------------------------------------------


def clear_stale_turn_claims() -> int:
    """Startup: a claim can only be held by a live turn of THIS process, so every claim
    found at boot belongs to a process that died mid-turn (single-process deployment,
    like the assistant's sweep). The turn is persisted as *interrupted* from its event
    log (the open bubble + an ``error`` row) and its log gets a terminal ``error`` so a
    reattaching client sees it end; it is never resumed. Then the claim is cleared."""
    db = SessionLocal()
    try:
        rows = db.query(ChatSession).filter(ChatSession.active_turn.isnot(None)).all()
        for row in rows:
            try:
                _finalize_dead_turn(db, row)
            except Exception:  # pragma: no cover — the sweep must still clear the claim
                db.rollback()
                logger.exception("chat: could not finalize the dead turn of %s", row.session_id)
        count = db.execute(
            update(ChatSession)
            .where(ChatSession.active_turn.isnot(None))
            .values(active_turn=None, active_turn_started_at=None, active_turn_token=None)
        ).rowcount
        db.commit()
        return count
    finally:
        db.close()


def _finalize_dead_turn(db: Session, row: ChatSession) -> None:
    turn = row.active_turn
    if turn is None:
        return
    events = turn_events(db, row.agent_id, row.session_id, turn)
    if not events or any(e.event in TERMINAL_EVENTS for e in events):
        return
    # replay the log through the same reducer the driver ran (rows for every logged event
    # already exist), then write only what is still missing: the open bubble + the marker
    recorder = TranscriptRecorder(row.workspace_id or "", row.agent_id, prompt="",
                                  actor_id=row.actor_id, attachments=None,
                                  runtime_version=None, persist=False)
    for e in events:
        recorder.feed({"event": e.event, "data": e.data or {}})
    recorder.persist = True
    recorder.interrupted()
    db.rollback()
    db.add(ChatTurnEvent(
        workspace_id=row.workspace_id, agent_id=row.agent_id, session_id=row.session_id,
        turn=turn, seq=events[-1].seq + 1, event="error", data={"message": "interrupted"},
    ))
    db.commit()
    logger.warning("chat: turn %s of session %s was left by a dead process; persisted interrupted",
                   turn, row.session_id)
