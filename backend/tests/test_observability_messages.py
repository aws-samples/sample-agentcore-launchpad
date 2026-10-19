"""Synthetic content-log fixtures; no production prompts or tool data."""

import json

import pytest

from app.core.db import SessionLocal
from app.services import observability as obs
from app.services import observability_messages as messages

from .conftest import ws_ctx


def _event(name, body, *, span="model", timestamp=0, **fields):
    return {
        "spanId": span, "eventName": name, "body": body,
        "timeUnixNano": 0, "observedTimeUnixNano": timestamp, **fields,
    }


def _rows(*events):
    return [{"@message": json.dumps(event)} for event in events]


def _call(tool_id="call-1", value=None):
    return {
        "type": "function", "id": tool_id,
        "function": {"name": "lookup", "arguments": value or {"item": "sample"}},
    }


def _use(tool_id="call-1", value=None):
    return {"toolUse": {
        "toolUseId": tool_id, "name": "lookup", "input": value or {"item": "sample"},
    }}


def _result(tool_id="call-1", text="found", **fields):
    return {"toolResult": {
        "toolUseId": tool_id, "content": [{"text": text}], **fields,
    }}


def _blocks(entry, side):
    return [block for message in entry[side] for block in message["blocks"]]


def test_provider_history_and_current_choice_have_correct_sides_and_order():
    events = [
        _event("gen_ai.system.message", {"content": [{"text": "instructions"}]}, timestamp=1),
        _event("gen_ai.user.message", {"content": "question"}, timestamp=2),
        _event("gen_ai.assistant.message", {
            "content": json.dumps([{"text": "previous answer"}, _use()]),
            "tool_calls": [_call(value=json.dumps({"item": "sample"}))],
        }, timestamp=3),
        _event("gen_ai.tool.message", {"id": "call-1", "content": [{"text": "found"}]},
               timestamp=4),
        _event("gen_ai.user.message", {"content": [_result()]}, timestamp=5),
        # Identical text in two real turns must not be deduplicated.
        _event("gen_ai.user.message", {"content": "question"}, timestamp=6),
        _event("gen_ai.choice", {
            "message": {"content": [{"text": "current answer"}]},
            "finish_reason": "end_turn",
        }, timestamp=7),
        _event("gen_ai.choice", {
            "message": {"role": "assistant", "tool_calls": [_call()]},
            "finish_reason": "tool_use",
        }, span="earlier-model", timestamp=1),
    ]
    parsed = obs.parse_message_events(_rows(*reversed(events)))
    current = parsed["model"]
    assert [m["role"] for m in current["input"]] == [
        "system", "user", "assistant", "tool", "user",
    ]
    assert [b["text"] for b in _blocks(current, "input") if b["type"] == "text"] == [
        "instructions", "question", "previous answer", "question",
    ]
    assert [b["type"] for b in _blocks(current, "input")].count("tool_use") == 1
    assert [b["type"] for b in _blocks(current, "input")].count("tool_result") == 1
    assert current["output"] == [{
        "role": "assistant", "finish_reason": "end_turn",
        "blocks": [{"type": "text", "text": "current answer"}],
    }]
    assert parsed["earlier-model"]["output"][0]["finish_reason"] == "tool_use"
    assert _blocks(parsed["earlier-model"], "output")[0]["name"] == "lookup"
    assert "_tool_key" not in json.dumps(parsed)


def test_tool_dedup_uses_id_and_full_content_and_keeps_richer_status():
    prefix = "x" * 600
    events = [
        _event("gen_ai.tool.message", {"id": "call-1", "content": [{"text": prefix + "A"}]}),
        _event("gen_ai.user.message", {"content": [
            _result(text=prefix + "A", status="success"),
            _result(text=prefix + "B"),  # identical display prefix, distinct content
            _result(tool_id="call-2", text=prefix + "A"),  # distinct call
            {"toolResult": {"content": [{"text": "no id"}]}},
            {"toolResult": {"content": [{"text": "no id"}]}},
        ]}),
    ]
    blocks = _blocks(obs.parse_message_events(_rows(*events))["model"], "input")
    assert len(blocks) == 5
    assert blocks[0]["status"] == "success"
    assert all(len(block["text"]) <= 500 for block in blocks)


def test_strands_tool_result_beside_message_survives_without_duplicate_output():
    result = _result(status="success")
    body = {"output": {"messages": [
        {"role": "assistant", "content": {
            "message": json.dumps([{"text": "checking"}, _use()]),
            "tool.result": json.dumps([result]),
        }},
        {"role": "assistant", "content": json.dumps([result])},
        {"role": "assistant", "content": "checking"},
    ]}}
    parsed = obs.parse_message_events(_rows(_event("strands.telemetry.tracer", body)))["model"]
    assert [b["type"] for b in _blocks(parsed, "output")] == [
        "text", "tool_use", "tool_result", "text",
    ]
    assert _blocks(parsed, "output")[2]["status"] == "success"


@pytest.mark.parametrize("serialized", [False, True])
def test_envelopes_accept_decoded_or_serialized_messages_and_plain_native_text(serialized):
    inputs = [{"role": "user", "content": [{"text": "hello"}]}]
    outputs = [{"role": "assistant", "content": "native completion"}]
    encode = json.dumps if serialized else lambda value: value
    event = _event("sdk.content", {
        "input": {"messages": encode(inputs)}, "output": {"messages": encode(outputs)},
    })
    parsed = obs.parse_message_events(_rows(event))["model"]
    assert _blocks(parsed, "input") == [{"type": "text", "text": "hello"}]
    assert _blocks(parsed, "output") == [{"type": "text", "text": "native completion"}]


def test_full_envelope_wins_per_side_and_all_event_provenance_survives():
    events = [
        _event("gen_ai.user.message", {"content": "event input"},
               attributes={"session.id": "event-session"}),
        _event("sdk.content", {"input": {"messages": [
            {"role": "user", "content": "full input"},
        ]}}, resource={"attributes": {"session.id": "envelope-session"}}),
        _event("gen_ai.user.message", {"content": "later input"},
               resource={"session.id": "private-session"}),
        _event("sdk.content", {"input": {"messages": [
            {"role": "user", "content": "duplicate envelope"},
        ]}}, attributes={"session.id": "other-session"}),
        _event("gen_ai.choice", {"message": {"content": "current output"}}),
    ]
    parsed = obs.parse_message_events(_rows(*events))["model"]
    assert parsed["session_ids"] == [
        "event-session", "envelope-session", "private-session", "other-session",
    ]
    assert _blocks(parsed, "input") == [{"type": "text", "text": "full input"}]
    assert _blocks(parsed, "output") == [{"type": "text", "text": "current output"}]


def test_timestamp_fallbacks_and_attribute_event_name_are_supported():
    rows = _rows(
        _event(None, {"content": "last"}, attributes={"event.name": "gen_ai.user.message"}),
        _event("gen_ai.user.message", {"content": "middle"}),
        _event("gen_ai.user.message", {"content": "first"},
               timeUnixNano="1700000000000000000"),
    )
    rows[0]["@timestamp"] = "2023-11-14 22:13:22.000"
    rows[1]["@timestamp"] = "2023-11-14T22:13:21Z"
    entry = obs.parse_message_events(rows)["model"]
    assert [b["text"] for b in _blocks(entry, "input")] == ["first", "middle", "last"]


def test_malformed_nested_content_is_readable_and_does_not_discard_other_blocks():
    event = _event("gen_ai.choice", {
        "finish_reason": "end_turn",
        "message": {"content": [
            {"toolUse": "bad"}, {"toolResult": 5}, {"unknown": "value"},
            {"text": "answer"},
        ], "tool_calls": [None, {"function": []}]},
    })
    parsed = obs.parse_message_events([
        {"@message": "not-json"}, {"@message": "[]"}, *_rows(event),
        *_rows(_event("gen_ai.choice", {"message": "plain answer"}, span="plain")),
        *_rows(_event(["malformed", "name"], {"content": "ignore"})),
    ])
    blocks = _blocks(parsed["model"], "output")
    assert [b["type"] for b in blocks] == ["other", "other", "other", "text", "other", "other"]
    assert blocks[3]["text"] == "answer"
    assert _blocks(parsed["plain"], "output") == [{"type": "text", "text": "plain answer"}]


def test_span_budget_ignores_logs_and_keeps_late_output_and_provenance():
    events = [
        _event("ordinary.log", {"message": "noise"}, span=f"log-{i}") for i in range(70)
    ]
    events += [
        _event("gen_ai.user.message", {"content": "input"}, span=f"span-{i}")
        for i in range(messages.MESSAGE_SPANS_CAP + 1)
    ]
    events += [
        _event("gen_ai.user.message", {"content": "same"},
               span="span-0", attributes={"session.id": f"session-{i}"})
        for i in range(messages.MESSAGES_PER_SIDE + 1)
    ]
    events += [
        _event("gen_ai.choice", {"message": {"content": "late output"}},
               span=f"span-{i}", attributes={"session.id": "late-session"})
        for i in (0, messages.MESSAGE_SPANS_CAP - 1)
    ]
    parsed = obs.parse_message_events(_rows(*events))
    assert len(parsed) == messages.MESSAGE_SPANS_CAP
    assert len(parsed["span-0"]["input"]) == messages.MESSAGES_PER_SIDE
    assert len(parsed["span-0"]["session_ids"]) == messages.MESSAGES_PER_SIDE + 2
    for span in ("span-0", f"span-{messages.MESSAGE_SPANS_CAP - 1}"):
        assert _blocks(parsed[span], "output")[0]["text"] == "late output"
        assert "late-session" in parsed[span]["session_ids"]


def test_content_provenance_includes_unrenderable_events_but_ignores_ordinary_logs():
    events = [
        _event("gen_ai.choice", None, attributes={"session.id": "private-choice"}),
        _event("sdk.content", {"input": {"messages": []}},
               resource={"attributes": {"session.id": "private-envelope"}}),
        _event(None, None, attributes={
            "event.name": "gen_ai.user.message", "session.id": "private-history",
        }),
        _event("ordinary.log", {"message": "noise"},
               attributes={"session.id": "unrelated-session"}),
    ]
    assert messages.message_event_session_ids([
        {"@message": "not-json"}, {"@message": "[]"}, *_rows(*events),
    ]) == {"private-choice", "private-envelope", "private-history"}


def test_message_block_and_text_limits_apply_to_decoded_arrays():
    event = _event("sdk.content", {"input": {"messages": [
        {"role": "user", "content": [{"text": "x" * 5000}] * 25} for _ in range(25)
    ]}})
    entry = obs.parse_message_events(_rows(event))["model"]
    assert len(entry["input"]) == messages.MESSAGES_PER_SIDE
    for message in entry["input"]:
        assert len(message["blocks"]) == messages.BLOCKS_PER_MESSAGE
        assert all(len(block["text"]) == messages.MESSAGE_TEXT_CAP for block in message["blocks"])


def test_message_query_filters_content_and_sorts_before_bounded_limit():
    query = obs.q_trace_message_events("a" * 32)
    # CloudWatch's discovered array fields do not satisfy ispresent; jsonParse
    # map access was verified to retain Strands envelopes as well as GenAI events.
    assert "jsonParse(@message) as record" in query
    assert "ispresent(record.body.input.messages)" in query
    assert "ispresent(record.body.output.messages)" in query
    assert "record.attributes.`event.name` in" in query
    for event in messages.GEN_AI_MESSAGE_EVENTS:
        assert json.dumps(event) in query
    assert query.index("| filter") < query.index("| sort @timestamp asc") < query.index("| limit")


@pytest.mark.parametrize("events_available", [False, True])
def test_trace_mcp_fallback_is_same_span_and_explicit_events_win_per_side(
    monkeypatch, events_available,
):
    attrs = {
        "gen_ai.tool.name": "lookup", "gen_ai.tool.call.arguments": "{}",
        "gen_ai.tool.call.result": "CallToolResult(content='found', isError=False)",
        "session.id": "tool-session",
    }
    spans = [{
        "spanId": span, "name": "tools/call" if span != "http" else "POST",
        "startTimeUnixNano": 1, "endTimeUnixNano": 2, "durationNano": 1,
        "attributes": attrs if span == "tool" else {},
        "resource": {"attributes": {"aws.log.group.names": "/aws/bedrock-agentcore/runtimes/r"}},
    } for span in ("tool", "http")]
    event = _event("sdk.content", {"output": {"messages": [
        {"role": "tool", "content": "explicit event result"},
    ]}}, span="tool")

    def query(queries, *args, **kwargs):
        if "spans" in queries:
            return {"spans": _rows(*spans)}
        if not events_available:
            raise RuntimeError("content log unavailable")
        return {"events": _rows(event)}

    monkeypatch.setattr(obs, "run_insights_queries", query)
    with SessionLocal() as db:
        result = obs.get_trace("e" * 32, "1h", db, ws_ctx(), force=True)
    by_span = {span["span_id"]: span for span in result["spans"]}
    tool = by_span["tool"]["messages"]
    assert _blocks(tool, "input") == [{"type": "tool_use", "name": "lookup", "input": "{}"}]
    expected = "explicit event result" if events_available else attrs["gen_ai.tool.call.result"]
    assert _blocks(tool, "output")[0]["text"] == expected
    assert by_span["http"]["messages"] is None
    assert result["meta"]["session_ids"] == ["tool-session"]


def test_attribute_fallback_shares_the_message_span_budget(monkeypatch):
    spans = [{
        "spanId": f"tool-{i}", "name": "tools/call",
        "startTimeUnixNano": i + 1, "endTimeUnixNano": i + 2, "durationNano": 1,
        "attributes": {
            "gen_ai.tool.name": "lookup", "gen_ai.tool.call.arguments": "{}",
            "gen_ai.tool.call.result": "found",
        },
    } for i in range(messages.MESSAGE_SPANS_CAP + 1)]
    monkeypatch.setattr(
        obs, "run_insights_queries", lambda *args, **kwargs: {"spans": _rows(*spans)},
    )
    with SessionLocal() as db:
        result = obs.get_trace("f" * 32, "1h", db, ws_ctx(), force=True)
    assert sum(bool(span["messages"]) for span in result["spans"]) == messages.MESSAGE_SPANS_CAP
    assert result["spans"][-1]["messages"] is None
