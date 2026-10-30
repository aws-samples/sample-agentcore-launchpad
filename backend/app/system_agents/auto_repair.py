"""Auto-repair sweep for installed system presets (N17-obs206, user ruling 2026-10-22).

Why. Bootstrap step 10 repairs the evaluation worker of the *default* workspace at every
host update, but a preset installed in any OTHER workspace, and the optional architect
preset everywhere, were never touched: every catalog-shaping registry write (a provider
created or deleted, N17-obs203 / obs234) and every host update that ships a newer release
left them stale until an administrator clicked 修复快照 per preset per workspace. This
module makes that repair automatic — the user's ask "随重新部署自动更新".

What a sweep does. ``plan(db)`` walks every workspace row and every catalogue preset and
returns the **candidates** — the installed, active presets whose last deploy did not fail —
plus a skip row for every other pair. Nothing is claimed in the plan. The staleness decision
is NOT re-implemented here: when a candidate's turn comes, ``claim(db, candidate)`` calls
the existing ``install_preset(db, row, preset, edit=None, force=False, trigger="auto")``
and ``service._repair`` is the single funnel — it re-publishes when the serving snapshot
pinned an older model catalog than the ledger's (``catalog_behind``, read off the newest
NON-failed deployment, N17-obs259) or when the spec it re-derives from this build differs
from the stored one (a newer release, an added spec member), and answers
``changed=False`` for a current preset (``skipped: current``). Its durable claim (status +
row version CAS in the job's transaction) is what guarantees ONE winner between the sweep, a
card click and bootstrap step 10 — whoever loses adopts the winner's job (``skipped:
in_flight``). Presets that are deploying / uninstalling (a job owns the row), not installed,
or whose newest deployment or job **failed** (N17-obs259: the row may still read ``active``
because the live version survived; the failure is an administrator's to look at — the sweep
never retries it) are pre-filtered so the sweep never contends for a claim it cannot win and
never masks a failure as "current".

How the jobs run — claim lazily, launch serially. The scheduler drains its FIFO of
candidates one at a time: claim THIS candidate (one ``queued`` job row), launch it
(``start_deploy_async``), ``join`` the worker (``JOB_CAP_S`` = 30 min), then the next. At
most ONE auto-repair job is therefore non-terminal at any time (N17-obs260): a host update or
crash mid-drain leaves at most that one job for ``resume_pending_jobs`` to resume, and the
remaining candidates are simply re-planned by the next startup sweep — never N-1 parallel
billable deploys and never a ledger full of queued jobs holding the deploy gate closed. A
job still running past the cap is no longer waited for (the queue moves on; the job itself
keeps running to its own end). The job payload carries ``repair_trigger: auto`` and
``repair_reason: "auto: catalog snapshot behind v10 < v20"`` (``"auto: release update"`` for
a spec that re-derives differently), so the card reads 自动修复中.

Triggers — exactly two. (a) startup: ``schedule_startup()`` after ``resume_pending_jobs()``
schedules a sweep ``Settings.system_presets_auto_repair_start_delay_s`` (60 s) later — a
newer release arrives with a host update, so this is where "随重新部署自动更新" lands;
(b) every journal row that moves the catalog version, plus every model-role write
(``model_registry.add_catalog_change_listener`` / ``arms_auto_repair``), schedules a
DEBOUNCED sweep ``system_presets_auto_repair_debounce_s`` (180 s) after the LAST write, so a
burst of provider edits coalesces into one sweep; a burst that never pauses is still swept
``DEBOUNCE_MAX_WAIT_S`` (15 min) after its first write. ``Settings.system_presets_auto_repair
= false`` turns both off.

Hermetic by construction. ``AutoRepairScheduler`` is a small state machine with an
injectable clock, planner, claimer and launcher; ``run_due()`` is the one step the thread
loop repeats, so tests drive it directly with a fake clock and never sleep. The suite sets
``LAUNCHPAD_SYSTEM_PRESETS_AUTO_REPAIR=0`` so no app built in a test schedules anything.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.ledger import Workspace
from app.system_agents import presets as catalogue
from app.system_agents import service

logger = logging.getLogger("launchpad.system_agents.auto_repair")

TRIGGER_AUTO = "auto"
JOB_CAP_S = 30 * 60  # one deploy job may hold the serial queue at most this long
DEBOUNCE_MAX_WAIT_S = 15 * 60  # a never-pausing burst of writes is still swept after this
HISTORY_KEEP = 20

# ---------------------------------------------------------------------------
# candidates + outcomes
# ---------------------------------------------------------------------------

LAUNCHED = "launched"  # a repair job was claimed and handed to the launcher
SKIPPED = "skipped"  # nothing to do, or someone else owns the row — `reason` says which

R_CURRENT = "current"  # `_repair` answered changed=False: the preset is current
R_NOT_INSTALLED = "not_installed"
R_DEPLOYING = "deploying"
R_UNINSTALLING = "uninstalling"
R_FAILED = "failed"  # the row, its newest deployment or its newest job failed
R_IN_FLIGHT = "in_flight"  # claim lost to a concurrent repair (card click / bootstrap)
R_REFUSED = "refused"  # the install path answered a structured refusal (`code` says why)
R_ERROR = "error"


@dataclass(frozen=True)
class Candidate:
    """One installed, active preset the planner found eligible; claimed only at its turn."""

    workspace_id: str
    preset_key: str
    agent_id: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.workspace_id, self.preset_key)


@dataclass(frozen=True)
class Outcome:
    workspace_id: str
    preset_key: str
    agent_id: str | None
    action: str  # LAUNCHED | SKIPPED
    reason: str  # LAUNCHED: `_repair`'s repair_reason; SKIPPED: the R_* vocabulary
    job_id: str | None = None
    code: str | None = None  # the AppError code behind R_REFUSED
    message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# plan (ledger only, no claim) and claim (one candidate, one job at most)
# ---------------------------------------------------------------------------


def _skip(workspace_id: str, preset_key: str, agent_id: str | None, reason: str,
          **extra: Any) -> Outcome:
    return Outcome(workspace_id=workspace_id, preset_key=preset_key, agent_id=agent_id,
                   action=SKIPPED, reason=reason, **extra)


def _prefilter(db: Session, row: Workspace, preset: catalogue.SystemPreset
               ) -> Candidate | Outcome:
    """The candidate, or the skip outcome for a preset the sweep must not contend for."""
    agent = service.find_installed(db, row.id, preset)
    if agent is None:
        return _skip(row.id, preset.key, None, R_NOT_INSTALLED)
    if agent.status == service.STATUS_DEPLOYING:
        _deployment, job = service._latest_job(db, agent)
        return _skip(row.id, preset.key, agent.id, R_DEPLOYING, job_id=job.id if job else None)
    if agent.status == service.STATUS_UNINSTALLING:
        return _skip(row.id, preset.key, agent.id, R_UNINSTALLING)
    if agent.status != service.STATUS_ACTIVE:
        return _skip(row.id, preset.key, agent.id, R_FAILED)
    deployment, job = service._latest_job(db, agent)
    if (deployment is not None and deployment.status == "failed") or (
            job is not None and job.status == "failed"):
        # N17-obs259: the row reads `active` because the live version survived the failed
        # re-publish — an administrator's to look at; an automatic retry would both hide
        # the failure and loop on a deterministic one
        return _skip(row.id, preset.key, agent.id, R_FAILED,
                     job_id=job.id if job else None, message=agent.error)
    return Candidate(workspace_id=row.id, preset_key=preset.key, agent_id=agent.id)


def plan(db: Session) -> tuple[list[Candidate], list[Outcome]]:
    """Every (workspace, preset) pair, split into candidates (installed, active, last deploy
    not failed — claimed later, one at a time) and skip outcomes. Ledger only, no claim."""
    candidates: list[Candidate] = []
    skipped: list[Outcome] = []
    for row in db.query(Workspace).order_by(Workspace.id).all():
        for preset in catalogue.PRESETS.values():
            verdict = _prefilter(db, row, preset)
            (candidates if isinstance(verdict, Candidate) else skipped).append(verdict)
    return candidates, skipped


def claim(db: Session, candidate: Candidate, *, trigger: str = TRIGGER_AUTO) -> Outcome:
    """Offer ONE candidate to ``install_preset`` right before it is launched. The row is
    re-read and pre-filtered again (it may have moved since the plan); `_repair` decides
    staleness and owns the claim. Fail-soft: a refusal or an error is a skip outcome."""
    row = db.get(Workspace, candidate.workspace_id)
    preset = catalogue.get_preset(candidate.preset_key)
    if row is None or preset is None:
        return _skip(candidate.workspace_id, candidate.preset_key, candidate.agent_id,
                     R_NOT_INSTALLED)
    verdict = _prefilter(db, row, preset)
    if isinstance(verdict, Outcome):
        return verdict
    try:
        outcome = service.install_preset(db, row, preset, None, force=False, trigger=trigger)
        db.commit()
    except AppError as exc:
        db.rollback()
        return _skip(row.id, preset.key, candidate.agent_id, R_REFUSED, code=exc.code,
                     message=str(exc.message))
    except Exception as exc:  # noqa: BLE001 — one preset never fails the sweep
        db.rollback()
        logger.exception("auto-repair: %s in workspace %s failed", preset.key, row.id)
        return _skip(row.id, preset.key, candidate.agent_id, R_ERROR, message=str(exc))
    agent_id = outcome.agent.id
    if outcome.job is not None and outcome.changed:
        why = str((outcome.job.payload or {}).get("repair_reason") or "")
        logger.info("auto-repair: %s in workspace %s → job %s (%s)",
                    preset.key, row.id, outcome.job.id, why)
        return Outcome(workspace_id=row.id, preset_key=preset.key, agent_id=agent_id,
                       action=LAUNCHED, reason=why, job_id=outcome.job.id)
    if outcome.agent.status == service.STATUS_DEPLOYING:
        # the claim went to a concurrent repair (a card click, bootstrap step 10): its job
        # is the one that will converge the row
        return _skip(row.id, preset.key, agent_id, R_IN_FLIGHT,
                     job_id=outcome.job.id if outcome.job else None)
    return _skip(row.id, preset.key, agent_id, R_CURRENT)


def _plan_with_session() -> tuple[list[Candidate], list[Outcome]]:
    db = SessionLocal()
    try:
        return plan(db)
    finally:
        db.close()


def _claim_with_session(candidate: Candidate) -> Outcome:
    db = SessionLocal()
    try:
        return claim(db, candidate)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# scheduler: debounced sweeps + the serial claim-and-launch queue, on one thread
# ---------------------------------------------------------------------------


def _default_launch(job_id: str) -> Any:
    # resolved lazily: the pipeline imports the system-agents service (cycle at import)
    from app.deployer.pipeline import start_deploy_async

    return start_deploy_async(job_id)


class AutoRepairScheduler:
    """One host-wide owner of (1) the pending debounced sweep and (2) the FIFO of
    candidates, each claimed and launched in turn.

    ``schedule(delay_s, reason)`` moves the single deadline to ``now + delay`` (trailing-edge
    debounce: a burst of writes yields one sweep ``delay`` after the LAST one, but never
    later than ``DEBOUNCE_MAX_WAIT_S`` after the FIRST) and wakes the thread;
    ``enqueue(candidates)`` appends to the FIFO (deduplicated) and wakes it. ``run_due(now)``
    is the thread's one step — a due deadline runs the plan and queues its candidates, then
    the queue is drained: ``claim(candidate)``; if a job was claimed, ``launch(job)`` and
    ``join`` the returned worker (``JOB_CAP_S``); next. Tests call ``run_due`` with a fake
    clock and a fake launcher; nothing sleeps.
    """

    def __init__(
        self,
        *,
        plan_fn: Callable[[], tuple[list[Candidate], list[Outcome]]] = _plan_with_session,
        claim_fn: Callable[[Candidate], Outcome] = _claim_with_session,
        launch: Callable[[str], Any] = _default_launch,
        clock: Callable[[], float] = time.monotonic,
        job_cap_s: float = JOB_CAP_S,
        max_wait_s: float = DEBOUNCE_MAX_WAIT_S,
        start_thread: bool = True,
    ) -> None:
        self._plan_fn = plan_fn
        self._claim_fn = claim_fn
        self._launch = launch
        self._clock = clock
        self._job_cap_s = job_cap_s
        self._max_wait_s = max_wait_s
        self._start_thread = start_thread
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._deadline: float | None = None
        self._first_armed: float | None = None
        self._reasons: list[str] = []
        self._queue: deque[Candidate] = deque()
        self._running: Candidate | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_KEEP)
        self.sweeps = 0
        self.launches: deque[str] = deque(maxlen=HISTORY_KEEP * 10)

    # ---- inputs -------------------------------------------------------------------

    def schedule(self, delay_s: float, reason: str) -> float:
        """Debounce: (re)arm the one deadline at ``now + delay_s``, capped at
        ``first arming + max_wait_s``; returns it."""
        with self._lock:
            now = self._clock()
            if self._first_armed is None:
                self._first_armed = now
            self._deadline = min(now + max(0.0, float(delay_s)),
                                 self._first_armed + self._max_wait_s)
            self._reasons.append(reason)
            deadline = self._deadline
        self._ensure_thread()
        self._wake.set()
        return deadline

    def enqueue(self, candidates: list[Candidate]) -> None:
        with self._lock:
            queued = {c.key for c in self._queue}
            running = self._running.key if self._running else None
            for candidate in candidates:
                if candidate.key not in queued and candidate.key != running:
                    self._queue.append(candidate)
                    queued.add(candidate.key)
        if candidates:
            self._ensure_thread()
            self._wake.set()

    # ---- state ----------------------------------------------------------------------

    def pending(self) -> dict[str, Any]:
        with self._lock:
            due_in = None if self._deadline is None else max(0.0, self._deadline - self._clock())
            return {
                "due_in_s": due_in,
                "reasons": list(self._reasons),
                "queued": [c.key for c in self._queue],
                "running": self._running.key if self._running else None,
                "thread_alive": self._thread is not None and self._thread.is_alive(),
            }

    # ---- the one step ---------------------------------------------------------------

    def run_due(self, now: float | None = None) -> list[Outcome] | None:
        """Run the plan if its deadline is due and queue the candidates, then drain the
        queue (claim → launch → join, one at a time). Returns every outcome of this step —
        the plan's skips plus one row per candidate drained — or ``None`` when no sweep was
        due and the queue was empty."""
        now = self._clock() if now is None else now
        outcomes: list[Outcome] | None = None
        with self._lock:
            due = self._deadline is not None and now >= self._deadline
            reasons = list(self._reasons)
            if due:
                self._deadline = None
                self._first_armed = None
                self._reasons = []
        if due:
            try:
                candidates, skipped = self._plan_fn()
            except Exception:  # noqa: BLE001 — the loop must survive a broken plan
                logger.exception("auto-repair plan failed (%s)", ", ".join(reasons))
                candidates, skipped = [], []
            self.sweeps += 1
            outcomes = list(skipped)
            self.enqueue(candidates)
        drained = self._drain()
        if drained or outcomes is not None:
            outcomes = (outcomes or []) + drained
        if due:
            self.history.append({
                "at": now, "reasons": reasons,
                "launched": launched_job_ids(outcomes or []),
                "skipped": [o.as_dict() for o in (outcomes or []) if o.action == SKIPPED],
            })
            log_sweep(len(candidates), outcomes or [])
        return outcomes

    def _drain(self) -> list[Outcome]:
        drained: list[Outcome] = []
        while not self._stop.is_set():
            with self._lock:
                if not self._queue:
                    return drained
                candidate = self._queue.popleft()
                self._running = candidate
            try:
                outcome = self._claim_fn(candidate)
                drained.append(outcome)
                if outcome.action != LAUNCHED or not outcome.job_id:
                    continue
                worker = self._launch(outcome.job_id)
                self.launches.append(outcome.job_id)
                join = getattr(worker, "join", None)
                if callable(join):
                    join(self._job_cap_s)
                    if getattr(worker, "is_alive", lambda: False)():
                        logger.warning(
                            "auto-repair: job %s still running after %.0f s — no longer "
                            "waited for; moving on to the next candidate",
                            outcome.job_id, self._job_cap_s,
                        )
            except Exception:  # noqa: BLE001 — one candidate never stalls the queue
                logger.exception("auto-repair: candidate %s failed", candidate.key)
                drained.append(_skip(candidate.workspace_id, candidate.preset_key,
                                     candidate.agent_id, R_ERROR, message="launch failed"))
            finally:
                with self._lock:
                    self._running = None
        return drained

    # ---- thread -----------------------------------------------------------------------

    def _ensure_thread(self) -> None:
        if not self._start_thread:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="system-presets-auto-repair")
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                deadline = self._deadline
                idle = deadline is None and not self._queue
            if idle:
                timeout: float | None = None
            else:
                timeout = 0.0 if deadline is None else max(0.0, deadline - self._clock())
            if timeout is None or timeout > 0:
                self._wake.wait(timeout)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self.run_due()
            except Exception:  # noqa: BLE001
                logger.exception("auto-repair loop step failed")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None


# N17-obs268: the skip reasons the sweep summary always names (zeros included), in this order;
# any other reason (deploying / uninstalling / error) is appended only when it occurred
SUMMARY_REASONS: tuple[str, ...] = (R_CURRENT, R_FAILED, R_IN_FLIGHT, R_REFUSED,
                                    R_NOT_INSTALLED)


def skip_summary(outcomes: list[Outcome]) -> str:
    """`{current: a, failed: b, in_flight: c, refused: d, not_installed: e}` (+ any other
    reason that occurred) — the `skipped=` part of the sweep's summary line."""
    counts: dict[str, int] = dict.fromkeys(SUMMARY_REASONS, 0)
    for outcome in outcomes:
        if outcome.action == SKIPPED:
            counts[outcome.reason] = counts.get(outcome.reason, 0) + 1
    return "{" + ", ".join(f"{reason}: {n}" for reason, n in counts.items()) + "}"


def log_sweep(planned: int, outcomes: list[Outcome]) -> None:
    """N17-obs268 (deploy-ops-34): a sweep's result used to live only in the in-process
    `history` — the log could not say why a preset was not repaired. Every sweep now logs ONE
    INFO summary (`planned` = the candidates the plan queued, `launched` = jobs handed to the
    launcher) and one INFO line per skipped preset (workspace, preset, reason, and the job /
    refusal code / message when there is one). Never raises."""
    try:
        launched = launched_job_ids(outcomes)
        logger.info("auto-repair sweep: planned=%d launched=%d skipped=%s", planned,
                    len(launched), skip_summary(outcomes))
        for outcome in outcomes:
            if outcome.action != SKIPPED:
                continue
            extra = "".join(
                f" {name}={value}" for name, value in (
                    ("job", outcome.job_id), ("code", outcome.code),
                    ("message", (outcome.message or "")[:200] or None))
                if value)
            logger.info("auto-repair skip: workspace=%s preset=%s reason=%s%s",
                        outcome.workspace_id, outcome.preset_key, outcome.reason, extra)
    except Exception:  # noqa: BLE001 — observability never breaks the sweep
        logger.exception("auto-repair: sweep log failed")


def launched_job_ids(outcomes: list[Outcome]) -> list[str]:
    return [o.job_id for o in outcomes if o.action == LAUNCHED and o.job_id]


# ---------------------------------------------------------------------------
# process-wide slot + the two triggers
# ---------------------------------------------------------------------------

_scheduler: AutoRepairScheduler | None = None


def set_scheduler(scheduler: AutoRepairScheduler | None) -> AutoRepairScheduler | None:
    """Install (or, with ``None``, remove) the process's scheduler; returns the previous
    one. ``create_app`` installs a real one; tests install a fake or none."""
    global _scheduler
    previous, _scheduler = _scheduler, scheduler
    return previous


def scheduler() -> AutoRepairScheduler | None:
    return _scheduler


def install() -> AutoRepairScheduler:
    """``create_app`` hook: one scheduler per process (idempotent) and the registry
    listener. The thread starts lazily on the first schedule."""
    from app.services import model_registry

    global _scheduler
    if _scheduler is None:
        _scheduler = AutoRepairScheduler()
    model_registry.add_catalog_change_listener(on_catalog_change)
    return _scheduler


def enabled(settings: Any | None = None) -> bool:
    settings = settings if settings is not None else get_settings()
    return bool(getattr(settings, "system_presets_auto_repair", False))


def schedule_startup(settings: Any | None = None) -> float | None:
    """Trigger (a): after ``resume_pending_jobs()``. Returns the deadline, or None when the
    flag is off / no scheduler is installed."""
    settings = settings if settings is not None else get_settings()
    if not enabled(settings) or _scheduler is None:
        return None
    delay = float(getattr(settings, "system_presets_auto_repair_start_delay_s", 60))
    logger.info("system presets auto-repair: sweep scheduled %.0f s after startup", delay)
    return _scheduler.schedule(delay, "startup")


def on_catalog_change(row: Any, settings: Any | None = None) -> float | None:
    """Trigger (b): a journal row that moves the catalog version or writes a model role (the
    registry calls this only for those). Debounced: the deadline moves to ``now + debounce``
    on every write, capped at ``DEBOUNCE_MAX_WAIT_S`` after the first."""
    settings = settings if settings is not None else get_settings()
    if not enabled(settings) or _scheduler is None:
        return None
    delay = float(getattr(settings, "system_presets_auto_repair_debounce_s", 180))
    operation = getattr(row, "operation", None) or "catalog change"
    return _scheduler.schedule(delay, f"catalog change: {operation}")
