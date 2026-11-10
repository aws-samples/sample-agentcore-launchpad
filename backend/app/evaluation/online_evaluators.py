"""Evaluator-set validation shared by every online evaluation config creator (N12 E2-1).

Online evaluation scores live traces, which carry no ground truth, so two evaluator
families are refused up front: the built-in trajectory matchers, and custom judges
whose instructions reference a ground-truth placeholder (``{expected_response}`` &
friends). Custom evaluators are ``eval_evaluators`` rows in CN (plan CA-3), so the
check is one ledger read — no control-plane call, no fail-open path.

Both the Runtime-canary create route (``app.optimization.canary_routers``) and the
per-agent online evaluation surface (``app.evaluation.online``) call
:func:`normalize_online_evaluators`; the ``code_prefix`` keeps each caller's error-code
family (``canary.evaluator_unsupported`` vs ``online_eval.evaluator_unsupported``)
stable for the frontend.

This module also hosts the evaluator **polarity** table the results surfaces and the
recommendation evidence share (the AWS wrapper that used to carry it is deleted by W-E1).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import DEFAULT_WORKSPACE_ID
from app.core.errors import AppError
from app.evaluation.models import EvalEvaluator
from app.services.strands_eval.evaluators import BUILTIN_LEVELS, TRAJECTORY_LEVELS
from app.services.strands_eval.judge_placeholders import (  # noqa: F401 — re-exported
    GROUND_TRUTH_PLACEHOLDERS,
)

# The pair every experiment used before the set became selectable; also the
# create-form default on the online evaluation sub-page.
ONLINE_EVAL_DEFAULT = ("Builtin.GoalSuccessRate", "Builtin.Helpfulness")
ONLINE_EVAL_MAX = 10  # the Global service capped the list at 10; the UI still does

# Judge placeholders fed from dataset reference inputs rather than from the trace itself
# (``GROUND_TRUTH_PLACEHOLDERS``, one catalogue in ``strands_eval.judge_placeholders``).
# ``{context}``, ``{assistant_turn}``, ``{available_tools}``, ``{tool_turn}`` and
# ``{actual_tool_trajectory}`` are trace-derived — rendered from the session spans
# (N17-obs311) — and stay valid online.

# Evaluators where a HIGHER score means the agent misbehaved. Every other evaluator —
# including Harmfulness / Stereotyping, which score 1.0 for a clean answer — is
# higher-is-better; custom judges are +1 (the platform's own default scale is
# pass=1.0 / fail=0.0).
LOWER_IS_BETTER_EVALUATORS = frozenset({"Builtin.Refusal"})


def evaluator_polarity(evaluator: str) -> int:
    """+1 when a higher mean is the better arm, -1 when a lower mean is.

    Accepts a bare id (``Builtin.Refusal``) or an evaluator ARN
    (``arn:<partition>:bedrock-agentcore:::evaluator/Builtin.Refusal``).
    """
    return -1 if (evaluator or "").rsplit("/", 1)[-1] in LOWER_IS_BETTER_EVALUATORS else 1


def ground_truth_placeholders(instructions: str | None) -> list[str]:
    """Ground-truth fields a custom judge's instructions reference (aliases included,
    N17-obs437: ``evaluators_local.ground_truth_placeholders``)."""
    from app.evaluation import evaluators_local

    return evaluators_local.ground_truth_placeholders(instructions)


def is_categorical_scale(scale: Any) -> bool:
    """A custom judge's ``rating_scale`` with entries but no numeric ``value`` — the judge
    scores on categories, so there is no mean to compare two arms by (the same rule
    ``recommendations._evaluator_problem`` applies to a recommendation's target)."""
    items = [item for item in (scale or []) if isinstance(item, dict)]
    return bool(items) and not any(isinstance(item.get("value"), int | float) for item in items)


def normalize_online_evaluators(
    ids: Sequence[str] | None,
    db: Any = None,
    *,
    code_prefix: str = "experiment",
    workspace_id: str | None = None,
    require_numeric: bool = False,
) -> list[str]:
    """Validate an operator-chosen evaluator set for online evaluation.

    ``None``/empty falls back to :data:`ONLINE_EVAL_DEFAULT`. Custom ids are read
    from ``eval_evaluators`` after dedup and the count cap, so a built-in-only
    selection — the common case — touches no ledger at all. ``db`` is the caller's
    SQLAlchemy session; anything else in that slot (W-E3's ``stage_gateway`` still
    passes a boto control client positionally) is ignored and a short-lived
    session is opened instead.

    ``require_numeric`` (upstream b5eb3dc8 parity, N12-obs46) additionally refuses a custom
    judge whose rating scale is categorical (``{code_prefix}.evaluator_categorical``): a
    caller that compares arm means (the canary verdict) needs a numeric score. It reads
    the same ``eval_evaluators`` row, so it costs no extra query.
    """
    code = f"{code_prefix}.evaluator_unsupported"
    chosen: list[str] = []
    for raw in ids or ():
        evaluator = str(raw).strip()
        if not evaluator or evaluator in chosen:
            continue
        if evaluator in TRAJECTORY_LEVELS:
            raise AppError(
                code,
                f"{evaluator} scores against dataset ground truth, which online "
                "evaluation does not carry — use a batch evaluation run instead",
                {"evaluator": evaluator},
                status_code=400,
            )
        if evaluator.startswith("Builtin.") and evaluator not in BUILTIN_LEVELS:
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
    custom = [e for e in chosen if not e.startswith("Builtin.")]
    if custom:
        _assert_custom_rows_online_safe(
            custom, db, code, workspace_id or DEFAULT_WORKSPACE_ID,
            categorical_code=f"{code_prefix}.evaluator_categorical" if require_numeric else None,
        )
    return chosen


def _assert_custom_rows_online_safe(
    evaluators: list[str], db: Any, code: str, workspace_id: str, *,
    categorical_code: str | None = None,
) -> None:
    if not isinstance(db, Session):
        raise TypeError("normalize_online_evaluators needs the ledger Session (N12 E2-1)")
    rows = {
        row.id: row
        for row in db.scalars(
            select(EvalEvaluator).where(
                EvalEvaluator.workspace_id == workspace_id,
                EvalEvaluator.id.in_(evaluators),
            )
        )
    }
    for evaluator in evaluators:
        row = rows.get(evaluator)
        if row is None:
            raise AppError(
                code,
                f"unknown evaluator {evaluator}",
                {"evaluator": evaluator},
                status_code=400,
            )
        # N17-obs437: every definition kind — a judge's rubric (aliases included), a derived
        # evaluator's base, a managed code evaluator's reference rules
        from app.evaluation import coverage, evaluators_local

        placeholders = coverage.ground_truth_needs(
            evaluators_local.definition_of(row), db, workspace_id)
        if placeholders:
            rendered = ", ".join(f"{{{p}}}" for p in placeholders)
            raise AppError(
                code,
                f"{evaluator} references {rendered}, which is ground truth online "
                "evaluation does not carry — use a batch evaluation run instead",
                {"evaluator": evaluator, "placeholders": placeholders},
                status_code=400,
            )
        if categorical_code and is_categorical_scale(row.rating_scale):
            raise AppError(
                categorical_code,
                f"{evaluator} rates on a categorical scale, which gives no numeric score to "
                "compare the two versions by — use a judge with a numerical rating scale",
                {"evaluator": evaluator},
                status_code=400,
            )


def refuse_hidden_evaluators(
    db: Session, workspace_id: str, viewer: Any, ids: Sequence[str] | None, *,
    code_prefix: str = "experiment",
) -> None:
    """A custom evaluator workspace isolation hides from ``viewer`` is refused exactly
    like an unknown id (``{code_prefix}.evaluator_unsupported``, 400) — before any row
    is written. Built-ins are never hidden; a non-filtering viewer costs no query."""
    from app.services import visibility

    if not getattr(viewer, "filters", False):
        return
    custom = [
        str(e).strip() for e in ids or ()
        if str(e).strip() and not str(e).strip().startswith("Builtin.")
    ]
    if not custom:
        return
    rows = db.scalars(
        select(EvalEvaluator).where(
            EvalEvaluator.workspace_id == workspace_id, EvalEvaluator.id.in_(custom)
        )
    ).all()
    for row in rows:
        if not visibility.sees(viewer, db, row.created_by):
            raise AppError(
                f"{code_prefix}.evaluator_unsupported",
                f"unknown evaluator {row.id}",
                {"evaluator": row.id},
                status_code=400,
            )
