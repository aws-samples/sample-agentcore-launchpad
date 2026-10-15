"""Spend budgets for long-running jobs: the payer, estimates and boundary stops.

Child 2 of the budgets design (``services/budgets.py`` is the gate, the ledger
and the settings; ``services/usage_meter.py`` writes the rows). A job — an eval
run, an experiment or canary action, a recommendation, a Skill Lab job — runs on
a worker thread long after the request that started it, so:

- **The payer is stored on the row when the work starts** (``EvalRun.created_by``,
  the experiment / canary *action caller*, ``EvalRecommendation.requested_by``,
  ``SkillLabJob.created_by``) and the worker rebuilds a ``JobBudget`` from it.
  The config admin / auth-disabled operator store no ``users.id``: their jobs are
  metered under their name and never stopped.
- **Exact where usage is reported:** each agent turn of the job gets a fresh
  ``UsageMeter`` (``JobBudget.meter``) wired into the invoke wrapper's
  ``on_usage`` sink — Harness ``metadata``, the Claude Agent SDK
  ``complete.usage``, ConverseStream usage. Such a turn never gets an estimate.
- **A flagged ``job_estimate`` where it is not**, debited as each unit starts:
  an opaque runtime's turn (the agent's recent measured average), a simulated
  persona's actor turns, the batch judges, a gateway traffic prompt, a Skill Lab
  task / step / taskgen job. A true-up (Skill Lab ``results.json``) or child 3's
  reconciliation supersedes them.
- **Boundary stops:** ``JobBudget.check()`` raises ``BudgetStop`` once the payer
  is at or over the limit. Each job calls it at its natural boundaries (before a
  scenario, a prompt, a batch evaluation, a train step); work already started
  finishes, so the overshoot is bounded by one unit per worker. The stop is
  reported as ``budget_exceeded`` with everything done so far kept.

A stop is not a refusal: ``exceeded()`` re-reads the payer's limit at every
boundary (an admin may raise it mid-job) and fails open on a ledger error — a
lost check must not kill a job.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.budgets import UsageEvent
from app.services import budgets, rates
from app.services.usage_meter import KIND_JOB_ESTIMATE, KIND_TOKENS, UsageMeter

logger = logging.getLogger("launchpad.usage")

STOP_REASON = "budget_exceeded"
# the measured turns an agent's "recent average" is taken over (design: N = 20,
# across payers)
RECENT_TURNS = 20
# The vendored trainer's default mini-batch (configs/skilleval/default.yaml
# train.batch_size, never overridden by runner.build_train_config): steps per
# epoch = ceil(train tasks / 4).
SKILL_LAB_TRAIN_BATCH = 4


class BudgetStop(Exception):  # noqa: N818 — a stop, not an error the caller mishandled
    """A job reached its payer's monthly limit at a boundary. Deliberately not an
    ``AppError``: the job's own error handling must not turn it into a failure."""

    def __init__(self, error: AppError) -> None:
        self.error = error
        super().__init__(stop_message(error))

    @property
    def message(self) -> str:
        return stop_message(self.error)

    @property
    def detail(self) -> dict[str, Any]:
        return dict(self.error.detail) if isinstance(self.error.detail, dict) else {}


def stop_message(error: AppError) -> str:
    """``budget_exceeded: monthly budget reached: $x of $y used; it resets on …`` —
    the stop text a job writes into its ``error`` column; ``is_budget_stop`` and
    the console read the prefix back."""
    return f"{STOP_REASON}: {error.message}"


def is_budget_stop(text: str | None) -> bool:
    return bool(text) and str(text).startswith(STOP_REASON)


def stop_reason_of(error: str | None) -> str | None:
    """A row's derived ``stop_reason`` for the API: ``budget_exceeded`` when its
    error carries the budget-stop prefix (a run ``stopped``, a Skill Lab job
    ``interrupted`` or ``failed`` before it started), else None."""
    return STOP_REASON if is_budget_stop(error) else None


# ── the payer of a job ───────────────────────────────────────────────────────


class JobBudget:
    """The payer of one long-running job plus the context its rows carry."""

    def __init__(
        self,
        *,
        user_id: str | None,
        user_name: str | None,
        source: str,
        origin_workspace_id: str | None = None,
        agent_id: str | None = None,
        resource_id: str | None = None,
        ref_type: str | None = None,
        ref_id: str | None = None,
    ) -> None:
        self.user_id = user_id or None
        self.user_name = user_name
        self.source = source
        self.origin_workspace_id = origin_workspace_id
        self.agent_id = agent_id
        self.resource_id = resource_id
        self.ref_type = ref_type
        self.ref_id = ref_id

    @classmethod
    def for_user(
        cls, user_id: str | None, source: str, *, user_name: str | None = None, **ctx: Any,
    ) -> JobBudget:
        """From a stored ``users.id`` (a job row's payer). The display name is
        looked up when the row did not keep one."""
        name = user_name
        if user_id and name is None:
            name = UsageMeter.for_user(user_id, source).user_name
        return cls(user_id=user_id, user_name=name, source=source, **ctx)

    @classmethod
    def for_creator(cls, creator: Any, source: str, **ctx: Any) -> JobBudget:
        """From a ``services.creators.Creator`` (an action caller); None → an
        unknown, exempt payer."""
        return cls(
            user_id=getattr(creator, "user_id", None),
            user_name=getattr(creator, "name", None),
            source=source, **ctx,
        )

    @property
    def exempt(self) -> bool:
        """No ``users`` row: never limited, never stopped (still metered)."""
        return not self.user_id

    def exceeded(self, *, extra_usd: float = 0.0) -> AppError | None:
        """The payer's ``budget.exceeded`` error at this moment, or None. Re-reads
        the limit each time; ``extra_usd`` is spend incurred but not written yet. A
        ledger failure is logged and treated as "not over"."""
        if self.exempt:
            return None
        try:
            db = SessionLocal()
            try:
                payer = budgets.payer_for_user_id(db, self.user_id, self.user_name)
                return budgets.exceeded(db, payer, extra_usd=extra_usd)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 — a lost check must never kill a job
            logger.warning("job budget: could not check %s", self.user_id, exc_info=True)
            return None

    def check(self) -> None:
        """A boundary: raise ``BudgetStop`` once the payer is at or over the limit."""
        error = self.exceeded()
        if error is not None:
            raise BudgetStop(error)

    def meter(self, **overrides: Any) -> UsageMeter:
        """A fresh meter for one unit of this job (one agent turn, one call)."""
        fields: dict[str, Any] = {
            "user_id": self.user_id, "user_name": self.user_name, "source": self.source,
            "origin_workspace_id": self.origin_workspace_id, "agent_id": self.agent_id,
            "resource_id": self.resource_id, "ref_type": self.ref_type, "ref_id": self.ref_id,
        }
        fields.update(overrides)
        return UsageMeter(**fields)

    def estimate(
        self,
        usd: float | None,
        *,
        quantity: float,
        unit: str,
        detail: dict[str, Any] | None = None,
        model_id: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
    ) -> str | None:
        """Debit one flagged ``job_estimate`` row now; a ``session_id`` is also
        recorded in ``usage_sessions`` (the payer's session, for reconciliation)."""
        meter = self.meter(session_id=session_id, **({"agent_id": agent_id} if agent_id else {}))
        return meter.job_estimate(
            usd, quantity=quantity, unit=unit, detail=detail, model_id=model_id,
            upsert_session=bool(session_id),
        )


# ── per-unit estimates ───────────────────────────────────────────────────────


def recent_turn_cost(db: Session, agent_id: str | None) -> float | None:
    """The mean ``usd`` of the agent's last ``RECENT_TURNS`` measured turns (exact
    ``tokens`` rows, any payer, any source), or None when it has none.

    Only rows on a session are turns: a recommendation provider's model calls
    (gepa_lite) are also an exact ``tokens`` row carrying the agent's id, but one
    row is a whole optimizer job with no session — averaging it in would charge
    each opaque turn or traffic prompt a whole optimization."""
    if not agent_id:
        return None
    rows = (
        db.query(UsageEvent.usd)
        .filter(
            UsageEvent.agent_id == agent_id,
            UsageEvent.kind == KIND_TOKENS,
            UsageEvent.session_id.is_not(None),
            UsageEvent.estimated.is_(False),
            UsageEvent.superseded_by.is_(None),
            UsageEvent.usd.is_not(None),
        )
        .order_by(UsageEvent.created_at.desc())
        .limit(RECENT_TURNS)
        .all()
    )
    values = [float(usd) for (usd,) in rows if usd is not None]
    return round(sum(values) / len(values), 8) if values else None


def turn_estimate(db: Session, agent_id: str | None, *, fallback: str) -> tuple[float, str]:
    """``(usd, basis)`` for one opaque agent turn: the agent's recent measured
    average, else the ``fallback`` budget setting (``opaque_turn_estimate_usd`` for
    an eval replay turn, ``agent_turn_estimate_usd`` for a traffic prompt)."""
    recent = recent_turn_cost(db, agent_id)
    if recent is not None:
        return recent, "recent_average"
    return float(budgets.budget_settings(db)[fallback]), fallback


def persona_actor_usd(db: Session, actor_model_id: str | None, turns: int) -> float | None:
    """The simulated-persona actor's estimated cost for ``turns`` turns at its
    model's price, or None when that model has no price."""
    settings = budgets.budget_settings(db)
    usd, priced = rates.price_tokens(
        actor_model_id,
        settings["persona_actor_input_tokens"] * turns,
        settings["persona_actor_output_tokens"] * turns,
    )
    return usd if priced else None


def _builtin(evaluator_id: str) -> bool:
    return evaluator_id.startswith(("Builtin.", "ThirdParty."))


def judge_estimate(
    db: Session, evaluators: list[str], sessions: int,
) -> tuple[float, dict[str, Any]]:
    """The batch judges of an eval run before ``StartBatchEvaluation``: sessions ×
    evaluators, each evaluation priced at the calibrated built-in tokens per
    evaluation × the batch (Tier2) rate; a custom evaluator pays the per-evaluation
    fee plus those tokens at the platform judge model (when it has a price).

    One evaluation per (evaluator × session) — a trace- or tool-level evaluator
    judges more often; reconciliation (child 3) replaces this with the exact
    result-record count."""
    from app.core.config import get_settings

    fee = rates.agentcore_rates(db)
    cal = rates.calibration(db)
    tokens_in = cal["builtin_eval_input_tokens"]
    tokens_out = cal["builtin_eval_output_tokens"]
    builtin_each = (tokens_in / 1000 * fee["evaluation_batch_input_per_1k"]
                    + tokens_out / 1000 * fee["evaluation_batch_output_per_1k"])
    judge = (get_settings().judge_model_id or "").strip() or None
    judge_usd, _priced = rates.price_tokens(judge, tokens_in, tokens_out)
    custom_each = fee["evaluation_custom"] + (judge_usd or 0.0)
    per: dict[str, float] = {}
    for evaluator in dict.fromkeys(evaluators):
        per[evaluator] = round(builtin_each if _builtin(evaluator) else custom_each, 8)
    total = round(sum(per.values()) * max(0, sessions), 8)
    return total, {"estimate": "judges", "sessions": sessions,
                   "per_evaluation_usd": per, "judge_model_id": judge}


def skill_lab_units(job_type: str, command: list[str], params: dict[str, Any] | None,
                    counts: dict[str, Any] | None = None) -> int:
    """The units a Skill Lab job's estimate counts: eval → tasks (``--limit``
    applied), train → total steps (epochs × ceil(train tasks / batch)), taskgen → 1."""
    import json
    from pathlib import Path

    params = params or {}
    if job_type == "taskgen":
        return 1
    if job_type == "eval":
        try:
            tasks_path = Path(command[command.index("--tasks") + 1])
            count = len(json.loads(tasks_path.read_text(encoding="utf-8")))
        except (ValueError, IndexError, OSError):
            count = int((counts or {}).get("tasks") or 0)
        limit = int(params.get("limit") or 0)
        return max(0, min(count, limit) if limit > 0 else count)
    train = int((counts or {}).get("train") or 0)
    epochs = max(1, int(params.get("epochs") or 1))
    return epochs * max(1, math.ceil(train / SKILL_LAB_TRAIN_BATCH)) if train else epochs


SKILL_LAB_UNIT_KEYS = {
    "eval": ("skill_lab_eval_task_usd", "task"),
    "train": ("skill_lab_train_step_usd", "step"),
    "taskgen": ("skill_lab_taskgen_job_usd", "job"),
}


def skill_lab_unit_usd(db: Session, job_type: str) -> tuple[float, str]:
    key, unit = SKILL_LAB_UNIT_KEYS.get(job_type, SKILL_LAB_UNIT_KEYS["eval"])
    return float(budgets.budget_settings(db)[key]), unit


# ── the "estimate exceeds what is left" notice on create ─────────────────────


def notice(db: Session, user_id: str | None, estimate_usd: float | None) -> dict[str, Any] | None:
    """For a budgeted payer whose job's estimate is more than they have left:
    ``{estimate_usd, remaining_usd, limit_usd}`` — the job is still allowed and
    stops at its next boundary once the limit is reached. None otherwise (an
    unlimited or exempt payer, or an estimate that fits)."""
    if not user_id or estimate_usd is None:
        return None
    payer = budgets.payer_for_user_id(db, user_id)
    left = budgets.remaining(db, payer)
    if left is None or estimate_usd <= left:
        return None
    return {"estimate_usd": round(float(estimate_usd), 6), "remaining_usd": left,
            "limit_usd": payer.limit_usd, "period": budgets.period_of(),
            "resets_at": budgets.resets_at(budgets.period_of())}


def eval_run_forecast(
    db: Session,
    *,
    agent: Any,
    items: list[dict[str, Any]],
    evaluators: list[str],
    mode: str,
    actor_model_id: str | None,
    session_count: int | None,
) -> float:
    """A whole eval run's forecast for the create notice: replayed turns at the
    agent's turn estimate, persona actors at max turns, and the judges."""
    from app.evaluation import simulation
    from app.evaluation.scenarios import normalize_scenarios, scenario_prompts

    total = 0.0
    sessions = session_count or 0
    if agent is not None and items:
        _model, token_metered = budgets.agent_model(agent)
        turn, _basis = turn_estimate(
            db, agent.id,
            fallback="agent_turn_estimate_usd" if token_metered else "opaque_turn_estimate_usd",
        )
        scenarios = normalize_scenarios(items)
        sessions = len(scenarios)
        for scenario in scenarios:
            if simulation.is_simulated(scenario):
                turns = int(scenario.get("max_turns") or 10)
                total += turn * turns + (persona_actor_usd(db, actor_model_id, turns) or 0.0)
            else:
                total += turn * len(scenario_prompts(scenario))
    if mode == "evaluators" and evaluators and sessions:
        total += judge_estimate(db, evaluators, sessions)[0]
    return round(total, 6)


def ledger_rows(ref_type: str, ref_id: str, *, kind: str = KIND_JOB_ESTIMATE) -> list[str]:
    """Ids of a job's live (not superseded) rows of one kind — a true-up's targets."""
    db = SessionLocal()
    try:
        return [
            row_id for (row_id,) in db.query(UsageEvent.id).filter(
                UsageEvent.ref_type == ref_type, UsageEvent.ref_id == ref_id,
                UsageEvent.kind == kind, UsageEvent.superseded_by.is_(None),
            )
        ]
    finally:
        db.close()
