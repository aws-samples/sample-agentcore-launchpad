"""Unified deploy pipeline.

Every creation method converges into the same ordered stages:

    generate → package → provision → deploy → register

A method module contributes one callable per stage (or omits it to skip).
Stage progress is persisted on the Deployment row and mirrored as JSONL
into the Job log, so a restarted backend can resume from the first
non-succeeded stage.
"""

import copy
import json
import logging
import os
import signal
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.deployer import teardown
from app.models.ledger import Agent, Deployment, Job
from app.services import kb_cleanup, memory_ownership, snapshots, workspace_bootstrap
from app.services.creators import Creator
from app.services.workspace import WorkspaceContext, context_for_workspace

STAGE_ORDER = ["generate", "package", "provision", "deploy", "register"]

# N16-obs83: the terminal status of a job whose agent row was deleted (or vanished)
# while it ran. Shared by the Job, the Deployment and the stage the job stopped at.
CANCELLED = "cancelled"
# N17-obs113: a stage whose subprocess was stopped WITH the service (the SIGTERM
# `systemctl restart launchpad-backend` sends to the whole control group) is recorded
# `interrupted`; the Job stays `running` and `resume_pending_jobs()` re-runs the stage at
# the next start — never a terminal `failed` for a build the operator did not break.
INTERRUPTED = "interrupted"
STAGE_INTERRUPTED = (
    "the stage's subprocess was stopped with the service — the job is left running and "
    "resumes from this stage at the next backend start"
)
STAGE_INTERRUPTED_RETRY = (
    "the stage's subprocess was killed while the service kept running — the job is left "
    "running and the stage is re-run once in-process after {delay:g} s"
)
STAGE_INTERRUPTED_TWICE = (
    "the stage's subprocess was killed again on the in-process retry — the job fails"
)
# One bounded in-process retry per stage for a signal kill the service did not cause
# (lead ruling on N17-obs113: a job never waits indefinitely for a restart that may not
# come); the delay is fixed, the scheduler injectable (tests run the callback themselves).
INTERRUPTED_RETRY_LIMIT = 1
INTERRUPTED_RETRY_DELAY_SECONDS = 5.0
INTERRUPTED_RETRIES_KEY = "interrupted_retries"
CANCEL_REASON = (
    "cancelled: the agent was deleted while this deploy job was running — the job stops "
    "here, no further stage runs and no further AWS resource is created"
)
# Job statuses under which a deploy is in flight (a delete of its agent is refused).
LIVE_JOB_STATUSES = ("queued", "running")

# Child of the "launchpad" logger app.main uses, so deploy failures reach the
# process log (journalctl on prod) and not only the per-job JSONL ledger.
logger = logging.getLogger("launchpad.deploy")


@dataclass
class StageResult:
    detail: str = ""
    skipped: bool = False


class StageInterrupted(Exception):
    """A stage could not finish because its subprocess was killed from outside — a signal
    kill (N17-obs113). The pipeline records the stage ``interrupted`` and leaves the job
    ``running``: for the startup resume when the service is stopping, otherwise for ONE
    in-process retry after a short delay; a second kill ends the job ``failed`` with
    ``failure`` (the message naming the signal) — raise it instead of a failure."""

    def __init__(self, detail: str, *, failure: str | None = None) -> None:
        super().__init__(detail)
        self.failure = failure or detail


_SHUTDOWN = threading.Event()


def note_shutdown(*_args: object) -> None:
    """Mark this process as stopping (the chained SIGTERM handler calls it)."""
    _SHUTDOWN.set()


def shutting_down() -> bool:
    return _SHUTDOWN.is_set()


def killed_by_signal(returncode: int | None) -> bool:
    """True when a stage subprocess's exit status says a signal killed it (negative
    returncode): pip / uv never terminate themselves that way — a stop of the service's
    control group, an operator's kill or the OOM killer did. Such a stage is interrupted,
    never failed outright; what happens next (`shutting_down()` → resume at the next start,
    else one in-process retry) is the pipeline's decision."""
    return returncode is not None and returncode < 0


def schedule_later(delay: float, fn: Callable[[], object]) -> None:
    """Run ``fn`` on a daemon timer thread after ``delay`` seconds — the retry scheduler;
    tests replace it and run the callback themselves (no sleep)."""
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()


def _retry_interrupted(job_id: str) -> threading.Thread:
    """The in-process re-run of a job whose stage was interrupted while the service kept
    running: an adopt-`running` resume on the same worker path the startup uses."""
    _RESUMING.add(job_id)
    return start_deploy_async(job_id)


def _chained_term_handler(previous: object):
    """The SIGTERM handler: flag the shutdown, then hand the signal on exactly as it would
    have been handled without us — the server's own Python handler when there is one,
    nothing for SIG_IGN, and the default action (terminate) for SIG_DFL or a handler not
    installed from Python (`getsignal` → None). The flag must never cost the stop."""
    def handler(signum: int, frame: object) -> None:
        note_shutdown()
        if callable(previous):
            previous(signum, frame)
        elif previous == signal.SIG_IGN:
            return
        else:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
    return handler


def install_shutdown_hook() -> bool:
    """Chain a SIGTERM handler that flips the shutdown flag before the server's own
    handler (uvicorn's `handle_exit`) runs. Main thread only — returns False (and does
    nothing) elsewhere, e.g. under a test harness."""
    try:
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _chained_term_handler(previous))
        return True
    except ValueError:
        return False


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


@dataclass(frozen=True)
class MethodCapabilities:
    """What a creation method's deployed artifact supports — declared once, next to the
    method's ``register_method`` call, and read by every surface that must decide
    eligibility (config-bundle experiments, Runtime canaries). N12-obs33/34: eligibility
    is a property of the artifact the method renders, never of the method's name.

    ``config_bundle``: the platform-rendered entrypoint applies a routed Launchpad config
    bundle (system prompt + tool descriptions; CA-2 pointer → S3) — true for the Strands
    zip template and the self-built Harness template (``harness_convert.graft_config_bundle``).
    ``user_source``: the artifact is member-authored code the platform does not rewrite
    (Studio canvas output, BYOC) — a bundle shim may exist but consumption is unverified.
    ``runtime_versions``: deployed as an AgentCore Runtime whose immutable versions can be
    pinned behind named endpoints (the canary topology).
    ``candidate_mint``: the platform can rebuild the artifact from an edited spec
    (``canary_infra.mint_candidate_version``) — false for BYOC, whose source it does not own.
    """

    config_bundle: bool = False
    user_source: bool = False
    runtime_versions: bool = False
    candidate_mint: bool = False
    # ── creation-time option domain (N15-obs16 → round 16; plans/CAPABILITY-MATRIX-DESIGN.md) ──
    # What the method's generate stage can RENDER from a spec. ``schemas.agent.
    # validate_against_method`` refuses an option outside these sets at the request boundary
    # (422 ``agent.method_unsupported``), ``GET /api/capabilities.methods`` publishes them
    # and the wizard disables what the current method cannot take. The renderers keep their
    # own checks as the last line of defence; ``tests/test_creation_matrix.py`` proves the
    # two agree.
    builtin_tools: frozenset[str] = frozenset()  # AgentCore builtin configs it renders
    tool_types: frozenset[str] = frozenset()  # ToolRef.type members rendered WITHOUT auth
    # acting modes of an authenticated rest / mcp ToolRef (empty = no identity channel)
    tool_auth_modes: frozenset[str] = frozenset()
    native_tools: frozenset[str] = frozenset()
    protocols: frozenset[str] = frozenset({"http"})
    memory: bool = True
    knowledge_bases: bool = True
    byoc_kinds: frozenset[str] = frozenset()
    # workspace resource keys the deploy needs from the first stage on (a gateway tool
    # additionally needs ``gateway_id`` — conditional, so not listed here)
    requires: frozenset[str] = frozenset()

    SET_FIELDS = ("builtin_tools", "tool_types", "tool_auth_modes", "native_tools",
                  "protocols", "byoc_kinds", "requires")
    FLAG_FIELDS = ("memory", "knowledge_bases")

    def as_dict(self) -> dict[str, Any]:
        """The creation-time domain, JSON-shaped (sorted lists) — the wire form of
        ``GET /api/capabilities.methods[<name>]`` and of the frozen test fixture."""
        out: dict[str, Any] = {k: sorted(getattr(self, k)) for k in self.SET_FIELDS}
        out.update({k: bool(getattr(self, k)) for k in self.FLAG_FIELDS})
        return out


_METHODS: dict[str, MethodStages] = {}
_CAPABILITIES: dict[str, MethodCapabilities] = {}
# The method modules register themselves on import (app.main imports them for that side
# effect); a caller that runs before app.main — a script, a bare unit test — gets the
# built-ins loaded on first lookup instead of a silently empty table.
_BUILTIN_METHOD_MODULES = (
    "app.deployer.zip_runtime", "app.deployer.harness_local", "app.deployer.byoc",
)


def register_method(
    name: str, stages: MethodStages, capabilities: MethodCapabilities | None = None,
) -> None:
    _METHODS[name] = stages
    _CAPABILITIES[name] = capabilities or MethodCapabilities()


def get_method(name: str) -> MethodStages:
    if name not in _METHODS:
        raise ValueError(f"no deploy method registered for '{name}'")
    return _METHODS[name]


def _load_builtin_methods() -> None:
    import importlib

    for module in _BUILTIN_METHOD_MODULES:
        importlib.import_module(module)


def method_capabilities(name: str | None) -> MethodCapabilities:
    """The declared capabilities of ``name``; an unregistered method supports nothing."""
    if name not in _CAPABILITIES:
        _load_builtin_methods()
    return _CAPABILITIES.get(name or "", MethodCapabilities())


def registered_methods() -> list[str]:
    """Every registered creation method, sorted (the built-ins are loaded first)."""
    _load_builtin_methods()
    return sorted(_CAPABILITIES)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _log_line(job: Job, stage: str, message: str, level: str = "info") -> None:
    """Append one JSONL line to ``job.log`` WITHOUT committing — for a job's final line, which
    must ride in the same commit as its terminal status (N17-obs479: a poller that stops at
    the terminal status must already see it, and nothing writes the job after it is
    terminal)."""
    line = json.dumps(
        {"ts": _now_iso(), "stage": stage, "level": level, "msg": message}, ensure_ascii=False
    )
    job.log = (job.log + "\n" + line) if job.log else line


def _append_log(db: Session, job_id: str, stage: str, message: str, level: str = "info") -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    _log_line(job, stage, message, level)
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
            if status in ("succeeded", "skipped", "failed", CANCELLED, INTERRUPTED):
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
    actor: Creator | None = None,
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

    Every call also writes the agent's next spec snapshot (`services/snapshots`,
    upstream #277) in the same transaction: ``actor`` is who published (default: the
    agent's creator) and ``note`` a free label. A resumed job never comes back through
    here, so it never records a second one."""
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
            # who published and why (display; the snapshot carries the same)
            "actor_name": actor.name if actor else None,
            "note": note,
            **(payload_extra or {}),
        },
    )
    db.add(job)
    db.flush()
    deployment.job_id = job.id
    snapshots.record_snapshot(db, agent, deployment_id=deployment.id, actor=actor, note=note)
    if commit:
        db.commit()
    else:
        db.flush()
    return deployment, job


# Jobs the startup resume is about to adopt: the only launches allowed to take a job a
# dead process left `running`. Filled by `resume_pending_jobs` before it starts them.
_RESUMING: set[str] = set()


def execute_deploy_job(job_id: str, *, resume: bool | None = None) -> None:
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

        done = {s["name"] for s in deployment.stages if s["status"] in ("succeeded", "skipped")}
        # N17-obs13: the job's own spec — carried from here on; a job that predates the key
        # (resumed across the upgrade) starts from the row as the old finalizer would have
        packaged = (job.payload or {}).get(PACKAGED_SPEC_KEY)
        if packaged is None:
            packaged = copy.deepcopy(agent.spec or {})
            _carry_packaged_spec(db, job, packaged)
        # N16-obs83: the last known shape of the agent row, for the teardown a cancel runs
        # when the row itself is gone (the raw-DB race) — refreshed after every stage.
        snapshot = _agent_snapshot(db, agent_id)
        for stage_name in STAGE_ORDER:
            if stage_name in done:
                continue
            # N16-obs83: every stage re-reads the agent row before acting — a delete that
            # landed between two stages (an older client, a race with the 409 guard) ends
            # the job as `cancelled` instead of creating the next resource for a row
            # nobody owns any more.
            if agent_gone(db, agent_id):
                _cancel(db, ctx, job, deployment_id, agent_id, stage_name, snapshot)
                return

            def log(msg: str, _stage=stage_name) -> None:
                _append_log(db, job_id, _stage, msg)

            ctx.log = log
            _set_stage(db, deployment_id, stage_name, "running")
            _append_log(db, job_id, stage_name, "stage started")
            fn = stages.get(stage_name)
            try:
                db.refresh(agent)
                spec_before = copy.deepcopy(agent.spec or {})
                if stage_name == "generate":
                    _record_catalog(db, deployment, agent)
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
            except StageInterrupted as exc:
                # N17-obs113: the stage's subprocess was killed by a signal. The stage is
                # `interrupted` and the Job stays `running` — for the startup resume when
                # the service is stopping (`resume_pending_jobs` picks `queued` / `running`
                # and `done` only skips succeeded / skipped stages); otherwise for ONE
                # in-process retry after a fixed delay. A second kill ends the job failed
                # with the signal sentence: a job never waits for a restart indefinitely.
                detail = str(exc)
                logger.warning(
                    "deploy job %s: agent %s interrupted at stage %s: %s",
                    job_id, agent_id, stage_name, detail,
                )
                if shutting_down():
                    _set_stage(db, deployment_id, stage_name, INTERRUPTED, detail)
                    _append_log(db, job_id, stage_name, detail, level="warning")
                    _append_log(db, job_id, "job", STAGE_INTERRUPTED, level="warning")
                    return
                retries = dict((job.payload or {}).get(INTERRUPTED_RETRIES_KEY) or {})
                if retries.get(stage_name, 0) >= INTERRUPTED_RETRY_LIMIT:
                    failure = exc.failure
                    _set_stage(db, deployment_id, stage_name, "failed", failure)
                    _append_log(db, job_id, stage_name, failure, level="error")
                    _append_log(db, job_id, "job", STAGE_INTERRUPTED_TWICE, level="error")
                    _finish(db, job_id, deployment_id, agent_id, error=failure)
                    return
                retries[stage_name] = retries.get(stage_name, 0) + 1
                job.payload = {**(job.payload or {}), INTERRUPTED_RETRIES_KEY: retries}
                _set_stage(db, deployment_id, stage_name, INTERRUPTED, detail)  # commits
                _append_log(db, job_id, stage_name, detail, level="warning")
                _append_log(
                    db, job_id, "job",
                    STAGE_INTERRUPTED_RETRY.format(delay=INTERRUPTED_RETRY_DELAY_SECONDS),
                    level="warning",
                )
                schedule_later(
                    INTERRUPTED_RETRY_DELAY_SECONDS, lambda: _retry_interrupted(job_id)
                )
                return
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                if agent_gone(db, agent_id):
                    # The row went away under the stage (its refresh / re-read raised, or
                    # a stage wrote onto a vanished row): the exception is the symptom,
                    # the delete is the truth — the job is cancelled, not failed.
                    _append_log(db, job_id, stage_name, detail, level="warning")
                    _cancel(db, ctx, job, deployment_id, agent_id, stage_name, snapshot)
                    return
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
            rewritten = _apply_stage_rewrite(db, agent, spec_before, packaged)
            if rewritten is not None:
                packaged = rewritten
                _carry_packaged_spec(db, job, packaged)
            snapshot = _agent_snapshot(db, agent_id) or snapshot

        if agent_gone(db, agent_id):
            # deleted during the last stage: `_finish` must never flip a deleted row back
            # to `active` (that is how the live orphan read as a succeeded job)
            _cancel(db, ctx, job, deployment_id, agent_id, None, snapshot)
            return
        _finish(db, job_id, deployment_id, agent_id, error=None)
    except Exception as exc:  # job-level failure — never crash the worker
        # The session may sit in a failed transaction; clear it before touching rows.
        db.rollback()
        _fail_job(db, job_id, exc)
    finally:
        db.close()


def agent_gone(db: Session, agent_id: str) -> bool:
    """True when the agent row is missing or marked ``deleted`` — read with a fresh
    column SELECT, never from the session's identity map, so a delete another request
    (or process) committed is seen the moment the next stage asks."""
    status = db.execute(select(Agent.status).where(Agent.id == agent_id)).scalar_one_or_none()
    return status is None or status == "deleted"


def _agent_marked_deleted(db: Session, agent_id: str) -> bool:
    """True only when the row exists and carries ``status = deleted`` (fresh SELECT)."""
    status = db.execute(select(Agent.status).where(Agent.id == agent_id)).scalar_one_or_none()
    return status == "deleted"


def _agent_snapshot(db: Session, agent_id: str) -> Agent | None:
    """A transient copy of the agent row (never attached to a session) — what the
    cancel teardown names its resources from once the row itself is gone."""
    mapper = sa_inspect(Agent)
    row = db.execute(
        select(*[prop.columns[0] for prop in mapper.column_attrs]).where(Agent.id == agent_id)
    ).first()
    if row is None:
        return None
    snap = Agent()
    for prop, value in zip(mapper.column_attrs, row, strict=True):
        setattr(snap, prop.key, value)
    return snap


def in_flight_deploy_job(db: Session, agent_id: str) -> tuple[Job, Deployment, str] | None:
    """The agent's live deploy job (``queued`` / ``running``), its Deployment and the stage
    it is at (the running stage, else the first pending one) — ``None`` when every job of
    the agent is terminal. N16-obs83: what ``DELETE /api/agents/{id}`` refuses on."""
    rows = (
        db.query(Job, Deployment)
        .join(Deployment, Deployment.job_id == Job.id)
        .filter(Deployment.agent_id == agent_id, Job.status.in_(LIVE_JOB_STATUSES))
        .order_by(Job.created_at.desc())
        .all()
    )
    if not rows:
        return None
    job, deployment = rows[0]
    stages = deployment.stages or []
    stage = next((s["name"] for s in stages if s.get("status") == "running"), None) or next(
        (s["name"] for s in stages if s.get("status") == "pending"), STAGE_ORDER[0]
    )
    return job, deployment, stage


def refuse_deploy_in_progress(db: Session, agent_id: str, *, then: str) -> None:
    """agents.md §1: a deploy job owns its agent until it is terminal. 409
    ``agent.deploy_in_progress {job_id, stage, job_status}`` while the agent has a
    ``queued`` / ``running`` job — nothing touched. The one envelope every publisher of the
    agent's live version answers: ``DELETE /api/agents/{id}`` (N16-obs83) and, N17-obs13,
    the Runtime canary's ``complete`` / ``rollback`` (§1.2). ``then`` finishes the
    message ("delete the agent", "complete the canary")."""
    busy = in_flight_deploy_job(db, agent_id)
    if busy is None:
        return
    job, _deployment, stage = busy
    raise AppError(
        "agent.deploy_in_progress",
        f"a deployment of this agent is in progress (job {job.id}, stage {stage}) — wait for "
        f"it to finish or fail, then {then}",
        {"job_id": job.id, "stage": stage, "job_status": job.status},
        status_code=409,
    )


#: `Job.payload` key: the spec THIS job packaged — the row's spec when the job started (or
#: resumed) plus every rewrite one of its own stages made (BYOC `generate` stamps provenance,
#: a harness deploy pins skills). `_finish` stamps THAT onto the job's snapshot (N17-obs13
#: belt), never the agent row's current spec.
PACKAGED_SPEC_KEY = "packaged_spec"


def _carry_packaged_spec(db: Session, job: Job, spec: dict[str, Any]) -> None:
    """Persist the job's own spec onto its row (committed), so a resumed job and
    ``_finish`` read what was packaged — not what another writer put on the agent row."""
    job.payload = {**(job.payload or {}), PACKAGED_SPEC_KEY: copy.deepcopy(spec)}
    db.commit()


def _apply_stage_rewrite(
    db: Session, agent: Agent, before: dict[str, Any], packaged: dict[str, Any],
) -> dict[str, Any] | None:
    """The spec keys the stage that just ran changed, applied onto the job's own spec —
    or ``None`` when it changed nothing (or the row is gone: the loop's ``agent_gone``
    check cancels the job next). The row is compared right before and right after the
    stage call (stages write through ``ctx.session()`` and commit), so the attribution
    window is the stage itself: a writer that lands BETWEEN two stages (the canary promote
    before its 409 guard) changes the next stage's ``before`` — never a delta — and is
    never carried, even when a later stage rewrites other keys on top of it."""
    if agent_gone(db, agent.id):
        return None
    db.refresh(agent)
    after = agent.spec or {}
    if after == before:
        return None
    changed = {k: copy.deepcopy(v) for k, v in after.items() if k not in before or before[k] != v}
    removed = {k for k in before if k not in after}
    return {**{k: v for k, v in packaged.items() if k not in removed}, **changed}


def _cancel(
    db: Session,
    ctx: StageContext,
    job: Job,
    deployment_id: str,
    agent_id: str,
    stage_name: str | None,
    snapshot: Agent | None,
) -> None:
    """N16-obs83: end the job ``cancelled`` because its agent row is gone / deleted.

    ``stage_name`` is the stage that did NOT run (``None`` = every stage had finished);
    it is marked ``cancelled`` with the reason so the Deployment reads honestly. The
    agent row, if it still exists, is left exactly as the delete put it (``deleted``) —
    never flipped back to ``active`` or ``failed``. Then the job tears down what IT
    created (``_teardown_after_cancel``) and records the report on its payload.
    """
    job_id = job.id
    where = stage_name or "job"
    logger.warning("deploy job %s: agent %s deleted mid-deploy — cancelled at %s",
                   job_id, agent_id, where)
    _append_log(db, job_id, where, CANCEL_REASON, level="warning")
    deployment = db.get(Deployment, deployment_id)
    # which stages ran — read BEFORE the stopped stage is marked, so a stage that was
    # `running` when the row vanished (it may have created something) counts as ran
    ran = {
        s["name"] for s in ((deployment.stages if deployment else []) or [])
        if s.get("status") not in (None, "pending")
    }
    if stage_name is not None:
        _set_stage(db, deployment_id, stage_name, CANCELLED, "stopped — the agent was deleted")
    steps = _teardown_after_cancel(db, ctx, job, ran, snapshot)
    job = db.get(Job, job_id)
    job.status = CANCELLED
    job.error = CANCEL_REASON
    job.payload = {
        **(job.payload or {}),
        "cancelled": {"stage": stage_name, "reason": "agent_deleted",
                      "teardown": [teardown.step_dict(s) for s in steps]},
    }
    if deployment is not None:
        deployment.status = CANCELLED
        deployment.ended_at = datetime.now(UTC)
    db.commit()


def _teardown_after_cancel(
    db: Session, ctx: StageContext, job: Job, ran: set[str], snapshot: Agent | None,
) -> list[teardown.TeardownStep]:
    """What a cancelled job leaves behind, torn down and reported.

    ``ran`` = the stages that started (finished or not — a stage interrupted mid-way may
    have created its resource). A ``create``-mode job owns everything those stages made:
    the Runtime (deploy stage reached — ``resource_id`` on the row), then the residue
    (role + inline policies, S3 artifact prefix, memory snapshots / records, registry
    record, log groups — the
    delete route's own ``teardown.cleanup_residue``, best-effort and idempotent). An
    ``update``-mode job created a new VERSION on the deleted agent's existing runtime and
    nothing of its own: those resources belong to the agent's delete, which already ran
    (or, in the raw race, to the operator), so nothing is touched here and the log says so.
    Nothing ran past ``generate`` → nothing exists → nothing to tear down.
    """
    log = lambda msg: _append_log(db, job.id, "teardown", msg)  # noqa: E731
    if snapshot is None:
        log("no snapshot of the agent row — nothing can be named for teardown")
        return []
    if (job.payload or {}).get("mode", "create") != "create":
        log("update-mode job: the new version rode the agent's existing runtime; its "
            "resources belong to the agent's delete — nothing torn down by the job")
        if "register" not in ran:
            return []
        # ...except a registry record the re-register may have CREATED after the delete
        # route removed the agent's (the row keeps the old id; the backend recreates)
        try:
            step = teardown._step_registry_record(snapshot, ctx.workspace, log)
        except Exception as exc:  # noqa: BLE001 — best-effort by contract
            step = teardown.TeardownStep(teardown.STEP_REGISTRY, teardown.FAILED,
                                         teardown.short_reason(exc),
                                         reason=teardown.short_reason(exc))
        log(f"registry record after a cancelled re-register: {step.status} · {step.detail}")
        return [step]
    if not (ran & {"package", "provision", "deploy", "register"}):
        log("no stage past generate ran — no AWS resource was created, nothing to tear down")
        return []
    steps: list[teardown.TeardownStep] = []
    if "deploy" in ran and snapshot.resource_id:
        # the Runtime this job created (CreateAgentRuntime accepted) — the hard step first,
        # so the role is only removed once nothing references it
        from app.routers.agents import delete_agent_cloud_resource

        try:
            delete_agent_cloud_resource(snapshot, ctx.workspace)
        except Exception as exc:  # noqa: BLE001 — reported, never re-raised
            steps.append(teardown.TeardownStep(
                teardown.STEP_RUNTIME, teardown.FAILED, teardown.short_reason(exc),
                reason=teardown.short_reason(exc),
            ))
            log(f"runtime {snapshot.resource_id} could not be deleted: "
                f"{teardown.short_reason(exc)}")
        else:
            steps.append(teardown.TeardownStep(
                teardown.STEP_RUNTIME, teardown.DELETED, snapshot.resource_id))
            log(f"deleted runtime {snapshot.resource_id} the cancelled job had created")
    from app.core.config import get_settings

    steps.extend(teardown.cleanup_residue(
        snapshot, ctx.workspace, settings=get_settings(), log=log,
    ))
    return steps


def _close_cancelled(
    db: Session, job: Job | None, deployment: Deployment | None, where: str,
    *, symptom: str | None = None,
) -> None:
    """The finalizers' refusal (N16-obs83): close the Job / Deployment as ``cancelled``
    without touching the agent row. ``symptom`` is the error the finalizer was about to
    record — kept in the log so nothing is hidden, while the status tells the truth."""
    if job is None:
        return
    job.status = CANCELLED
    job.error = CANCEL_REASON
    job.payload = {
        **(job.payload or {}),
        "cancelled": {**((job.payload or {}).get("cancelled") or {}),
                      "reason": "agent_deleted", "finalizer_refused": True},
    }
    if deployment is not None:
        deployment.status = CANCELLED
        deployment.ended_at = datetime.now(UTC)
    # N17-obs479: the log lines ride in the terminal commit
    if symptom:
        _log_line(job, where, symptom, level="warning")
    _log_line(job, where, CANCEL_REASON + " — the finalizer refused to write onto the deleted row",
              level="warning")
    db.commit()


def _record_catalog(db: Session, deployment: Deployment, agent: Agent) -> None:
    """D21 (M1-d): pin the published model catalog the `generate` stage renders from onto
    the Deployment row — its version and the resolved descriptor of the spec's model — for
    EVERY method (BYOC has no generate callable but reads the same snapshot through its env).
    Resumed jobs re-run `generate`, so the pin follows the render that actually shipped."""
    from app.schemas.agent import catalog_version, model_descriptor

    model_id = str((agent.spec or {}).get("model_id") or "")
    deployment.catalog_version = int(catalog_version())
    deployment.model_descriptor = model_descriptor(model_id) if model_id else None
    db.commit()


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
    deployment = db.get(Deployment, deployment_id) if deployment_id else None
    if agent_id and _agent_marked_deleted(db, agent_id):
        # N16-obs83: the row was deleted by the member — writing `failed` onto it would
        # un-delete it. The job is cancelled, the row untouched, the error still logged.
        # (A row that is physically MISSING keeps the historical `failed`: there is no
        # row to resurrect, and the half-deleted-ledger contract below still holds.)
        _close_cancelled(db, job, deployment, "job", symptom=detail)
        return
    job.status = "failed"
    job.error = detail
    if deployment is not None:
        deployment.status = "failed"
        deployment.ended_at = now
    agent = db.get(Agent, agent_id) if agent_id else None
    keeps_live = agent is not None and _keeps_live_version(db, job, agent, deployment)
    if agent is not None:
        agent.status = "active" if keeps_live else "failed"
        agent.error = detail
    # N17-obs479: the failure's log lines ride in the terminal commit
    _log_line(job, "job", detail, level="error")
    _log_line(job, "job", traceback.format_exc(limit=3), level="debug")
    if keeps_live:
        _log_line(job, "job", LIVE_VERSION_KEPT)
    db.commit()


# N10v3-bug7 (= N10v2-bug9): a redeploy that fails in package/provision used to flip a
# SERVING agent to `failed`, Chat answered 409 agent.not_active and the console hid Chat —
# although the live version was untouched and READY. The status model is now: an agent
# with a live deployed version stays `active` when a later deployment fails; the failure
# is on the Deployment/Job and on `Agent.error` (the read model's `last_deploy_error`).
# A first deploy that fails still yields `failed`.
LIVE_VERSION_KEPT = (
    "the live version is untouched (the deploy stage did not run, or had finished) and "
    "keeps serving — the agent stays active; this failure is recorded as its last deploy "
    "error"
)


# Deploy-stage states under which the live version is provably untouched: the stage
# never ran (the failure was in generate / package / provision) or it finished (the
# failure was in register, after the new version was READY). A FAILED or RUNNING deploy
# stage means UpdateAgentRuntime may have been accepted — zip_runtime writes the new
# `agentRuntimeVersion` onto the row before `wait_runtime_ready` — so the row's version
# may name a version that never became READY; that agent is `failed`, not active.
_LIVE_SAFE_DEPLOY_STATES = frozenset({"pending", "succeeded", "skipped"})


def _deploy_stage_state(deployment: Deployment) -> str | None:
    return next(
        (s.get("status") for s in (deployment.stages or []) if s.get("name") == "deploy"),
        None,
    )


def _keeps_live_version(
    db: Session, job: Job, agent: Agent, deployment: Deployment | None
) -> bool:
    """True when this failed job was an UPDATE of an agent that already serves a
    version AND the failure left that version untouched: the row has a Runtime (ARN),
    the agent has a VERIFIED live version, and this deployment's ``deploy`` stage is
    still pending (or already succeeded — a ``register`` failure). A create-mode job
    (first deploy, or a re-create after the resource vanished) never qualifies —
    nothing is serving then; nor does a failure inside the ``deploy`` stage itself.

    N17-obs100: "verified live version" is read from the agent's deployment history,
    newest first, skipping this job's own deployment: the newest earlier deployment
    that SUCCEEDED proves a version is live — unless a deployment between it and now
    failed inside (or was cut inside) its ``deploy`` stage. That failure made the live
    state unknown (agents.md §4.3: UpdateAgentRuntime may have been accepted) and the
    agent ``failed``; a re-publish that then fails BEFORE its own ``deploy`` stage has
    verified nothing, so the agent stays ``failed`` (§4.2) instead of reading ``active``
    on a runtime nobody confirmed. Only a later SUCCEEDED publish re-verifies it."""
    if (job.payload or {}).get("mode") != "update" or not agent.arn:
        return False
    if deployment is None:
        return False
    if _deploy_stage_state(deployment) not in _LIVE_SAFE_DEPLOY_STATES:
        return False
    return _has_verified_live_version(db, agent.id, exclude=deployment.id)


def _has_verified_live_version(db: Session, agent_id: str, *, exclude: str) -> bool:
    """Walk the agent's other deployments newest-first: the first one that succeeded
    says a version is live; an earlier-met one whose ``deploy`` stage is not in a
    live-safe state says the live state is unknown (nothing verified since)."""
    history = (
        db.query(Deployment)
        .filter(Deployment.agent_id == agent_id, Deployment.id != exclude)
        .order_by(Deployment.started_at.desc())
        .all()
    )
    for earlier in history:
        if earlier.status == "succeeded":
            return True
        if _deploy_stage_state(earlier) not in _LIVE_SAFE_DEPLOY_STATES:
            return False
    return False


def _finish(
    db: Session, job_id: str, deployment_id: str, agent_id: str, error: str | None
) -> None:
    job = db.get(Job, job_id)
    deployment = db.get(Deployment, deployment_id)
    if agent_gone(db, agent_id):
        # N16-obs83 (live, t1, 13:30Z): this finalizer wrote `active` + the deployed
        # snapshot onto a row the member had deleted a minute earlier — the agent came
        # back to the list pointing at resources the console could later not delete.
        # The row is re-read HERE, in the finalizer's own transaction: gone / deleted →
        # nothing is written to it, the snapshot is not stamped, the job is `cancelled`.
        _close_cancelled(db, job, deployment, "job", symptom=error)
        return
    agent = db.get(Agent, agent_id)
    now = datetime.now(UTC)
    if error is None:
        job.status = "succeeded"
        deployment.status = "succeeded"
        agent.status = "active"
        agent.error = None
        agent.last_strip = None  # N17-obs31: the re-publish dropped the stripped KB's tool
        # the version this publish left live, and the spec as deployed — the one the
        # job's own stages packaged (`generate` may rewrite it), carried on the Job row,
        # NEVER the agent row's current spec: N17-obs13 — a concurrent publish between
        # `package` and here (a canary promote, before its 409 guard) must not be
        # recorded as what THIS job deployed. A job that predates the key (resumed
        # across the upgrade) falls back to the row, as before.
        snapshots.stamp_deployed(
            db, deployment_id, agent, spec=(job.payload or {}).get(PACKAGED_SPEC_KEY),
        )
    else:
        job.status = "failed"
        job.error = error
        deployment.status = "failed"
        keeps_live = _keeps_live_version(db, job, agent, deployment)
        agent.status = "active" if keeps_live else "failed"
        agent.error = error
        if keeps_live:
            _append_log(db, job_id, "job", LIVE_VERSION_KEPT)
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
    from app.system_agents import uninstall as system_uninstall

    starters: dict[str, Callable[[str], threading.Thread]] = {
        "deploy_agent": start_deploy_async,
        workspace_bootstrap.JOB_TYPE: workspace_bootstrap.start_bootstrap_async,
        # a crashed uninstall is still `running` in the ledger; the resume starter
        # is the only caller allowed to pick such a job up again
        system_uninstall.JOB_TYPE: system_uninstall.start_uninstall_resume,
        # a log-group settle (N-SCN-obs8) a restart cut short re-runs its recorded schedule
        # against its recorded prefix; the agent row may be gone, the payload suffices
        teardown.SETTLE_JOB_TYPE: teardown.start_settle_resume,
        # a KB storage cleanup (WB-008) a restart cut short re-runs its recorded steps
        # against its recorded bucket/indices; the KB row is gone, the payload suffices
        kb_cleanup.JOB_TYPE: kb_cleanup.start_cleanup_resume,
        # an architect-conversation CLEAR resumes from its verified checkpoints
        assistant_purge.JOB_TYPE: assistant_purge.start_purge_resume,
    }
    db = SessionLocal()
    try:
        # rev-r17 nit: a validate-only bootstrap probe a restart cut short is failed (not
        # re-run) and its row handed back; a row stuck `bootstrapping` with no live job is
        # restored — BEFORE the resumable jobs are collected, so neither is started below
        swept = workspace_bootstrap.sweep_interrupted(db)
        if swept["probes_failed"] or swept["rows_restored"]:
            logging.getLogger("launchpad").warning(
                "workspace bootstrap sweep: %d interrupted probe(s) failed, %d stuck row(s) "
                "restored", len(swept["probes_failed"]), len(swept["rows_restored"]),
            )
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
