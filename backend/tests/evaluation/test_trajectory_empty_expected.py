"""N17-obs376 — an explicit empty expected trajectory means "no tools expected", as on Global.

Global scores ``expectedTrajectory.toolNames = []`` (DIFF-GLOBAL-EVAL B5): session 2335a936
(a refusal, no tool called) → Yes on all three matchers; 7f557e29 / f0469605 / 539d13ba
(1–2 tools called) → No. Ours collapsed ``[]`` into "no expectation" (NOT_APPLICABLE) at
three places: the scenario → truth fold, ``evaluation_data`` and the matcher. Only an ABSENT
field is NOT_APPLICABLE now.
"""

import pytest
from strands_evals.types.evaluation import EvaluationData

from app.evaluation import coverage, scenarios
from app.services.strands_eval import engine_core, judge_placeholders
from app.services.strands_eval import evaluators as ev

MATCHERS = ["Builtin.TrajectoryExactOrderMatch", "Builtin.TrajectoryInOrderMatch",
            "Builtin.TrajectoryAnyOrderMatch"]


def _judge(evaluator_id, actual, expected):
    (out,) = ev.build({"id": evaluator_id}, None).evaluate(
        EvaluationData(input="q", actual_trajectory=actual, expected_trajectory=expected))
    return out


@pytest.mark.parametrize("evaluator_id", MATCHERS)
@pytest.mark.parametrize("actual,passed", [
    ([], True),                                   # 2335a936: no tool called → Yes
    (["kb_search"], False),                       # 7f557e29: one tool → No
    (["kb_search", "kb_search"], False),          # f0469605
])
def test_an_explicit_empty_trajectory_expects_no_tools(evaluator_id, actual, passed):
    out = _judge(evaluator_id, actual, [])
    assert out.score == (1.0 if passed else 0.0) and out.test_pass is passed
    assert out.label != "NOT_APPLICABLE"


@pytest.mark.parametrize("evaluator_id", MATCHERS)
def test_an_absent_trajectory_stays_not_applicable(evaluator_id):
    assert _judge(evaluator_id, ["kb_search"], None).label == "NOT_APPLICABLE"


def test_the_truth_fold_keeps_empty_apart_from_absent():
    truth = engine_core.ground_truth_from_scenarios([
        {"scenario_id": "none", "turns": [{"input": "hi"}], "expected_trajectory": []},
        {"scenario_id": "absent", "turns": [{"input": "hi"}]},
    ], ["s-1", "s-2"])
    assert truth["s-1"]["expected_trajectory"] == []
    assert truth["s-2"]["expected_trajectory"] is None


def test_evaluation_data_keeps_an_empty_expectation():
    from strands_evals.types.trace import Session

    task = {"output": "a", "trajectory": Session(session_id="s", traces=[])}
    empty = engine_core.evaluation_data("s", task, {"expected_trajectory": []})
    absent = engine_core.evaluation_data("s", task, {"expected_trajectory": None})
    assert empty.expected_trajectory == [] and absent.expected_trajectory is None


def test_an_empty_expectation_counts_as_trajectory_ground_truth():
    items = [{"scenario_id": "none", "turns": [{"input": "hi"}], "expected_trajectory": []}]
    assert "expected_tool_trajectory" in scenarios.available_ground_truth(items)
    (target,) = [t for t in coverage.reference_targets(items) if t["kind"] == "session"]
    assert "expected_tool_trajectory" in target["fields"]
    absent = [{"scenario_id": "absent", "turns": [{"input": "hi"}]}]
    assert "expected_tool_trajectory" not in scenarios.available_ground_truth(absent)


def test_the_placeholder_says_no_tools_are_expected():
    case = EvaluationData(input="q", expected_trajectory=[])
    assert judge_placeholders.render_placeholders("{expected_tool_trajectory}", case) == (
        "(no tools expected)")
