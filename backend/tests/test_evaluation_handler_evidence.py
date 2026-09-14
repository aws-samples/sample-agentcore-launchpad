"""SE-047 phase A — pure handler evidence matrix (no DB, no network, no app import).

Documents mirror what the installed Strands tracer and the bedrock_agentcore ADOT
serializer emit: model spans (``gen_ai.operation.name: chat``) carry serialized
content-block lists in ``gen_ai.choice.message`` / ADOT ``content.message``; agent spans
(``invoke_agent``) carry plain ``str(response)``; the new
``gen_ai.client.inference.operation.details`` event carries a serialized message list.
Every case below runs through ``lambda_handler`` with immutable in-memory rules."""

import json

import pytest

from app.assistant.lambda_runtime import handler

T1 = {"traceIds": ["t1"]}
LEAK = {"version": 1, "checks": [{"id": "leak", "type": "output_not_contains", "text": "amber"}]}
NO_TOOLS = {"version": 1, "checks": [{"id": "none", "type": "tool_count", "max": 0}]}
FORBID = {"version": 1, "checks": [{"id": "s", "type": "tool_set", "forbidden": ["shell"]}]}
SEQ = {"version": 1, "checks": [{"id": "s", "type": "tool_sequence", "mode": "exact",
                                 "tools": ["weather", "calendar"]}]}
RULES = {"version": 1, "evaluators": {"ev": LEAK, "tools": NO_TOOLS, "forbid": FORBID,
                                      "seq": SEQ}}


@pytest.fixture(autouse=True)
def rules(monkeypatch):
    monkeypatch.setattr(handler, "_RULES", json.loads(json.dumps(RULES)))


def blocks(*texts):
    return json.dumps([{"text": t} for t in texts])


def span(trace, sid, name, attrs=None, start=1, end=None, events=None, **extra):
    doc = {"traceId": trace, "spanId": sid, "name": name,
           "attributes": {"session.id": "s1", **(attrs or {})},
           "startTimeUnixNano": start, "endTimeUnixNano": start + 1 if end is None else end}
    if events is not None:
        doc["events"] = events
    doc.update(extra)
    return doc


def model_span(trace, sid, start, finish="end_turn", message=None, attrs=None, end=None):
    """Legacy tracer model span: gen_ai.choice with serialized content blocks."""
    ev = {"name": "gen_ai.choice", "attributes": {"finish_reason": finish}}
    if message is not None:
        ev["attributes"]["message"] = message
    return span(trace, sid, "chat", {"gen_ai.operation.name": "chat", **(attrs or {})},
                start, end=end, events=[ev])


def adot_turn(trace, sid, start, message, finish="end_turn", history=()):
    """Legacy tracer span + the ADOT conversation record the installed serializer builds."""
    inputs = [{"role": "user", "content": {"content": "hello"}}]
    inputs += [{"role": "assistant", "content": {"content": h}} for h in history]
    log = {"traceId": trace, "spanId": sid, "timeUnixNano": start + 1,
           "attributes": {"event.name": "strands", "session.id": "s1"},
           "body": {"input": {"messages": inputs},
                    "output": {"messages": [{"role": "assistant",
                                             "content": {"message": message,
                                                         "finish_reason": finish}}]}}}
    return [span(trace, sid, "chat", {"gen_ai.operation.name": "chat"}, start), log]


def agent_span(trace, sid, start, text, finish="end_turn"):
    """Legacy tracer end_agent_span: plain str(response)."""
    return span(trace, sid, "invoke_agent kid", {"gen_ai.operation.name": "invoke_agent"}, start,
                events=[{"name": "gen_ai.choice",
                         "attributes": {"message": text, "finish_reason": finish}}])


def details_span(trace, sid, start, parts, finish="end_turn", plain=False):
    """New tracer: gen_ai.client.inference.operation.details with gen_ai.output.messages."""
    op = "invoke_agent" if plain else "chat"
    msg = [{"role": "assistant", "finish_reason": finish, "parts": parts}]
    return span(trace, sid, op, {"gen_ai.operation.name": op}, start, events=[
        {"name": "gen_ai.client.inference.operation.details",
         "attributes": {"gen_ai.output.messages": json.dumps(msg)}}])


def tool(trace, sid, name, start):
    return span(trace, sid, f"execute_tool {name}",
                {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name}, start)


def event(level, spans, refs=None, target=None, name="ev"):
    return {"schemaVersion": "1.0", "evaluatorId": "x", "evaluatorName": name,
            "evaluationLevel": level, "evaluationInput": {"sessionSpans": spans},
            "evaluationReferenceInputs": refs or [], "evaluationTarget": target}


def run(spans, level="SESSION", name="ev", **kw):
    return handler.lambda_handler(event(level, spans, name=name, **kw), None)


# ---------------------------------------------------------------- positives

def test_positive_legacy_adot_new_details_and_tool_trajectory():
    assert run(adot_turn("t1", "m", 10, blocks("It is sunny")))["label"] == "PASS"
    assert run([model_span("t1", "m", 10, message=blocks("fine"))])["label"] == "PASS"
    assert run([agent_span("t1", "a", 10, "[Notice] safe")])["label"] == "PASS"
    assert run([agent_span("t1", "a", 10, '{"example": not JSON, only prose')])["label"] == "PASS"
    assert run([details_span("t1", "m", 10, [{"type": "text", "content": "[Notice] safe"}])])[
        "label"] == "PASS"
    assert run([details_span("t1", "m", 10, [{"type": "text", "content": "ok"}], plain=True)])[
        "label"] == "PASS"
    # complete multi-step tool trajectory: tool_use turn → tool → final answer
    traj = [model_span("t1", "m1", 10, finish="tool_use",
                       message=json.dumps([{"toolUse": {"name": "weather"}}])),
            tool("t1", "x", "weather", 20)] + adot_turn("t1", "m2", 30, blocks("sunny"))
    out = run(traj, name="forbid")
    assert out["label"] == "PASS", out
    assert run(traj + [tool("t1", "y", "calendar", 25)], name="seq")["label"] == "PASS"
    # exact duplicate span document counted once
    dup = traj + [dict(traj[1])]
    assert run(dup + [], name="tools")["label"] == "FAIL"  # one tool call, max 0
    # history excluded; multipart leak fails
    assert run(adot_turn("t1", "m", 10, blocks("Noted."), history=["amber"]))["label"] == "PASS"
    assert run(adot_turn("t1", "m", 10, blocks("amber", "and goodbye")))["label"] == "FAIL"
    # multiple complete TRACE targets score independently with their own references
    rr = {"version": 1, "checks": [{"id": "r", "type": "reference_response"}]}
    handler._RULES["evaluators"]["rr"] = rr
    spans = adot_turn("t1", "m1", 10, blocks("fine")) + adot_turn("t2", "m2", 20, blocks("ok"))
    refs = [{"context": {"spanContext": {"sessionId": "s1", "traceId": "t1"}},
             "expectedResponse": {"text": "fine"}},
            {"context": {"spanContext": {"sessionId": "s1", "traceId": "t2"}},
             "expectedResponse": {"text": "nope"}}]
    out = run(spans, "TRACE", name="rr", refs=refs, target={"traceIds": ["t1", "t2"]})
    assert out["label"] == "FAIL" and "r@t1=pass" in out["explanation"] and "r@t2=fail" in out[
        "explanation"]
    # a genuinely malformed serialized envelope on a model span is an error
    assert run([model_span("t1", "m", 10, message='[{"text": "amber"')])[
        "errorCode"] == "MALFORMED_OUTPUT"
    assert run(adot_turn("t1", "m", 10, "plain text under a model span"))[
        "errorCode"] == "MALFORMED_OUTPUT"


# ---------------------------------------------------------------- finish consistency

def test_every_finish_indication_must_agree_on_a_terminal_stop():
    # event end_turn + span finish_reasons [length]
    cut = model_span("t1", "m", 10, message=blocks("safe"),
                     attrs={"gen_ai.response.finish_reasons": ["length"]})
    assert run([cut])["errorCode"] == "TRUNCATED"
    # ADOT record end_turn + span [length]
    adot = adot_turn("t1", "m", 10, blocks("safe"))
    adot[0]["attributes"]["gen_ai.response.finish_reasons"] = ["length"]
    assert run(adot)["errorCode"] == "TRUNCATED"
    # gen_ai.completion + [length, stop]: a stop hidden behind a length is not complete
    both = span("t1", "m", "chat", {"gen_ai.completion": "safe",
                                    "gen_ai.response.finish_reasons": ["length", "stop"]}, 10)
    assert run([both])["errorCode"] == "TRUNCATED"
    # OTLP arrayValue finish reasons
    otlp = {"traceId": "t1", "spanId": "m", "name": "chat", "startTimeUnixNano": 10,
            "endTimeUnixNano": 11, "attributes": [
                {"key": "gen_ai.completion", "value": {"stringValue": "safe"}},
                {"key": "gen_ai.response.finish_reasons",
                 "value": {"arrayValue": {"values": [{"stringValue": "length"}]}}}]}
    assert run([otlp])["errorCode"] == "TRUNCATED"
    # a stop event next to a tool_use span indicator is a continuation, not complete
    mixed = model_span("t1", "m", 10, message=blocks("safe"),
                       attrs={"gen_ai.response.finish_reasons": ["tool_use"]})
    assert run([mixed])["errorCode"] == "INCOMPLETE"
    # finished span needs a valid end timestamp: absent / 0 / before start
    for end in (None, 0, 9):
        doc = model_span("t1", "m", 10, message=blocks("safe"), end=end)
        if end is None:
            doc.pop("endTimeUnixNano")
        assert run([doc])["errorCode"] == "INCOMPLETE", end
    assert run([model_span("t1", "m", 10, message=blocks("safe"), end=10)])["label"] == "PASS"


# ---------------------------------------------------------------- whole-scope negatives

def test_session_negative_rule_needs_every_turn_present_and_complete():
    early_missing = [model_span("t1", "m1", 10)] + adot_turn("t1", "m2", 20, blocks("bye"))
    out = run(early_missing)
    assert out["errorCode"] == "EVIDENCE_INCOMPLETE"
    early_cut = [model_span("t1", "m1", 10, finish="max_tokens", message=blocks("x"))] + \
        adot_turn("t1", "m2", 20, blocks("bye"))
    assert run(early_cut)["errorCode"] == "EVIDENCE_INCOMPLETE"
    # the earlier observed leak still fails; a final-only rule is described as such
    leak = adot_turn("t1", "m1", 10, blocks("amber!")) + adot_turn("t1", "m2", 20, blocks("bye"))
    out = run(leak)
    assert out["label"] == "FAIL" and "all 2 assistant turn(s)" in out["explanation"]
    handler._RULES["evaluators"]["final"] = {"version": 1, "checks": [
        {"id": "c", "type": "output_contains", "text": "bye"}]}
    out = run(leak, name="final")
    assert out["label"] == "PASS" and "final turn" in out["explanation"]
    # a normal tool trajectory (tool_use turn then answer) is complete scope, still PASS
    traj = [model_span("t1", "m1", 10, finish="tool_use",
                       message=json.dumps([{"toolUse": {"name": "weather"}}])),
            tool("t1", "x", "weather", 20)] + adot_turn("t1", "m2", 30, blocks("sunny"))
    assert run(traj)["label"] == "PASS"
    # a tool_use turn without any tool span is inconsistent evidence
    assert run([traj[0]] + traj[2:])["errorCode"] == "INCONSISTENT"


# ---------------------------------------------------------------- identity

def test_tool_and_document_identity():
    ok = adot_turn("t1", "m", 30, blocks("ok"))
    bare = [span("t1", "x", "execute_tool", {"gen_ai.operation.name": "execute_tool"}, 3)]
    assert run(bare + ok, name="tools")["errorCode"] == "UNKNOWN_TOOL"
    bare_name_only = [span("t1", "x", "execute_tool", {}, 3)]
    assert run(bare_name_only + ok, name="tools")["errorCode"] == "UNKNOWN_TOOL"
    # duplicate id with same attrs/name but different events (safe vs amber) / timestamps
    a = adot_turn("t1", "m", 10, blocks("safe"))
    b = adot_turn("t1", "m", 10, blocks("amber"))
    assert run(a + [b[1]])["errorCode"] == "CONFLICTING_DUPLICATE"
    shifted = dict(a[0], endTimeUnixNano=99)
    assert run(a + [shifted])["errorCode"] == "CONFLICTING_DUPLICATE"
    ev1 = model_span("t1", "m", 10, message=blocks("safe"))
    ev2 = model_span("t1", "m", 10, message=blocks("amber"))
    assert run([ev1, ev2])["errorCode"] == "CONFLICTING_DUPLICATE"
    # OTLP duplicate attribute keys shell → weather never last-write-win
    otlp_tool = {"traceId": "t1", "spanId": "x", "name": "execute_tool", "startTimeUnixNano": 3,
                 "endTimeUnixNano": 4, "attributes": [
                     {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}},
                     {"key": "gen_ai.tool.name", "value": {"stringValue": "shell"}},
                     {"key": "gen_ai.tool.name", "value": {"stringValue": "weather"}}]}
    assert run([otlp_tool] + ok, name="forbid")["errorCode"] == "CONFLICTING_DUPLICATE"
    # equal-start sequence ambiguity stays; counts still work
    tie = [tool("t1", "a", "weather", 3), tool("t1", "b", "calendar", 3)] + ok
    assert run(tie, name="seq")["errorCode"] == "AMBIGUOUS_ORDER"
    handler._RULES["evaluators"]["two"] = {"version": 1, "checks": [
        {"id": "c", "type": "tool_count", "min": 2, "max": 2}]}
    assert run(tie, name="two")["label"] == "PASS"


# ---------------------------------------------------------------- strict targets

def test_strict_target_presence_and_types():
    ok = adot_turn("t1", "m", 10, blocks("ok"))
    for bad in (0, False, "", []):
        out = run(ok, "TRACE", target={"traceIds": ["t1"], "spanIds": bad})
        assert out["errorCode"] == "BAD_TARGET", bad
    assert run(ok, "TRACE", target={"traceIds": [""]})["errorCode"] == "TARGET_UNRESOLVED"
    assert run(ok, "TRACE", target={"traceIds": [1]})["errorCode"] == "TARGET_UNRESOLVED"
    assert run(ok, "TRACE", target={"traceIds": ["t1"], "extra": 1})["errorCode"] == "BAD_TARGET"
    assert run(ok, "SESSION", target={})["errorCode"] == "BAD_TARGET"
    # a document without traceId can never satisfy a target
    noid = [dict(d, traceId="") for d in ok]
    assert run(noid, "TRACE", target={"traceIds": [""]})["errorCode"] == "TARGET_UNRESOLVED"
    assert run(ok, "TRACE", target={"traceIds": ["t1"]})["label"] == "PASS"
