"""Referrers of an Agent row that refuse its removal (N16-obs111 / N16-obs111a / N17-obs18).

A Runtime canary whose status is not terminal (`CANARY_BLOCKING_STATUSES`) still routes
traffic to — or holds versions of — its champion and challenger; an evaluation run that is
ACTIVE (`evaluation.service.ACTIVE_STATUSES`, the same definition that blocks dataset deletes)
is about to replay against the agent's Runtime. Every path that removes an Agent row asks
here first and refuses with 409 ``agent.in_use {canaries, runs}`` while one exists: the
ordinary delete (``routers/agents.teardown_agent_row``) and the system-preset uninstall
(``system_agents.service.uninstall_preset``). A re-publish asks for the canaries only (a
queued run is designed to pick up the version live at dispatch, §22). The architect purge is
NOT a caller: it cleans the conversation's canaries before its agent step by design.
"""
from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.evaluation.models import EvalRun
from app.evaluation.service import ACTIVE_STATUSES
from app.models.ledger import Agent
from app.optimization.models import CANARY_BLOCKING_STATUSES, RuntimeCanary


def blocking_canaries(db: Session, agent_id: str) -> list[RuntimeCanary]:
    """Non-terminal canaries naming ``agent_id`` as champion or challenger, oldest first.
    Matched on the agent id alone (ids are global): a canary naming this agent routes to it
    whatever its own ``workspace_id`` column says (legacy NULL rows included)."""
    return (
        db.query(RuntimeCanary)
        .filter(
            RuntimeCanary.status.in_(CANARY_BLOCKING_STATUSES),
            or_(RuntimeCanary.champion_agent_id == agent_id,
                RuntimeCanary.challenger_agent_id == agent_id),
        )
        .order_by(RuntimeCanary.created_at.asc())
        .all()
    )


def blocking_runs(db: Session, agent_id: str) -> list[EvalRun]:
    """ACTIVE evaluation runs (queued / invoking / waiting / evaluating) naming ``agent_id``,
    oldest first — the runs that would dispatch against a torn-down Runtime (N17-obs18)."""
    return (
        db.query(EvalRun)
        .filter(EvalRun.agent_id == agent_id, EvalRun.status.in_(ACTIVE_STATUSES))
        .order_by(EvalRun.created_at.asc())
        .all()
    )


REFERRER_KINDS = ("canaries", "runs")


def refuse_agent_in_use(
    db: Session, agent: Agent, *, action: str = "removing the agent",
    kinds: tuple[str, ...] = REFERRER_KINDS,
) -> None:
    """409 ``agent.in_use {canaries: [{id, name, status, stage}], runs: [{id, name, status}]}``
    while a running canary — or, N17-obs18, an ACTIVE evaluation run — names the agent:
    members and administrators alike, no force; `completed` / `rolled_back` / `cleaned`
    canaries and terminal runs never block. The operator settles the canary (`cleanup`,
    `rollback` or `complete`) or stops / waits for the run first. ``kinds`` selects the
    referrers consulted: the delete asks for both, a re-publish for the canaries only."""
    canaries = blocking_canaries(db, agent.id) if "canaries" in kinds else []
    runs = blocking_runs(db, agent.id) if "runs" in kinds else []
    if not canaries and not runs:
        return
    parts = []
    if canaries:
        parts.append(f"{len(canaries)} Runtime canary(ies) on this agent are still running — "
                     "settle them (cleanup, rollback or complete)")
    if runs:
        parts.append(f"{len(runs)} evaluation run(s) on this agent are still active — stop "
                     "them or wait for them to finish")
    raise AppError(
        "agent.in_use",
        "; ".join(parts) + f" before {action}",
        {"canaries": [
            {"id": c.id, "name": c.name, "status": c.status, "stage": c.stage} for c in canaries
        ], "runs": [{"id": r.id, "name": r.name, "status": r.status} for r in runs]},
        status_code=409,
    )
