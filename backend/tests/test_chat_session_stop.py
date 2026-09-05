"""POST /api/chat/{agent_id}/sessions/{session_id}/stop — explicit end of the live
AgentCore Runtime session behind a Chat conversation (StopRuntimeSession)."""

from botocore.exceptions import ClientError

import app.routers.agents as agents_router  # noqa: F401 (ensures methods registered)
import app.services.chat as chat_service
import app.services.invoke as invoke_service
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, ChatSession
from app.services.agentcore import runtime as rt

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/stop-me"


def _delta_events(text: str):
    return iter([{"event": "delta", "data": {"text": text}}])


def make_agent(method="zip_runtime", name="stop-agent", spec=None, workspace=None) -> str:
    db = SessionLocal()
    agent = Agent(
        workspace_id=workspace or DEFAULT_WORKSPACE_ID,
        name=name, method=method, status="active",
        arn=RUNTIME_ARN if method != "harness" else RUNTIME_ARN.replace("runtime/", "harness/"),
        spec=spec or {"name": name},
    )
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()
    return agent_id


def start_session(client, monkeypatch, agent_id: str) -> str:
    """One chat turn through the router, so the ledger has a real session row."""
    monkeypatch.setattr(
        chat_service,
        "invoke_agent_events",
        lambda a, p, session_id=None, actor_id="river", **_kw: _delta_events("ok"),
    )
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "hi"})
    assert res.status_code == 200
    return client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"][0]["session_id"]


class StopDataPlane:
    """Stub of the `bedrock-agentcore` data-plane client: records the call."""

    def __init__(self, error_code: str | None = None):
        self.calls: list[dict] = []
        self.error_code = error_code

    def stop_runtime_session(self, **params):
        self.calls.append(params)
        if self.error_code:
            raise ClientError(
                {"Error": {"Code": self.error_code, "Message": "Session not found"}},
                "StopRuntimeSession",
            )
        return {"runtimeSessionId": params["runtimeSessionId"], "statusCode": 200}


def _row(agent_id: str, session_id: str) -> ChatSession:
    db = SessionLocal()
    try:
        return (
            db.query(ChatSession)
            .filter(ChatSession.agent_id == agent_id, ChatSession.session_id == session_id)
            .one()
        )
    finally:
        db.close()


def test_stop_runtime_session_wrapper_sends_arn_and_session(monkeypatch):
    plane = StopDataPlane()
    out = rt.stop_runtime_session(plane, runtime_arn=RUNTIME_ARN, session_id="s" * 33)
    assert plane.calls == [{"agentRuntimeArn": RUNTIME_ARN, "runtimeSessionId": "s" * 33}]
    assert out == {"session_id": "s" * 33, "status_code": 200}
    rt.stop_runtime_session(plane, runtime_arn=RUNTIME_ARN, session_id="x" * 33, qualifier="v3")
    assert plane.calls[-1]["qualifier"] == "v3"


def test_stop_session_runtime_agent_ends_and_stamps_ended_at(client, monkeypatch):
    agent_id = make_agent()
    sid = start_session(client, monkeypatch, agent_id)
    plane = StopDataPlane()
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: plane)

    res = client.post(f"/api/chat/{agent_id}/sessions/{sid}/stop")

    assert res.status_code == 200
    body = res.json()
    assert body["ended"] is True and body["already_ended"] is False
    assert body["session_id"] == sid and body["ended_at"]
    assert plane.calls == [{"agentRuntimeArn": RUNTIME_ARN, "runtimeSessionId": sid}]
    assert _row(agent_id, sid).ended_at is not None
    # the transcript is kept: history still replays and the rail lists the row
    history = client.get(f"/api/chat/{agent_id}/history", params={"session_id": sid}).json()
    assert [m["role"] for m in history["messages"]] == ["user", "agent"]


def test_list_sessions_reports_ended_at_after_stop(client, monkeypatch):
    agent_id = make_agent(name="stop-list-agent")
    sid = start_session(client, monkeypatch, agent_id)
    before = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"][0]
    assert before["ended_at"] is None
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: StopDataPlane())
    client.post(f"/api/chat/{agent_id}/sessions/{sid}/stop")
    after = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"][0]
    assert after["session_id"] == sid and after["ended_at"]


def test_stop_session_already_gone_is_success_with_already_ended(client, monkeypatch):
    agent_id = make_agent(method="container", name="stop-gone-agent")
    sid = start_session(client, monkeypatch, agent_id)
    monkeypatch.setattr(
        invoke_service, "data_client",
        lambda _ws=None: StopDataPlane(error_code="ResourceNotFoundException"),
    )

    res = client.post(f"/api/chat/{agent_id}/sessions/{sid}/stop")

    assert res.status_code == 200
    assert res.json()["ended"] is True and res.json()["already_ended"] is True
    assert _row(agent_id, sid).ended_at is not None


def test_stop_session_twice_keeps_first_ended_at(client, monkeypatch):
    agent_id = make_agent(name="stop-twice-agent")
    sid = start_session(client, monkeypatch, agent_id)
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: StopDataPlane())
    first = client.post(f"/api/chat/{agent_id}/sessions/{sid}/stop").json()["ended_at"]
    monkeypatch.setattr(
        invoke_service, "data_client",
        lambda _ws=None: StopDataPlane(error_code="ResourceNotFoundException"),
    )
    second = client.post(f"/api/chat/{agent_id}/sessions/{sid}/stop").json()
    assert second["already_ended"] is True and second["ended_at"] == first


def test_stop_session_retryable_conflict_surfaces_as_409_envelope(client, monkeypatch):
    agent_id = make_agent(name="stop-conflict-agent")
    sid = start_session(client, monkeypatch, agent_id)
    monkeypatch.setattr(
        invoke_service, "data_client",
        lambda _ws=None: StopDataPlane(error_code="RetryableConflictException"),
    )
    res = client.post(f"/api/chat/{agent_id}/sessions/{sid}/stop")
    assert res.status_code == 409
    assert res.json()["code"] == "aws.conflict"
    assert res.json()["detail"]["aws_error_code"] == "RetryableConflictException"
    assert _row(agent_id, sid).ended_at is None  # not ended: the row says so


def test_stop_session_harness_agent_is_unsupported(client, monkeypatch):
    agent_id = make_agent(method="harness", name="stop-harness-agent")
    sid = start_session(client, monkeypatch, agent_id)
    plane = StopDataPlane()
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: plane)

    res = client.post(f"/api/chat/{agent_id}/sessions/{sid}/stop")

    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "chat.session_stop_unsupported"
    assert body["detail"]["reason_code"] == "harness"
    assert body["message"]
    assert plane.calls == []
    assert _row(agent_id, sid).ended_at is None


def test_stop_session_discovered_harness_is_unsupported(client, monkeypatch):
    agent_id = make_agent(
        method="discovered_runtime", name="stop-disc-harness",
        spec={"name": "d", "discovery": {"resource_type": "harness", "aws_status": "READY",
                                          "authorizer_type": "none"}},
    )
    sid = start_session(client, monkeypatch, agent_id)
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: StopDataPlane())
    res = client.post(f"/api/chat/{agent_id}/sessions/{sid}/stop")
    assert res.status_code == 409
    assert res.json()["code"] == "chat.session_stop_unsupported"


def test_stop_session_discovered_runtime_qualifies(client, monkeypatch):
    agent_id = make_agent(
        method="discovered_runtime", name="stop-disc-runtime",
        spec={"name": "d", "protocol": "http",
              "discovery": {"resource_type": "runtime", "aws_status": "READY",
                            "authorizer_type": "none", "artifact_type": "code"}},
    )
    sid = start_session(client, monkeypatch, agent_id)
    plane = StopDataPlane()
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: plane)
    res = client.post(f"/api/chat/{agent_id}/sessions/{sid}/stop")
    assert res.status_code == 200 and len(plane.calls) == 1


def test_stop_session_of_another_agent_is_404(client, monkeypatch):
    owner = make_agent(name="stop-owner-agent")
    other = make_agent(name="stop-other-agent")
    sid = start_session(client, monkeypatch, owner)
    plane = StopDataPlane()
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: plane)

    res = client.post(f"/api/chat/{other}/sessions/{sid}/stop")

    assert res.status_code == 404
    assert res.json()["code"] == "chat.session_not_found"
    assert plane.calls == []
    assert _row(owner, sid).ended_at is None


def test_stop_session_of_another_workspace_is_404(client, monkeypatch):
    foreign = make_agent(name="stop-foreign-agent", workspace="ws-elsewhere")
    db = SessionLocal()
    db.add(ChatSession(workspace_id="ws-elsewhere", agent_id=foreign,
                       session_id="f" * 33, turns=1))
    db.commit()
    db.close()
    plane = StopDataPlane()
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: plane)
    res = client.post(f"/api/chat/{foreign}/sessions/{'f' * 33}/stop")
    assert res.status_code == 404
    assert plane.calls == []


def test_stop_session_unknown_session_id_is_404(client, monkeypatch):
    agent_id = make_agent(name="stop-unknown-agent")
    plane = StopDataPlane()
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: plane)
    res = client.post(f"/api/chat/{agent_id}/sessions/{'n' * 33}/stop")
    assert res.status_code == 404 and plane.calls == []


def test_stop_session_then_new_turn_clears_ended_at(client, monkeypatch):
    """A prompt posted under an ended id starts a fresh runtime session with that
    id, so the rail must stop calling it ended."""
    agent_id = make_agent(name="stop-revive-agent")
    sid = start_session(client, monkeypatch, agent_id)
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: StopDataPlane())
    client.post(f"/api/chat/{agent_id}/sessions/{sid}/stop")
    assert _row(agent_id, sid).ended_at is not None
    client.post(f"/api/chat/{agent_id}", json={"prompt": "again", "session_id": sid})
    assert _row(agent_id, sid).ended_at is None
    assert client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"][0]["ended_at"] is None
