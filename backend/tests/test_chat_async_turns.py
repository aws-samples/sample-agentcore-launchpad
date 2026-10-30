"""N-ARCH-obs15 v2 (chat parity): a console chat turn is a server-side unit of work,
independent of any connection. Pins: a subscriber that leaves mid-stream changes nothing
about the ``ChatMessage`` rows (identical to a watched turn); the per-turn event log
(``chat_turn_events``) is ``seq``-ordered and matches the ``id:`` frames; ``GET …/sessions/
{sid}/turns/{turn}/stream?from=`` replays exactly the missed events and tails the live
producer; the history's ``live_turn`` tells a page load what to reattach to; a second turn
on a session while one runs is refused; the policy gate still refuses inside the stream
and still writes its audit row; the as_user authorization URL never reaches the log; the
startup sweep finalizes a dead process's turn from its event log — never resumes it.
Hermetic: temp ledger, ``invoke_agent_events`` stubbed."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

import app.routers.chat as chat_router
import app.services.chat as chat_service
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.display_time import display_iso
from app.core.errors import AppError
from app.models.ledger import Agent, ChatMessage, ChatSession, ChatTurnEvent
from app.services import chat_turns
from app.services.chat import chat_stream
from tests.conftest import ws_ctx
from tests.test_chat_api import delta_events, make_active_agent

SID = "s" * 40


def _frames(text: str) -> list[tuple[str, dict, int | None]]:
    """``(event, data, id)`` per SSE frame — ``id:`` carries the turn's ``seq``."""
    out = []
    # SSE: LF-only parsing (N17-obs52: splitlines() cut a data: JSON string at U+2028-class
    # characters); CRLF / bare-CR streams are normalised as a whole BEFORE the frame split
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
            out.append((kind, data, seq))
    return out


def _rows(sid: str) -> list[tuple[str, str, str | None, bool]]:
    db = SessionLocal()
    try:
        return [(m.role, m.text, m.name, m.truncated) for m in
                db.query(ChatMessage).filter(ChatMessage.session_id == sid)
                .order_by(ChatMessage.id.asc()).all()]
    finally:
        db.close()


def _log(agent_id: str, sid: str, turn: int = 1) -> list[tuple[str, int, dict]]:
    db = SessionLocal()
    try:
        return [(e.event, e.seq, e.data)
                for e in chat_turns.turn_events(db, agent_id, sid, turn)]
    finally:
        db.close()


def _session(agent_id: str, sid: str) -> ChatSession | None:
    db = SessionLocal()
    try:
        return chat_turns.session_row(db, DEFAULT_WORKSPACE_ID, agent_id, sid)
    finally:
        db.close()


def _take(sub, n: int) -> list[dict]:
    out: list[dict] = []
    while len(out) < n:
        event = next(sub)
        if event["event"] != "heartbeat":
            out.append(event)
    return out


def _wait_finished(agent_id: str, sid: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if chat_turns.live_run(agent_id, sid) is None and not any(
                t.name.startswith("chat-turn-driver-") for t in threading.enumerate()):
            return
        time.sleep(0.02)
    raise AssertionError("the detached chat turn did not finish within the budget")


def _start(agent_id: str, sid: str, prompt: str = "hi") -> chat_turns.ChatLiveTurn:
    """A detached turn started the way the router does, without a response attached."""
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
    finally:
        db.close()
    recorder = chat_turns.TranscriptRecorder(
        DEFAULT_WORKSPACE_ID, agent_id, prompt=prompt, actor_id="river", attachments=None,
        runtime_version=agent.version,
    )
    return chat_turns.start_turn(
        agent_id, DEFAULT_WORKSPACE_ID, sid, actor_id="river", username="river",
        stream=lambda s: chat_stream(agent, prompt, session_id=s, workspace=ws_ctx()),
        recorder=recorder,
    )


class _Gated:
    """``invoke_agent_events`` stub: ``head`` events, wait for the gate, ``tail`` events."""

    def __init__(self, head: list[dict], tail: list[dict]) -> None:
        self.head, self.tail = head, tail
        self.gate = threading.Event()

    def __call__(self, *_a, **_k):
        yield from self.head
        self.gate.wait(timeout=30)
        yield from self.tail


SCRIPT = [
    {"event": "delta", "data": {"text": "Looking that up. "}},
    {"event": "tool", "data": {"name": "search", "id": "t-1"}},
    {"event": "delta", "data": {"text": "The answer "}},
    {"event": "delta", "data": {"text": "is 42."}},
]


# ---------------------------------------------------------------------------
# the connection is not the turn
# ---------------------------------------------------------------------------


def test_frames_carry_seq_and_meta_turn_and_match_the_event_log(client, monkeypatch):
    agent_id = make_active_agent(method="container", name="chat-seq")
    monkeypatch.setattr(chat_service, "invoke_agent_events", lambda *a, **k: iter(SCRIPT))
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "q", "session_id": SID})
    assert res.status_code == 200
    framed = [(k, s) for k, _d, s in _frames(res.text) if k != "heartbeat"]
    assert [s for _k, s in framed] == list(range(1, len(framed) + 1))
    assert [k for k, _s in framed] == ["meta", "delta", "tool", "delta", "delta", "done"]
    meta = _frames(res.text)[0][1]
    assert meta["session_id"] == SID and meta["turn"] == 1 and meta["mode"] == "stream"
    assert "event: heartbeat" not in res.text  # keep-alives are comments, never events
    log = _log(agent_id, SID)
    assert [(e, s) for e, s, _d in log] == framed
    assert log[0][2]["turn"] == 1
    db = SessionLocal()
    try:
        assert all(e.workspace_id == DEFAULT_WORKSPACE_ID
                   for e in db.query(ChatTurnEvent).all())
    finally:
        db.close()
    row = _session(agent_id, SID)
    assert row.turns == 1 and row.active_turn is None and row.active_turn_token is None
    # the second turn of the same session is turn 2 with its own seq 1..n
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "again", "session_id": SID})
    frames = _frames(res.text)
    assert frames[0][1]["turn"] == 2 and frames[0][2] == 1 and frames[-1][0] == "done"
    assert [e for e, _s, _d in _log(agent_id, SID, 2)][-1] == "done"


def test_subscriber_leaving_mid_turn_changes_nothing_about_the_transcript(
    client, monkeypatch
):
    """Two sessions, the same scripted reply: one watched to the end over HTTP, one whose
    only subscriber closes after two events. ChatMessage rows and the event log are
    identical; the claim is released; nothing is marked interrupted."""
    agent_id = make_active_agent(method="container", name="chat-detach")
    monkeypatch.setattr(chat_service, "invoke_agent_events", lambda *a, **k: iter(SCRIPT))
    watched = "w" * 40
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "hi", "session_id": watched})
    assert _frames(res.text)[-1][0] == "done"

    gated = _Gated(SCRIPT[:1], SCRIPT[1:])
    monkeypatch.setattr(chat_service, "invoke_agent_events", gated)
    detached = "d" * 40
    live = _start(agent_id, detached)
    sub = live.subscribe(0)
    seen = _take(sub, 2)
    assert [e["event"] for e in seen] == ["meta", "delta"]
    sub.close()  # the member's connection is gone
    gated.gate.set()
    _wait_finished(agent_id, detached)

    assert _rows(detached) == _rows(watched)
    assert [r[0] for r in _rows(detached)] == ["user", "agent", "tool", "agent"]
    assert [(e, s) for e, s, _d in _log(agent_id, detached)] == \
        [(e, s) for e, s, _d in _log(agent_id, watched)]
    assert _log(agent_id, detached)[-1][0] == "done"
    row = _session(agent_id, detached)
    assert row.active_turn is None and row.turns == 1


# ---------------------------------------------------------------------------
# reattach
# ---------------------------------------------------------------------------


def test_reattach_from_seq_replays_the_missed_events_then_tails_the_producer(
    client, monkeypatch
):
    agent_id = make_active_agent(method="container", name="chat-reattach")
    gated = _Gated(SCRIPT[:2], SCRIPT[2:])
    monkeypatch.setattr(chat_service, "invoke_agent_events", gated)
    live = _start(agent_id, SID)
    sub = live.subscribe(0)
    head = _take(sub, 3)  # meta, delta, tool — then the connection drops
    sub.close()
    assert [e["event"] for e in head] == ["meta", "delta", "tool"]
    history = client.get(f"/api/chat/{agent_id}/history", params={"session_id": SID}).json()
    assert history["live_turn"] == {
        "turn": 1, "seq": 3, "started_at": display_iso(live.started_at),
        "state": "running", "author": "river",
    }
    assert [m["role"] for m in history["messages"]] == ["user", "agent", "tool"]
    sessions = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"]
    assert sessions[0]["live_turn"]["turn"] == 1 and sessions[0]["live_turn"]["state"] == "running"

    results: list = []
    reader = threading.Thread(target=lambda: results.append(client.get(
        f"/api/chat/{agent_id}/sessions/{SID}/turns/1/stream", params={"from": 3})))
    reader.start()
    time.sleep(0.2)
    gated.gate.set()
    reader.join(timeout=15)
    res = results[0]
    assert res.status_code == 200, res.text
    tail = [(k, s) for k, _d, s in _frames(res.text) if k != "heartbeat"]
    assert tail == [("delta", 4), ("delta", 5), ("done", 6)]
    _wait_finished(agent_id, SID)
    assert [r[0] for r in _rows(SID)] == ["user", "agent", "tool", "agent"]
    assert client.get(f"/api/chat/{agent_id}/history",
                      params={"session_id": SID}).json()["live_turn"] is None


def test_reattach_after_completion_replays_everything_and_ends_with_done(client, monkeypatch):
    agent_id = make_active_agent(method="container", name="chat-replay")
    monkeypatch.setattr(chat_service, "invoke_agent_events", lambda *a, **k: iter(SCRIPT))
    original = [(k, s) for k, _d, s in _frames(client.post(
        f"/api/chat/{agent_id}", json={"prompt": "q", "session_id": SID}).text)]
    base = f"/api/chat/{agent_id}/sessions/{SID}/turns/1/stream"
    res = client.get(base)
    assert res.status_code == 200
    assert [(k, s) for k, _d, s in _frames(res.text)] == original
    # from the penultimate seq: only the terminal event
    res = client.get(base, params={"from": original[-1][1] - 1})
    assert [(k, s) for k, _d, s in _frames(res.text)] == [original[-1]]
    # at or past the end: a bare ``done`` so the client can settle
    assert [k for k, _d, _s in _frames(client.get(base, params={"from": 99}).text)] == ["done"]
    res = client.get(base, params={"from": "x"})
    assert res.status_code == 422 and res.json()["code"] == "chat.invalid_seq"


def test_reattach_refusals(client, monkeypatch):
    agent_id = make_active_agent(method="container", name="chat-refuse")
    monkeypatch.setattr(chat_service, "invoke_agent_events", lambda *a, **k: iter(SCRIPT))
    client.post(f"/api/chat/{agent_id}", json={"prompt": "q", "session_id": SID})
    res = client.get(f"/api/chat/{agent_id}/sessions/{SID}/turns/7/stream")
    assert res.status_code == 404 and res.json()["code"] == "chat.turn_not_found"
    res = client.get(f"/api/chat/{agent_id}/sessions/{'z' * 40}/turns/1/stream")
    assert res.status_code == 404 and res.json()["code"] == "chat.session_not_found"
    other = make_active_agent(method="container", name="chat-other")
    res = client.get(f"/api/chat/{other}/sessions/{SID}/turns/1/stream")
    assert res.status_code == 404 and res.json()["code"] == "chat.session_not_found"
    # another member's turn: the member who sent a turn is the one who reattaches
    row = _session(agent_id, SID)
    db = SessionLocal()
    try:
        with pytest.raises(AppError) as exc:
            chat_turns.require_turn(db, row, 1, "someone-else")
        assert exc.value.code == "chat.turn_not_yours" and exc.value.status_code == 409
        assert exc.value.detail == {"author": "river"}
        chat_turns.require_turn(db, row, 1, "river")  # the sender
    finally:
        db.close()


def test_turn_ownership_is_the_sender_not_the_sessions_first_owner(client, monkeypatch):
    """rev-r11 nit 1: the base POST lets any workspace member continue a session while
    ``chat_sessions.actor_id`` keeps the first sender (the Memory owner). The reattach
    ownership is per TURN — the sender: the live run's username while it runs, the
    ``author`` the driver stamps on the logged ``meta`` afterwards — so the member who
    continued the session reattaches to their own turn, the first owner is refused on it
    (and vice versa), and a viewer who sent neither is refused on both."""
    agent_id = make_active_agent(method="container", name="chat-shared")
    monkeypatch.setattr(chat_service, "invoke_agent_events", lambda *a, **k: iter(SCRIPT))
    # turn 1: river opens the session (its actor); the wire ``meta`` names the sender
    frames = _frames(client.post(f"/api/chat/{agent_id}",
                                 json={"prompt": "q", "session_id": SID}).text)
    assert frames[0][0] == "meta" and frames[0][1]["turn"] == 1
    assert frames[0][1]["author"] == "river"
    assert _log(agent_id, SID, 1)[0][2]["author"] == "river"  # logged, not only on the wire
    # turn 2: pat continues the shared session (the router keeps ``actor_id`` = river and
    # passes the sender as the live username — replayed here with a gated producer)
    gated = _Gated(SCRIPT[:1], SCRIPT[1:])
    monkeypatch.setattr(chat_service, "invoke_agent_events", gated)
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
    finally:
        db.close()
    recorder = chat_turns.TranscriptRecorder(
        DEFAULT_WORKSPACE_ID, agent_id, prompt="again", actor_id="river", attachments=None,
        runtime_version=agent.version,
    )
    live = chat_turns.start_turn(
        agent_id, DEFAULT_WORKSPACE_ID, SID, actor_id="river", username="pat",
        stream=lambda s: chat_stream(agent, "again", session_id=s, workspace=ws_ctx()),
        recorder=recorder,
    )
    deadline = time.monotonic() + 5
    while live.turn is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert live.turn == 2
    row = _session(agent_id, SID)
    assert row.actor_id == "river"  # the session's first owner is unchanged
    db = SessionLocal()
    try:
        # while live: the page-load detail names pat, pat's reattach passes, river's and a
        # stranger's are refused with the sender
        view = chat_turns.live_turn_view(db, row)
        assert view["turn"] == 2 and view["author"] == "pat"
        history = client.get(f"/api/chat/{agent_id}/history", params={"session_id": SID}).json()
        assert history["live_turn"]["author"] == "pat"
        chat_turns.require_turn(db, row, 2, "pat")
        for viewer in ("river", "someone-else"):
            with pytest.raises(AppError) as exc:
                chat_turns.require_turn(db, row, 2, viewer)
            assert exc.value.code == "chat.turn_not_yours" and exc.value.detail == {"author": "pat"}
        # river's own earlier turn is still river's, not pat's
        chat_turns.require_turn(db, row, 1, "river")
        with pytest.raises(AppError) as exc:
            chat_turns.require_turn(db, row, 1, "pat")
        assert exc.value.detail == {"author": "river"}
        gated.gate.set()
        _wait_finished(agent_id, SID)
        # finished: the logged ``meta.author`` keeps the same answers
        assert _log(agent_id, SID, 2)[0][2]["author"] == "pat"
        chat_turns.require_turn(db, row, 2, "pat")
        with pytest.raises(AppError) as exc:
            chat_turns.require_turn(db, row, 2, "river")
        assert exc.value.detail == {"author": "pat"}
        chat_turns.require_turn(db, row, 1, "river")
        assert chat_turns.turn_author(db, row, 1) == "river"
        assert chat_turns.turn_author(db, row, 2) == "pat"
        # the wire: the first owner reattaching to pat's turn is a 409 end to end (the test
        # client is the gate-off operator "river")
        res = client.get(f"/api/chat/{agent_id}/sessions/{SID}/turns/2/stream")
        assert res.status_code == 409 and res.json()["code"] == "chat.turn_not_yours"
        assert res.json()["detail"] == {"author": "pat"}
        assert client.get(f"/api/chat/{agent_id}/sessions/{SID}/turns/1/stream").status_code == 200
    finally:
        db.close()


def test_turn_ownership_falls_back_to_the_session_actor_for_a_log_without_author(client):
    """A log written before the ``meta.author`` stamp (or a dead process's turn seeded
    without it) keeps the pre-r11-b2 answer: the session's actor owns the turn."""
    agent_id = make_active_agent(method="container", name="chat-oldlog")
    _dead_turn(agent_id, SID)  # its ``meta`` carries no ``author``
    row = _session(agent_id, SID)
    db = SessionLocal()
    try:
        assert chat_turns.turn_author(db, row, 1) == "river"
        assert chat_turns.live_turn_view(db, row)["author"] == "river"
        chat_turns.require_turn(db, row, 1, "river")
        with pytest.raises(AppError) as exc:
            chat_turns.require_turn(db, row, 1, "pat")
        assert exc.value.detail == {"author": "river"}
    finally:
        db.close()


def test_second_post_while_a_turn_runs_is_refused(client, monkeypatch):
    agent_id = make_active_agent(method="container", name="chat-409")
    gated = _Gated(SCRIPT[:1], SCRIPT[1:])
    monkeypatch.setattr(chat_service, "invoke_agent_events", gated)
    live = _start(agent_id, SID)
    deadline = time.monotonic() + 5
    while live.turn is None and time.monotonic() < deadline:
        time.sleep(0.02)
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "again", "session_id": SID})
    assert res.status_code == 409 and res.json()["code"] == "chat.turn_in_progress"
    assert res.json()["detail"] == {"active_turn": 1}
    # a fresh session of the same agent is not blocked
    other = "o" * 40
    assert chat_turns.live_run(agent_id, other) is None
    gated.gate.set()
    _wait_finished(agent_id, SID)
    assert _session(agent_id, SID).turns == 1  # the refused POST wrote nothing
    assert [r[0] for r in _rows(SID)] == ["user", "agent", "tool", "agent"]
    # a still-valid claim held by nobody here (another process) also refuses…
    db = SessionLocal()
    try:
        row = chat_turns.session_row(db, DEFAULT_WORKSPACE_ID, agent_id, SID)
        row.active_turn, row.active_turn_token = 1, "other"
        row.active_turn_started_at = datetime.now(UTC) - timedelta(seconds=120)
        db.commit()
    finally:
        db.close()
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "x", "session_id": SID})
    assert res.status_code == 409 and res.json()["code"] == "chat.turn_in_progress"
    # …until it is older than the claim TTL (an orphan the sweep did not see yet)
    db = SessionLocal()
    try:
        row = chat_turns.session_row(db, DEFAULT_WORKSPACE_ID, agent_id, SID)
        row.active_turn_started_at = datetime.now(UTC) - timedelta(
            seconds=chat_turns.TURN_CLAIM_TTL_S + 5)
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(chat_service, "invoke_agent_events", lambda *a, **k: iter(SCRIPT))
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "x", "session_id": SID})
    assert res.status_code == 200 and _frames(res.text)[0][1]["turn"] == 2


# ---------------------------------------------------------------------------
# the p2-a frames
# ---------------------------------------------------------------------------


def test_runtime_guard_frames_flow_through_the_log_and_the_transcript(client, monkeypatch):
    """p2-a: a ``policy_denied`` frame the runtime guard emitted is logged with its audit
    id, replayed on reattach, and the ``policy`` row still follows its tool row."""
    agent_id = make_active_agent(method="container", name="chat-guard")
    frame = {"tool": "hr-database___create_payout", "tool_use_id": "t-1",
             "reason": "Policy evaluation denied due to cap-1", "policy_id": "cap-1",
             "decision_id": "dec-1"}
    monkeypatch.setattr(chat_service, "invoke_agent_events", lambda *a, **k: iter([
        {"event": "tool", "data": {"name": "gw_create_payout", "id": "t-1"}},
        {"event": "policy_denied", "data": frame},
        *delta_events("blocked by policy"),
    ]))
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "pay 800", "session_id": SID})
    assert [k for k, _d, _s in _frames(res.text)] == \
        ["meta", "tool", "policy_denied", "delta", "done"]
    assert _log(agent_id, SID)[2] == ("policy_denied", 3, frame)
    assert [(r[0], r[1], r[2]) for r in _rows(SID)] == [
        ("user", "pay 800", None), ("tool", "", "gw_create_payout"),
        ("policy", "Policy evaluation denied due to cap-1", "hr-database___create_payout"),
        ("agent", "blocked by policy", None),
    ]
    replay = _frames(client.get(f"/api/chat/{agent_id}/sessions/{SID}/turns/1/stream").text)
    assert replay[2] == ("policy_denied", frame, 3)


def test_the_as_user_authorization_url_never_reaches_the_log(client, monkeypatch):
    agent_id = make_active_agent(method="container", name="chat-ask")
    ask = {"provider": "github", "tool": "list_repos", "scopes": ["repo"],
           "url": "https://idp.example/authorize?state=once", "agent_id": agent_id}
    monkeypatch.setattr(chat_service, "invoke_agent_events", lambda *a, **k: iter([
        {"event": "auth_required", "data": ask},
        *delta_events("please authorize"),
    ]))
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "repos", "session_id": SID})
    frames = _frames(res.text)
    assert frames[1][0] == "auth_required" and frames[1][1]["url"] == ask["url"]  # the wire
    logged = _log(agent_id, SID)[1]
    assert logged[0] == "auth_required" and "url" not in logged[2]
    assert logged[2]["provider"] == "github" and logged[2]["tool"] == "list_repos"
    assert "once" not in json.dumps([d for _e, _s, d in _log(agent_id, SID)])
    assert [(r[0], r[1], r[2]) for r in _rows(SID)] == [
        ("user", "repos", None), ("auth", "github", "list_repos"),
        ("agent", "please authorize", None),
    ]


# ---------------------------------------------------------------------------
# only the producer's own failure or a dead process interrupt a turn
# ---------------------------------------------------------------------------


def test_producer_failure_while_nobody_watches_persists_the_error_row_and_event(
    client, monkeypatch
):
    agent_id = make_active_agent(method="container", name="chat-fail")

    def boom(*_a, **_k):
        raise RuntimeError("runtime exploded")

    monkeypatch.setattr(chat_service, "invoke_agent_events", boom)
    _start(agent_id, SID, prompt="doomed")
    _wait_finished(agent_id, SID)
    rows = _rows(SID)
    assert [r[0] for r in rows] == ["user", "error"] and "runtime exploded" in rows[1][1]
    assert [e for e, _s, _d in _log(agent_id, SID)] == ["meta", "error"]
    assert _session(agent_id, SID).active_turn is None
    res = client.get(f"/api/chat/{agent_id}/sessions/{SID}/turns/1/stream")
    assert [k for k, _d, _s in _frames(res.text)] == ["meta", "error"]


def test_a_producer_that_ends_without_a_terminal_event_still_ends_the_log(client, monkeypatch):
    """Only a scripted producer can do this (``chat_stream`` always ends on done/error):
    the rows are what the response generator used to keep, and the log gets a terminal
    ``error`` so a reattaching client is not left waiting."""
    agent_id = make_active_agent(method="container", name="chat-noend")

    def fake_stream(agent, prompt, **kwargs):
        yield {"event": "meta", "data": {"session_id": kwargs["session_id"],
                                         "agent": agent.name, "mode": "stream"}}
        yield {"event": "delta", "data": {"text": "half"}}

    monkeypatch.setattr(chat_router, "chat_stream", fake_stream)
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "q", "session_id": SID})
    assert [k for k, _d, _s in _frames(res.text)] == ["meta", "delta", "error"]
    assert [(r[0], r[1]) for r in _rows(SID)] == [("user", "q"), ("agent", "half")]
    assert _log(agent_id, SID)[-1] == ("error", 3, {"message": "interrupted"})


def _dead_turn(agent_id: str, sid: str, *, with_terminal: bool = False) -> None:
    """A session a dead process left mid-turn: the claim, the rows of every logged event
    (user row, tool row) and the event log of what had streamed — no terminal event."""
    db = SessionLocal()
    try:
        db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, session_id=sid,
                           actor_id="river", turns=1, active_turn=1, active_turn_token="dead",
                           active_turn_started_at=datetime.now(UTC) - timedelta(seconds=120)))
        db.add(ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                           session_id=sid, role="user", text="compute"))
        db.add(ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                           session_id=sid, role="agent", text="Looking that up. "))
        db.add(ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                           session_id=sid, role="tool", text="", name="search"))
        events = [("meta", {"session_id": sid, "agent": "a", "mode": "stream", "turn": 1}),
                  ("delta", {"text": "Looking that up. "}),
                  ("tool", {"name": "search", "id": "t-1"}),
                  ("delta", {"text": "The answer "}),
                  ("delta", {"text": "is 4"})]
        if with_terminal:
            events.append(("done", {"latency_ms": 5}))
        for seq, (event, data) in enumerate(events, start=1):
            db.add(ChatTurnEvent(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                                 session_id=sid, turn=1, seq=seq, event=event, data=data))
        db.commit()
    finally:
        db.close()


def test_startup_sweep_finalizes_a_dead_turn_from_its_event_log(client):
    agent_id = make_active_agent(method="container", name="chat-sweep")
    _dead_turn(agent_id, SID)
    history = client.get(f"/api/chat/{agent_id}/history", params={"session_id": SID}).json()
    assert history["live_turn"] == {"turn": 1, "seq": 5, "state": "running", "author": "river",
                                    "started_at": history["live_turn"]["started_at"]}
    assert chat_turns.clear_stale_turn_claims() == 1
    rows = _rows(SID)
    assert [(r[0], r[1]) for r in rows] == [
        ("user", "compute"), ("agent", "Looking that up. "), ("tool", ""),
        ("agent", "The answer is 4"),  # the open bubble, rebuilt from the log
        ("error", chat_turns.INTERRUPTED_TEXT),
    ]
    log = _log(agent_id, SID)
    assert log[-1] == ("error", 6, {"message": "interrupted"})
    row = _session(agent_id, SID)
    assert row.active_turn is None and row.active_turn_token is None
    assert client.get(f"/api/chat/{agent_id}/history",
                      params={"session_id": SID}).json()["live_turn"] is None
    # a reattaching client sees the turn end
    res = client.get(f"/api/chat/{agent_id}/sessions/{SID}/turns/1/stream", params={"from": 5})
    assert [(k, s) for k, _d, s in _frames(res.text)] == [("error", 6)]
    assert chat_turns.clear_stale_turn_claims() == 0  # idempotent


def test_startup_sweep_only_clears_a_claim_whose_turn_already_ended(client):
    agent_id = make_active_agent(method="container", name="chat-sweep-done")
    _dead_turn(agent_id, SID, with_terminal=True)
    before = _rows(SID)
    assert chat_turns.clear_stale_turn_claims() == 1
    assert _rows(SID) == before  # nothing written
    assert [e for e, _s, _d in _log(agent_id, SID)] == \
        ["meta", "delta", "tool", "delta", "delta", "done"]
    assert _session(agent_id, SID).active_turn is None


def test_reattach_polls_the_ledger_while_another_process_holds_the_claim(client):
    agent_id = make_active_agent(method="container", name="chat-poll")
    _dead_turn(agent_id, SID)  # a fresh claim nobody holds here
    got: list[dict] = []
    sub = chat_turns.stream_turn(DEFAULT_WORKSPACE_ID, agent_id, SID, 1, after=3)
    got += _take(sub, 2)
    assert [(e["event"], e["seq"]) for e in got] == [("delta", 4), ("delta", 5)]
    heartbeats = 0
    while heartbeats < 1:  # nothing new: the poller keeps the connection alive
        if next(sub)["event"] == "heartbeat":
            heartbeats += 1
    db = SessionLocal()
    try:  # the other process finishes the turn
        db.add(ChatTurnEvent(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                             session_id=SID, turn=1, seq=6, event="done",
                             data={"latency_ms": 9}))
        db.commit()
    finally:
        db.close()
    rest = [e for e in sub if e["event"] != "heartbeat"]
    assert [(e["event"], e["seq"]) for e in rest] == [("done", 6)]
