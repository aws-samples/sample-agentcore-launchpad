"""The uninstall worker for system-managed presets.

Queued by ``service.uninstall_preset`` (claim + job in one commit), run on a daemon
thread by the router, and resumed by ``pipeline.resume_pending_jobs`` after a
restart — the same conventions as deploy jobs, plus three guarantees this preset
promises that the ordinary delete does not:

* **exclusive, fenced execution** — a job runs only if it is the row's *current*
  uninstall attempt, of the right type and workspace, and it wins the
  ``queued → running`` CAS (a resume may pick up a ``running`` job the crashed
  process left behind). The fence is re-checked before every cloud step and again
  inside the finalizing transaction, so a superseded or duplicate worker is inert
  and can never finish, fail or clean up for a later attempt — nor resolve a
  replacement install's resources by reused name;
* **strict teardown** — the harness must actually disappear (``GetHarness`` →
  ``ResourceNotFoundException``) before the role is removed; ``DELETE_FAILED``, a
  timeout, a KB-target failure or an IAM role that could not be deleted keep the
  row ``uninstalling`` for an explicit retry. Per-resource progress is recorded on
  the job so a retry is idempotent and visible;
* **deleted means gone** — only a fully verified teardown marks the row ``deleted``.

The ordinary agent delete (``routers.agents._delete_agent_resources``) keeps its
best-effort semantics; this module deliberately does not call it.
"""

import logging
import threading
import time
import traceback
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.deployer import harness as harness_method
from app.deployer.pipeline import _append_log
from app.models.ledger import Agent, Job
from app.services import agent_iam
from app.services import kb_gateway as kbgw
from app.services.agentcore import harness as hc
from app.services.workspace import WorkspaceContext, context_for_workspace

logger = logging.getLogger("launchpad.system_agents")

JOB_TYPE = "uninstall_system_agent"
# Harness deletion is asynchronous (DeleteHarness answers DELETING). Bounded wait:
# a timeout is a retryable failure, never a success.
HARNESS_GONE_TIMEOUT_S = 90
HARNESS_POLL_S = 3
_sleep = time.sleep  # injectable in tests

_STEPS = ("kb_target", "harness", "role")


class Superseded(Exception):
    """This worker no longer owns the row; every effect is skipped."""


def _owner_job_id(db, agent_id: str) -> str | None:
    from app.system_agents.service import _owner_job

    owner = _owner_job(db, agent_id)
    return owner.id if owner else None


def _fence(db, job_id: str) -> tuple[Job, Agent]:
    """Re-read job + row; raise Superseded unless this job still owns the teardown."""
    db.expire_all()
    job = db.get(Job, job_id)
    if job is None or job.type != JOB_TYPE or job.status != "running":
        raise Superseded(f"job {job_id} is not a running uninstall job")
    agent = db.get(Agent, (job.payload or {}).get("agent_id") or "")
    if agent is None or agent.status != "uninstalling":
        raise Superseded("the row is no longer uninstalling")
    if agent.workspace_id != job.workspace_id:
        raise Superseded("job and row belong to different workspaces")
    if (job.payload or {}).get("preset_key") != agent.system_key:
        raise Superseded("job and row name different presets")
    if _owner_job_id(db, agent.id) != job.id:
        raise Superseded("a newer uninstall attempt owns the row")
    return job, agent


def _progress(db, job: Job, step: str, state: str, detail: str = "") -> None:
    progress = dict((job.payload or {}).get("progress") or {})
    progress[step] = {"state": state, "detail": detail, "at": datetime.now(UTC).isoformat()}
    job.payload = {**(job.payload or {}), "progress": progress}
    db.commit()
    _append_log(db, job.id, step, f"{state}{' · ' + detail if detail else ''}")


def _teardown_kb_target(agent: Agent, workspace: WorkspaceContext) -> str:
    """Only a preset that mounted KBs has an agentic target; resolve it by the row's
    own name while the row still owns that name (the fence guarantees no
    replacement install exists yet)."""
    if not (agent.spec or {}).get("knowledge_bases"):
        return "no knowledge base mounted"
    gateway_id = workspace.resources.get("kb_gateway_id")
    if not gateway_id:
        return "workspace has no KB gateway"
    control = harness_method.control_client(workspace)
    kbgw.delete_agentic_target(control, gateway_id, (agent.spec or {}).get("name") or agent.name)
    return "agentic target removed"


def _teardown_harness(agent: Agent, workspace: WorkspaceContext, log) -> str:
    if not agent.resource_id:
        return "no harness recorded"
    client = harness_method.control_client(workspace)
    try:
        hc.delete_harness(client, agent.resource_id)
        log(f"DeleteHarness accepted for {agent.resource_id}")
    except client.exceptions.ResourceNotFoundException:
        return "harness already gone"
    deadline = time.monotonic() + HARNESS_GONE_TIMEOUT_S
    while True:
        try:
            status = hc.get_harness(client, agent.resource_id).get("status")
        except client.exceptions.ResourceNotFoundException:
            return "harness gone (GetHarness → ResourceNotFound)"
        if status == "DELETE_FAILED":
            raise RuntimeError(
                f"harness {agent.resource_id} reports DELETE_FAILED — retry the uninstall"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"harness {agent.resource_id} still {status or '?'} after "
                f"{HARNESS_GONE_TIMEOUT_S}s — retry the uninstall"
            )
        log(f"harness {agent.resource_id} status {status or '?'} — waiting for deletion")
        _sleep(HARNESS_POLL_S)


def _teardown_role(agent: Agent, workspace: WorkspaceContext, log) -> str:
    settings = get_settings()
    if not settings.per_agent_execution_roles:
        return "shared role — nothing to delete"
    if not agent_iam.delete_execution_role(agent, settings, workspace, log):
        raise RuntimeError(
            f"execution role {agent_iam.role_name_for(agent.name, agent.id)} could not be "
            "deleted — retry the uninstall"
        )
    return "execution role deleted"


def execute_uninstall_job(job_id: str, *, resume: bool = False) -> None:
    """Run (or, with ``resume=True``, resume) one uninstall job. Never raises."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None or job.type != JOB_TYPE:
            return
        if job.status not in ("queued", "running"):
            return  # terminal — inert
        allowed = ("queued", "running") if resume else ("queued",)
        won = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status.in_(allowed))
            .values(status="running", error=None)
        ).rowcount
        db.commit()
        if won != 1:
            logger.info("uninstall job %s: not claimed (another worker owns it)", job_id)
            return
        try:
            job, agent = _fence(db, job_id)
        except Superseded as exc:
            _fail(db, db.get(Job, job_id), None, f"superseded: {exc}")
            return

        def log(msg: str) -> None:
            _append_log(db, job_id, "teardown", msg)

        workspace = context_for_workspace(job.workspace_id)
        steps: dict[str, Any] = {
            "kb_target": lambda: _teardown_kb_target(agent, workspace),
            "harness": lambda: _teardown_harness(agent, workspace, log),
            "role": lambda: _teardown_role(agent, workspace, log),
        }
        for step in _STEPS:
            try:
                job, agent = _fence(db, job_id)  # before EVERY cloud step
                done = ((job.payload or {}).get("progress") or {}).get(step, {})
                if done.get("state") == "done":
                    continue
                detail = steps[step]()
                _progress(db, job, step, "done", detail)
            except Superseded as exc:
                _fail(db, db.get(Job, job_id), None, f"superseded during {step}: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 — recorded, retryable
                detail = f"{type(exc).__name__}: {exc}"
                _append_log(db, job_id, step, traceback.format_exc(limit=3), level="debug")
                _progress(db, db.get(Job, job_id), step, "failed", detail)
                _fail(db, db.get(Job, job_id), agent, f"{step}: {detail}")
                return

        # fenced finalization: the row flips only if it is still ours to flip
        try:
            job, agent = _fence(db, job_id)
        except Superseded as exc:
            _fail(db, db.get(Job, job_id), None, f"superseded at finalization: {exc}")
            return
        flipped = db.execute(
            update(Agent)
            .where(Agent.id == agent.id, Agent.status == "uninstalling",
                   Agent.updated_at == agent.updated_at)
            .values(status="deleted", error=None, updated_at=datetime.now(UTC))
        ).rowcount
        if flipped != 1:
            db.rollback()
            _fail(db, db.get(Job, job_id), None, "superseded at finalization: row changed")
            return
        job.status = "succeeded"
        job.payload = {**(job.payload or {}), "aws_resource_deleted": True}
        db.commit()
        _append_log(db, job_id, "teardown", "done · every resource verified gone")
    except Exception as exc:  # noqa: BLE001 — job-level failure must never crash the worker
        db.rollback()
        logger.exception("uninstall job %s failed outside teardown: %s", job_id, exc)
        job = db.get(Job, job_id)
        if job is not None and job.status == "running":
            _fail(db, job, None, f"{type(exc).__name__}: {exc}")
    finally:
        db.close()


def _fail(db, job: Job | None, agent: Agent | None, detail: str) -> None:
    if job is None:
        return
    db.refresh(job)
    if job.status != "running":
        # a terminal job belongs to whoever finished it; a stale worker records nothing
        logger.info("uninstall job %s: %s (job already %s)", job.id, detail, job.status)
        return
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


def start_uninstall_resume(job_id: str) -> threading.Thread:
    """Startup resume: may pick up a job the crashed process left ``running``."""
    thread = threading.Thread(
        target=execute_uninstall_job, args=(job_id,), kwargs={"resume": True}, daemon=True
    )
    thread.start()
    return thread
