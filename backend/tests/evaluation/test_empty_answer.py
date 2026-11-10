"""N17-obs411 — an empty final answer is decided by a rule, not rated by a judge.

Live on a LangGraph turn whose extracted answer was "", Faithfulness, ResponseRelevance and
Coherence scored 1.0. Now an empty (or whitespace) answer never reaches a judge:
Helpfulness / Correctness / InstructionFollowing / Conciseness / ResponseRelevance get the
bottom of their scale (an empty response fails them, and pulls the run mean down);
Faithfulness / Coherence — nothing to rate — get NOT_APPLICABLE. Every such row carries
``deterministic: "empty_answer"`` (record) and ``judge_inference.deterministic`` (stored row).
Refusal and the trajectory / tool judges still judge the turn.
"""

import asyncio

import pytest
from strands_evals.types.trace import AgentInvocationSpan

from app.evaluation.judge_budget import row_judge
from app.services.strands_eval import engine_core
from app.services.strands_eval import evaluators as ev
from app.services.strands_eval.results import aggregate_scores, records_from_outputs
from tests.evaluation.fakes import FakeJudge
from tests.evaluation.test_custom_judge_placeholders import _case

BOTTOM = {"Builtin.Helpfulness": "Not Helpful At All", "Builtin.Correctness": "Incorrect",
          "Builtin.InstructionFollowing": "No", "Builtin.Conciseness": "Not Concise",
          "Builtin.ResponseRelevance": "Not At All"}
NOT_APPLICABLE = ["Builtin.Faithfulness", "Builtin.Coherence"]
RULED = sorted([*BOTTOM, *NOT_APPLICABLE])


def _with_answer(text):
    case = _case()
    agent = next(s for s in case.actual_trajectory.traces[-1].spans
                 if isinstance(s, AgentInvocationSpan))
    agent.agent_response = text
    case.actual_output = text
    return case


def _records(evaluator_id, outputs, trace_id="t", session_id="s"):
    return records_from_outputs(job_id="j", session_id=session_id, evaluator_id=evaluator_id,
                                level="TRACE", outputs=outputs, trace_id=trace_id)


def test_the_policy_table():
    assert ev.EMPTY_ANSWER_BOTTOM == BOTTOM
    assert ev.EMPTY_ANSWER_NOT_APPLICABLE == frozenset(NOT_APPLICABLE)
    for evaluator_id, label in BOTTOM.items():
        assert label in ev.GLOBAL_LABELS[evaluator_id]  # Global's own bottom label
    for untouched in ("Builtin.Refusal", "Builtin.Harmfulness", "Builtin.GoalSuccessRate",
                      "Builtin.ToolSelectionAccuracy", "Builtin.TrajectoryExactOrderMatch"):
        assert untouched not in RULED


@pytest.mark.parametrize("evaluator_id", sorted(BOTTOM))
@pytest.mark.parametrize("answer", ["", "   \n\t "])
def test_an_empty_answer_scores_the_bottom_without_a_judge_call(evaluator_id, answer):
    judge = FakeJudge()
    [out] = ev.build({"id": evaluator_id}, judge).evaluate(_with_answer(answer))
    assert judge.calls == []
    [record] = _records(evaluator_id, [out])
    assert (record["score"], record["label"]) == (0.0, BOTTOM[evaluator_id])
    assert record["deterministic"] == "empty_answer" and record["error_type"] is None
    assert record["explanation"].startswith("empty answer") and "without a judge call" in \
        record["explanation"]


@pytest.mark.parametrize("evaluator_id", NOT_APPLICABLE)
@pytest.mark.parametrize("answer", ["", "  "])
def test_faithfulness_and_coherence_are_not_applicable(evaluator_id, answer):
    judge = FakeJudge()
    [out] = ev.build({"id": evaluator_id}, judge).evaluate(_with_answer(answer))
    assert judge.calls == []
    [record] = _records(evaluator_id, [out])
    assert record["score"] is None and record["label"] == "NOT_APPLICABLE"
    assert record["deterministic"] == "empty_answer" and "nothing" in record["explanation"]


def test_the_stored_row_says_no_judge_produced_it():
    [out] = ev.build({"id": "Builtin.Helpfulness"}, FakeJudge()).evaluate(_with_answer(""))
    [record] = _records("Builtin.Helpfulness", [out])
    judges = {"Builtin.Helpfulness": {"model_id": "global.zai.glm-5.3"}}
    assert row_judge(record, judges) == (True, {"model_id": None,
                                                "deterministic": "empty_answer"})
    judged = {**record, "deterministic": None}
    assert row_judge(judged, judges) == (True, {"model_id": "global.zai.glm-5.3"})


@pytest.mark.parametrize("evaluator_id", RULED)
def test_a_real_answer_is_still_judged(evaluator_id):
    judge = FakeJudge()
    outputs = ev.build({"id": evaluator_id}, judge).evaluate(_with_answer("Ada has 12 days."))
    assert judge.calls
    assert all("deterministic" not in r for r in _records(evaluator_id, outputs))


def test_refusal_and_a_derived_row():
    judge = FakeJudge()
    ev.build({"id": "Builtin.Refusal"}, judge).evaluate(_with_answer(""))
    assert judge.calls  # Refusal still judges an empty turn
    derived = ev.build({"id": "cust_d", "definition": "derived",
                        "base_evaluator_id": "Builtin.Helpfulness"}, FakeJudge())
    assert derived.evaluate(_with_answer(""))[0].label == "Not Helpful At All"


def test_the_async_path_applies_the_rule_too():
    judge = FakeJudge()
    built = ev.build({"id": "Builtin.ResponseRelevance"}, judge)
    [out] = asyncio.run(built.evaluate_async(_with_answer("")))
    assert (out.score, out.label, judge.calls) == (0.0, "Not At All", [])


@pytest.mark.parametrize("evaluator_id", sorted(BOTTOM))
def test_an_empty_turn_lowers_the_run_mean_of_a_bottom_scored_evaluator(evaluator_id):
    """Two turns: one judged answer at 1.0 and one empty answer → mean 0.5 over n = 2.
    A NOT_APPLICABLE row would have left the mean at 1.0 over n = 1."""
    judged = ev.EvaluationOutput(score=1.0, test_pass=True, label="x", reason="judged")
    [empty] = ev.build({"id": evaluator_id}, FakeJudge()).evaluate(_with_answer(""))
    records = _records(evaluator_id, [judged], trace_id="t1") + \
        _records(evaluator_id, [empty], trace_id="t2")
    records[1]["id"] = "other"
    [row] = aggregate_scores(records)
    assert (row["score"], row["n"]) == (0.5, 2)


@pytest.mark.parametrize("evaluator_id", NOT_APPLICABLE)
def test_a_not_applicable_empty_turn_stays_out_of_the_mean(evaluator_id):
    judged = ev.EvaluationOutput(score=1.0, test_pass=True, label="x", reason="judged")
    [empty] = ev.build({"id": evaluator_id}, FakeJudge()).evaluate(_with_answer(""))
    [row] = aggregate_scores(_records(evaluator_id, [judged], trace_id="t1")
                             + _records(evaluator_id, [empty], trace_id="t2"))
    assert (row["score"], row["n"]) == (1.0, 1)


def test_per_turn_scoring_rules_only_the_empty_turn():
    units = engine_core.turn_cases(_with_answer(""))
    built = ev.build({"id": "Builtin.Helpfulness"}, FakeJudge())
    rows = [built.evaluate(unit)[0] for _trace, unit in units]
    assert getattr(rows[-1], "deterministic", None) == "empty_answer"
    assert all(getattr(r, "deterministic", None) is None for r in rows[:-1])
