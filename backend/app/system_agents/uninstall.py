"""The uninstall worker for system-managed presets.

Queued by ``service.uninstall_preset`` (claim + job in one commit), run on a daemon
thread by the router, and resumed by ``pipeline.resume_pending_jobs`` after a
restart — the same conventions as deploy jobs, plus the guarantees this preset
promises that the ordinary delete does not:

* **exclusive execution on this host** — a worker runs only while it holds an
  advisory ``fcntl`` lock on a per-agent file under ``data/locks/system-agents/``
  (released automatically when the holder's process dies, so crash recovery never
  waits on a dead owner) **and** wins the job's ``queued → running`` CAS (a startup
  resume may adopt a ``running`` job the crashed process left — the lock is what
  makes that adoption safe against a still-alive twin). This is a single-host,
  SQLite-shaped guarantee: the ledger and the lock directory live on one machine.
  It is not a distributed lease;
* **fenced effects** — before every cloud step and inside the finalizing
  transaction the worker re-reads job + row and requires: right job type and
  workspace, job still ``running``, row still ``uninstalling``, this job still the
  row's newest attempt. A superseded, duplicate or late worker is inert;
* **exact resource identity** — the harness id comes from the row, the KB target id
  is resolved once under the fence (by the reserved name, while the row still owns
  that name exclusively) and pinned on the job BEFORE deletion; deletion and
  readback use the pinned id, never a fresh name lookup; the execution role is the
  deterministic per-agent role and must carry this agent's ownership tag;
* **strict teardown** — the KB agentic target (any historical one, not only what the
  desired spec says) must be read back as gone; ``DeleteHarness`` is polled until
  ``GetHarness`` raises ``ResourceNotFoundException``; the role is deleted with the
  low-level IAM helper and its ``False`` is a failure. ``AccessDenied``, throttling,
  ``FAILED``/``DELETE_FAILED`` and timeouts keep the row ``uninstalling`` for a retry.
  Per-step progress (with the exact resource ids) is recorded on the job; a new
  attempt carries verified steps forward only when their resource ids still match;
* **deleted means gone** — only a fully verified teardown marks the row ``deleted``.

The ordinary agent delete (``routers.agents._delete_agent_resources``) and the
best-effort ``kb_gateway`` helpers keep their semantics; this module does not call
them for cleanup.
"""

import fcntl
import logging
import os
import threading
import time
import traceback
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update

from app.core.config import DATA_DIR
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
# Harness / target deletion is asynchronous. Bounded waits: a timeout is a retryable
# failure, never a success.
HARNESS_GONE_TIMEOUT_S = 90
TARGET_GONE_TIMEOUT_S = 60
POLL_S = 3
_sleep = time.sleep  # injectable in tests
LOCK_DIR = DATA_DIR / "locks" / "system-agents"

_STEPS = ("kb_target", "harness", "role")


class Superseded(Exception):
    """This worker no longer owns the row; every effect is skipped."""


# ---------------------------------------------------------------------------
# exclusivity: advisory per-agent lock (single host) + durable fence
# ---------------------------------------------------------------------------


def _acquire_lock(agent_id: str):
    """Exclusive, non-blocking advisory lock for one agent's teardown; None if held.

    ``flock`` locks are owned by the open file description: the kernel releases them
    when the holder exits or crashes, so a resume after a process death proceeds
    while a still-running twin (thread or process on this host) is refused.
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_DIR / f"uninstall-{agent_id}.lock", os.O_RDWR | os.O_CREAT, 0o600)
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


def _progress(db, job_id: str, step: str, state: str, detail: str = "", **extra: Any) -> None:
    """Record step progress on the job — fenced, so a superseded worker writes nothing."""
    job, _agent = _fence(db, job_id)
    progress = dict((job.payload or {}).get("progress") or {})
    entry = {**progress.get(step, {}), "state": state, "detail": detail,
             "at": datetime.now(UTC).isoformat(), **extra}
    progress[step] = entry
    job.payload = {**(job.payload or {}), "progress": progress}
    db.commit()
    _append_log(db, job.id, step, f"{state}{' · ' + detail if detail else ''}")


def _step_state(job: Job, step: str) -> dict[str, Any]:
    return dict(((job.payload or {}).get("progress") or {}).get(step) or {})


# ---------------------------------------------------------------------------
# strict teardown steps (low-level clients; every failure propagates)
# ---------------------------------------------------------------------------


def _is_not_found(client: Any, exc: BaseException) -> bool:
    rnf = getattr(getattr(client, "exceptions", None), "ResourceNotFoundException", None)
    if rnf is not None and isinstance(exc, rnf):
        return True
    return type(exc).__name__ == "ResourceNotFoundException"


def _list_all_targets(control: Any, gateway_id: str) -> list[dict[str, Any]]:
    """Every gateway target across all pages (the shared helper reads one page)."""
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"gatewayIdentifier": gateway_id, "maxResults": 100}
    while True:
        page = control.list_gateway_targets(**kwargs)
        items.extend(page.get("items") or [])
        token = page.get("nextToken")
        if not token:
            return items
        kwargs["nextToken"] = token


def _teardown_kb_target(
    agent: Agent, workspace: WorkspaceContext, *, db=None, job_id: str | None = None
) -> str:
    """Remove this installation's agentic KB target, strictly.

    The desired spec is NOT consulted to decide whether a target exists: a failed
    detach-repair rewrites the spec before the old target is gone. While the row is
    ``uninstalling`` it owns the reserved name exclusively, so the target carrying
    ``agentic_target_name(<preset name>)`` is this installation's (historical or
    current). Its id is pinned on the job before deletion; deletion and readback use
    the pinned id only.
    """
    gateway_id = workspace.resources.get("kb_gateway_id")
    recorded = _step_state(db.get(Job, job_id), "kb_target") if db is not None and job_id else {}
    target_id = recorded.get("target_id")
    if not gateway_id:
        if target_id or (agent.spec or {}).get("knowledge_bases"):
            raise RuntimeError(
                "a knowledge-base target may exist but the workspace resource map has no "
                "kb_gateway_id — restore the KB gateway resources, then retry the uninstall"
            )
        return "workspace has no KB gateway"
    control = harness_method.control_client(workspace)
    if not target_id:
        name = kbgw.agentic_target_name((agent.spec or {}).get("name") or agent.name)
        matches = [t for t in _list_all_targets(control, gateway_id) if t.get("name") == name]
        if not matches:
            return "no agentic target on the KB gateway"
        target_id = matches[0]["targetId"]
        if db is not None and job_id:
            # pin the exact id BEFORE the delete; the fence inside _progress guarantees
            # the row is still ours to clean
            _progress(db, job_id, "kb_target", "pinned", f"target {target_id}",
                      target_id=target_id)
    try:
        control.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
    except Exception as exc:  # noqa: BLE001 — anything but not-found propagates
        if not _is_not_found(control, exc):
            raise
        return f"agentic target {target_id} already gone"
    deadline = time.monotonic() + TARGET_GONE_TIMEOUT_S
    while True:
        try:
            status = control.get_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target_id
            ).get("status")
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(control, exc):
                return f"agentic target {target_id} gone (GetGatewayTarget → ResourceNotFound)"
            raise
        if status in ("FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"):
            raise RuntimeError(f"target {target_id} reports {status} — retry the uninstall")
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"target {target_id} still {status or '?'} after {TARGET_GONE_TIMEOUT_S}s — "
                "retry the uninstall"
            )
        _sleep(POLL_S)


def _teardown_harness(agent: Agent, workspace: WorkspaceContext, log) -> str:
    if not agent.resource_id:
        return "no harness recorded"
    client = harness_method.control_client(workspace)
    try:
        hc.delete_harness(client, agent.resource_id)
        log(f"DeleteHarness accepted for {agent.resource_id}")
    except Exception as exc:  # noqa: BLE001
        if not _is_not_found(client, exc):
            raise
        return "harness already gone"
    deadline = time.monotonic() + HARNESS_GONE_TIMEOUT_S
    while True:
        try:
            status = hc.get_harness(client, agent.resource_id).get("status")
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(client, exc):
                return "harness gone (GetHarness → ResourceNotFound)"
            raise
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
        _sleep(POLL_S)


def _teardown_role(agent: Agent, workspace: WorkspaceContext, log) -> str:
    """Delete the preset's dedicated role by *installed* ownership, not by the current
    ``per_agent_execution_roles`` toggle: a preset is never deployed on the shared role
    (`service.require_dedicated_role`), so its role is always the deterministic
    per-agent one. The role must carry this agent's ownership tag; the shared
    workspace role is never touched."""
    name = agent_iam.role_name_for(agent.name, agent.id)
    shared = agent_iam.shared_role_arn(workspace)
    iam = workspace.client("iam")
    try:
        role = iam.get_role(RoleName=name)["Role"]
    except Exception as exc:  # noqa: BLE001
        if agent_iam._is_no_such_entity(exc):
            return f"execution role {name} already gone"
        raise
    if role.get("Arn") and role["Arn"] == shared:
        raise RuntimeError(f"role {name} is the shared workspace role — refusing to delete it")
    tags = {t.get("Key"): t.get("Value") for t in role.get("Tags") or []}
    if tags.get(agent_iam.MANAGED_TAG_KEY) != agent.id:
        raise RuntimeError(
            f"role {name} is not tagged {agent_iam.MANAGED_TAG_KEY}={agent.id} — not this "
            "installation's role; refusing to delete it (retry after inspecting IAM)"
        )
    if not agent_iam.delete_role(iam, agent, log):
        raise RuntimeError(f"execution role {name} could not be deleted — retry the uninstall")
    return f"execution role {name} deleted"


# ---------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------


def _resource_id_for(step: str, agent: Agent) -> str:
    if step == "harness":
        return agent.resource_id or ""
    if step == "role":
        return agent_iam.role_name_for(agent.name, agent.id)
    return ""  # kb_target carries its own pinned target_id


def execute_uninstall_job(job_id: str, *, resume: bool = False) -> None:
    """Run (or, with ``resume=True``, resume) one uninstall job. Never raises."""
    db = SessionLocal()
    lock = None
    try:
        job = db.get(Job, job_id)
        if job is None or job.type != JOB_TYPE:
            return
        if job.status not in ("queued", "running"):
            return  # terminal — inert
        agent_id = (job.payload or {}).get("agent_id") or ""
        lock = _acquire_lock(agent_id)
        if lock is None:
            logger.info("uninstall job %s: another worker holds the teardown lock", job_id)
            return
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
        for step in _STEPS:
            try:
                job, agent = _fence(db, job_id)  # before EVERY cloud step
                done = _step_state(job, step)
                if done.get("state") == "done" and done.get("resource_id", "") == (
                    _resource_id_for(step, agent)
                ):
                    continue  # verified earlier for the SAME resource
                if step == "kb_target":
                    detail = _teardown_kb_target(agent, workspace, db=db, job_id=job_id)
                elif step == "harness":
                    detail = _teardown_harness(agent, workspace, log)
                else:
                    detail = _teardown_role(agent, workspace, log)
                _progress(db, job_id, step, "done", detail,
                          resource_id=_resource_id_for(step, agent))
            except Superseded as exc:
                _fail(db, db.get(Job, job_id), None, f"superseded during {step}: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 — recorded, retryable
                detail = f"{type(exc).__name__}: {exc}"
                _append_log(db, job_id, step, traceback.format_exc(limit=3), level="debug")
                try:
                    _progress(db, job_id, step, "failed", detail)
                except Superseded:
                    pass
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
        _release_lock(lock)
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
    """Startup resume: may adopt a job the crashed process left ``running`` — safe
    because the advisory lock the dead process held is gone, while a live twin's
    lock still refuses us."""
    thread = threading.Thread(
        target=execute_uninstall_job, args=(job_id,), kwargs={"resume": True}, daemon=True
    )
    thread.start()
    return thread


