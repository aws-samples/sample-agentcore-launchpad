"""Audit item 6 — the trajectory evaluators follow Global's binary matching rules.

Global (ground-truth-evaluations, "Trajectory matching with expected trajectory"):

- ExactOrderMatch: "same tools, same order, no extras". Expected [calculator, weather],
  actual [calculator, weather] → Pass; actual [calculator, weather, calculator] → Fail.
- InOrderMatch: "Expected tools must appear in order, but extra tools are allowed between
  them". Actual [calculator, some_tool, weather] → Pass.
- AnyOrderMatch: "All expected tools must be present, order doesn't matter, extras
  allowed". Actual [weather, calculator] → Pass.

The library's scorers zip the lists (trailing extras ignored) and give partial credit, so
the doc's own Fail example scored 1.00 "match" and a missing tool 0.50 "partial".
"""

import pytest
from strands_evals.types.evaluation import EvaluationData

from app.services.strands_eval import evaluators as ev

EXACT = "Builtin.TrajectoryExactOrderMatch"
IN_ORDER = "Builtin.TrajectoryInOrderMatch"
ANY_ORDER = "Builtin.TrajectoryAnyOrderMatch"
EXPECTED = ["calculator", "weather"]


def _judge(evaluator_id, actual, expected=EXPECTED):
    out = ev.build({"id": evaluator_id}, None).evaluate(
        EvaluationData(input="q", actual_trajectory=actual, expected_trajectory=expected))
    assert len(out) == 1
    return out[0]


@pytest.mark.parametrize("evaluator_id,actual,verdict", [
    # the Global doc's examples
    (EXACT, ["calculator", "weather"], "Yes"),
    (EXACT, ["calculator", "weather", "calculator"], "No"),
    (IN_ORDER, ["calculator", "some_tool", "weather"], "Yes"),
    (ANY_ORDER, ["weather", "calculator"], "Yes"),
    # the same rules on the counter-cases
    (EXACT, ["weather", "calculator"], "No"),
    (EXACT, ["calculator"], "No"),
    (IN_ORDER, ["weather", "calculator"], "No"),
    (IN_ORDER, ["calculator"], "No"),
    (IN_ORDER, ["calculator", "weather", "calculator"], "Yes"),
    (ANY_ORDER, ["weather", "some_tool", "calculator", "weather"], "Yes"),
    (ANY_ORDER, ["calculator"], "No"),
    (ANY_ORDER, [], "No"),
])
def test_the_global_rules_give_binary_pass_or_fail(evaluator_id, actual, verdict):
    out = _judge(evaluator_id, actual)
    assert out.label == verdict
    assert out.score == (1.0 if verdict == "Yes" else 0.0)
    assert out.test_pass is (verdict == "Yes")


def test_the_reason_keeps_the_partial_fraction_and_both_lists():
    out = _judge(ANY_ORDER, ["calculator"])
    assert out.label == "No"
    assert "1/2" in out.reason
    assert "calculator" in out.reason and "weather" in out.reason


def test_in_order_matches_each_expected_tool_once_in_sequence():
    # a repeated expected tool needs a repeated call (subsequence, not set membership)
    assert _judge(IN_ORDER, ["a", "b"], ["a", "a", "b"]).label == "No"
    assert _judge(IN_ORDER, ["a", "x", "a", "b"], ["a", "a", "b"]).label == "Yes"
