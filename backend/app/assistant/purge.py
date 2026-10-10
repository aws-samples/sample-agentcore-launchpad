"""Deleting an architect-assistant conversation together with what it created.

The History panel offers CLEAR on a conversation. A conversation may have produced
real resources — an approved proposal deployed an Agent (Harness + execution role),
an evaluation-assets operation created AgentCore evaluators, a Lambda with its role /
log group / policies and a local Dataset — and a member may have run a canary or an
experiment on that Agent (named Harness / runtime endpoints, an A/B test, online
evaluation configs, a dedicated gateway with its targets and trace delivery). Deleting
only the transcript would leave those orphaned and unattributed, and deleting the
Agent first fails: ``DeleteHarness`` answers ``ConflictException`` while a canary's
named endpoints still exist (issue #246). So the purge removes everything **through the
same paths that already delete it one by one**, in dependency order, and only counts a
deletion done once AWS reads it back as gone:

1. ``footprint`` — what would be removed and what blocks it (an in-flight turn or
   Skill import, a queued / running / cleaning operation, a deployment job, a canary or
   experiment action still running). The console shows this in the confirm dialog;
   nothing here touches AWS.
2. ``request_purge`` — refuses on any blocker (409) or missing permission (403) with
   nothing deleted. A transcript-only conversation is deleted inline. Anything else
   pins its **ownership evidence** (the Agents its approvals deployed, their canaries
   and experiments, the operations and Datasets) on a durable ``Job`` claimed by the
   conversation (``purge_job_id``) and returns it; the worker runs on a daemon thread
   and ``pipeline.resume_pending_jobs`` resumes it after a restart.
3. the worker, step by step, re-checking the claim (fence) before each one:
   canaries (``act_cleanup`` under the canary's own ``running_action`` claim, then a
   bounded readback until the A/B test, online-eval configs, gateway and BOTH named
   endpoints are gone), experiments (``act_cleanup``), evaluation-assets operations
   (the fenced ``cleanup_operation``), the local Datasets, then every Agent (resource
   delete → readback until ``GetHarness`` / ``GetAgentRuntime`` is not found → its own
   execution role → ledger), the canaries' retained candidate zips, and only then the
   ledger rows (operations, plans, proposals, messages, conversation).

Every step is idempotent (already-absent is success) and records a checkpoint on the
job; a ``skipped`` cleanup result, a resource still ``DELETING`` after the bounded
wait, a ``DELETE_FAILED``, a dependency this conversation does not own (another named
endpoint on its Harness) or a refusal stops the job as ``failed`` with an actionable
``blocker`` — never as success. The conversation and its ownership evidence stay; a
second CLEAR starts a new job that carries the pinned inventory and every verified
checkpoint forward, so it resumes where the last one stopped.

While a purge job is live the conversation refuses every write
(``service.accessible_conversation``) and its Agents refuse canary / experiment /
redeploy / convert / delete requests (``refuse_while_clearing``), so nothing new is
created behind the teardown. Canary and experiment ledger rows are kept (history).

Authorization: owner-bound like every conversation route; when the footprint holds
anything beyond ledger rows the caller must also hold the permission the individual
route sets — ``agents.deploy`` for cloud assets (cleanup parity) and ``agents.delete``
for an Agent (``DELETE /api/agents/{id}`` parity). Canary and experiment cleanup are
member routes, so they add none. Administrators hold every permission.
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, or_, update
from sqlalchemy.orm import Session

from app.assistant import evaluation_assets as assets
from app.core.config import DATA_DIR
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.deployer.pipeline import _append_log
from app.evaluation.models import EvalDataset
from app.models.assistant import (
    AssistantConversation,
    AssistantEvaluationPlan,
    AssistantMessage,
    AssistantProposal,
    EvaluationAssetOperation,
)
from app.models.ledger import Agent, Deployment, Job
from app.optimization.models import Experiment, RuntimeCanary
from app.services.workspace import WorkspaceContext, context_for_workspace

logger = logging.getLogger(__name__)

PERMISSION_AGENT_DELETE = "agents.delete"
JOB_TYPE = "purge_assistant_conversation"

LIVE_JOB_STATUSES = ("queued", "running")
LIVE_OPERATION_STATUSES = ("queued", "running", "cleaning")

# AWS deletion is asynchronous: a canary's named endpoints stayed DELETING for minutes
# in the incident. Bounded waits — a timeout is a retryable blocker, never success.
CANARY_GONE_TIMEOUT_S = 900
AGENT_GONE_TIMEOUT_S = 600
POLL_S = 10
_sleep = time.sleep  # injectable in tests
LOCK_DIR = DATA_DIR / "locks" / "assistant-purge"

_NOT_FOUND = {"ResourceNotFoundException", "NotFoundException"}
_FAILED_STATES = ("DELETE_FAILED", "FAILED", "DELETE_UNSUCCESSFUL")


class Blocked(Exception):
    """A step could not finish; the job stops with this as its actionable blocker."""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None,
                 status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.message, self.detail, self.status_code = (
            code, message, detail or {}, status_code)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail,
                "status_code": self.status_code}


class Superseded(Exception):
    """This worker no longer owns the conversation's teardown; every effect is skipped."""


# ---------------------------------------------------------------------------
# inventory (ledger reads only)
# ---------------------------------------------------------------------------


def _agent_ids(db: Session, conversation_id: str) -> list[str]:
    """Every Agent id an approved proposal of this conversation deployed, whatever its
    ledger status (one proposal → one agent; several revisions may have deployed)."""
    rows = (
        db.query(AssistantProposal.agent_id)
        .filter(AssistantProposal.conversation_id == conversation_id,
                AssistantProposal.agent_id.isnot(None))
        .order_by(AssistantProposal.created_at.asc())
        .all()
    )
    return list(dict.fromkeys(r[0] for r in rows))


def _agents_of(db: Session, conversation_id: str) -> list[Agent]:
    """The deployed Agents that are not deleted yet."""
    out: list[Agent] = []
    for agent_id in _agent_ids(db, conversation_id):
        agent = db.get(Agent, agent_id)
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


def _canaries_of(db: Session, workspace_id: str | None, agent_ids: list[str]
                 ) -> list[RuntimeCanary]:
    """Canaries of the conversation's Agents, in the conversation's workspace only —
    a dependency of an Agent this conversation deployed is owned through it."""
    if not agent_ids:
        return []
    return (
        db.query(RuntimeCanary)
        .filter(RuntimeCanary.workspace_id == workspace_id,
                or_(RuntimeCanary.champion_agent_id.in_(agent_ids),
                    RuntimeCanary.challenger_agent_id.in_(agent_ids)))
        .order_by(RuntimeCanary.created_at.asc())
        .all()
    )


def _experiments_of(db: Session, workspace_id: str | None, agent_ids: list[str]
                    ) -> list[Experiment]:
    if not agent_ids:
        return []
    return (
        db.query(Experiment)
        .filter(Experiment.workspace_id == workspace_id, Experiment.agent_id.in_(agent_ids))
        .order_by(Experiment.created_at.asc())
        .all()
    )


def _canary_pending(row: RuntimeCanary) -> bool:
    """A canary whose cleanup has not run, or ran and left resources behind."""
    from app.optimization import canary_service

    if row.status != "cleaned":
        return True
    return bool(canary_service.cleanup_blockers((row.artifacts or {}).get("cleanup") or []))


def _experiment_pending(row: Experiment) -> bool:
    if row.status != "cleaned":
        return True
    return any(r.get("status") == "skipped" for r in (row.artifacts or {}).get("cleanup") or [])


def _job_of(db: Session, row: AssistantConversation) -> Job | None:
    if not row.purge_job_id:
        return None
    job = db.get(Job, row.purge_job_id)
    return job if job is not None and job.type == JOB_TYPE else None


def live_purge_job(db: Session, row: AssistantConversation) -> Job | None:
    job = _job_of(db, row)
    return job if job is not None and job.status in LIVE_JOB_STATUSES else None


def _pinned_agent_ids(job: Job | None) -> list[str]:
    owned = ((job.payload or {}).get("owned") or {}) if job is not None else {}
    return [a["id"] for a in owned.get("agents") or []]


def _live_deploy_jobs(db: Session, agent_ids: list[str]) -> list[Job]:
    if not agent_ids:
        return []
    job_ids = [r[0] for r in db.query(Deployment.job_id)
               .filter(Deployment.agent_id.in_(agent_ids), Deployment.job_id.isnot(None)).all()]
    if not job_ids:
        return []
    return db.query(Job).filter(Job.id.in_(job_ids), Job.status.in_(LIVE_JOB_STATUSES)).all()


def _job_out(job: Job | None) -> dict[str, Any] | None:
    if job is None:
        return None
    payload = job.payload or {}
    return {"job_id": job.id, "status": job.status, "error": job.error,
            "blocker": payload.get("blocker"), "step": payload.get("step"),
            "updated_at": job.updated_at.isoformat() if job.updated_at else None}


def footprint(db: Session, row: AssistantConversation) -> dict[str, Any]:
    """What a purge would remove, and what blocks it right now. Ledger reads only."""
    operations = _operations_of(db, row.id)
    last_job = _job_of(db, row)
    agent_ids = list(dict.fromkeys(_agent_ids(db, row.id) + _pinned_agent_ids(last_job)))
    agents = [a for a in (db.get(Agent, i) for i in agent_ids) if a is not None]
    progress = ((last_job.payload or {}).get("progress") or {}) if last_job else {}
    pinned = set(_pinned_agent_ids(last_job))
    # an Agent a previous attempt already deleted from the ledger but did not yet read
    # back as gone on AWS is still part of the clear
    agents = [a for a in agents if a.status != "deleted" or (
        a.id in pinned and (progress.get(f"agent:{a.id}") or {}).get("state") != "done")]
    canaries = _canaries_of(db, row.workspace_id, agent_ids)
    experiments = _experiments_of(db, row.workspace_id, agent_ids)
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
    job_ids = {p.job_id for p in db.query(AssistantProposal).filter(
        AssistantProposal.conversation_id == row.id, AssistantProposal.job_id.isnot(None)).all()}
    live_jobs = {j.id: j for j in _live_deploy_jobs(db, agent_ids)}
    for job_id in job_ids:
        job = db.get(Job, job_id)
        if job is not None and job.status in LIVE_JOB_STATUSES:
            live_jobs[job.id] = job
    for job in live_jobs.values():
        blockers.append({"kind": "job", "id": job.id,
                         "reason": f"deployment job is {job.status}"})
    live = live_purge_job(db, row)
    for c in canaries:
        if c.running_action and live is None:
            blockers.append({"kind": "canary", "id": c.id,
                             "reason": f"canary {c.name} is running {c.running_action}"})
    for e in experiments:
        if e.running_action and live is None:
            blockers.append({"kind": "experiment", "id": e.id,
                             "reason": f"experiment {e.name} is running {e.running_action}"})
    cloud_operations = [op for op in operations if op.status in assets.CLEANABLE_STATUSES]
    return {
        "conversation_id": row.id,
        "title": row.title,
        "turns": row.turns,
        "proposals": db.query(AssistantProposal)
                       .filter(AssistantProposal.conversation_id == row.id).count(),
        "agents": [{"id": a.id, "name": a.name, "status": a.status, "method": a.method}
                   for a in agents],
        "canaries": [_canary_out(c) for c in canaries],
        "experiments": [{"id": e.id, "name": e.name, "agent_id": e.agent_id,
                         "status": e.status, "pending": _experiment_pending(e)}
                        for e in experiments],
        "operations": [{"id": op.id, "status": op.status, "plan_revision": op.plan_revision,
                        "dataset_id": op.dataset_id,
                        "cloud_resources": sum(
                            1 for r in (op.resources or [])
                            if r.get("kind") != "dataset" and r.get("status") == "ready")}
                       for op in operations],
        "datasets": [{"id": d.id, "name": d.name, "item_count": len(d.items or []),
                      "cloud": bool(d.cloud)} for d in datasets],
        "blockers": blockers,
        # anything beyond ledger rows needs the permission of the individual route
        "required_permissions": (
            ([assets.PERMISSION] if cloud_operations else [])
            + ([PERMISSION_AGENT_DELETE] if agents else [])
        ),
        "purge": _job_out(last_job),
    }


def _canary_out(row: RuntimeCanary) -> dict[str, Any]:
    artifacts = row.artifacts or {}
    setup = artifacts.get("setup") or {}
    kind = artifacts.get("kind") or "runtime"
    endpoints = [e for e in (setup.get("stable_endpoint"), setup.get("treatment_endpoint")) if e]
    if kind == "harness":
        from app.optimization import canary_harness

        endpoints = [canary_harness.control_endpoint(row.id),
                     canary_harness.treatment_endpoint(row.id)]
    return {
        "id": row.id, "name": row.name, "kind": kind, "status": row.status,
        "agent_id": row.champion_agent_id, "running_action": row.running_action,
        "pending": _canary_pending(row),
        # what the clear stops and removes (named for the confirm dialog)
        "resources": {"ab_test": bool(setup.get("ab_test_id")),
                      "gateway": setup.get("gateway_id"),
                      "endpoints": endpoints,
                      "online_evaluations": sum(
                          1 for t in (setup.get("champion") or {}, setup.get("challenger") or {})
                          if t.get("online_eval_id"))},
    }


def _ledger_only(fp: dict[str, Any]) -> bool:
    return not (fp["agents"] or fp["canaries"] or fp["experiments"] or fp["datasets"]
                or any(o["status"] in assets.CLEANABLE_STATUSES for o in fp["operations"]))


# ---------------------------------------------------------------------------
# guards other routes call while a purge job is live
# ---------------------------------------------------------------------------


def clearing_job_for_agent(db: Session, agent_id: str) -> Job | None:
    """The live purge job whose conversation deployed ``agent_id``, if any."""
    conversation_ids = [r[0] for r in db.query(AssistantProposal.conversation_id)
                        .filter(AssistantProposal.agent_id == agent_id).distinct().all()]
    for cid in conversation_ids:
        row = db.get(AssistantConversation, cid)
        if row is not None:
            job = live_purge_job(db, row)
            if job is not None:
                return job
    return None


def refuse_while_clearing(agent_id: str | None, action: str) -> None:
    """Refuse (409) a mutation of an Agent whose conversation is being cleared — a
    canary, experiment, redeploy or delete started now would race the teardown.
    Opens its own short-lived session, like ``system_agents.refuse_system_agent_id``."""
    if not agent_id:
        return
    db = SessionLocal()
    try:
        job = clearing_job_for_agent(db, agent_id)
    finally:
        db.close()
    if job is not None:
        raise AppError(
            "assistant.agent_clearing",
            f"this agent's architect conversation is being cleared (job {job.id}) — "
            f"{action} is refused until the clear finishes",
            {"agent_id": agent_id, "job_id": job.id, "action": action}, status_code=409,
        )


# ---------------------------------------------------------------------------
# request (route side)
# ---------------------------------------------------------------------------


def _delete_rows(db: Session, conversation_id: str) -> None:
    """The ledger rows, children first."""
    db.execute(delete(EvaluationAssetOperation)
               .where(EvaluationAssetOperation.conversation_id == conversation_id))
    db.execute(delete(AssistantEvaluationPlan)
               .where(AssistantEvaluationPlan.conversation_id == conversation_id))
    db.execute(delete(AssistantProposal)
               .where(AssistantProposal.conversation_id == conversation_id))
    db.execute(delete(AssistantMessage).where(AssistantMessage.conversation_id == conversation_id))
    db.execute(delete(AssistantConversation).where(AssistantConversation.id == conversation_id))


def _empty_result() -> dict[str, Any]:
    return {"operations_cleaned": [], "datasets": [], "agents": [], "canaries": [],
            "experiments": []}


def _inventory(db: Session, row: AssistantConversation, previous: Job | None) -> dict[str, Any]:
    """The ownership evidence a job pins: ids only, unioned with the previous attempt's
    (an Agent it already deleted from the ledger stays owned until read back gone)."""
    prior = ((previous.payload or {}).get("owned") or {}) if previous is not None else {}
    agents = {a["id"]: a for a in prior.get("agents") or []}
    for agent in _agents_of(db, row.id):
        agents.setdefault(agent.id, {"id": agent.id, "name": agent.name, "method": agent.method,
                                     "resource_id": agent.resource_id})
    agent_ids = list(agents)
    canaries = list(dict.fromkeys(
        [*(prior.get("canaries") or []),
         *(c.id for c in _canaries_of(db, row.workspace_id, agent_ids))]))
    experiments = list(dict.fromkeys(
        [*(prior.get("experiments") or []),
         *(e.id for e in _experiments_of(db, row.workspace_id, agent_ids))]))
    datasets = list(dict.fromkeys(
        [*(prior.get("datasets") or []),
         *(d.id for d in _datasets_of(db, _operations_of(db, row.id)))]))
    return {"agents": list(agents.values()), "canaries": canaries,
            "experiments": experiments, "datasets": datasets}


def request_purge(
    db: Session, row: AssistantConversation, *, can: Callable[[str], bool],
) -> tuple[Job | None, dict[str, Any] | None]:
    """Validate and claim a purge. Returns ``(job, None)`` for a cloud purge the caller
    must start (an already-live job is returned as is — idempotent), or
    ``(None, result)`` when the conversation was ledger-only and is already gone."""
    live = live_purge_job(db, row)
    if live is not None:
        return live, None
    fp = footprint(db, row)
    if fp["blockers"]:
        raise AppError(
            "assistant.conversation_busy",
            "the conversation still has work in flight — wait for it to finish (or stop it) "
            "before clearing: " + "; ".join(b["reason"] for b in fp["blockers"]),
            {"blockers": fp["blockers"]}, status_code=409,
        )
    missing = [p for p in fp["required_permissions"] if not can(p)]
    if missing:
        raise AppError(
            "auth.permission_required",
            "this conversation created cloud assets or an Agent; clearing it together with "
            f"them requires the {', '.join(repr(p) for p in missing)} permission",
            {"permission": missing[0], "missing": missing,
             "agents": fp["agents"], "operations": fp["operations"]}, status_code=403,
        )
    previous = _job_of(db, row)
    if _ledger_only(fp):
        _delete_rows(db, row.id)
        db.commit()
        logger.info("assistant conversation %s purged (ledger only)", row.id)
        return None, {"deleted": True, "conversation_id": row.id, **_empty_result()}
    # verified checkpoints carry forward, and so does an accepted delete request (the
    # next attempt re-reads instead of re-sending); nothing else does
    carried = {k: v for k, v in (((previous.payload or {}).get("progress") or {})
                                 if previous is not None else {}).items()
               if v.get("state") == "done" or v.get("delete_requested")}
    job = Job(workspace_id=row.workspace_id, type=JOB_TYPE, status="queued", payload={
        "conversation_id": row.id,
        "title": row.title,
        "owned": _inventory(db, row, previous),
        "progress": carried,
        "result": ((previous.payload or {}).get("result") if previous else None)
        or _empty_result(),
        "previous_job_id": previous.id if previous is not None else None,
    })
    db.add(job)
    db.flush()
    current = AssistantConversation.purge_job_id
    claimed = db.execute(
        update(AssistantConversation)
        .where(AssistantConversation.id == row.id,
               current.is_(None) if row.purge_job_id is None else current == row.purge_job_id)
        .values(purge_job_id=job.id)
        .execution_options(synchronize_session=False)
    ).rowcount
    if claimed != 1:
        db.rollback()
        raise AppError("assistant.conversation_clearing",
                       "another clear of this conversation started at the same time",
                       status_code=409)
    db.commit()
    _append_log(db, job.id, "queued", f"clear queued with {len(job.payload['owned']['agents'])} "
                f"agent(s), {len(job.payload['owned']['canaries'])} canary(ies)")
    return job, None


# ---------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------


def _acquire_lock(conversation_id: str):
    """Exclusive, non-blocking advisory lock for one conversation's teardown (released
    by the kernel when the holder dies, so a resume after a crash proceeds)."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_DIR / f"purge-{conversation_id}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release_lock(fd) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _is_not_found(exc: BaseException) -> bool:
    return type(exc).__name__ in _NOT_FOUND


class _Run:
    """One worker attempt: the fenced session, the pinned inventory and checkpoints."""

    def __init__(self, db: Session, job_id: str, workspace: WorkspaceContext,
                 clients: assets.ClientFactory) -> None:
        self.db, self.job_id, self.workspace, self.clients = db, job_id, workspace, clients
        job = db.get(Job, job_id)
        self.conversation_id: str = (job.payload or {})["conversation_id"]
        self.workspace_id = job.workspace_id
        self.owned: dict[str, Any] = (job.payload or {}).get("owned") or {}
        self.marker = f"clearing conversation {self.conversation_id[:8]}: "

    def fence(self) -> Job:
        """Re-read job + conversation; Superseded unless this job still owns the purge."""
        self.db.expire_all()
        job = self.db.get(Job, self.job_id)
        if job is None or job.type != JOB_TYPE or job.status != "running":
            raise Superseded(f"job {self.job_id} is not a running purge job")
        row = self.db.get(AssistantConversation, self.conversation_id)
        if row is None or row.purge_job_id != job.id or row.workspace_id != job.workspace_id:
            raise Superseded("the conversation is no longer claimed by this job")
        return job

    def done(self, key: str) -> bool:
        job = self.db.get(Job, self.job_id)
        return (((job.payload or {}).get("progress") or {}).get(key) or {}).get("state") == "done"

    def mark(self, key: str, state: str, detail: str = "", **extra: Any) -> None:
        job = self.fence()
        payload = dict(job.payload or {})
        progress = dict(payload.get("progress") or {})
        progress[key] = {**(progress.get(key) or {}), "state": state, "detail": detail,
                         "at": datetime.now(UTC).isoformat(), **extra}
        payload["progress"] = progress
        payload["step"] = {"key": key, "state": state, "detail": detail}
        job.payload = payload
        self.db.commit()
        _append_log(self.db, self.job_id, key, f"{state}{' · ' + detail if detail else ''}")

    def record(self, bucket: str, entry: Any) -> None:
        job = self.fence()
        payload = dict(job.payload or {})
        result = {**_empty_result(), **(payload.get("result") or {})}
        if entry not in result[bucket]:
            result[bucket] = [*result[bucket], entry]
        payload["result"] = result
        job.payload = payload
        self.db.commit()

    def log(self, message: str) -> None:
        _append_log(self.db, self.job_id, "teardown", message)


def _wait_gone(run: _Run, key: str, what: str, probe: Callable[[], list[dict[str, str]]],
               timeout_s: float) -> None:
    """Poll ``probe`` (the resources still present) until it is empty. A failed delete
    or the bounded timeout is a blocker; the checkpoint is not written here."""
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = probe()
        if not remaining:
            return
        listed = ", ".join(f"{r['category']} ({r.get('status') or '?'})" for r in remaining)
        if any(str(r.get("status", "")).upper() in _FAILED_STATES for r in remaining):
            raise Blocked("assistant.purge_delete_failed",
                          f"{what}: AWS reports {listed} — inspect it in the console, then "
                          "retry the clear", {"key": key, "remaining": remaining})
        if time.monotonic() >= deadline:
            raise Blocked("assistant.purge_still_deleting",
                          f"{what}: AWS is still deleting {listed} after {int(timeout_s)}s — "
                          "retry the clear in a few minutes",
                          {"key": key, "remaining": remaining})
        run.mark(key, "waiting", listed)
        _sleep(POLL_S)


def _claim_action(run: _Run, model: Any, row_id: str) -> None:
    """Take the row's own ``running_action`` claim (``cleanup``) so the canary /
    experiment routes refuse a concurrent action; a claim this conversation's earlier
    attempt left (crash) is re-adopted by its progress marker."""
    won = run.db.execute(
        update(model)
        .where(model.id == row_id,
               or_(model.running_action.is_(None),
                   (model.running_action == "cleanup") & model.progress.startswith(run.marker)))
        .values(running_action="cleanup", progress=run.marker + "starting cleanup", error=None)
        .execution_options(synchronize_session=False)
    ).rowcount
    run.db.commit()
    if won != 1:
        current = run.db.get(model, row_id)
        raise Blocked("assistant.purge_dependency_busy",
                      f"{model.__tablename__[:-1].replace('_', ' ')} {row_id} is running "
                      f"{current.running_action if current else '?'} — wait for it to finish, "
                      "then retry the clear", {"id": row_id})


def _release_action(run: _Run, model: Any, row_id: str, error: str | None = None) -> None:
    run.db.execute(
        update(model)
        .where(model.id == row_id, model.running_action == "cleanup",
               model.progress.startswith(run.marker))
        .values(running_action=None, progress=None, error=error)
        .execution_options(synchronize_session=False)
    )
    run.db.commit()


def _action_progress(run: _Run, model: Any, row_id: str) -> Callable[[str], None]:
    def progress(message: str) -> None:
        db = SessionLocal()
        try:
            db.execute(update(model).where(model.id == row_id)
                       .values(progress=(run.marker + message)[:300])
                       .execution_options(synchronize_session=False))
            db.commit()
        finally:
            db.close()
        _append_log(run.db, run.job_id, "teardown", message)
    return progress


def _step_canaries(run: _Run) -> None:
    from app.optimization import canary_service

    for canary_id in run.owned.get("canaries") or []:
        key = f"canary:{canary_id}"
        run.fence()
        if run.done(key):
            continue
        row = run.db.get(RuntimeCanary, canary_id)
        if row is None:
            run.mark(key, "done", "canary row gone")
            continue
        if row.workspace_id != run.workspace_id:
            raise Blocked("assistant.purge_unowned",
                          f"canary {canary_id} belongs to another workspace — not cleaned",
                          {"canary_id": canary_id})
        canary_service._refuse_system_subject(row, "cleanup")
        if _canary_pending(row):
            run.mark(key, "cleaning", f"stopping and removing canary {row.name}")
            _claim_action(run, RuntimeCanary, canary_id)
            try:
                results = canary_service.act_cleanup(
                    canary_id, _action_progress(run, RuntimeCanary, canary_id))
            except Exception as exc:
                _release_action(run, RuntimeCanary, canary_id,
                                f"cleanup: {type(exc).__name__}: {exc}"[:500])
                raise
            blockers = canary_service.cleanup_blockers(results)
            _release_action(run, RuntimeCanary, canary_id,
                            f"cleanup: {len(blockers)} resource(s) not removed" if blockers
                            else None)
            if blockers:
                raise Blocked(
                    "assistant.purge_canary_incomplete",
                    f"canary {row.name} ({canary_id}) cleanup left "
                    + ", ".join(f"{b['category']} ({b.get('detail') or 'skipped'})"
                                for b in blockers)
                    + " — review the canary, then retry the clear",
                    {"canary_id": canary_id, "remaining": blockers})
        run.db.expire_all()
        fresh = run.db.get(RuntimeCanary, canary_id)
        _wait_gone(run, key, f"canary {fresh.name}",
                   lambda row=fresh: canary_service.remaining_resources(row, run.workspace),
                   CANARY_GONE_TIMEOUT_S)
        run.mark(key, "done", "A/B test, online evaluations, gateway and endpoints read back gone")
        run.record("canaries", {"id": canary_id, "name": fresh.name})


def _step_experiments(run: _Run) -> None:
    from app.optimization import service as experiment_service

    for exp_id in run.owned.get("experiments") or []:
        key = f"experiment:{exp_id}"
        run.fence()
        if run.done(key):
            continue
        row = run.db.get(Experiment, exp_id)
        if row is None or not _experiment_pending(row):
            run.mark(key, "done", "already cleaned" if row else "experiment row gone")
            continue
        if row.workspace_id != run.workspace_id:
            raise Blocked("assistant.purge_unowned",
                          f"experiment {exp_id} belongs to another workspace — not cleaned",
                          {"experiment_id": exp_id})
        run.mark(key, "cleaning", f"removing experiment {row.name}")
        _claim_action(run, Experiment, exp_id)
        try:
            results = experiment_service.act_cleanup(
                exp_id, _action_progress(run, Experiment, exp_id))
        except Exception as exc:
            _release_action(run, Experiment, exp_id, f"cleanup: {type(exc).__name__}: {exc}"[:500])
            raise
        skipped = [r for r in results if r.get("status") == "skipped"]
        _release_action(run, Experiment, exp_id,
                        f"cleanup: {len(skipped)} resource(s) not removed" if skipped else None)
        if skipped:
            raise Blocked(
                "assistant.purge_experiment_incomplete",
                f"experiment {row.name} ({exp_id}) cleanup left "
                + ", ".join(f"{r['category']} ({r.get('detail') or 'skipped'})" for r in skipped)
                + " — review the experiment, then retry the clear",
                {"experiment_id": exp_id, "remaining": skipped})
        run.mark(key, "done", "cleaned")
        run.record("experiments", {"id": exp_id, "name": row.name})


def _step_operations(run: _Run) -> None:
    for op in _operations_of(run.db, run.conversation_id):
        run.fence()
        op = run.db.get(EvaluationAssetOperation, op.id)
        if op.status not in assets.CLEANABLE_STATUSES:
            continue
        op = assets.cleanup_operation(run.db, op, run.workspace, clients=run.clients)
        if op.status != "cleaned":
            remaining = [r for r in (op.resources or [])
                         if r.get("status") not in ("deleted", "skipped", "pending", "blocked")]
            raise Blocked(
                "assistant.conversation_assets_remain",
                f"evaluation-assets operation {op.id} could not be fully cleaned "
                f"(status {op.status}); review it in the Evaluation Assets panel — the "
                "conversation was kept so the remaining resources stay attributable",
                {"operation_id": op.id, "status": op.status,
                 "remaining": [{"kind": r.get("kind"), "name": r.get("name"),
                                "status": r.get("status"), "error": r.get("error")}
                               for r in remaining]})
        run.record("operations_cleaned", op.id)


def _step_datasets(run: _Run) -> None:
    # local rows only — the assistant never syncs them to AWS; a copy a member synced
    # by hand is that member's asset and is listed as such in the footprint
    for dataset_id in run.owned.get("datasets") or []:
        run.fence()
        ds = run.db.get(EvalDataset, dataset_id)
        if ds is None:
            continue
        if ds.workspace_id != run.workspace_id:
            raise Blocked("assistant.purge_unowned",
                          f"dataset {dataset_id} belongs to another workspace — not deleted",
                          {"dataset_id": dataset_id})
        run.record("datasets", {"id": ds.id, "name": ds.name})
        run.db.delete(ds)
        run.db.commit()


def agent_remaining(agent: Agent, workspace: WorkspaceContext) -> list[dict[str, str]]:
    """AWS readback of the Agent's own resource: empty once Get… is not found."""
    from app.services.agentcore import harness as hc
    from app.services.agentcore import runtime as rt
    from app.services.agentcore.client import control_client

    if not agent.resource_id:
        return []
    control = control_client(workspace)
    try:
        if agent.method == "harness":
            status = hc.get_harness(control, agent.resource_id).get("status")
            return [{"category": f"harness:{agent.resource_id}", "status": str(status or "")}]
        status = rt.get_runtime(control, agent.resource_id).get("status")
        return [{"category": f"runtime:{agent.resource_id}", "status": str(status or "")}]
    except Exception as exc:
        if _is_not_found(exc):
            return []
        raise


def foreign_endpoints(agent: Agent, workspace: WorkspaceContext) -> list[dict[str, str]]:
    """Named endpoints on the Agent's Harness besides DEFAULT — by now every endpoint
    one of this conversation's canaries created is read back gone, so anything left
    belongs to something the conversation does not own and would 409 DeleteHarness."""
    from app.services.agentcore import harness as hc
    from app.services.agentcore.client import control_client

    if agent.method != "harness" or not agent.resource_id:
        return []
    try:
        endpoints = hc.list_harness_endpoints(control_client(workspace), agent.resource_id)
    except Exception as exc:
        if _is_not_found(exc):
            return []
        raise
    return [{"category": f"endpoint:{e.get('endpointName')}", "status": str(e.get("status") or "")}
            for e in endpoints if e.get("endpointName") not in (None, "DEFAULT")]


def _step_agents(run: _Run) -> None:
    from app.routers import agents as agents_router  # local import: router ↔ service cycle
    from app.services.runtime_discovery import DISCOVERED_METHOD
    from app.system_agents.service import refuse_system_mutation

    for pinned in run.owned.get("agents") or []:
        agent_id = pinned["id"]
        key = f"agent:{agent_id}"
        run.fence()
        if run.done(key):
            continue
        agent = run.db.get(Agent, agent_id)
        if agent is None:
            run.mark(key, "done", "agent row gone")
            continue
        if agent.workspace_id != run.workspace_id:
            raise Blocked("assistant.purge_unowned",
                          f"agent {agent.name} belongs to another workspace — not deleted",
                          {"agent_id": agent_id})
        refuse_system_mutation(agent, "delete")  # a preset is never this purge's
        busy = _live_deploy_jobs(run.db, [agent_id])
        if busy or agent.status == "deploying":
            raise Blocked("assistant.purge_agent_busy",
                          f"agent {agent.name} has a deployment in flight — wait for it, "
                          "then retry the clear", {"agent_id": agent_id,
                                                   "jobs": [j.id for j in busy]})
        if agent.method == DISCOVERED_METHOD:
            if agent.status != "deleted":
                agents_router.mark_agent_deleted(run.db, agent)
            run.mark(key, "done", "discovered runtime — externally owned, ledger only")
            run.record("agents", {"id": agent.id, "name": agent.name,
                                  "aws_resource_deleted": False})
            continue
        progress = (run.db.get(Job, run.job_id).payload or {}).get("progress") or {}
        requested = bool((progress.get(key) or {}).get("delete_requested"))
        if requested and any("DELET" not in str(r.get("status", "")).upper()
                             for r in agent_remaining(agent, run.workspace)):
            requested = False  # present and not deleting: the earlier request did not take
        if not requested:
            foreign = foreign_endpoints(agent, run.workspace)
            if foreign:
                raise Blocked(
                    "assistant.purge_agent_dependency",
                    f"Harness {agent.resource_id} of agent {agent.name} still has "
                    + ", ".join(f"{f['category']} ({f['status'] or '?'})" for f in foreign)
                    + " that this conversation's canaries do not own — remove them, then "
                    "retry the clear",
                    {"agent_id": agent_id, "remaining": foreign})
            run.mark(key, "deleting", f"deleting {agent.method} {agent.resource_id or ''}")
            try:
                agents_router.delete_agent_cloud_resource(agent, run.workspace)
            except Exception as exc:
                if type(exc).__name__ == "ConflictException":
                    raise Blocked(
                        "assistant.purge_agent_conflict",
                        f"AWS refused to delete agent {agent.name} ({exc}) — a dependency "
                        "still references it; retry the clear once it is gone",
                        {"agent_id": agent_id,
                         "remaining": foreign_endpoints(agent, run.workspace)}) from exc
                raise
            run.mark(key, "deleting", "delete accepted", delete_requested=True)
        _wait_gone(run, key, f"agent {agent.name}",
                   lambda row=agent: agent_remaining(row, run.workspace), AGENT_GONE_TIMEOUT_S)
        # after the resource is read back gone, never before: a role still referenced
        # can wedge the runtime's own deletion
        if not agents_router.delete_agent_role(agent, run.workspace):
            raise Blocked("assistant.purge_role_remains",
                          f"the execution role of agent {agent.name} could not be deleted — "
                          "retry the clear", {"agent_id": agent_id})
        record = _clean_registry_record(run, key, agent)
        run.db.expire_all()
        agent = run.db.get(Agent, agent_id)
        if agent.status != "deleted":
            agents_router.mark_agent_deleted(run.db, agent)
        run.mark(key, "done", "resource read back gone, role deleted, "
                 f"registry record {record}, ledger marked deleted")
        run.record("agents", {"id": agent.id, "name": agent.name, "aws_resource_deleted": True,
                              "registry_record": record})


def _clean_registry_record(run: _Run, key: str, agent: Agent) -> str:
    """The agent's own A2A Registry record (created by the deploy ``register`` stage),
    deleted and read back gone. A record id that now names something else is not this
    agent's and is kept (``foreign``); a Registry error is a retryable failure."""
    from app.services import registry_console

    state = registry_console.delete_agent_record(agent, run.workspace)
    if state in ("none", "foreign"):
        return {"none": "none", "foreign": "kept (not this agent's)"}[state]

    def probe(row: Agent = agent) -> list[dict[str, str]]:
        found = registry_console.agent_record_state(row, run.workspace)
        return [] if found in ("absent", "none", "foreign") else [
            {"category": f"registry-record:{row.registry_record_id}", "status": found}]

    _wait_gone(run, key, f"registry record of agent {agent.name}", probe, AGENT_GONE_TIMEOUT_S)
    return "deleted"


def _step_canary_artifacts(run: _Run) -> None:
    """The candidate zips a canary kept because they backed the live version: with the
    Agent read back gone nothing runs them any more."""
    from app.optimization import canary_infra, canary_service

    for canary_id in run.owned.get("canaries") or []:
        key = f"canary-artifacts:{canary_id}"
        run.fence()
        if run.done(key):
            continue
        row = run.db.get(RuntimeCanary, canary_id)
        keys = canary_service.retained_artifact_keys(row) if row is not None else []
        for s3_key in keys:
            canary_infra.delete_object_quiet(s3_key, run.workspace)
        run.mark(key, "done", f"{len(keys)} candidate artifact(s) removed")


_STEPS: tuple[tuple[str, Callable[[_Run], None]], ...] = (
    ("canaries", _step_canaries),
    ("experiments", _step_experiments),
    ("operations", _step_operations),
    ("datasets", _step_datasets),
    ("agents", _step_agents),
    ("canary_artifacts", _step_canary_artifacts),
)


def execute_purge_job(
    job_id: str, *, resume: bool = False, workspace: WorkspaceContext | None = None,
    clients: assets.ClientFactory = assets._default_clients,
) -> None:
    """Run (or, with ``resume=True``, adopt a crashed) purge job. Never raises."""
    db = SessionLocal()
    lock = None
    try:
        job = db.get(Job, job_id)
        if job is None or job.type != JOB_TYPE or job.status not in LIVE_JOB_STATUSES:
            return
        lock = _acquire_lock((job.payload or {}).get("conversation_id") or job_id)
        if lock is None:
            logger.info("purge job %s: another worker holds the conversation lock", job_id)
            return
        allowed = LIVE_JOB_STATUSES if resume else ("queued",)
        won = db.execute(
            update(Job).where(Job.id == job_id, Job.status.in_(allowed))
            .values(status="running", error=None)
            .execution_options(synchronize_session=False)
        ).rowcount
        db.commit()
        if won != 1:
            return
        run = _Run(db, job_id, workspace or context_for_workspace(job.workspace_id), clients)
        for step, fn in _STEPS:
            run.fence()
            try:
                fn(run)
            except (Blocked, Superseded):
                raise
            except AppError as exc:  # a refusal from a guarded path (preset, permission…)
                raise Blocked(exc.code, exc.message, getattr(exc, "detail", None) or {},
                              exc.status_code) from exc
            except Exception as exc:  # noqa: BLE001 — recorded, retryable
                _append_log(db, job_id, step, traceback.format_exc(limit=3), level="debug")
                raise Blocked("assistant.purge_step_failed",
                              f"{step}: {type(exc).__name__}: {exc} — retry the clear",
                              {"step": step}) from exc
        # fenced finalization: only now do the ledger rows (the ownership evidence) go
        job = run.fence()
        _delete_rows(db, run.conversation_id)
        job.status = "succeeded"
        job.payload = {**(job.payload or {}), "deleted": True,
                       "step": {"key": "ledger", "state": "done", "detail": ""}}
        db.commit()
        _append_log(db, job_id, "ledger", "done · conversation rows removed")
        logger.info("assistant conversation %s purged: %s", run.conversation_id,
                    (job.payload or {}).get("result"))
    except Superseded as exc:
        db.rollback()
        logger.info("purge job %s superseded: %s", job_id, exc)
        # never left `running`: a startup resume would re-adopt it forever
        _fail(db, job_id, Blocked("assistant.purge_superseded", f"superseded: {exc}"))
    except Blocked as exc:
        db.rollback()
        _fail(db, job_id, exc)
    except Exception as exc:  # noqa: BLE001 — job-level failure must never crash the worker
        db.rollback()
        logger.exception("purge job %s failed outside a step", job_id)
        _fail(db, job_id, Blocked("assistant.purge_step_failed",
                                  f"{type(exc).__name__}: {exc} — retry the clear"))
    finally:
        _release_lock(lock)
        db.close()


def _fail(db: Session, job_id: str, blocked: Blocked) -> None:
    db.expire_all()
    job = db.get(Job, job_id)
    if job is None or job.status != "running":
        return
    logger.warning("purge job %s stopped: %s", job_id, blocked.message)
    job.status = "failed"
    job.error = blocked.message[:500]
    job.payload = {**(job.payload or {}), "blocker": blocked.as_dict()}
    db.commit()
    _append_log(db, job_id, "teardown", blocked.message, level="error")


def job_result(job: Job) -> dict[str, Any]:
    payload = job.payload or {}
    return {"deleted": job.status == "succeeded", "conversation_id": payload.get("conversation_id"),
            "job_id": job.id, "status": job.status, "error": job.error,
            "blocker": payload.get("blocker"),
            **{**_empty_result(), **(payload.get("result") or {})}}


def purge(
    db: Session, row: AssistantConversation, workspace: WorkspaceContext, *,
    can: Callable[[str], bool],
    clients: assets.ClientFactory = assets._default_clients,
) -> dict[str, Any]:
    """Synchronous purge (tests, scripts): request + run the job inline. Raises the
    job's blocker as ``AppError`` (409 when blocked or resources remain; 403 when a
    required permission is missing)."""
    job, done = request_purge(db, row, can=can)
    if done is not None:
        return done
    execute_purge_job(job.id, workspace=workspace, clients=clients)
    db.expire_all()
    job = db.get(Job, job.id)
    if job.status == "succeeded":
        return job_result(job)
    blocker = (job.payload or {}).get("blocker") or {
        "code": "assistant.purge_step_failed", "message": job.error or "purge failed",
        "detail": {}, "status_code": 409}
    raise AppError(blocker["code"], blocker["message"],
                   {**(blocker.get("detail") or {}), "job_id": job.id},
                   status_code=blocker.get("status_code") or 409)


# ---------------------------------------------------------------------------
# starters: at most one live worker thread per job in this process
# ---------------------------------------------------------------------------

_WORKERS: dict[str, threading.Thread] = {}
_WORKERS_LOCK = threading.Lock()


def _launch(job_id: str, *, resume: bool) -> threading.Thread:
    with _WORKERS_LOCK:
        existing = _WORKERS.get(job_id)
        if existing is not None and existing.is_alive():
            return existing

        def run() -> None:
            try:
                execute_purge_job(job_id, resume=resume)
            finally:
                with _WORKERS_LOCK:
                    if _WORKERS.get(job_id) is threading.current_thread():
                        del _WORKERS[job_id]

        thread = threading.Thread(target=run, daemon=True, name=f"purge-{job_id[:8]}")
        _WORKERS[job_id] = thread
        try:
            thread.start()
        except Exception:
            _WORKERS.pop(job_id, None)
            raise
        return thread


def start_purge_async(job_id: str) -> threading.Thread:
    return _launch(job_id, resume=False)


def start_purge_resume(job_id: str) -> threading.Thread:
    """Startup resume: may adopt a job a crashed process left ``running``."""
    return _launch(job_id, resume=True)
