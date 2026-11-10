"""Online evaluation scores live traces, which carry no ground truth.

A custom judge whose instructions reference ``{expected_response}`` &co must be
refused up front (N12 E2-1: the judge is an ``eval_evaluators`` row, so the check
is a ledger read — no control-plane call, no fail-open path any more).
"""

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation.models import EvalEvaluator
from app.evaluation.online_evaluators import ONLINE_EVAL_DEFAULT, normalize_online_evaluators


def _judge(db, evaluator_id, instructions, *, definition="judge", **cols):
    fields = {"id": evaluator_id, "workspace_id": DEFAULT_WORKSPACE_ID, "name": evaluator_id,
              "definition": definition, "instructions": instructions, **cols}
    row = EvalEvaluator(**fields)
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def test_builtin_only_selection_needs_no_ledger(db):
    chosen = normalize_online_evaluators(["Builtin.Helpfulness", "Builtin.Refusal"], db)
    assert chosen == ["Builtin.Helpfulness", "Builtin.Refusal"]


def test_default_fallback():
    assert normalize_online_evaluators(None, None) == list(ONLINE_EVAL_DEFAULT)
    assert normalize_online_evaluators([], None) == list(ONLINE_EVAL_DEFAULT)


@pytest.mark.parametrize(
    "placeholder",
    ["expected_response", "expected_tool_trajectory", "assertions"],
)
def test_ground_truth_judge_rejected(db, placeholder):
    _judge(db, "gt_judge", f"Compare {{context}} against {{{placeholder}}}.")
    with pytest.raises(AppError) as excinfo:
        normalize_online_evaluators(["Builtin.Helpfulness", "gt_judge"], db)
    err = excinfo.value
    assert err.code == "experiment.evaluator_unsupported"
    assert err.status_code == 400
    assert placeholder in str(err)
    assert err.detail["placeholders"] == [placeholder]


def test_trace_only_placeholders_are_fine_online(db):
    """context / assistant_turn / actual_tool_trajectory come from the trace."""
    _judge(db, "quality_judge",
           "Rate {assistant_turn} given {context} and {actual_tool_trajectory}.")
    assert normalize_online_evaluators(["quality_judge"], db) == ["quality_judge"]


def test_code_and_derived_evaluators_have_no_instructions_to_inspect(db):
    _judge(db, "lambda_judge", None, definition="code", lambda_arn="arn:aws-cn:lambda:x")
    _judge(db, "derived_judge", None, definition="derived",
           base_evaluator_id="Builtin.Helpfulness")
    assert normalize_online_evaluators(["lambda_judge", "derived_judge"], db) == [
        "lambda_judge", "derived_judge"]


def test_unknown_custom_evaluator_is_rejected(db):
    with pytest.raises(AppError) as excinfo:
        normalize_online_evaluators(["ghost_judge"], db)
    assert excinfo.value.status_code == 400
    assert "ghost_judge" in str(excinfo.value)


def test_another_workspaces_judge_is_unknown_here(db):
    _judge(db, "foreign_judge", "Rate {assistant_turn}.", workspace_id="other-ws")
    with pytest.raises(AppError):
        normalize_online_evaluators(["foreign_judge"], db)


def test_inspection_happens_after_dedup_and_cap(db):
    _judge(db, "judge", "Rate {assistant_turn}.")
    assert normalize_online_evaluators(["judge", "judge", " judge "], db) == ["judge"]
    over_cap = [f"judge{i}" for i in range(11)]
    with pytest.raises(AppError) as excinfo:
        normalize_online_evaluators(over_cap, db)
    assert "at most 10" in str(excinfo.value)


def test_skill_builtins_accepted(db):
    chosen = normalize_online_evaluators(
        ["Builtin.SkillSelectionAccuracy", "Builtin.SkillInstructionFollowing"], db
    )
    assert chosen == ["Builtin.SkillSelectionAccuracy", "Builtin.SkillInstructionFollowing"]


def test_skill_builtin_typo_rejected(db):
    with pytest.raises(AppError) as excinfo:
        normalize_online_evaluators(["Builtin.SkillTypo"], db)
    assert excinfo.value.status_code == 400


def test_trajectory_matchers_rejected_with_the_caller_code_family(db):
    with pytest.raises(AppError) as trajectory:
        normalize_online_evaluators(["Builtin.TrajectoryInOrderMatch"], db)
    assert trajectory.value.code == "experiment.evaluator_unsupported"
    with pytest.raises(AppError) as online:
        normalize_online_evaluators(["Builtin.TrajectoryInOrderMatch"], db,
                                    code_prefix="online_eval")
    assert online.value.code == "online_eval.evaluator_unsupported"


def test_a_non_session_second_argument_is_refused(db):
    """E2-1 dropped the control-client parameter: every caller hands over the ledger
    Session (the experiment router included), so an unknown custom id is a 400 everywhere."""

    class NotASession:
        pass

    with pytest.raises(TypeError):
        normalize_online_evaluators(["legacy_unknown"], NotASession())
    with pytest.raises(AppError):
        normalize_online_evaluators(["legacy_unknown"], db)
