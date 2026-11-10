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
* **exact resource identity** — the Runtime id comes from the row (``agent.resource_id``,
  set by the ordinary register stage every method shares); the execution role is the
  deterministic per-agent role and must carry this agent's ownership tag; the KB
  agentic target this preset might once have used has no CN implementation and is
  never resolved;
* **strict teardown** — ``DeleteAgentRuntime`` is polled until ``GetAgentRuntime``
  raises ``ResourceNotFoundException`` (the self-built Harness is an ordinary
  AgentCore Runtime; the same two calls `zip_runtime.delete_agent_resources` and
  `agentcore.runtime.get_runtime` wrap for every other method — mirrors the pre-CN
  ``DeleteHarness``/``GetHarness`` loop); the role is deleted with the low-level IAM
  helper and its ``False`` is a failure. ``AccessDenied``, throttling, a timed-out
  poll and a Runtime that never reaches "gone" keep the row ``uninstalling`` for a
  retry. Per-step progress (with the exact resource ids) is recorded on the job; a
  new attempt carries verified steps forward only when their resource ids still match;
* **registry records go with the preset** — the ``registry`` step deletes the A2A record
  the register stage made (``deployer.teardown._step_registry_record``, same name/kind
  check as an ordinary delete) and the preset's protected AGENT_SKILLS record through the
  maintenance path that owns it (``skill_registry.unregister_system_skill``), both
  idempotently (N10v3-bug6 / N11-r4-bug1: the records used to outlive the preset, and the
  protected one could be removed by nobody);
* **deleted means gone** — only a fully verified teardown marks the row ``deleted``.

The ordinary agent delete (``routers.agents._delete_agent_resources``) and the
best-effort knowledge-base helpers keep their semantics; this module does not call
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
from app.deployer import teardown, zip_runtime
from app.deployer.pipeline import _append_log, _log_line
from app.models.ledger import Agent, Job
from app.services import agent_iam
from app.services.agentcore import runtime as rt
from app.services.workspace import WorkspaceContext, context_for_workspace

logger = logging.getLogger("launchpad.system_agents")

JOB_TYPE = "uninstall_system_agent"
# A QUEUED job may wait this long for a retiring predecessor's lock (the previous
# attempt commits its failure before it releases the lock, so an operator can request
# the retry inside that window). A running job never waits: it is either ours (resume)
# or someone else's.
LOCK_WAIT_S = 30
LOCK_POLL_S = 0.2
_lock_sleep = time.sleep
LOCK_DIR = DATA_DIR / "locks" / "system-agents"

# How long the "harness" step polls GetAgentRuntime after DeleteAgentRuntime before
# giving up and failing the job (retried by another explicit uninstall).
RUNTIME_GONE_TIMEOUT_S = 300
RUNTIME_GONE_POLL_S = 2
_sleep = time.sleep

_STEPS = ("kb_target", "harness", "role", "registry")


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


def _teardown_runtime(agent: Agent, workspace: WorkspaceContext, log) -> str:
    """Delete the preset's AgentCore Runtime and poll until it reads back gone.

    The self-built Harness is an ordinary Runtime (``deployer.harness_local`` shares
    `zip_runtime`'s provision/deploy/register stages), so teardown is the same two
    calls every other method's delete uses — mirroring the pre-CN ``DeleteHarness``
    accepted-then-poll-``GetHarness`` shape this module's docstring describes.
    """
    if not agent.resource_id:
        return "runtime already gone (no resource id)"
    client = zip_runtime.control_client(workspace)
    try:
        rt.delete_runtime(client, agent.resource_id)
    except client.exceptions.ResourceNotFoundException:
        return f"runtime {agent.resource_id} already gone"
    log(f"DeleteAgentRuntime accepted for {agent.resource_id}; polling GetAgentRuntime")
    deadline = time.monotonic() + RUNTIME_GONE_TIMEOUT_S
    while True:
        try:
            rt.get_runtime(client, agent.resource_id)
        except client.exceptions.ResourceNotFoundException:
            return f"runtime {agent.resource_id} deleted"
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"runtime {agent.resource_id} still present after {RUNTIME_GONE_TIMEOUT_S}s"
            )
        _sleep(RUNTIME_GONE_POLL_S)


def _teardown_registry(db, agent: Agent, workspace: WorkspaceContext, log) -> str:
    """Delete the preset's registry records: its A2A record (by id, only if it is still
    THIS agent's A2A record — the ordinary delete's check) and its protected Skill record
    + mapping row through the maintenance path. Not-found is "already gone"; any other
    registry error propagates and fails the step for a retry."""
    from app.system_agents import skill_registry  # resolved here: it imports service

    a2a = teardown._step_registry_record(agent, workspace, log)
    skill = skill_registry.unregister_system_skill(db, workspace, agent.system_key or "", log)
    detail = f"a2a: {a2a.detail or a2a.status} · skill: {skill}"
    if a2a.status == teardown.SKIPPED and skill.startswith("skipped"):
        return "skipped: " + detail
    return detail


# ---------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------


def _resource_id_for(step: str, agent: Agent) -> str:
    if step == "harness":
        return agent.resource_id or ""
    if step == "role":
        return agent_iam.role_name_for(agent.name, agent.id)
    if step == "registry":
        return agent.registry_record_id or ""
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
        if lock is None and job.status == "queued":
            # a distinct queued retry behind a retiring predecessor: wait, bounded, for
            # its lock to be released, then proceed through the same CAS + fence
            deadline = time.monotonic() + LOCK_WAIT_S
            while lock is None and time.monotonic() < deadline:
                _lock_sleep(LOCK_POLL_S)
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
                    detail = "skipped: not managed in this build"
                elif step == "harness":
                    detail = _teardown_runtime(agent, workspace, log)
                elif step == "role":
                    detail = _teardown_role(agent, workspace, log)
                else:
                    detail = _teardown_registry(db, agent, workspace, log)
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
        job.payload = {**(job.payload or {}), "aws_resource_deleted": True,
                       "teardown": teardown_summary(job)}
        # N17-obs479: the final line rides in the terminal commit
        _log_line(job, "teardown", "done · every resource verified gone")
        db.commit()
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
    job.payload = {**(job.payload or {}), "teardown": teardown_summary(job)}
    if agent is not None:
        agent.error = f"uninstall failed: {detail}"[:500]
        agent.updated_at = datetime.now(UTC)
    _log_line(job, "teardown", detail, level="error")  # N17-obs479: one commit
    db.commit()


def teardown_summary(job: Job) -> dict[str, list[str]]:
    """``{removed, skipped, failed}`` step names from the job's per-step progress — what
    this attempt verified gone, what it never manages here, and where it stopped. Written
    on success and on failure so the console can say what a stuck uninstall did remove
    (N10v2-bug8) instead of only that it failed."""
    progress = (job.payload or {}).get("progress") or {}
    removed = [s for s in _STEPS if progress.get(s, {}).get("state") == "done"
               and not str(progress[s].get("detail", "")).startswith("skipped")]
    skipped = [s for s in _STEPS if progress.get(s, {}).get("state") == "done"
               and str(progress[s].get("detail", "")).startswith("skipped")]
    failed = [s for s in _STEPS if progress.get(s, {}).get("state") == "failed"]
    return {"removed": removed, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------------------
# starters: at most one live worker thread per job in this process
# ---------------------------------------------------------------------------
#
# The route re-wakes a still-queued job on every repeated DELETE (a queued job must
# never depend on a restart), and a queued worker may wait up to LOCK_WAIT_S for a
# retiring predecessor's lock. Without coalescing, N repeated requests would park N
# waiting threads on one job. This registry bounds that to one live worker per job
# *in this process*; it is a waiter bound only — the kernel flock (per agent, cross
# process) and the queued→running CAS remain the exclusivity and ownership mechanisms.
_WORKERS: dict[str, threading.Thread] = {}
_WORKERS_LOCK = threading.Lock()


def live_worker(job_id: str) -> threading.Thread | None:
    """The live worker thread tracked for ``job_id`` in this process, if any."""
    with _WORKERS_LOCK:
        thread = _WORKERS.get(job_id)
        return thread if thread is not None and thread.is_alive() else None


def _launch(job_id: str, *, resume: bool) -> threading.Thread:
    """Start a worker for ``job_id`` unless one is already live here; return the live one.

    The registry entry is removed in the worker's own ``finally`` (completion, failure,
    superseded, lock-wait timeout) and on a start failure, so a later request can
    re-wake the same queued job with a fresh thread. Concurrent callers serialize on
    the registry lock, so exactly one of them creates the thread.
    """
    with _WORKERS_LOCK:
        existing = _WORKERS.get(job_id)
        if existing is not None and existing.is_alive():
            return existing

        def run() -> None:
            try:
                execute_uninstall_job(job_id, resume=resume)
            finally:
                with _WORKERS_LOCK:
                    if _WORKERS.get(job_id) is threading.current_thread():
                        del _WORKERS[job_id]

        thread = threading.Thread(target=run, daemon=True, name=f"uninstall-{job_id[:8]}")
        _WORKERS[job_id] = thread
        try:
            thread.start()
        except Exception:
            _WORKERS.pop(job_id, None)  # never leave a dead placeholder behind
            raise
        return thread


def start_uninstall_async(job_id: str) -> threading.Thread:
    return _launch(job_id, resume=False)


def start_uninstall_resume(job_id: str) -> threading.Thread:
    """Startup resume: may adopt a job the crashed process left ``running`` — safe
    because the advisory lock the dead process held is gone, while a live twin's
    lock still refuses us."""
    return _launch(job_id, resume=True)
