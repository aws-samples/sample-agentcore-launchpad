"""Evaluator-set validation shared by every online evaluation config creator.

Online evaluation scores live traces, which carry no ground truth, so two
evaluator families are refused up front rather than by AWS halfway through a
multi-step action: the built-in trajectory matchers, and custom judges whose
instructions reference a ground-truth placeholder (``{expected_response}`` &
friends). AWS enforces the same constraint, but only at
``CreateOnlineEvaluationConfig``.

Both the experiment gateway stage (``app.optimization.service``) and the
per-agent online evaluation surface (``app.evaluation.online``) call
:func:`normalize_online_evaluators`; the ``code_prefix`` keeps each caller's
error-code family (``experiment.evaluator_unsupported`` vs
``online_eval.evaluator_unsupported``) stable for the frontend.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.errors import AppError
from app.evaluation import agentcore_eval as ac

# The pair every experiment used before the set became selectable; also the
# create-form default on the online evaluation sub-page.
ONLINE_EVAL_DEFAULT = ("Builtin.GoalSuccessRate", "Builtin.Helpfulness")
ONLINE_EVAL_MAX = 10  # CreateOnlineEvaluationConfig caps the list at 10


def _is_not_found(exc: Exception) -> bool:
    return type(exc).__name__ in {"ResourceNotFoundException", "NotFoundException"}


def normalize_online_evaluators(
    ids: Sequence[str] | None,
    control: Any,
    *,
    code_prefix: str = "experiment",
) -> list[str]:
    """Validate an operator-chosen evaluator set for online evaluation.

    ``None``/empty falls back to :data:`ONLINE_EVAL_DEFAULT`. Inspecting a custom
    judge costs one ``GetEvaluator``, so it happens after dedup and the count cap
    and only for non-``Builtin.`` ids — a built-in-only selection, the common
    case, makes no AWS call at all.
    """
    code = f"{code_prefix}.evaluator_unsupported"
    chosen: list[str] = []
    for raw in ids or ():
        evaluator = str(raw).strip()
        if not evaluator or evaluator in chosen:
            continue
        if evaluator in ac.TRAJECTORY_EVALUATORS:
            raise AppError(
                code,
                f"{evaluator} scores against dataset ground truth, which online "
                "evaluation does not carry — use a batch evaluation run instead",
                {"evaluator": evaluator},
                status_code=400,
            )
        if evaluator.startswith("Builtin.") and evaluator not in ac.ALL_BUILTIN_EVALUATORS:
            raise AppError(
                code,
                f"unknown built-in evaluator {evaluator}",
                {"evaluator": evaluator},
                status_code=400,
            )
        chosen.append(evaluator)
    if not chosen:
        return list(ONLINE_EVAL_DEFAULT)
    if len(chosen) > ONLINE_EVAL_MAX:
        raise AppError(
            code,
            f"online evaluation accepts at most {ONLINE_EVAL_MAX} evaluators, "
            f"got {len(chosen)}",
            {"count": len(chosen)},
            status_code=400,
        )
    for evaluator in (e for e in chosen if not e.startswith("Builtin.")):
        _assert_no_ground_truth(control, evaluator, code)
    return chosen


def _assert_no_ground_truth(control: Any, evaluator: str, code: str) -> None:
    """Reject a custom judge that needs ground truth it will never get online."""
    try:
        detail = ac.get_evaluator(control, evaluator_id=evaluator)
    except Exception as exc:
        if _is_not_found(exc):
            raise AppError(
                code,
                f"unknown evaluator {evaluator}",
                {"evaluator": evaluator},
                status_code=400,
            ) from exc
        # a control-plane blip must not block the caller's action: AWS enforces
        # the same constraint server-side, so fail open and let it have the last word
        return
    placeholders = ac.ground_truth_placeholders(ac.judge_instructions(detail))
    if placeholders:
        rendered = ", ".join(f"{{{p}}}" for p in placeholders)
        raise AppError(
            code,
            f"{evaluator} references {rendered}, which is ground truth online "
            "evaluation does not carry — use a batch evaluation run instead",
            {"evaluator": evaluator, "placeholders": placeholders},
            status_code=400,
        )
