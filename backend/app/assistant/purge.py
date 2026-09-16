"""Deleting an architect-assistant conversation together with what it created.

The History panel offers CLEAR on a conversation. A conversation may have produced
real resources — an approved proposal deployed an Agent (Harness + execution role),
an evaluation-assets operation created AgentCore evaluators, a Lambda with its role /
log group / policies and a local Dataset. Deleting only the transcript would leave
those orphaned and unattributed, so the purge removes them too, **through the same
paths that already delete them one by one** and in dependency order:

1. ``footprint`` — what would be removed and what blocks it (an in-flight turn, a
   queued / running / cleaning operation, a deployment job still running). The console
   shows this in the confirm dialog; nothing here touches AWS.
2. ``purge`` — refuses on any blocker (409, nothing deleted), then per operation runs
   the fenced ``cleanup_operation`` (identity-checked, checkpointed); an operation that
   does not reach ``cleaned`` (a conflict, an unknown-ownership resource) stops the purge
   with a 409 naming it — the conversation stays so the conflict remains reviewable.
   Then the local Datasets the operations created, then every Agent an approval
   deployed (``delete_agent_row``: preset refusal, AWS resource, role, ledger), and only
   then the ledger rows (operations, plans, proposals, messages, conversation).

Authorization: owner-bound like every conversation route; when the footprint holds
anything beyond ledger rows (cloud assets or an Agent) the caller must also be an
administrator — the same bar the individual cleanup / deploy routes set.

The purge is not atomic across AWS calls (no transaction spans them); it is idempotent
in the sense that a retry after a partial failure continues from what is left.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.assistant import evaluation_assets as assets
from app.core.errors import AppError
from app.evaluation.models import EvalDataset
from app.models.assistant import (
    AssistantConversation,
    AssistantEvaluationPlan,
    AssistantMessage,
    AssistantProposal,
    EvaluationAssetOperation,
)
from app.models.ledger import Agent, Job
from app.services.workspace import WorkspaceContext

logger = logging.getLogger(__name__)

LIVE_JOB_STATUSES = ("queued", "running")
LIVE_OPERATION_STATUSES = ("queued", "running", "cleaning")


def _agents_of(db: Session, conversation_id: str) -> list[Agent]:
    """Every Agent an approved proposal of this conversation deployed and that is not
    deleted yet (one proposal → one agent; several revisions may have deployed)."""
    rows = (
        db.query(AssistantProposal)
        .filter(AssistantProposal.conversation_id == conversation_id,
                AssistantProposal.agent_id.isnot(None))
        .all()
    )
    seen: set[str] = set()
    out: list[Agent] = []
    for p in rows:
        if p.agent_id in seen:
            continue
        seen.add(p.agent_id)
        agent = db.get(Agent, p.agent_id)
        if agent is not None and agent.status != "deleted":
            out.append(agent)
    return out


def _operations_of(db: Session, conversation_id: str) -> list[EvaluationAssetOperation]:
    return (
        db.query(EvaluationAssetOperation)
        .filter(EvaluationAssetOperation.conversation_id == conversation_id)
        .order_by(EvaluationAssetOperation.created_at.asc())
        .all()
    )


def _datasets_of(db: Session, operations: list[EvaluationAssetOperation]) -> list[EvalDataset]:
    out: list[EvalDataset] = []
    seen: set[str] = set()
    for op in operations:
        if op.dataset_id and op.dataset_id not in seen:
            seen.add(op.dataset_id)
            ds = db.get(EvalDataset, op.dataset_id)
            if ds is not None:
                out.append(ds)
    return out


def footprint(db: Session, row: AssistantConversation) -> dict[str, Any]:
    """What a purge would remove, and what blocks it right now. Ledger reads only."""
    operations = _operations_of(db, row.id)
    agents = _agents_of(db, row.id)
    datasets = _datasets_of(db, operations)
    blockers: list[dict[str, str]] = []
    if row.active_turn is not None:
        blockers.append({"kind": "turn", "id": str(row.active_turn),
                         "reason": "a turn is still streaming"})
    if row.preparation_token:
        blockers.append({"kind": "preparation", "id": row.id,
                         "reason": "Skill import is still running"})
    for op in operations:
        if op.status in LIVE_OPERATION_STATUSES:
            blockers.append({"kind": "operation", "id": op.id,
                             "reason": f"evaluation-assets operation is {op.status}"})
    for p in db.query(AssistantProposal).filter(
            AssistantProposal.conversation_id == row.id,
            AssistantProposal.job_id.isnot(None)).all():
        job = db.get(Job, p.job_id)
        if job is not None and job.status in LIVE_JOB_STATUSES:
            blockers.append({"kind": "job", "id": job.id,
                             "reason": f"deployment job is {job.status}"})
    cloud_operations = [op for op in operations if op.status in assets.CLEANABLE_STATUSES]
    return {
        "conversation_id": row.id,
        "title": row.title,
        "turns": row.turns,
        "proposals": db.query(AssistantProposal)
                       .filter(AssistantProposal.conversation_id == row.id).count(),
        "agents": [{"id": a.id, "name": a.name, "status": a.status, "method": a.method}
                   for a in agents],
        "operations": [{"id": op.id, "status": op.status, "plan_revision": op.plan_revision,
                        "dataset_id": op.dataset_id,
                        "cloud_resources": sum(
                            1 for r in (op.resources or [])
                            if r.get("kind") != "dataset" and r.get("status") == "ready")}
                       for op in operations],
        "datasets": [{"id": d.id, "name": d.name, "item_count": len(d.items or []),
                      "cloud": bool(d.cloud)} for d in datasets],
        "blockers": blockers,
        # anything beyond ledger rows needs an administrator (cleanup / deploy parity)
        "requires_admin": bool(agents or cloud_operations),
    }


def purge(
    db: Session, row: AssistantConversation, workspace: WorkspaceContext, *, is_admin: bool,
    clients: assets.ClientFactory = assets._default_clients,
) -> dict[str, Any]:
    """Delete the conversation and everything it created (see module docstring).
    Raises ``AppError`` 409 when blocked or when an operation's cleanup leaves owned
    resources; 403 when assets exist and the caller is not an administrator."""
    fp = footprint(db, row)
    if fp["blockers"]:
        raise AppError(
            "assistant.conversation_busy",
            "the conversation still has work in flight — wait for it to finish (or stop it) "
            "before clearing: " + "; ".join(b["reason"] for b in fp["blockers"]),
            {"blockers": fp["blockers"]}, status_code=409,
        )
    if fp["requires_admin"] and not is_admin:
        raise AppError(
            "assistant.conversation_purge_admin",
            "this conversation created cloud assets or an Agent; only an administrator can "
            "clear it together with them",
            {"agents": fp["agents"], "operations": fp["operations"]}, status_code=403,
        )
    conversation_id = row.id
    removed: dict[str, Any] = {"operations_cleaned": [], "datasets": [], "agents": []}

    # 1. cloud assets of every operation, dependency first, fenced and checkpointed
    for op in _operations_of(db, conversation_id):
        if op.status not in assets.CLEANABLE_STATUSES:
            continue
        op = assets.cleanup_operation(db, op, workspace, clients=clients)
        if op.status != "cleaned":
            remaining = [r for r in (op.resources or [])
                         if r.get("status") not in ("deleted", "skipped", "pending", "blocked")]
            raise AppError(
                "assistant.conversation_assets_remain",
                f"evaluation-assets operation {op.id} could not be fully cleaned "
                f"(status {op.status}); review it in the Evaluation Assets panel — the "
                "conversation was kept so the remaining resources stay attributable",
                {"operation_id": op.id, "status": op.status,
                 "remaining": [{"kind": r.get("kind"), "name": r.get("name"),
                                "status": r.get("status"), "error": r.get("error")}
                               for r in remaining]},
                status_code=409,
            )
        removed["operations_cleaned"].append(op.id)

    # 2. the local Datasets those operations created (local rows only — the assistant
    #    never syncs them to AWS; a copy a member synced by hand is that member's asset
    #    and is listed as such in the footprint)
    for ds in _datasets_of(db, _operations_of(db, conversation_id)):
        removed["datasets"].append({"id": ds.id, "name": ds.name})
        db.delete(ds)
    db.commit()

    # 3. every Agent an approval deployed — the same teardown as DELETE /api/agents/{id}
    from app.routers.agents import delete_agent_row  # local import: router ↔ service cycle

    for agent in _agents_of(db, conversation_id):
        aws_deleted = delete_agent_row(db, agent, workspace)
        removed["agents"].append({"id": agent.id, "name": agent.name,
                                  "aws_resource_deleted": aws_deleted})

    # 4. the ledger rows, children first
    db.execute(delete(EvaluationAssetOperation)
               .where(EvaluationAssetOperation.conversation_id == conversation_id))
    db.execute(delete(AssistantEvaluationPlan)
               .where(AssistantEvaluationPlan.conversation_id == conversation_id))
    db.execute(delete(AssistantProposal)
               .where(AssistantProposal.conversation_id == conversation_id))
    db.execute(delete(AssistantMessage).where(AssistantMessage.conversation_id == conversation_id))
    db.execute(delete(AssistantConversation).where(AssistantConversation.id == conversation_id))
    db.commit()
    logger.info("assistant conversation %s purged: %s", conversation_id, removed)
    return {"deleted": True, "conversation_id": conversation_id, **removed}
