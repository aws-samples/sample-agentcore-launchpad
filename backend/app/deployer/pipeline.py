"""Unified deploy pipeline.

Every creation method converges into the same ordered stages:

    generate → package → provision → deploy → register

A method module contributes one callable per stage (or omits it to skip).
Stage progress is persisted on the Deployment row and mirrored as JSONL
into the Job log, so a restarted backend can resume from the first
non-succeeded stage.
"""

import json
import logging
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models.ledger import Agent, Deployment, Job
from app.services import workspace_bootstrap
from app.services.workspace import WorkspaceContext, context_for_workspace

STAGE_ORDER = ["generate", "package", "provision", "deploy", "register"]

# Child of the "launchpad" logger app.main uses, so deploy failures reach the
# process log (journalctl on prod) and not only the per-job JSONL ledger.
logger = logging.getLogger("launchpad.deploy")


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
    payload_extra: dict[str, Any] | None = None,
    commit: bool = True,
) -> tuple[Deployment, Job]:
    """Create the Deployment (stages pending) + Job rows for one deploy run.

    ``mode`` is "create" for a first deploy or "update" for an in-place
    re-publish; the deploy stage reads it to choose Create* vs Update* APIs.
    Promotion updates may skip registry publication because identity is
    unchanged and registry failure must not obscure a successful rollout.
    ``payload_extra`` lands on the Job in the SAME commit as the rows (a caller
    that must pin data to the job — the system-preset release — cannot be left
    with a runnable job and no pin by a crash between two commits). ``commit=False``
    leaves the rows flushed in the caller's transaction so the caller can link the
    freshly minted ids onto its own rows and commit everything at once."""
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
            **(payload_extra or {}),
        },
    )
    db.add(job)
    db.flush()
    deployment.job_id = job.id
    if commit:
        db.commit()
    else:
        db.flush()
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
        if agent.system_key:
            # Every system-preset job — fresh or resumed, whatever stages already
            # succeeded or were skipped — proves its release pin and the complete
            # expected skill URI for THIS workspace before a single stage runs.
            from app.system_agents.service import assert_job_release_pinned

            assert_job_release_pinned(job.payload, agent, ctx.workspace)
        if (job.payload or {}).get("assistant"):
            # An assistant-approved job deploys exactly the reviewed bindings or
            # fails closed before any stage: the pinned gateway auth identity, skill
            # content digests, KB-gateway prerequisites and memory binding are
            # re-resolved and compared here (fresh or resumed job alike).
            from app.assistant.service import assert_job_bindings_pinned

            assert_job_bindings_pinned(job.payload, agent, ctx.workspace)
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
                logger.warning(
                    "deploy job %s: agent %s failed at stage %s: %s",
                    job_id, agent_id, stage_name, detail,
                )
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
        # The session may sit in a failed transaction; clear it before touching rows.
        db.rollback()
        _fail_job(db, job_id, exc)
    finally:
        db.close()


def _fail_job(db: Session, job_id: str, exc: BaseException) -> None:
    """Land a failure raised *outside* any stage on the same rows a stage failure does.

    Workspace gone (`LookupError`), unregistered method (`ValueError`) or missing
    ledger rows all reach here. The agent must still end up `failed` — otherwise
    the Create page polls forever, redeploy is refused with 409 and Overview keeps
    counting it as deploying. Rows are resolved from the job payload and each one
    is optional, so a half-deleted ledger still gets the job marked failed.
    """
    detail = f"{type(exc).__name__}: {exc}"
    job = db.get(Job, job_id)
    payload = (job.payload or {}) if job is not None else {}
    agent_id = payload.get("agent_id")
    deployment_id = payload.get("deployment_id")
    logger.exception(
        "deploy job %s: agent %s failed outside a stage: %s", job_id, agent_id, detail
    )
    if job is None:
        return
    now = datetime.now(UTC)
    job.status = "failed"
    job.error = detail
    deployment = db.get(Deployment, deployment_id) if deployment_id else None
    if deployment is not None:
        deployment.status = "failed"
        deployment.ended_at = now
    agent = db.get(Agent, agent_id) if agent_id else None
    if agent is not None:
        agent.status = "failed"
        agent.error = detail
    db.commit()
    _append_log(db, job_id, "job", detail, level="error")
    _append_log(db, job_id, "job", traceback.format_exc(limit=3), level="debug")


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


# One live worker per deploy job in this process. A repeated approval (or any
# retry) may re-wake a job that is still `queued` because its first starter
# failed; the registry makes that a no-op while a worker is alive, so the pipeline
# never runs twice for one job. Single-host bound, like the uninstall registry.
_LIVE_WORKERS: dict[str, threading.Thread] = {}
_LIVE_LOCK = threading.Lock()


def live_deploy_worker(job_id: str) -> threading.Thread | None:
    with _LIVE_LOCK:
        thread = _LIVE_WORKERS.get(job_id)
        return thread if thread is not None and thread.is_alive() else None


def start_deploy_async(job_id: str) -> threading.Thread:
    with _LIVE_LOCK:
        existing = _LIVE_WORKERS.get(job_id)
        if existing is not None and existing.is_alive():
            return existing

        def run() -> None:
            try:
                execute_deploy_job(job_id)
            finally:
                with _LIVE_LOCK:
                    if _LIVE_WORKERS.get(job_id) is threading.current_thread():
                        del _LIVE_WORKERS[job_id]

        thread = threading.Thread(target=run, daemon=True, name=f"deploy-{job_id[:8]}")
        _LIVE_WORKERS[job_id] = thread
        try:
            thread.start()
        except Exception:
            _LIVE_WORKERS.pop(job_id, None)
            raise
        return thread


def resume_pending_jobs() -> list[str]:
    """Called on startup: re-run the staged jobs a restart interrupted.

    Every resumable job type registers its starter here. Both kinds rehydrate
    their workspace from `jobs.workspace_id` inside the worker, so this only has
    to hand over the id.
    """
    # Lazy: the uninstall worker imports the agents router (teardown helper), which
    # imports the system-agents service, which imports this module.
    from app.system_agents import uninstall as system_uninstall

    starters: dict[str, Callable[[str], threading.Thread]] = {
        "deploy_agent": start_deploy_async,
        workspace_bootstrap.JOB_TYPE: workspace_bootstrap.start_bootstrap_async,
        # a crashed uninstall is still `running` in the ledger; the resume starter
        # is the only caller allowed to pick such a job up again
        system_uninstall.JOB_TYPE: system_uninstall.start_uninstall_resume,
    }
    db = SessionLocal()
    try:
        pending = (
            db.query(Job)
            .filter(
                Job.type.in_(list(starters)),
                Job.status.in_(["queued", "running"]),
            )
            .all()
        )
        found = [(j.id, j.type) for j in pending]
    finally:
        db.close()
    for job_id, job_type in found:
        starters[job_type](job_id)
    return [job_id for job_id, _ in found]
