"""N17-obs449 — the failure detector on a replayed GLM-5.3 Bedrock stream.

Live (deploy-ops-43): both hr-eval-demo insights runs logged the detector's parse warning on
20/20 sessions with ``input_value=''`` — an EMPTY reply. GLM-5.3 on Bedrock answers with a
``reasoningContent`` block and THEN a ``text`` block (real record:
tests/fixtures/eval_corpus/d38bac2b014e.json); the detector keeps text deltas only, so a reply
whose budget is spent in the reasoning block — ``stopReason: max_tokens`` before any text —
reads as ''. This replays ConverseStream events through the REAL strands ``BedrockModel``
(stubbed client) and the platform's ``detect_session_failures``.
"""

from __future__ import annotations

import json

import pytest
from strands.models.bedrock import BedrockModel
from strands_evals.detectors import failure_detector as fd
from strands_evals.types.detector import ConfidenceLevel
from strands_evals.types.trace import Session

from app.services.strands_eval import insights, model_calls

EMPTY = json.dumps({"errors": []})
ONE = json.dumps({"errors": [{"location": "s1", "category": ["task-instruction-non-compliance"],
                              "evidence": ["said 9 days; its own formula gives 6"],
                              "confidence": ["high"]}]})


class _GlmStream:
    """``bedrock-runtime`` stub: GLM-shaped ConverseStream events. Below ``reasoning_tokens``
    of budget the reply ends inside the reasoning block (stopReason max_tokens, no text);
    at or above it the text block follows."""

    def __init__(self, text: str, reasoning_tokens: int):
        self.text, self.reasoning_tokens = text, reasoning_tokens
        self.budgets: list[int] = []

    def converse_stream(self, **request):
        budget = int(request["inferenceConfig"]["maxTokens"])
        self.budgets.append(budget)
        events = [{"messageStart": {"role": "assistant"}},
                  {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {
                      "reasoningContent": {"text": "Walking the spans against the taxonomy…"}}}}]
        if budget < self.reasoning_tokens:
            events += [{"contentBlockStop": {"contentBlockIndex": 0}},
                       {"messageStop": {"stopReason": "max_tokens"}},
                       {"metadata": {"usage": {"inputTokens": 9000, "outputTokens": budget,
                                               "totalTokens": 9000 + budget},
                                     "metrics": {"latencyMs": 1}}}]
        else:
            events += [{"contentBlockStop": {"contentBlockIndex": 0}},
                       {"contentBlockDelta": {"contentBlockIndex": 1,
                                              "delta": {"text": self.text}}},
                       {"contentBlockStop": {"contentBlockIndex": 1}},
                       {"messageStop": {"stopReason": "end_turn"}},
                       {"metadata": {"usage": {"inputTokens": 9000, "outputTokens": 300,
                                               "totalTokens": 9300}, "metrics": {"latencyMs": 1}}}]
        return {"stream": iter(events)}


def _glm(text: str, reasoning_tokens: int, budget: int = 4096) -> tuple[BedrockModel, _GlmStream]:
    model = BedrockModel(model_id="global.zai.glm-5.3", region_name="us-west-2", max_tokens=budget)
    model.client = _GlmStream(text, reasoning_tokens)
    return model, model.client


SESSION = Session(session_id="s1", traces=[])


def test_the_library_alone_reads_a_reasoning_only_reply_as_no_failures():
    """The live symptom, reproduced: the budget ends in the reasoning block → '' → []."""
    model, _stub = _glm(ONE, reasoning_tokens=10**9)
    assert fd.detect_failures(SESSION, model=model,
                              confidence_threshold=ConfidenceLevel.MEDIUM).failures == []


def test_the_text_block_after_reasoning_is_what_the_detector_reads():
    model, _stub = _glm(ONE, reasoning_tokens=0)
    (item,) = fd.detect_failures(SESSION, model=model,
                                 confidence_threshold=ConfidenceLevel.MEDIUM).failures
    assert item.category == ["task-instruction-non-compliance"]


def test_detect_session_failures_raises_on_the_max_tokens_stop():
    model, _stub = _glm(ONE, reasoning_tokens=10**9)
    with pytest.raises(model_calls.OutputTruncated):
        insights.detect_session_failures(SESSION, model, ConfidenceLevel.MEDIUM)


def test_the_budget_retry_reaches_the_text_block():
    """call_with_budget doubles the budget until the reply gets past its reasoning."""
    model, stub = _glm(EMPTY, reasoning_tokens=8192)

    def call(max_tokens: int):
        with model_calls.model_budget(model, max_tokens):
            return insights.detect_session_failures(SESSION, model, ConfidenceLevel.MEDIUM)

    diagnosis, dropped = model_calls.call_with_budget(call, budget=4096, cap=16384,
                                                      label="failure-analysis judge")
    assert stub.budgets == [4096, 8192]
    assert diagnosis.failures == [] and dropped == 0


def test_still_reasoning_at_the_cap_is_a_budget_error_not_no_failures():
    model, stub = _glm(EMPTY, reasoning_tokens=10**9)

    def call(max_tokens: int):
        with model_calls.model_budget(model, max_tokens):
            return insights.detect_session_failures(SESSION, model, ConfidenceLevel.MEDIUM)

    with pytest.raises(model_calls.JudgeBudgetExceeded):
        model_calls.call_with_budget(call, budget=4096, cap=16384, label="failure-analysis judge")
    assert stub.budgets == [4096, 8192, 16384]


# ── stop reasons with no answer (live run 79951c9dbb1c: 12/18 still empty at 16384) ──────────
class _GlmReply:
    """``bedrock-runtime`` stub with one scripted detector reply — ``reasoning`` text, optional
    ``text`` and the ``stop`` reason — and a structured-output (root-cause) answer for a
    request that carries tools."""

    def __init__(self, *, reasoning: str, text: str = "", stop: str = "end_turn"):
        self.reasoning, self.text, self.stop = reasoning, text, stop
        self.tool_calls = 0

    def converse_stream(self, **request):
        if request.get("toolConfig"):
            self.tool_calls += 1
            name = request["toolConfig"]["tools"][0]["toolSpec"]["name"]
            events = [{"messageStart": {"role": "assistant"}},
                      {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {
                          "toolUseId": "t1", "name": name}}}},
                      {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {
                          "input": json.dumps(RCA)}}}},
                      {"contentBlockStop": {"contentBlockIndex": 0}},
                      {"messageStop": {"stopReason": "tool_use"}}]
            return {"stream": iter(events)}
        events = [{"messageStart": {"role": "assistant"}},
                  {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {
                      "reasoningContent": {"text": self.reasoning}}}},
                  {"contentBlockStop": {"contentBlockIndex": 0}}]
        if self.text:
            events += [{"contentBlockDelta": {"contentBlockIndex": 1,
                                              "delta": {"text": self.text}}},
                       {"contentBlockStop": {"contentBlockIndex": 1}}]
        events += [{"messageStop": {"stopReason": self.stop}}]
        return {"stream": iter(events)}


RCA = {"root_causes": [{
    "failure_span_id": "s1", "location": "s1", "failure_causality": "PRIMARY_FAILURE",
    "failure_propagation_impact": ["QUALITY_DEGRADATION"],
    "failure_detection_timing": "IMMEDIATELY_AT_OCCURRENCE", "completion_status": "PARTIAL_SUCCESS",
    "root_cause_explanation": "ignored its own formula",
    "fix_recommendation": {"fix_type": "SYSTEM_PROMPT_FIX", "recommendation": "state the formula"},
}]}


def _reply(**kwargs) -> tuple[BedrockModel, _GlmReply]:
    model = BedrockModel(model_id="global.zai.glm-5.3", region_name="us-west-2", max_tokens=16384)
    model.client = _GlmReply(**kwargs)
    return model, model.client


def test_a_reasoning_only_end_turn_is_empty_even_when_the_reasoning_holds_json():
    """The reasoning block is the model's private draft (lead ruling, obs449): a payload that
    appears only there is never read as the answer — the session is a detection error."""
    model, stub = _reply(reasoning=f"draft: {ONE}")
    with pytest.raises(insights.FailureDetectionStopped) as stopped:
        insights.detect_session_failures(SESSION, model, ConfidenceLevel.MEDIUM)
    assert stopped.value.kind == insights.DETECTION_EMPTY and stub.tool_calls == 0


def test_a_text_answer_is_read_and_the_reasoning_ignored():
    model, _stub = _reply(reasoning=f"draft {ONE}", text=EMPTY)
    diagnosis, dropped = insights.detect_session_failures(SESSION, model, ConfidenceLevel.MEDIUM)
    assert diagnosis.failures == [] and dropped == 0


@pytest.mark.parametrize("stop, kind", [
    ("content_filtered", insights.DETECTION_FILTERED),
    ("guardrail_intervened", insights.DETECTION_FILTERED),
    ("model_context_window_exceeded", insights.DETECTION_CONTEXT),
])
def test_h3_a_filter_or_context_stop_is_its_own_error_and_not_retried(stop, kind):
    model, _stub = _reply(reasoning="…", stop=stop)
    with pytest.raises(insights.FailureDetectionStopped) as stopped:
        insights.detect_session_failures(SESSION, model, ConfidenceLevel.MEDIUM)
    assert stopped.value.kind == kind
    assert not model_calls.is_max_tokens_error(stopped.value)  # the ladder does not retry it


def test_h4_an_empty_end_turn_with_no_payload_anywhere_is_an_empty_error():
    model, _stub = _reply(reasoning="I will analyse the spans now.")
    with pytest.raises(insights.FailureDetectionStopped) as stopped:
        insights.detect_session_failures(SESSION, model, ConfidenceLevel.MEDIUM)
    assert stopped.value.kind == insights.DETECTION_EMPTY
