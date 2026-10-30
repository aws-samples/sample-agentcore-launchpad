"""``RuntimeExecutor`` — jobs on the evaluation worker Runtime (N12 E1-3b; plan §B.0).

The production executor behind ``executor.select_executor``: ``InvokeAgentRuntime`` on the
system-managed ``launchpad-eval-worker`` with a fresh ``runtimeSessionId`` per job attempt,
then S3 polling / ingest of the worker's artefacts through the funnel's S3 client. The
worker's ack is synchronous (``ACCEPTED`` | ``REJECTED``); everything after that is read
from S3 (``status.json`` / ``results.jsonl`` / ``done.json``), so a lost backend process
re-attaches by reading the same keys. ``cancel`` writes the marker; ``stop_session`` is the
hard stop (``StopRuntimeSession``) the service applies after the grace period.

``inprocess_job_runner`` is the same worker dispatcher (``jobs_batch.run_job``) wired to the
backend's clients for the dev / SCORE-NOW ``InProcessExecutor``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, NamedTuple

from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.errors import AppError
from app.evaluation import job_descriptors, jobs
from app.evaluation.jobs import JobDone, JobSpec, JobStatus
from app.evaluation.judge_budget import judge_budget, judge_budget_cap
from app.evaluation.strands_engine import default_judge_factory
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import data_client
from app.services.strands_eval import code_evaluators, jobs_batch
from app.services.strands_eval.sink import JobSink, S3Store, StoreReader
from app.services.workspace import WorkspaceContext, context_for_workspace
from app.templates.eval_worker import RUNTIME_USER_ID

WORKER_ARN_KEY = "eval_worker_runtime_arn"
WORKER_AGENT_KEY = "eval_worker_agent_id"
DISPATCH_ARTIFACT = "dispatch.json"


def worker_missing(detail: dict[str, Any] | None = None) -> AppError:
    return AppError(
        "evaluation.worker_missing",
        "this workspace has no evaluation worker (launchpad-eval-worker) — run "
        "`make bootstrap` or install it from the System agents page; datasets, "
        "evaluators and SCORE NOW keep working without it",
        detail or {}, status_code=503,
    )


class WorkerState(NamedTuple):
    """What the preset row says about the workspace's evaluation worker right now."""

    arn: str | None
    status: str  # not_installed | deploying | uninstalling | failed | draft | active


def resolve_worker(ws: WorkspaceContext) -> WorkerState:
    """The worker *currently installed* in ``ws`` — the eval-worker preset row (``active``
    + its Runtime ARN), read on every call.

    N12-bug6: the resource map's ``eval_worker_runtime_arn`` is written at bootstrap and
    survives ``DELETE /api/system-agents/eval-worker``, so it is never consulted here — a
    stale ARN alone must not count as an installed worker. The preset row is what install
    / repair / uninstall maintain (``system_agents/service.py``), so it is the truth."""
    from app.core.db import SessionLocal
    from app.models.ledger import Agent
    from app.system_agents.presets import EVAL_WORKER_KEY

    db = SessionLocal()
    try:
        row = (
            db.query(Agent)
            .filter(Agent.workspace_id == ws.id, Agent.system_key == EVAL_WORKER_KEY,
                    Agent.status != "deleted")
            .first()
        )
        if row is None:
            return WorkerState(None, "not_installed")
        arn = str(row.arn or "") or None
        return WorkerState(arn if row.status == "active" else None, row.status)
    finally:
        db.close()


WorkerResolver = Callable[[WorkspaceContext], "WorkerState | str | None"]


def worker_arn(ws: WorkspaceContext) -> str | None:
    """The installed worker's ARN or ``None`` (see :func:`resolve_worker`)."""
    return resolve_worker(ws).arn


def require_installed_worker(ws: WorkspaceContext,
                             resolver: WorkerResolver = resolve_worker) -> str:
    """The ARN of an active worker, or 503 ``evaluation.worker_missing`` naming the
    preset's current status (``not_installed`` / ``deploying`` / ``uninstalling`` / …)."""
    state = resolver(ws)
    if not isinstance(state, WorkerState):
        state = WorkerState(state or None, "active" if state else "not_installed")
    if state.arn and state.status == "active":
        return state.arn
    exc = worker_missing({"worker_status": state.status})
    if state.status != "not_installed":
        exc.message = (
            f"the evaluation worker (launchpad-eval-worker) is {state.status} — jobs are "
            "accepted again once the System agents page shows it active; datasets, "
            "evaluators and SCORE NOW keep working meanwhile"
        )
    return _raise(exc)


def _raise(exc: AppError) -> str:
    raise exc


class RuntimeExecutor:
    def __init__(
        self,
        *,
        data_client_factory: Callable[[WorkspaceContext], Any] | None = None,
        s3_client_factory: Callable[[WorkspaceContext], Any] | None = None,
        worker_resolver: WorkerResolver | None = None,
    ) -> None:
        self._data = data_client_factory or data_client
        self._s3 = s3_client_factory or (lambda ws: ws.client("s3"))
        self._resolve_worker = worker_resolver or resolve_worker

    # ── wiring ──
    def _bucket(self, ws: WorkspaceContext) -> str:
        bucket = str((ws.resources or {}).get("artifacts_bucket") or "")
        if not bucket:
            raise worker_missing({"missing": "artifacts_bucket"})
        return bucket

    def _store(self, ws: WorkspaceContext) -> S3Store:
        return S3Store(self._s3(ws), self._bucket(ws))

    def _reader(self, ws: WorkspaceContext) -> StoreReader:
        return StoreReader(self._store(ws))

    def _arn(self, ws: WorkspaceContext) -> str:
        return require_installed_worker(ws, self._resolve_worker)

    def assert_ready(self, ws: WorkspaceContext) -> str:
        """Submission-time probe (``executor.assert_executor_ready``): the bucket and an
        *active* worker, resolved now — not a value cached at registration."""
        self._bucket(ws)
        return self._arn(ws)

    # ── EvalExecutor ──
    def submit(self, ws: WorkspaceContext, job: JobSpec) -> str:
        jobs.validate_job_type(job.job_type)
        arn = self._arn(ws)
        store = self._store(ws)
        session_id = jobs.runtime_session_id(job.job_id, job.attempt)
        store.put(
            jobs.artifact_key(job.job_id, job.attempt, DISPATCH_ARTIFACT),
            json.dumps({
                "runtime_session_id": session_id, "worker_arn": arn,
                "dispatched_at": datetime.now(UTC).isoformat(), "job_type": job.job_type,
            }).encode("utf-8"),
        )
        # D21 (M1-d): the descriptors of every catalog model the spec names ride with the job
        job_descriptors.attach(job)
        response = self._data(ws).invoke_agent_runtime(
            agentRuntimeArn=arn,
            qualifier="DEFAULT",
            runtimeSessionId=session_id,
            runtimeUserId=RUNTIME_USER_ID,  # the worker exchanges the injected token for the key
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(job.to_payload()).encode("utf-8"),
        )
        ack = _read_ack(response)
        state = str(ack.get("state") or "")
        if state == "REJECTED":
            code = str(ack.get("code") or "evaluation.worker_rejected")
            message = str(ack.get("message") or "the evaluation worker rejected the job")
            raise AppError(code, message, {"job_id": job.job_id, "ack": ack},
                           status_code=400 if code == jobs.KIND_UNSUPPORTED_CODE else 502)
        if state != "ACCEPTED":
            raise AppError("evaluation.worker_rejected",
                           "the evaluation worker returned no acknowledgement",
                           {"job_id": job.job_id, "ack": ack}, status_code=502)
        return job.job_id

    def status(self, ws: WorkspaceContext, job_id: str) -> JobStatus | None:
        return self._reader(ws).status(job_id)

    def done(self, ws: WorkspaceContext, job_id: str) -> JobDone | None:
        return self._reader(ws).done(job_id)

    def ingest(self, ws: WorkspaceContext, job_id: str, since: int = 0) -> list[dict[str, Any]]:
        return self._reader(ws).ingest(job_id, since)

    def cancel(self, ws: WorkspaceContext, job_id: str) -> None:
        self._reader(ws).cancel(job_id)

    # ── beyond the Protocol ──
    def dispatch(self, ws: WorkspaceContext, job_id: str) -> dict[str, Any] | None:
        return self._reader(ws).artifact(job_id, DISPATCH_ARTIFACT)

    def invocations(self, ws: WorkspaceContext, job_id: str,
                    attempt: int | None = None) -> list[dict[str, Any]]:
        return self._reader(ws).invocations(job_id, attempt)

    def artifact(self, ws: WorkspaceContext, job_id: str, name: str) -> Any | None:
        return self._reader(ws).artifact(job_id, name)

    def attempt_artifacts(self, ws: WorkspaceContext, job_id: str, name: str) -> list[Any]:
        return self._reader(ws).attempt_artifacts(job_id, name)

    def jobs_with_prefix(self, ws: WorkspaceContext, prefix: str) -> list[str]:
        return self._reader(ws).jobs_with_prefix(prefix)

    def stop_session(self, ws: WorkspaceContext, job_id: str) -> bool:
        """``StopRuntimeSession`` on the job's worker session (the hard cancel); False when
        the session is already gone."""
        dispatch = self.dispatch(ws, job_id)
        if not dispatch:
            return False
        try:
            rt.stop_runtime_session(
                self._data(ws), runtime_arn=str(dispatch["worker_arn"]),
                session_id=str(dispatch["runtime_session_id"]),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return False
            raise
        return True


def _read_ack(response: dict[str, Any]) -> dict[str, Any]:
    body = response.get("response")
    raw = body.read() if hasattr(body, "read") else body
    if hasattr(body, "close"):
        body.close()
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")
    text = text.strip()
    if text.startswith("data:"):  # an SSE-framed single event
        text = "\n".join(line[len("data:"):].strip() for line in text.splitlines()
                         if line.startswith("data:"))
    try:
        ack = json.loads(text) if text else {}
    except ValueError:
        return {"raw": text[:500]}
    return ack if isinstance(ack, dict) else {"raw": ack}


def lambda_clients(ws: WorkspaceContext) -> code_evaluators.LambdaClients:
    """N17-obs333: one funnel Lambda client per code-evaluator timeout (read timeout =
    ``lambda_timeout_s`` + margin, one attempt), cached per timeout."""
    return code_evaluators.LambdaClients(lambda timeout_s: ws.client(
        "lambda", cache_token=f"code-evaluator-{timeout_s}",
        config=code_evaluators.lambda_config(timeout_s)))


# ── the in-process executor runs the worker's dispatcher against the backend's clients ──
def worker_env_for(ws: WorkspaceContext) -> jobs_batch.WorkerEnv:
    settings = get_settings()
    return jobs_batch.WorkerEnv(
        logs_client=ws.client("logs"),
        data_client=data_client(ws),
        judge_factory=default_judge_factory,
        default_model_id=settings.eval_judge_model_id,
        lambda_client=lambda_clients(ws) if settings.eval_code_evaluators_enabled else None,
        code_evaluators_enabled=settings.eval_code_evaluators_enabled,
        spans_source=settings.observability_spans_source,
        judge_concurrency=settings.eval_judge_concurrency,
        # N-SCN-bug2: same budget policy as the worker (model-aware start, capped doubling)
        judge_budget_cap=judge_budget_cap(None, settings),
        judge_budget=lambda model_id: judge_budget(model_id, settings),
        # N12-obs53 / N12-obs55: the waits before the 2nd / 3rd attempt on a transient
        # upstream error (replay AND judge), same list the worker is rendered with
        transient_backoff_s=settings.eval_transient_retry_backoff,
    )


def inprocess_job_runner(job: JobSpec, sink: JobSink) -> None:
    """``executor.set_job_runner`` target: the job's workspace is rebuilt from the spec
    (``workspace.id``), the same code the Runtime worker runs."""
    ws = context_for_workspace(str((job.workspace or {}).get("id") or "") or None)
    jobs_batch.run_job(job, sink, worker_env_for(ws))
