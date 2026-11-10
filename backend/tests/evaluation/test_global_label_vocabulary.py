"""N17-obs377 — builtin evaluators emit Global's label strings; the values are unchanged.

Measured on Global (DIFF-GLOBAL-EVAL B6, /tmp/diff-global gcache, 2026-10-23):
Correctness with expectedResponse → ``Correct`` / ``Incorrect`` (the library: ``CORRECT`` /
``INCORRECT``); GoalSuccessRate with assertions → ``Yes`` / ``No`` (``SUCCESS`` /
``FAILURE``); Helpfulness → ``Above And Beyond``, ``Very Helpful``, ``Somewhat Helpful``,
``Somewhat Unhelpful`` (the library lower-cases the second word); the trajectory matchers →
``Yes`` / ``No``. Every other builtin already matches the Global template enums.
"""

import enum
import importlib
import inspect

import pytest
from strands_evals.types.evaluation import EvaluationData

from app.services.strands_eval import evaluators as ev
from tests.evaluation.fakes import FakeModel
from tests.evaluation.test_custom_judge_placeholders import _case


@pytest.mark.parametrize("evaluator_id", sorted(ev.GLOBAL_LABELS))
def test_every_library_label_maps_into_the_global_vocabulary(evaluator_id):
    """Snapshot: a strands-evals bump that adds or renames a label fails here."""
    if evaluator_id in ev.TRAJECTORY_LEVELS:
        return
    cls = ev._SAME_NAMED[evaluator_id]
    labels = set()
    for klass in inspect.getmro(cls):
        module = importlib.import_module(klass.__module__)
        for _, member in inspect.getmembers(module, inspect.isclass):
            own = member.__module__ == module.__name__
            if own and issubclass(member, enum.Enum) and issubclass(member, str):
                labels |= {m.value for m in member}
    assert labels, evaluator_id
    for label in labels:
        assert ev.global_label(evaluator_id, label) in ev.GLOBAL_LABELS[evaluator_id], label


@pytest.mark.parametrize("evaluator_id,library,global_", [
    ("Builtin.Correctness", "CORRECT", "Correct"),
    ("Builtin.Correctness", "INCORRECT", "Incorrect"),
    ("Builtin.Correctness", "Perfectly Correct", "Perfectly Correct"),
    ("Builtin.GoalSuccessRate", "SUCCESS", "Yes"),
    ("Builtin.GoalSuccessRate", "FAILURE", "No"),
    ("Builtin.Helpfulness", "Above and beyond", "Above And Beyond"),
    ("Builtin.Helpfulness", "Very helpful", "Very Helpful"),
    ("Builtin.Helpfulness", "Somewhat unhelpful", "Somewhat Unhelpful"),
    ("Builtin.Helpfulness", "Not helpful at all", "Not Helpful At All"),
    ("Builtin.Faithfulness", "Completely Yes", "Completely Yes"),
])
def test_the_measured_global_strings(evaluator_id, library, global_):
    assert ev.global_label(evaluator_id, library) == global_


def test_correctness_with_reference_says_correct_and_keeps_the_value():
    judge = FakeModel(structured=[{"reasoning": "same", "verdict": "CORRECT"}])
    (out,) = ev.build({"id": "Builtin.Correctness"}, judge).evaluate(
        _case({"input": "How many vacation days does Ada have?",
               "expected_response": "Ada has 12 vacation days."}))
    assert out.label == "Correct" and out.score == 1.0


def test_goal_success_with_assertions_says_yes():
    judge = FakeModel(structured=[{"reasoning": "met", "verdict": "SUCCESS"}])
    (out,) = ev.build({"id": "Builtin.GoalSuccessRate"}, judge).evaluate(
        _case({"assertions": ["The agent looks Ada up"]}))
    assert out.label == "Yes" and out.score == 1.0


def test_helpfulness_uses_global_case_and_a_derived_row_too():
    for row in ({"id": "Builtin.Helpfulness"},
                {"id": "cust_help", "definition": "derived",
                 "base_evaluator_id": "Builtin.Helpfulness"}):
        judge = FakeModel(structured=[{"reasoning": "ok", "score": "Very helpful"}])
        (out,) = ev.build(row, judge).evaluate(_case())
        assert out.label == "Very Helpful" and out.score == pytest.approx(0.833, abs=0.001)


@pytest.mark.parametrize("evaluator_id", sorted(ev.TRAJECTORY_LEVELS))
def test_trajectory_matchers_say_yes_or_no(evaluator_id):
    built = ev.build({"id": evaluator_id}, None)
    yes = built.evaluate(EvaluationData(input="q", actual_trajectory=["a"],
                                        expected_trajectory=["a"]))[0]
    no = built.evaluate(EvaluationData(input="q", actual_trajectory=["b"],
                                       expected_trajectory=["a"]))[0]
    assert (yes.label, yes.score) == ("Yes", 1.0) and (no.label, no.score) == ("No", 0.0)
