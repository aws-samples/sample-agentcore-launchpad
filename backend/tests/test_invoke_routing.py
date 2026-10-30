"""Canary routing in the one invoke chain (N12 E3-2, plan §B.7; canary-only since N13-obs2).

Chat (``invoke_agent_events``) and the public ``/v1`` API (``invoke_agent_text``) share
``invoke_agent_text`` / ``invoke_agent_events``; the hook resolves the session id first,
asks ``routing.route_for`` for the sticky arm and sends it as ``qualifier`` (canary
endpoint) + ``baggage`` / ``payload["launchpad"]`` (owner + arm). Replaces the Global
``test_invoke_canary_route.py`` (gateway route, buffered-stream downgrade).
"""

import json

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent
from app.optimization import routing
from app.optimization.models import ExperimentAssignment, RuntimeCanary
from app.services import invoke as invoke_service
from app.services.agentcore import runtime as rt
from tests.conftest import ws_ctx

WS = ws_ctx()
ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/agent-abc"


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _DataPlane:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        return {"response": _Body(b'{"result": "ok"}'), "contentType": "application/json"}


@pytest.fixture
def data(monkeypatch) -> _DataPlane:
    plane = _DataPlane()
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: plane)
    return plane


def _agent(method: str = "zip_runtime", protocol: str = "http") -> Agent:
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="routed", method=method,
                      status="active", arn=ARN, resource_id="agent-abc",
                      spec={"protocol": protocol, "system_prompt": "hi"})
        db.add(agent)
        db.commit()
        db.refresh(agent)
        return agent
    finally:
        db.close()


def _canary(agent: Agent, *, live: bool, execution_status: str = "RUNNING") -> RuntimeCanary:
    artifacts = {
        "agent_meta": {"id": agent.id, "arn": ARN, "resource_id": "agent-abc"},
        "setup": {"stable_endpoint": "stableabc123", "treatment_endpoint": "treatabc123",
                  "runtime_id": "agent-abc", "v_current": "3"},
        "rounds": [],
    }
    if live:
        artifacts["abtest"] = {"ab_test_id": "local_c", "execution_status": execution_status,
                               "variants": routing.target_variants("stableabc123", "treatabc123",
                                                                   50, 50)}
    db = SessionLocal()
    try:
        row = RuntimeCanary(workspace_id=DEFAULT_WORKSPACE_ID, name="CAN",
                            champion_agent_id=agent.id, champion_agent_name=agent.name,
                            challenger_agent_id=agent.id, challenger_agent_name=agent.name,
                            artifacts=artifacts)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def _assignments(owner_id: str) -> dict[str, str]:
    db = SessionLocal()
    try:
        return {r.session_id: r.arm for r in db.query(ExperimentAssignment).filter_by(
            owner_id=owner_id)}
    finally:
        db.close()


def _payload(call: dict) -> dict:
    return json.loads(call["payload"])


def test_session_id_is_resolved_before_dispatch_so_a_new_session_is_routed(data):
    agent = _agent()
    exp = _canary(agent, live=True)
    result = invoke_service.invoke_agent_text(agent, "hello", workspace=WS)
    sid = result["session_id"]
    assert len(sid) >= 33
    (call,) = data.calls
    assert call["runtimeSessionId"] == sid
    assert sid in _assignments(exp.id)
    assert "baggage" in call and call["qualifier"] in {"stableabc123", "treatabc123"}


# ─── endpoint arm (canary) ───────────────────────────────────────────────────
def test_endpoint_arm_sets_the_named_endpoint_qualifier(data):
    agent = _agent()
    canary = _canary(agent, live=True)
    # find one session of each arm deterministically
    sids: dict[str, str] = {}
    db = SessionLocal()
    try:
        owner = routing.active_owner(db, agent.id)
    finally:
        db.close()
    for i in range(200):
        sid = f"sess{i:04d}" + "x" * 30
        sids.setdefault(routing.choose_arm(owner, sid), sid)
        if len(sids) == 2:
            break
    assert set(sids) == {"C", "T1"}
    for sid in sids.values():
        invoke_service.invoke_agent_text(agent, "hello", session_id=sid, workspace=WS)
    by_session = {c["runtimeSessionId"]: c for c in data.calls}
    assert by_session[sids["C"]]["qualifier"] == "stableabc123"
    assert by_session[sids["T1"]]["qualifier"] == "treatabc123"
    for arm, sid in sids.items():
        call = by_session[sid]
        assert "launchpad.bundle" not in call["baggage"]
        assert f"routing_experiment_variant_name={arm}" in call["baggage"]
        assert _payload(call)["launchpad"] == {"experiment": canary.id, "variant": arm}
    assert _assignments(canary.id) == {sids["C"]: "C", sids["T1"]: "T1"}


def test_provisioning_canary_pins_every_session_to_the_stable_endpoint(data):
    """Setup minted the candidate (DEFAULT auto-rolled) but the A/B row is not live:
    production must keep serving v_current through ``stable<id6>``."""
    agent = _agent()
    _canary(agent, live=False)
    for i in range(5):
        invoke_service.invoke_agent_text(agent, "hello", session_id=f"p{i}" + "x" * 36,
                                         workspace=WS)
    assert {c["qualifier"] for c in data.calls} == {"stableabc123"}


# ─── one arm for both entrances, sync and streaming ──────────────────────────
def test_chat_and_v1_and_streaming_share_one_sticky_arm(data):
    agent = _agent()
    exp = _canary(agent, live=True)
    sid = "shared-" + "s" * 40
    invoke_service.invoke_agent_text(agent, "turn 1", session_id=sid, workspace=WS,
                                     console_user=True)
    invoke_service.invoke_agent_text(agent, "turn 2", session_id=sid, workspace=WS,
                                     console_user=False)  # the public /v1 entrance
    events = list(invoke_service.invoke_agent_events(agent, "turn 3", session_id=sid,
                                                     workspace=WS))
    assert [e["event"] for e in events] == ["delta"]
    assert len(data.calls) == 3
    assert len({c["baggage"] for c in data.calls}) == 1
    assert len({c["qualifier"] for c in data.calls}) == 1
    arm = _assignments(exp.id)[sid]
    expected = {"experiment": exp.id, "variant": arm}
    assert all(_payload(c)["launchpad"] == expected for c in data.calls)
    assert len(_assignments(exp.id)) == 1


def test_streaming_path_streams_natively_during_a_canary(data, monkeypatch):
    """No buffered downgrade: the streaming entrance calls stream_runtime_events with the
    routing kwargs instead of falling back to a joined text turn."""
    agent = _agent()
    _canary(agent, live=True)
    seen: dict = {}

    def fake_stream(client, arn, prompt, **kwargs):
        seen.update(kwargs)
        yield {"event": "delta", "data": {"text": "streamed"}}

    monkeypatch.setattr(rt, "stream_runtime_events", fake_stream)
    events = list(invoke_service.invoke_agent_events(agent, "hi", session_id="t" * 40,
                                                     workspace=WS))
    assert events == [{"event": "delta", "data": {"text": "streamed"}}]
    assert seen["baggage"].startswith("aws.agentcore.gateway.routing_experiment_arn=")
    assert seen["launchpad_meta"]["variant"] in {"C", "T1"}
    assert seen["qualifier"] in {"stableabc123", "treatabc123"}


# ─── no owner / feature off / a2a ────────────────────────────────────────────
def test_no_active_owner_means_no_baggage_no_alias_no_qualifier(data):
    agent = _agent()
    _canary(agent, live=True, execution_status="STOPPED")  # complete / rollback stopped it
    invoke_service.invoke_agent_text(agent, "hello", session_id="n" * 40, workspace=WS)
    (call,) = data.calls
    assert "baggage" not in call and "qualifier" not in call
    assert "launchpad" not in _payload(call)
    db = SessionLocal()
    try:
        assert db.query(ExperimentAssignment).count() == 0
    finally:
        db.close()


def test_routing_is_skipped_when_the_optimization_feature_is_off(data, monkeypatch):
    from app.core import config

    monkeypatch.setenv("LAUNCHPAD_FEATURES", json.dumps(["agents", "chat", "evaluation"]))
    config.get_settings.cache_clear()
    try:
        agent = _agent()
        exp = _canary(agent, live=True)
        invoke_service.invoke_agent_text(agent, "hello", session_id="f" * 40, workspace=WS)
    finally:
        monkeypatch.delenv("LAUNCHPAD_FEATURES", raising=False)
        config.get_settings.cache_clear()
    (call,) = data.calls
    assert "baggage" not in call and "qualifier" not in call
    assert "launchpad" not in _payload(call)
    assert _assignments(exp.id) == {}


def test_a2a_agents_are_never_routed(data, monkeypatch):
    agent = _agent(protocol="a2a")
    exp = _canary(agent, live=True)
    captured = {}

    def fake_a2a(client, arn, prompt, session_id=None, **kwargs):
        captured.update(session_id=session_id, **kwargs)
        return {"text": "a2a", "session_id": session_id}

    monkeypatch.setattr(rt, "invoke_a2a_text", fake_a2a)
    invoke_service.invoke_agent_text(agent, "hello", session_id="a" * 40, workspace=WS)
    # N10v3-bug5 live correction: the A2A path sends runtimeUserId like the HTTP path;
    # N16-obs79: and the memory actor, as A2A message metadata — signed with the per-agent
    # key (N16-obs90; its value is never asserted or printed)
    key = captured.pop("actor_key")
    assert isinstance(key, str) and len(key) == 64
    assert captured == {"session_id": "a" * 40, "runtime_user_id": "default",
                        "actor_id": "default"}
    assert _assignments(exp.id) == {}
