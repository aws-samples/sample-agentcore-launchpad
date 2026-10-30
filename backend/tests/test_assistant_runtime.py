"""The architect turn against the self-built Harness runtime (plan R12): the
``InvokeAgentRuntime`` call carries the ``messages`` replay, ``runtimeSessionId`` and
``runtimeUserId``; the Converse-shaped SSE stream maps to the assistant's
``tool`` / ``tool_input`` / ``delta`` events; non-Converse stop reasons become the
``harness.execution_*`` errors the console already knows. Hermetic — no AWS."""

import json

import pytest

from app.assistant import runtime as arun
from app.core.errors import AppError

ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/arch-1"


class _Body:
    def __init__(self, events):
        self._raw = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()
        self.closed = False

    def iter_lines(self, chunk_size=None):
        return iter(self._raw.splitlines(keepends=True))

    def close(self):
        self.closed = True


class _Client:
    def __init__(self, events):
        self.events, self.calls, self.bodies = events, [], []

    def invoke_agent_runtime(self, **kw):
        self.calls.append(kw)
        body = _Body(self.events)
        self.bodies.append(body)
        return {"response": body, "contentType": "text/event-stream"}


def test_turn_sends_messages_and_maps_tool_input_and_text():
    events = [
        {"event": {"contentBlockStart": {"start": {"toolUse": {"name": "shell",
                                                                 "toolUseId": "t-1"}}}}},
        {"event": {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"command": "date"}'}}}}},
        {"event": {"contentBlockStop": {}}},
        {"event": {"contentBlockDelta": {"delta": {"text": "hello"}}}},
        {"event": {"messageStop": {"stopReason": "end_turn"}}},
    ]
    client = _Client(events)
    out = list(arun.stream_architect_turn(
        client, ARN, [{"role": "user", "content": [{"text": "hi"}]}],
        session_id="s" * 33, runtime_user_id="alice"))
    assert out == [{"event": "tool", "data": {"name": "shell", "id": "t-1"}},
                   {"event": "tool_input", "data": {"id": "t-1", "input": '{"command": "date"}'}},
                   {"event": "delta", "data": {"text": "hello"}}]
    call = client.calls[0]
    assert call["agentRuntimeArn"] == ARN
    assert call["runtimeUserId"] == "alice" and call["runtimeSessionId"] == "s" * 33
    payload = json.loads(call["payload"])
    assert payload["messages"][0]["role"] == "user" and payload["actor_id"] == "alice"
    assert client.bodies[0].closed


def test_tool_input_is_pieced_together_from_several_deltas_and_bare_events_are_accepted():
    # the SDK may emit the Converse event without the ``{"event": …}`` envelope
    events = [
        {"contentBlockStart": {"start": {"toolUse": {"name": "file_operations",
                                                     "toolUseId": "t-9"}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"path": '}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": '"/tmp/x"}'}}}},
        {"contentBlockStop": {}},
        {"contentBlockStop": {}},  # a text block closing: no pending tool → nothing
        {"messageStop": {"stopReason": "tool_use"}},
        {"contentBlockDelta": {"delta": {"text": "ok"}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]
    out = list(arun.stream_architect_turn(_Client(events), ARN, [],
                                          session_id="s" * 33, runtime_user_id="u"))
    assert out == [{"event": "tool", "data": {"name": "file_operations", "id": "t-9"}},
                   {"event": "tool_input", "data": {"id": "t-9", "input": '{"path": "/tmp/x"}'}},
                   {"event": "delta", "data": {"text": "ok"}}]


@pytest.mark.parametrize("stop, code", [
    ("timeout_exceeded", "harness.execution_timeout"),
    ("max_tokens", "harness.execution_limit"),
    ("Maximum iterations (12) reached", "harness.execution_limit"),
])
def test_non_terminal_stop_reasons_become_harness_errors(stop, code):
    client = _Client([{"event": {"messageStop": {"stopReason": stop}}}])
    with pytest.raises(AppError) as exc:
        list(arun.stream_architect_turn(client, ARN, [], session_id="s" * 33,
                                        runtime_user_id="u"))
    assert exc.value.code == code and exc.value.detail["stop_reason"] == stop
    assert exc.value.status_code == 502
    assert client.bodies[0].closed


def test_runtime_errors_surface_and_the_body_is_closed():
    client = _Client([{"runtimeClientError": {"message": "boom"}}])
    with pytest.raises(arun.RuntimeStreamError, match="boom"):
        list(arun.stream_architect_turn(client, ARN, [], session_id="s" * 33,
                                        runtime_user_id="u"))
    assert client.bodies[0].closed


SDK_ERROR_FRAME = {  # bedrock_agentcore/runtime/app.py::_stream_with_error_handling
    "error": "Read timeout on endpoint URL: \"https://bedrock-runtime.us-west-2…\"",
    "error_type": "ReadTimeoutError",
    "message": "An error occurred during streaming",
}


def test_the_sdk_error_frame_after_a_tool_step_is_a_runtime_stream_error_not_incomplete():
    """N-ARCH-obs18 (live 2026-10-16 ~05:00Z): the Harness loop re-raised after a tool
    step, the SDK closed the stream with its terminal error frame, and the turn was
    reported as ``harness.incomplete_response`` because the frame was skipped."""
    events = [
        {"event": {"contentBlockStart": {"start": {"toolUse": {"name": "skills",
                                                               "toolUseId": "t-1"}}}}},
        {"event": {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}}},
        {"event": {"contentBlockStop": {}}},
        {"event": {"messageStop": {"stopReason": "tool_use"}}},
        SDK_ERROR_FRAME,
    ]
    client = _Client(events)
    out = []
    with pytest.raises(arun.RuntimeStreamError) as exc:
        for event in arun.stream_architect_turn(client, ARN, [], session_id="s" * 33,
                                                runtime_user_id="u"):
            out.append(event)
    # the tool events before the frame still reached the consumer
    assert [e["event"] for e in out] == ["tool", "tool_input"]
    assert exc.value.error_type == "ReadTimeoutError"
    assert exc.value.detail["error_type"] == "ReadTimeoutError"
    assert "ReadTimeoutError" in str(exc.value) and "Read timeout on endpoint" in str(exc.value)
    assert "An error occurred during streaming" in str(exc.value)
    assert not isinstance(exc.value, AppError)
    assert client.bodies[0].closed


@pytest.mark.parametrize("frame", [
    SDK_ERROR_FRAME,
    {"error": "No entrypoint defined"},  # the SDK's one-JSON-body refusal
    {"error": "", "error_type": "ModelThrottledException", "message": "An error occurred"},
    {"event": {"error": "x", "error_type": "ValidationException", "message": "m"}},
])
def test_an_sdk_error_frame_as_the_only_payload_is_a_runtime_stream_error(frame):
    client = _Client([frame])
    with pytest.raises(arun.RuntimeStreamError) as exc:
        list(arun.stream_architect_turn(client, ARN, [], session_id="s" * 33,
                                        runtime_user_id="u"))
    inner = frame.get("event", frame)
    assert exc.value.error_type == inner.get("error_type")
    if inner.get("error_type"):
        assert inner["error_type"] in str(exc.value)
    if inner.get("error"):
        assert inner["error"] in str(exc.value)
    assert client.bodies[0].closed


def test_sdk_error_text_is_bounded_in_the_recorded_error():
    frame = {"error": "x" * 5000, "error_type": "ValidationException",
             "message": "An error occurred during streaming"}
    with pytest.raises(arun.RuntimeStreamError) as exc:
        list(arun.stream_architect_turn(_Client([frame]), ARN, [], session_id="s" * 33,
                                        runtime_user_id="u"))
    assert len(str(exc.value)) < arun.STREAM_ERROR_TEXT_MAX + 200
    assert "[+4400 chars]" in str(exc.value) and exc.value.detail["error_type"] == (
        "ValidationException")


def test_the_runtime_level_error_events_still_raise_without_an_error_type():
    client = _Client([{"internalServerException": {"message": "upstream 500"}}])
    with pytest.raises(arun.RuntimeStreamError, match="upstream 500") as exc:
        list(arun.stream_architect_turn(client, ARN, [], session_id="s" * 33,
                                        runtime_user_id="u"))
    assert exc.value.error_type is None and exc.value.detail == {}


def test_on_stream_receives_the_upstream_body_before_the_first_event():
    # (the on_stream hook fires before any line is read: a cancel can close a blocked body)
    seen: list = []
    client = _Client([{"event": {"contentBlockDelta": {"delta": {"text": "x"}}}}])
    out = list(arun.stream_architect_turn(
        client, ARN, [], session_id="s" * 33, runtime_user_id="u", on_stream=seen.append))
    assert out == [{"event": "delta", "data": {"text": "x"}}]
    assert seen == [client.bodies[0]]


def test_repr_fallback_events_are_decoded_like_json_ones():
    class Body(_Body):
        def __init__(self):
            text = "{'event': {'contentBlockDelta': {'delta': {'text': 'from repr'}}}}"
            self._raw = f"data: {json.dumps(text)}\n\n".encode()
            self.closed = False

    class Client(_Client):
        def invoke_agent_runtime(self, **kw):
            self.calls.append(kw)
            body = Body()
            self.bodies.append(body)
            return {"response": body, "contentType": "text/event-stream"}

    out = list(arun.stream_architect_turn(Client([]), ARN, [], session_id="s" * 33,
                                          runtime_user_id="u"))
    assert out == [{"event": "delta", "data": {"text": "from repr"}}]


def test_runtime_user_id_is_bounded_to_the_api_limit():
    client = _Client([{"event": {"contentBlockDelta": {"delta": {"text": "ok"}}}}])
    list(arun.stream_architect_turn(client, ARN, [], session_id="s" * 33,
                                    runtime_user_id="u" * 2000))
    assert len(client.calls[0]["runtimeUserId"]) == 1024


@pytest.mark.parametrize("events", [
    [],  # nothing at all
    [{"event": {"contentBlockStart": {"start": {"toolUse": {"name": "shell",
                                                           "toolUseId": "t"}}}}},
     {"event": {"contentBlockStop": {}}},
     {"event": {"messageStop": {"stopReason": "tool_use"}}}],  # cut after a tool step
])
def test_a_stream_without_reply_text_is_an_incomplete_response(events):
    client = _Client(events)
    with pytest.raises(AppError) as exc:
        list(arun.stream_architect_turn(client, ARN, [], session_id="s" * 33,
                                        runtime_user_id="u"))
    assert exc.value.code == "harness.incomplete_response" and exc.value.status_code == 502
    assert client.bodies[0].closed


def test_tool_input_whitespace_is_collapsed_for_the_transcript():
    events = [
        {"event": {"contentBlockStart": {"start": {"toolUse": {"name": "file_operations",
                                                                 "toolUseId": "t-0"}}}}},
        {"event": {"contentBlockDelta": {"delta": {"toolUse": {
            "input": '{"operation": "read",\n  '}}}}},
        {"event": {"contentBlockDelta": {"delta": {"toolUse": {"input": '"path": "x"}'}}}}},
        {"event": {"contentBlockStop": {}}},
        {"event": {"contentBlockDelta": {"delta": {"text": "Read it."}}}},
    ]
    out = list(arun.stream_architect_turn(_Client(events), ARN, [], session_id="s" * 33,
                                          runtime_user_id="u"))
    assert out[1]["data"]["input"] == '{"operation": "read", "path": "x"}'


def test_oversized_tool_input_is_bounded_with_a_visible_marker():
    big = "q" * (arun.TOOL_INPUT_MAX_CHARS + 40)
    events = [
        {"event": {"contentBlockStart": {"start": {"toolUse": {"name": "aws_knowledge",
                                                                 "toolUseId": "t-2"}}}}},
        {"event": {"contentBlockDelta": {"delta": {"toolUse": {"input": big}}}}},
        {"event": {"contentBlockStop": {}}},
        {"event": {"contentBlockDelta": {"delta": {"text": "done"}}}},
    ]
    out = list(arun.stream_architect_turn(_Client(events), ARN, [], session_id="s" * 33,
                                          runtime_user_id="u"))
    recorded = out[1]["data"]["input"]
    assert len(recorded) < len(big) and recorded.endswith("… [+40 chars]")
