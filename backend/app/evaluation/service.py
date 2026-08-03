"""Evaluation run orchestration (adapted from agentcore_eva_opt routers/runs.py
and routers/insights.py — github.com/xiehust/agentcore_eva_opt).

Pipeline per run (behind the account lock):
    invoking   — one runtime session per dataset item
    waiting    — traces land in CloudWatch (aws/spans)
    evaluating — StartBatchEvaluation scoped to exactly those sessions
    completed  — per-evaluator average scores (or insight trees)

Batch evaluation reads CloudWatch traces. Runtime-backed agents (zip_runtime /
studio / container) derive their span service name from the runtime; managed
harnesses run on an internal Strands runtime that emits
``service.name = "harness_{harnessName}.DEFAULT"`` with the
evaluation-parseable ``strands.telemetry.tracer`` scope (live-probed
2026-07-13) — the backing runtime id differs from the harnessId, so the
content-log group is discovered by log-group prefix instead of derived.
"""

import time
from typing import Any

import boto3

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.evaluation import agentcore_eval as ac
from app.evaluation import simulation, telemetry
from app.evaluation.models import EvalRun
from app.evaluation.queue import account_lock
from app.evaluation.scenarios import (
    ground_truth_metadata,
    normalize_scenarios,
    scenario_prompts,
)
from app.models.ledger import Agent
from app.services.agentcore import harness as hc
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client, data_client

EVAL_SUPPORTED_METHODS = {"zip_runtime", "studio", "container", "harness"}
TELEMETRY_READY_GRACE_SECONDS = 120
TELEMETRY_QUERY_LOOKBACK_MS = 60_000


def _harness_telemetry(agent: Agent, logs_client: Any = None) -> tuple[str, str]:
    """Harness span identity: the harnessId is ``{harnessName}-{suffix}`` and the
    managed backing runtime emits ``harness_{harnessName}.DEFAULT``. Its log
    group carries the BACKING runtime's own id (≠ harnessId) — discover it by
    prefix; a re-created harness leaves stale groups behind, so newest wins."""
    base = agent.resource_id.rsplit("-", 1)[0]
    prefix = f"/aws/bedrock-agentcore/runtimes/harness_{base}-"
    logs = logs_client or boto3.client("logs", region_name=get_settings().region)
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


def resolve_telemetry(agent: Agent, logs_client: Any = None) -> tuple[str, str]:
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
        return _harness_telemetry(agent, logs_client)
    detail = rt.get_runtime(control_client(), agent.resource_id)
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
    session_id: str,
    content_log_group: str,
    start_time_ms: int,
    stability_seconds: int,
) -> None:
    logs = boto3.client("logs", region_name=get_settings().region)
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
) -> None:
    """Drive one evaluation run to completion (runs inside the account lock).

    Scope is one of: dataset ``items`` (invoke fresh sessions), explicit
    ``existing_session_ids``, or a passive ``time_range`` window over the
    agent's past traffic — the window path skips invoke/wait entirely."""
    telemetry_start_ms = int(time.time() * 1000) - TELEMETRY_QUERY_LOOKBACK_MS
    try:
        data = data_client()
        session_ids = list(existing_session_ids or [])
        if not session_ids and not time_range:
            # One session per scenario. Predefined scenarios replay their turns
            # sequentially in that session; simulated persona scenarios run the
            # SDK's LLM-actor loop (actor_model_id plays the user). Ground
            # truth (assertions / expected trajectory / expected responses)
            # rides along as sessionMetadata.
            scenarios = normalize_scenarios(items)
            _update(run_id, status="invoking")
            for scenario in scenarios:
                sid: str | None = None
                if simulation.is_simulated(scenario):
                    sid = simulation.run_simulated_scenario(
                        data,
                        agent_arn=agent_arn,
                        method=method,
                        scenario=scenario,
                        actor_model_id=actor_model_id or "",
                        protocol=protocol,
                    )
                else:
                    for prompt in scenario_prompts(scenario):
                        if method == "harness":  # InvokeHarness, not the runtime data plane
                            result = hc.invoke_harness_text(data, agent_arn, prompt, session_id=sid)
                        elif protocol == "a2a":  # JSON-RPC runtimes reject {prompt}
                            result = rt.invoke_a2a_text(data, agent_arn, prompt, session_id=sid)
                        else:
                            result = rt.invoke_runtime_text(data, agent_arn, prompt, session_id=sid)
                        sid = result["session_id"]
                session_ids.append(sid)
                _update(run_id, session_ids=list(session_ids))
            if session_metadata is None:
                session_metadata = ground_truth_metadata(scenarios, session_ids) or None
            _update(run_id, status="waiting")
            _wait_for_fresh_telemetry(
                session_id=session_ids[-1],
                content_log_group=log_group,
                start_time_ms=telemetry_start_ms,
                stability_seconds=wait_seconds,
            )

        _update(run_id, status="evaluating", session_ids=session_ids)
        if mode == "insights":
            response = ac.start_insights_evaluation(
                data,
                name=f"run_{run_id[:8]}",
                service_name=service_name,
                log_groups=["aws/spans", log_group],
                session_ids=session_ids or None,
                time_range=time_range,
                insights=insights,
            )
        else:
            response = ac.start_batch_evaluation(
                data,
                name=f"run_{run_id[:8]}",
                service_name=service_name,
                log_groups=["aws/spans", log_group],
                session_ids=session_ids or None,
                time_range=time_range,
                evaluators=evaluators,
                session_metadata=session_metadata,
            )
        batch_id = response["batchEvaluationId"]
        _update(run_id, batch_eval_id=batch_id)
        # Insights cluster across sessions and routinely run 15-25 minutes;
        # give them a 30-minute budget instead of the evaluator default.
        if mode == "insights":
            result = ac.poll_batch_evaluation(
                data, batch_id=batch_id, max_polls=60, interval=30.0
            )
        else:
            result = ac.poll_batch_evaluation(data, batch_id=batch_id, max_polls=60)
        _finish_from_result(run_id, mode, result)
    except Exception as exc:
        _update(run_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:500])


def _finish_from_result(run_id: str, mode: str, result: dict[str, Any]) -> None:
    """Write a terminal batch-evaluation result back onto the run row.

    COMPLETED_WITH_ERRORS still completes the run, but the service's
    errorDetails (e.g. "insufficient samples for clustering") are surfaced in
    the error column so the UI can show why results are partial/empty."""
    status = result.get("status")
    if status not in ("COMPLETED", "COMPLETED_WITH_ERRORS"):
        raise RuntimeError(f"batch evaluation ended {status}")
    details = result.get("errorDetails") or []
    error = "; ".join(str(d) for d in details)[:500] or None
    if mode == "insights":
        _update(run_id, status="completed", insights=ac.parse_insights(result),
                error=error)
    else:
        _update(run_id, status="completed", scores=ac.parse_eval_scores(result),
                error=error)


def reconcile_run(run_id: str, *, mode: str, batch_id: str) -> None:
    """Finish a run whose in-process poller died (restart / dev reload) while
    the batch evaluation kept running server-side."""
    try:
        result = ac.poll_batch_evaluation(data_client(), batch_id=batch_id, max_polls=60)
        _finish_from_result(run_id, mode, result)
    except Exception as exc:
        _update(run_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:500])


INTERRUPTED_STATUSES = ("queued", "invoking", "waiting", "evaluating")


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
                account_lock.submit(
                    run.id,
                    lambda rid=run.id, m=run.mode, b=run.batch_eval_id: reconcile_run(
                        rid, mode=m, batch_id=b
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
) -> EvalRun:
    service_name, log_group = resolve_telemetry(agent)
    # Window runs have no dataset; encode the scope in dataset_name so the
    # runs list can render "window · Nh" without a schema change.
    if lookback_hours and not dataset_name:
        dataset_name = f"window:{lookback_hours}h"
    db = SessionLocal()
    try:
        run = EvalRun(
            agent_id=agent.id,
            agent_name=agent.name,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
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
    finally:
        db.close()

    position = account_lock.submit(
        run_id,
        lambda: execute_run(
            run_id,
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
        ),
    )
    _update(run_id, queue_position=position)
    db = SessionLocal()
    try:
        return db.get(EvalRun, run_id)
    finally:
        db.close()
