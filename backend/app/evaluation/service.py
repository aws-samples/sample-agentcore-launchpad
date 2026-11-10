"""Evaluation run orchestration on the evaluation worker (N12 E1-4; plan §B.0).

Pipeline per run (executed by the bounded-concurrency run queue):
    queued      — a run-queue slot (bounds the jobs dispatched at once)
    invoking    — the worker replays one runtime session per dataset scenario
    waiting     — the worker waits for the sessions' spans to land in CloudWatch
    evaluating  — the worker judges every session; results.jsonl is ingested as it grows
    completed   — per-evaluator average scores (or insight trees) from done.json
    failed      — the job failed, was lost, or outlived the configured wait
    stopped     — operator stop: cancelled locally while queued, else the cancel marker
                  (+ StopRuntimeSession after a grace period); partial scores kept

The backend plans the job (session ids, arms from the routing truth, ground truth,
evaluator definitions, log groups) and ships it as one ``JobSpec`` per shard; the worker
only executes. The three middle statuses mirror ``status.json.phase``; ``EvalRun.
batch_eval_id`` is the job id (the run id for a sharded run). ``eval_results`` rows are the
Global results-stream twin and the source of ``GET /runs/{id}/results``.
"""

from __future__ import annotations

import copy
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace
from typing import Any

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError, redacted_error
from app.evaluation import (
    evaluators_local,
    extraction_refs,
    jobs,
    judge_budget,
    results_store,
    run_coverage,
    run_failure,
    run_stats,
    scorecard,
)
from app.evaluation.executor import assert_executor_ready, select_executor
from app.evaluation.jobs import JobDone, JobSpec
from app.evaluation.models import EvalRun
from app.evaluation.queue import run_queue
from app.evaluation.scenarios import (
    input_preview,
    item_splits,
    items_fingerprint,
    normalize_scenarios,
    scenario_prompts,
)
from app.models.ledger import Agent
from app.services import creators, eval_tokens, runtime_sessions
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client
from app.services.strands_eval import deadlines, extraction_check
from app.services.strands_eval import runtime_invoke as _upstream
from app.services.strands_eval.engine_core import (
    ground_truth_from_scenarios,
    is_skill_evaluator,
)
from app.services.strands_eval.results import aggregate_scores
from app.services.workspace import WorkspaceContext, context_for_workspace
from app.templates import gateway_support

logger = logging.getLogger(__name__)

# byoc: telemetry identity derivation is method-agnostic for runtimes; whether
# the user's code emits gen_ai spans for the evaluator to read is theirs.
EVAL_SUPPORTED_METHODS = {"zip_runtime", "studio", "container", "harness", "byoc"}
SPANS_LOG_GROUP = "aws/spans"

# Ledger statuses a run can still be stopped from; everything else is terminal.
ACTIVE_STATUSES = ("queued", "invoking", "waiting", "evaluating")
INTERRUPTED_STATUSES = ACTIVE_STATUSES
# N17-obs450: the run error shapes live with their parser (`run_failure.classify`)
STOP_REASON = run_failure.STOP_REASON
STILL_RUNNING_PREFIX = run_failure.STILL_RUNNING_PREFIX
MAX_ATTEMPTS = 3
POLL_INTERVAL_S = 15.0  # status.json is written ≤ every 10 s on the worker
STOP_GRACE_S = 60.0  # how long a cancel marker may take before StopRuntimeSession
# N17-obs123 (cn/r17-t): the poller's artefact-store reads (`status.json`, `results.jsonl`,
# `done.json`) retry a TRANSIENT fault (S3 SlowDown / InternalError / a timeout — the one
# shared predicate `runtime_invoke.is_upstream_transient`) with this bounded backoff before
# the run fails; the worker job runs on regardless, so a persistent fault fails the run WITH
# the re-check hint (`recheck_run` re-attaches by `batch_eval_id`).
STORE_READ_BACKOFF_S: tuple[float, ...] = (1.0, 3.0, 5.0)
STORE_READ_FAILED_PREFIX = "evaluation job artefacts unreadable"
RESULT_RECORDS_MAX = 5000
_PHASE_ORDER = {"invoking": 0, "waiting": 1, "evaluating": 2}

_sleep = time.sleep  # injectable for tests
_now = time.time  # injectable for tests (stale / grace / deadline arithmetic)


class RunStopped(Exception):
    """The operator asked for the run to stop before its job was dispatched."""


class _StopFlags:
    """Operator stop requests, in-memory: the poller sees the flag at its next tick and
    cancels the job; a run that was never dequeued is settled by ``request_stop`` itself."""

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
    detail = rt.get_runtime(control_client(workspace), agent.resource_id)
    runtime_name = detail["agentRuntimeName"]
    return f"{runtime_name}.DEFAULT", (
        f"/aws/bedrock-agentcore/runtimes/{agent.resource_id}-DEFAULT"
    )


def _live_snapshot_seq(agent_id: str | None) -> int | None:
    """The agent's newest deployed snapshot seq, read now (None without an agent)."""
    if not agent_id:
        return None
    db = SessionLocal()
    try:
        return scorecard.newest_deployed_seq(db, agent_id)
    finally:
        db.close()


def _update(run_id: str, **fields: Any) -> None:
    db = SessionLocal()
    try:
        run = db.get(EvalRun, run_id)
        if run is None:
            return
        for key, value in fields.items():
            setattr(run, key, value)
        db.commit()
    finally:
        db.close()


def _get(run_id: str) -> EvalRun | None:
    db = SessionLocal()
    try:
        return db.get(EvalRun, run_id)
    finally:
        db.close()


# ─── planning (the backend's half: sessions, arms, ground truth, evaluators) ──────
@dataclass
class RunPlan:
    """Everything the worker needs, pre-computed by the backend (plan §B.0 ``spec``)."""

    run_id: str
    job_type: str  # batch_eval | insights
    target: dict[str, Any]
    sessions: list[dict[str, Any]]
    replay: bool
    wait: dict[str, Any]
    evaluator_defs: list[dict[str, Any]] = field(default_factory=list)
    ground_truth: dict[str, dict[str, Any]] = field(default_factory=dict)
    judge_model_id: str | None = None
    actor_model_id: str | None = None
    kinds: list[str] = field(default_factory=list)
    # N17-obs449: the insights FailureAnalysis judge (the `failure_detection` model role at
    # submit); None for a batch run or an insights run without FailureAnalysis
    failure_detection_model_id: str | None = None
    service_name: str = ""
    # upstream #289 (scorecards): the platform agent the run evaluates (None for a
    # log-source run) and, for a replay, session → the scenario it replays
    # (`EvalRun.scenario_map`: {session_id: {scenario_id, input}}); neither is shipped
    # to the worker (`spec()`), both are written on the row when the run is dispatched
    agent_id: str | None = None
    scenario_map: dict[str, dict[str, str]] | None = None
    # eval accuracy item 1: {session_id: reference} from the chat ledger for the sessions a
    # passive run evaluates (a replay collects its own); shipped per shard in the spec
    extraction_references: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def session_ids(self) -> list[str]:
        return [str(s["session_id"]) for s in self.sessions]

    def spec(self, sessions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        chosen = self.sessions if sessions is None else sessions
        spec = {
            "run_id": self.run_id, "target": dict(self.target), "sessions": list(chosen),
            "wait": dict(self.wait), "evaluator_defs": list(self.evaluator_defs),
            "ground_truth": {s["session_id"]: self.ground_truth[s["session_id"]]
                             for s in chosen if s["session_id"] in self.ground_truth},
            "judge_model_id": self.judge_model_id, "replay": self.replay,
            # N17-obs385: the model a judge out of output budget is re-tried on
            "judge_fallback_model_id": judge_budget.judge_fallback_model_id(),
            "judge_deadline_s": get_settings().eval_judge_deadline_s,  # P2.2
            # P2.2: the run's own wall-clock bound — the worker job ends before the hub gives up
            "run_deadline_s": deadlines.run_deadline_s(get_settings().eval_batch_wait_s,
                                                       get_settings().eval_judge_deadline_s),
            "service_name": self.service_name,
        }
        if self.actor_model_id:
            spec["actor_model_id"] = self.actor_model_id
        if self.failure_detection_model_id:
            spec["failure_detection_model_id"] = self.failure_detection_model_id
        refs = {s["session_id"]: self.extraction_references[s["session_id"]]
                for s in chosen if s["session_id"] in self.extraction_references}
        if refs and self.job_type == "batch_eval":
            spec["extraction_references"] = refs
        if self.job_type == "insights":
            spec["kinds"] = list(self.kinds)
        return spec

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assign_sessions(agent_id: str | None, session_ids: list[str]) -> dict[str, dict[str, Any]]:
    """``{session_id: {arm, qualifier, baggage, launchpad_meta}}`` from the routing truth
    (W-E3's ``optimization.routing.assign_batch``) when the agent has an active canary;
    ``{}`` otherwise — the worker then replays with the DEFAULT endpoint and no
    bundle pointer. Resolved lazily so E1 does not depend on E3's module shape."""
    if not agent_id or not session_ids:
        return {}
    try:
        from app.optimization import routing  # type: ignore[attr-defined]
    except ImportError:
        return {}
    active = getattr(routing, "active_owner", None)
    assign = getattr(routing, "assign_batch", None)
    if active is None or assign is None:
        return {}
    db = SessionLocal()
    try:
        owner = active(db, agent_id)
        if not owner:
            return {}
        assigned = assign(owner[1] if isinstance(owner, tuple) else owner, list(session_ids))
    finally:
        db.close()
    out: dict[str, dict[str, Any]] = {}
    for sid, assignment in (assigned or {}).items():
        if hasattr(assignment, "__dict__") and not isinstance(assignment, dict):
            assignment = dict(vars(assignment))
        out[str(sid)] = {
            "arm": assignment.get("arm"), "qualifier": assignment.get("qualifier"),
            "baggage": assignment.get("baggage"),
            "launchpad_meta": assignment.get("launchpad_meta"),
        }
    return out


def discover_sessions(
    workspace: WorkspaceContext, *, service_name: str, log_groups: list[str],
    time_range: dict[str, Any], limit: int,
) -> list[str]:
    """Session ids of the agent's traffic in ``time_range`` (window / online-report scopes):
    the worker evaluates explicit sessions, so the window is resolved here, once."""
    from app.services import observability

    start, end = time_range["startTime"], time_range["endTime"]
    hours = max(1, math.ceil((end - start).total_seconds() / 3600))
    service = service_name.replace('"', "")
    query = (
        "fields @timestamp | filter ispresent(attributes.session.id)"
        + (f' and resource.attributes.service.name = "{service}"' if service else "")
        + " | stats max(@timestamp) as last by attributes.session.id as session_id"
        f" | sort last desc | limit {int(limit)}"
    )
    rows = observability.run_insights_queries(
        {"sessions": query}, hours, workspace=workspace, log_groups=list(log_groups),
    )["sessions"]
    return [str(r["session_id"]) for r in rows if r.get("session_id")]


def _no_sessions_error(lookback_hours: int | None) -> AppError:
    window = f"the last {lookback_hours} h" if lookback_hours else "the selected window"
    return AppError(
        "run.no_sessions",
        f"no session of this agent was recorded in {window} — an insights run needs at "
        "least one session to analyse; widen the window or chat with the agent first",
        {"lookback_hours": lookback_hours},
        status_code=409,
    )


def plan_run(
    run_id: str,
    *,
    workspace: WorkspaceContext,
    agent_arn: str,
    agent_id: str | None,
    protocol: str,
    service_name: str,
    log_group: str,
    log_groups: list[str],
    items: list[dict[str, Any]],
    evaluators: list[str],
    mode: str,
    wait_seconds: int,
    existing_session_ids: list[str] | None,
    time_range: dict[str, Any] | None,
    insights: list[str] | None,
    actor_model_id: str | None,
    runtime_user_id: str | None,
    judge_model_id: str | None = None,
    failure_detection_model_id: str | None = None,
) -> RunPlan:
    settings = get_settings()
    scenarios = normalize_scenarios(items) if items else []
    sessions: list[dict[str, Any]] = []
    ground_truth: dict[str, dict[str, Any]] = {}
    replay = bool(scenarios)
    scenario_map: dict[str, dict[str, str]] = {}
    if scenarios:
        ids = [rt.new_session_id() for _ in scenarios]
        assignments = assign_sessions(agent_id, ids)
        ground_truth = ground_truth_from_scenarios(scenarios, ids)
        splits = item_splits(scenarios)
        for scenario, sid, split in zip(scenarios, ids, splits, strict=True):
            scenario_map[sid] = {"scenario_id": str(scenario.get("scenario_id") or ""),
                                 "input": input_preview(scenario),
                                 # D3-d / R3-c: tuning readers drop held-out sessions by it
                                 "split": split,
                                 # D3-c: the turns the scenario defines (a persona's are
                                 # driven by the actor: null); `turns_replayed` lands at finish
                                 "turns_defined": None if "actor_profile" in scenario
                                 else len(scenario_prompts(scenario))}
            if isinstance(scenario.get("expected_trajectory"), list) \
                    and scenario["expected_trajectory"]:
                # N17-obs407: what `_finish` compares with the tools the spans show
                scenario_map[sid]["expected_tools"] = [
                    str(n) for n in scenario["expected_trajectory"]]
            entry: dict[str, Any] = {
                "session_id": sid, "scenario_id": str(scenario.get("scenario_id") or ""),
                "prompts": scenario_prompts(scenario), "replay": True,
                **{"arm": None, "qualifier": None, "baggage": None, "launchpad_meta": None},
                **assignments.get(sid, {}),
            }
            if "actor_profile" in scenario:
                entry.update({
                    "actor_profile": dict(scenario.get("actor_profile") or {}),
                    "input": str(scenario.get("input") or ""),
                    "max_turns": int(scenario.get("max_turns") or 10),
                })
            sessions.append(entry)
    elif existing_session_ids:
        # per-user policy: a governed conversation's spans live under its derived
        # per-principal runtime sessions (`lpp-…`) — read those (unchanged without any)
        db = SessionLocal()
        try:
            read_ids = runtime_sessions.expand_session_ids(
                db, workspace.id, [str(s) for s in existing_session_ids], agent_id=agent_id)
        finally:
            db.close()
        sessions = [{"session_id": str(sid), "scenario_id": None, "prompts": [], "replay": False,
                     "arm": None, "qualifier": None, "baggage": None, "launchpad_meta": None}
                    for sid in read_ids]
    elif time_range:
        found = discover_sessions(workspace, service_name=service_name, log_groups=log_groups,
                                  time_range=time_range, limit=settings.eval_job_max_sessions)
        if mode == "insights" and not found:
            # `submit_run` already resolved the window for the API path; a caller that hands
            # a bare window straight to the plan fails here, before any job is dispatched
            # (the row reads `failed "run.no_sessions: …"`).
            raise _no_sessions_error(None)
        sessions = [{"session_id": sid, "scenario_id": None, "prompts": [], "replay": False,
                     "arm": None, "qualifier": None, "baggage": None, "launchpad_meta": None}
                    for sid in found]
    evaluator_defs: list[dict[str, Any]] = []
    references: dict[str, dict[str, Any]] = {}
    if mode == "evaluators":
        # P7.1: the definitions the run was submitted with — an edit while it waited in the
        # queue changes nothing; a run without a snapshot resolves them now, as before
        submitted = snapshot_definitions(_get(run_id))
        db = SessionLocal()
        try:
            evaluator_defs = submitted if submitted is not None else (
                evaluators_local.resolve_definitions(db, workspace, evaluators))
        finally:
            db.close()
        if not replay:
            references = extraction_refs.references_for(
                workspace.id, [str(s["session_id"]) for s in sessions])
    return RunPlan(
        run_id=run_id,
        job_type="insights" if mode == "insights" else "batch_eval",
        target={"runtime_arn": agent_arn, "protocol": protocol, "runtime_user_id": runtime_user_id},
        sessions=sessions,
        replay=replay,
        wait={
            "seconds": int(wait_seconds), "log_groups": list(log_groups),
            "content_log_group": log_group,
            "spans_source": settings.observability_spans_source,
            "lookback_hours": max(24, settings.eval_batch_wait_s // 3600 + 24),
        },
        evaluator_defs=evaluator_defs,
        ground_truth=ground_truth,
        # P3.2: the run's own judge (stamped on the row at submit); the setting is the
        # fallback for a caller that plans without one
        judge_model_id=judge_model_id or settings.eval_judge_model_id,
        actor_model_id=actor_model_id,
        kinds=list(insights or evaluators_local.INSIGHT_TYPES) if mode == "insights" else [],
        failure_detection_model_id=failure_detection_model_id if mode == "insights" else None,
        service_name=service_name,
        agent_id=agent_id,
        scenario_map=scenario_map or None,
        extraction_references=references,
    )


def _shards(plan: RunPlan) -> list[list[dict[str, Any]]]:
    settings = get_settings()
    ranges = jobs.plan_shards(
        len(plan.sessions), max(1, len(plan.evaluator_defs)),
        judge_concurrency=settings.eval_judge_concurrency, budget_s=settings.eval_job_budget_s,
        max_sessions=settings.eval_job_max_sessions,
    )
    if len(ranges) <= 1:
        return [list(plan.sessions)]
    return [plan.sessions[start:end] for start, end in ranges]


def _job_spec(plan: RunPlan, job_id: str, sessions: list[dict[str, Any]], *,
              workspace: WorkspaceContext, attempt: int = 1) -> JobSpec:
    bucket = str((workspace.resources or {}).get("artifacts_bucket") or "")
    return JobSpec(
        job_id=job_id, job_type=plan.job_type, attempt=attempt,
        workspace={"id": workspace.id, "account_id": workspace.account_id,
                   "region": workspace.region},
        sink={"bucket": bucket, "prefix": jobs.job_prefix(job_id, attempt)},
        spec=plan.spec(sessions), deadline_s=get_settings().eval_job_budget_s,
    )


# ─── execution: dispatch + poll ──────────────────────────────────────────────────
@dataclass
class _Tracked:
    job: JobSpec
    sessions: list[dict[str, Any]]
    ingested: int = 0
    done: JobDone | None = None
    dispatched_at: float = field(default_factory=lambda: _now())


def _usage_after_failure(run_id: str, workspace: Any, scope: dict[str, Any]) -> None:
    executor, tracked = scope.get("executor"), scope.get("tracked") or []
    if executor is not None and tracked:
        record_usage_on_failure(run_id, workspace=workspace, executor=executor,
                                job_ids=[t.job.job_id for t in tracked])


def execute_run(run_id: str, *, workspace: WorkspaceContext, plan: RunPlan) -> None:
    """Dispatch the run's job(s) to the executor and drive the row to a terminal status
    (runs on a run-queue worker). Never raises."""
    try:
        _check_stop(run_id)
        _check_agent_present(run_id, plan)  # N17-obs18: never dispatch against a deleted agent
        executor = select_executor(get_settings(), job_type=plan.job_type)
        shards = _shards(plan)
        tracked: list[_Tracked] = []
        batch_id = run_id
        for index, sessions in enumerate(shards):
            job_id = run_id if len(shards) == 1 else jobs.shard_job_id(run_id, index)
            tracked.append(_Tracked(job=_job_spec(plan, job_id, sessions, workspace=workspace),
                                    sessions=sessions))
        # the version this replay invokes, read again now: a deploy that finished while
        # the run waited in the queue is what runs (submit_run's stamp stays when there is
        # no deployed snapshot); the scenario map is known up front — the worker replays
        # the planned sessions, and a session that fails is listed in `session_failures`
        live_seq = _live_snapshot_seq(plan.agent_id) if plan.replay else None
        _update(run_id, status="invoking" if plan.replay else "evaluating",
                batch_eval_id=batch_id, session_ids=plan.session_ids,
                worker_release=worker_release_tag(),  # P7.6
                **({"agent_snapshot_seq": live_seq} if live_seq is not None else {}),
                **({"scenario_map": dict(plan.scenario_map)} if plan.scenario_map else {}))
        # P8.4: the run's token budget, split over its shards
        share = shard_token_budget(get_settings().eval_run_token_budget, len(tracked))
        for item in tracked:
            if share:
                item.job.spec["token_budget"] = share
        _prepare_tokens(run_id, workspace, plan, tracked)
        for item in tracked:
            _check_stop(run_id)
            executor.submit(workspace, item.job)
            item.dispatched_at = _now()
        _poll(run_id, workspace=workspace, executor=executor, tracked=tracked, plan=plan)
    except RunStopped:
        _update(run_id, status="stopped", error=STOP_REASON)
    except AppError as exc:
        _update(run_id, status="failed", error=f"{exc.code}: {exc.message}"[:500])
        _usage_after_failure(run_id, workspace, locals())
    except Exception as exc:  # noqa: BLE001 — the row carries the failure
        _update(run_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
        _usage_after_failure(run_id, workspace, locals())
    finally:
        _discard_tokens(workspace, locals().get("tracked") or [])
        stop_flags.clear(run_id)


def _prepare_tokens(run_id: str, workspace: WorkspaceContext, plan: RunPlan,
                    tracked: list[_Tracked]) -> None:
    """D5 (I9 v2, amendment 10): a replay of a governed agent hands each job ONE hub-signed
    ``System::"eval"`` token before the job is dispatched — never the launching user's
    identity (evaluation traffic is exempt from policy). Inert with the switch off."""
    if not get_settings().policy_gateway_interceptor or not plan.replay or not plan.agent_id:
        return
    db = SessionLocal()
    try:
        agent = db.get(Agent, plan.agent_id)
    finally:
        db.close()
    if agent is None:
        return
    for item in tracked:
        eval_tokens.prepare(workspace, agent, item.job, channel="eval",
                            owner_kind=eval_tokens.OWNER_EVAL_RUN, owner_id=run_id)


def _discard_tokens(workspace: WorkspaceContext, tracked: list[_Tracked]) -> None:
    """The run's watcher is done with its jobs: their token files go (I9: terminal state)."""
    if not tracked:
        return
    try:
        eval_tokens.discard_jobs(workspace, [t.job.job_id for t in tracked])
    except Exception:  # noqa: BLE001 — the startup sweep retries; tokens expire in 900 s
        logger.warning("eval tokens of run jobs not discarded")


def _check_agent_present(run_id: str, plan: RunPlan) -> None:
    """N17-obs18 belt: the run worker re-reads its agent at dispatch — a row deleted or gone
    while the run waited in the queue fails the run with ``agent.not_found`` BEFORE the
    status flips to ``invoking`` and before any version stamp, so no scorecard / version row
    is ever recorded for a deleted agent (the route refuses the delete first, §1.3; this
    catches a delete that landed anyway). Log-source runs have no agent and pass."""
    if not plan.agent_id:
        return
    db = SessionLocal()
    try:
        agent = db.get(Agent, plan.agent_id)
    finally:
        db.close()
    if agent is None or agent.status == "deleted":
        raise AppError("agent.not_found", "the agent was deleted before the run dispatched",
                       status_code=404)


def _read_store(what: str, read: Callable[[], Any]) -> Any:
    """One artefact-store read of the poller, retried on a transient fault (N17-obs123): the
    shared `is_upstream_transient` decides, `STORE_READ_BACKOFF_S` bounds the attempts (one more
    than its length) through the injectable `_sleep`. A fault that outlives the backoff raises a
    `RuntimeError` naming the artefact, the redacted cause and the re-check hint — `execute_run`
    lands it on the row as `failed`; a non-transient fault (AccessDenied) propagates at once."""
    for attempt in range(len(STORE_READ_BACKOFF_S) + 1):
        try:
            return read()
        except Exception as exc:  # noqa: BLE001 — classified below, re-raised when not retried
            if not _upstream.is_upstream_transient(exc):
                raise
            if attempt >= len(STORE_READ_BACKOFF_S):
                raise RuntimeError(
                    f"{STORE_READ_FAILED_PREFIX} ({what}) after {attempt + 1} attempts: "
                    f"{redacted_error(exc)} — the worker may still finish the job; re-check "
                    "the run to read the final result"
                ) from exc
            _sleep(_upstream.backoff_for(STORE_READ_BACKOFF_S, attempt))
    raise AssertionError("unreachable")  # pragma: no cover


def _poll(
    run_id: str, *, workspace: WorkspaceContext, executor: Any, tracked: list[_Tracked],
    plan: RunPlan | None,
) -> None:
    """Mirror the jobs' ``status.json`` onto the row, ingest ``results.jsonl`` incrementally,
    re-dispatch stale jobs (``attempt+1``, ≤ ``MAX_ATTEMPTS``), honour the operator stop, and
    settle the row from the ``done.json`` files — within ``eval_batch_wait_s``."""
    settings = get_settings()
    deadline = _now() + settings.eval_batch_wait_s
    cancel_requested_at: float | None = None
    mode = "insights" if tracked and tracked[0].job.job_type == "insights" else "evaluators"
    while True:
        now = _now()
        if stop_flags.requested(run_id) and cancel_requested_at is None:
            for item in tracked:
                if item.done is None:
                    executor.cancel(workspace, item.job.job_id)
            cancel_requested_at = now
        for item in tracked:
            if item.done is not None:
                continue
            status = _read_store("status.json",
                                 partial(executor.status, workspace, item.job.job_id))
            _ingest(run_id, workspace, executor, item)
            if status is not None and status.state in jobs.TERMINAL_STATES:
                item.done = _read_store("done.json",
                                        partial(executor.done, workspace, item.job.job_id))
                # N17-obs413: the finished attempt may be an EARLIER one than the one the
                # offset counted — read its results whole once (ids dedupe on upsert)
                item.ingested = 0
                _ingest(run_id, workspace, executor, item)
                continue
            if cancel_requested_at is not None:
                if now - cancel_requested_at >= STOP_GRACE_S:
                    stop_session = getattr(executor, "stop_session", None)
                    if stop_session is not None:
                        stop_session(workspace, item.job.job_id)
                    item.done = JobDone(state="STOPPED", error=STOP_REASON,
                                        result_count=item.ingested)
                continue
            stale = jobs.is_stale(status, now=datetime.now(UTC), stale_s=settings.eval_job_stale_s)
            if stale and now - item.dispatched_at > settings.eval_job_stale_s:
                if plan is None or item.job.attempt >= MAX_ATTEMPTS:
                    message = (status.progress.get("message") if status else None) or "no status"
                    item.done = JobDone(
                        state="FAILED", result_count=item.ingested,
                        error=run_failure.job_lost_text(item.job.job_id, item.job.attempt,
                                                        message),
                    )
                    continue
                _redispatch(run_id, workspace, executor, item, plan)
        _mirror_phase(run_id, workspace, executor, tracked)
        if get_settings().policy_gateway_interceptor:
            # D5 (I9 v2): re-sign a running job's token, delete a finished one's
            eval_tokens.tick(workspace, [(t.job.job_id, t.done is not None) for t in tracked])
        if all(item.done is not None for item in tracked):
            break
        if _now() > deadline:
            minutes = round(settings.eval_batch_wait_s / 60)
            raise RuntimeError(run_failure.still_running_text(minutes))
        _sleep(POLL_INTERVAL_S)
    _finish(run_id, mode, [item.done for item in tracked if item.done is not None],
            workspace=workspace, executor=executor, job_ids=[t.job.job_id for t in tracked])


def _ingest(run_id: str, workspace: WorkspaceContext, executor: Any, item: _Tracked) -> None:
    records = _read_store("results.jsonl",
                          partial(executor.ingest, workspace, item.job.job_id, since=item.ingested))
    if not records:
        return
    spec = item.job.spec or {}
    # P3.1 / P3.2: each row names the judge (and inference config) that produced it
    judges = judge_budget.result_judges(list(spec.get("evaluator_defs") or []),
                                        spec.get("judge_model_id"))
    db = SessionLocal()
    try:
        results_store.persist(db, workspace.id, source="run", owner_id=run_id, records=records,
                              judges=judges)
    finally:
        db.close()
    item.ingested += len(records)


def _redispatch(run_id: str, workspace: WorkspaceContext, executor: Any, item: _Tracked,
                plan: RunPlan) -> None:
    """Attempt+1 on a fresh worker session; sessions the lost attempt already replayed are
    not invoked again (their spans exist) — ``record_id`` dedupes overlapping results."""
    invoked: set[str] = set()
    reader = getattr(executor, "invocations", None)
    if reader is not None:
        try:
            invoked = {str(r.get("session_id")) for r in reader(workspace, item.job.job_id)
                       if r.get("status") == "ok"}
        except Exception:  # noqa: BLE001 — best effort
            invoked = set()
    sessions = [{**s, "replay": s.get("replay", True) and s["session_id"] not in invoked}
                for s in item.sessions]
    # N17-obs413: never two attempts of one job at once — stop the stale attempt's worker
    # session first (if it was alive after all, its result still counts: the reader takes
    # whichever attempt finished)
    stop_session = getattr(executor, "stop_session", None)
    if stop_session is not None:
        try:
            stop_session(workspace, item.job.job_id)
        except Exception:  # noqa: BLE001 — a session already gone is fine
            logger.warning("job %s: could not stop attempt %s before re-dispatch",
                           item.job.job_id, item.job.attempt)
    attempt = item.job.attempt + 1
    held_tokens = (item.job.spec or {}).get(eval_tokens.SPEC_FLAG)
    item.job = _job_spec(plan, item.job.job_id, sessions, workspace=workspace, attempt=attempt)
    if held_tokens:  # D5: the attempts of a job share its tokens.json (I9)
        item.job.spec[eval_tokens.SPEC_FLAG] = held_tokens
    item.sessions = sessions
    item.ingested = 0  # the new attempt's results.jsonl starts empty; ids dedupe on upsert
    executor.submit(workspace, item.job)
    item.dispatched_at = _now()


def _mirror_phase(run_id: str, workspace: WorkspaceContext, executor: Any,
                  tracked: list[_Tracked]) -> None:
    phases: list[str] = []
    beats: list[str] = []
    for item in tracked:
        if item.done is not None:
            continue
        status = _read_store("status.json", partial(executor.status, workspace, item.job.job_id))
        if status is not None and status.phase in _PHASE_ORDER:
            phases.append(status.phase)
        if status is not None and status.updated_at:
            beats.append(str(status.updated_at))
    run = _get(run_id) if phases or beats else None
    if run is None:
        return
    changes: dict[str, Any] = {}
    # N17-obs418: the newest heartbeat of the live jobs (the worker re-writes status.json
    # every HEARTBEAT_S) — the console shows a slow run as alive, not frozen
    beat = max(beats, key=_heartbeat_key) if beats else None
    if beat and run.last_heartbeat_at != beat:
        changes["last_heartbeat_at"] = beat
    if phases:
        phase = min(phases, key=lambda p: _PHASE_ORDER[p])
        if run.status in ACTIVE_STATUSES and run.status != phase:
            changes["status"] = phase
    if changes:
        _update(run_id, **changes)


def _heartbeat_key(stamp: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


_ERROR_EXCERPT_CHARS = 300


def record_errors_summary(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """``{count, total, first_type, first_message}`` over evaluator records that errored
    (``error_type`` / ``error_message`` set), or None when none did (N12-bug4)."""
    errored = [r for r in records if r.get("error_type") or r.get("error_message")]
    if not errored:
        return None
    first = errored[0]
    return {
        "count": len(errored),
        "total": len(records),
        "first_type": str(first.get("error_type") or "error"),
        "first_message": str(first.get("error_message") or "")[:_ERROR_EXCERPT_CHARS],
    }


def insight_errors_summary(blob: dict[str, Any], total_sessions: int) -> dict[str, Any] | None:
    """The insights analogue: ``diagnose_sessions`` lists each session whose analysis
    raised under ``errors`` (``{session_id, error: "Type: message"}``)."""
    errors = [e for e in (blob.get("errors") or []) if isinstance(e, dict)]
    if not errors:
        return None
    sessions = {str(e.get("session_id")) for e in errors}
    text = str(errors[0].get("error") or "error")
    first_type, sep, first_message = text.partition(": ")
    if not sep:
        first_type, first_message = "error", text
    summary: dict[str, Any] = {
        "count": len(sessions),
        "total": max(total_sessions, len(sessions)),
        "first_type": first_type.strip()[:64],
        "first_message": first_message.strip()[:_ERROR_EXCERPT_CHARS],
    }
    if total_sessions == 0:
        # sessions were resolved on the worker (the row lists none): the denominator is
        # unknown, so "every session failed" can never be concluded from it
        summary["total_known"] = False
    return summary


# N12-obs47 (upstream 00eb359b parity) — which SESSIONS a partial run left unscored, and why.
# The worker's error records share one wording (``strands_eval.jobs_batch._replay_failure_text``):
# a session that answered but whose spans never became queryable (N-SCN-bug3) reads
# "…answered but its telemetry never became ready: <Code>: …"; a scenario the target never
# answered (N-SCN-bug1) reads "…failed after k/N attempts: <Code>: …". Anything else is a judge
# that errored on a session it did see. ``error_kind`` is not persisted on ``eval_results``, so
# the kind is read off the message; ``tests/evaluation/test_run_session_failures.py`` pins the
# markers against the worker's actual text.
TELEMETRY_FAILURE_MARKER = "telemetry never became ready"
REPLAY_FAILURE_MARKER = "failed after "
SESSION_FAILURE_KINDS = ("telemetry_incomplete", "replay_failed", "evaluator_error")
FAILED_SESSIONS_MAX = 50


def _failure_kind(error_type: Any, message: Any) -> str:
    text = str(message or "")
    if TELEMETRY_FAILURE_MARKER in text:
        return "telemetry_incomplete"
    if REPLAY_FAILURE_MARKER in text and " attempts: " in text:
        return "replay_failed"
    return "evaluator_error"


def summarize_session_failures(
    records: list[dict[str, Any]], session_ids: list[str],
) -> dict[str, Any] | None:
    """Fold a run's errored records into per-session failures: ``{total, failed, kind,
    sessions: [{session_id, index, kind, error_type, message, arm}]}``, or None when no record
    errored.

    A session's ``kind`` is ``telemetry_incomplete`` when every error on it is the worker's
    telemetry-readiness failure (re-evaluating cannot recover it — the spans never landed),
    ``replay_failed`` when every error is an unanswered replay (a retry may help), else
    ``evaluator_error`` (a judge failed on a session it did see — some of its other
    evaluators may still have scored). The run-level ``kind`` folds the sessions (``mixed``
    when they differ). ``index`` is the 1-based position in the run's ``session_ids`` — the
    dataset scenario order — so runs on one dataset can be compared; ``total`` is the
    run's session count (the records' sessions when the row lists none). ``arm`` (N12-obs62,
    additive) is the session's pre-assigned experiment / canary arm as ``eval_results.arm``
    carries it (``C`` | ``T1``; None for a plain run) — a canary's failures are attributed
    without the assignment rows or the arm's logs."""
    errors: dict[str, list[tuple[Any, Any]]] = {}
    arms: dict[str, str] = {}
    seen: set[str] = set()
    for rec in records:
        sid = str(rec.get("session_id") or "")
        if not sid:
            continue
        seen.add(sid)
        if rec.get("arm") and sid not in arms:
            arms[sid] = str(rec["arm"])
        if rec.get("error_type") or rec.get("error_message"):
            errors.setdefault(sid, []).append((rec.get("error_type"), rec.get("error_message")))
    if not errors:
        return None
    position = {sid: i + 1 for i, sid in enumerate(session_ids or [])}
    total = len(session_ids) if session_ids else len(seen)
    ordered = sorted(errors, key=lambda sid: position.get(sid, len(position) + 1))
    sessions = []
    for sid in ordered:
        kinds = {_failure_kind(kind, msg) for kind, msg in errors[sid]}
        first_type, first_message = errors[sid][0]
        sessions.append({
            "session_id": sid,
            "index": position.get(sid),
            "kind": kinds.pop() if len(kinds) == 1 else "evaluator_error",
            "error_type": str(first_type or "error"),
            "message": str(first_message or "")[:_ERROR_EXCERPT_CHARS],
            "arm": arms.get(sid),
        })
    run_kinds = {s["kind"] for s in sessions}
    return {
        "total": max(total, len(sessions)),
        "failed": len(sessions),
        "kind": run_kinds.pop() if len(run_kinds) == 1 else "mixed",
        "sessions": sessions[:FAILED_SESSIONS_MAX],
    }


def _errors_verdict(mode: str, summary: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """``(status, error)`` the errored records impose on an otherwise completed run:
    every record errored → ``failed`` (nothing was evaluated); some → ``completed`` with
    the summary as the run's ``error`` (the console's completed-with-errors state)."""
    if not summary or summary["count"] <= 0:
        return None, None
    unit = "insight sessions" if mode == "insights" else "evaluator records"
    first = f"{summary['first_type']}: {summary['first_message']}".rstrip(": ")
    all_failed = bool(summary.get("total_known", True) and summary["count"] >= summary["total"])
    return ("failed" if all_failed else "completed"), run_failure.records_text(
        count=summary["count"], total=summary["total"], unit=unit, first=first,
        all_failed=all_failed)


def evaluator_snapshot(db: Any, workspace: Any, evaluator_ids: list[str]) -> list[dict[str, Any]]:
    """P7.1: the run's evaluators as they are NOW — ``[{id, name, kind, version,
    definition_hash, definition}]``; a built-in is versioned by the library release."""
    out: list[dict[str, Any]] = []
    for definition in evaluators_local.resolve_definitions(db, workspace, evaluator_ids):
        if "definition" not in definition:
            out.append({"id": definition["id"], "name": definition["id"], "kind": "builtin",
                        "version": evaluators_local.library_version(), "definition_hash": None,
                        "definition": dict(definition)})
            continue
        out.append({"id": definition["id"], "name": definition.get("name"),
                    "kind": definition.get("definition"), "version": definition.get("version"),
                    "definition_hash": definition.get("definition_hash"),
                    "definition": dict(definition)})
    return out


def snapshot_definitions(run: Any) -> list[dict[str, Any]] | None:
    """The definitions a run was submitted with (``None`` on a run without a snapshot)."""
    snapshot = getattr(run, "evaluator_snapshot", None)
    if not snapshot:
        return None
    return [dict(entry["definition"]) for entry in snapshot]


def worker_release_tag() -> str | None:
    """P7.6: the evaluation worker release this hub dispatches to (``<version>-<digest12>``)."""
    try:
        from app.templates import eval_worker

        return eval_worker.release_tag(eval_worker.release())
    except Exception:  # noqa: BLE001 — never block a run on the stamp
        return None


def fold_tool_units(dones: list[JobDone]) -> dict[str, int]:
    """N17-obs416: ``{session_id: tool calls}`` from the jobs' ``done.json`` (an older
    worker reports none — the coverage then keeps its old TOOL_CALL rule)."""
    out: dict[str, int] = {}
    for done in dones:
        out.update({str(k): int(v) for k, v in (done.summary.get("tool_units") or {}).items()})
    return out


def fold_skill_units(dones: list[JobDone]) -> dict[str, int] | None:
    """N17-obs435: ``{session_id: skill loads}`` from the jobs' ``done.json`` — the skill
    evaluators' expected units; ``None`` when no job reported any (an older worker)."""
    reported = [d.summary.get("skill_units") for d in dones
                if isinstance(d.summary.get("skill_units"), dict)]
    if not reported:
        return None
    out: dict[str, int] = {}
    for counts in reported:
        out.update({str(k): int(v) for k, v in counts.items()})
    return out


def skill_evaluator_ids(run: Any) -> list[str]:
    """N17-obs435: the run's evaluators that write one row per skill load
    (``engine_core.is_skill_evaluator`` over the submitted definitions — a custom judge on a
    skill builtin included; the ids alone without a snapshot)."""
    definitions = snapshot_definitions(run)
    if definitions is None:
        definitions = [{"id": e} for e in (getattr(run, "evaluators", None) or [])]
    return [str(d.get("id")) for d in definitions if is_skill_evaluator(d)]


def shard_token_budget(budget: int, shards: int) -> int | None:
    """Each shard's share of the run's token budget (``None`` = no budget)."""
    if not budget or shards <= 0:
        return None
    return -(-int(budget) // int(shards))


_USAGE_KEYS = ("inputTokens", "outputTokens", "totalTokens")


def run_token_usage(executor: Any, workspace: Any, job_ids: list[str],
                    dones: list[JobDone], *, budget: int) -> dict[str, Any] | None:
    """N17-obs417: the run's judge tokens over EVERY attempt of every job — the per-attempt
    ``usage.json`` sidecars (a failed / lost attempt's included); a job without sidecars
    (an older worker) contributes its ``done.json`` totals."""
    reader = getattr(executor, "attempt_artifacts", None)
    totals = dict.fromkeys(_USAGE_KEYS, 0)
    seen = False
    by_job = dict(zip(job_ids, dones, strict=False)) if len(job_ids) == len(dones) else {}
    for job_id in job_ids:
        sidecars: list[Any] = []
        if reader is not None:
            try:
                sidecars = [s for s in reader(workspace, job_id, "usage.json")
                            if isinstance(s, dict)]
            except Exception:  # noqa: BLE001 — accounting never fails a run
                sidecars = []
        if not sidecars:
            done = by_job.get(job_id)
            report = (done.summary.get("token_usage") if done is not None else None)
            sidecars = [report] if isinstance(report, dict) else []
        for usage in sidecars:
            seen = True
            for key in _USAGE_KEYS:
                totals[key] += int(usage.get(key) or 0)
    if not seen:
        return fold_token_usage(dones, budget=budget)
    return {**totals, "budget": int(budget) or None,
            "exhausted": any(d.summary.get("token_budget_exhausted") for d in dones)}


def record_usage_on_failure(run_id: str, *, workspace: Any, executor: Any,
                            job_ids: list[str]) -> None:
    """N17-obs417: a run that fails before it settles (its jobs lost, the hub's wait over)
    still records what its attempts spent — never raises."""
    try:
        usage = run_token_usage(executor, workspace, job_ids, [],
                                budget=get_settings().eval_run_token_budget)
        if usage is not None:
            _update(run_id, token_usage=usage)
    except Exception:  # noqa: BLE001
        logger.warning("run %s: token usage of the failed run not recorded", run_id)


def fold_token_usage(dones: list[JobDone], *, budget: int) -> dict[str, Any] | None:
    """The run's judge tokens from its jobs' ``done.json`` (``None`` when no job reported
    any — an older worker), with the budget and whether a job exhausted its share."""
    reported = [d.summary.get("token_usage") for d in dones
                if isinstance(d.summary.get("token_usage"), dict)]
    if not reported:
        return None
    totals = {key: sum(int(r.get(key) or 0) for r in reported)
              for key in ("inputTokens", "outputTokens", "totalTokens")}
    return {**totals, "budget": int(budget) or None,
            "exhausted": any(d.summary.get("token_budget_exhausted") for d in dones)}


def _publish_coverage(workspace: WorkspaceContext, run: Any, coverage: dict[str, Any],
                      settings: Any) -> None:
    """R3-f: the coverage EMF line for a CloudWatch alarm — only with the results mirror
    prefix configured; never fails the run."""
    prefix = str(getattr(settings, "eval_results_log_group_prefix", "") or "")
    if not prefix:
        return
    try:
        run_coverage.publish_coverage(workspace.client("logs"), prefix, run, coverage)
    except Exception:  # noqa: BLE001 — advisory
        logger.warning("run %s: coverage mirror failed", getattr(run, "id", "?"))


def _finish(
    run_id: str, mode: str, dones: list[JobDone], *, workspace: WorkspaceContext,
    executor: Any, job_ids: list[str],
) -> None:
    """Settle the row from the jobs' ``done.json`` files: any FAILED → failed (with the
    reason), any STOPPED → stopped (partial scores kept), else completed — unless every
    evaluator record (insights: every session) errored on the worker, which is a failed
    run with the first error as its reason; some errored → completed with the summary as
    ``error`` and ``record_errors`` (N12-bug4: 15/15 ``RuntimeError`` records used to
    land as a green ``completed`` with ``scores: []``)."""
    failed = [d for d in dones if d.state == "FAILED"]
    stopped = [d for d in dones if d.state == "STOPPED"]
    budget_stops = [b for d in dones for b in (d.summary.get("budget_stops") or [])]
    scenario_failures = skipped_personas(dones)
    if mode == "insights":
        blob: dict[str, Any] = {}
        for job_id, done in zip(job_ids, dones, strict=False):
            artifact = None
            reader = getattr(executor, "artifact", None)
            if reader is not None:
                artifact = reader(workspace, job_id, "insights.json")
            artifact = artifact or done.summary.get("insights") or {}
            for key, value in artifact.items():
                if isinstance(value, list):
                    blob.setdefault(key, []).extend(value)
        run = _get(run_id)
        errors = insight_errors_summary(blob, len(run.session_ids or []) if run else 0)
        parsed: dict[str, Any] = {"insights": blob, "record_errors": errors}
    else:
        db = SessionLocal()
        try:
            records = results_store.read_run_records(db, run_id)
        finally:
            db.close()
        errors = record_errors_summary(records)
        run = _get(run_id)
        parsed = {
            # EVAL-TRUST 3.2: each evaluator's sessions_n + 95 % CI on session means
            "scores": _confident_scores(run, aggregate_scores(records), records),
            "record_errors": errors,
            # N12-obs47: which sessions the errored records belong to, and why (None when
            # none errored) — the console explains a partial run from this, not from `error`
            "session_failures": summarize_session_failures(
                records, list(run.session_ids or []) if run is not None else []),
        }
    parsed["scenario_failures"] = scenario_failures or None
    turns_map = fold_replay_turns(_get(run_id), dones)
    if turns_map is not None:
        parsed["scenario_map"] = turns_map
    warnings = trajectory_warnings(_get(run_id), dones)
    if warnings is not None:
        parsed["warnings"] = warnings
    if mode != "insights":
        parsed["extraction_check"] = fold_extraction_checks(dones)
        # R3-f: expected vs judged units — no silent failure
        coverage_run = SimpleNamespace(
            id=run_id, agent_name=getattr(run, "agent_name", None), mode=mode,
            session_ids=list(getattr(run, "session_ids", None) or []),
            evaluators=list(getattr(run, "evaluators", None) or []),
            extraction_check=parsed["extraction_check"],
            tool_units=fold_tool_units(dones),  # N17-obs416
            skill_units=fold_skill_units(dones),  # N17-obs435
            skill_evaluators=skill_evaluator_ids(run))
        settings = get_settings()
        parsed["unit_coverage"] = run_coverage.run_coverage(
            records, coverage_run, threshold=settings.eval_coverage_min)
        # N17-obs417: every attempt's sidecar — a failed / lost attempt's tokens count too
        parsed["token_usage"] = run_token_usage(executor, workspace, job_ids, dones,
                                                budget=settings.eval_run_token_budget)
        _publish_coverage(workspace, coverage_run, parsed["unit_coverage"], settings)
    if failed:
        reason = "; ".join(str(d.error or "job failed") for d in failed)[:500]
        _update(run_id, status="failed", error=reason, budget_stops=budget_stops or None,
                **parsed)
        return
    if stopped or stop_flags.requested(run_id):
        _update(run_id, status="stopped", error=STOP_REASON, budget_stops=budget_stops or None,
                **parsed)
        return
    status, error = _errors_verdict(mode, errors)
    _update(run_id, status=status or "completed", error=error,
            budget_stops=budget_stops or None, **parsed)


def fold_replay_turns(run: EvalRun | None, dones: list[JobDone]) -> dict[str, Any] | None:
    """D3-c: ``EvalRun.scenario_map`` with each session's ``turns_replayed`` — what its last
    replay attempt actually sent (the jobs' ``done.json`` ``replay_turns``) — or None when no
    job reported any (a passive run, a worker older than the count). A session a lost
    attempt replayed and the redispatch skipped keeps no count."""
    replayed: dict[str, int] = {}
    for done in dones:
        for sid, count in (done.summary.get("replay_turns") or {}).items():
            replayed[str(sid)] = int(count)
    if run is None or not run.scenario_map or not replayed:
        return None
    return {sid: ({**entry, "turns_replayed": replayed[sid]} if sid in replayed else entry)
            for sid, entry in run.scenario_map.items()}


TOOL_NOT_OBSERVED = "expected_tool_not_observed"


def trajectory_warnings(run: EvalRun | None, dones: list[JobDone]) -> list[dict[str, Any]] | None:
    """N17-obs407, open catalogues (studio / byoc / own code / imported runtimes — names the
    run start cannot check): one non-blocking warning per scenario whose expected_trajectory
    names a tool that appears in NONE of the run's sessions' spans — the usual sign of a
    naming mismatch rather than an agent that skipped a tool. From the jobs' ``done.json``
    ``observed_tools``; None when no job reported them (an older worker) or the agent's
    catalogue was checked at run start."""
    from app.evaluation import agent_tools

    observed: set[str] = set()
    reported = False
    for done in dones:
        if "observed_tools" in done.summary:
            reported = True
            for names in (done.summary.get("observed_tools") or {}).values():
                observed.update(str(n) for n in names or [])
    if run is None or not reported or not run.scenario_map:
        return None
    db = SessionLocal()
    try:
        agent = db.get(Agent, run.agent_id) if run.agent_id else None
        spec = dict(agent.spec or {}) if agent else None
    finally:
        db.close()
    if spec is not None and not agent_tools.agent_tools(spec).open:
        return None
    out: list[dict[str, Any]] = []
    for sid in run.session_ids or []:
        entry = (run.scenario_map or {}).get(sid) or {}
        missing = [n for n in dict.fromkeys(entry.get("expected_tools") or [])
                   if n not in observed]
        if missing:
            out.append({
                "kind": TOOL_NOT_OBSERVED, "session_id": sid,
                "scenario_id": entry.get("scenario_id"), "tools": missing,
                "observed": sorted(observed)[:50],
                "message": f"expected tool(s) {', '.join(missing)} never appear in this "
                           "agent's spans — check the names the agent calls its tools by"})
    return out


def _confident_scores(run: EvalRun | None, scores: list[dict[str, Any]],
                      records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """EVAL-TRUST 3.2 / N17-obs423: ``scores`` with each evaluator's interval clamped to its
    scale (``run_stats.scale_bounds``)."""
    ids = [str(s.get("evaluatorId")) for s in scores]
    db = SessionLocal()
    try:
        bounds = run_stats.scale_bounds(db, run.workspace_id if run else None, ids,
                                        run_stats.session_scores(records))
    finally:
        db.close()
    return run_stats.with_confidence(scores, records, bounds)


def backfill_confidence(run: EvalRun) -> list[dict[str, Any]] | None:
    """EVAL-TRUST 3.2: a finished evaluators run scored before the interval existed gets it
    from its stored ``eval_results`` (one ledger read, persisted once); None when there is
    nothing to add."""
    if (run.mode != "evaluators" or run.status in ACTIVE_STATUSES or not run.scores
            or run_stats.has_confidence(run.scores)):
        return None
    db = SessionLocal()
    try:
        records = results_store.read_run_records(db, run.id)
    finally:
        db.close()
    if not records:
        return None
    scores = _confident_scores(run, list(run.scores), records)
    _update(run.id, scores=scores)
    return scores


def fold_extraction_checks(dones: list[JobDone]) -> dict[str, Any] | None:
    """``EvalRun.extraction_check`` (eval accuracy item 1): the shards' ``extraction_checks``
    merged — ``{summary, sessions: {session_id: extraction_check}}`` — or None when no job
    reported one (nothing evaluated, a worker older than the check)."""
    sessions: dict[str, dict[str, Any]] = {}
    for done in dones:
        sessions.update(done.summary.get("extraction_checks") or {})
    if not sessions:
        return None
    return {"summary": extraction_check.summarise(sessions), "sessions": sessions}


SCENARIO_ERROR_CHARS = 300  # upstream #251: one bounded line per skipped persona


def skipped_personas(dones: list[JobDone]) -> list[dict[str, str]]:
    """Upstream #251 parity (02607ab4): ``EvalRun.scenario_failures`` — the simulated persona
    scenarios the worker skipped after their replay attempts while the run went on, as
    ``[{scenario_id, retries, code, error}]`` (``retries`` a string, as upstream serialises
    it — the attempts made minus one). Folded from the ``simulated`` entries of the jobs'
    ``replay_failures[]`` summaries (``jobs_batch._replay_failures_summary``); predefined
    scenarios that failed to replay are NOT listed here — they stay the N-SCN-bug1 errored
    scenarios of ``session_failures``."""
    out: list[dict[str, str]] = []
    for done in dones:
        for failure in done.summary.get("replay_failures") or []:
            if not failure.get("simulated"):
                continue
            code = str(failure.get("code") or "error")
            out.append({
                "scenario_id": str(failure.get("scenario_id") or "unknown"),
                "retries": str(max(int(failure.get("attempts") or 1) - 1, 0)),
                "code": code,
                "error": f"{code}: {failure.get('error') or ''}"[:SCENARIO_ERROR_CHARS],
            })
    return out


# ─── re-attach: recheck, resume ─────────────────────────────────────────────────────
def _job_ids_for(executor: Any, workspace: WorkspaceContext, batch_id: str) -> list[str]:
    """The run's job ids: the run id itself, or its shards (``<run>-s<i>``)."""
    lister = getattr(executor, "jobs_with_prefix", None)
    if lister is not None:
        shards = sorted(j for j in lister(workspace, f"{batch_id}-s") if j.startswith(batch_id))
        if shards:
            return shards
    return [batch_id]


def reconcile_run(
    run_id: str, *, mode: str, batch_id: str, workspace: WorkspaceContext
) -> None:
    """Finish a run whose in-process poller died (restart / dev reload) while the worker
    kept running: re-attach to the job(s) by id and poll them to the end."""
    try:
        executor = select_executor(get_settings(), job_type="insights" if mode == "insights"
                                   else "batch_eval")
        tracked = [
            _Tracked(job=JobSpec(job_id=job_id, job_type="insights" if mode == "insights"
                                 else "batch_eval", workspace={"id": workspace.id},
                                 sink={}, spec={}), sessions=[])
            for job_id in _job_ids_for(executor, workspace, batch_id)
        ]
        for item in tracked:
            item.dispatched_at = 0.0  # a lost job is stale right away, never re-dispatched
        _poll(run_id, workspace=workspace, executor=executor, tracked=tracked, plan=None)
    except Exception as exc:  # noqa: BLE001
        _update(run_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
    finally:
        _discard_tokens(workspace, locals().get("tracked") or [])
        stop_flags.clear(run_id)


def request_stop(run_id: str, *, workspace: WorkspaceContext) -> EvalRun:
    """Operator stop for an active run; returns the refreshed row.

    * still ``queued`` → cancelled locally, the worker skips it, the row is ``stopped``
      right away and nothing is dispatched;
    * dispatched → the cancel marker is written for the job(s); the poller observes the
      worker's ``STOPPED`` (or ends the session after the grace period) and settles the
      row as ``stopped`` with the sessions judged so far.
    A terminal run (completed / failed / stopped) is a 409 conflict."""
    run = _get(run_id)
    if run is None:
        raise AppError("run.not_found", "run not found", status_code=404)
    if run.status not in ACTIVE_STATUSES:
        raise AppError(
            "run.not_active",
            f"run is already {run.status} and cannot be stopped",
            status_code=409,
        )
    batch_id = run.batch_eval_id
    # The flag first: whichever way the worker is racing us, the poller finds it at
    # its next tick or the dispatch loop before the first submit.
    stop_flags.request(run_id)
    if batch_id:
        try:
            executor = select_executor(get_settings(),
                                       job_type="insights" if run.mode == "insights"
                                       else "batch_eval")
            for job_id in _job_ids_for(executor, workspace, batch_id):
                executor.cancel(workspace, job_id)
        except AppError:
            pass  # no executor to reach (worker gone): the poller settles the row
    elif run_queue.cancel(run_id):
        # Never dequeued: nothing is running, so the row is settled here and the
        # flag is not needed (no callable will ever read it).
        stop_flags.clear(run_id)
        _update(run_id, status="stopped", error=STOP_REASON)
    return _get(run_id)  # type: ignore[return-value]


def recheck_run(run_id: str, *, workspace: WorkspaceContext) -> EvalRun:
    """Re-read a failed run's job from the artefacts, the source of truth.

    A run fails locally when its poller gives up or dies, while the worker may keep
    running (and finish). A finished job settles the row exactly as the poller would
    have; one still running puts the row back to ``evaluating`` with a fresh poller; one
    that left no artefacts at all is lost for good (``failed`` with that reason). Only
    ``failed`` runs that dispatched a job qualify (409 ``run.not_recheckable`` otherwise).
    Reads only — no new job is started."""
    run = _get(run_id)
    if run is None:
        raise AppError("run.not_found", "run not found", status_code=404)
    if _is_legacy_partial(run):
        # N12-obs47: a partially failed evaluators run finished before `session_failures`
        # existed — its records are in the ledger, so the summary is a local read (no job
        # is touched, nothing re-runs); the row stays `completed`
        backfill_session_failures(run_id)
        return _get(run_id)  # type: ignore[return-value]
    if run.status != "failed" or not run.batch_eval_id:
        raise AppError(
            "run.not_recheckable",
            "only a failed run that dispatched an evaluation job (or a partially failed "
            "run without its unscored-session summary) can be re-checked",
            status_code=409,
        )
    mode, batch_id = run.mode, run.batch_eval_id
    executor = select_executor(get_settings(), job_type="insights" if mode == "insights"
                               else "batch_eval")
    job_ids = _job_ids_for(executor, workspace, batch_id)
    statuses = {job_id: executor.status(workspace, job_id) for job_id in job_ids}
    if all(s is None for s in statuses.values()):
        _update(run_id, status="failed",
                error="the evaluation job left no artefacts (lost before it started) — "
                      "submit the run again")
        return _get(run_id)  # type: ignore[return-value]
    _update(run_id, status="evaluating", error=None)
    run_queue.submit(
        run_id,
        lambda: reconcile_run(run_id, mode=mode, batch_id=batch_id, workspace=workspace),
    )
    return _get(run_id)  # type: ignore[return-value]


def _is_legacy_partial(run: EvalRun) -> bool:
    """A `completed` evaluators run with errored records (``error`` set, N12-bug4) whose row
    predates ``session_failures`` — the summary can be rebuilt from ``eval_results``."""
    return (run.status == "completed" and bool(run.error) and run.session_failures is None
            and run.mode == "evaluators")


def backfill_session_failures(run_id: str) -> dict[str, Any] | None:
    """Compute and store ``session_failures`` for a legacy partial run from its ledger
    records (one read, no worker call); returns the summary (None when nothing errored)."""
    run = _get(run_id)
    if run is None:
        return None
    db = SessionLocal()
    try:
        records = results_store.read_run_records(db, run_id)
    finally:
        db.close()
    summary = summarize_session_failures(records, list(run.session_ids or []))
    _update(run_id, session_failures=summary)
    return summary


def run_results(run: EvalRun, *, workspace: WorkspaceContext) -> dict[str, Any]:
    """Per-session judge records of a run — score, label and the judge's explanation
    for every evaluator, grouped by session, from ``eval_results`` (the Global results
    log stream's twin). Degrades rather than raises: ``available=false`` + ``reason`` when
    the run has nothing to read (insights run, no job, still active, no rows)."""
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
        db = SessionLocal()
        try:
            records = results_store.read_run_records(db, run.id)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — a broken ledger read degrades
        return {**base, "reason": "unreadable", "detail": f"{type(exc).__name__}: {exc}"[:300]}
    if not records:
        return {**base, "reason": "stream_missing"}
    if _is_legacy_partial(run):
        # a partial run finished before the summary existed: backfill it from the records
        # just read (no extra read) so the task detail's next poll explains the gap
        _update(run.id, session_failures=summarize_session_failures(
            records, list(run.session_ids or [])))
    records = records[:RESULT_RECORDS_MAX]
    by_session: dict[str, list[dict[str, Any]]] = {}
    scenario_of = dict(run.scenario_map or {})  # N17-obs391: the session's dataset scenario
    for rec in records:
        scenario = scenario_of.get(str(rec["session_id"])) or {}
        by_session.setdefault(str(rec["session_id"]), []).append({
            **results_store.normalize_result_record(rec),
            "scenario_id": scenario.get("scenario_id"),
            "scenario_label": scenario.get("input"),
            # R3-f (additive): why this row did not score — null on a judged row
            "failure_class": run_coverage.failure_class(rec),
            # P8.3 (additive): the unit's judge tokens, on its first row
            "token_usage": rec.get("token_usage")})
    ordered = [sid for sid in (run.session_ids or []) if sid in by_session]
    ordered += [sid for sid in by_session if sid not in set(ordered)]
    checks = (run.extraction_check or {}).get("sessions") or {}
    judged = judged_turns(records)
    return {
        **base,
        "available": True,
        # eval accuracy item 1 (additive): each session's extraction reconciliation (null when
        # the worker reported none) and the run-level summary
        "sessions": [{"session_id": sid, "results": by_session[sid],
                      "extraction_check": checks.get(sid),
                      # N17-obs391 (additive): the dataset scenario the session replayed
                      "scenario_id": (scenario_of.get(sid) or {}).get("scenario_id"),
                      "scenario_label": (scenario_of.get(sid) or {}).get("input"),
                      # D3-c (additive): the scenario's turns — defined by the dataset,
                      # sent by the replay, judged by a turn-level evaluator
                      "turns": scenario_turns(scenario_of.get(sid), judged.get(sid))}
                     for sid in ordered],
        "extraction_check": (run.extraction_check or {}).get("summary"),
        "count": len(records),
        "truncated": len(records) >= RESULT_RECORDS_MAX,
    }


def judged_turns(records: list[dict[str, Any]]) -> dict[str, int]:
    """D3-c: ``{session_id: distinct turns (trace ids) a TRACE-level evaluator scored}`` — an
    errored row is no judgement."""
    traces: dict[str, set[str]] = {}
    for rec in records:
        if rec.get("level") == "TRACE" and rec.get("trace_id") and rec.get("score") is not None:
            traces.setdefault(str(rec["session_id"]), set()).add(str(rec["trace_id"]))
    return {sid: len(ids) for sid, ids in traces.items()}


def scenario_turns(entry: dict[str, Any] | None, judged: int | None) -> dict[str, Any]:
    """``{defined, replayed, judged}`` of one session (D3-c); null where unknown — a passive
    session, an older row, a persona's defined turns, a run without turn-level scores."""
    entry = entry or {}
    return {"defined": entry.get("turns_defined"), "replayed": entry.get("turns_replayed"),
            "judged": judged}


def _token_owner_terminal(owner_kind: str, owner_id: str) -> bool:
    """D5 startup sweep: a job's token file outlives its watcher unless the owner is about
    to be watched again — an eval run that ``resume_interrupted_runs`` re-attaches. Canary
    traffic is never resumed (its in-flight action is cleared at startup)."""
    if owner_kind != eval_tokens.OWNER_EVAL_RUN:
        return True
    run = _get(owner_id)
    return not (run is not None and run.status in ("invoking", "waiting", "evaluating")
                and run.batch_eval_id)


def sweep_token_files() -> list[str]:
    """Startup: delete the eval / canary token files no watcher will refresh (I9)."""
    return eval_tokens.sweep_on_startup(owner_terminal=_token_owner_terminal)


def resume_interrupted_runs() -> list[str]:
    """Startup reconciliation. The queue worker and its pollers are in-memory, so a
    backend restart orphans in-flight rows: runs whose job was dispatched are re-attached
    (the worker kept running); runs killed before dispatch lost their plan and are failed
    honestly."""
    db = SessionLocal()
    try:
        rows = db.query(EvalRun).filter(EvalRun.status.in_(INTERRUPTED_STATUSES)).all()
        resumed: list[str] = []
        for run in rows:
            if run.status in ("invoking", "waiting", "evaluating") and run.batch_eval_id:
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
                run.error = ("interrupted by a backend restart before the evaluation job "
                             "was dispatched — submit the run again")
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
    creator: creators.Creator | None = None,
    judge_model_id: str | None = None,
    tool_name_rewrites: list[dict[str, str]] | None = None,
    dataset_split: str | None = None,
    dataset_revision: int | None = None,
) -> EvalRun:
    """Queue one run. The telemetry comes from the platform ``agent`` — or, with
    ``agent=None``, from ``log_source`` {service_name, log_group_names}: an agent that is
    not a platform agent whose spans and content logs are already in CloudWatch. Such a
    run is passive only (session ids or a time window); there is nothing to replay.

    The executor is resolved BEFORE any row exists, so a workspace without the worker
    answers 503 ``evaluation.worker_missing`` (and prod + inprocess 409) to the request
    instead of failing the run later. The worker is probed from the preset row *now*
    (N12-bug6): uninstalled / deploying → 503, a stale resource-map ARN does not count."""
    job_type = "insights" if mode == "insights" else "batch_eval"
    assert_executor_ready(select_executor(get_settings(), job_type=job_type), workspace)
    # P3.1 / P3.2: the judge is fixed when the run is submitted — the caller's choice, else
    # the `judge` model role NOW — and recorded with the inference config it runs with
    judge = judge_budget.judge_inference(judge_model_id)
    if mode == "insights" and "Builtin.Insight.FailureAnalysis" in (
            insights or evaluators_local.INSIGHT_TYPES):
        # N17-obs449: the FailureAnalysis detector's own judge (the `failure_detection` role
        # NOW), recorded beside the run's judge so the console names it
        judge["failure_detection"] = judge_budget.failure_detection_inference()
    if agent is None:
        if not log_source or dataset_items:
            raise ValueError("an agent-less run needs a log_source and a passive scope")
        service_name = log_source["service_name"]
        log_groups = list(log_source["log_group_names"])
        log_group = log_groups[0]
    else:
        service_name, log_group = resolve_telemetry(agent, workspace)
        log_groups = [SPANS_LOG_GROUP, log_group]
    # Window runs have no dataset; encode the scope in dataset_name so the
    # runs list can render "window · Nh" without a schema change.
    if lookback_hours and not dataset_name:
        dataset_name = f"window:{lookback_hours}h"
    # what a replay replays (a cloud run also keeps `dataset_version`) — upstream #289
    fingerprint = items_fingerprint(dataset_items)
    if mode == "insights" and time_range and not session_ids:
        # N13-obs19 (E14 NC-02): an insights window is resolved to session ids HERE, before
        # any row — one Logs Insights query at request time (as `_assert_log_groups_exist`
        # already costs one DescribeLogGroups per group) — so a window with nothing in it
        # answers 409 instead of a `completed` run with three empty trees. The ids found are
        # what the run analyses; `plan_run` does not discover them again.
        session_ids = discover_sessions(
            workspace, service_name=service_name, log_groups=log_groups,
            time_range=time_range, limit=get_settings().eval_job_max_sessions,
        )
        if not session_ids:
            raise _no_sessions_error(lookback_hours)
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
            # the version the run evaluates: the newest snapshot that reached AWS
            agent_snapshot_seq=scorecard.newest_deployed_seq(db, agent.id) if agent else None,
            dataset_fingerprint=fingerprint[0] if fingerprint else None,
            dataset_size=fingerprint[1] if fingerprint else None,
            judge_model_id=judge["model_id"],
            judge_inference=judge,
            tool_name_rewrites=tool_name_rewrites or None,
            dataset_split=dataset_split,
            dataset_revision=dataset_revision,
            # P7.1: which version of each evaluator scores this run
            evaluator_snapshot=evaluator_snapshot(db, workspace, evaluators)
            if mode == "evaluators" and evaluators else None,
        )
        creators.stamp(run, creator)
        db.add(run)
        db.commit()
        run_id = run.id
        agent_arn = agent.arn if agent else ""
        agent_ledger_id = agent.id if agent else None
        agent_protocol = ((agent.spec or {}) if agent else {}).get("protocol") or "http"
        # Gateway-tool agents need a runtimeUserId or the Runtime injects no
        # workload token and the eval run measures a tool-less agent.
        agent_runtime_user = gateway_support.runtime_user_id(agent.spec) if agent else None
    finally:
        db.close()

    # The snapshot is taken HERE, before the callable is enqueued: a dataset
    # edit that lands while the run waits in the queue must not change what
    # it replays, so the queued callable never touches the caller's list.
    items_snapshot = copy.deepcopy(dataset_items)

    def start() -> None:
        try:
            plan = plan_run(
                run_id, workspace=workspace, agent_arn=agent_arn, agent_id=agent_ledger_id,
                protocol=agent_protocol, service_name=service_name, log_group=log_group,
                log_groups=log_groups, items=items_snapshot, evaluators=evaluators, mode=mode,
                wait_seconds=wait_seconds, existing_session_ids=session_ids,
                time_range=time_range, insights=insights, actor_model_id=actor_model_id,
                runtime_user_id=agent_runtime_user, judge_model_id=judge["model_id"],
                failure_detection_model_id=(judge.get("failure_detection") or {}).get("model_id"),
            )
        except AppError as exc:
            _update(run_id, status="failed", error=f"{exc.code}: {exc.message}"[:500])
            stop_flags.clear(run_id)
            return
        except Exception as exc:  # noqa: BLE001
            _update(run_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
            stop_flags.clear(run_id)
            return
        execute_run(run_id, workspace=workspace, plan=plan)

    position = run_queue.submit(run_id, start)
    _update(run_id, queue_position=position)
    return _get(run_id)  # type: ignore[return-value]


# Kept for callers that type-check the plan outside the queue (tests, online reports).
PlanBuilder = Callable[..., RunPlan]
