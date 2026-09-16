"""Synthetic public-handler regressions for Strands wrappers and Bedrock provider spans."""

import copy
import json

import pytest

from app.assistant.lambda_runtime import handler
from tests.test_evaluation_assets import _event, _model_turn, _span, _tool, _turn_log

EXACT = {
    "version": 1,
    "checks": [{"id": "answer", "type": "output_exact", "text": "amber"}],
}


def _tool_history(log):
    log["body"]["input"]["messages"] = [
        {"role": "system", "content": {"content": "Use the supplied context."}},
        {"role": "user", "content": {"content": "What is the selected colour?"}},
        {"role": "tool", "content": {"content": "Synthetic lookup result."}},
    ]


def _pair():
    wrapper, log = _model_turn("t1", "wrapper", "amber", start=100)
    wrapper["attributes"]["gen_ai.system"] = "strands-agents"
    wrapper["endTimeUnixNano"] = 200
    log["timeUnixNano"] = 195
    _tool_history(log)
    provider = _span(
        "t1", "provider", "chat synthetic-model",
        {"gen_ai.operation.name": "chat", "gen_ai.system": "aws.bedrock",
         "gen_ai.response.finish_reasons": ["end_turn"]},
        start=110,
    )
    provider.update(parentSpanId="wrapper", endTimeUnixNano=190)
    return wrapper, provider, log


def _evaluate(docs, level="SESSION", rules=EXACT):
    target = {"traceIds": ["t1"]} if level == "TRACE" else None
    return handler.evaluate(rules, _event(level, list(docs), target=target))


def _assert_technical_error(result):
    assert "errorCode" in result, result
    assert result["errorCode"] != "INTERNAL", result
    assert "label" not in result and "value" not in result


@pytest.mark.parametrize("level", ["SESSION", "TRACE"])
def test_model_tool_history_does_not_hide_current_assistant_output(level):
    span, log = _model_turn("t1", "model", "amber")
    _tool_history(log)
    assert _evaluate([span, log], level)["label"] == "PASS"
    log["body"]["output"]["messages"][0]["content"]["message"] = json.dumps([{"text": "blue"}])
    assert _evaluate([span, log], level)["label"] == "FAIL"


def test_pure_tool_output_is_never_an_assistant_answer():
    tool = _tool("t1", "lookup", "weather", 100)
    tool[1]["body"]["output"]["messages"][0]["content"]["message"] = json.dumps([{"text": "amber"}])
    assert _evaluate(tool)["errorCode"] == "NO_MODEL_TURN"
    empty_model = _span("t1", "model", "chat", {"gen_ai.operation.name": "chat"}, start=110)
    assert _evaluate([*tool, empty_model])["errorCode"] == "NO_OUTPUT"


def test_pure_tool_log_attached_to_a_chat_span_is_still_not_an_answer():
    span = _span("t1", "model", "chat", {"gen_ai.operation.name": "chat"}, start=100)
    log = _tool("t1", "model", "weather", 100)[1]
    # A tool result has a tool response id, without a canonical model finish reason.
    log["body"]["output"]["messages"][0]["content"]["message"] = json.dumps([{"text": "amber"}])
    assert _evaluate([span, log])["errorCode"] == "NO_OUTPUT"


@pytest.mark.parametrize("level", ["SESSION", "TRACE"])
def test_direct_strands_wrapper_and_bedrock_provider_use_current_wrapper_output(level):
    docs = _pair()
    assert _evaluate(docs, level)["label"] == "PASS"
    # Neither input order nor the provider's later start makes it a separate answer.
    assert _evaluate(list(reversed(docs)), level)["label"] == "PASS"


def test_identityless_lambda_callback_uses_the_same_wrapper_evidence(monkeypatch):
    monkeypatch.setattr(handler, "_RULES", {
        "version": 1, "evaluators": {"synthetic_reference_response": EXACT},
    })
    event = _event("SESSION", list(_pair()))
    del event["evaluatorId"], event["evaluatorName"]
    assert handler.lambda_handler(event, None)["label"] == "PASS"


@pytest.mark.parametrize("finish,error", [
    ("length", "TRUNCATED"), ("max_tokens", "TRUNCATED"),
    ("tool_use", "INCOMPLETE"), ("tool_calls", "INCOMPLETE"),
    ("unrecognized-finish", "UNKNOWN_FINISH"),
])
def test_provider_finish_cannot_be_overridden_by_wrapper_stop(finish, error):
    wrapper, provider, log = _pair()
    provider["attributes"]["gen_ai.response.finish_reasons"] = [finish]
    result = _evaluate([wrapper, provider, log])
    assert result["errorCode"] == error
    assert "label" not in result


@pytest.mark.parametrize("finish,error", [("length", "TRUNCATED"), ("tool_use", "INCOMPLETE")])
def test_wrapper_finish_cannot_be_overridden_by_provider_stop(finish, error):
    wrapper, provider, log = _pair()
    log["body"]["output"]["messages"][0]["content"]["finish_reason"] = finish
    assert _evaluate([wrapper, provider, log])["errorCode"] == error


@pytest.mark.parametrize("source", ["completion", "log"])
def test_conflicting_wrapper_and_provider_outputs_are_a_technical_error(source):
    wrapper, provider, log = _pair()
    docs = [wrapper, provider, log]
    if source == "completion":
        provider["attributes"]["gen_ai.completion"] = "blue"
    else:
        docs.append(_turn_log("t1", "provider", json.dumps([{"text": "blue"}]), time=185))
    assert _evaluate(docs)["errorCode"] == "CONFLICTING_OUTPUT"


def test_equal_provider_and_wrapper_output_copies_do_not_conflict():
    wrapper, provider, log = _pair()
    provider["attributes"]["gen_ai.completion"] = "amber"
    assert _evaluate([wrapper, provider, log])["label"] == "PASS"


@pytest.mark.parametrize("relationship", ["no_parent", "other_parent", "cross_trace"])
def test_unrelated_provider_never_borrows_wrapper_output(relationship):
    wrapper, provider, log = _pair()
    if relationship == "no_parent":
        provider.pop("parentSpanId")
    elif relationship == "other_parent":
        provider["parentSpanId"] = "different-wrapper"
    else:
        provider["traceId"] = "t2"
    assert _evaluate([wrapper, provider, log])["errorCode"] == "NO_OUTPUT"


def test_multiple_direct_provider_children_are_not_collapsed_into_one_answer():
    wrapper, provider, log = _pair()
    second = copy.deepcopy(provider)
    second.update(spanId="second-provider", startTimeUnixNano=120, endTimeUnixNano=180)
    assert _evaluate([wrapper, provider, second, log])["errorCode"] == "NO_OUTPUT"


@pytest.mark.parametrize("target,field,value", [
    ("wrapper", "startTimeUnixNano", None),
    ("wrapper", "endTimeUnixNano", None),
    ("wrapper", "endTimeUnixNano", 99),
    ("provider", "startTimeUnixNano", None),
    ("provider", "endTimeUnixNano", None),
    ("provider", "endTimeUnixNano", 109),
    ("provider", "endTimeUnixNano", 201),
    ("provider", "startTimeUnixNano", 201),
])
def test_unverifiable_or_invalid_nested_time_bounds_fail_closed(target, field, value):
    wrapper, provider, log = _pair()
    doc = wrapper if target == "wrapper" else provider
    if value is None:
        doc.pop(field)
    else:
        doc[field] = value
    _assert_technical_error(_evaluate([wrapper, provider, log]))


@pytest.mark.parametrize("root_answer", [False, True])
def test_input_history_only_never_supplies_the_wrapper_answer(root_answer):
    wrapper, provider, log = _pair()
    log["body"].pop("output")
    log["body"]["input"]["messages"].append({
        "role": "assistant", "content": {"message": json.dumps([{"text": "amber"}]),
                                       "finish_reason": "end_turn"},
    })
    docs = [wrapper, provider, log]
    if root_answer:
        root = _span("t1", "root-agent", "agent", {
            "gen_ai.operation.name": "invoke_agent", "gen_ai.completion": "amber",
        }, start=1)
        root["endTimeUnixNano"] = 300
        docs.insert(0, root)
    assert _evaluate(docs)["errorCode"] == "NO_OUTPUT"


def test_embedded_adot_span_events_supply_current_output_without_changing_scope():
    wrapper, provider, log = _pair()
    nested = copy.deepcopy(log)
    nested.pop("traceId")
    nested["span_id"] = nested.pop("spanId")
    nested["time_unix_nano"] = nested.pop("timeUnixNano")
    wrapper["span_events"] = [nested]
    assert _evaluate([wrapper, provider])["label"] == "PASS"
    provider["attributes"]["gen_ai.response.finish_reasons"] = ["length"]
    assert _evaluate([wrapper, provider])["errorCode"] == "TRUNCATED"


def test_provider_dropped_evidence_is_not_hidden_by_complete_wrapper():
    wrapper, provider, log = _pair()
    provider["droppedAttributesCount"] = 1
    assert _evaluate([wrapper, provider, log])["errorCode"] == "TRUNCATED_EVIDENCE"


def test_prior_real_answers_remain_in_whole_session_negative_checks():
    prior = _model_turn("t1", "earlier-model", "blue", start=10)
    rules = {"version": 1, "checks": [
        {"id": "no_blue", "type": "output_not_contains", "text": "blue"},
    ]}
    assert _evaluate([*prior, *_pair()], rules=rules)["label"] == "FAIL"
