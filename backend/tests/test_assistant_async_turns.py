# ruff: noqa: F811  — pytest fixtures are imported by name from test_assistant
"""N-ARCH-obs15: an architect turn is a server-side unit of work, independent of any
connection. Pins: a subscriber that leaves mid-stream changes nothing about the turn's
result (transcript + proposal identical to a watched turn); the per-turn event log
(``assistant_turn_events``) is ``seq``-ordered and complete; ``GET …/turns/{turn}/stream
?from=`` replays exactly the missed events and tails the live producer; the detail's
``live_turn`` tells a page load what to reattach to; a second turn while one runs is
refused; a producer failure while nobody watches still persists the error row; the
startup sweep finalizes a dead process's turn from its event log — never resumes it.
Hermetic: temp ledger, sockets refused, AWS client factory refused."""

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.assistant import service
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.assistant import AssistantConversation, AssistantMessage, AssistantTurnEvent
from app.models.ledger import Workspace
from app.routers.auth import ROLE_MEMBER, Identity
from app.services.workspace import workspace_context

from .test_assistant import (  # noqa: F401 — shared hermetic fixtures + helpers
    ADMIN_CREDS,
    BASE,
    MEMBER_CREDS,
    VALID_PROPOSAL,
    _block,
    _latest,
    _open,
    _sse,
    _turn,
    gated,
    harness,
    no_aws_clients,
    no_network,
    ready,
    sse_lines,
)
from .test_assistant_residuals import _wait_turn_finished

IDENTITY = Identity(username="river", role="admin")


def _sse_with_ids(text: str) -> list[tuple[str, dict, int | None]]:
    """``(event, data, id)`` per frame — ``id:`` carries the turn's ``seq``."""
    events = []
    # SSE: normalise the WHOLE text (CRLF / bare CR → LF) first, then split frames on LF only —
    # splitlines() cut a data: JSON string at U+2028-class characters (N17-obs52, rev-r17)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for frame in text.split("\n\n"):
        kind, data, seq = None, None, None
        for line in frame.split("\n"):
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
            elif line.startswith("id:"):
                seq = int(line[3:].strip())
        if kind:
            events.append((kind, data, seq))
    return events


def _start(cid: str, prompt: str = "go") -> service.LiveTurn:
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        return service.start_turn(cid, DEFAULT_WORKSPACE_ID, row, workspace_context(row),
                                  IDENTITY, prompt)
    finally:
        db.close()


def _take(sub, n: int) -> list[dict]:
    """The next ``n`` events of a subscription, keep-alives skipped."""
    out: list[dict] = []
    while len(out) < n:
        event = next(sub)
        if event["event"] != "heartbeat":
            out.append(event)
    return out


def _deltas(text: str, size: int = 40) -> list[dict]:
    return [{"contentBlockDelta": {"delta": {"text": text[i:i + size]}}}
            for i in range(0, len(text), size)]


class _GatedBody:
    """Streams ``head`` events, waits for ``gate``, then streams ``tail`` events."""

    def __init__(self, head, tail):
        self.head, self.tail = head, tail
        self.gate = threading.Event()
        self.closed = False

    def iter_lines(self, chunk_size=None):
        yield from sse_lines(self.head)
        self.gate.wait(timeout=30)
        yield from sse_lines(self.tail)

    def close(self):
        self.closed = True
        self.gate.set()


# ---------------------------------------------------------------------------
# the connection is not the turn
# ---------------------------------------------------------------------------


def test_subscriber_leaving_mid_stream_changes_nothing_about_the_turns_result(
    client, ready, harness
):
    """Two conversations, the same scripted reply: one watched to the end, one whose only
    subscriber closes after two events. Transcript rows, proposal content hash and the
    event log are identical; neither carries an interrupted marker."""
    reply = "Here is the design. " * 10 + _block(VALID_PROPOSAL) + "\n\nNext: review."
    harness.reply(reply)
    watched = _open(client)
    watched_events = _turn(client, watched, "go")
    assert watched_events[-1][0] == "done"

    detached = _open(client)
    live = _start(detached)
    sub = live.subscribe(0)
    seen = _take(sub, 2)
    assert [e["event"] for e in seen] == ["meta", "delta"]
    sub.close()  # the member's connection is gone
    _wait_turn_finished(detached)

    db = SessionLocal()
    try:
        a = [(m.role, m.name, m.text) for m in service._messages(db, watched)]
        b = [(m.role, m.name, m.text) for m in service._messages(db, detached)]
        assert [r[0] for r in b] == ["user", "assistant"]
        assert a == b
        pa, pb = service.latest_proposal(db, watched), service.latest_proposal(db, detached)
        assert pa is not None and pb is not None
        assert (pa.content_hash, pa.status, pa.revision) == (
            pb.content_hash, pb.status, pb.revision)
        assert db.get(AssistantConversation, detached).active_turn is None
        la = [(e.event, e.seq) for e in service.turn_events(db, watched, 1)]
        lb = [(e.event, e.seq) for e in service.turn_events(db, detached, 1)]
        assert la == lb and la[-1][0] == "done"
    finally:
        db.close()


def test_event_log_seq_is_monotonic_and_matches_the_stream(client, ready, harness):
    harness.reply("alpha beta gamma " * 6 + _block(VALID_PROPOSAL), tools=("lookup",))
    cid = _open(client)
    res = client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "go"})
    assert res.status_code == 200
    framed = [(k, s) for k, _d, s in _sse_with_ids(res.text) if k != "heartbeat"]
    assert [s for _k, s in framed] == list(range(1, len(framed) + 1))
    assert framed[0][0] == "meta" and framed[-1][0] == "done"
    assert "tool" in [k for k, _s in framed]
    db = SessionLocal()
    try:
        rows = service.turn_events(db, cid, 1)
        assert [(r.event, r.seq) for r in rows] == framed
        assert all(r.workspace_id == DEFAULT_WORKSPACE_ID for r in rows)
        assert not any(r.event == "heartbeat" for r in rows)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# reattach
# ---------------------------------------------------------------------------


def test_reattach_from_seq_replays_the_missed_events_then_tails_the_producer(
    client, ready, harness
):
    head = _deltas("first part of the answer. " * 4)
    tail = _deltas("second part after the member came back. " * 3)
    body = _GatedBody(head, tail)
    harness.invoke_agent_runtime = lambda **kw: {"response": body,
                                                 "contentType": "text/event-stream"}
    cid = _open(client)
    live = _start(cid)
    sub = live.subscribe(0)
    seen = _take(sub, 1)  # meta only; the member's connection drops here
    sub.close()
    deadline = time.monotonic() + 5
    while live.seq < 1 + len(head) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert live.seq == 1 + len(head)  # the producer kept streaming unwatched
    detail = _latest(client, cid)
    assert detail["live_turn"] == {
        "turn": 1, "seq": 1 + len(head), "started_at": detail["live_turn"]["started_at"],
        "state": "running", "author": "river",
    } and detail["turn_in_progress"] == 1

    # the route wraps exactly this generator (the test client cannot stream a body)
    tail_sub = service.stream_turn(cid, 1, after=seen[-1]["seq"])
    replayed = _take(tail_sub, len(head))
    # exactly the missed deltas, in order, before the gate opened
    assert [e["seq"] for e in replayed] == list(range(2, 2 + len(head)))
    assert "".join(e["data"]["text"] for e in replayed) == "".join(
        e["contentBlockDelta"]["delta"]["text"] for e in head)
    assert service.live_run(cid) is not None  # still running: what follows is a tail
    body.gate.set()
    rest = [e for e in tail_sub if e["event"] != "heartbeat"]
    kinds = [e["event"] for e in rest]
    assert kinds[-1] == "done" and kinds.count("delta") == len(tail)
    assert [e["seq"] for e in rest] == list(range(2 + len(head), 2 + len(head) + len(rest)))
    _wait_turn_finished(cid)
    assert _latest(client, cid)["live_turn"] is None


def test_reattach_polls_the_ledger_while_another_process_holds_the_claim(client, ready):
    """No live run in this process, a fresh claim in the ledger (another worker, or a
    turn the next startup sweep will finalize): the stream replays the log, then polls it
    while the claim is held, and ends on the terminal event that lands later."""
    cid = _open(client)
    _dead_turn(cid, "typed so far ")  # a fresh claim: started 120 s ago, TTL 1800 s
    detail = _latest(client, cid)
    assert detail["live_turn"]["turn"] == 1 and detail["live_turn"]["state"] == "running"
    assert detail["live_turn"]["seq"] == 2 and detail["live_turn"]["author"] == "river"
    got: list[dict] = []
    sub = service.stream_turn(cid, 1, after=0)
    got += _take(sub, 2)
    assert [e["event"] for e in got] == ["meta", "delta"]
    heartbeats = 0
    while heartbeats < 1:  # nothing new: the poller keeps the connection alive
        if next(sub)["event"] == "heartbeat":
            heartbeats += 1
    db = SessionLocal()
    try:  # the other process streams one more delta and finishes with an error
        ws = db.get(AssistantConversation, cid).workspace_id
        db.add(AssistantTurnEvent(workspace_id=ws, conversation_id=cid, turn=1, seq=3,
                                  event="delta", data={"text": "and more"}))
        db.add(AssistantTurnEvent(workspace_id=ws, conversation_id=cid, turn=1, seq=4,
                                  event="error", data={"message": "boom"}))
        db.commit()
    finally:
        db.close()
    rest = [e for e in sub if e["event"] != "heartbeat"]
    assert [(e["event"], e["seq"]) for e in rest] == [("delta", 3), ("error", 4)]


def test_reattach_after_completion_replays_everything_and_ends_with_done(
    client, ready, harness
):
    harness.reply("done deal " * 5 + _block(VALID_PROPOSAL))
    cid = _open(client)
    original = [(k, s) for k, _d, s in _sse_with_ids(
        client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "go"}).text)
        if k != "heartbeat"]
    res = client.get(f"{BASE}/conversations/{cid}/turns/1/stream")
    assert res.status_code == 200, res.text
    replay = [(k, s) for k, _d, s in _sse_with_ids(res.text) if k != "heartbeat"]
    assert replay == original and replay[-1][0] == "done"
    assert any(k == "proposal" for k, _s in replay)
    # from the penultimate seq: only the terminal event
    res = client.get(f"{BASE}/conversations/{cid}/turns/1/stream",
                     params={"from": original[-1][1] - 1})
    assert [(k, s) for k, _d, s in _sse_with_ids(res.text)] == [original[-1]]
    # at or past the end: a bare ``done`` so the client can settle
    res = client.get(f"{BASE}/conversations/{cid}/turns/1/stream",
                     params={"from": original[-1][1]})
    assert [k for k, _d, _s in _sse_with_ids(res.text)] == ["done"]
    assert client.get(f"{BASE}/conversations/{cid}/turns/1/stream",
                      params={"from": "x"}).status_code == 422


def test_reattach_refusals_unknown_turn_and_another_members_turn(gated, harness):
    admin, member, _other, _ids, _preset = gated
    harness.reply("a short reply")
    cid = _open(admin)
    shared = admin.put(f"{BASE}/conversations/{cid}/sharing", json={"shared": True})
    assert shared.status_code == 200
    events = _turn(admin, cid, "the admin's turn")
    assert events[-1][0] == "done"
    assert admin.get(f"{BASE}/conversations/{cid}/turns/1/stream").status_code == 200
    res = admin.get(f"{BASE}/conversations/{cid}/turns/7/stream")
    assert res.status_code == 404 and res.json()["code"] == "assistant.turn_not_found"
    # a collaborator reads the transcript but may not attach to the sender's stream
    assert _latest(member, cid)["messages"][0]["author"] == ADMIN_CREDS["username"]
    res = member.get(f"{BASE}/conversations/{cid}/turns/1/stream")
    assert res.status_code == 409 and res.json()["code"] == "assistant.turn_not_yours"
    assert res.json()["detail"]["author"] == ADMIN_CREDS["username"]
    # the member's own turn on the shared conversation is theirs to reattach to
    assert _turn(member, cid, "the member's turn")[-1][0] == "done"
    assert member.get(f"{BASE}/conversations/{cid}/turns/2/stream").status_code == 200
    assert admin.get(f"{BASE}/conversations/{cid}/turns/2/stream").status_code == 409
    assert _latest(member, cid)["messages"][-2]["author"] == MEMBER_CREDS["username"]


def test_second_post_while_a_detached_turn_runs_is_refused(client, ready, harness):
    body = _GatedBody(_deltas("working. "), _deltas(" finished."))
    harness.invoke_agent_runtime = lambda **kw: {"response": body,
                                                 "contentType": "text/event-stream"}
    cid = _open(client)
    live = _start(cid)
    deadline = time.monotonic() + 5
    while live.turn is None and time.monotonic() < deadline:
        time.sleep(0.02)
    res = client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "again"})
    assert res.status_code == 409 and res.json()["code"] == "assistant.turn_in_progress"
    assert len(harness.calls) == 1 if harness.calls else True
    body.gate.set()
    _wait_turn_finished(cid)
    detail = _latest(client, cid)
    assert detail["turns"] == 1 and detail["live_turn"] is None
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]


# ---------------------------------------------------------------------------
# only the producer's own failure, a lost claim or a dead process interrupt a turn
# ---------------------------------------------------------------------------


def test_producer_failure_while_nobody_watches_persists_the_error_row_and_event(
    client, ready, harness
):
    harness.reply("partial " * 5)
    harness.fail_after = 2  # every attempt (including the one replay) ends in a stream error
    cid = _open(client)
    _start(cid)
    _wait_turn_finished(cid)
    db = SessionLocal()
    try:
        msgs = service._messages(db, cid)
        shown = [m for m in msgs if m.role != service.SESSION_ROLE]
        assert [m.role for m in shown] == ["user", "assistant", "error"]
        assert shown[2].name is None and "interrupted" not in shown[2].text
        assert db.get(AssistantConversation, cid).active_turn is None
        events = service.turn_events(db, cid, 1)
        assert events[-1].event == "error" and "retry" in [e.event for e in events]
        assert service.latest_proposal(db, cid) is None
    finally:
        db.close()
    res = client.get(f"{BASE}/conversations/{cid}/turns/1/stream")
    kinds = [k for k, _d, _s in _sse_with_ids(res.text)]
    assert kinds[0] == "meta" and kinds[-1] == "error"


def _dead_turn(cid: str, streamed: str, *, with_terminal_row: bool = False) -> None:
    """A conversation a dead process left mid-turn: the claim, the user row, the event
    log of what had streamed — and no terminal transcript row (unless asked)."""
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        row.turns, row.active_turn, row.active_turn_token = 1, 1, "dead-process-token"
        row.active_turn_started_at = datetime.now(UTC) - timedelta(seconds=120)
        db.add(AssistantMessage(workspace_id=row.workspace_id, conversation_id=cid, turn=1,
                                role="user", text="go", author="river",
                                runtime_session_id="s" * 40))
        events = [{"event": "meta", "data": {"turn": 1}}]
        events += [{"event": "delta", "data": {"text": streamed[i:i + 40]}}
                   for i in range(0, len(streamed), 40)]
        for seq, e in enumerate(events, start=1):
            db.add(AssistantTurnEvent(workspace_id=row.workspace_id, conversation_id=cid,
                                      turn=1, seq=seq, event=e["event"], data=e["data"]))
        if with_terminal_row:
            db.add(AssistantMessage(workspace_id=row.workspace_id, conversation_id=cid, turn=1,
                                    role="assistant", text=streamed,
                                    runtime_session_id="s" * 40))
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("complete_block", [True, False])
def test_startup_sweep_finalizes_a_dead_turn_from_its_event_log(client, ready, complete_block):
    """The process died mid-turn. On the next startup the turn is persisted as
    interrupted from what the log had: the text, and a proposal block that had streamed
    completely (an unclosed one yields none); the log gets its terminal event so a
    reattaching client sees the end; the claim is cleared. Nothing is re-invoked."""
    prose = "Design for the helpdesk agent. " * 3
    block = _block(VALID_PROPOSAL)
    streamed = prose + block if complete_block else prose + block[: len(block) // 2]
    cid = _open(client)
    _dead_turn(cid, streamed)
    assert service.clear_stale_turn_claims() == 1
    db = SessionLocal()
    try:
        conv = db.get(AssistantConversation, cid)
        assert conv.active_turn is None and conv.active_turn_token is None
        msgs = [m for m in service._messages(db, cid) if m.role != service.SESSION_ROLE]
        roles = [m.role for m in msgs]
        assert roles[:3] == ["user", "assistant", "error"]
        assert msgs[1].text == streamed
        assert msgs[2].name == service.TURN_INTERRUPTED_NAME
        assert msgs[2].text == service.TURN_INTERRUPTED_TEXT
        latest = service.latest_proposal(db, cid)
        if complete_block:
            assert latest is not None and latest.status == "draft" and latest.revision == 1
            assert latest.created_by == "river"
        else:
            assert latest is None
        events = service.turn_events(db, cid, 1)
        assert events[-1].event == "error" and events[-1].data == {"message": "interrupted"}
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
    finally:
        db.close()
    # a reattach after the restart replays the log and ends on the sweep's terminal event
    res = client.get(f"{BASE}/conversations/{cid}/turns/1/stream")
    kinds = [k for k, _d, _s in _sse_with_ids(res.text)]
    assert kinds[0] == "meta" and kinds[-1] == "error"
    assert _latest(client, cid)["live_turn"] is None


def test_startup_sweep_only_clears_a_claim_whose_turn_already_has_its_terminal_row(
    client, ready
):
    cid = _open(client)
    _dead_turn(cid, "already answered", with_terminal_row=True)
    assert service.clear_stale_turn_claims() == 1
    db = SessionLocal()
    try:
        msgs = service._messages(db, cid)
        assert [m.role for m in msgs] == ["user", "assistant"]  # no interrupted row added
        assert db.get(AssistantConversation, cid).active_turn is None
        assert not any(e.event in service.TURN_TERMINAL_EVENTS
                       for e in service.turn_events(db, cid, 1))
    finally:
        db.close()


def test_explicit_cancel_is_still_an_interrupted_turn(ready, harness):
    """``TurnRun.cancel`` keeps its meaning (an abort), it is just no longer what a
    closed connection does."""
    harness.reply("some text " * 4 + _block(VALID_PROPOSAL))
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        conv = service.create_conversation(db, row, workspace_context(row), IDENTITY, "t")
        run = service.TurnRun()
        gen = service.run_turn(db, conv, row, workspace_context(row), IDENTITY, "go", run=run)
        run.generator = gen
        assert next(gen)["event"] == "meta"
        assert next(gen)["event"] == "delta"
        run.cancel(wait_s=5)
        assert run.finished
        msgs = service._messages(db, conv.id)
        assert [m.role for m in msgs] == ["user", "assistant", "error"]
        assert msgs[2].name == service.TURN_INTERRUPTED_NAME
    finally:
        db.close()


# ---------------------------------------------------------------------------
# rev-r10 follow-ups: the driver's own cleanup is explicit, the pre-meta window is closed
# ---------------------------------------------------------------------------


def test_driver_failure_closes_run_turn_and_finalizes_without_gc(
    client, ready, harness, monkeypatch
):
    """A failure in the driver's loop body (here ``LiveTurn.publish`` on the first delta)
    leaves ``run_turn`` suspended at its yield. The driver closes it explicitly, so the
    moment the driver thread has joined — no ``gc.collect`` anywhere — the turn is persisted
    interrupted, the claim is released and the upstream stream is closed."""
    harness.reply("partial answer " * 4)
    original = service.LiveTurn.publish
    tripped: list[str] = []

    def publish(self, event):
        if event["event"] == "delta" and not tripped:
            tripped.append(event["data"]["text"])
            raise RuntimeError("publish exploded")
        return original(self, event)

    monkeypatch.setattr(service.LiveTurn, "publish", publish)
    cid = _open(client)
    live = _start(cid)
    live.thread.join(timeout=15)
    assert not live.thread.is_alive() and live.done and tripped
    # asserted immediately after the join: nothing below may depend on a collector
    assert live.run.inner is not None and live.run.inner.gi_frame is None  # closed
    assert live.run.finished
    assert harness.streams[-1].closed  # the upstream was closed by run_turn's finalizer
    db = SessionLocal()
    try:
        conv = db.get(AssistantConversation, cid)
        assert conv.active_turn is None and conv.active_turn_token is None
        msgs = [m for m in service._messages(db, cid) if m.role != service.SESSION_ROLE]
        assert [m.role for m in msgs] == ["user", "assistant", "error"]
        assert msgs[1].text == tripped[0]  # what run_turn had produced before the failure
        assert msgs[2].name == service.TURN_INTERRUPTED_NAME
        assert service.latest_proposal(db, cid) is None
        events = service.turn_events(db, cid, 1)
        assert [e.event for e in events] == ["meta", "error"]
        assert "publish exploded" in events[-1].data["message"]
    finally:
        db.close()
    assert service.live_run(cid) is None
    assert _latest(client, cid)["live_turn"] is None


def test_reattach_to_turn_n_never_tails_a_pre_meta_turn_n_plus_one(
    client, ready, harness, monkeypatch
):
    """Turn 1 is complete; turn 2 has been started but is held before its ``meta`` (its
    number is not published yet). A reattach to turn 1 must end on turn 1's own terminal
    event — never tail the unidentified live buffer — and ``live_run(cid, 2)`` resolves
    to the live run as soon as ``meta`` lands."""
    harness.reply("turn one. " * 3)
    cid = _open(client)
    assert _turn(client, cid, "first")[-1][0] == "done"
    gate = threading.Event()
    compose = service.compose_messages

    def gated_compose(*args, **kwargs):  # after the claim, before ``meta``
        gate.wait(timeout=30)
        return compose(*args, **kwargs)

    monkeypatch.setattr(service, "compose_messages", gated_compose)
    live = _start(cid, "second")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:  # the claim is taken on the driver thread
        db = SessionLocal()
        try:
            if db.get(AssistantConversation, cid).active_turn == 2:
                break
        finally:
            db.close()
        time.sleep(0.02)
    assert live.turn is None and service.live_run(cid) is live  # pre-meta, still live
    assert service.live_run(cid, 1) is None  # waited one HEARTBEAT_S, then refused
    # the reattach to turn 1 takes the ledger path and ends on turn 1's terminal event
    replay = [e for e in service.stream_turn(cid, 1) if e["event"] != "heartbeat"]
    assert replay[0]["event"] == "meta" and replay[0]["data"]["turn"] == 1
    assert replay[-1]["event"] == "done" and replay[-1]["data"] == {"turn": 1}
    assert live.turn is None  # turn 2 is still held: nothing of it was handed out
    detail = _latest(client, cid)
    assert detail["live_turn"]["turn"] == 2  # the view reports the claim, not turn 1

    # a reattach to turn 2 that arrives pre-meta waits for ``meta`` instead of refusing
    found: list = []
    waiter = threading.Thread(target=lambda: found.append(service.live_run(cid, 2)))
    waiter.start()
    time.sleep(0.1)
    gate.set()
    waiter.join(timeout=5)
    assert found == [live] and live.turn == 2
    assert service.live_run(cid, 1) is None
    _wait_turn_finished(cid)
    detail = _latest(client, cid)
    assert detail["turns"] == 2 and detail["live_turn"] is None
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"] * 2


# ---------------------------------------------------------------------------
# upstream #241 × N-ARCH-obs15: a read-only viewer tails, never sends
# ---------------------------------------------------------------------------


def test_read_only_admin_viewer_tails_a_members_live_turn_but_never_sends(gated, harness):
    """An administrator reading another member's UNSHARED conversation sees its
    ``live_turn`` and may TAIL it through the stream route — the stream is a
    subscription (it writes nothing) and a read-only viewer has no turn of its own to
    reattach to — while every send stays 404 and another member sees nothing. Once the
    conversation is shared the admin is a collaborator and the unchanged sender rule
    (``409 assistant.turn_not_yours``) applies to it again."""
    admin, member, other, ids, _preset = gated
    body = _GatedBody(_deltas("working. "), _deltas(" finished."))
    harness.invoke_agent_runtime = lambda **kw: {"response": body,
                                                 "contentType": "text/event-stream"}
    cid = _open(member)
    sender = Identity(username=MEMBER_CREDS["username"], role=ROLE_MEMBER,
                      user_id=ids[MEMBER_CREDS["username"]])
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        live = service.start_turn(cid, DEFAULT_WORKSPACE_ID, row, workspace_context(row),
                                  sender, "the member's turn")
    finally:
        db.close()
    deadline = time.monotonic() + 5
    while live.seq < 2 and time.monotonic() < deadline:  # meta + the head delta
        time.sleep(0.02)
    assert live.turn == 1 and live.seq >= 2

    detail = admin.get(f"{BASE}/conversations/{cid}")
    assert detail.status_code == 200, detail.text
    shown = detail.json()
    assert shown["read_only"] is True and shown["mine"] is False
    assert shown["turn_in_progress"] == 1
    assert shown["live_turn"]["turn"] == 1 and shown["live_turn"]["state"] == "running"
    assert shown["live_turn"]["author"] == MEMBER_CREDS["username"]
    # a send is refused by the collaboration rule (404 — never the live claim's 409)
    res = admin.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "x"})
    assert res.status_code == 404 and res.json()["code"] == "assistant.conversation_not_found"
    # another member neither reads the conversation nor tails its turn
    assert other.get(f"{BASE}/conversations/{cid}").status_code == 404
    assert other.get(f"{BASE}/conversations/{cid}/turns/1/stream").status_code == 404

    # the read-only viewer tails the live turn from the start
    tailed: dict = {}
    thread = threading.Thread(
        target=lambda: tailed.update(res=admin.get(f"{BASE}/conversations/{cid}/turns/1/stream")),
        daemon=True)
    thread.start()
    time.sleep(0.3)
    assert thread.is_alive()  # attached and waiting on the producer (gate still closed)
    body.gate.set()
    thread.join(timeout=30)
    assert not thread.is_alive()
    res = tailed["res"]
    assert res.status_code == 200, res.text
    events = _sse_with_ids(res.text)
    kinds = [k for k, _d, _s in events]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    assert "".join(d["text"] for k, d, _s in events if k == "delta") == "working.  finished."
    seqs = [s for _k, _d, s in events if s is not None]
    assert seqs == list(range(1, len(seqs) + 1))

    _wait_turn_finished(cid)
    after = admin.get(f"{BASE}/conversations/{cid}").json()
    assert after["live_turn"] is None and after["read_only"] is True
    assert [(m["role"], m["author"]) for m in after["messages"]] == [
        ("user", MEMBER_CREDS["username"]), ("assistant", None)]
    # the finished turn replays for the viewer; a turn that never happened is 404
    assert admin.get(f"{BASE}/conversations/{cid}/turns/1/stream").status_code == 200
    res = admin.get(f"{BASE}/conversations/{cid}/turns/2/stream")
    assert res.status_code == 404 and res.json()["code"] == "assistant.turn_not_found"
    # the owner reattaches to its own turn as before
    assert member.get(f"{BASE}/conversations/{cid}/turns/1/stream").status_code == 200

    # shared → the admin collaborates, and the sender rule is the collaborator's
    assert member.put(f"{BASE}/conversations/{cid}/sharing",
                      json={"shared": True}).status_code == 200
    assert admin.get(f"{BASE}/conversations/{cid}").json()["read_only"] is False
    res = admin.get(f"{BASE}/conversations/{cid}/turns/1/stream")
    assert res.status_code == 409 and res.json()["code"] == "assistant.turn_not_yours"
    assert res.json()["detail"]["author"] == MEMBER_CREDS["username"]
