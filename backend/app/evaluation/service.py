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

import copy
import math
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
from botocore.exceptions import ClientError

from app.core.config import get_settings
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

# byoc: telemetry identity derivation is method-agnostic for runtimes; whether
# the user's code emits gen_ai spans for the evaluator to read is theirs.
EVAL_SUPPORTED_METHODS = {"zip_runtime", "studio", "container", "harness", "byoc"}
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


# A scenario whose invocation hits an upstream hiccup is replayed this many more times,
# each in a fresh session, before the run fails (live 2026-10-04: one transient
# "The server had an error while processing your request" failed a 10-scenario run;
# the same prompt replayed cleanly 3/3).
TRANSIENT_SCENARIO_RETRIES = 2
# A timed-out attempt already spent the agent's whole budget, so it gets one replay only.
TIMEOUT_SCENARIO_RETRIES = 1
# The agent spent its own budget on the scenario (timeout after its replay, iteration /
# token limit): that is the scenario's result — its session is scored as it stands and
# recorded in ``EvalRun.budget_stops`` instead of failing the whole run.
BUDGET_STOP_CODES = frozenset({"harness.execution_timeout", "harness.execution_limit"})
SCENARIO_ERROR_CHARS = 300


def _scenario_error(exc: BaseException) -> dict[str, str]:
    """A failed scenario's ``code`` + one-line ``error`` for ``EvalRun.scenario_failures``."""
    code = getattr(exc, "code", None)
    if code:
        return {"code": str(code), "error": f"{code}: {getattr(exc, 'message', exc)}"[
            :SCENARIO_ERROR_CHARS]}
    return {"code": type(exc).__name__, "error": f"{type(exc).__name__}: {exc}"[
        :SCENARIO_ERROR_CHARS]}
# ``runtimeClientError`` is the mid-stream event; ``RuntimeClientError`` the same failure
# raised by the InvokeHarness call itself, with a 4xx status (live 2026-10-08: "Runtime
# health check failed or timed out" failed a whole 19-scenario run at one scenario)
_TRANSIENT_CODES = frozenset({
    "runtimeClientError", "RuntimeClientError", "internalServerException",
    "InternalServerException",
    "throttlingException", "ThrottlingException", "serviceUnavailableException",
    "ServiceUnavailableException",
})


def transient_invoke_error(exc: BaseException) -> bool:
    """An upstream failure worth replaying the scenario: a mid-stream
    ``runtimeClientError`` / ``internalServerException`` (botocore's
    ``EventStreamError``, or the ``RuntimeError`` ``iter_harness_stream`` raises for
    those events), throttling, a 5xx, or a Harness loop that stopped right after a
    tool step without answering (``harness.incomplete_response`` with stop reason
    ``tool_result`` / ``tool_use`` — live 2026-10-04: 1 of 3 replays of a research
    prompt, the other replays answered normally), or a Harness execution timeout
    (live 2026-10-04: a model call that never returned held a research scenario
    silent until the 600 s budget ran out — retried at most
    ``TIMEOUT_SCENARIO_RETRIES`` times). A JWT-inbound agent's bearer invoke
    counts the same way: an HTTP 5xx / 429 answer (``RuntimeBearerHttpError``)
    or a transport failure (``httpx.TransportError``: connect / read timeout,
    dropped connection) is transient, while an authorizer rejection
    (``RuntimeBearerAuthError``, 401/403) and any other 4xx are final.
    Iteration / token limits and every other error are final."""
    if isinstance(exc, AppError):
        if exc.code == "harness.execution_timeout":
            return True
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        # stop_reason None: the stream closed with neither a stop event nor any text —
        # the agent never ran (live 2026-10-04: no span at all for that session).
        return exc.code == "harness.incomplete_response" and detail.get("stop_reason") in {
            "tool_result", "tool_use", None}
    if isinstance(exc, ClientError):
        error = exc.response.get("Error") or {}
        status = (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 0
        return error.get("Code") in _TRANSIENT_CODES or int(status) >= 500
    if isinstance(exc, rt.RuntimeBearerAuthError):
        return False
    if isinstance(exc, rt.RuntimeBearerHttpError):
        return exc.status_code >= 500 or exc.status_code == 429
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, RuntimeError):
        return str(exc).startswith(("runtime client error", "internal server error"))
    return False


def execute_run(
    run_id: str,
    *,
    workspace: WorkspaceContext,
    agent_arn: str,
    method: str,
    service_name: str,
    log_group: str,
    log_groups: list[str] | None = None,
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
    agent_id: str | None = None,
    inbound_jwt: bool = False,
) -> None:
    """Drive one evaluation run to completion (runs on a run-queue worker).

    Scope is one of: dataset ``items`` (invoke fresh sessions), explicit
    ``existing_session_ids``, a passive ``time_range`` window over the
    agent's past traffic — the window path skips invoke/wait entirely — or an
    online evaluation config (``online_config_arn`` + ``time_range``): an
    on-demand report over the sessions that config sampled, where the batch
    inherits the config's insights/evaluators (passing them is rejected).
    ``log_groups`` are the batch's input log groups (default: ``aws/spans`` plus
    the agent's own ``log_group``)."""
    input_groups = list(log_groups or ["aws/spans", log_group])
    telemetry_start_ms = int(time.time() * 1000) - TELEMETRY_QUERY_LOOKBACK_MS
    attempt: dict[str, str] = {}
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
            result = _poll_batch(data, batch_id)
            _finish_from_result(run_id, mode, result, workspace=workspace)
            return
        if not session_ids and not time_range:
            # One session per scenario. Predefined scenarios replay their turns
            # sequentially in that session; simulated persona scenarios run the
            # SDK's LLM-actor loop (actor_model_id plays the user). Ground
            # truth (assertions / expected trajectory / expected responses)
            # rides along as sessionMetadata.
            scenarios = normalize_scenarios(items)
            metadata_entries: list[dict[str, Any]] = []
            watermark_sid: str | None = None

            def invoke(prompt: str, sid: str | None) -> dict[str, Any]:
                if method == "harness":  # InvokeHarness, not the runtime data plane
                    attempt["session_id"] = sid or hc.new_session_id()
                    return hc.invoke_harness_text(
                        data, agent_arn, prompt, session_id=attempt["session_id"],
                    )
                if protocol == "a2a":  # JSON-RPC runtimes reject {prompt}
                    return rt.invoke_a2a_text(data, agent_arn, prompt, session_id=sid)
                if inbound_jwt:  # JWT authorizer: Bearer, never SigV4
                    from app.services import inbound_auth as inbound_auth_service

                    return rt.invoke_runtime_text_bearer(
                        workspace.region,
                        agent_arn,
                        inbound_auth_service.m2m_bearer_token(workspace),
                        prompt,
                        session_id=sid,
                    )
                return rt.invoke_runtime_text(
                    data,
                    agent_arn,
                    prompt,
                    session_id=sid,
                    runtime_user_id=runtime_user_id,
                )

            budget_stops: list[dict[str, str]] = []
            # A simulated persona that still fails after its retries is skipped,
            # not fatal: the LLM actor's conversation is not reproducible, and one
            # stalled model call (live 2026-10-10: a Harness GPT-6 call returned
            # 0 tokens for 600 s) must not discard the other personas' sessions.
            # The run fails only when no scenario produced a session.
            scenario_failures: list[dict[str, str]] = []
            _update(run_id, status="invoking")
            for scenario in scenarios:
                _check_stop(run_id)
                attempt.clear()
                attempt["scenario_id"] = str(scenario.get("scenario_id") or "unknown")
                sid: str | None = None
                skipped = False
                for retry in range(TRANSIENT_SCENARIO_RETRIES + 1):
                    try:
                        sid = None  # a replay starts the whole scenario in a fresh session
                        if simulation.is_simulated(scenario):
                            sim_kwargs: dict[str, Any] = {}
                            if inbound_jwt:
                                # JWT runtimes: persona turns Bearer-invoke like replay turns
                                sim_kwargs["invoke_text"] = invoke
                            sid = simulation.run_simulated_scenario(
                                data,
                                agent_arn=agent_arn,
                                method=method,
                                scenario=scenario,
                                actor_model_id=actor_model_id or "",
                                protocol=protocol,
                                runtime_user_id=runtime_user_id,
                                **sim_kwargs,
                            )
                        else:
                            for prompt in scenario_prompts(scenario):
                                _check_stop(run_id)
                                sid = invoke(prompt, sid)["session_id"]
                        break
                    except Exception as exc:
                        timed_out = getattr(exc, "code", None) == "harness.execution_timeout"
                        if (retry == TRANSIENT_SCENARIO_RETRIES or not transient_invoke_error(exc)
                                or (timed_out and retry >= TIMEOUT_SCENARIO_RETRIES)):
                            code = getattr(exc, "code", None)
                            if code not in BUDGET_STOP_CODES or not attempt.get("session_id"):
                                if not simulation.is_simulated(scenario):
                                    raise
                                scenario_failures.append({
                                    "scenario_id": attempt["scenario_id"],
                                    "retries": attempt.get("retried", "0"),
                                    **_scenario_error(exc),
                                })
                                skipped = True
                                break
                            sid = attempt["session_id"]
                            detail = getattr(exc, "detail", None)
                            budget_stops.append({
                                "scenario_id": attempt["scenario_id"], "session_id": sid,
                                "code": code, "stop_reason": str(
                                    (detail if isinstance(detail, dict) else {})
                                    .get("stop_reason") or ""),
                            })
                            break
                        _check_stop(run_id)
                        attempt["retried"] = str(retry + 1)
                if skipped:
                    _update(run_id, scenario_failures=list(scenario_failures))
                    continue
                session_ids.append(sid)
                watermark_sid = sid
                metadata_entries.extend(ground_truth_metadata([scenario], [sid]))
                _update(run_id, session_ids=list(session_ids),
                        budget_stops=list(budget_stops) or None)
            if not session_ids:
                first = scenario_failures[0] if scenario_failures else {}
                raise RuntimeError(
                    f"all {len(scenarios)} scenarios failed — first: "
                    f"{first.get('scenario_id')}: {first.get('error')}"
                )
            if session_metadata is None:
                session_metadata = metadata_entries or None
            _check_stop(run_id)
            _update(run_id, status="waiting")
            _wait_for_fresh_telemetry(
                workspace=workspace,
                session_id=watermark_sid or session_ids[-1],
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
                    log_groups=input_groups,
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
                    log_groups=input_groups,
                    session_ids=session_ids or None,
                    time_range=time_range,
                    evaluators=evaluators,
                    session_metadata=session_metadata,
                )
            )
        batch_id = response["batchEvaluationId"]
        _update(run_id, batch_eval_id=batch_id)
        _stop_batch_if_requested(run_id, data, batch_id)
        result = _poll_batch(data, batch_id)
        _finish_from_result(run_id, mode, result, workspace=workspace)
    except RunStopped:
        _update(run_id, status="stopped", error=STOP_REASON)
    except AppError as exc:
        parts = [f"{exc.code}: {exc.message}"]
        if isinstance(exc.detail, dict) and "stop_reason" in exc.detail:
            parts.append(f"stop_reason={exc.detail['stop_reason']!r}")
        parts.extend(f"{key}={value}" for key, value in attempt.items())
        _update(run_id, status="failed", error=" · ".join(parts)[:500])
    except Exception as exc:
        parts = [f"{type(exc).__name__}: {exc}"[:400]]
        parts.extend(f"{key}={value}" for key, value in attempt.items())
        _update(run_id, status="failed", error=" · ".join(parts)[:500])
    finally:
        stop_flags.clear(run_id)


def _stop_batch_if_requested(run_id: str, data: Any, batch_id: str) -> None:
    """Close the race between "no batch id yet" (the stop route could only set
    the flag) and StartBatchEvaluation having just returned: forward the stop
    to AWS now so the poller observes STOPPING → STOPPED."""
    if stop_flags.requested(run_id):
        ac.stop_batch_evaluation(data, batch_id=batch_id)


BATCH_POLL_INTERVAL_S = 30.0
STILL_RUNNING_PREFIX = "batch evaluation still"


def _batch_wait_s() -> int:
    return get_settings().eval_batch_wait_s


def _poll_batch(data: Any, batch_id: str) -> dict[str, Any]:
    """Poll one batch evaluation until it is terminal or the configured wait
    (``eval_batch_wait_s``) runs out — the same budget for every run mode."""
    polls = max(1, math.ceil(_batch_wait_s() / BATCH_POLL_INTERVAL_S))
    return ac.poll_batch_evaluation(
        data, batch_id=batch_id, max_polls=polls, interval=BATCH_POLL_INTERVAL_S
    )


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
            else {"scores": ac.parse_eval_scores(
                result, records_reader=_records_reader(workspace, result))}
        )
        _update(run_id, status="stopped", error=reason[:500], **parsed)
        return
    if status not in ac.EVAL_TERMINAL:
        # Not a failure on AWS: the batch outlived the wait. Say so, and point at
        # re-check instead of implying the evaluation itself failed.
        minutes = round(_batch_wait_s() / 60)
        raise RuntimeError(
            f"{STILL_RUNNING_PREFIX} {status} after the {minutes}-minute wait — AWS may "
            "still finish it; re-check the run to read the final result"
        )
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
        reader = _records_reader(workspace, result)
        scores = ac.parse_eval_scores(result, records_reader=reader)
        failures = (
            _session_failures(run_id, result, reader)
            if status == "COMPLETED_WITH_ERRORS" else None
        )
        _update(run_id, status="completed", scores=scores, error=error,
                session_failures=failures)


def _session_failures(run_id: str, result: dict[str, Any], reader: Any) -> dict[str, Any] | None:
    """The partial batch's skipped sessions, best-effort: an unreadable stream still
    records AWS's counters (kind ``unknown``) so the console can say how many."""
    db = SessionLocal()
    try:
        run = db.get(EvalRun, run_id)
        session_ids = list(run.session_ids or []) if run is not None else []
    finally:
        db.close()
    try:
        records = (reader() or []) if reader is not None else []
    except Exception:
        records = []
    return ac.summarize_session_failures(result, records, session_ids)


def _records_reader(workspace: WorkspaceContext | None, result: dict[str, Any]) -> Any:
    """Lazy, memoized reader of a finished batch's results-stream records — for the
    evaluators AWS summarises without an average (``parse_eval_scores``) and for the
    skipped-session summary, which then share one read."""
    location = ac.results_stream(result)
    if workspace is None or location is None:
        return None
    cache: list[list[dict[str, Any]]] = []

    def read() -> list[dict[str, Any]]:
        if not cache:
            cache.append(ac.read_result_records(workspace.client("logs"), *location))
        return cache[0]

    return read


def reconcile_run(
    run_id: str, *, mode: str, batch_id: str, workspace: WorkspaceContext
) -> None:
    """Finish a run whose in-process poller died (restart / dev reload) while
    the batch evaluation kept running server-side."""
    try:
        result = _poll_batch(data_client(workspace), batch_id)
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


def recheck_run(run_id: str, *, workspace: WorkspaceContext) -> EvalRun:
    """Re-read a failed run's batch evaluation from AWS, the source of truth.

    A run fails locally when its poller gives up or dies, while the batch may
    keep running (and finish) on AWS. A terminal batch settles the row exactly
    as the poller would have; one still running puts the row back to
    ``evaluating`` with a fresh poller. Only ``failed`` runs that started a batch
    qualify, plus partially failed ``completed`` runs that predate
    ``session_failures`` (re-finishing backfills it); 409 ``run.not_recheckable``
    otherwise. Reads only — no new
    evaluation is started, nothing is billed."""
    db = SessionLocal()
    try:
        run = db.get(EvalRun, run_id)
        if run is None:
            raise AppError("run.not_found", "run not found", status_code=404)
        # also a partially failed run finished before ``session_failures`` existed:
        # re-finishing it from the batch result backfills which sessions were skipped
        legacy_partial = (run.status == "completed" and bool(run.error)
                          and run.session_failures is None and run.mode == "evaluators")
        if (run.status != "failed" and not legacy_partial) or not run.batch_eval_id:
            raise AppError(
                "run.not_recheckable",
                "only a failed run that started a batch evaluation (or a partially "
                "failed run without its skipped-session summary) can be re-checked",
                status_code=409,
            )
        mode, batch_id = run.mode, run.batch_eval_id
    finally:
        db.close()
    data = data_client(workspace)
    result = data.get_batch_evaluation(batchEvaluationId=batch_id)
    if legacy_partial and result.get("status") not in ac.EVAL_TERMINAL:
        # a finished run never goes back to polling; nothing to backfill from yet
        raise AppError("run.not_recheckable",
                       f"batch evaluation is {result.get('status')}, not terminal",
                       status_code=409)
    if result.get("status") in ac.EVAL_TERMINAL:
        try:
            _finish_from_result(run_id, mode, result, workspace=workspace)
        except RuntimeError as exc:
            _update(run_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
    else:
        _update(run_id, status="evaluating", error=None)
        run_queue.submit(
            run_id,
            lambda: reconcile_run(run_id, mode=mode, batch_id=batch_id, workspace=workspace),
        )
    db = SessionLocal()
    try:
        return db.get(EvalRun, run_id)
    finally:
        db.close()


INTERRUPTED_STATUSES = ACTIVE_STATUSES


def run_results(run: EvalRun, *, workspace: WorkspaceContext) -> dict[str, Any]:
    """Per-session judge records of a run — score, label and the judge's
    explanation for every evaluator, grouped by session.

    The row only stores the per-evaluator averages (``run.scores``); the
    explanations live exclusively in the batch's own results log stream
    (``GetBatchEvaluation.outputConfig.cloudWatchConfig``), the same
    ``gen_ai.evaluation.result`` records the optimizer reads as evidence. Read
    on demand, never persisted — a terminal batch's stream is immutable.

    Degrades rather than raises: ``available=false`` + ``reason`` when the run
    has nothing to read (insights run, no batch, still active, stream not
    reported) or the read itself failed (``unreadable`` + ``detail``).
    Sessions come back in the run's own ``session_ids`` order (dataset order),
    then any the stream knows and the row does not.
    """
    base: dict[str, Any] = {
        "run_id": run.id,
        "batch_eval_id": run.batch_eval_id,
        "available": False,
        "sessions": [],
        "count": 0,
        "truncated": False,
    }
    if run.mode != "evaluators":
        return {**base, "reason": "insights_run"}
    if not run.batch_eval_id:
        return {**base, "reason": "no_batch"}
    if run.status in ACTIVE_STATUSES:
        return {**base, "reason": "run_active"}
    try:
        detail = ac.get_batch_evaluation(data_client(workspace), batch_id=run.batch_eval_id)
        location = ac.results_stream(detail)
        if location is None:
            return {**base, "reason": "stream_missing"}
        records = ac.read_result_records(workspace.client("logs"), *location)
    except Exception as exc:  # ClientError, network, malformed stream — all degrade
        return {**base, "reason": "unreadable", "detail": f"{type(exc).__name__}: {exc}"[:300]}

    if (run.status == "completed" and run.error and run.session_failures is None
            and detail.get("status") == "COMPLETED_WITH_ERRORS"):
        # a partial run finished before the summary existed: backfill it from the
        # records just read (no extra AWS call)
        _update(run.id, session_failures=ac.summarize_session_failures(
            detail, records, list(run.session_ids or [])))
    by_session: dict[str, list[dict[str, Any]]] = {}
    for attrs in records:
        sid = attrs.get("session.id")
        if not sid:
            continue
        by_session.setdefault(str(sid), []).append(ac.normalize_result_record(attrs))
    ordered = [sid for sid in (run.session_ids or []) if sid in by_session]
    ordered += [sid for sid in by_session if sid not in set(ordered)]
    return {
        **base,
        "available": True,
        "sessions": [{"session_id": sid, "results": by_session[sid]} for sid in ordered],
        "count": len(records),
        "truncated": len(records) >= ac.RESULT_RECORDS_MAX,
    }


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
    agent: Agent | None,
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
    name: str | None = None,
    description: str | None = None,
    log_source: dict[str, Any] | None = None,
) -> EvalRun:
    """Queue one run. The telemetry comes from the platform ``agent`` — or, with
    ``agent=None``, from ``log_source`` {service_name, log_group_names}: an agent
    that is not a platform agent (off-runtime, or no ledger row) whose spans and
    content logs are already in CloudWatch. Such a run is passive only (session
    ids or a time window); there is nothing to invoke."""
    if agent is None:
        if not log_source or dataset_items:
            raise ValueError("an agent-less run needs a log_source and a passive scope")
        service_name = log_source["service_name"]
        log_groups = list(log_source["log_group_names"])
        log_group = log_groups[0]
    else:
        if dataset_items and agent.inbound_auth_mode == "jwt":
            # replay turns Bearer-invoke with the workspace M2M token; an agent on
            # another IdP refuses it, so say so before a run row is created
            from app.services import inbound_auth as inbound_auth_service

            inbound_auth_service.require_platform_reachable(agent, workspace, "m2m")
        service_name, log_group = resolve_telemetry(agent, workspace)
        log_groups = ["aws/spans", log_group]
    # Window runs have no dataset; encode the scope in dataset_name so the
    # runs list can render "window · Nh" without a schema change.
    if lookback_hours and not dataset_name:
        dataset_name = f"window:{lookback_hours}h"
    db = SessionLocal()
    try:
        run = EvalRun(
            workspace_id=agent.workspace_id if agent else workspace.id,
            agent_id=agent.id if agent else "",
            agent_name=agent.name if agent else service_name[:64],
            log_source=None if agent else {
                "service_name": service_name, "log_group_names": log_groups,
            },
            name=name,
            description=description,
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
        agent_arn = agent.arn if agent else ""
        agent_ledger_id = agent.id if agent else None
        agent_method = agent.method if agent else ""
        agent_protocol = ((agent.spec or {}) if agent else {}).get("protocol") or "http"
        # Gateway-tool agents need a runtimeUserId or the Runtime injects no
        # workload token and the eval run measures a tool-less agent.
        agent_runtime_user = gateway_support.runtime_user_id(agent.spec) if agent else None
        # JWT-inbound runtimes reject SigV4 — the run Bearer-invokes with the
        # workspace M2M token (fails with a named error if unconfigured).
        agent_inbound_jwt = bool(agent) and agent.inbound_auth_mode == "jwt"
    finally:
        db.close()

    # The snapshot is taken HERE, before the callable is enqueued: a dataset
    # edit that lands while the run waits in the queue must not change what
    # it replays, so the queued callable never touches the caller's list.
    items_snapshot = copy.deepcopy(dataset_items)
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
            log_groups=log_groups,
            items=items_snapshot,
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
            agent_id=agent_ledger_id,
            inbound_jwt=agent_inbound_jwt,
        ),
    )
    _update(run_id, queue_position=position)
    db = SessionLocal()
    try:
        return db.get(EvalRun, run_id)
    finally:
        db.close()
