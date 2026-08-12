"""Unified deploy pipeline.

Every creation method converges into the same ordered stages:

    generate → package → provision → deploy → register

A method module contributes one callable per stage (or omits it to skip).
Stage progress is persisted on the Deployment row and mirrored as JSONL
into the Job log, so a restarted backend can resume from the first
non-succeeded stage.
"""

import json
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models.ledger import Agent, Deployment, Job
from app.services.workspace import WorkspaceContext, context_for_workspace

STAGE_ORDER = ["generate", "package", "provision", "deploy", "register"]


@dataclass
class StageResult:
    detail: str = ""
    skipped: bool = False


@dataclass
class StageContext:
    """Mutable bag handed to every stage of one deployment run.

    ``workspace`` is the environment this deploy targets, rehydrated from the Job
    row rather than read from ambient settings — a job resumed after a restart
    must land in the same account/region it started in. Note the two unrelated
    senses of "session" here: ``session()`` opens a ledger session, while AWS
    clients come from ``workspace.client(...)``.
    """

    agent_id: str
    deployment_id: str
    job_id: str
    workspace: WorkspaceContext
    scratch: dict[str, Any] = field(default_factory=dict)
    log: Callable[[str], None] = lambda msg: None

    def session(self) -> Session:
        return SessionLocal()


StageFn = Callable[[StageContext, Agent], StageResult]
MethodStages = dict[str, StageFn]

_METHODS: dict[str, MethodStages] = {}


def register_method(name: str, stages: MethodStages) -> None:
    _METHODS[name] = stages


def get_method(name: str) -> MethodStages:
    if name not in _METHODS:
        raise ValueError(f"no deploy method registered for '{name}'")
    return _METHODS[name]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _append_log(db: Session, job_id: str, stage: str, message: str, level: str = "info") -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    line = json.dumps(
        {"ts": _now_iso(), "stage": stage, "level": level, "msg": message}, ensure_ascii=False
    )
    job.log = (job.log + "\n" + line) if job.log else line
    db.commit()


def _set_stage(
    db: Session, deployment_id: str, stage: str, status: str, detail: str = ""
) -> None:
    dep = db.get(Deployment, deployment_id)
    if dep is None:
        return
    stages = [dict(s) for s in dep.stages]
    for s in stages:
        if s["name"] == stage:
            s["status"] = status
            if detail:
                s["detail"] = detail
            if status == "running":
                s["started_at"] = _now_iso()
            if status in ("succeeded", "skipped", "failed"):
                s["ended_at"] = _now_iso()
    dep.stages = stages
    db.commit()


def create_deployment(
    db: Session,
    agent: Agent,
    mode: str = "create",
    *,
    skip_register: bool = False,
) -> tuple[Deployment, Job]:
    """Create the Deployment (stages pending) + Job rows for one deploy run.

    ``mode`` is "create" for a first deploy or "update" for an in-place
    re-publish; the deploy stage reads it to choose Create* vs Update* APIs.
    Promotion updates may skip registry publication because identity is
    unchanged and registry failure must not obscure a successful rollout."""
    # The workspace comes off the agent, not the request: a promotion or resumed
    # job must land in the same environment as the agent it deploys.
    deployment = Deployment(
        workspace_id=agent.workspace_id,
        agent_id=agent.id,
        stages=[{"name": s, "status": "pending", "detail": ""} for s in STAGE_ORDER],
    )
    db.add(deployment)
    db.flush()
    job = Job(
        workspace_id=agent.workspace_id,
        type="deploy_agent",
        payload={
            "agent_id": agent.id,
            "deployment_id": deployment.id,
            "mode": mode,
            "skip_register": skip_register,
        },
    )
    db.add(job)
    db.flush()
    deployment.job_id = job.id
    db.commit()
    return deployment, job


def execute_deploy_job(job_id: str) -> None:
    """Run (or resume) one deploy job to completion. Never raises."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return
        agent_id = job.payload["agent_id"]
        deployment_id = job.payload["deployment_id"]
        job.status = "running"
        db.commit()

        agent = db.get(Agent, agent_id)
        deployment = db.get(Deployment, deployment_id)
        if agent is None or deployment is None:
            raise RuntimeError("ledger rows missing for job")

        stages = get_method(agent.method)
        ctx = StageContext(
            agent_id=agent_id,
            deployment_id=deployment_id,
            job_id=job_id,
            workspace=context_for_workspace(job.workspace_id),
        )
        ctx.scratch["mode"] = job.payload.get("mode", "create")

        done = {s["name"] for s in deployment.stages if s["status"] in ("succeeded", "skipped")}
        for stage_name in STAGE_ORDER:
            if stage_name in done:
                continue

            def log(msg: str, _stage=stage_name) -> None:
                _append_log(db, job_id, _stage, msg)

            ctx.log = log
            _set_stage(db, deployment_id, stage_name, "running")
            _append_log(db, job_id, stage_name, "stage started")
            fn = stages.get(stage_name)
            try:
                db.refresh(agent)
                if stage_name == "register" and job.payload.get("skip_register"):
                    result = StageResult(
                        skipped=True, detail="promotion update keeps existing registry identity"
                    )
                else:
                    result = (
                        fn(ctx, agent)
                        if fn
                        else StageResult(skipped=True, detail="not used")
                    )
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                _set_stage(db, deployment_id, stage_name, "failed", detail)
                _append_log(db, job_id, stage_name, detail, level="error")
                _append_log(db, job_id, stage_name, traceback.format_exc(limit=3), level="debug")
                _finish(db, job_id, deployment_id, agent_id, error=detail)
                return
            status = "skipped" if result.skipped else "succeeded"
            _set_stage(db, deployment_id, stage_name, status, result.detail)
            _append_log(db, job_id, stage_name, result.detail or status)

        _finish(db, job_id, deployment_id, agent_id, error=None)
    except Exception as exc:  # job-level failure — never crash the worker
        db.rollback()
        job = db.get(Job, job_id)
        if job is not None:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            db.commit()
    finally:
        db.close()


def _finish(
    db: Session, job_id: str, deployment_id: str, agent_id: str, error: str | None
) -> None:
    job = db.get(Job, job_id)
    deployment = db.get(Deployment, deployment_id)
    agent = db.get(Agent, agent_id)
    now = datetime.now(UTC)
    if error is None:
        job.status = "succeeded"
        deployment.status = "succeeded"
        agent.status = "active"
        agent.error = None
    else:
        job.status = "failed"
        job.error = error
        deployment.status = "failed"
        agent.status = "failed"
        agent.error = error
    deployment.ended_at = now
    db.commit()


def start_deploy_async(job_id: str) -> threading.Thread:
    thread = threading.Thread(target=execute_deploy_job, args=(job_id,), daemon=True)
    thread.start()
    return thread


def resume_pending_jobs() -> list[str]:
    """Called on startup: re-run deploy jobs interrupted by a restart."""
    db = SessionLocal()
    try:
        pending = (
            db.query(Job)
            .filter(Job.type == "deploy_agent", Job.status.in_(["queued", "running"]))
            .all()
        )
        ids = [j.id for j in pending]
    finally:
        db.close()
    for job_id in ids:
        start_deploy_async(job_id)
    return ids
