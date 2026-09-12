"""The uninstall worker for system-managed presets.

Queued by ``service.uninstall_preset`` (claim + job in one commit), run on a daemon
thread by the router, and resumed by ``pipeline.resume_pending_jobs`` after a
restart — the same conventions as deploy jobs. Teardown uses the exact helper the
ordinary agent delete uses (``routers.agents._delete_agent_resources``), which is
idempotent per resource (a harness already gone is not an error, the role delete
never raises) and addresses only the resources named on this row, so a retry can
never touch another install's resources.

Outcome:

* success → the row is marked ``deleted`` (identity released), the job succeeds;
* failure → the row STAYS ``uninstalling`` with the reason on both the job and the
  row; a later explicit uninstall creates a new attempt; install/repair keep
  refusing meanwhile;
* crash → the job is still ``queued``/``running`` in the ledger and is re-run on
  startup; the worker only acts on rows that are still ``uninstalling``.
"""

import logging
import threading
import traceback
from datetime import UTC, datetime

from app.core.db import SessionLocal
from app.deployer.pipeline import _append_log
from app.models.ledger import Agent, Job
from app.services.workspace import context_for_workspace

logger = logging.getLogger("launchpad.system_agents")

JOB_TYPE = "uninstall_system_agent"


def execute_uninstall_job(job_id: str) -> None:
    """Run (or resume) one uninstall job to completion. Never raises."""
    from app.routers.agents import _delete_agent_resources

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = "running"
        job.error = None
        db.commit()
        agent_id = (job.payload or {}).get("agent_id")
        agent = db.get(Agent, agent_id) if agent_id else None
        if agent is None:
            _fail(db, job, None, "ledger row missing for the uninstall job")
            return
        if agent.status == "deleted":
            _append_log(db, job_id, "teardown", "row already deleted — nothing to do")
            job.status = "succeeded"
            db.commit()
            return
        if agent.status != "uninstalling":
            _fail(db, job, None, f"row is '{agent.status}', not 'uninstalling'; refusing")
            return
        _append_log(db, job_id, "teardown", f"removing AWS resources of {agent.name} ({agent.id})")
        try:
            workspace = context_for_workspace(job.workspace_id)
            aws_deleted = _delete_agent_resources(agent, workspace)
        except Exception as exc:  # noqa: BLE001 — recorded on the row, retryable
            detail = f"{type(exc).__name__}: {exc}"
            _append_log(db, job_id, "teardown", traceback.format_exc(limit=3), level="debug")
            _fail(db, job, agent, detail)
            return
        agent.status = "deleted"
        agent.error = None
        agent.updated_at = datetime.now(UTC)
        job.status = "succeeded"
        job.payload = {**(job.payload or {}), "aws_resource_deleted": aws_deleted}
        db.commit()
        _append_log(db, job_id, "teardown", f"done · aws_resource_deleted={aws_deleted}")
    except Exception as exc:  # noqa: BLE001 — job-level failure must never crash the worker
        db.rollback()
        logger.exception("uninstall job %s failed outside teardown: %s", job_id, exc)
        job = db.get(Job, job_id)
        if job is not None:
            _fail(db, job, None, f"{type(exc).__name__}: {exc}")
    finally:
        db.close()


def _fail(db, job: Job, agent: Agent | None, detail: str) -> None:
    logger.warning("uninstall job %s failed: %s", job.id, detail)
    job.status = "failed"
    job.error = detail[:500]
    if agent is not None:
        agent.error = f"uninstall failed: {detail}"[:500]
        agent.updated_at = datetime.now(UTC)
    db.commit()
    _append_log(db, job.id, "teardown", detail, level="error")


def start_uninstall_async(job_id: str) -> threading.Thread:
    thread = threading.Thread(target=execute_uninstall_job, args=(job_id,), daemon=True)
    thread.start()
    return thread
