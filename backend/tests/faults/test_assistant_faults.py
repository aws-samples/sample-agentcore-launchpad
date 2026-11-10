# ruff: noqa: F811  — pytest fixtures are imported by name from tests.test_assistant
"""The architect turn under injected upstream faults (quality pack Q4, N12-obs58).

Pins the contract of ``assistant/service.run_turn`` against the exact exception shapes
the upstreams produce (``tests/faults/conftest.py``): one transient is replayed once in a
fresh Harness session after ``assistant_replay_backoff_seconds``, with the ``retry``
event and the ``turn_replay`` row recording the reason and the wait; a second one ends
the turn on a structured ``error`` whose ``detail.error_type`` is the Harness-side class
(N-ARCH-obs18), the claim released and the conversation usable again. The one surface
the turn does NOT replay today — a botocore ``ReadTimeoutError`` on the
``InvokeAgentRuntime`` call itself — is pinned as ``xfail(strict)`` on a behaviour probe,
so it flips to a real pass the moment the classifier (r12-f) treats it as transient.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ReadTimeoutError
from sqlalchemy import select

from app.assistant import service
from app.core.db import SessionLocal
from app.models.assistant import AssistantConversation, AssistantMessage, AssistantTurnEvent
from tests.faults.conftest import ENDPOINT, assistant_replays
from tests.test_assistant import _latest, _open, _turn

REPLY = "The full answer after the hiccup."
ATTEMPTS = 1 + service.TURN_TRANSIENT_RETRIES  # one replay (N-ARCH-obs18)
BACKOFF_S = 0.3

# (fixture, surface): every rendering the turn classifies as a replayable transient
RECOVERING = [
    ("bedrock_5xx", "frame"), ("bedrock_5xx", "client"),
    ("throttle", "frame"), ("throttle", "client"),
    ("read_timeout", "frame"),
]


@pytest.fixture
def fault(request, bedrock_5xx, throttle, read_timeout):
    name, surface = request.param
    factory = {"bedrock_5xx": bedrock_5xx, "throttle": throttle, "read_timeout": read_timeout}
    return factory[name](1, surface=surface)[0]


def _ids(cases: list[tuple[str, str]]) -> list[str]:
    return [f"{name}-{surface}" for name, surface in cases]


def _rows(cid: str, **where) -> list[AssistantMessage]:
    db = SessionLocal()
    try:
        return db.scalars(select(AssistantMessage).where(
            AssistantMessage.conversation_id == cid,
            *(getattr(AssistantMessage, k) == v for k, v in where.items()),
        )).all()
    finally:
        db.close()


def _claim(cid: str) -> int | None:
    db = SessionLocal()
    try:
        return db.get(AssistantConversation, cid).active_turn
    finally:
        db.close()


@pytest.mark.parametrize("fault", RECOVERING, indirect=True, ids=_ids(RECOVERING))
def test_turn_recovers_after_one_transient(client, ready, harness, backoff, fault):
    """One fault, then the reply: a ``retry`` event names the fault and the wait, the
    fresh attempt runs in a NEW runtime session after the back-off, the half reply the
    failed attempt streamed is discarded, and the ``turn_replay`` row keeps the record."""
    backoff(BACKOFF_S)
    harness.reply(REPLY)
    harness.inject(fault)
    cid = _open(client)
    events = _turn(client, cid, "q")

    kinds = [k for k, _ in events]
    assert kinds.count("retry") == 1 and kinds[-1] == "done", kinds
    assert len(harness.stamps) == ATTEMPTS and harness.served == [fault]
    retry = dict(events)["retry"]
    assert retry["attempt"] == 1 and retry["backoff_s"] == BACKOFF_S
    assert fault.marker.lower() in retry["reason"].lower(), retry
    assert harness.stamps[1] - harness.stamps[0] >= BACKOFF_S  # the back-off was honoured
    assert len({c["runtimeSessionId"] for c in harness.calls}) == ATTEMPTS  # fresh session
    # the member sees one clean exchange; the discarded half reply is nowhere
    turn = [m for m in _latest(client, cid)["messages"] if m["turn"] == 1]
    assert [m["role"] for m in turn] == ["user", "assistant"]
    assert turn[-1]["text"] == REPLY and "half an answer" not in turn[-1]["text"]
    deltas = "".join(d.get("text", "") for k, d in events if k == "delta")
    # what streamed before and after the retry (a raised call streams nothing first)
    partial = "half an answer " if fault.surface == "frame" else ""
    assert deltas == partial + REPLY
    (replay_row,) = _rows(cid, name="turn_replay")
    assert fault.marker.lower() in replay_row.text.lower()
    assert replay_row.text.endswith(f"(waited {BACKOFF_S:g}s)")
    assert _claim(cid) is None  # the claim is released with the reply


@pytest.mark.parametrize("fault", RECOVERING, indirect=True, ids=_ids(RECOVERING))
def test_turn_fails_cleanly_after_two_transients(client, ready, harness, fault):
    """The replay hits the same fault: exactly ``ATTEMPTS`` calls, one ``retry``, then a
    terminal ``error`` that names the cause (``detail.error_type`` for an SDK frame), one
    ``error`` row, one logged terminal event, the claim released — and the next turn works."""
    harness.reply(REPLY)
    harness.inject(fault, fault)
    cid = _open(client)
    events = _turn(client, cid, "q")

    kinds = [k for k, _ in events]
    assert kinds.count("retry") == 1 and kinds[-1] == "error", kinds
    assert len(harness.stamps) == ATTEMPTS and harness.faults == []
    error = events[-1][1]
    assert "code" not in error  # not an AppError: the upstream's own words reach the member
    assert fault.marker.lower() in error["message"].lower(), error
    if fault.surface == "frame":
        assert error["detail"]["error_type"] == fault.error_type  # N-ARCH-obs18
        assert error["detail"]["error"] == fault.error
    else:
        assert "detail" not in error  # a botocore exception carries no SDK frame
    rows = _rows(cid, role="error")
    assert len(rows) == 1 and fault.marker.lower() in rows[0].text.lower()
    # a stream that errors keeps what the LAST attempt streamed beside the error row
    # (``_persist_partial``); a raised call streamed nothing
    partial = [r.text for r in _rows(cid, role="assistant")]
    assert partial == (["half an answer "] if fault.surface == "frame" else [])
    db = SessionLocal()
    try:
        logged = db.scalars(select(AssistantTurnEvent).where(
            AssistantTurnEvent.conversation_id == cid, AssistantTurnEvent.event == "error",
        )).all()
        assert len(logged) == 1 and fault.marker.lower() in logged[0].data["message"].lower()
    finally:
        db.close()
    assert _claim(cid) is None
    # the conversation is not wedged: a healthy next turn goes through
    events = _turn(client, cid, "again")
    assert [k for k, _ in events][-1] == "done" and len(harness.stamps) == ATTEMPTS + 1


TRANSPORT_TIMEOUT_REPLAYED = assistant_replays(ReadTimeoutError(endpoint_url=ENDPOINT))


@pytest.mark.xfail(
    condition=not TRANSPORT_TIMEOUT_REPLAYED, strict=True,
    reason=("the architect turn does not classify a botocore ReadTimeoutError on the "
            "InvokeAgentRuntime call as transient (service._replayable_turn_error: only "
            "RuntimeStreamError, harness.incomplete_response and a transient ClientError "
            "replay) — the eval worker does (runtime_invoke.is_transient_invoke_error); "
            "expected to flip with the shared classifier of r12-f"),
)
def test_a_transport_read_timeout_on_the_invoke_call_is_replayed(
    client, ready, harness, read_timeout
):
    """A socket read timeout between the backend and the Runtime is the same hiccup as a
    5xx — the eval worker already replays it. Pinned as the spec; xfail while the turn
    ends on ``error`` instead."""
    harness.reply(REPLY)
    harness.inject(read_timeout(1, surface="transport"))
    cid = _open(client)
    events = _turn(client, cid, "q")
    kinds = [k for k, _ in events]
    assert "retry" in kinds and kinds[-1] == "done", kinds
    assert len(harness.stamps) == ATTEMPTS


@pytest.mark.parametrize(
    "error_type", ["ValidationException", "ContextWindowOverflowException",
                   "ExpiredTokenException"],
)
def test_any_sdk_error_frame_is_replayed_once_without_a_wait_and_names_its_class(
    client, ready, harness, backoff, sdk_error_frame, error_type
):
    """Every terminal SDK frame is a ``RuntimeStreamError`` and so replayed once — but
    only the upstream-transient markers earn the back-off: a validation / context-window
    / credential failure replays at once, and when it repeats the member and the turn
    event log get the exact Harness-side class."""
    backoff(BACKOFF_S)
    harness.inject(sdk_error_frame(error_type), sdk_error_frame(error_type))
    cid = _open(client)
    events = _turn(client, cid, "q")

    kinds = [k for k, _ in events]
    assert kinds.count("retry") == 1 and kinds[-1] == "error", kinds
    # no upstream-transient marker: no wait — the event's ``backoff_s`` and the replay row's
    # missing "waited" note are the evidence (an elapsed-time upper bound is not: a loaded
    # gate can schedule the second call hundreds of ms later with no back-off at all)
    assert dict(events)["retry"]["backoff_s"] == 0
    error = events[-1][1]
    assert error["detail"]["error_type"] == error_type
    assert "runtime returned error: " + error_type in error["message"]
    (row,) = _rows(cid, name="turn_replay")
    assert error_type in row.text and "waited" not in row.text
