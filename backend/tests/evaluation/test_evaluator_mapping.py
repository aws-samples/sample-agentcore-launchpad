"""N12 E0-5 — Launchpad evaluator ids → strands_evals evaluators (plan §B.2 table).

The judge is always the injected model object (risk R-3: `model=None` would make strands
build a default Claude model without the bearer); trajectory evaluators are deterministic
and need no model; every builtin keeps its Launchpad id as the evaluator `name`.
"""

import json

import pytest

from app.evaluation import evaluators_local
from app.services.strands_eval import evaluators as ev
from app.services.strands_eval.context_relevance import ContextRelevanceEvaluator
from tests.evaluation.fakes import FakeModel
from tests.evaluation.test_custom_judge_placeholders import _case

ALL_IDS = sorted({**ev.BUILTIN_LEVELS, **ev.TRAJECTORY_LEVELS})


def test_catalog_mirrors_the_contract_ids_and_levels():
    assert ev.BUILTIN_LEVELS == evaluators_local.ALL_BUILTIN_EVALUATORS
    assert ev.TRAJECTORY_LEVELS == evaluators_local.TRAJECTORY_EVALUATORS
    assert set(ev.LEVELS) == set(ALL_IDS)


@pytest.mark.parametrize("evaluator_id", ALL_IDS)
def test_every_builtin_builds_with_its_id_as_name(evaluator_id):
    judge = FakeModel()
    built = ev.build({"id": evaluator_id}, judge)
    assert built.name == evaluator_id
    if evaluator_id in ev.TRAJECTORY_LEVELS:
        assert isinstance(built, ev.TrajectoryMatchEvaluator)
    else:
        assert built.model is judge, "the judge must be the injected model object"
        assert not isinstance(built.model, str | type(None))


@pytest.mark.parametrize("evaluator_id", sorted(ev.BUILTIN_LEVELS))
def test_llm_evaluators_refuse_a_missing_judge(evaluator_id):
    with pytest.raises(ValueError, match="judge"):
        ev.build({"id": evaluator_id}, None)
    with pytest.raises(ValueError, match="judge"):
        ev.build({"id": evaluator_id}, "global.zai.glm-5.3")  # a str would build Claude


def test_context_relevance_is_a_launchpad_judge_on_a_fixed_scale():
    """N17-obs403: it reads the tool results and answers one of three labels."""
    judge = FakeModel(structured=[{"reasoning": "the lookup answers it",
                                   "score": "Partially Relevant"}])
    built = ev.build({"id": "Builtin.ContextRelevance"}, judge)
    assert isinstance(built, ContextRelevanceEvaluator)
    assert "Global" not in built.system_prompt and "Fully Relevant" in built.system_prompt
    [out] = built.evaluate(_case())
    assert (out.label, out.score, out.test_pass) == ("Partially Relevant", 0.5, True)
    sent = json.dumps([c.get("messages") for c in judge.calls], ensure_ascii=False, default=str)
    assert "Tool result:" in sent and "Ada has 12 vacation days left." in sent


def test_custom_judge_renders_the_rating_scale():
    from strands_evals.evaluators import OutputEvaluator

    judge = FakeModel()
    row = {"id": "cust_abc", "definition": "judge", "level": "TRACE",
           "instructions": "Rate the tone of {session}",
           "rating_scale": [{"label": "pass", "value": 1.0, "definition": "polite"},
                            {"label": "fail", "value": 0.0, "definition": "rude"}]}
    built = ev.build(row, judge)
    assert isinstance(built, OutputEvaluator) and built.name == "cust_abc"
    assert built.model is judge
    assert "Rate the tone" in built.rubric and "pass" in built.rubric and "1.0" in built.rubric
    assert ev.level_of(row) == "TRACE"


def test_derived_evaluator_wraps_its_base_with_the_row_model():
    judge = FakeModel()
    row = {"id": "cust_der", "definition": "derived", "base_evaluator_id": "Builtin.Helpfulness"}
    built = ev.build(row, judge)
    assert built.name == "cust_der" and built.model is judge
    assert ev.level_of(row) == "TRACE"  # the base's level
    with pytest.raises(ValueError):
        ev.build({"id": "x", "definition": "derived", "base_evaluator_id": "Builtin.Nope"}, judge)


def test_code_evaluator_invokes_the_injected_lambda_client():
    import json

    from strands_evals.types.evaluation import EvaluationData

    class FakeLambda:
        calls: list = []

        def invoke(self, **kwargs):
            self.calls.append(kwargs)
            body = json.dumps({"label": "pass", "value": 1.0, "explanation": "ok"}).encode()

            class _P:
                def read(self):
                    return body
            return {"StatusCode": 200, "Payload": _P()}

    row = {"id": "cust_code", "definition": "code", "level": "SESSION",
           "lambda_arn": "arn:aws-cn:lambda:cn-northwest-1:111122223333:function:f",
           "lambda_timeout_s": 30}
    built = ev.build(row, None, lambda_client=FakeLambda())
    out = built.evaluate(EvaluationData(input="q", actual_output="a"))
    assert out[0].score == 1.0 and out[0].label == "pass"
    assert FakeLambda.calls[0]["FunctionName"] == row["lambda_arn"]
    with pytest.raises(ValueError, match="lambda"):
        ev.build(row, None)  # no client injected


@pytest.mark.parametrize("evaluator_id,actual,expected,score", [
    ("Builtin.TrajectoryExactOrderMatch", ["a", "b"], ["a", "b"], 1.0),
    ("Builtin.TrajectoryExactOrderMatch", ["b", "a"], ["a", "b"], 0.0),
    ("Builtin.TrajectoryInOrderMatch", ["x", "a", "y", "b"], ["a", "b"], 1.0),
    ("Builtin.TrajectoryInOrderMatch", ["b", "a"], ["a", "b"], 0.0),  # binary (audit item 6)
    ("Builtin.TrajectoryAnyOrderMatch", ["b", "a"], ["a", "b"], 1.0),
    ("Builtin.TrajectoryAnyOrderMatch", ["b"], ["a", "b"], 0.0),
])
def test_trajectory_evaluators_score_deterministically_without_a_model(
    evaluator_id, actual, expected, score
):
    from strands_evals.types.evaluation import EvaluationData

    built = ev.build({"id": evaluator_id}, None)
    out = built.evaluate(EvaluationData(input="q", actual_trajectory=actual,
                                        expected_trajectory=expected))
    assert len(out) == 1 and out[0].score == pytest.approx(score)
    assert out[0].test_pass is (score == 1.0)


def test_trajectory_evaluator_reads_tool_names_from_a_session():
    import json
    from pathlib import Path

    from strands_evals.types.evaluation import EvaluationData

    from app.services.strands_eval.spans_provider import LaunchpadSpansProvider
    from tests.evaluation.fakes import FakeLogs

    fx = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "spans_session.json")
                    .read_text())
    logs = FakeLogs({"sort": [{"@message": json.dumps(r)} for r in fx["records"]]})
    session = LaunchpadSpansProvider(logs, [fx["log_group"]],
                                     spans_source="runtime_logs").session(fx["session_id"])
    built = ev.build({"id": "Builtin.TrajectoryInOrderMatch"}, None)
    out = built.evaluate(EvaluationData(input="q", actual_trajectory=session,
                                        expected_trajectory=["hr_lookup"]))
    assert out[0].score == 1.0
    assert ev.actual_tool_names(session) == ["hr_lookup"]


def test_no_expected_trajectory_is_not_applicable():
    from strands_evals.types.evaluation import EvaluationData

    out = ev.build({"id": "Builtin.TrajectoryAnyOrderMatch"}, None).evaluate(
        EvaluationData(input="q", actual_trajectory=["a"]))
    assert out[0].label == "NOT_APPLICABLE" and out[0].score == 0.0


def test_unknown_ids_are_refused():
    with pytest.raises(ValueError, match="unknown"):
        ev.build({"id": "Builtin.Nope"}, FakeModel())
    with pytest.raises(ValueError):
        ev.build({"id": "x", "definition": "mystery"}, FakeModel())
