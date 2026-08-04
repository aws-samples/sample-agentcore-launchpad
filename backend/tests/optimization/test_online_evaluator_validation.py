"""Online evaluation scores live traces, which carry no ground truth.

A custom judge whose instructions reference ``{expected_response}`` &co must be
refused up front — AWS refuses it too, but only at
``CreateOnlineEvaluationConfig``, i.e. after ``stage_gateway`` has already
created the shared gateway and the v1 runtime target.
"""

import pytest

from app.core.errors import AppError
from app.optimization.service import ONLINE_EVAL_DEFAULT, normalize_online_evaluators


class ResourceNotFoundException(Exception):
    pass


class StubControl:
    """Records GetEvaluator traffic so tests can assert it stays off the fast path."""

    def __init__(self, evaluators=None, error=None):
        self.evaluators = evaluators or {}
        self.error = error
        self.calls: list[str] = []

    def get_evaluator(self, evaluatorId):  # noqa: N803 — boto3 shape
        self.calls.append(evaluatorId)
        if self.error is not None:
            raise self.error
        return self.evaluators[evaluatorId]


def _judge(instructions):
    return {"evaluatorConfig": {"llmAsAJudge": {"instructions": instructions}}}


def test_builtin_only_selection_makes_no_aws_call():
    control = StubControl()
    chosen = normalize_online_evaluators(
        ["Builtin.Helpfulness", "Builtin.Refusal"], control
    )
    assert chosen == ["Builtin.Helpfulness", "Builtin.Refusal"]
    assert control.calls == []


def test_default_fallback_makes_no_aws_call():
    control = StubControl()
    assert normalize_online_evaluators(None, control) == list(ONLINE_EVAL_DEFAULT)
    assert normalize_online_evaluators([], control) == list(ONLINE_EVAL_DEFAULT)
    assert control.calls == []


@pytest.mark.parametrize(
    "placeholder",
    ["expected_response", "expected_tool_trajectory", "assertions"],
)
def test_ground_truth_judge_rejected(placeholder):
    control = StubControl(
        {"gt_judge": _judge(f"Compare {{context}} against {{{placeholder}}}.")}
    )
    with pytest.raises(AppError) as excinfo:
        normalize_online_evaluators(["Builtin.Helpfulness", "gt_judge"], control)
    err = excinfo.value
    assert err.code == "experiment.evaluator_unsupported"
    assert err.status_code == 400
    assert placeholder in str(err)
    assert err.detail["placeholders"] == [placeholder]


def test_trace_only_placeholders_are_fine_online():
    """context / assistant_turn / actual_tool_trajectory come from the trace."""
    control = StubControl({
        "quality_judge": _judge(
            "Rate {assistant_turn} given {context} and {actual_tool_trajectory}."
        )
    })
    chosen = normalize_online_evaluators(["quality_judge"], control)
    assert chosen == ["quality_judge"]
    assert control.calls == ["quality_judge"]


def test_code_based_evaluator_has_no_instructions_to_inspect():
    control = StubControl({
        "lambda_judge": {
            "evaluatorConfig": {"codeBased": {"lambdaFunctionArn": "arn:aws:lambda:x"}}
        }
    })
    assert normalize_online_evaluators(["lambda_judge"], control) == ["lambda_judge"]


def test_unknown_custom_evaluator_is_rejected_not_deferred():
    control = StubControl(error=ResourceNotFoundException("no such evaluator"))
    with pytest.raises(AppError) as excinfo:
        normalize_online_evaluators(["ghost_judge"], control)
    assert excinfo.value.status_code == 400
    assert "ghost_judge" in str(excinfo.value)


def test_transient_lookup_failure_fails_open():
    """A control-plane blip must not block gateway creation — AWS still enforces
    the constraint server-side, so degrade to the pre-check behaviour."""
    control = StubControl(error=RuntimeError("throttled"))
    assert normalize_online_evaluators(["some_judge"], control) == ["some_judge"]


def test_inspection_happens_after_dedup_and_cap():
    control = StubControl({"judge": _judge("Rate {assistant_turn}.")})
    assert normalize_online_evaluators(["judge", "judge", " judge "], control) == ["judge"]
    assert control.calls == ["judge"]  # deduped before the lookup

    over_cap = [f"judge{i}" for i in range(11)]
    control = StubControl()
    with pytest.raises(AppError):
        normalize_online_evaluators(over_cap, control)
    assert control.calls == []  # rejected without paying for 11 lookups


def test_existing_rejections_unchanged():
    control = StubControl()
    with pytest.raises(AppError) as trajectory:
        normalize_online_evaluators(["Builtin.TrajectoryInOrderMatch"], control)
    assert trajectory.value.code == "experiment.evaluator_unsupported"
    with pytest.raises(AppError) as unknown_builtin:
        normalize_online_evaluators(["Builtin.NotAThing"], control)
    assert unknown_builtin.value.status_code == 400
    assert control.calls == []
