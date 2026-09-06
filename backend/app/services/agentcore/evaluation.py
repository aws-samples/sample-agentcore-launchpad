"""Thin wrapper over the AgentCore Evaluation data-plane ``Evaluate`` API.

Explicit-client style (tests inject stubs). Shape per bedrock-agentcore
2024-02-28 (botocore 1.43.x): ``POST /evaluations/evaluate/{evaluatorId}`` takes
``evaluationInput.sessionSpans`` — the raw span records of ONE session as
CloudWatch stores them (1..20 000 documents) — and answers synchronously with
``evaluationResults[]`` (at most 10 per call). A partial failure is reported
inline on the result (``errorCode`` / ``errorMessage``), not as an exception,
so callers must render error rows rather than assume every result scored.

Only session-level scoring is wrapped here: ``evaluationTarget`` (trace/span
targeting) and ``evaluationReferenceInputs`` (ground truth) are deliberately not
exposed — the batch path (``app.evaluation.agentcore_eval``) owns those.
"""

from typing import Any

# The service caps a single response at 10 results; a single evaluator id
# returns one result per call today, but the cap is part of the contract.
MAX_RESULTS_PER_CALL = 10


def evaluate_session_spans(
    client: Any, *, evaluator_id: str, spans: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Score one session's spans with one evaluator; returns the raw results.

    ``client`` is the ``bedrock-agentcore`` data-plane client
    (``services.agentcore.client.data_client``). Each call is one judge model
    inference — callers sequence evaluators rather than fan out.
    """
    if not spans:
        raise ValueError("evaluate_session_spans needs at least one span")
    response = client.evaluate(
        evaluatorId=evaluator_id,
        evaluationInput={"sessionSpans": spans},
    )
    return list(response.get("evaluationResults") or [])


def normalize_result(raw: dict[str, Any], *, evaluator_id: str) -> dict[str, Any]:
    """Project one ``EvaluationResultContent`` onto the console's stable row.

    ``value``/``label`` are absent when the result carries ``errorCode`` — the
    row is then an error row for that evaluator, and the console shows the
    message inline instead of a score.
    """
    context = raw.get("context") or {}
    usage = raw.get("tokenUsage") or {}
    return {
        "evaluator_id": raw.get("evaluatorId") or evaluator_id,
        "evaluator_name": raw.get("evaluatorName"),
        "evaluator_arn": raw.get("evaluatorArn"),
        "value": raw.get("value"),
        "label": raw.get("label"),
        "explanation": raw.get("explanation"),
        "span_context": context.get("spanContext"),
        "token_usage": {
            "input": usage.get("inputTokens"),
            "output": usage.get("outputTokens"),
            "total": usage.get("totalTokens"),
        } if usage else None,
        "error_code": raw.get("errorCode"),
        "error_message": raw.get("errorMessage"),
    }
