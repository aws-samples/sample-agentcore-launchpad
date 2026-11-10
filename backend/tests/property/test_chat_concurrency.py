"""E04 — two clients on one Chat session (round 16, channel A, `tests/property`).

Property / model tests of the detached chat turn against
``docs/cn/interfaces/assistant-turns.md`` §8 (Chat playground parity). Every invariant
names the clause it enforces; none copies a threshold from the code.

* §8.2 *Key and claim* — "One running turn per session": a second POST while a turn is
  live answers ``409 chat.turn_in_progress {active_turn}``.
* §8.2 *Producer* — "Per event the driver runs the transcript reducer **first**, then
  publishes, then logs — so 'event in the log ⇒ its rows exist'".
* §4 / §8.2 *Event log* — ``seq`` is "contiguous from 1", unique
  ``(agent_id, session_id, turn, seq)``.
* §8.3 — reattach "replays ``seq > from`` (default 0), tails the live producer … then the
  terminal event"; ``404 chat.turn_not_found``; stop route: "only the runtime session is
  terminated" (the ledger row and the live turn are not).
* §8.2 *Sweep* — "If the turn's log has no terminal event, the log is replayed through the
  reducer with persistence off, then the open bubble … and an ``error`` row
  ``interrupted: …`` are written, and a terminal ``error {"message": "interrupted"}`` event
  is appended. A claim whose log already ended is only cleared. Never resumed."
* §8.5 — "a subscriber leaving mid-turn → rows identical to a watched turn"; §8.4 — "the
  replay from seq 0 builds the rows exactly as a just-sent turn does" (history == log).

Hermetic: ``invoke_agent_events`` is a scripted producer (held open with a gate so the other
client can act mid-turn; the AgentCore SDK's REAL terminal error frame — the
``tests/faults`` ``Fault.frame()`` shape — is raised exactly where ``_runtime_payload_events``
raises it); the stop route's data plane is a recorder. ``LAUNCHPAD_SOAK=1`` runs the slow
variants with 10× the examples / steps.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, Phase, assume, given, note, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

import app.services.chat as chat_service
import app.services.invoke as invoke_service
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import ChatMessage, ChatSession, ChatTurnEvent
from app.services import chat_turns, live_turns
from app.services.agentcore import runtime as rt
from tests.faults.conftest import Fault, _bedrock_5xx, _read_timeout, _throttle
from tests.test_chat_api import make_active_agent
from tests.test_chat_async_turns import _frames, _log, _rows, _session

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"
TRACE = os.environ.get("LAUNCHPAD_CHAT_PROP_TRACE") == "1"
SCALE = 10 if SOAK else 1
EXAMPLES = 5 * SCALE
STEPS = 15 * SCALE
# How long a wait on a turn may take before the harness calls it a hang. Every wait on a
# detached turn is a ``Thread.join`` on ITS driver (deterministic: it returns the instant the
# turn is over), so the budget is reached only on a real hang and may be generous;
# ``LAUNCHPAD_TEST_BUDGET_SCALE`` (default 1) stretches it on a saturated host (N16-obs89d).
BUDGET_SCALE = float(os.environ.get("LAUNCHPAD_TEST_BUDGET_SCALE", "1") or 1)
BUDGET_S = 20.0 * BUDGET_SCALE
# a gated Script releases ITSELF after this, so a gate the harness forgot leaks no thread
# past the test; it must outlast BUDGET_S so the wait reports the harness bug first
GATE_VALVE_S = 1.5 * BUDGET_S
# a program example costs ~1 s of real HTTP + threads: shrinking a failure would eat the
# gate's budget (Hypothesis caps it at 5 min) — the failing program is printed instead
NO_SHRINK = (Phase.explicit, Phase.reuse, Phase.generate, Phase.target)

TERMINAL = set(chat_turns.TERMINAL_EVENTS)
INTERRUPTED_ROW = chat_turns.INTERRUPTED_TEXT  # the §8.2 sweep's error row text


# ---------------------------------------------------------------------------
# the scripted producer (one per test), the stop data plane, small helpers
# ---------------------------------------------------------------------------


class Script:
    """One producer run: ``head`` events, optionally wait for ``gate``, ``tail`` events,
    then either end normally or raise the SDK terminal error frame as the runtime
    wrapper does (``rt.sdk_stream_error`` — the ``_runtime_payload_events`` raise)."""

    def __init__(self, head: list[dict], tail: list[dict] | None = None, *,
                 gate: threading.Event | None = None, fault: dict | None = None) -> None:
        self.head, self.tail, self.gate, self.fault = head, list(tail or []), gate, fault

    def run(self):
        yield from self.head
        if self.gate is not None:
            self.gate.wait(timeout=GATE_VALVE_S)
        yield from self.tail
        if self.fault is not None:
            raise rt.sdk_stream_error(self.fault)


class Producer:
    """``invoke_agent_events`` stand-in: scripts are consumed in order; a call without a
    queued script gets ``DEFAULT``. Records every (session_id, prompt) it served, and (through
    the ``producer`` fixture's registry seam) every detached turn the test started, so a wait
    for a turn to finish is a join on its driver thread — never a poll of the process's
    thread list (N16-obs89d: a turn held open on purpose on ANOTHER session is not a hang)."""

    DEFAULT = [
        {"event": "delta", "data": {"text": "Looking that up. "}},
        {"event": "tool", "data": {"name": "search", "id": "t-1"}},
        {"event": "delta", "data": {"text": "The answer "}},
        {"event": "delta", "data": {"text": "is 42."}},
    ]

    def __init__(self) -> None:
        self.queue: list[Script] = []
        self.calls: list[tuple[str, str]] = []
        self.turns: list[chat_turns.ChatLiveTurn] = []  # every turn started in this test
        self.lock = threading.Lock()

    def __call__(self, agent, prompt, session_id=None, actor_id="river", **_kw):
        with self.lock:
            self.calls.append((session_id, prompt))
            script = self.queue.pop(0) if self.queue else Script(list(self.DEFAULT))
        return script.run()

    def record(self, live: live_turns.LiveTurn) -> None:
        if isinstance(live, chat_turns.ChatLiveTurn):
            with self.lock:
                self.turns.append(live)

    def _join(self, turns: list[chat_turns.ChatLiveTurn], deadline: float, what: str) -> None:
        for live in turns:
            thread = live.thread
            if thread is None or thread is threading.current_thread():
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            assert not thread.is_alive(), (
                f"{what} did not finish within the budget ({BUDGET_S:.0f}s): "
                f"turn {live.turn} of session {live.session_id[:8]} ({thread.name}) is still "
                f"running — set LAUNCHPAD_TEST_BUDGET_SCALE on a saturated host, or a gate "
                f"was never released")

    def wait_finished(self, agent_id: str, session_id: str) -> None:
        """Block until every turn started on ``(agent_id, session_id)`` has ended for good:
        its driver thread has exited (terminal event durable, claim released, registry entry
        dropped). A join, so it returns the instant the turn is over; the budget is reached
        only on a real hang. A turn still live on another session is NOT waited for."""
        deadline = time.monotonic() + BUDGET_S
        with self.lock:
            mine = [t for t in self.turns
                    if t.agent_id == agent_id and t.session_id == session_id]
        live = chat_turns.live_run(agent_id, session_id)
        if live is not None and live not in mine:  # registered before the seam recorded it
            mine.append(live)
        self._join(mine, deadline, "a detached chat turn")
        assert chat_turns.live_run(agent_id, session_id) is None

    def wait_all_finished(self) -> None:
        """Teardown: nothing THIS test started is still running (no driver leaks into the
        next test of the worker — the gates are set by the caller first)."""
        deadline = time.monotonic() + BUDGET_S
        with self.lock:
            turns = list(self.turns)
        self._join(turns, deadline, "a detached chat turn of this test")


class StopPlane:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def stop_runtime_session(self, **params):
        self.calls.append(params)
        return {"runtimeSessionId": params["runtimeSessionId"], "statusCode": 200}


def _sid() -> str:
    return uuid.uuid4().hex + "ab" * 4  # 40 chars, the SID shape the suite uses


def _deltas(n: int, tag: str) -> list[dict]:
    return [{"event": "delta", "data": {"text": f"{tag}{i} "}} for i in range(n)]


def _history(client: TestClient, agent_id: str, sid: str) -> dict:
    res = client.get(f"/api/chat/{agent_id}/history", params={"session_id": sid})
    assert res.status_code == 200, res.text
    return res.json()


def _reattach(client: TestClient, agent_id: str, sid: str, turn: int, after: int):
    return client.get(f"/api/chat/{agent_id}/sessions/{sid}/turns/{turn}/stream",
                      params={"from": after})


def _non_hb(frames):
    return [(k, d, s) for k, d, s in frames if k != "heartbeat"]


def _replay_rows(prompt: str, log: list[tuple[str, int, dict]]) -> list[tuple[str, str, Any]]:
    """The rows the §8.2 reducer builds from a turn's log alone (the user row's text is
    the request's prompt, which the log does not carry)."""
    out: list[tuple[str, str, Any]] = []

    def save(_ws, _aid, _sid, role, text, **kw):
        out.append((role, text, kw.get("name")))

    rec = chat_turns.TranscriptRecorder(DEFAULT_WORKSPACE_ID, "a", prompt=prompt,
                                        actor_id="river", attachments=None,
                                        runtime_version=None, save=save,
                                        track=lambda *a, **k: None)
    for event, _seq, data in log:
        rec.feed({"event": event, "data": data or {}})
    return out


def _claim(agent_id: str, sid: str) -> int | None:
    row = _session(agent_id, sid)
    return None if row is None else row.active_turn


@pytest.fixture
def producer(monkeypatch, client) -> Producer:
    p = Producer()
    monkeypatch.setattr(chat_service, "invoke_agent_events", p)
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: StopPlane())
    # the registry seam: every detached turn started while this test runs is recorded, so
    # the waits below join exactly the drivers this test owns (``Producer.wait_finished``)
    real_start = live_turns.LiveTurnRegistry.start

    def recording_start(registry, live, target, args, **kw):
        started = real_start(registry, live, target, args, **kw)
        p.record(started)
        return started

    monkeypatch.setattr(live_turns.LiveTurnRegistry, "start", recording_start)
    return p


# ---------------------------------------------------------------------------
# the session world: one agent, one session, two clients, a program of ops
# ---------------------------------------------------------------------------


class World:
    """One agent + one session driven by two console clients (same TestClient, two
    'tabs'). Tracks what the model expects: the prompts per turn, the frames every POST
    and reattach saw, the claim values observed while turns ran."""

    def __init__(self, client: TestClient, producer: Producer, agent_id: str) -> None:
        self.client, self.producer, self.agent_id = client, producer, agent_id
        # one TestClient per tab: a TestClient serialises its requests through one
        # portal, so two tabs on one client would never overlap (the race would be fake)
        self.tabs = {"A": TestClient(client.app), "B": TestClient(client.app)}
        self.sid = _sid()
        self.prompts: dict[int, str] = {}
        self.post_frames: dict[int, list] = {}
        self.statuses: list[int] = []
        self.refusals: list[tuple] = []
        self.claims_seen: set[int | None] = set()
        self.threads: list[threading.Thread] = []
        self.gates: list[threading.Event] = []
        self.open_turn: bool = False  # the model: a gated turn is live
        self.reattach_results: list[tuple[int, int, list, bool]] = []  # + live at request
        self.stop_results: list[dict] = []
        self.stopped_during_turn = False
        # N16-obs79: once END SESSION has stamped the row, a send carrying THIS session id
        # is 409 ``chat.session_ended`` and no turn is ever appended again; only a turn
        # already live at the stop still finishes. The reopen path is a NEW session id.
        self.ended: bool = False
        self.turns_allowed: int | None = None  # turns the ledger may hold after the stop
        self.fresh_sessions: list[str] = []

    # -- the model's answer to a send on this session ------------------------
    def expected_status(self) -> int:
        return 409 if (self.open_turn or self.ended) else 200

    def expected_refusal(self) -> str | None:
        if self.open_turn:
            return "chat.turn_in_progress"  # §8.2 wins while a turn is live
        return "chat.session_ended" if self.ended else None

    def send_checked(self, tag: str) -> int:
        """A plain send asserted against the model (status AND refusal code)."""
        expected, code = self.expected_status(), self.expected_refusal()
        status = self.send(tag)
        assert status == expected, (status, expected, self.refusals[-1:] if status != 200 else None)
        if status == 409:
            assert self.refusals[-1][1] == code, self.refusals[-1]
        return status

    def fresh_session_send(self, tag: str = "A") -> None:
        """N16-obs79: the console's way after END SESSION — the next prompt opens a NEW
        session. Always 200; the ended row keeps ``ended_at`` and gains no turn."""
        sid = _sid()
        res = self.tabs[tag].post(f"/api/chat/{self.agent_id}",
                                  json={"prompt": f"{tag}-fresh-{len(self.fresh_sessions)}",
                                        "session_id": sid})
        assert res.status_code == 200, res.text
        self.fresh_sessions.append(sid)
        # this turn only: a gated turn held open on the model's own session is live by
        # design, not a hang (N16-obs89d)
        self.producer.wait_finished(self.agent_id, sid)
        fresh = _session(self.agent_id, sid)
        assert fresh is not None and fresh.turns == 1 and fresh.ended_at is None
        if self.ended:
            row = _session(self.agent_id, self.sid)
            assert row is not None and row.ended_at is not None

    # -- requests -----------------------------------------------------------
    def _post(self, prompt: str, tab: str = "A") -> tuple[int, list]:
        res = self.tabs[tab].post(f"/api/chat/{self.agent_id}",
                                  json={"prompt": prompt, "session_id": self.sid})
        frames = _frames(res.text) if res.status_code == 200 else []
        if res.status_code != 200:  # the refusal's code, for the diagnosis of a wrong status
            self.refusals.append((res.status_code, res.json().get("code"), tab, prompt,
                                  str(res.request.url), res.text[:300]))
        return res.status_code, frames

    def _record_post(self, status: int, frames: list, prompt: str) -> None:
        self.statuses.append(status)
        if status == 200:
            meta = next(d for k, d, _s in frames if k == "meta")
            self.prompts[meta["turn"]] = prompt
            self.post_frames[meta["turn"]] = frames

    def send(self, tag: str) -> int:
        prompt = f"{tag}-{len(self.statuses)}"
        status, frames = self._post(prompt, tag)
        self._record_post(status, frames, prompt)
        return status

    def send_gated(self, tag: str, head: int, tail: int) -> threading.Event:
        gate = threading.Event()
        self.producer.queue.append(Script(_deltas(head, "h"), _deltas(tail, "t"), gate=gate))
        prompt = f"{tag}-gated-{len(self.statuses)}"
        done = threading.Event()
        box: dict[str, Any] = {}

        def go():
            box["status"], box["frames"] = self._post(prompt, tag)
            done.set()

        t = threading.Thread(target=go, name="tab-send-gated", daemon=True)
        t.start()
        # the turn is live once the registry knows it (meta published → turn number)
        deadline = time.monotonic() + BUDGET_S
        while time.monotonic() < deadline:
            live = chat_turns.live_run(self.agent_id, self.sid)
            if live is not None and live.turn is not None:
                break
            time.sleep(0.005)
        else:
            raise AssertionError("the gated turn never became live")
        self.claims_seen.add(_claim(self.agent_id, self.sid))
        self.gates.append(gate)
        self.threads.append(t)
        self.open_turn = True
        t.finisher = (done, box, prompt)  # type: ignore[attr-defined]
        return gate

    def release(self) -> None:
        for gate in self.gates:
            gate.set()
        self.gates.clear()
        for t in self.threads:
            t.join(timeout=BUDGET_S)
            assert not t.is_alive(), "a tab thread is stuck"
            fin = getattr(t, "finisher", None)
            if fin is not None:
                done, box, prompt = fin
                assert done.is_set()
                self._record_post(box["status"], box["frames"], prompt)
        self.threads.clear()
        self.producer.wait_finished(self.agent_id, self.sid)
        self.open_turn = False

    def race_send(self) -> list[int]:
        """Two tabs POST at the same instant; the producer of whichever wins is held open
        until the loser has answered, so the two requests really overlap (a scripted
        producer would otherwise finish before the second tab checked capacity)."""
        barrier = threading.Barrier(2)
        gate = threading.Event()
        self.producer.queue.append(Script(_deltas(1, "r"), _deltas(1, "r"), gate=gate))
        out: list[tuple[int, list, str]] = []
        lock = threading.Lock()

        def go(tag):
            prompt = f"{tag}-race-{len(self.statuses)}"
            barrier.wait(timeout=10)
            status, frames = self._post(prompt, tag)
            with lock:
                out.append((status, frames, prompt))
            if status == 409:
                gate.set()  # the loser answered: let the winner finish

        ts = [threading.Thread(target=go, args=(tag,), daemon=True) for tag in ("A", "B")]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=BUDGET_S)
            if t.is_alive():
                gate.set()
                t.join(timeout=BUDGET_S)
            assert not t.is_alive()
        gate.set()
        for status, frames, prompt in out:
            self._record_post(status, frames, prompt)
        self.producer.queue = [s for s in self.producer.queue if s.gate is not gate]
        self.producer.wait_finished(self.agent_id, self.sid)
        return sorted(s for s, _f, _p in out)

    def reconnect(self, after: int) -> None:
        if not self.prompts and not self.open_turn:
            return
        turn = max(self.prompts) if self.prompts else 1
        live = chat_turns.live_run(self.agent_id, self.sid)
        if live is not None and live.turn is not None:
            turn = live.turn
        if self.open_turn:
            box: dict[str, Any] = {}

            def go():
                res = _reattach(self.tabs["B"], self.agent_id, self.sid, turn, after)
                box["res"] = res
                self.reattach_results.append((turn, after, _frames(res.text), True))

            t = threading.Thread(target=go, name="tab-reattach", daemon=True)
            t.start()
            self.threads.append(t)
            return
        res = _reattach(self.client, self.agent_id, self.sid, turn, after)
        assert res.status_code == 200, res.text
        self.reattach_results.append((turn, after, _frames(res.text), False))

    def abort_subscription(self) -> None:
        """A tab that subscribes to the live turn and leaves before the end: the
        subscription ends, the turn does not (§8.4: "an unmount aborts the subscription
        only"). Exercised at the ``LiveTurn.subscribe`` seam — Starlette's TestClient
        drains a streamed response instead of sending ``http.disconnect``, so a closed
        ``client.stream`` would wait for the turn; the ASGI disconnect itself is pinned by
        ``tests/test_assistant_residuals.py``."""
        live = chat_turns.live_run(self.agent_id, self.sid)
        if live is None:
            return
        sub = live.subscribe(0)
        next(sub)
        sub.close()
        assert chat_turns.live_run(self.agent_id, self.sid) is live  # still running

    def refresh(self) -> None:
        if _session(self.agent_id, self.sid) is None:
            return
        body = _history(self.client, self.agent_id, self.sid)
        live = body["live_turn"]
        if self.open_turn:
            assert live is not None and live["state"] in ("running", "finishing"), live
            self.claims_seen.add(_claim(self.agent_id, self.sid))
        else:
            assert live is None, live

    def stop(self) -> None:
        if _session(self.agent_id, self.sid) is None:
            return
        res = self.client.post(f"/api/chat/{self.agent_id}/sessions/{self.sid}/stop")
        assert res.status_code == 200, res.text
        self.stop_results.append(res.json())
        if self.open_turn:
            self.stopped_during_turn = True
        if not self.ended:
            # the live turn (if any) still finishes and is recorded on release; nothing after
            self.ended = True
            self.turns_allowed = len(self.prompts) + (1 if self.open_turn else 0)

    # -- the invariants (checked once everything is released) --------------
    def check(self) -> None:
        assert not self.open_turn
        row = _session(self.agent_id, self.sid)
        if not self.prompts:
            assert row is None or row.turns == 0
            return
        turns = sorted(self.prompts)
        # §8.2 claim: one turn per session; the claim is released after the terminal event
        assert row is not None and row.active_turn is None and row.active_turn_token is None
        assert row.turns == len(turns) == self.statuses.count(200)
        assert turns == list(range(1, len(turns) + 1)), turns
        assert all(c is None or c in turns for c in self.claims_seen), self.claims_seen
        all_deltas: list[str] = []
        for turn in turns:
            log = _log(self.agent_id, self.sid, turn)
            seqs = [s for _e, s, _d in log]
            # §4 / §8.2: seq contiguous from 1, unique
            assert seqs == list(range(1, len(seqs) + 1)), (turn, seqs)
            kinds = [e for e, _s, _d in log]
            assert kinds[0] == "meta" and kinds[-1] in TERMINAL, kinds
            assert sum(k in TERMINAL for k in kinds) == 1, kinds  # exactly one terminal
            # the POST's frames == the log (ids are the seqs)
            framed = [(k, s) for k, _d, s in _non_hb(self.post_frames[turn])]
            assert framed == [(e, s) for e, s, _d in log], (turn, framed, log)
            all_deltas.extend(d.get("text", "") for e, _s, d in log if e == "delta")
        # §8.5 history == replayed log (every turn, in order)
        expected: list[tuple[str, str, Any]] = []
        for turn in turns:
            expected.extend(_replay_rows(self.prompts[turn], _log(self.agent_id, self.sid, turn)))
        rows = [(r, t, n) for r, t, n, _tr in _rows(self.sid)]
        assert rows == expected, (rows, expected)
        history = _history(self.client, self.agent_id, self.sid)["messages"]
        assert [(m["role"], m["text"], m["name"]) for m in history] == expected
        # no duplicate / lost delta: every delta lands once in an agent row
        assert "".join(t for r, t, _n in rows if r == "agent") == "".join(all_deltas)
        # §8.3 reattach: exactly the events with seq > from, same terminal event
        for turn, after, frames, was_live in self.reattach_results:
            log = _log(self.agent_id, self.sid, turn)
            got = [(k, s) for k, _d, s in _non_hb(frames)]
            expected_tail = [(e, s) for e, s, _d in log if s > after]
            if expected_tail:
                assert got == expected_tail, (turn, after, got)
            else:
                # §8.3: a reattach from at / past the turn's end — live (N16-obs45, fixed in
                # live_turns.tail_turn) or finished — settles the client on a bare ``done``
                # (no ``id:`` — the event is not in the log and never will be)
                assert got == [("done", None)], (turn, after, got, was_live)
        # N16-obs79: an ended session never gained a turn after the stop, and the marker
        # outlived every later send on this id (each of them answered 409 session_ended)
        if self.ended:
            assert row.ended_at is not None
            assert len(self.prompts) == self.turns_allowed, (len(self.prompts), self.turns_allowed)
        # stop: "only the runtime session is terminated" — every turn still ended on its
        # own terminal event (checked above); the row carries ended_at from the stop
        if self.stop_results:
            assert all(r["ended"] for r in self.stop_results)


# ---------------------------------------------------------------------------
# 1. the two-client interleaving program
# ---------------------------------------------------------------------------

_OP = st.one_of(
    st.tuples(st.just("send"), st.sampled_from(["A", "B"])),
    st.tuples(st.just("send_gated"), st.sampled_from(["A", "B"]),
              st.integers(0, 3), st.integers(0, 3)),
    st.tuples(st.just("send_while_running"), st.sampled_from(["A", "B"])),
    st.tuples(st.just("race_send")),
    st.tuples(st.just("reconnect"), st.integers(0, 6)),
    st.tuples(st.just("refresh")),
    st.tuples(st.just("stop")),
    st.tuples(st.just("abort")),
    st.tuples(st.just("release")),
    st.tuples(st.just("fresh_send")),
)


def _run_program(client, producer, ops: list[tuple]) -> None:
    world = World(client, producer, make_active_agent(method="zip_runtime",
                                                       name=f"chat-prop-{uuid.uuid4().hex[:8]}"))
    try:
        # every program opens the session first, so no later op degenerates to a no-op
        for op in [("send", "A"), *ops]:
            kind = op[0]
            started = time.monotonic()
            if kind == "send":
                # §8.2: 409 chat.turn_in_progress while a turn is live; N16-obs79: 409
                # chat.session_ended once the session was stopped; 200 otherwise
                world.send_checked(op[1])
            elif kind == "send_gated":
                if world.open_turn or world.ended:
                    world.send_checked(op[1])
                else:
                    world.send_gated(op[1], op[2], op[3])
            elif kind == "send_while_running":
                if not world.open_turn and world.ended:
                    world.send_checked(op[1])  # no turn can start on the ended session
                    continue
                if not world.open_turn:
                    world.send_gated(op[1], 1, 1)
                res = world.tabs["B"].post(f"/api/chat/{world.agent_id}",
                                           json={"prompt": "second tab", "session_id": world.sid})
                assert res.status_code == 409 and res.json()["code"] == "chat.turn_in_progress"
                live = chat_turns.live_run(world.agent_id, world.sid)
                assert res.json()["detail"]["active_turn"] == live.turn
                world.statuses.append(409)
            elif kind == "race_send":
                if world.open_turn:
                    world.release()
                # exactly one of two simultaneous sends runs; the other is refused — on an
                # ended session both are refused (N16-obs79)
                assert world.race_send() == ([409, 409] if world.ended else [200, 409])
            elif kind == "fresh_send":
                world.fresh_session_send(op[1] if len(op) > 1 else "A")
            elif kind == "reconnect":
                world.reconnect(op[1])
            elif kind == "refresh":
                world.refresh()
            elif kind == "stop":
                world.stop()
            elif kind == "abort":
                world.abort_subscription()
            elif kind == "release":
                world.release()
            # no op may stall: a wait on a gate nobody releases is a harness bug
            if TRACE:
                print(f"[chat-prop] {op} {time.monotonic() - started:.2f}s", file=sys.stderr)
            assert time.monotonic() - started < BUDGET_S / 2, ("stalled op", op, ops)
        world.release()
        world.check()
    finally:
        for gate in world.gates:
            gate.set()
        producer.wait_all_finished()


@settings(deadline=None, max_examples=4 * SCALE, phases=NO_SHRINK,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(ops=st.lists(_OP, min_size=2, max_size=5))
def test_two_clients_interleave_send_stop_reconnect_refresh(client, producer, ops):
    """§8.2 one turn per session (409 for the second tab), §4 contiguous unique seq, §8.5
    history == replayed log, §8.3 reattach == the events after ``from`` ending on the same
    terminal event, stop ends only the runtime session — over a random program of two
    tabs sending / holding / reconnecting / refreshing / stopping / aborting."""
    _run_program(client, producer, ops)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@settings(deadline=None, max_examples=EXAMPLES * 4, phases=NO_SHRINK,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(ops=st.lists(_OP, min_size=4, max_size=20))
def test_soak_two_clients_interleave(client, producer, ops):
    _run_program(client, producer, ops)


# ---------------------------------------------------------------------------
# 2. a dead process's turn: the interrupted marker exactly once, sweep idempotent
# ---------------------------------------------------------------------------


def _seed_dead_turn(agent_id: str, sid: str, head: int, with_tool: bool, tail: int,
                    terminal: bool) -> list[tuple[str, dict]]:
    """A session a dead process left: the claim, the rows of every logged event (§8.2
    'event in the log ⇒ its rows exist'), the log, no terminal event unless asked."""
    events: list[tuple[str, dict]] = [("meta", {"session_id": sid, "agent": "a",
                                                "mode": "stream", "turn": 1, "author": "river"})]
    events += [("delta", {"text": f"h{i} "}) for i in range(head)]
    if with_tool:
        events.append(("tool", {"name": "search", "id": "t-1"}))
    events += [("delta", {"text": f"t{i} "}) for i in range(tail)]
    if terminal:
        events.append(("done", {"latency_ms": 1}))
    db = SessionLocal()
    try:
        db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, session_id=sid,
                           actor_id="river", turns=1, active_turn=1, active_turn_token="dead",
                           active_turn_started_at=datetime.now(UTC) - timedelta(seconds=120)))
        db.add(ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                           session_id=sid, role="user", text="compute"))
        if with_tool:
            if head:
                db.add(ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                                   session_id=sid, role="agent",
                                   text="".join(f"h{i} " for i in range(head))))
            db.add(ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                               session_id=sid, role="tool", text="", name="search"))
        if terminal:
            open_text = "".join(f"t{i} " for i in range(tail)) if with_tool else (
                "".join(f"h{i} " for i in range(head)) + "".join(f"t{i} " for i in range(tail)))
            if open_text:
                db.add(ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                                   session_id=sid, role="agent", text=open_text))
        for seq, (event, data) in enumerate(events, start=1):
            db.add(ChatTurnEvent(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                                 session_id=sid, turn=1, seq=seq, event=event, data=data))
        db.commit()
    finally:
        db.close()
    return events


@settings(deadline=None, max_examples=EXAMPLES,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(head=st.integers(0, 3), with_tool=st.booleans(), tail=st.integers(0, 3),
       terminal=st.booleans(), second_sweep_is_restart=st.booleans())
def test_interrupted_marker_exactly_once(client, head, with_tool, tail, terminal,
                                         second_sweep_is_restart):
    """§8.2 Sweep: a claim found at boot is finalized from its log — the open bubble, ONE
    ``interrupted: …`` error row and ONE terminal ``error {"message": "interrupted"}``
    event; "a claim whose log already ended is only cleared"; a second start finds nothing
    (idempotent); a reattach after the sweep ends at once on that terminal event."""
    agent_id = make_active_agent(method="zip_runtime", name=f"chat-dead-{uuid.uuid4().hex[:8]}")
    sid = _sid()
    events = _seed_dead_turn(agent_id, sid, head, with_tool, tail, terminal)
    rows_before = _rows(sid)

    create_app(resume_jobs=True)  # the restart
    if second_sweep_is_restart:
        create_app(resume_jobs=True)
    else:
        assert chat_turns.clear_stale_turn_claims() == 0

    row = _session(agent_id, sid)
    assert row.active_turn is None and row.active_turn_token is None
    log = _log(agent_id, sid, 1)
    rows = _rows(sid)
    if terminal:
        # the log had ended: only the claim goes — rows and log byte-identical
        assert [(e, s, d) for e, s, d in log] == [(e, i + 1, d) for i, (e, d) in enumerate(events)]
        assert rows == rows_before
        return
    kinds = [e for e, _s, _d in log]
    assert kinds[:len(events)] == [e for e, _d in events]
    assert kinds[len(events):] == ["error"], kinds
    assert log[-1][2] == {"message": "interrupted"} and log[-1][1] == len(events) + 1
    error_rows = [r for r in rows if r[0] == "error"]
    assert error_rows == [("error", INTERRUPTED_ROW, None, False)], rows
    # the open bubble (text after the last tool) is kept exactly once, before the marker
    open_text = "".join(f"t{i} " for i in range(tail)) if with_tool else (
        "".join(f"h{i} " for i in range(head)) + "".join(f"t{i} " for i in range(tail)))
    expected = list(rows_before)
    if open_text:
        expected.append(("agent", open_text, None, False))
    expected.append(("error", INTERRUPTED_ROW, None, False))
    assert rows == expected, (rows, expected)
    # §8.3: a reattach replays the log and ends on the appended terminal event, at once
    started = time.monotonic()
    res = _reattach(client, agent_id, sid, 1, 0)
    assert res.status_code == 200
    got = [(k, s) for k, _d, s in _non_hb(_frames(res.text))]
    assert got == [(e, s) for e, s, _d in log]
    assert time.monotonic() - started < chat_turns.live_turns.HEARTBEAT_S * 3
    # the next send on the session is a fresh turn 2
    with pytest.MonkeyPatch.context() as mp:
        producer = Producer()
        mp.setattr(chat_service, "invoke_agent_events", producer)
        res = client.post(f"/api/chat/{agent_id}", json={"prompt": "again", "session_id": sid})
    assert res.status_code == 200, res.text
    assert _frames(res.text)[0][1]["turn"] == 2


# ---------------------------------------------------------------------------
# 3. the SDK terminal error frame mid-stream
# ---------------------------------------------------------------------------

_FAULTS = st.sampled_from([
    _bedrock_5xx("frame"), _throttle("frame"), _read_timeout("frame"),
    Fault(name="validation", error_type="ValidationException",
          error="ValidationException: injected by tests/property"),
    Fault(name="empty-error", error_type="ContextWindowOverflowException", error=""),
])


@settings(deadline=None, max_examples=EXAMPLES,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(k=st.integers(0, 4), with_tool=st.booleans(), fault=_FAULTS)
def test_stream_fault_frame_ends_the_turn_once_and_keeps_the_partial_answer(
    client, producer, k, with_tool, fault,
):
    """§8.2 Producer — ``chat_stream`` "never raises mid-stream; errors surface as an
    ``error`` event"; §4 — the terminal ``error`` names the frame's ``error_type``; the
    partial answer is kept (§8.2 reducer: "``error`` keeps the partial answer and adds the
    error row"); one terminal event; the claim is released so the next send is turn + 1."""
    agent_id = make_active_agent(method="zip_runtime", name=f"chat-fault-{uuid.uuid4().hex[:8]}")
    sid = _sid()
    head = _deltas(k, "d")
    if with_tool:
        head = head[:1] + [{"event": "tool", "data": {"name": "search", "id": "t-1"}}] + head[1:]
    producer.queue.append(Script(head, fault=fault.frame()))
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "q", "session_id": sid})
    assert res.status_code == 200, res.text
    frames = _non_hb(_frames(res.text))
    kinds = [kf for kf, _d, _s in frames]
    assert kinds == ["meta", *[e["event"] for e in head], "error"], kinds
    error = frames[-1][1]
    assert fault.error_type in error["message"], error
    assert "done" not in kinds
    producer.wait_finished(agent_id, sid)
    log = _log(agent_id, sid, 1)
    assert [e for e, _s, _d in log] == kinds
    assert [s for _e, s, _d in log] == list(range(1, len(kinds) + 1))
    row = _session(agent_id, sid)
    assert row.active_turn is None and row.turns == 1
    rows = [(r, t, n) for r, t, n, _tr in _rows(sid)]
    expected = _replay_rows("q", log)
    assert rows == expected, (rows, expected)
    assert rows[-1][0] == "error" and fault.error_type in rows[-1][1]
    kept = "".join(t for r, t, _n in rows if r == "agent")
    assert kept == "".join(e["data"]["text"] for e in head if e["event"] == "delta")
    # the next send is a fresh turn, never a 409
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "again", "session_id": sid})
    assert res.status_code == 200, res.text
    assert _frames(res.text)[0][1]["turn"] == 2 and _frames(res.text)[-1][0] == "done"
    producer.wait_finished(agent_id, sid)


# ---------------------------------------------------------------------------
# 4. the state machine
# ---------------------------------------------------------------------------


# N16-obs49 (open): every occurrence of the generic-404 race shape the ``race`` rule discards
# (``assume(False)``) is recorded here with the world's diagnostics. The gate run ignores the
# list (determinism); the soak twin FAILS when it is non-empty, so the race is hunted under
# ``LAUNCHPAD_SOAK=1`` and never silently discarded there.
OBS49_OCCURRENCES: list[dict] = []


class ChatSessionMachine(RuleBasedStateMachine):
    """Random walk over one session: scripted sends, a held-open turn, reconnects,
    refreshes, stops, and a 'restart' modelled as a dead turn on a sibling session
    swept by ``clear_stale_turn_claims`` (the process itself cannot restart mid-turn)."""

    client: TestClient
    producer: Producer

    def __init__(self) -> None:
        super().__init__()
        self.world = World(self.client, self.producer,
                           make_active_agent(method="zip_runtime",
                                             name=f"chat-sm-{uuid.uuid4().hex[:8]}"))
        self.dead: list[tuple[str, int]] = []  # (sid, logged events) of swept sessions

    @initialize()
    def start(self) -> None:
        self.world.refresh()
        self._t = time.monotonic()

    def _trace(self, what: str) -> None:
        if TRACE:
            now = time.monotonic()
            print(f"[chat-sm] {what} {now - self._t:.2f}s open={self.world.open_turn}",
                  file=sys.stderr)
            self._t = now

    @rule(tag=st.sampled_from(["A", "B"]))
    def send(self, tag: str) -> None:
        self._trace("send")
        self.world.send_checked(tag)

    @rule(tag=st.sampled_from(["A", "B"]), head=st.integers(0, 2), tail=st.integers(0, 2))
    def start_gated(self, tag: str, head: int, tail: int) -> None:
        self._trace("start_gated")
        if self.world.open_turn or self.world.ended:
            # a live turn (§8.2) or an ended session (N16-obs79): refused, never live
            self.world.send_checked(tag)
        else:
            self.world.send_gated(tag, head, tail)

    @rule(tag=st.sampled_from(["A", "B"]))
    def fresh_session(self, tag: str) -> None:
        """N16-obs79: after END SESSION the console opens a NEW session — always possible."""
        self._trace("fresh_session")
        self.world.fresh_session_send(tag)

    @rule()
    def release_gate(self) -> None:
        self._trace("release_gate")
        self.world.release()

    @rule()
    def race(self) -> None:
        self._trace("race")
        if self.world.open_turn:
            self.world.release()
        statuses = self.world.race_send()
        if self.world.ended:
            assert statuses == [409, 409], self.world.refusals[-3:]  # N16-obs79
            return
        if statuses == [200, 404] and self.world.refusals and \
                self.world.refusals[-1][1] == "http.404":
            # N16-obs49 (open): under heavy gate load (32 xdist workers) the racing tab's
            # POST intermittently answers Starlette's generic 404 (`http.404`, no route
            # matched) instead of 409 `chat.turn_in_progress` — ~1 run in 8; not yet
            # isolated (diagnostics in ``world.refusals``). The example is discarded so the
            # gate stays deterministic; the register row keeps it open. The occurrence is
            # counted for the soak twin, which fails on any (rev-r16 verdict on r16-e).
            note(f"N16-obs49 generic 404 on the racing POST: {self.world.refusals[-1]}")
            OBS49_OCCURRENCES.append({
                "statuses": list(statuses), "agent_id": self.world.agent_id,
                "session_id": self.world.sid, "open_turn": self.world.open_turn,
                "refusals": list(self.world.refusals[-3:]), "at": time.time(),
            })
            assume(False)
        assert statuses == [200, 409], self.world.refusals[-3:]

    @rule(after=st.integers(0, 6))
    def reconnect(self, after: int) -> None:
        self._trace("reconnect")
        self.world.reconnect(after)

    @rule()
    def refresh(self) -> None:
        self._trace("refresh")
        self.world.refresh()

    @rule()
    def stop(self) -> None:
        self._trace("stop")
        self.world.stop()

    @rule(head=st.integers(0, 2), with_tool=st.booleans())
    def restart_with_a_dead_turn(self, head: int, with_tool: bool) -> None:
        """A sibling session a dead process left; the sweep must finalize it exactly once
        and never touch THIS session's live turn (its claim is this process's)."""
        self._trace("restart_with_a_dead_turn")
        if self.world.open_turn:
            self.world.release()
        sid = _sid()
        events = _seed_dead_turn(self.world.agent_id, sid, head, with_tool, 1, False)
        cleared = chat_turns.clear_stale_turn_claims()
        assert cleared == 1, cleared
        log = _log(self.world.agent_id, sid, 1)
        assert [e for e, _s, _d in log][-1] == "error" and len(log) == len(events) + 1
        self.dead.append((sid, len(log)))

    @invariant()
    def claim_is_at_most_the_live_turn(self) -> None:
        claim = _claim(self.world.agent_id, self.world.sid)
        live = chat_turns.live_run(self.world.agent_id, self.world.sid)
        if live is None or live.turn is None:
            assert claim is None or self.world.open_turn, claim
        else:
            assert claim in (None, live.turn), (claim, live.turn)

    @invariant()
    def swept_sessions_stay_final(self) -> None:
        for sid, n in self.dead:
            log = _log(self.world.agent_id, sid, 1)
            assert len(log) == n and sum(e in TERMINAL for e, _s, _d in log) == 1
            assert _claim(self.world.agent_id, sid) is None

    def teardown(self) -> None:
        try:
            self.world.release()
            self.world.check()
        finally:
            for gate in self.world.gates:
                gate.set()
            self.producer.wait_all_finished()


def _run_machine(client, producer, *, examples: int, steps: int) -> None:
    OBS49_OCCURRENCES.clear()
    ChatSessionMachine.client = client
    ChatSessionMachine.producer = producer
    ChatSessionMachine.TestCase.settings = settings(
        deadline=None, max_examples=examples, stateful_step_count=steps, phases=NO_SHRINK,
        suppress_health_check=[HealthCheck.too_slow],
    )
    ChatSessionMachine.TestCase().runTest()


def test_chat_session_state_machine(client, producer):
    _run_machine(client, producer, examples=3 * SCALE, steps=STEPS)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
def test_soak_chat_session_state_machine(client, producer):
    """The machine at soak scale — and the N16-obs49 hunt: the gate run discards the
    generic-404 race shape to stay deterministic; here every discarded occurrence counts and
    ANY makes the test fail with the diagnostics the world captured (statuses, the last
    refusals ``(status, code, tab, prompt, …)``, session), so the open register row gets a
    reproduction the moment the soak sees one."""
    _run_machine(client, producer, examples=30, steps=STEPS * 4)
    assert not OBS49_OCCURRENCES, (
        f"N16-obs49 reproduced {len(OBS49_OCCURRENCES)} time(s) under soak — the racing POST "
        f"answered the generic 404 instead of 409 chat.turn_in_progress: "
        + json.dumps(OBS49_OCCURRENCES, default=str, ensure_ascii=False)[:4000]
    )


# ---------------------------------------------------------------------------
# N16-obs45 — a live-tail reattach from at/past the last seq never ends on a terminal event
# ---------------------------------------------------------------------------


def test_reattach_from_beyond_the_live_buffer_still_ends_on_a_terminal_event(client, producer):
    """Repro: a turn held open after `meta` + one delta (its final log is meta=1, delta=2,
    done=3); a second tab reattaches with ``from=3`` while the turn is live; the gate is
    released. Expected (§8.3, and what the ledger path does): the stream ends on a
    terminal frame — the turn's own ``done`` or the bare ``done``. Observed:
    ``LiveTurn.subscribe(after=3)`` returns once ``done`` is set without yielding anything
    (``live_turns.tail_turn`` takes the live branch and never reaches ``ended``), so the
    client sees ``[]``."""
    agent_id = make_active_agent(method="zip_runtime", name="chat-obs45")
    sid = _sid()
    gate = threading.Event()
    producer.queue.append(Script(_deltas(1, "h"), [], gate=gate))
    box: dict[str, Any] = {}

    def send():
        box["post"] = client.post(f"/api/chat/{agent_id}",
                                  json={"prompt": "q", "session_id": sid})

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    deadline = time.monotonic() + BUDGET_S
    while time.monotonic() < deadline:
        live = chat_turns.live_run(agent_id, sid)
        if live is not None and live.turn is not None and live.seq >= 2:
            break
        time.sleep(0.005)
    tab_b = TestClient(client.app)

    def reattach():
        box["re"] = _reattach(tab_b, agent_id, sid, 1, 3)

    watcher = threading.Thread(target=reattach, daemon=True)
    watcher.start()
    time.sleep(0.2)  # the watcher is subscribed before the turn ends
    gate.set()
    sender.join(timeout=BUDGET_S)
    watcher.join(timeout=BUDGET_S)
    producer.wait_finished(agent_id, sid)
    post = [(k, s) for k, _d, s in _non_hb(_frames(box["post"].text))]
    assert post == [("meta", 1), ("delta", 2), ("done", 3)], post
    got = [(k, s) for k, _d, s in _non_hb(_frames(box["re"].text))]
    assert got and got[-1][0] in TERMINAL, got  # a terminal frame, as the ledger path gives
    later = _reattach(client, agent_id, sid, 1, 3)
    assert [(k, s) for k, _d, s in _non_hb(_frames(later.text))] == [("done", None)]


# ---------------------------------------------------------------------------
# a reattach refusal pin the programs rely on
# ---------------------------------------------------------------------------


def test_reattach_to_a_turn_the_session_never_reached_is_404(client, producer):
    """§8.3: ``404 chat.turn_not_found`` (turn ≤ 0 or > turns)."""
    agent_id = make_active_agent(method="zip_runtime", name="chat-404")
    sid = _sid()
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "q", "session_id": sid})
    assert res.status_code == 200
    producer.wait_finished(agent_id, sid)
    for turn in (0, 2, 99):
        res = _reattach(client, agent_id, sid, turn, 0)
        assert res.status_code == 404 and res.json()["code"] == "chat.turn_not_found", res.text
    body = json.loads(_reattach(client, agent_id, sid, 1, 0).text.split("data:")[1].split("\n")[0])
    assert body["turn"] == 1
