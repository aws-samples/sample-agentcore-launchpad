"""Install / repair / uninstall of the coding runtime as a durable ``Job``.

Requested by an administrator through ``/api/coding/runtime`` (never on startup,
never on a read). One live job per workspace — install and uninstall are mutually
exclusive, and a second request of the same kind returns the live job instead of
queueing another (idempotent). The worker runs on a daemon thread, claims its job
with a ``queued → running`` compare-and-set, and writes JSONL progress onto
``Job.log`` (stages ``role | image | runtime | wait | done``).

Not resumable by design: a backend restart fails an interrupted job at startup
(``sweep_interrupted_jobs``) and the administrator re-runs it. Every step below is
idempotent (role reconcile, content-addressed image, drift-checked runtime), so a
re-run picks up wherever the dead one stopped — including a CodeBuild or a
runtime create that kept going on AWS.
"""

from __future__ import annotations

import logging
import threading
import traceback
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.deployer.pipeline import _append_log
from app.models.ledger import Job
from app.services.workspace import context_for_workspace, merge_workspace_resources

from . import infra

logger = logging.getLogger("launchpad.coding")

INSTALL_JOB_TYPE = "coding_runtime_install"
UNINSTALL_JOB_TYPE = "coding_runtime_uninstall"
JOB_TYPES = (INSTALL_JOB_TYPE, UNINSTALL_JOB_TYPE)
LIVE_STATUSES = ("queued", "running")
INTERRUPTED = "interrupted by a backend restart — run it again (every step is idempotent)"
_ERROR_MAX = 2000

_CLAIM_LOCK = threading.Lock()
_on_finish: list[Any] = []  # callbacks(workspace_id) — the status cache registers here


def latest_job(db: Session, workspace_id: str) -> Job | None:
    return (
        db.query(Job)
        .filter(Job.workspace_id == workspace_id, Job.type.in_(JOB_TYPES))
        .order_by(Job.created_at.desc())
        .first()
    )


def live_job(db: Session, workspace_id: str) -> Job | None:
    return (
        db.query(Job)
        .filter(
            Job.workspace_id == workspace_id,
            Job.type.in_(JOB_TYPES),
            Job.status.in_(LIVE_STATUSES),
        )
        .order_by(Job.created_at.desc())
        .first()
    )


def running_projects(db: Session, workspace_id: str) -> int:
    """Projects with a turn in flight (or a start being claimed) — the uninstall
    refusal: removing the runtime would strand those turns."""
    from app.coding.models import PROJECT_RUNNING, CodingProject

    return (
        db.query(CodingProject)
        .filter(CodingProject.workspace_id == workspace_id,
                CodingProject.status == PROJECT_RUNNING)
        .count()
    )


def request_job(
    db: Session,
    workspace_id: str,
    job_type: str,
    *,
    requested_by: str | None,
    requested_by_name: str,
) -> tuple[Job, bool]:
    """Queue an install or uninstall; ``(job, started)``. A live job of the same
    type is returned as is (``started=False``); one of the other type is a 409."""
    if job_type not in JOB_TYPES:
        raise ValueError(job_type)
    with _CLAIM_LOCK:
        db.expire_all()
        live = live_job(db, workspace_id)
        if live is not None:
            if live.type == job_type:
                return live, False
            raise AppError(
                "coding.runtime_busy",
                "an install or uninstall of the coding runtime is already running here",
                {"job_id": live.id, "type": live.type},
                status_code=409,
            )
        job = Job(
            workspace_id=workspace_id,
            type=job_type,
            status="queued",
            payload={"requested_by": requested_by, "requested_by_name": requested_by_name},
        )
        db.add(job)
        db.commit()
    verb = "install" if job_type == INSTALL_JOB_TYPE else "uninstall"
    _append_log(db, job.id, "queued", f"{verb} requested by {requested_by_name or 'unknown'}")
    db.refresh(job)
    return job, True


def _finish(db: Session, job_id: str, status: str, error: str | None = None) -> None:
    db.expire_all()
    job = db.get(Job, job_id)
    if job is None:
        return
    job.status = status
    job.error = (error or "")[:_ERROR_MAX] or None
    db.commit()


def execute_job(job_id: str) -> None:
    """The worker body (also called directly by tests). Never raises."""
    db = SessionLocal()
    workspace_id: str | None = None
    try:
        claimed = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.type.in_(JOB_TYPES), Job.status == "queued")
            .values(status="running")
            .execution_options(synchronize_session=False)
        ).rowcount
        db.commit()
        if claimed != 1:
            logger.info("coding runtime job %s: not queued any more, skipped", job_id)
            return
        job = db.get(Job, job_id)
        workspace_id = job.workspace_id
        job_type = job.type

        def log(stage: str, message: str) -> None:
            logger.info("coding runtime %s [%s] %s", job_id[:8], stage, message)
            _append_log(db, job_id, stage, message)

        try:
            ctx = context_for_workspace(workspace_id)
            if job_type == INSTALL_JOB_TYPE:
                keys: dict[str, Any] = infra.ensure_coding_runtime(ctx, workspace_id, log)
                merge_workspace_resources(ctx, keys)
                log("done", f"coding runtime ready · {keys.get(infra.KEY_IMAGE_TAG)}")
            else:
                keys = infra.uninstall_coding_runtime(ctx, workspace_id, log)
                merge_workspace_resources(ctx, keys)
                log("done", "coding runtime uninstalled (project snapshots kept)")
            _finish(db, job_id, "succeeded")
        except Exception as exc:  # noqa: BLE001 — a job records its failure, never raises
            logger.warning("coding runtime job %s failed: %s\n%s", job_id, exc,
                           traceback.format_exc())
            try:
                _append_log(db, job_id, "failed", str(exc)[:_ERROR_MAX], level="error")
            except Exception:  # noqa: BLE001
                db.rollback()
            _finish(db, job_id, "failed", str(exc))
    except Exception:  # noqa: BLE001 — the claim/ledger itself failed
        logger.exception("coding runtime job %s crashed", job_id)
    finally:
        db.close()
        if workspace_id:
            for callback in list(_on_finish):
                try:
                    callback(workspace_id)
                except Exception:  # noqa: BLE001
                    logger.exception("coding runtime job finish callback failed")


def start_async(job_id: str) -> threading.Thread:
    thread = threading.Thread(
        target=execute_job, args=(job_id,), daemon=True, name=f"coding-runtime-{job_id[:8]}"
    )
    thread.start()
    return thread


def sweep_interrupted_jobs() -> list[str]:
    """Startup: an install/uninstall a dead process left ``queued``/``running`` is
    failed honestly (its thread is gone). The admin re-runs it."""
    db = SessionLocal()
    try:
        rows = (
            db.query(Job)
            .filter(Job.type.in_(JOB_TYPES), Job.status.in_(LIVE_STATUSES))
            .all()
        )
        ids = [row.id for row in rows]
        for row in rows:
            row.status = "failed"
            row.error = INTERRUPTED
        db.commit()
        for job_id in ids:
            _append_log(db, job_id, "failed", INTERRUPTED, level="error")
        if ids:
            logger.info("failed %d interrupted coding runtime job(s)", len(ids))
        return ids
    finally:
        db.close()
