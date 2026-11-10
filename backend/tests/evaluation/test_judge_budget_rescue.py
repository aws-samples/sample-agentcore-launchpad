"""N17-obs385 — a judge that runs out of output budget is rescued before the turn is lost.

Round 1 (research-17, d38bac2b014e turn 3: an attachment + a 4-file answer): GLM 5.3
exceeded 16384 output tokens on all three budgets for Correctness and Faithfulness, so the
turn had no score where Global had one. Now, after the doubling retries: (1) one re-judge on
the case with overlong text cut with a visible marker; (2) the ``judge_fallback`` model
role, when configured; the reason is recorded on the row (its explanation and its
``judge_inference.rescue``). Only when every step fails is it an error row, naming them.
"""

import pytest
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput
from strands_evals.types.trace import (
    AgentInvocationSpan,
    Session,
    SpanInfo,
    ToolCall,
    ToolExecutionSpan,
    ToolResult,
    Trace,
)

from app.services.strands_eval import engine_core, model_calls
from app.services.strands_eval import evaluators as ev
from tests.test_strands_engine import MAX_TOKENS, evaluate, logs_with, rows, ws  # noqa: F401

GLM, KIMI = "global.zai.glm-5.3", "global.moonshotai.kimi-k3"
LONG = "很长的交付内容。" * 4000  # 32 000 characters


def _info():
    from datetime import UTC, datetime

    now = datetime(2026, 10, 23, tzinfo=UTC)
    return SpanInfo(session_id="s", span_id="x", trace_id="t", start_time=now, end_time=now)


def _case(answer=LONG, tool_result="ok"):
    session = Session(session_id="s", traces=[Trace(trace_id="t", session_id="s", spans=[
        ToolExecutionSpan(span_info=_info(), tool_call=ToolCall(name="read", arguments={}),
                          tool_result=ToolResult(content=tool_result)),
        AgentInvocationSpan(span_info=_info(), user_prompt="交付四件套", agent_response=answer,
                            available_tools=[]),
    ])])
    return EvaluationData(input="交付四件套", actual_output=answer, actual_trajectory=session)


def test_truncate_case_cuts_long_text_with_a_visible_marker_and_leaves_the_original():
    case = _case(tool_result="r" * 20_000)
    small, truncated = ev.truncate_case(case)
    assert truncated is True
    agent = small.actual_trajectory.traces[0].spans[1]
    tool = small.actual_trajectory.traces[0].spans[0]
    assert len(agent.agent_response) < len(LONG) and "characters truncated" in agent.agent_response
    assert agent.agent_response.startswith(LONG[:100]) and agent.agent_response.endswith(LONG[-50:])
    assert "characters truncated" in tool.tool_result.content
    assert small.actual_output == agent.agent_response
    # the input case is untouched
    assert case.actual_trajectory.traces[0].spans[1].agent_response == LONG


def test_a_short_case_is_not_truncated():
    case = _case(answer="短答案")
    small, truncated = ev.truncate_case(case)
    assert truncated is False and small is case


def _judge_at(fails):
    calls = []

    def judge_at(model_id, max_tokens, case):
        truncated = "characters truncated" in str(case.actual_output)
        calls.append((model_id, max_tokens, truncated))
        if (model_id, truncated) in fails:
            raise MAX_TOKENS
        return [EvaluationOutput(score=1.0, test_pass=True, reason="fits", label="Yes")]
    judge_at.calls = calls
    return judge_at


def _exceeded():
    return model_calls.JudgeBudgetExceeded("judge Builtin.Correctness", 16384, 3, GLM)


def test_the_truncated_context_rescues_the_judgement():
    judge_at = _judge_at({(GLM, False)})
    out = engine_core.rescue_judgement(_exceeded(), case=_case(), judge_at=judge_at,
                                       model_id=GLM, fallback_model_id=None, cap=16384)
    assert out[0].score == 1.0
    assert judge_at.calls == [(GLM, 16384, True)]
    assert out.rescue == {"reason": "JudgeBudgetExceeded", "budget": 16384,
                          "context_truncated": True, "model_id": GLM, "fallback_from": None}


def test_the_fallback_model_rescues_when_the_truncated_context_does_not():
    judge_at = _judge_at({(GLM, False), (GLM, True)})
    out = engine_core.rescue_judgement(_exceeded(), case=_case(), judge_at=judge_at,
                                       model_id=GLM, fallback_model_id=KIMI, cap=16384)
    assert judge_at.calls == [(GLM, 16384, True), (KIMI, 16384, True)]
    assert out.rescue["model_id"] == KIMI and out.rescue["fallback_from"] == GLM


def test_every_step_failing_is_an_error_naming_what_was_tried():
    judge_at = _judge_at({(GLM, False), (GLM, True)})
    with pytest.raises(model_calls.JudgeBudgetExceeded) as caught:
        engine_core.rescue_judgement(_exceeded(), case=_case(), judge_at=judge_at,
                                     model_id=GLM, fallback_model_id=None, cap=16384)
    text = str(caught.value)
    assert "exceeded 16384 output tokens after 3 attempts" in text
    assert "truncated context" in text and "no judge_fallback model is configured" in text


def test_another_error_while_rescuing_propagates():
    def judge_at(model_id, max_tokens, case):
        raise RuntimeError("upstream down")

    with pytest.raises(RuntimeError, match="upstream down"):
        engine_core.rescue_judgement(_exceeded(), case=_case(), judge_at=judge_at,
                                     model_id=GLM, fallback_model_id=KIMI, cap=16384)


def test_the_engine_rescues_with_the_fallback_role_and_records_it(ws, monkeypatch):  # noqa: F811
    from app.core.config import get_settings
    from app.evaluation.strands_engine import StrandsEngine
    from tests.evaluation.fakes import FakeJudge

    monkeypatch.setattr(get_settings(), "eval_judge_fallback_model_id", KIMI)
    calls = []

    def judge_factory(model_id, max_tokens=None, **kw):
        calls.append((model_id, max_tokens))
        if model_id == GLM:
            return FakeJudge(fail_with=MAX_TOKENS)
        return FakeJudge(overrides={"score": "Very helpful", "reasoning": "fits now"})

    eng = StrandsEngine(logs_client=lambda _ws: logs_with(), lambda_client=lambda _ws: None,
                        judge_factory=judge_factory, sleep=lambda _s: None)
    (record,) = evaluate(eng, ws, owner_id="run_rescue")
    assert record["error_type"] is None and record["score"] == pytest.approx(0.833)
    assert record["judge_rescue"]["model_id"] == KIMI
    assert record["explanation"].startswith("[judged by global.moonshotai.kimi-k3")
    (row,) = [r for r in rows() if r.run_id == "run_rescue"]
    assert row.judge_model_id == KIMI
    assert row.judge_inference["rescue"]["fallback_from"] == GLM
    assert row.judge_inference["temperature_applied"] is False  # Kimi: provider default
