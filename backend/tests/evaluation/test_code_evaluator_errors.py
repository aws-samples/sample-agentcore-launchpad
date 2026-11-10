"""N17-obs328 — a code evaluator that did not score is an error row, never a 0.0.

Global: the Lambda answers ``{label (required), value?, explanation?}`` or ``{errorCode,
errorMessage}``. An unhandled exception or a Lambda timeout comes back as ``FunctionError``
with ``{errorMessage, errorType}`` and no ``errorCode``; before this fix all of them became
``score 0.0`` — a real-looking FAIL averaged into the run score. Replayed over a real corpus
session (``code_eval_support``) through the production engine.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.strands_eval import code_evaluators
from app.services.strands_eval.results import aggregate_scores
from tests.evaluation.code_eval_support import TURN_TRACES, InProcessLambda, run_corpus


def _answering(body: Any, function_error: str | None = None) -> InProcessLambda:
    return InProcessLambda(lambda event, context: body, function_error=function_error)


def _only(records: list[dict[str, Any]]) -> dict[str, Any]:
    assert len(records) == 1, records
    return records[0]


@pytest.mark.parametrize(("body", "function_error", "error_type", "message"), [
    ({"errorMessage": "boom", "errorType": "KeyError", "stackTrace": []}, "Unhandled",
     "lambda_function_error", "KeyError: boom"),
    ({"errorMessage": "2026-10-23T09:00:00Z abc Task timed out after 3.00 seconds"},
     "Unhandled", "lambda_timeout", "Task timed out after 3.00 seconds"),
    ({"errorType": "Sandbox.Timedout", "errorMessage": "RequestId: x Error: Task timed out"},
     "Unhandled", "lambda_timeout", "Task timed out"),
    ({"explanation": "forgot the label", "value": 1.0}, None, "missing_label", "label"),
    ({"label": "  ", "value": 1.0}, None, "missing_label", "label"),
    ({"errorCode": "VALIDATION_FAILED", "errorMessage": "no tool spans"}, None,
     "VALIDATION_FAILED", "no tool spans"),
    ({"label": "Pass", "value": "high"}, None, "invalid_value", "'high'"),
    ({"label": "Pass", "value": True}, None, "invalid_value", "True"),
    (["not", "an", "object"], None, "invalid_response", "list"),
])
def test_an_unscored_unit_is_a_typed_error_row(body, function_error, error_type, message):
    client = _answering(body, function_error)
    row = _only(run_corpus("SESSION", client))
    assert (row["score"], row["label"], row["error_type"]) == (None, None, error_type)
    assert message in row["error_message"]
    assert len(client.events) == 1  # a Lambda failure is never retried as "transient"
    assert aggregate_scores([row]) == []


def test_every_turn_of_a_timed_out_trace_evaluator_is_an_error_not_a_zero():
    client = _answering({"errorMessage": "Task timed out after 30.00 seconds"}, "Unhandled")
    records = run_corpus("TRACE", client)
    assert [(r["trace_id"], r["score"], r["error_type"]) for r in records] == [
        (t, None, "lambda_timeout") for t in TURN_TRACES]
    assert len(client.events) == 3


def test_a_label_without_a_value_keeps_the_label_and_scores_none():
    row = _only(run_corpus("SESSION", _answering({"label": "Pass", "explanation": "ok"})))
    assert (row["score"], row["label"], row["explanation"], row["error_type"]) == (
        None, "Pass", "ok", None)
    assert aggregate_scores([row]) == []  # never dragged to 0 in the mean


def test_a_valued_answer_is_unchanged():
    row = _only(run_corpus("SESSION", _answering({"label": "Fail", "value": 0.25,
                                                  "explanation": "x"})))
    assert (row["score"], row["label"], row["error_type"]) == (0.25, "Fail", None)


def test_an_sdk_error_response_with_null_success_fields_is_typed_by_its_code():
    """``EvaluatorOutput(errorCode=…).model_dump()`` also carries ``label: None`` etc."""
    body = {"value": None, "label": None, "explanation": None, "errorCode": "NO_TRACE",
            "errorMessage": "target trace missing"}
    row = _only(run_corpus("SESSION", _answering(body)))
    assert (row["score"], row["error_type"], row["error_message"]) == (
        None, "NO_TRACE", "target trace missing")


def test_a_payload_over_the_lambda_limit_is_an_error_row_without_an_invoke(monkeypatch):
    monkeypatch.setattr(code_evaluators, "PAYLOAD_LIMIT_BYTES", 1000)
    client = _answering({"label": "Pass", "value": 1.0})
    row = _only(run_corpus("SESSION", client))
    assert (row["score"], row["error_type"]) == (None, "payload_too_large")
    assert client.events == []
