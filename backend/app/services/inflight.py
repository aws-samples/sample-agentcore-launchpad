"""Pre-restart in-flight inventory: what a backend restart will do to background work.

Every prod update restarts the backend, and nine startup hooks in `create_app`
(plus two read-time reapers) give in-flight rows four different fates. This module
reads the ledger once and names each in-flight row with the fate the restart gives
it, so an operator can decide *before* `systemctl restart` whether to wait.

Ledger-only on purpose: it answers "what will THIS process's restart do", not "what
is the AWS resource state" — no AWS client, no write. It selects only the columns it
needs and skips a table or column the database does not have yet, so it also works
when the checkout is ahead of the ledger (after `git merge`, before the restart
runs `_migrate`).

`STARTUP_HOOKS` and `resumable_job_types()` are drift-guarded against the source
of `create_app` and `resume_pending_jobs` (tests/test_inflight.py): a new startup
hook or job starter fails the suite until this inventory knows about it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.evaluation.agentcore_eval import REC_TERMINAL
from app.evaluation.models import EvalPipeline, EvalRecommendation, EvalRun
from app.evaluation.recommendations import PROVIDER_JOB_PREFIX
from app.evaluation.service import INTERRUPTED_STATUSES
from app.models.assistant import AssistantConversation, EvaluationAssetOperation
from app.models.ledger import Job, PolicyChange
from app.optimization.models import Experiment, RuntimeCanary
from app.skill_lab.models import SkillLabJob

# --- the closed set of restart outcomes ------------------------------------------
RESUMES = "resumes"  # a startup hook re-runs it from its last checkpoint
RECONCILES = "reconciles"  # a startup hook settles it from AWS (re-poll, no replay)
FAILS_INTERRUPTED = "fails_interrupted"  # a startup hook fails it as interrupted
FAILS_ON_READ = "fails_on_read"  # the next read of the row fails it (journal is silent)
CLEARED_RETRY = "cleared_retry"  # an action flag is cleared; the user must retry
RESTART_SAFE = "restart_safe"  # the work lives on AWS; the next read refreshes it
UNHANDLED = "unhandled"  # nothing picks it up: a restart strands the row

OUTCOMES = (RESUMES, RECONCILES, FAILS_INTERRUPTED, FAILS_ON_READ, CLEARED_RETRY,
            RESTART_SAFE, UNHANDLED)
# outcomes after which nothing is lost — the restart may proceed
BENIGN_OUTCOMES = frozenset({RESUMES, RECONCILES, RESTART_SAFE})

EXIT_NOTHING = 0  # nothing in flight (or only restart_safe work)
EXIT_RESUMES = 2  # only resumes/reconciles: restart is fine, expect resume lines
EXIT_DISRUPTS = 3  # something would be failed, cleared or stranded

# --- inventory kinds --------------------------------------------------------------
JOB = "job"
EVAL_ASSET_OPERATION = "evaluation_asset_operation"
EVAL_RUN = "eval_run"
EXPERIMENT_ACTION = "experiment_action"
CANARY_ACTION = "canary_action"
SKILL_LAB_JOB = "skill_lab_job"
POLICY_CHANGE = "policy_change"
EVAL_PIPELINE = "eval_pipeline"
EVAL_RECOMMENDATION = "eval_recommendation"
ASSISTANT_TURN = "assistant_turn"
ASSISTANT_PREPARATION = "assistant_preparation"

# Every call in the `if resume_jobs:` block of `create_app`, by the name main.py
# calls it, with the inventory kinds it settles. Hooks that touch no ledger row map
# to a reason instead. Drift-guarded: tests/test_inflight.py parses main.py.
STARTUP_HOOKS: dict[str, tuple[str, ...] | str] = {
    "clear_stale_turn_claims": (ASSISTANT_TURN, ASSISTANT_PREPARATION),
    "resume_pending_jobs": (JOB,),
    "resume_evaluation_assets": (EVAL_ASSET_OPERATION,),
    "resume_interrupted_runs": (EVAL_RUN,),
    "clear_stale_running_actions": (EXPERIMENT_ACTION,),
    "sweep_skill_lab_jobs": (SKILL_LAB_JOB,),
    "clear_stale_canary_actions": (CANARY_ACTION,),
    "reconcile_policy_changes": (POLICY_CHANGE,),
    "local_exec.reap_orphan_containers":
        "kills leftover studio exec containers + scratch dirs; transient, no ledger row",
    "start_auto_refresh": "starts the model-price refresh thread; restart-safe, no ledger row",
}

# Subsystems that fail an interrupted row only when it is next read — a startup
# journal never mentions them, which is why the inventory must.
READ_TIME_REAPERS = {
    EVAL_PIPELINE: "evaluation.pipeline_routers._reap",
    EVAL_RECOMMENDATION: "evaluation.recommendations.refresh",
}

_JOB_ACTIVE = ("queued", "running")
_ASSET_OP_ACTIVE = ("queued", "running")  # resume_operations
_SKILL_LAB_ACTIVE = ("queued", "running")  # sweep_interrupted_jobs
_POLICY_ACTIVE = ("pending", "running")  # reconcile_policy_changes


def resumable_job_types() -> frozenset[str]:
    """The `Job.type` keys of `resume_pending_jobs`'s `starters` dict."""
    # Lazy, as in resume_pending_jobs: the uninstall worker's import chain reaches
    # the deployer pipeline.
    from app.assistant import purge as assistant_purge
    from app.services import workspace_bootstrap
    from app.system_agents import uninstall as system_uninstall

    return frozenset({
        "deploy_agent",
        workspace_bootstrap.JOB_TYPE,
        system_uninstall.JOB_TYPE,
        assistant_purge.JOB_TYPE,
    })


@dataclass(frozen=True)
class InflightItem:
    kind: str
    id: str
    workspace_id: str | None
    status: str  # the row's status, or the action/turn that holds the flag
    label: str  # what the row is, for a human
    since: datetime | None  # started/updated/heartbeat — the freshest progress stamp
    age_seconds: float | None
    restart_outcome: str
    handled_by: str | None  # the hook (or read-time reaper) that settles it

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["since"] = self.since.isoformat() if self.since else None
        return out


def exit_code(items: Iterable[InflightItem]) -> int:
    outcomes = {i.restart_outcome for i in items}
    if not outcomes - {RESTART_SAFE}:
        return EXIT_NOTHING
    if outcomes <= BENIGN_OUTCOMES:
        return EXIT_RESUMES
    return EXIT_DISRUPTS


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    # SQLite hands timezone-aware columns back naive; they were written as UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _first(*stamps: datetime | None) -> datetime | None:
    return next((_utc(s) for s in stamps if s is not None), None)


class _Reader:
    """Selects only the columns a kind needs, from tables the ledger actually has."""

    def __init__(self, db: Session) -> None:
        self.db = db
        inspector = inspect(db.get_bind())
        self._columns = {
            name: {c["name"] for c in inspector.get_columns(name)}
            for name in inspector.get_table_names()
        }
        self.skipped: list[str] = []

    def rows(self, model: Any, columns: list[str], *where: Callable[[Any], Any]) -> list[Any]:
        table = model.__tablename__
        live = self._columns.get(table)
        if live is None:
            self.skipped.append(f"{table}: table not in this ledger yet")
            return []
        missing = [c for c in columns if c not in live]
        if missing:
            self.skipped.append(f"{table}: column(s) not in this ledger yet: "
                                f"{', '.join(missing)}")
            return []
        stmt = select(*(getattr(model, c) for c in columns))
        for clause in where:
            stmt = stmt.where(clause(model))
        return list(self.db.execute(stmt).all())


def collect_inflight(
    db: Session, *, now: datetime | None = None, skipped: list[str] | None = None
) -> list[InflightItem]:
    """Every in-flight ledger row and the fate a backend restart gives it.

    Read-only: a SELECT per kind, nothing else. ``skipped`` (when given) collects
    the tables/columns this ledger does not have yet.
    """
    now = _utc(now) or datetime.now(UTC)
    reader = _Reader(db)
    items: list[InflightItem] = []

    def add(kind: str, row_id: str, ws: str | None, status: str, label: str,
            since: datetime | None, outcome: str, handled_by: str | None) -> None:
        since = _utc(since)
        age = (now - since).total_seconds() if since else None
        items.append(InflightItem(kind, row_id, ws, status, label, since, age, outcome,
                                  handled_by))

    resumable = resumable_job_types()
    for r in reader.rows(Job, ["id", "workspace_id", "type", "status", "updated_at"],
                         lambda m: m.status.in_(_JOB_ACTIVE)):
        if r.type in resumable:
            add(JOB, r.id, r.workspace_id, r.status, r.type, r.updated_at, RESUMES,
                "resume_pending_jobs")
        else:
            add(JOB, r.id, r.workspace_id, r.status,
                f"{r.type} — no resume starter: the row stays {r.status} forever",
                r.updated_at, UNHANDLED, None)

    for r in reader.rows(EvaluationAssetOperation,
                         ["id", "workspace_id", "status", "heartbeat_at", "updated_at"],
                         lambda m: m.status.in_(_ASSET_OP_ACTIVE)):
        add(EVAL_ASSET_OPERATION, r.id, r.workspace_id, r.status,
            "evaluation-asset materialization", _first(r.heartbeat_at, r.updated_at),
            RESUMES, "resume_evaluation_assets")

    for r in reader.rows(EvalRun, ["id", "workspace_id", "status", "mode", "name",
                                   "agent_name", "batch_eval_id", "updated_at"],
                         lambda m: m.status.in_(INTERRUPTED_STATUSES)):
        what = f"{r.mode} run {r.name or r.agent_name}"
        # the exact split resume_interrupted_runs makes
        if r.status == "evaluating" and r.batch_eval_id:
            add(EVAL_RUN, r.id, r.workspace_id, r.status,
                f"{what} — batch {r.batch_eval_id} is re-polled", r.updated_at, RECONCILES,
                "resume_interrupted_runs")
        else:
            add(EVAL_RUN, r.id, r.workspace_id, r.status,
                f"{what} — no batch started yet: failed, submit it again", r.updated_at,
                FAILS_INTERRUPTED, "resume_interrupted_runs")

    for model, kind, hook in ((Experiment, EXPERIMENT_ACTION, "clear_stale_running_actions"),
                              (RuntimeCanary, CANARY_ACTION, "clear_stale_canary_actions")):
        for r in reader.rows(model, ["id", "workspace_id", "name", "running_action",
                                     "updated_at"],
                             lambda m: m.running_action.isnot(None)):
            add(kind, r.id, r.workspace_id, r.running_action,
                f"{r.name} — action cleared, the user must retry it", r.updated_at,
                CLEARED_RETRY, hook)

    for r in reader.rows(SkillLabJob, ["id", "workspace_id", "type", "status", "taskset_name",
                                       "started_at", "created_at"],
                         lambda m: m.status.in_(_SKILL_LAB_ACTIVE)):
        then = ("resumable on request from the last completed step" if r.type == "train"
                else "submit it again")
        add(SKILL_LAB_JOB, r.id, r.workspace_id, r.status,
            f"{r.type} {r.taskset_name} — subprocess dies; {then}",
            _first(r.started_at, r.created_at), FAILS_INTERRUPTED, "sweep_skill_lab_jobs")

    for r in reader.rows(PolicyChange, ["id", "workspace_id", "status", "gateway_name",
                                        "policy_name", "started_at", "created_at"],
                         lambda m: m.status.in_(_POLICY_ACTIVE)):
        target = f"{r.gateway_name}/{r.policy_name}" if r.policy_name else r.gateway_name
        add(POLICY_CHANGE, r.id, r.workspace_id, r.status,
            f"policy change on {target} — settled from AWS, never replayed",
            _first(r.started_at, r.created_at), RECONCILES, "reconcile_policy_changes")

    for r in reader.rows(EvalPipeline, ["id", "workspace_id", "status", "name", "updated_at"],
                         lambda m: m.status == "running"):
        add(EVAL_PIPELINE, r.id, r.workspace_id, r.status,
            f"data pipeline {r.name} — failed when next read", r.updated_at, FAILS_ON_READ,
            READ_TIME_REAPERS[EVAL_PIPELINE])

    for r in reader.rows(EvalRecommendation, ["id", "workspace_id", "status", "kind",
                                              "recommendation_id", "updated_at"],
                         lambda m: m.status.notin_(REC_TERMINAL)):
        if r.recommendation_id.startswith(PROVIDER_JOB_PREFIX):
            add(EVAL_RECOMMENDATION, r.id, r.workspace_id, r.status,
                f"{r.kind} provider job — its thread dies; failed when next read",
                r.updated_at, FAILS_ON_READ, READ_TIME_REAPERS[EVAL_RECOMMENDATION])
        else:
            add(EVAL_RECOMMENDATION, r.id, r.workspace_id, r.status,
                f"{r.kind} AgentCore job — refreshed from GetRecommendation on read",
                r.updated_at, RESTART_SAFE, READ_TIME_REAPERS[EVAL_RECOMMENDATION])

    for r in reader.rows(AssistantConversation,
                         ["id", "workspace_id", "title", "active_turn", "active_turn_started_at"],
                         lambda m: m.active_turn.isnot(None)):
        add(ASSISTANT_TURN, r.id, r.workspace_id, f"turn {r.active_turn}",
            f"architect conversation {r.title} — the open stream dies, the claim is freed",
            r.active_turn_started_at, FAILS_INTERRUPTED, "clear_stale_turn_claims")

    for r in reader.rows(AssistantConversation,
                         ["id", "workspace_id", "title", "preparation_token", "updated_at"],
                         lambda m: m.preparation_token.isnot(None)):
        add(ASSISTANT_PREPARATION, r.id, r.workspace_id, "preparing",
            f"architect conversation {r.title} — preparation claim cleared, retry it",
            r.updated_at, CLEARED_RETRY, "clear_stale_turn_claims")

    if skipped is not None:
        skipped.extend(reader.skipped)
    return items
