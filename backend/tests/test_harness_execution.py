"""Harness failures must survive partial output and stop replay before a paid batch."""

import json
from unittest.mock import MagicMock

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import service as evaluation
from app.evaluation.models import EvalRun
from app.models.ledger import Agent
from app.services import chat, invoke
from app.services.agentcore import harness as hc
from tests.conftest import ws_ctx

ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/test-123"
SID = "session-" + "a" * 40


def text(value):
    return {"contentBlockDelta": {"delta": {"text": value}}}


def stop(reason):
    return {"messageStop": {"stopReason": reason}}


class Stream:
    def __init__(self, events, *, read_error=None, close_error=None):
        self.events = iter(events)
        self.read_error = read_error
        self.close_error = close_error
        self.closed = 0
        self.drained = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.events)
        except StopIteration:
            self.drained = True
            if self.read_error:
                raise self.read_error from None
            raise

    def close(self):
        self.closed += 1
        if self.close_error:
            raise self.close_error


def client_for(stream):
    client = MagicMock()
    client.invoke_harness.return_value = {"stream": stream}
    return client


def consume(client, mode):
    if mode == "sync":
        return hc.invoke_harness_text(client, ARN, "hello", session_id=SID)
    return list(hc.invoke_harness_events(
        client, ARN, [{"role": "user", "content": [{"text": "hello"}]}],
        session_id=SID, actor_id="default",
    ))


@pytest.mark.parametrize("mode", ["sync", "stream"])
@pytest.mark.parametrize(("reason", "code", "status"), [
    ("timeout_exceeded", "harness.execution_timeout", 504),
    ("Timeout exceeded: 30s (elapsed 30.1s)", "harness.execution_timeout", 504),
    ("cancelled", "harness.execution_cancelled", 502),
    ("canceled", "harness.execution_cancelled", 502),
    ("max_tokens", "harness.execution_limit", 502),
    ("max_iterations", "harness.execution_limit", 502),
    ("limit_turns", "harness.execution_limit", 502),
    ("limit_output_tokens", "harness.execution_limit", 502),
    ("limit_total_tokens", "harness.execution_limit", 502),
    ("Max iterations exceeded: 10", "harness.execution_limit", 502),
    ("Max output tokens exceeded: 400/300", "harness.execution_limit", 502),
    ("guardrail_intervened", "harness.incomplete_response", 502),
    ("interrupt", "harness.incomplete_response", 502),
    ("future_unknown_stop", "harness.incomplete_response", 502),
    (None, "harness.incomplete_response", 502),
])
def test_late_failure_after_text_and_model_end_turn(mode, reason, code, status):
    stream = Stream([
        text("partial"), stop("end_turn"), stop(reason),
        text("must not turn the failure into success"), stop("end_turn"),
        {"metadata": {"usage": {"outputTokens": 12}}},
    ])
    with pytest.raises(AppError) as error:
        consume(client_for(stream), mode)
    assert error.value.code == code
    assert error.value.status_code == status
    assert error.value.detail == {"stop_reason": reason}
    assert stream.drained and stream.closed == 1


@pytest.mark.parametrize("mode", ["sync", "stream"])
def test_outer_timeout_wins_over_inner_cancellation(mode):
    stream = Stream([text("partial"), stop("cancelled"), stop("timeout_exceeded")])
    with pytest.raises(AppError) as error:
        consume(client_for(stream), mode)
    assert error.value.code == "harness.execution_timeout"
    assert error.value.detail == {"stop_reason": "timeout_exceeded"}
    assert stream.drained and stream.closed == 1


def test_stream_yields_partial_text_then_raises_without_post_failure_text():
    stream = Stream([
        text("partial"), stop("cancelled"), text("ignore"), stop("timeout_exceeded"),
    ])
    events = hc.invoke_harness_events(
        client_for(stream), ARN, [], session_id=SID, actor_id="actor",
    )
    assert next(events) == {"event": "delta", "data": {"text": "partial"}}
    with pytest.raises(AppError) as error:
        next(events)
    assert error.value.code == "harness.execution_timeout"
    assert stream.closed == 1


@pytest.mark.parametrize("mode", ["sync", "stream"])
@pytest.mark.parametrize("final_reason", ["end_turn", "stop_sequence"])
def test_successful_tool_cycle_is_drained_and_closed(mode, final_reason):
    stream = Stream([
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {
            "toolUse": {"name": "kb_search", "toolUseId": "tool-1"},
        }}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {
            "toolUse": {"input": '{"query": '},
        }}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {
            "toolUse": {"input": '"schedule"}'},
        }}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        stop("tool_use"),
        {"contentBlockStart": {"start": {"toolResult": {"toolUseId": "tool-1"}}}},
        stop("tool_result"),
        text("final answer"), stop(final_reason), {"metadata": {"usage": {}}},
    ])
    client = client_for(stream)
    result = consume(client, mode)
    if mode == "sync":
        assert result == {"text": "final answer", "session_id": SID}
    else:
        assert result == [
            {"event": "tool", "data": {"name": "kb_search", "id": "tool-1"}},
            {"event": "tool_input", "data": {
                "name": "kb_search", "id": "tool-1", "input": '{"query": "schedule"}',
            }},
            {"event": "delta", "data": {"text": "final answer"}},
        ]
    assert client.invoke_harness.call_args.kwargs["runtimeSessionId"] == SID
    assert stream.drained and stream.closed == 1


@pytest.mark.parametrize("mode", ["sync", "stream"])
@pytest.mark.parametrize("events", [
    [], [stop("end_turn")], [text(" "), stop("end_turn")],
    [text("I will check"), stop("tool_use"), stop("tool_result")],
])
def test_empty_or_unfinished_tool_response_is_not_success(mode, events):
    stream = Stream(events)
    with pytest.raises(AppError) as error:
        consume(client_for(stream), mode)
    assert error.value.code == "harness.incomplete_response"
    assert stream.closed == 1


@pytest.mark.parametrize("mode", ["sync", "stream"])
def test_transport_failure_releases_resource_without_hiding_known_timeout(mode):
    stream = Stream(
        [text("partial"), stop("timeout_exceeded")],
        read_error=OSError("connection reset"), close_error=OSError("close failed"),
    )
    with pytest.raises(AppError) as error:
        consume(client_for(stream), mode)
    assert error.value.code == "harness.execution_timeout"
    assert isinstance(error.value.__cause__, OSError)
    assert stream.closed == 1


@pytest.mark.parametrize("mode", ["sync", "stream"])
@pytest.mark.parametrize("event", ["runtimeClientError", "internalServerException"])
def test_runtime_error_after_text_releases_resource(mode, event):
    stream = Stream([text("partial"), {event: {"message": "boom"}}])
    with pytest.raises(RuntimeError, match="boom"):
        consume(client_for(stream), mode)
    assert stream.closed == 1


def test_consumer_disconnect_closes_upstream_without_draining():
    stream = Stream([text("partial"), text("rest"), stop("end_turn")])
    on_stream = MagicMock()
    events = hc.invoke_harness_events(
        client_for(stream), ARN, [], session_id=SID, actor_id="actor", on_stream=on_stream,
    )
    next(events)
    on_stream.assert_called_once_with(stream)
    events.close()
    assert stream.closed == 1 and not stream.drained


def test_on_stream_callback_failure_closes_upstream():
    stream = Stream([text("answer"), stop("end_turn")])
    events = hc.invoke_harness_events(
        client_for(stream), ARN, [], session_id=SID, actor_id="actor",
        on_stream=MagicMock(side_effect=ValueError("callback failed")),
    )
    with pytest.raises(ValueError, match="callback failed"):
        next(events)
    assert stream.closed == 1


@pytest.mark.parametrize("mode", ["sync", "stream"])
def test_shared_invoke_entry_propagates_harness_failure(monkeypatch, mode):
    stream = Stream([text("partial"), stop("timeout_exceeded")])
    monkeypatch.setattr(invoke, "data_client", lambda _ws: client_for(stream))
    agent = Agent(
        id="agent-harness", workspace_id=DEFAULT_WORKSPACE_ID, name="test-harness",
        method="harness", arn=ARN, status="active", spec={},
    )
    with pytest.raises(AppError) as error:
        if mode == "sync":
            invoke.invoke_agent_text(agent, "hello", workspace=ws_ctx())
        else:
            list(invoke.invoke_agent_events(agent, "hello", workspace=ws_ctx()))
    assert error.value.code == "harness.execution_timeout"
    assert stream.closed == 1


@pytest.mark.parametrize("reason", ["timeout_exceeded", "cancelled", "limit_turns"])
def test_failed_replay_never_waits_for_telemetry_or_starts_batch(monkeypatch, reason):
    stream = Stream([text("partial"), stop("end_turn"), stop(reason)])
    client = client_for(stream)
    monkeypatch.setattr(evaluation, "data_client", lambda _ws: client)
    telemetry_wait = MagicMock()
    monkeypatch.setattr(evaluation, "_wait_for_fresh_telemetry", telemetry_wait)
    with SessionLocal() as db:
        run = EvalRun(
            workspace_id=DEFAULT_WORKSPACE_ID, agent_id="agent-harness",
            agent_name="test-harness", status="queued", evaluators=["Builtin.Helpfulness"],
        )
        db.add(run)
        db.commit()
        run_id = run.id
    evaluation.execute_run(
        run_id, workspace=ws_ctx(), agent_arn=ARN, method="harness",
        service_name="harness_test.DEFAULT", log_group="/test",
        items=[{"prompt": "hello"}, {"prompt": "must not be replayed"}],
        evaluators=["Builtin.Helpfulness"], mode="evaluators", wait_seconds=0,
    )
    with SessionLocal() as db:
        run = db.get(EvalRun, run_id)
        assert run.status == "failed"
        assert "Harness execution" in run.error
        assert run.batch_eval_id is None
        assert not run.session_ids
    client.invoke_harness.assert_called_once()
    telemetry_wait.assert_not_called()
    client.start_batch_evaluation.assert_not_called()
    assert stream.drained and stream.closed == 1


def create_agent():
    with SessionLocal() as db:
        agent = Agent(
            id="agent-harness", workspace_id=DEFAULT_WORKSPACE_ID, name="test-harness",
            method="harness", arn=ARN, status="active", spec={},
        )
        db.add(agent)
        db.commit()
        return agent


def invoke_endpoint(client, entrance, agent_id):
    headers = {}
    if entrance.startswith("public"):
        key = client.post("/api/apikeys", json={"name": "harness-test"}).json()["key"]
        headers["X-Api-Key"] = key
    paths = {
        "chat": f"/api/chat/{agent_id}",
        "public-stream": f"/v1/agents/{agent_id}/invoke-stream",
        "console-sync": f"/api/agents/{agent_id}/invoke",
        "public-sync": f"/v1/agents/{agent_id}/invoke",
    }
    return client.post(
        paths[entrance], json={"prompt": "hello", "session_id": SID}, headers=headers,
    )


def sse_events(response):
    events = []
    for frame in response.text.strip().split("\n\n"):
        lines = frame.splitlines()
        events.append({
            "event": lines[0].removeprefix("event: "),
            "data": json.loads(lines[1].removeprefix("data: ")),
        })
    return events


@pytest.mark.parametrize("entrance", ["chat", "public-stream", "console-sync", "public-sync"])
@pytest.mark.parametrize(("reasons", "code", "status"), [
    (["cancelled", "timeout_exceeded"], "harness.execution_timeout", 504),
    (["cancelled"], "harness.execution_cancelled", 502),
    (["Max iterations exceeded: 10"], "harness.execution_limit", 502),
    (["interrupt"], "harness.incomplete_response", 502),
])
def test_real_endpoints_preserve_late_harness_failure(
    client, monkeypatch, entrance, reasons, code, status,
):
    stream = Stream([
        text("partial answer"), stop("end_turn"), *(stop(reason) for reason in reasons),
        {"metadata": {"usage": {"outputTokens": 8}}},
    ])
    data = client_for(stream)
    monkeypatch.setattr(chat, "data_client", lambda _ws=None: data)
    monkeypatch.setattr(invoke, "data_client", lambda _ws=None: data)
    agent = create_agent()

    response = invoke_endpoint(client, entrance, agent.id)

    if entrance in {"chat", "public-stream"}:
        assert response.status_code == 200  # status was sent before the stream failed
        events = sse_events(response)
        assert [event["event"] for event in events] == ["meta", "delta", "error"]
        assert events[1]["data"] == {"text": "partial answer"}
        error = events[-1]["data"]
    else:
        assert response.status_code == status
        error = response.json()
    assert error["code"] == code
    assert error["message"].startswith("Harness execution")
    assert error["detail"] == {"stop_reason": reasons[-1]}
    assert stream.drained and stream.closed == 1
    data.invoke_harness.assert_called_once()
    data.invoke_agent_runtime.assert_not_called()
    if entrance == "chat":
        history = client.get(
            f"/api/chat/{agent.id}/history", params={"session_id": SID},
        ).json()["messages"]
        assert [(entry["role"], entry["text"]) for entry in history] == [
            ("user", "hello"), ("agent", "partial answer"), ("error", error["message"]),
        ]


@pytest.mark.parametrize("entrance", ["chat", "public-stream"])
def test_stream_endpoints_complete_normal_tool_cycles(client, monkeypatch, entrance):
    stream = Stream([
        {"contentBlockStart": {"start": {"toolUse": {
            "name": "kb_search", "toolUseId": "tool-1",
        }}}},
        stop("tool_use"),
        {"contentBlockStart": {"start": {"toolResult": {"toolUseId": "tool-1"}}}},
        stop("tool_result"), text("final answer"), stop("end_turn"),
        {"metadata": {"usage": {}}},
    ])
    data = client_for(stream)
    monkeypatch.setattr(chat, "data_client", lambda _ws=None: data)
    agent = create_agent()

    response = invoke_endpoint(client, entrance, agent.id)

    assert response.status_code == 200
    events = sse_events(response)
    assert [event["event"] for event in events] == ["meta", "tool", "delta", "done"]
    assert events[1]["data"] == {"name": "kb_search", "id": "tool-1"}
    assert events[2]["data"] == {"text": "final answer"}
    assert stream.drained and stream.closed == 1


@pytest.mark.parametrize("entrance", ["chat", "public-stream"])
def test_empty_stream_is_an_sse_error_without_done(client, monkeypatch, entrance):
    stream = Stream([])
    monkeypatch.setattr(chat, "data_client", lambda _ws=None: client_for(stream))
    response = invoke_endpoint(client, entrance, create_agent().id)
    events = sse_events(response)
    assert [event["event"] for event in events] == ["meta", "error"]
    assert events[-1]["data"]["code"] == "harness.incomplete_response"
    assert events[-1]["data"]["detail"] == {"stop_reason": None}
    assert stream.closed == 1


def test_shared_chat_generator_closes_upstream_on_disconnect(monkeypatch):
    stream = Stream([text("partial"), text("rest"), stop("end_turn")])
    monkeypatch.setattr(chat, "data_client", lambda _ws=None: client_for(stream))
    events = chat.chat_stream(create_agent(), "hello", session_id=SID, workspace=ws_ctx())
    assert next(events)["event"] == "meta"
    assert next(events)["event"] == "delta"
    events.close()
    assert stream.closed == 1 and not stream.drained


def test_chat_stream_keeps_invocation_scoped_gateway_identity(monkeypatch):
    stream = Stream([text("answer"), stop("end_turn")])
    data = client_for(stream)
    monkeypatch.setattr(chat, "data_client", lambda _ws=None: data)
    tools = [{"name": "approved-gateway", "type": "remote_mcp"}]
    authenticated_tools = MagicMock(return_value=tools)
    monkeypatch.setattr(hc, "user_authenticated_tools", authenticated_tools)
    workspace = ws_ctx()
    events = list(chat.chat_stream(
        create_agent(), "hello", session_id=SID, actor_id="agent__operator",
        runtime_user_id="operator", gateway_access_token="test-token", workspace=workspace,
    ))
    assert events[-1]["event"] == "done"
    authenticated_tools.assert_called_once_with({}, workspace.resources, "test-token")
    assert data.invoke_harness.call_args.kwargs == {
        "harnessArn": ARN, "runtimeSessionId": SID, "actorId": "agent__operator",
        "runtimeUserId": "operator", "tools": tools,
        "messages": [{"role": "user", "content": [{"text": "hello"}]}],
    }
    assert stream.closed == 1
