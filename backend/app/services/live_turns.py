"""Detached turns — the generic half of N-ARCH-obs15, shared by the architect assistant
(``app/assistant/service.py``, v1) and the Chat playground (``services/chat_turns.py``, v2).

A *turn* is a server-side unit of work owned by the process, not by the SSE connection
that started it: a driver thread iterates the producer to its end, every event gets a
monotonic ``seq`` (per turn, from 1), is published to an in-memory buffer every
subscriber tails, and is appended to a per-domain event-log table. Closing a subscriber
(client gone, ``GeneratorExit``) detaches that subscriber only. This module holds what
does not depend on the domain:

* :class:`LiveTurn` — the buffer + condition variable, ``publish`` / ``subscribe``;
* :class:`LiveTurnRegistry` — one live turn per key, with the pre-``meta`` wait that keeps
  a reattach to turn N from tailing a just-started N+1;
* :class:`EventLogBatcher` — the flush policy of the event log (batched while only deltas
  arrive, immediate for anything else and for the terminal event);
* :func:`tail_turn` — the ledger-side reattach path (replay, poll while the claim is held,
  then the terminal event);
* :class:`TurnStreamResponse` / :func:`sse_body` — a streaming response that owns its
  subscription and never the turn.

The domain modules keep the claim (durable fence), the producer, the transcript rows and
the startup sweep. Contract: ``docs/cn/interfaces/assistant-turns.md``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

from fastapi.responses import StreamingResponse

from app.core.db import SQLITE_MAX_INT, SessionLocal
from app.core.errors import AppError
from app.services.chat import sse_encode

logger = logging.getLogger(__name__)

# One keep-alive comment per idle second — the proxy and the browser both see traffic
# and a detached subscriber's ``close`` lands within one beat.
HEARTBEAT_S = 1.0
# The event log is made durable at most once per this interval while only deltas arrive
# (a 10-minute turn streams thousands of deltas); any other event flushes at once.
EVENT_FLUSH_S = 1.0
TERMINAL_EVENTS = ("done", "error")


class LiveTurn:
    """One running turn: the in-memory event buffer every subscriber tails and the driver
    thread that fills it. ``seq`` is the number of events published so far;
    ``events[i]`` has ``seq == i + 1``. ``turn`` is known once ``meta`` was published.

    Domain subclasses add their own identity fields and may override
    :meth:`is_finishing` (the assistant's run handle carries that flag)."""

    def __init__(self, key: str, workspace_id: str | None, username: str) -> None:
        self.key = key
        self.workspace_id = workspace_id
        self.username = username
        self.turn: int | None = None
        self.started_at = datetime.now(UTC)
        self.events: list[dict[str, Any]] = []
        self.done = False
        # the upstream stream ended; the reply is being validated / persisted
        self.finishing = False
        self.cond = threading.Condition()
        self.thread: threading.Thread | None = None

    @property
    def seq(self) -> int:
        with self.cond:
            return len(self.events)

    def is_finishing(self) -> bool:
        return self.finishing

    @property
    def state(self) -> str:
        return "finishing" if self.is_finishing() and not self.done else "running"

    def publish(self, event: dict[str, Any]) -> dict[str, Any]:
        with self.cond:
            stamped = {"event": event["event"], "data": event["data"], "seq": len(self.events) + 1}
            self.events.append(stamped)
            self.cond.notify_all()
        return stamped

    def finish(self) -> None:
        with self.cond:
            self.done = True
            self.cond.notify_all()

    def subscribe(self, after: int = 0) -> Iterator[dict[str, Any]]:
        """Every event with ``seq > after`` (buffered ones first, then live), a
        ``heartbeat`` whenever nothing arrived for ``HEARTBEAT_S``; ends after the turn's
        terminal event. Closing the generator detaches this subscriber only."""
        index = max(0, after)
        while True:
            with self.cond:
                event = self.events[index] if index < len(self.events) else None
                if event is not None and event["event"] in TERMINAL_EVENTS and not self.done:
                    # the terminal event is handed out only once the turn is finalized
                    # (claim released, upstream closed): a reader that saw it may rely on
                    # the ledger — never on a finalizer still running
                    self.cond.wait(HEARTBEAT_S)
                    event = None
                elif event is not None:
                    index += 1
                elif self.done:
                    return
                else:
                    self.cond.wait(HEARTBEAT_S)
            if event is None:
                # keep-alive; also the moment a detached subscriber's ``close`` lands
                yield {"event": "heartbeat", "data": {}}
                continue
            yield event
            if event["event"] in TERMINAL_EVENTS:
                return


class LiveTurnRegistry:
    """The turns running in THIS process, one per key (conversation id, chat session…)."""

    def __init__(self) -> None:
        self._runs: dict[str, LiveTurn] = {}
        self._lock = threading.Lock()

    def get(self, key: str, turn: int | None = None) -> LiveTurn | None:
        """The turn running here for ``key`` (and, when given, for exactly that turn
        number), or ``None``.

        With ``turn`` given, a live run whose number is not known yet (its ``meta`` has not
        been published — the driver is between start and the claim) is **not** assumed to
        be that turn: a reattach to turn N racing a just-started N+1 must never tail N+1's
        buffer. The caller waits for ``meta`` up to ``HEARTBEAT_S``; a run still
        unidentified after that answers ``None`` (the ledger path re-checks every poll)."""
        with self._lock:
            live = self._runs.get(key)
        if live is None or live.done:
            return None
        if turn is None:
            return live
        if live.turn is None:
            deadline = time.monotonic() + HEARTBEAT_S
            with live.cond:
                while live.turn is None and not live.done:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    live.cond.wait(remaining)
        if live.done or live.turn != turn:
            return None
        return live

    def start(
        self, live: LiveTurn, target: Callable[..., None], args: tuple[Any, ...], *,
        name: str, conflict: Callable[[LiveTurn], AppError],
    ) -> LiveTurn:
        """Register ``live`` under its key and start its driver thread. A second start for
        a key whose turn is still live here is refused with ``conflict(current)``."""
        with self._lock:
            current = self._runs.get(live.key)
            if current is not None and not current.done:
                raise conflict(current)
            self._runs[live.key] = live
        live.thread = threading.Thread(target=target, args=args, daemon=True, name=name)
        live.thread.start()
        return live

    def rekey(self, live: LiveTurn, key: str) -> None:
        """The producer reported a different key at ``meta`` (a scripted stream in tests):
        follow it so reattach lookups find the turn."""
        with self._lock:
            if self._runs.get(live.key) is live:
                del self._runs[live.key]
            live.key = key
            self._runs[key] = live

    def drop(self, live: LiveTurn) -> None:
        with self._lock:
            if self._runs.get(live.key) is live:
                del self._runs[live.key]


class EventLogBatcher:
    """The event log's flush policy: ``add`` queues a published event; ``flush`` writes
    the queue once the turn number is known (``ready``); deltas are batched up to
    ``EVENT_FLUSH_S``, anything else flushes at once; ``idle`` flushes on a heartbeat."""

    def __init__(self, write: Callable[[list[dict[str, Any]]], None],
                 ready: Callable[[], bool]) -> None:
        self._write = write
        self._ready = ready
        self.pending: list[dict[str, Any]] = []
        self.last_flush = time.monotonic()

    def flush(self) -> None:
        if self.pending and self._ready():
            self._write(self.pending)
        self.pending.clear()
        self.last_flush = time.monotonic()

    def add(self, stamped: dict[str, Any]) -> None:
        self.pending.append(stamped)
        if stamped["event"] != "delta" or time.monotonic() - self.last_flush >= EVENT_FLUSH_S:
            self.flush()

    def idle(self) -> None:
        if self.pending:  # idle upstream: a cheap moment to make the log durable
            self.flush()


def _tail_live(run: LiveTurn, after: int) -> Iterator[dict[str, Any]]:
    """Tail ``run`` from ``after``; the generator's return value says whether a terminal
    event was handed out (``subscribe`` ends silently when ``after`` is at or past the
    turn's last seq and the turn is done)."""
    terminal = False
    for event in run.subscribe(after):
        terminal = terminal or event["event"] in TERMINAL_EVENTS
        yield event
    return terminal


def tail_turn(
    *,
    live: Callable[[], LiveTurn | None],
    read_events: Callable[[Any, int], list[dict[str, Any]]],
    claim_held: Callable[[Any], bool],
    ended: Callable[[Any], dict[str, Any]],
    after: int = 0,
) -> Iterator[dict[str, Any]]:
    """Reattach to a turn: the persisted events with ``seq > after``, then the live tail
    (this process's buffer, or the ledger polled while the claim is held elsewhere),
    then the terminal event. A turn with no log and no claim ends at once with
    ``ended(db)`` (``done`` / ``error: interrupted``, the domain decides). Opens its own
    session; ``read_events(db, after)`` returns ``{event, data, seq}`` dicts."""
    run = live()
    if run is not None:
        if (yield from _tail_live(run, after)):
            return
        # the buffer held nothing past ``after`` (a reattach from at / beyond the terminal
        # seq of a turn that finished meanwhile): the ledger path below still ends the
        # stream on a terminal event (N16-obs45)
    db = SessionLocal()
    last = after
    try:
        while True:
            db.rollback()
            db.expire_all()
            for event in read_events(db, last):
                last = event["seq"]
                yield event
                if event["event"] in TERMINAL_EVENTS:
                    return
            run = live()
            if run is not None:  # started here meanwhile: switch to the in-memory tail
                if (yield from _tail_live(run, last)):
                    return
                continue  # ended without handing out a terminal: re-read the ledger
            if not claim_held(db):
                if read_events(db, last):
                    continue  # the terminal event landed between the two reads
                yield ended(db)
                return
            time.sleep(HEARTBEAT_S)
            yield {"event": "heartbeat", "data": {}}
    finally:
        db.close()


class TurnStreamResponse(StreamingResponse):
    """A streaming response that owns its SUBSCRIPTION to a turn — never the turn
    (N-ARCH-obs15). Whatever way the response ends — completion, ASGI 2.0 disconnect
    (task-group cancel) or ASGI 2.4 send error — the body generator is closed
    deterministically (bounded retries while it is still executing in the worker
    thread), so the subscriber leaves the turn's condition variable at once; the turn
    itself keeps running on its driver thread to its terminal row."""

    def __init__(self, body: Iterator[str], *args, **kwargs) -> None:
        super().__init__(body, *args, **kwargs)
        self._body = body

    def _detach(self, wait_s: float = 5.0) -> None:
        close = getattr(self._body, "close", None)
        if not callable(close):
            return
        deadline = time.monotonic() + wait_s
        while True:
            try:
                close()
                return
            except ValueError:  # "generator already executing" in the worker thread
                if time.monotonic() > deadline:
                    logger.warning("live turns: subscriber still executing after %.1fs", wait_s)
                    return
                time.sleep(0.02)
            except Exception:  # pragma: no cover — cleanup must not raise
                logger.exception("live turns: closing the turn subscriber failed")
                return

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            import anyio

            with anyio.CancelScope(shield=True):  # cleanup survives a cancelled scope
                await anyio.to_thread.run_sync(self._detach)


def sse_body(events: Iterator[dict[str, Any]]) -> Iterator[str]:
    """Frame a subscriber's events; a refusal raised after the response started (a claim
    race) becomes the stream's ``error`` event, as before."""
    try:
        for event in events:
            yield sse_encode(event)
    except AppError as exc:
        yield sse_encode({"event": "error", "data": {"code": exc.code, "message": exc.message}})


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def parse_from(raw: str | None, code: str) -> int:
    """``?from=<seq>`` of a reattach: an integer in ``0..SQLITE_MAX_INT``, else ``422
    <code>``. N16-obs86: a Python int above SQLite's range used to reach the ``seq > after``
    query and raise ``OverflowError`` (a 500) — the r12-h class, bounded here like every
    other integer parameter (``core/db.SQLITE_MAX_INT``). A negative value is 422 too."""
    try:
        value = int(raw if raw not in (None, "") else "0")
    except ValueError as exc:
        raise AppError(code, "`from` must be an integer", status_code=422) from exc
    if value < 0 or value > SQLITE_MAX_INT:
        raise AppError(code, f"`from` must be an integer between 0 and {SQLITE_MAX_INT}",
                       {"from": str(value), "min": 0, "max": SQLITE_MAX_INT}, status_code=422)
    return value
