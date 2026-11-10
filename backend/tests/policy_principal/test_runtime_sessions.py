# ruff: noqa: F811 — pytest fixtures are imported explicitly from test_mint_entrances
"""Stream A re-review defect 2: with the principal carrier on, the AgentCore Runtime session
of a turn is derived from (the caller's session id, the principal), so two principals never
share a runtime process (the runtime holds the token per process) — no squatting, no
mismatch 409; one principal keeps its conversation; at most one in-flight turn per runtime
session (409 ``policy.session_busy``). Switch off: the session ids are byte-identical."""

from __future__ import annotations

import threading

import pytest

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.policy import PolicyRuntimeSession
from app.services import invoke as invoke_service
from app.services import principal_mint
from app.services.agentcore import runtime as rt
from app.services.policy_entities import Principal
from tests.conftest import ws_ctx
from tests.policy_principal.test_mint_entrances import (  # noqa: F401
    USER,
    _agent,
    _Body,
    _DataPlane,
    _payload,
    _switches,
    _verify,
    data,
    kms,
    on,
)

ADMIN = type("I", (), {"username": "hr-admin", "role": "admin", "user_id": "u_hradmin01"})()
SID = "s" * 40


def _turn(agent, who=USER, session_id=SID):
    return invoke_service.invoke_agent_text(agent, "hi", session_id=session_id,
                                            principal=Principal.from_identity(who))


def test_two_principals_in_one_conversation_get_different_runtime_sessions(data, on, signer):
    agent = _agent()
    first = _turn(agent)
    second = _turn(agent, ADMIN)  # an admin continuing a member's session: no 409
    user_rt, admin_rt = (c["runtimeSessionId"] for c in data.calls)
    assert user_rt != admin_rt and SID not in (user_rt, admin_rt)
    assert user_rt.startswith("lpp-") and 33 <= len(user_rt) <= 256
    # each token names the runtime session its turn ran in, and each principal its own
    for call, rt_id in zip(data.calls, (user_rt, admin_rt), strict=True):
        claims = _verify(_payload(call)["launchpad"]["principal"], signer, agent.id)
        assert claims["sid"] == rt_id
    # the callers keep their own conversation id
    assert first["session_id"] == second["session_id"] == SID


def test_one_principal_keeps_its_runtime_session_across_turns(data, on):
    agent = _agent()
    _turn(agent)
    _turn(agent)
    assert data.calls[0]["runtimeSessionId"] == data.calls[1]["runtimeSessionId"]
    _turn(agent, session_id="t" * 40)  # another conversation: another runtime session
    assert data.calls[2]["runtimeSessionId"] != data.calls[0]["runtimeSessionId"]


def test_the_derived_sessions_are_recorded_under_the_conversation(data, on):
    agent = _agent()
    _turn(agent)
    _turn(agent, ADMIN)
    _turn(agent)
    db = SessionLocal()
    try:
        rows = db.query(PolicyRuntimeSession).filter_by(agent_id=agent.id,
                                                        session_id=SID).all()
    finally:
        db.close()
    assert sorted(r.principal for r in rows) == ["user:u_hradmin01", "user:u_hrintern01"]
    assert set(principal_mint.runtime_sessions_for(agent.id, SID)) == {
        c["runtimeSessionId"] for c in data.calls}


def test_switch_off_sends_the_callers_session_id_unchanged(data, monkeypatch, kms):
    _switches(global_on=False, workspace_on=False, monkeypatch=monkeypatch)
    agent = _agent()
    _turn(agent)
    _turn(agent, ADMIN)
    assert [c["runtimeSessionId"] for c in data.calls] == [SID, SID]
    assert principal_mint.runtime_sessions_for(agent.id, SID) == []
    _switches(global_on=True, workspace_on=False, monkeypatch=monkeypatch)
    _turn(agent)
    assert data.calls[-1]["runtimeSessionId"] == SID


def test_an_exempt_preset_keeps_the_callers_session(data, on):
    agent = _agent(system_key="architect", name="launchpad-architect")
    _turn(agent)
    assert data.calls[0]["runtimeSessionId"] == SID


def test_the_streaming_entrance_derives_the_same_runtime_session(on, monkeypatch, data):
    agent = _agent()
    sent: list[str] = []

    def stream(_client, _arn, _prompt, **kw):
        sent.append(kw["session_id"])
        yield {"event": "delta", "data": {"text": "a"}}

    monkeypatch.setattr(rt, "stream_runtime_events", stream)
    list(invoke_service.invoke_agent_events(agent, "hi", session_id=SID,
                                            principal=Principal.from_identity(USER)))
    expected = principal_mint.derive_runtime_session_id(
        agent, Principal.from_identity(USER), SID)
    assert sent == [expected]


class _BlockingPlane(_DataPlane):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        assert self.release.wait(5)
        return {"response": _Body(self.body), "contentType": "application/json"}


def test_one_turn_in_flight_per_runtime_session(on, monkeypatch):
    plane = _BlockingPlane()
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: plane)
    agent = _agent()
    done: dict = {}
    worker = threading.Thread(target=lambda: done.setdefault("first", _turn(agent)))
    worker.start()
    assert plane.entered.wait(5)
    try:
        with pytest.raises(AppError) as err:
            _turn(agent)  # the same principal, the same conversation, still running
        assert err.value.code == "policy.session_busy" and err.value.status_code == 409
        # another principal in the SAME conversation runs in its own runtime session
        other = threading.Thread(target=lambda: done.setdefault("admin", _turn(agent, ADMIN)))
        other.start()
    finally:
        plane.release.set()
    worker.join(5)
    other.join(5)
    assert done["first"]["text"] == "ok" and done["admin"]["text"] == "ok"
    assert principal_mint._IN_FLIGHT == set()
    assert _turn(agent)["text"] == "ok"  # free again


def test_a_failed_turn_frees_the_slot(on, monkeypatch):
    class _Failing(_DataPlane):
        def invoke_agent_runtime(self, **kwargs):
            raise RuntimeError("runtime 500")

    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: _Failing())
    with pytest.raises(RuntimeError):
        _turn(_agent())
    assert principal_mint._IN_FLIGHT == set()


def test_ending_the_conversation_stops_every_principals_runtime_session(data, on,
                                                                         monkeypatch):
    agent = _agent()
    _turn(agent)
    _turn(agent, ADMIN)
    stopped: list[str] = []
    monkeypatch.setattr(rt, "stop_runtime_session",
                        lambda _c, *, runtime_arn, session_id: stopped.append(session_id))
    out = invoke_service.stop_agent_session(agent, SID)
    assert out == {"ended": True, "already_ended": False}
    assert set(stopped) == {SID, *(c["runtimeSessionId"] for c in data.calls)}


def test_ending_with_the_switch_off_stops_the_one_session(data, monkeypatch, kms):
    _switches(global_on=False, workspace_on=False, monkeypatch=monkeypatch)
    agent = _agent()
    stopped: list[str] = []
    monkeypatch.setattr(rt, "stop_runtime_session",
                        lambda _c, *, runtime_arn, session_id: stopped.append(session_id))
    invoke_service.stop_agent_session(agent, SID)
    assert stopped == [SID]


def test_the_trace_rail_reads_the_derived_sessions_too(monkeypatch):
    from app.services import traces

    asked: list[str] = []

    def find(session_id, _ws, _hours, logs=None):
        asked.append(session_id)
        return ([{"traceId": "t", "spanId": session_id[-4:], "name": "x"}],
                {"aws/spans": 1})

    monkeypatch.setattr(traces, "find_session_spans", find)
    monkeypatch.setattr(traces, "normalize_spans", lambda raw: raw)
    monkeypatch.setattr(traces, "insights_url", lambda *a, **k: "")
    out = traces.session_trace(SID, ws_ctx(), also=["lpp-a" * 8, "lpp-b" * 8])
    assert asked == [SID, "lpp-a" * 8, "lpp-b" * 8]
    assert out["span_count"] == 3 and out["log_groups"] == ["aws/spans"]
    asked.clear()
    traces.session_trace(SID, ws_ctx())
    assert asked == [SID]


def test_a_session_store_error_fails_closed_with_503(data, on, monkeypatch):
    """Never fall back to the shared caller id: two principals could share a process."""
    from sqlalchemy.exc import OperationalError

    def broken(*_a, **_k):
        raise OperationalError("INSERT", {}, Exception("database is locked"))

    monkeypatch.setattr(principal_mint, "_remember", broken)
    with pytest.raises(AppError) as err:
        _turn(_agent())
    assert err.value.code == "policy.session_unavailable" and err.value.status_code == 503
    assert data.calls == []  # nothing reached the runtime


def test_deleting_the_agent_forgets_its_derived_sessions(data, on):
    from app.models.ledger import Agent
    from app.routers.agents import mark_agent_deleted

    agent = _agent()
    keep = _agent(name="other", arn=agent.arn.replace("agent-abc", "agent-def"))
    _turn(agent)
    _turn(agent, ADMIN)
    _turn(keep)
    db = SessionLocal()
    try:
        mark_agent_deleted(db, db.get(Agent, agent.id))
    finally:
        db.close()
    assert principal_mint.runtime_sessions_for(agent.id, SID) == []
    assert len(principal_mint.runtime_sessions_for(keep.id, SID)) == 1
