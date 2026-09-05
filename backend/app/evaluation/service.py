"""Evaluation run orchestration (adapted from agentcore_eva_opt routers/runs.py
and routers/insights.py — github.com/xiehust/agentcore_eva_opt).

Pipeline per run (executed by the bounded-concurrency run queue):
    invoking   — one runtime session per dataset item
    waiting    — traces land in CloudWatch (aws/spans)
    evaluating — StartBatchEvaluation scoped to exactly those sessions
    completed  — per-evaluator average scores (or insight trees)
    stopped    — operator stop: cancelled locally while queued/replaying, or
                 StopBatchEvaluation once a batch exists (partial scores kept)

Batch evaluation reads CloudWatch traces. Runtime-backed agents (zip_runtime /
studio / container) derive their span service name from the runtime; managed
harnesses run on an internal Strands runtime that emits
``service.name = "harness_{harnessName}.DEFAULT"`` with the
evaluation-parseable ``strands.telemetry.tracer`` scope (live-probed
2026-07-13) — the backing runtime id differs from the harnessId, so the
content-log group is discovered by log-group prefix instead of derived.
"""

import threading
import time
from collections.abc import Callable
from typing import Any

from botocore.exceptions import ClientError

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.evaluation import agentcore_eval as ac
from app.evaluation import simulation, telemetry
from app.evaluation.models import EvalRun
from app.evaluation.queue import run_queue
from app.evaluation.scenarios import (
    ground_truth_metadata,
    normalize_scenarios,
    scenario_prompts,
)
from app.models.ledger import Agent
from app.services.agentcore import harness as hc
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client, data_client
from app.services.workspace import WorkspaceContext, context_for_workspace
from app.templates import gateway_support

EVAL_SUPPORTED_METHODS = {"zip_runtime", "studio", "container", "harness"}
TELEMETRY_READY_GRACE_SECONDS = 120
TELEMETRY_QUERY_LOOKBACK_MS = 60_000

_sleep = time.sleep  # injectable for tests

# Ledger statuses a run can still be stopped from; everything else is terminal.
ACTIVE_STATUSES = ("queued", "invoking", "waiting", "evaluating")
STOP_REASON = "stopped by operator"


class RunStopped(Exception):
    """Raised inside execute_run when the operator asked for the run to stop
    before its batch evaluation was started."""


class _StopFlags:
    """Operator stop requests for runs whose work is still in this process
    (dataset replay / telemetry wait — no batch evaluation to stop on AWS yet).
    The replay loop polls the flag between prompts and before
    StartBatchEvaluation; in-memory by design — a restart fails those runs
    honestly in resume_interrupted_runs anyway."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids: set[str] = set()

    def request(self, run_id: str) -> None:
        with self._lock:
            self._ids.add(run_id)

    def requested(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._ids

    def clear(self, run_id: str) -> None:
        with self._lock:
            self._ids.discard(run_id)


stop_flags = _StopFlags()


def stop_requested(run_id: str) -> bool:
    return stop_flags.requested(run_id)


def _check_stop(run_id: str) -> None:
    if stop_flags.requested(run_id):
        raise RunStopped(run_id)

# With up to eval_max_concurrent_runs of our own batches (plus anything else in
# the account) contending for the 5-active / 3-TPS account quotas, a start can
# fail transiently — retry those instead of failing a run that already paid for
# its invoke/wait phases.
_RETRYABLE_START_CODES = {
    "ThrottlingException",
    "ConflictException",
    "ServiceQuotaExceededException",
    "TooManyRequestsException",
}
_START_RETRY_DELAYS_S = (20.0, 40.0, 80.0)


def _start_with_retry(start: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    for delay in _START_RETRY_DELAYS_S:
        try:
            return start()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in _RETRYABLE_START_CODES:
                raise
            _sleep(delay)
    return start()


def _harness_telemetry(
    agent: Agent, workspace: WorkspaceContext, logs_client: Any = None
) -> tuple[str, str]:
    """Harness span identity: the harnessId is ``{harnessName}-{suffix}`` and the
    managed backing runtime emits ``harness_{harnessName}.DEFAULT``. Its log
    group carries the BACKING runtime's own id (≠ harnessId) — discover it by
    prefix; a re-created harness leaves stale groups behind, so newest wins."""
    base = agent.resource_id.rsplit("-", 1)[0]
    prefix = f"/aws/bedrock-agentcore/runtimes/harness_{base}-"
    logs = logs_client or workspace.client("logs")
    groups = [
        g for g in logs.describe_log_groups(logGroupNamePrefix=prefix).get("logGroups", [])
        if g["logGroupName"].endswith("-DEFAULT")
    ]
    if not groups:
        raise AppError(
            "eval.harness_no_telemetry",
            "this harness has no telemetry log group yet — run at least one "
            "chat/invoke session first, then start the evaluation",
            status_code=400,
        )
    newest = max(groups, key=lambda g: g.get("creationTime", 0))
    return f"harness_{base}.DEFAULT", newest["logGroupName"]


def resolve_telemetry(
    agent: Agent, workspace: WorkspaceContext, logs_client: Any = None
) -> tuple[str, str]:
    """(service_name, log_group) for a platform agent's spans + content logs."""
    if agent.method not in EVAL_SUPPORTED_METHODS:
        raise AppError(
            "eval.method_unsupported",
            f"batch evaluation is not available for method '{agent.method}'",
            status_code=400,
        )
    if not agent.resource_id:
        raise AppError("eval.agent_not_deployed", "agent has no runtime", status_code=400)
    if agent.method == "harness":
        return _harness_telemetry(agent, workspace, logs_client)
    detail = rt.get_runtime(control_client(workspace), agent.resource_id)
    runtime_name = detail["agentRuntimeName"]
    return f"{runtime_name}.DEFAULT", (
        f"/aws/bedrock-agentcore/runtimes/{agent.resource_id}-DEFAULT"
    )


def _update(run_id: str, **fields: Any) -> None:
    db = SessionLocal()
    try:
        run = db.get(EvalRun, run_id)
        for key, value in fields.items():
            setattr(run, key, value)
        db.commit()
    finally:
        db.close()


def _wait_for_fresh_telemetry(
    *,
    workspace: WorkspaceContext,
    session_id: str,
    content_log_group: str,
    start_time_ms: int,
    stability_seconds: int,
) -> None:
    logs = workspace.client("logs")
    telemetry.wait_for_evaluation_telemetry(
        logs,
        session_id=session_id,
        content_log_group=content_log_group,
        start_time_ms=start_time_ms,
        stability_seconds=stability_seconds,
        timeout_seconds=stability_seconds + TELEMETRY_READY_GRACE_SECONDS,
    )


def execute_run(
    run_id: str,
    *,
    workspace: WorkspaceContext,
    agent_arn: str,
    method: str,
    service_name: str,
    log_group: str,
    protocol: str = "http",
    items: list[dict[str, Any]],
    evaluators: list[str],
    mode: str,
    wait_seconds: int,
    existing_session_ids: list[str] | None = None,
    time_range: dict[str, Any] | None = None,
    insights: list[str] | None = None,
    session_metadata: list[dict[str, Any]] | None = None,
    actor_model_id: str | None = None,
    runtime_user_id: str | None = None,
    online_config_arn: str | None = None,
) -> None:
    """Drive one evaluation run to completion (runs on a run-queue worker).

    Scope is one of: dataset ``items`` (invoke fresh sessions), explicit
    ``existing_session_ids``, a passive ``time_range`` window over the
    agent's past traffic — the window path skips invoke/wait entirely — or an
    online evaluation config (``online_config_arn`` + ``time_range``): an
    on-demand report over the sessions that config sampled, where the batch
    inherits the config's insights/evaluators (passing them is rejected)."""
    telemetry_start_ms = int(time.time() * 1000) - TELEMETRY_QUERY_LOOKBACK_MS
    try:
        _check_stop(run_id)
        data = data_client(workspace)
        session_ids = list(existing_session_ids or [])
        if online_config_arn:
            _update(run_id, status="evaluating")
            _check_stop(run_id)
            response = _start_with_retry(
                lambda: ac.start_online_report(
                    data,
                    name=f"run_{run_id[:8]}",
                    config_arn=online_config_arn,
                    time_range=time_range or {},
                    description="Launchpad on-demand online report",
                )
            )
            batch_id = response["batchEvaluationId"]
            _update(run_id, batch_eval_id=batch_id)
            _stop_batch_if_requested(run_id, data, batch_id)
            result = ac.poll_batch_evaluation(
                data, batch_id=batch_id, max_polls=60, interval=30.0
            )
            _finish_from_result(run_id, mode, result, workspace=workspace)
            return
        if not session_ids and not time_range:
            # One session per scenario. Predefined scenarios replay their turns
            # sequentially in that session; simulated persona scenarios run the
            # SDK's LLM-actor loop (actor_model_id plays the user). Ground
            # truth (assertions / expected trajectory / expected responses)
            # rides along as sessionMetadata.
            scenarios = normalize_scenarios(items)
            _update(run_id, status="invoking")
            for scenario in scenarios:
                _check_stop(run_id)
                sid: str | None = None
                if simulation.is_simulated(scenario):
                    sid = simulation.run_simulated_scenario(
                        data,
                        agent_arn=agent_arn,
                        method=method,
                        scenario=scenario,
                        actor_model_id=actor_model_id or "",
                        protocol=protocol,
                        runtime_user_id=runtime_user_id,
                    )
                else:
                    for prompt in scenario_prompts(scenario):
                        _check_stop(run_id)
                        if method == "harness":  # InvokeHarness, not the runtime data plane
                            result = hc.invoke_harness_text(data, agent_arn, prompt, session_id=sid)
                        elif protocol == "a2a":  # JSON-RPC runtimes reject {prompt}
                            result = rt.invoke_a2a_text(data, agent_arn, prompt, session_id=sid)
                        else:
                            result = rt.invoke_runtime_text(
                                data,
                                agent_arn,
                                prompt,
                                session_id=sid,
                                runtime_user_id=runtime_user_id,
                            )
                        sid = result["session_id"]
                session_ids.append(sid)
                _update(run_id, session_ids=list(session_ids))
            if session_metadata is None:
                session_metadata = ground_truth_metadata(scenarios, session_ids) or None
            _check_stop(run_id)
            _update(run_id, status="waiting")
            _wait_for_fresh_telemetry(
                workspace=workspace,
                session_id=session_ids[-1],
                content_log_group=log_group,
                start_time_ms=telemetry_start_ms,
                stability_seconds=wait_seconds,
            )

        # Last exit before the batch exists on AWS: a stop requested during
        # replay/wait ends the run here without ever calling StartBatchEvaluation.
        _check_stop(run_id)
        _update(run_id, status="evaluating", session_ids=session_ids)
        if mode == "insights":
            response = _start_with_retry(
                lambda: ac.start_insights_evaluation(
                    data,
                    name=f"run_{run_id[:8]}",
                    service_name=service_name,
                    log_groups=["aws/spans", log_group],
                    session_ids=session_ids or None,
                    time_range=time_range,
                    insights=insights,
                )
            )
        else:
            response = _start_with_retry(
                lambda: ac.start_batch_evaluation(
                    data,
                    name=f"run_{run_id[:8]}",
                    service_name=service_name,
                    log_groups=["aws/spans", log_group],
                    session_ids=session_ids or None,
                    time_range=time_range,
                    evaluators=evaluators,
                    session_metadata=session_metadata,
                )
            )
        batch_id = response["batchEvaluationId"]
        _update(run_id, batch_eval_id=batch_id)
        _stop_batch_if_requested(run_id, data, batch_id)
        # Insights cluster across sessions and routinely run 15-25 minutes;
        # give them a 30-minute budget instead of the evaluator default.
        if mode == "insights":
            result = ac.poll_batch_evaluation(
                data, batch_id=batch_id, max_polls=60, interval=30.0
            )
        else:
            result = ac.poll_batch_evaluation(data, batch_id=batch_id, max_polls=60)
        _finish_from_result(run_id, mode, result, workspace=workspace)
    except RunStopped:
        _update(run_id, status="stopped", error=STOP_REASON)
    except Exception as exc:
        _update(run_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
    finally:
        stop_flags.clear(run_id)


def _stop_batch_if_requested(run_id: str, data: Any, batch_id: str) -> None:
    """Close the race between "no batch id yet" (the stop route could only set
    the flag) and StartBatchEvaluation having just returned: forward the stop
    to AWS now so the poller observes STOPPING → STOPPED."""
    if stop_flags.requested(run_id):
        ac.stop_batch_evaluation(data, batch_id=batch_id)


def _finish_from_result(
    run_id: str,
    mode: str,
    result: dict[str, Any],
    *,
    workspace: WorkspaceContext | None = None,
) -> None:
    """Write a terminal batch-evaluation result back onto the run row.

    STOPPED (operator stop) ends the run as ``stopped`` with whatever scores
    the batch had produced. COMPLETED_WITH_ERRORS still completes the run, but the service's
    errorDetails (e.g. "insufficient samples for clustering") are surfaced in
    the error column so the UI can show why results are partial/empty.

    A non-completed batch raises, and the message carries everything the
    operator needs: the service's errorDetails plus the first per-trace
    evaluator error, which lives only in the batch's results log stream. (A run
    whose judge prompt wants ground truth the dataset lacks fails every single
    session with exactly that reason, and used to surface as a bare "ended
    FAILED".)"""
    status = result.get("status")
    details = result.get("errorDetails") or []
    if status == "STOPPED":
        # Operator stop (StopBatchEvaluation): terminal, but not a failure. AWS
        # keeps the results of the sessions it had already judged, so the
        # partial scores / insight trees are recorded like a completed run's.
        reason = STOP_REASON
        if details:
            reason += " — " + "; ".join(str(d) for d in details)
        parsed = (
            {"insights": ac.parse_insights(result)}
            if mode == "insights"
            else {"scores": ac.parse_eval_scores(result)}
        )
        _update(run_id, status="stopped", error=reason[:500], **parsed)
        return
    if status not in ("COMPLETED", "COMPLETED_WITH_ERRORS"):
        message = f"batch evaluation ended {status}"
        if details:
            message += " — " + "; ".join(str(d) for d in details)
        reason = (
            ac.batch_failure_reason(lambda: workspace.client("logs"), result)
            if workspace is not None
            else None
        )
        if reason:
            message += f" · {reason}"
        raise RuntimeError(message)
    error = "; ".join(str(d) for d in details)[:500] or None
    if mode == "insights":
        _update(run_id, status="completed", insights=ac.parse_insights(result),
                error=error)
    else:
        _update(run_id, status="completed", scores=ac.parse_eval_scores(result),
                error=error)


def reconcile_run(
    run_id: str, *, mode: str, batch_id: str, workspace: WorkspaceContext
) -> None:
    """Finish a run whose in-process poller died (restart / dev reload) while
    the batch evaluation kept running server-side."""
    try:
        result = ac.poll_batch_evaluation(
            data_client(workspace), batch_id=batch_id, max_polls=60
        )
        _finish_from_result(run_id, mode, result, workspace=workspace)
    except Exception as exc:
        _update(run_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:500])


def request_stop(run_id: str, *, workspace: WorkspaceContext) -> EvalRun:
    """Operator stop for an active run; returns the refreshed row.

    * a batch already exists on AWS → ``StopBatchEvaluation``; the poller
      (or startup reconciliation) sees STOPPING → STOPPED and finishes the row
      as ``stopped`` with the sessions judged so far;
    * still ``queued`` → cancelled locally, the worker skips it, the row is
      ``stopped`` right away and AWS is never called;
    * replaying / waiting for telemetry with no batch yet → a stop flag the
      loop polls between prompts and before StartBatchEvaluation.
    A terminal run (completed / failed / stopped) is a 409 conflict."""
    db = SessionLocal()
    try:
        run = db.get(EvalRun, run_id)
        if run is None:
            raise AppError("run.not_found", "run not found", status_code=404)
        if run.status not in ACTIVE_STATUSES:
            raise AppError(
                "run.not_active",
                f"run is already {run.status} and cannot be stopped",
                status_code=409,
            )
        batch_id = run.batch_eval_id
    finally:
        db.close()
    # The flag first: whichever way the worker is racing us, it either finds
    # the flag at its next check or forwards the stop right after the batch
    # starts (_stop_batch_if_requested).
    stop_flags.request(run_id)
    if batch_id:
        ac.stop_batch_evaluation(data_client(workspace), batch_id=batch_id)
    elif run_queue.cancel(run_id):
        # Never dequeued: nothing is running, so the row is settled here and
        # the flag is not needed (no callable will ever read it).
        stop_flags.clear(run_id)
        _update(run_id, status="stopped", error=STOP_REASON)
    db = SessionLocal()
    try:
        return db.get(EvalRun, run_id)
    finally:
        db.close()


INTERRUPTED_STATUSES = ACTIVE_STATUSES


def resume_interrupted_runs() -> list[str]:
    """Startup reconciliation. The account-lock worker and its pollers are
    in-memory, so a backend restart orphans in-flight rows: runs that already
    started a batch are re-polled to completion; runs killed before the batch
    started lost their in-memory work and are failed honestly."""
    db = SessionLocal()
    try:
        rows = db.query(EvalRun).filter(EvalRun.status.in_(INTERRUPTED_STATUSES)).all()
        resumed: list[str] = []
        for run in rows:
            if run.status == "evaluating" and run.batch_eval_id:
                # The workspace is rebuilt from the row, not from ambient
                # settings: the batch is being polled in the account/region the
                # run was submitted against.
                workspace = context_for_workspace(run.workspace_id)
                run_queue.submit(
                    run.id,
                    lambda rid=run.id, m=run.mode, b=run.batch_eval_id, w=workspace: (
                        reconcile_run(rid, mode=m, batch_id=b, workspace=w)
                    ),
                )
                resumed.append(run.id)
            else:
                run.status = "failed"
                run.error = ("interrupted by a backend restart before the batch "
                             "evaluation started — submit the run again")
        db.commit()
        return resumed
    finally:
        db.close()


def submit_run(
    *,
    agent: Agent,
    workspace: WorkspaceContext,
    dataset_items: list[dict[str, Any]],
    dataset_id: str | None,
    dataset_name: str | None,
    evaluators: list[str],
    mode: str = "evaluators",
    wait_seconds: int = 180,
    session_ids: list[str] | None = None,
    time_range: dict[str, Any] | None = None,
    insights: list[str] | None = None,
    session_metadata: list[dict[str, Any]] | None = None,
    lookback_hours: int | None = None,
    actor_model_id: str | None = None,
    online_config_arn: str | None = None,
    dataset_version: str | None = None,
) -> EvalRun:
    service_name, log_group = resolve_telemetry(agent, workspace)
    # Window runs have no dataset; encode the scope in dataset_name so the
    # runs list can render "window · Nh" without a schema change.
    if lookback_hours and not dataset_name:
        dataset_name = f"window:{lookback_hours}h"
    db = SessionLocal()
    try:
        run = EvalRun(
            workspace_id=agent.workspace_id,
            agent_id=agent.id,
            agent_name=agent.name,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            dataset_version=dataset_version,
            mode=mode,
            evaluators=evaluators,
            status="queued",
            session_ids=session_ids or [],
        )
        db.add(run)
        db.commit()
        run_id = run.id
        agent_arn = agent.arn
        agent_method = agent.method
        agent_protocol = (agent.spec or {}).get("protocol") or "http"
        # Gateway-tool agents need a runtimeUserId or the Runtime injects no
        # workload token and the eval run measures a tool-less agent.
        agent_runtime_user = gateway_support.runtime_user_id(agent.spec)
    finally:
        db.close()

    position = run_queue.submit(
        run_id,
        lambda: execute_run(
            run_id,
            workspace=workspace,
            agent_arn=agent_arn,
            method=agent_method,
            protocol=agent_protocol,
            service_name=service_name,
            log_group=log_group,
            items=dataset_items,
            evaluators=evaluators,
            mode=mode,
            wait_seconds=wait_seconds,
            existing_session_ids=session_ids,
            time_range=time_range,
            insights=insights,
            session_metadata=session_metadata,
            actor_model_id=actor_model_id,
            runtime_user_id=agent_runtime_user,
            online_config_arn=online_config_arn,
        ),
    )
    _update(run_id, queue_position=position)
    db = SessionLocal()
    try:
        return db.get(EvalRun, run_id)
    finally:
        db.close()
