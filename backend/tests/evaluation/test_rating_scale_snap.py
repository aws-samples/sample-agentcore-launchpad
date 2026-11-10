"""Audit item 7 — a custom judge's verdict is snapped to its rating scale.

Global (create-evaluator): a judge rates on its ``ratingScale``, either ``numerical``
(``{value, label, definition}``) or ``categorical`` (``{label, definition}``), and the result
carries the chosen entry's label and value. Ours trusted the judge's free float ``score`` and
any ``label`` string: a judge answering ``label="Good", score=0.8`` on a 1 / 0.75 / 0.5 scale
was stored as 0.8, and an invented label was stored as is.
"""

import pytest
from strands_evals.types.evaluation import EvaluationData

from app.services.strands_eval import engine_core
from app.services.strands_eval import evaluators as ev
from app.services.strands_eval.results import records_from_outputs
from tests.evaluation.fakes import FakeModel

# the Global doc's numerical example scale
NUMERICAL = [
    {"value": 1, "label": "Very Good", "definition": "completely accurate"},
    {"value": 0.75, "label": "Good", "definition": "mostly accurate"},
    {"value": 0.50, "label": "OK", "definition": "partially correct"},
    {"value": 0.25, "label": "Poor", "definition": "significant errors"},
    {"value": 0, "label": "Very Poor", "definition": "completely incorrect"},
]
# the Global doc's categorical example scale
CATEGORICAL = [
    {"label": "PASS", "definition": "All assertions are satisfied by the session"},
    {"label": "FAIL", "definition": "One or more assertions are not satisfied"},
    {"label": "INCONCLUSIVE", "definition": "Insufficient information"},
]


def _judge(scale, verdict):
    row = {"id": "cust_scale", "definition": "judge", "level": "TRACE",
           "instructions": "Rate the answer: {assistant_turn}", "rating_scale": scale}
    built = ev.build(row, FakeModel(structured=[verdict]))
    return built.evaluate(EvaluationData(input="q", actual_output="a"))


def test_the_label_decides_and_the_score_is_the_entrys_value():
    out = _judge(NUMERICAL, {"score": 0.8, "test_pass": True, "reason": "r", "label": "Good"})
    assert out[0].label == "Good" and out[0].score == 0.75


def test_the_label_is_matched_case_insensitively_and_returned_in_the_scales_case():
    out = _judge(NUMERICAL, {"score": 1.0, "test_pass": True, "reason": "r",
                             "label": " very good "})
    assert out[0].label == "Very Good" and out[0].score == 1.0
    assert out[0].test_pass is True


def test_a_missing_label_is_recovered_only_from_an_exact_scale_value():
    out = _judge(NUMERICAL, {"score": 0.25, "test_pass": False, "reason": "r"})
    assert out[0].label == "Poor" and out[0].score == 0.25
    with pytest.raises(ev.JudgeLabelOffScale):
        _judge(NUMERICAL, {"score": 0.6, "test_pass": False, "reason": "r"})


def test_an_off_scale_label_is_an_invalid_label_error_row():
    with pytest.raises(ev.JudgeLabelOffScale) as caught:
        _judge(NUMERICAL, {"score": 0.75, "test_pass": True, "reason": "r",
                           "label": "Excellent"})
    assert caught.value.error_type == "invalid_label"
    assert "Excellent" in str(caught.value) and "Very Good" in str(caught.value)


def test_the_engine_turns_an_off_scale_label_into_an_error_record():
    row = {"id": "cust_scale", "definition": "judge", "level": "TRACE",
           "instructions": "Rate the answer: {assistant_turn}", "rating_scale": NUMERICAL}
    judge = FakeModel(structured=[{"score": 0.9, "test_pass": True, "reason": "r",
                                   "label": "Superb"}])
    evaluators = engine_core.EvaluatorSet([row], lambda *_a, **_k: judge, default_model_id="m")

    class Provider:
        def get_evaluation_data(self, session_id):
            from strands_evals.types.trace import Session
            return {"output": "a", "trajectory": Session(session_id=session_id, traces=[])}

    records = engine_core.evaluate_session(Provider(), "sess-00001", evaluators, None,
                                           job_id="j", sleep=lambda _s: None)
    assert len(records) == 1
    assert records[0]["error_type"] == "invalid_label" and records[0]["score"] is None


def test_a_categorical_scale_keeps_the_label_and_has_no_score():
    out = _judge(CATEGORICAL, {"score": 1.0, "test_pass": True, "reason": "r",
                               "label": "inconclusive"})
    assert out[0].label == "INCONCLUSIVE" and out[0].score is None
    records = records_from_outputs(job_id="j", session_id="s", evaluator_id="cust_scale",
                                   level="TRACE", outputs=out)
    assert records[0]["score"] is None and records[0]["label"] == "INCONCLUSIVE"
    assert records[0]["error_type"] is None


def test_a_categorical_scale_refuses_an_off_scale_label():
    with pytest.raises(ev.JudgeLabelOffScale):
        _judge(CATEGORICAL, {"score": 1.0, "test_pass": True, "reason": "r", "label": "YES"})


def test_the_categorical_rubric_names_labels_not_none_values():
    built = ev.build({"id": "c", "definition": "judge", "level": "TRACE",
                      "instructions": "Check {context}", "rating_scale": CATEGORICAL},
                     FakeModel())
    assert "None" not in built.rubric
    assert "- PASS: All assertions are satisfied by the session" in built.rubric
