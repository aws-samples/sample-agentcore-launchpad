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
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text, update
from sqlalchemy.orm import Session

from app.core.actor import user_id_for
from app.core.db import SessionLocal
from app.core.project_scope import project_context
from app.models.ledger import Agent, Deployment, Job
from app.services import memory_ownership, workspace_bootstrap
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
    actor: str | None = None,
    note: str | None = None,
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
    freshly minted ids onto its own rows and commit everything at once.

    Every call also writes a ledger spec snapshot (T18) in the same transaction —
    ``actor`` names who published (default: the agent's owner) and ``note`` is a free
    label such as "rollback to #2"."""
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
        # the agent's project, also when the deploy starts off a request (a promotion
        # worker, a resumed job); a system preset's deploy is platform-level
        project_id=agent.project_id,
        type="deploy_agent",
        payload={
            **({"platform": True} if agent.project_id is None else {}),
            "agent_id": agent.id,
            "deployment_id": deployment.id,
            "mode": mode,
            "skip_register": skip_register,
            # who published and why — read back by the Agent-DLC release bookkeeping
            "actor": actor,
            # the requester's immutable id, for SoD on the release record (§3.6)
            "actor_user_id": user_id_for(actor),
            "note": note,
            **(payload_extra or {}),
        },
    )
    db.add(job)
    db.flush()
    deployment.job_id = job.id
    from app.services.snapshots import record_snapshot

    record_snapshot(db, agent, deployment_id=deployment.id, created_by=actor, note=note)
    if commit:
        db.commit()
    else:
        db.flush()
    return deployment, job


# ── network gate (design projects §12.4–§12.6) ─────────────────────────────────

def _workspace_ec2(workspace: WorkspaceContext) -> Any:
    return workspace.client("ec2")


# ec2 client factory for the deploy-time preflight; tests swap it (`set_preflight_ec2`)
_preflight_ec2: Callable[[WorkspaceContext], Any] = _workspace_ec2


def set_preflight_ec2(
    factory: Callable[[WorkspaceContext], Any],
) -> Callable[[WorkspaceContext], Any]:
    """Swap the EC2 client the deploy-time preflight reads (tests); returns the old one."""
    global _preflight_ec2
    previous, _preflight_ec2 = _preflight_ec2, factory
    return previous


def network_gate(
    db: Session, workspace: WorkspaceContext, spec: dict[str, Any], *,
    previous_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Refuse a deploy before its job exists when its network cannot work; returns the
    job payload extras the network needs (empty for most deploys).

    * no network while the workspace forbids PUBLIC → `network.public_not_allowed`
      (`network_enforcement.assert_public_allowed`);
    * a network profile → the preflight re-run for THIS agent's features
      (`network_preflight.features_of_spec`): any blocking item → 409
      `network.preflight_failed` with the blocking items as `details`. A
      `legacy-<hash8>` profile (migrated from an inline BYO network, §12.10) is not
      preflighted — its deploys behave as they did before profiles existed.

    A re-publish that moves the agent OFF a profile carries the old network
    (`network_from`), so the harness deployer sets PUBLIC instead of keeping the
    live value."""
    from app.core.errors import AppError
    from app.deployer.network import NETWORK_FROM_KEY, network_of
    from app.models.network import LEGACY_PREFIX
    from app.services import network_enforcement, network_profiles
    from app.services import network_preflight as preflight

    network = network_of(spec)
    network_enforcement.assert_public_allowed(db, workspace.id, network)
    extras: dict[str, Any] = {}
    previous = network_of(previous_spec) if previous_spec is not None else None
    if previous and previous != network:
        extras[NETWORK_FROM_KEY] = previous
    profile_id = network_profiles.network_profile_id(network)
    if profile_id is None or profile_id.startswith(LEGACY_PREFIX):
        return extras
    report = network_profiles.run_check(
        db, workspace, profile_id, features=preflight.features_of_spec(spec),
        ec2=_preflight_ec2(workspace),
    )
    blocking = [r for r in report["results"] if r.get("severity") == "block"]
    if blocking:
        raise AppError(
            "network.preflight_failed",
            f"network profile '{profile_id}' cannot host this agent: "
            + "; ".join(str(r.get("message") or r.get("check")) for r in blocking),
            {"profile_id": profile_id, "results": blocking},
            status_code=409,
        )
    return extras


# ── teardown: wait for AgentCore to release a VPC agent's ENIs (design projects §12.9) ──
#
# AgentCore's network service-linked role holds ENIs in the profile's subnets and lets
# go of them some time after the runtime / harness is deleted; until then the subnets
# and security groups cannot be deleted. The agent delete starts one `agent_eni_release`
# job (`start_eni_release_wait`) that polls `ec2:DescribeNetworkInterfaces` (filtered by
# the profile's security groups and subnets) for a few minutes; what is still there
# after that is the scheduled teardown's (`sweep_eni_releases`, run by `dlc.teardown`)
# until a hard deadline fails the job with the ENI ids. The profile cannot be archived
# while such a job runs (`network_profiles.archive_profile`).
#
# Which ENIs are this agent's: the ones whose description names its runtime / harness
# id; when no other live agent shares the profile, every ENI in its subnets + SGs (the
# service documents no tag or description format, so a shared profile without a
# matching description has nothing attributable to wait for).

ENI_JOB_TYPE = "agent_eni_release"
ENI_WAIT_SECONDS = 300
ENI_POLL_SECONDS = 15
ENI_DEADLINE = 6 * 3600  # seconds after the delete; then the job fails with the ids

# ec2 client factory for the ENI poll; tests swap it (`set_teardown_ec2`)
_teardown_ec2: Callable[[WorkspaceContext], Any] = _workspace_ec2
_eni_sleep: Callable[[float], None] = time.sleep
_ENI_WAITERS: dict[str, threading.Thread] = {}


def set_teardown_ec2(
    factory: Callable[[WorkspaceContext], Any],
) -> Callable[[WorkspaceContext], Any]:
    """Swap the EC2 client the teardown ENI poll reads (tests); returns the old one."""
    global _teardown_ec2
    previous, _teardown_ec2 = _teardown_ec2, factory
    return previous


def _agent_enis(workspace: WorkspaceContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The ENIs still held for the deleted agent the job payload describes."""
    vpc = payload.get("vpc") or {}
    ec2 = _teardown_ec2(workspace)
    kwargs: dict[str, Any] = {"Filters": [
        {"Name": "group-id", "Values": list(vpc.get("security_groups") or [])},
        {"Name": "subnet-id", "Values": list(vpc.get("subnets") or [])},
    ]}
    rows: list[dict[str, Any]] = []
    while True:
        page = ec2.describe_network_interfaces(**kwargs)
        rows.extend(page.get("NetworkInterfaces") or [])
        token = page.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    marks = [m for m in (payload.get("resource_id"), payload.get("arn")) if m]
    named = [e for e in rows if any(m in str(e.get("Description") or "") for m in marks)]
    if named or payload.get("shared"):
        return named
    return rows


def start_eni_release_wait(
    db: Session, agent: Agent, workspace: WorkspaceContext, *, start: bool = True,
) -> str | None:
    """After an agent's AWS resource is deleted: the job id of the ENI wait, or None
    when the agent ran PUBLIC (nothing to wait for) or its network no longer resolves."""
    from app.core.errors import AppError
    from app.services import network_profiles

    network = (agent.spec or {}).get("network") or None
    if network is None or not (agent.resource_id or agent.arn):
        return None
    try:
        vpc = network_profiles.resolve_vpc(db, agent.workspace_id or "", network)
    except AppError:
        return None
    if not vpc:
        return None
    profile_id = network_profiles.network_profile_id(network)
    shared = False
    if profile_id is not None:
        users = network_profiles.agents_using(db, agent.workspace_id or "", profile_id)
        # the agent itself still counts while its row is not yet marked deleted
        own = 0 if agent.status == "deleted" else 1
        shared = users.get(profile_id, 0) - own > 0
    deadline = datetime.now(UTC).timestamp() + ENI_DEADLINE
    job = Job(
        workspace_id=agent.workspace_id,
        project_id=agent.project_id,
        type=ENI_JOB_TYPE,
        status="running",
        payload={
            **({"platform": True} if agent.project_id is None else {}),
            "agent_id": agent.id,
            "resource_id": agent.resource_id,
            "arn": agent.arn,
            "profile_id": profile_id,
            "vpc": vpc,
            "shared": shared,
            "deadline": deadline,
        },
    )
    db.add(job)
    db.commit()
    _append_log(db, job.id, "teardown",
                f"waiting for AgentCore to release the ENIs in {', '.join(vpc['subnets'])}")
    if start:
        start_eni_release_async(job.id)
    return job.id


def _poll_eni_job(db: Session, job: Job) -> bool:
    """One look; True once nothing attributable remains (the job is then succeeded)."""
    workspace = context_for_workspace(job.workspace_id)
    remaining = _agent_enis(workspace, job.payload or {})
    if not remaining:
        job.status = "succeeded"
        job.error = None
        db.commit()
        _append_log(db, job.id, "teardown", "every ENI released")
        return True
    ids = sorted(str(e.get("NetworkInterfaceId")) for e in remaining)
    if datetime.now(UTC).timestamp() >= float((job.payload or {}).get("deadline") or 0):
        job.status = "failed"
        job.error = (f"{len(ids)} ENI(s) still present after {ENI_DEADLINE // 3600} h: "
                     f"{', '.join(ids)} — check them in the EC2 console before deleting "
                     "the subnets or security groups")
        db.commit()
        _append_log(db, job.id, "teardown", job.error, level="error")
        return True
    payload = dict(job.payload or {})
    payload["remaining"] = ids
    job.payload = payload
    db.commit()
    return False


def wait_for_eni_release(job_id: str, *, wait_seconds: float = ENI_WAIT_SECONDS,
                         poll_seconds: float = ENI_POLL_SECONDS) -> None:
    """The waiter: poll every `poll_seconds` for up to `wait_seconds`, inside the job's
    project; then leave the job running for the scheduled sweep."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None or job.type != ENI_JOB_TYPE or job.status != "running":
            return
        with project_context(job.project_id):
            waited = 0.0
            while True:
                try:
                    if _poll_eni_job(db, job):
                        return
                except Exception as exc:  # noqa: BLE001 — a read failure is the sweep's
                    _append_log(db, job_id, "teardown",
                                f"ENI read failed ({type(exc).__name__}: {exc})",
                                level="warning")
                    break
                if waited >= wait_seconds:
                    break
                _eni_sleep(poll_seconds)
                waited += poll_seconds
            _append_log(db, job_id, "teardown",
                        "ENIs still present — the scheduled teardown keeps checking")
    finally:
        db.close()


def start_eni_release_async(job_id: str) -> threading.Thread:
    with _LIVE_LOCK:
        existing = _ENI_WAITERS.get(job_id)
        if existing is not None and existing.is_alive():
            return existing

        def run() -> None:
            try:
                wait_for_eni_release(job_id)
            finally:
                with _LIVE_LOCK:
                    if _ENI_WAITERS.get(job_id) is threading.current_thread():
                        del _ENI_WAITERS[job_id]

        thread = threading.Thread(target=run, daemon=True, name=f"eni-{job_id[:8]}")
        _ENI_WAITERS[job_id] = thread
        thread.start()
        return thread


def sweep_eni_releases(db: Session) -> dict[str, Any]:
    """The scheduled half (`dlc.teardown`): one look at each ENI wait no live waiter
    of this process is polling."""
    with project_context(None):
        jobs = db.query(Job).filter(Job.type == ENI_JOB_TYPE, Job.status == "running") \
            .limit(50).all()
    out: list[dict[str, Any]] = []
    for job in jobs:
        with _LIVE_LOCK:
            waiter = _ENI_WAITERS.get(job.id)
        if waiter is not None and waiter.is_alive():
            continue
        try:
            with project_context(job.project_id):
                done = _poll_eni_job(db, job)
            out.append({"job_id": job.id, "status": job.status if done else "waiting"})
        except Exception as exc:  # noqa: BLE001 — one job must not stop the rest
            db.rollback()
            out.append({"job_id": job.id, "status": f"error:{type(exc).__name__}"})
    return {"eni_releases": out}


def pending_eni_releases(db: Session, workspace_id: str, profile_id: str) -> list[str]:
    """Agents of this profile whose ENIs are still being waited for."""
    with project_context(None):
        jobs = db.query(Job).filter(Job.type == ENI_JOB_TYPE, Job.status == "running",
                                    Job.workspace_id == workspace_id).all()
    return [str((j.payload or {}).get("agent_id")) for j in jobs
            if (j.payload or {}).get("profile_id") == profile_id]


# Jobs the startup resume is about to adopt: the only launches allowed to take a job a
# dead process left `running`. Filled by `resume_pending_jobs` before it starts them.
_RESUMING: set[str] = set()


def execute_deploy_job(job_id: str, *, resume: bool | None = None) -> None:
    """Run the job as its project (rows it writes — releases, audit — are stamped and
    read like the request that started it); see ``_execute_deploy_job``."""
    db = SessionLocal()
    try:
        found = db.execute(
            text("SELECT project_id FROM jobs WHERE id = :id"), {"id": job_id},
        ).first()
    finally:
        db.close()
    with project_context(found[0] if found else None):
        _execute_deploy_job(job_id, resume=resume)


def _execute_deploy_job(job_id: str, *, resume: bool | None = None) -> None:
    """Run (or resume) one deploy job to completion. Never raises.

    Durable eligibility claim: a fresh launch (``start_deploy_async``) takes
    ``queued → running`` with one conditional UPDATE; only the explicit startup
    resume (and a direct call, which is how the resume tests drive it) may also adopt
    a job a dead process left ``running``. A terminal job (succeeded/failed) is inert
    — a late retry that re-wakes a job whose worker already finished runs nothing.
    """
    if resume is None:
        resume = True  # direct callers are resume drivers; the starter passes False
    if job_id in _RESUMING:
        _RESUMING.discard(job_id)
        resume = True
    db = SessionLocal()
    try:
        eligible = ["queued", "running"] if resume else ["queued"]
        claimed = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status.in_(eligible))
            .values(status="running", updated_at=datetime.now(UTC))
        ).rowcount
        db.commit()
        if claimed != 1:
            return
        job = db.get(Job, job_id)
        if job is None:
            return
        agent_id = job.payload["agent_id"]
        deployment_id = job.payload["deployment_id"]

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
        # A spec that pins its own memory deploys only onto a memory this
        # workspace manages and that is ACTIVE (issue #55) — checked here, before
        # any stage grants or binds it, so every path into a job (create,
        # re-publish, convert, recommendation accept, startup resume) is covered.
        memory_ownership.require_spec_memory(db, ctx.workspace, agent.spec)
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
            # The stages consume the reviewed bindings (gateway ARN + outbound auth,
            # memory ARN, KB gateway, skill content identity) instead of re-resolving.
            ctx.scratch["assistant_pin"] = job.payload["assistant"]
        ctx.scratch["mode"] = job.payload.get("mode", "create")
        if (job.payload or {}).get("network_from"):
            # a re-publish that moved the agent off a network profile (network_gate)
            ctx.scratch["network_from"] = job.payload["network_from"]

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
        from app.services.snapshots import stamp_version

        stamp_version(db, deployment_id, agent.version)
        # gate before traffic: a migrated agent's `live` endpoint follows (direct) or a
        # release record opens on `candidate` (gated) — never fails the deploy
        from app.dlc.releases import after_deploy

        payload = job.payload or {}
        after_deploy(db, agent, note=payload.get("note"), actor=payload.get("actor"),
                     actor_user_id=payload.get("actor_user_id"))
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
                execute_deploy_job(job_id, resume=False)
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
    from app.assistant import purge as assistant_purge
    from app.services import (
        cost_tags,
        project_isolation,
        project_migration,
        project_purge,
        promotion_exec,
        registry_sync,
        storage_provision,
    )
    from app.system_agents import uninstall as system_uninstall

    starters: dict[str, Callable[[str], threading.Thread]] = {
        "deploy_agent": start_deploy_async,
        workspace_bootstrap.JOB_TYPE: workspace_bootstrap.start_bootstrap_async,
        # a crashed uninstall is still `running` in the ledger; the resume starter
        # is the only caller allowed to pick such a job up again
        system_uninstall.JOB_TYPE: system_uninstall.start_uninstall_resume,
        # T27: a release interrupted mid-stage continues from the first unfinished one
        promotion_exec.JOB_TYPE: promotion_exec.start_resume,
        # an architect-conversation CLEAR resumes from its verified checkpoints
        assistant_purge.JOB_TYPE: assistant_purge.start_purge_resume,
        # a VPC agent's teardown keeps waiting for its ENIs (§12.9)
        ENI_JOB_TYPE: start_eni_release_async,

        # projects §10.2: an isolated project's provisioning continues from its first
        # unfinished stage, inside the job's project
        project_isolation.JOB_TYPE: project_isolation.start_provision_async,
        # projects §10.11: a migration continues from its first unfinished agent step
        # (a republish already started is waited on, not started again)
        project_migration.JOB_TYPE: project_migration.start_migration_async,
        # projects §11.5: a purge continues from its last finished table / agent
        project_purge.JOB_TYPE: project_purge.start_purge_resume,
        # cost-tags §1.3: a back-fill continues with the first resource not yet tagged
        cost_tags.JOB_TYPE: cost_tags.start_backfill_async,
        # POST /api/registry/sync-defaults: re-runs the sync (every step is an upsert)
        registry_sync.JOB_TYPE: registry_sync.start_async,
        # storage-mounts §7: an S3 Files create / teardown continues from its first
        # unfinished stage (every stage is list-or-get, then create)
        storage_provision.JOB_TYPE: storage_provision.start_async,
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
        if job_type == "deploy_agent":
            _RESUMING.add(job_id)  # startup may adopt a `running` job a dead process left
        starters[job_type](job_id)
    return [job_id for job_id, _ in found]
