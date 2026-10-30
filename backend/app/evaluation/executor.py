"""Where evaluation jobs execute (N12 E0-5; plan §B.0 + CA-8 ruling).

``EvalExecutor`` is the seam the run pipeline, the online scheduler and the experiment /
canary stages submit jobs through. Two implementations:

* ``RuntimeExecutor`` (W-E1-3b) — the production path: ``InvokeAgentRuntime`` on the
  system-managed ``launchpad_eval_worker``, S3 polling/ingest. Registered in ``main.py``
  with :func:`set_runtime_executor`; absent → 503 ``evaluation.worker_missing``.
* :class:`InProcessExecutor` — the same job runner on a daemon thread against the same
  artefact layout (an in-memory or S3 :class:`ObjectStore`). Allowed only for the hermetic
  suite, ``make dev`` without a worker, and the synchronous SCORE NOW call: in
  ``run_mode=prod`` :func:`select_executor` refuses it for every batch kind with 409
  ``evaluation.executor_not_allowed`` — it can never become a silent production flag.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Protocol

from app.core.errors import AppError
from app.evaluation import jobs
from app.evaluation.jobs import JobDone, JobSpec, JobStatus, KindUnsupportedError
from app.services.strands_eval.sink import (  # noqa: F401 — re-exported seam names
    JobSink,
    MemoryStore,
    ObjectStore,
    S3Store,
    StoreReader,
)

ONDEMAND = "ondemand"  # SCORE NOW — one session, ≤ 5 evaluators, the caller waits
BATCH_JOB_TYPES = jobs.JOB_TYPES


class EvalExecutor(Protocol):
    def submit(self, ws: Any, job: JobSpec) -> str: ...
    def status(self, ws: Any, job_id: str) -> JobStatus | None: ...
    def done(self, ws: Any, job_id: str) -> JobDone | None: ...
    def ingest(self, ws: Any, job_id: str, since: int = 0) -> list[dict[str, Any]]: ...
    def cancel(self, ws: Any, job_id: str) -> None: ...


# ── object stores + the job's sink: ONE implementation, shipped into the worker zip ──
# (services/strands_eval/sink.py — the names stay importable from here)
JobRunner = Callable[[JobSpec, JobSink], None]
_StoreReader = StoreReader


class InProcessExecutor:
    """Runs the worker's job runner on a daemon thread against ``store``."""

    def __init__(self, store: ObjectStore, *, runner: JobRunner) -> None:
        self.store = store
        self._runner = runner
        self._reader = _StoreReader(store)
        self._threads: dict[str, threading.Thread] = {}

    def submit(self, ws: Any, job: JobSpec) -> str:
        jobs.validate_job_type(job.job_type)
        sink = JobSink(self.store, job)
        sink.status("invoking", message="starting")

        def run() -> None:
            try:
                self._runner(job, sink)
                if not sink.finished:
                    sink.finish("COMPLETED")
            except Exception as exc:  # the runner's crash is the job's failure, never ours
                sink.finish("FAILED", error=f"{type(exc).__name__}: {exc}"[:2000])

        thread = threading.Thread(target=run, daemon=True, name=f"eval-job-{job.job_id}")
        self._threads[job.job_id] = thread
        thread.start()
        return job.job_id

    def wait(self, job_id: str, *, timeout_s: float = 30.0) -> bool:
        thread = self._threads.get(job_id)
        if thread is None:
            return True
        thread.join(timeout_s)
        return not thread.is_alive()

    def status(self, ws: Any, job_id: str) -> JobStatus | None:
        return self._reader.status(job_id)

    def done(self, ws: Any, job_id: str) -> JobDone | None:
        return self._reader.done(job_id)

    def ingest(self, ws: Any, job_id: str, since: int = 0) -> list[dict[str, Any]]:
        return self._reader.ingest(job_id, since)

    def cancel(self, ws: Any, job_id: str) -> None:
        self._reader.cancel(job_id)

    # the same extra surface RuntimeExecutor offers (service.py reads them through getattr)
    def artifact(self, ws: Any, job_id: str, name: str) -> Any | None:
        return self._reader.artifact(job_id, name)

    def attempt_artifacts(self, ws: Any, job_id: str, name: str) -> list[Any]:
        return self._reader.attempt_artifacts(job_id, name)

    def invocations(self, ws: Any, job_id: str, attempt: int | None = None) -> list[dict[str, Any]]:
        return self._reader.invocations(job_id, attempt)

    def jobs_with_prefix(self, ws: Any, prefix: str) -> list[str]:
        return self._reader.jobs_with_prefix(prefix)


# ── slots + selection (CA-8) ────────────────────────────────────────────────
_runtime_executor: EvalExecutor | None = None
_inprocess_executor: InProcessExecutor | None = None
_job_registry: JobRunner | None = None


def set_runtime_executor(executor: EvalExecutor | None) -> None:
    """``main.py`` registers W-E1-3b's RuntimeExecutor here when the feature is on."""
    global _runtime_executor
    _runtime_executor = executor


def runtime_executor_or_none() -> EvalExecutor | None:
    return _runtime_executor


def set_job_runner(runner: JobRunner | None) -> None:
    """The worker's job dispatcher (W-E1-3b's ``templates/eval_worker`` registry), reused
    by the in-process executor so dev and prod execute the same code."""
    global _job_registry
    _job_registry = runner


def set_inprocess_executor(executor: InProcessExecutor | None) -> None:
    global _inprocess_executor
    _inprocess_executor = executor


def _unregistered_runner(job: JobSpec, sink: JobSink) -> None:
    raise RuntimeError(
        f"no job runner registered for {job.job_type!r} — W-E1-3b's worker dispatcher "
        "(evaluation.executor.set_job_runner) is not wired in this process"
    )


def inprocess_executor() -> InProcessExecutor:
    global _inprocess_executor
    if _inprocess_executor is None:
        _inprocess_executor = InProcessExecutor(MemoryStore(),
                                                runner=_job_registry or _unregistered_runner)
    return _inprocess_executor


def inprocess_allowed(settings: Any, job_type: str) -> bool:
    """CA-8: in-process execution is fine for SCORE NOW anywhere, and for batch kinds only
    outside production."""
    if job_type == ONDEMAND:
        return True
    return settings.run_mode != "prod"


def assert_executor_ready(executor: EvalExecutor, workspace: Any) -> None:
    """A submit path's readiness probe (N12-bug6): an executor that depends on a workspace
    resource — the worker Runtime — re-checks it from the *current* state (the eval-worker
    preset row) so the request is refused with 503 ``evaluation.worker_missing`` instead
    of a job that is accepted and then fails on a stale ARN. Executors without a probe
    (in-process, test fakes) are always ready."""
    probe = getattr(executor, "assert_ready", None)
    if probe is not None:
        probe(workspace)


def select_executor(settings: Any, *, job_type: str) -> EvalExecutor:
    """The executor a submit path must use for ``job_type`` under ``settings``."""
    if job_type != ONDEMAND:
        try:
            jobs.validate_job_type(job_type)
        except KindUnsupportedError as exc:
            raise AppError(exc.code, str(exc), {"job_type": job_type}, status_code=400) from exc
    if job_type == ONDEMAND:
        return inprocess_executor()
    if settings.eval_executor == "inprocess":
        if not inprocess_allowed(settings, job_type):
            raise AppError(
                "evaluation.executor_not_allowed",
                "eval_executor=inprocess is not allowed in run_mode=prod for "
                f"{job_type} jobs — deploy the evaluation worker (make bootstrap) and use "
                "eval_executor=runtime; in-process execution is for dev and SCORE NOW only",
                {"job_type": job_type, "eval_executor": "inprocess", "run_mode": "prod"},
                status_code=409,
            )
        return inprocess_executor()
    executor = runtime_executor_or_none()
    if executor is None:
        raise AppError(
            "evaluation.worker_missing",
            "this workspace has no evaluation worker (launchpad_eval_worker) — run "
            "`make bootstrap` or install it from the System agents page; datasets, "
            "evaluators and SCORE NOW keep working without it",
            {"job_type": job_type}, status_code=503,
        )
    return executor
