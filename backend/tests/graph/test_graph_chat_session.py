# ruff: noqa: F811  — pytest fixtures are imported by name from tests.test_visibility
"""E04 — the chat session lifecycle graph walked through the real routes (round 16, r16-o).

Graph: ``tests/graph/graphs/chat_session.yaml``. Hermetic: the runtime producer is the
scripted, gate-able ``Producer`` of ``tests/property/test_chat_concurrency.py`` (r16-e), so a
turn can be held LIVE while the next edge fires; the data-plane stop is a recorder that
answers ResourceNotFoundException once a session was stopped. Accounts: ada opens the
session (two "tabs" — the gated turn runs on its own client), bob is another member, the
administrator reads everything.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

import app.services.chat as chat_service
import app.services.invoke as invoke_service
from app.core.db import SessionLocal
from app.models.ledger import ChatMessage, ChatSession, ChatTurnEvent
from app.services import chat_turns, live_turns
from tests.graph import neighbourhood
from tests.graph.executor import GRAPHS, World, assert_tables_explicit, params_for
from tests.graph.walks import run_walk, walk_seeds
from tests.property.test_chat_concurrency import Producer, Script, _deltas
from tests.test_visibility import PEER, _active_agent, ada, admin, app, bob  # noqa: F401

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"
BUDGET_S = 10.0


class StopPlane:
    """StopRuntimeSession recorder: a session stopped once is gone on AWS afterwards."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def stop_runtime_session(self, **params):
        sid = params["runtimeSessionId"]
        if sid in self.calls:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
                "StopRuntimeSession",
            )
        self.calls.append(sid)
        return {"runtimeSessionId": sid, "statusCode": 200}


@pytest.fixture
def seams(monkeypatch):
    producer = Producer()
    plane = StopPlane()
    monkeypatch.setattr(chat_service, "invoke_agent_events", producer)
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: plane)
    # the registry seam (N16-obs89d): every detached turn this test starts is recorded, so a
    # wait is a join on that turn's driver — never a poll of the process's thread list
    real_start = live_turns.LiveTurnRegistry.start

    def recording_start(registry, live, target, args, **kw):
        started = real_start(registry, live, target, args, **kw)
        producer.record(started)
        return started

    monkeypatch.setattr(live_turns.LiveTurnRegistry, "start", recording_start)
    # the `/v1` sync seam sits BELOW the shared chain (the data-plane wrapper), so the
    # route runs the real `invoke_agent_text` — its gates included (N16-obs88 made the
    # ended-session 409 one of them); the stub only answers the runtime call
    monkeypatch.setattr(
        invoke_service.rt, "invoke_runtime_text",
        lambda _client, _arn, prompt, *, session_id, **_kw: {
            "text": f"v1 says {prompt}", "session_id": session_id,
        },
    )
    return producer, plane


def _frames(text: str) -> list[tuple[str, int | None, dict]]:
    """``(event, id, data)`` per SSE frame, heartbeats included."""
    import json

    out = []
    text = text.replace("\r\n", "\n").replace("\r", "\n")  # whole text first (rev-r17)
    for frame in text.split("\n\n"):
        kind, seq, data = None, None, None
        for line in frame.split("\n"):  # SSE: LF only (N17-obs52)
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("id:"):
                seq = int(line[3:].strip())
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if kind:
            out.append((kind, seq, data or {}))
    return out


def _event_count(agent_id: str, sid: str, turn: int) -> int:
    db = SessionLocal()
    try:
        return (
            db.query(ChatTurnEvent)
            .filter(ChatTurnEvent.agent_id == agent_id, ChatTurnEvent.session_id == sid,
                    ChatTurnEvent.turn == turn)
            .count()
        )
    finally:
        db.close()


def _session(agent_id: str, sid: str) -> ChatSession | None:
    db = SessionLocal()
    try:
        row = (db.query(ChatSession)
               .filter(ChatSession.agent_id == agent_id, ChatSession.session_id == sid).first())
        if row is not None:
            db.expunge(row)
        return row
    finally:
        db.close()


def _messages(sid: str) -> int:
    db = SessionLocal()
    try:
        return db.query(ChatMessage).filter(ChatMessage.session_id == sid).count()
    finally:
        db.close()


class ChatWorld(World):
    graph = "chat_session"
    UNDRIVEN: dict[str, str] = {}
    XFAIL: dict[str, str] = {}  # N16-obs88 fixed in cn/r17-a: `v1_send_ended` is covered
    FIXED: dict[str, list[str]] = {"N16-obs88": ["v1_send_ended"]}  # neighbourhood keeps it

    def __init__(self, clients, agent_id: str, producer: Producer, plane: StopPlane,
                 tab: TestClient, api_key: str) -> None:
        super().__init__(clients)
        self.agent_id = agent_id
        self.producer, self.plane, self.tab, self.api_key = producer, plane, tab, api_key
        self.sid = uuid.uuid4().hex + "ab" * 4
        self.turn = 0
        self.post_frames: dict[int, list] = {}
        self.gate: threading.Event | None = None
        self.thread: threading.Thread | None = None
        self.box: dict = {}
        self.ended_at: str | None = None

    @property
    def base(self) -> str:
        return f"/api/chat/{self.agent_id}"

    def post(self, client: TestClient, sid: str | None = None):
        return client.post(self.base, json={"prompt": f"p{self.turn + 1}",
                                            "session_id": sid or self.sid})

    # -- the live turn --------------------------------------------------------------
    def _send_gated(self, edge) -> None:
        gate = threading.Event()
        self.producer.queue.append(Script(_deltas(2, "h"), _deltas(2, "t"), gate=gate))
        box: dict = {}

        def go():
            res = self.tab.post(self.base, json={"prompt": f"p{self.turn + 1}",
                                                 "session_id": self.sid})
            box["status"], box["text"] = res.status_code, res.text

        thread = threading.Thread(target=go, name="graph-chat-tab", daemon=True)
        thread.start()
        deadline = time.monotonic() + BUDGET_S
        while time.monotonic() < deadline:
            live = chat_turns.live_run(self.agent_id, self.sid)
            if live is not None and live.turn is not None:
                break
            if "status" in box:  # refused before it went live
                break
            time.sleep(0.005)
        else:
            raise AssertionError("the gated turn never became live")
        if "status" in box:
            raise AssertionError(("the gated turn was refused", box))
        self.gate, self.thread, self.box = gate, thread, box
        self.turn += 1
        # the producer persists the turn's `meta` event (chat_turn_events seq 1) a moment
        # AFTER the run is live; a refusal edge right after this one snapshots the ledger,
        # so wait for that write too or the snapshot blames the refusal for it (seen under
        # host load in the r16-q coverage run: `send_while_live wrote chat_turn_events`)
        while time.monotonic() < deadline:
            if _event_count(self.agent_id, self.sid, self.turn) >= 1:
                break
            time.sleep(0.005)
        else:
            raise AssertionError("the live turn's meta event was never persisted")
        row = _session(self.agent_id, self.sid)
        assert row is not None and row.active_turn == self.turn, (edge.id, row and row.active_turn)
        self.ids.add(self.sid)

    edge_first_turn = _send_gated
    edge_next_turn = _send_gated
    edge_console_turn_after_v1 = _send_gated

    def edge_turn_completes(self, edge) -> None:
        assert self.gate is not None and self.thread is not None
        self.gate.set()
        self.thread.join(timeout=BUDGET_S)
        assert not self.thread.is_alive(), "the tab thread is stuck"
        assert self.box["status"] == 200, self.box
        frames = _frames(self.box["text"])
        meta = next(d for k, _s, d in frames if k == "meta")
        assert meta["turn"] == self.turn, (meta, self.turn)
        assert frames[-1][0] == "done", [k for k, _s, _d in frames]
        self.post_frames[self.turn] = [(k, s, d) for k, s, d in frames if k != "heartbeat"]
        self.producer.wait_finished(self.agent_id, self.sid)
        row = _session(self.agent_id, self.sid)
        assert row is not None and row.active_turn is None and row.turns == self.turn
        history = self.ada.get(f"{self.base}/history", params={"session_id": self.sid}).json()
        assert len(history["messages"]) >= 2 * self.turn, history
        self.gate = self.thread = None

    def edge_send_while_live(self, edge) -> None:
        body = self.expect(self.post(self.ada), edge)
        assert body["detail"]["active_turn"] == self.turn

    def edge_invalid_session_id(self, edge) -> None:
        self.expect(self.post(self.ada, sid="x" * 20), edge)

    # -- reattach --------------------------------------------------------------------
    def edge_reattach_open(self, edge) -> None:
        res = self.ada.get(f"{self.base}/sessions/{self.sid}/turns/{self.turn}/stream",
                           params={"from": 0})
        self.expect(res, edge)
        replay = [(k, s, d) for k, s, d in _frames(res.text) if k != "heartbeat"]
        assert replay[-1][0] == "done"
        # §8.3 — the replay is the persisted log; it carries the same seq-stamped frames the
        # POST streamed (differential oracle, heartbeats aside)
        posted = self.post_frames[self.turn]
        assert [(k, s) for k, s, _d in replay] == [(k, s) for k, s, _d in posted]

    def edge_reattach_unknown_turn(self, edge) -> None:
        res = self.ada.get(f"{self.base}/sessions/{self.sid}/turns/{self.turn + 5}/stream")
        self.expect(res, edge)

    def edge_admin_reattach(self, edge) -> None:
        res = self.admin.get(f"{self.base}/sessions/{self.sid}/turns/1/stream")
        body = self.expect(res, edge)
        assert body["detail"]["author"] == "ada"

    # -- the other entrance ------------------------------------------------------------
    def edge_v1_turn(self, edge) -> None:
        before = _session(self.agent_id, self.sid).turns
        res = self.ada.post(f"/v1/agents/{self.agent_id}/invoke",
                            json={"prompt": "via v1", "session_id": self.sid},
                            headers={"X-Api-Key": self.api_key})
        body = self.expect(res, edge)
        assert body["session_id"] == self.sid
        self.turn += 1
        row = _session(self.agent_id, self.sid)
        assert row.turns == before + 1, (row.turns, before)
        listed = self.ada.get(f"{self.base}/sessions").json()["sessions"]
        mine = [s for s in listed if s["session_id"] == self.sid]
        assert mine and mine[0]["turns"] == row.turns  # one row, both entrances counted

    edge_v1_turn_again = edge_v1_turn

    def edge_reattach_v1_turn(self, edge) -> None:
        """AT_8_3_V1 (N16-obs112, docs ruling in cn/r16-q): a reattach to the turn the /v1
        sync invoke made answers 200 with exactly one bare `done {"turn": n}` frame — no
        `meta`, no `delta` — and never tails (bounded by BUDGET_S)."""
        box: dict = {}

        def go():
            res = self.tab.get(f"{self.base}/sessions/{self.sid}/turns/{self.turn}/stream",
                               params={"from": 0})
            box["status"], box["text"] = res.status_code, res.text

        thread = threading.Thread(target=go, name="graph-chat-reattach-v1", daemon=True)
        thread.start()
        thread.join(timeout=BUDGET_S)
        assert "status" in box, (
            f"reattach to the /v1 turn {self.turn} gave no response within {BUDGET_S:.0f} s "
            "(the SSE tails a turn that will never end)")
        self.note(f"reattach_v1_turn → {box['status']} {box['text'][:160]!r}")
        assert box["status"] == edge.expect["status"], (box["status"], box["text"][:300])
        frames = [(k, d) for k, _s, d in _frames(box["text"]) if k != "heartbeat"]
        assert [k for k, _d in frames] == edge.expect["frames"], frames
        assert frames[0][1] == {"turn": self.turn}, frames

    def edge_v1_send_ended(self, edge) -> None:
        """N16-obs88 (fixed in cn/r17-a): the `/v1` entrance answers the console's envelope
        — same code, same `{session_id, ended_at}` detail — and appends no message; the
        executor's per-table digest proves the refusal wrote nothing at all."""
        before, turns = _messages(self.sid), _session(self.agent_id, self.sid).turns
        res = self.ada.post(f"/v1/agents/{self.agent_id}/invoke",
                            json={"prompt": "via v1 after stop", "session_id": self.sid},
                            headers={"X-Api-Key": self.api_key})
        body = self.expect(res, edge)
        assert body["detail"] == {"session_id": self.sid, "ended_at": self.ended_at}, body
        row = _session(self.agent_id, self.sid)
        assert row.ended_at is not None and row.turns == turns
        assert _messages(self.sid) == before

    # -- end session ---------------------------------------------------------------------
    def edge_stop_open(self, edge) -> None:
        self._stop_ok(edge)

    def edge_stop_after_v1(self, edge) -> None:
        self._stop_ok(edge)

    def _stop_ok(self, edge) -> None:
        body = self.expect(self.ada.post(f"{self.base}/sessions/{self.sid}/stop"), edge)
        assert body["ended"] is True and body["already_ended"] is False
        assert body["ended_at"] and self.plane.calls[-1] == self.sid
        self.ended_at = body["ended_at"]
        assert _session(self.agent_id, self.sid).ended_at is not None

    def edge_stop_ended(self, edge) -> None:
        body = self.expect(self.ada.post(f"{self.base}/sessions/{self.sid}/stop"), edge)
        assert body["already_ended"] is True and body["ended"] is True
        assert body["ended_at"] == self.ended_at

    def edge_send_ended(self, edge) -> None:
        before = _messages(self.sid)
        body = self.expect(self.post(self.ada), edge)
        assert body["detail"]["session_id"] == self.sid and body["detail"]["ended_at"]
        assert _messages(self.sid) == before

    def edge_history_ended(self, edge) -> None:
        res = self.ada.get(f"{self.base}/history", params={"session_id": self.sid})
        body = self.expect(res, edge)
        assert len(body["messages"]) == _messages(self.sid) >= 2

    # -- other accounts ---------------------------------------------------------------------
    def edge_other_member_history(self, edge) -> None:
        self.expect(self.bob.get(f"{self.base}/history", params={"session_id": self.sid}), edge)

    def edge_other_member_stop(self, edge) -> None:
        calls = len(self.plane.calls)
        self.expect(self.bob.post(f"{self.base}/sessions/{self.sid}/stop"), edge)
        assert len(self.plane.calls) == calls and _session(self.agent_id, self.sid).ended_at is None

    def edge_other_member_post(self, edge) -> None:
        self.expect(self.post(self.bob), edge)

    def edge_other_member_reattach(self, edge) -> None:
        self.expect(self.bob.get(f"{self.base}/sessions/{self.sid}/turns/1/stream"), edge)

    def close(self) -> None:
        if self.gate is not None:
            self.gate.set()
        if self.thread is not None:
            self.thread.join(timeout=BUDGET_S)
        self.producer.wait_all_finished()


@pytest.fixture
def make_world(app, admin, ada, bob, seams):
    """A fresh world per call: its own agent, tab and /v1 key (the walk shrinker replays on
    fresh worlds)."""
    ada_id, ada_client = ada
    _bob_id, bob_client = bob
    producer, plane = seams

    def make() -> ChatWorld:
        tab = TestClient(app, client=PEER)
        tab.cookies = ada_client.cookies
        tag = uuid.uuid4().hex[:6]
        key = ada_client.post("/api/apikeys", json={"name": f"graph-v1-{tag}"}).json()["key"]
        agent_id = _active_agent(f"g-chat-{tag}", created_by=ada_id)
        return ChatWorld({"admin": admin, "ada": ada_client, "bob": bob_client}, agent_id,
                         producer, plane, tab, key)

    return make


@pytest.fixture
def world(make_world) -> ChatWorld:
    return make_world()


def test_every_edge_is_driven_or_declared():
    assert_tables_explicit(ChatWorld)


def _settle(world: ChatWorld) -> None:
    """Never leave a gated turn behind for the next test of the worker."""
    world.close()


@pytest.mark.parametrize("path", params_for(ChatWorld))
def test_chat_session_path(world, path):
    try:
        world.run(path)
    finally:
        _settle(world)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize("path", params_for(ChatWorld, include_covered_pairs=True))
def test_soak_chat_session_every_edge_pair(world, path):
    try:
        world.run(path)
    finally:
        _settle(world)



@pytest.mark.parametrize("seed", walk_seeds())
def test_chat_session_walk(make_world, seed):
    """Seeded random walk (20–60 edges) through every enabled edge of the graph — refusal
    loops included — on a fresh world; a failure is shrunk on fresh worlds and reported
    with its minimal edge sequence + repro-hash (``tests/graph/walks.py``)."""
    run_walk(make_world, ChatWorld, GRAPHS["chat_session"], seed)


@pytest.mark.parametrize("path", neighbourhood.params_for(ChatWorld, GRAPHS))
def test_chat_session_neighbourhood(world, path):
    """Loop (a): every edge pair within distance 2 of a pinned (register) edge, every 3-edge
    window through it and every prime path containing it, as its own test id
    (``tests/graph/neighbourhood.py``) — strict xfail while the pin stands."""
    try:
        world.run(path)
    finally:
        _settle(world)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize(
    "path", neighbourhood.params_for(ChatWorld, GRAPHS, neighbourhood.SOAK_KINDS))
def test_soak_chat_session_neighbourhood_pairs(world, path):
    """The distance-2 edge pairs around every pinned edge (re-runs of enumerated paths)."""
    try:
        world.run(path)
    finally:
        _settle(world)
