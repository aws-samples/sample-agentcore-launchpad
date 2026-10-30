"""Audit item 2 — Builtin.Correctness compares the answer against the expected RESPONSE.

Global (ground-truth-evaluations): "`Builtin.Correctness` | Trace | `expectedResponse`" and
"Traces without ground truth are evaluated using the ground truth-free variant"; the
"Correctness with ground truth" template fills ``EXPECTED RESPONSE:`` with
``{expected_response}``. The strands class keys its reference mode on ``expected_assertion``
and renders the ASSERTIONS there, so a scenario with only an expected response got the
3-level no-reference judge and a scenario with assertions got "answer vs assertion list".
"""

import json

from strands_evals.types.evaluation import EvaluationData

from app.services.strands_eval import evaluators as ev
from tests.evaluation.fakes import FakeModel
from tests.evaluation.test_custom_judge_placeholders import _case

REFERENCE_VERDICT = {"reasoning": "same facts", "verdict": "CORRECT"}
BASIC_VERDICT = {"reasoning": "fine", "score": "Perfectly Correct"}


def _sent(judge: FakeModel) -> str:
    return json.dumps([c.get("messages") or c.get("prompt") for c in judge.calls],
                      ensure_ascii=False, default=str)


def _with_truth(**truth) -> EvaluationData:
    return _case({"input": "How many vacation days does Ada have?", **truth})


def test_an_expected_response_alone_selects_the_ground_truth_template():
    judge = FakeModel(structured=[REFERENCE_VERDICT])
    built = ev.build({"id": "Builtin.Correctness"}, judge)
    out = built.evaluate(_with_truth(expected_response="Ada has 12 vacation days."))
    assert out[0].label == "Correct" and out[0].score == 1.0  # Global case (N17-obs377)
    sent = _sent(judge)
    assert "EXPECTED RESPONSE:\\nAda has 12 vacation days." in sent
    assert "USER QUERY:\\nHow many vacation days does Ada have?" in sent
    assert "AGENT RESPONSE:\\nAda has 12 vacation days left." in sent


def test_assertions_never_reach_correctness_and_keep_the_free_variant():
    judge = FakeModel(structured=[BASIC_VERDICT])
    built = ev.build({"id": "Builtin.Correctness"}, judge)
    out = built.evaluate(_with_truth(assertions=["The agent looks Ada up"]))
    assert out[0].label == "Perfectly Correct"  # the ground-truth-free 3-level rubric
    sent = _sent(judge)
    assert "EXPECTED RESPONSE" not in sent and "looks Ada up" not in sent


def test_with_both_fields_the_expected_response_is_rendered_not_the_assertions():
    judge = FakeModel(structured=[REFERENCE_VERDICT])
    built = ev.build({"id": "Builtin.Correctness"}, judge)
    built.evaluate(_with_truth(expected_response="Ada has 12 vacation days.",
                               assertions=["The agent looks Ada up"]))
    sent = _sent(judge)
    assert "EXPECTED RESPONSE:\\nAda has 12 vacation days." in sent
    assert "looks Ada up" not in sent


def test_the_reference_system_prompt_is_the_global_ground_truth_text():
    built = ev.build({"id": "Builtin.Correctness"}, FakeModel())
    assert built.reference_system_prompt.startswith(
        "You are an evaluator assessing whether an agent's response correctly addresses a "
        "user query.")
    assert "Determine if the agent response is CORRECT by comparing it to the expected " \
           "response." in built.reference_system_prompt


def test_a_derived_row_on_correctness_keeps_the_ground_truth_mode():
    judge = FakeModel(structured=[REFERENCE_VERDICT])
    built = ev.build({"id": "cust_corr", "definition": "derived",
                      "base_evaluator_id": "Builtin.Correctness"}, judge)
    built.evaluate(_with_truth(expected_response="Ada has 12 vacation days."))
    assert "EXPECTED RESPONSE:\\nAda has 12 vacation days." in _sent(judge)
