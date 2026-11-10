"""A code-based evaluator Lambda written ONLY against the Global AgentCore docs
(code-based-evaluators.html, the AgentCore SDK's ``@custom_code_based_evaluator``) — the
test double for N17-obs327: a Lambda written for Global must run unchanged on Launchpad CN.

It counts the Strands ``execute_tool`` span documents of its target in ``sessionSpans``:
TRACE → the target trace's tool calls; TOOL_CALL → the target span's own tool name; SESSION
→ every tool call, checked against the session-level ``expectedTrajectory`` reference.
Nothing here knows about Launchpad's own case fields.
"""

from __future__ import annotations

from bedrock_agentcore.evaluation.custom_code_based_evaluators import (
    EvaluatorInput,
    EvaluatorOutput,
    custom_code_based_evaluator,
)

SEEN: list[EvaluatorInput] = []


def _tool_spans(spans: list[dict]) -> list[dict]:
    return [s for s in spans
            if (s.get("attributes") or {}).get("gen_ai.operation.name") == "execute_tool"
            and str((s.get("scope") or {}).get("name") or "").startswith("strands")]


@custom_code_based_evaluator()
def handler(input: EvaluatorInput, context) -> EvaluatorOutput:  # noqa: A002 — the SDK's name
    SEEN.append(input)
    tools = _tool_spans(input.session_spans)
    if input.evaluation_level == "TOOL_CALL":
        span = next((s for s in tools if s["spanId"] == input.target_span_id), None)
        if span is None:
            return EvaluatorOutput(errorCode="SPAN_NOT_FOUND", errorMessage=input.target_span_id)
        name = span["attributes"].get("gen_ai.tool.name")
        return EvaluatorOutput(label="Called", value=1.0, explanation=f"{name}")
    if input.evaluation_level == "TRACE":
        mine = [s for s in tools if s["traceId"] == input.target_trace_id]
        return EvaluatorOutput(label="Pass" if mine else "Fail", value=float(len(mine)),
                               explanation=f"{input.target_trace_id}: {len(mine)} tool call(s)")
    expected = next((r.expected_trajectory for r in input.reference_inputs
                     if r.expected_trajectory), None)
    names = [s["attributes"].get("gen_ai.tool.name") for s in tools]
    if expected is None:
        return EvaluatorOutput(label="NoReference", explanation=f"{len(names)} call(s)")
    ok = names == list(expected["toolNames"])
    return EvaluatorOutput(label="Pass" if ok else "Fail", value=1.0 if ok else 0.0,
                           explanation=f"actual={names}")
