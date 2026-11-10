"""Replay traffic for an experiment / canary as ONE pre-assigned ``batch_eval`` job
(N12 E3-1; plan §B.0 "Replay happens from the worker, routing stays authoritative in
the backend", §B.1 ``send_gateway_traffic`` row).

The Global stage SigV4-posted every prompt through the experiment Gateway, which
split the sessions across arms and scored them with a per-variant online evaluation.
In CN the backend **plans** the replay — one fresh Runtime session per prompt,
``routing.assign_batch`` writes the arm of every session into
``experiment_assignments`` first — and ships ``spec.sessions[]`` (arm, endpoint
``qualifier``, ``baggage``, ``launchpad_meta`` payload alias) to the evaluation worker,
which invokes the target runtime directly, waits for spans, judges each session with
the experiment's online evaluators and streams ``results.jsonl``. :func:`run` polls the
executor, mirrors the worker's progress onto the ledger row, ingests the records into
``eval_results`` (``arm`` stamped from the assignment) and reconciles the sessions the
worker never reached.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.evaluation import jobs
from app.evaluation.jobs import JobSpec
from app.evaluation.judge_budget import judge_fallback_model_id
from app.evaluation.models import EvalEvaluator, EvalResult
from app.evaluation.scenarios import scenario_prompts
from app.optimization import routing
from app.optimization.progress_steps import step
from app.optimization.routing import Assignment, OwnerRef
from app.services import eval_tokens
from app.services.agentcore import runtime as rt
from app.services.strands_eval import extraction_check
from app.services.workspace import WorkspaceContext

Progress = Callable[[str], None]

# Ceiling on in-flight replay invocations inside the worker — every prompt opens its
# own runtime session, so this bounds how hard a replay leans on the target runtime's
# concurrency quota. A code constant so no yaml/env value can raise it.
TRAFFIC_MAX_CONCURRENCY = 10
# Runtime user id every experiment / canary replay session is attributed to.
TRAFFIC_USER_ID = "launchpad-experiment-traffic"
# How often the stage re-reads the job's S3 markers (R-18: status lags ≤ 25 s).
TRAFFIC_POLL_S = 15.0
# Spans-wait budget the worker applies per replayed session before judging.
TRAFFIC_WAIT_SECONDS = 180
_BUILTIN_PREFIX = "Builtin."
_EVALUATOR_COLUMNS = (
    "id", "name", "level", "definition", "instructions", "rating_scale", "model_id",
    "base_evaluator_id", "lambda_arn", "lambda_timeout_s",
)

_sleep = time.sleep  # injectable
_monotonic = time.monotonic  # injectable


def _noop(_msg: str) -> None:
    pass


def new_job_id() -> str:
    """12 chars — fits ``EvalResult.run_id`` (String(16))."""
    return "tr" + uuid.uuid4().hex[:10]


def items_from_prompts(prompts: Sequence[str]) -> list[tuple[str, str]]:
    return [(f"item_{index + 1}", prompt) for index, prompt in enumerate(prompts)]


def evaluator_defs(
    evaluator_ids: Sequence[str], db: Session | None = None,
) -> list[dict[str, Any]]:
    """Resolved evaluator definitions for the job spec: builtin ids as ``{"id"}``, custom
    rows (``eval_evaluators``) dumped so the worker (which has no SQLite) can build them."""
    custom = [e for e in evaluator_ids if not e.startswith(_BUILTIN_PREFIX)]
    if not custom:
        return [{"id": evaluator_id} for evaluator_id in evaluator_ids]
    session, owned = routing._with_db(db)
    try:
        rows = {row.id: row for row in session.query(EvalEvaluator).filter(
            EvalEvaluator.id.in_(custom))}
        missing = [e for e in custom if e not in rows]
        if missing:
            raise ValueError(f"unknown evaluator(s): {', '.join(missing)}")
        defs: list[dict[str, Any]] = []
        for evaluator_id in evaluator_ids:
            row = rows.get(evaluator_id)
            defs.append({"id": evaluator_id} if row is None else {
                col: getattr(row, col) for col in _EVALUATOR_COLUMNS})
        return defs
    finally:
        if owned:
            session.close()


def content_log_group_for(resource_id: str, qualifier: str | None) -> str:
    """The runtime log group a session served by ``qualifier`` writes its content log to
    (N-SCN-bug3): a canary's named endpoint has its own group, bundle arms share DEFAULT."""
    return f"/aws/bedrock-agentcore/runtimes/{resource_id}-{qualifier or 'DEFAULT'}"


def log_groups_for(owner: OwnerRef, resource_id: str) -> list[str]:
    """Where the replayed sessions' spans land: the DEFAULT endpoint group for a bundle
    experiment, one group per named endpoint for a canary, plus ``aws/spans``."""
    endpoints = sorted({arm.qualifier for arm in owner.arms if arm.qualifier})
    if not endpoints:
        endpoints = ["DEFAULT"]
    return [content_log_group_for(resource_id, ep) for ep in endpoints] + ["aws/spans"]


class ReplayJobFailed(RuntimeError):
    """The worker finished the replay job FAILED. ``result`` is the traffic artifact built
    from what DID complete (invoked-ok sessions, per-arm counts, ingested records) so the
    caller can record the attempt before surfacing the error (N-SCN-bug3)."""

    def __init__(self, message: str, result: dict[str, Any]):
        super().__init__(message)
        self.result = result


def plan(
    ws: WorkspaceContext,
    agent: dict[str, Any],
    items: Sequence[tuple[str, Turns]],
    owner: OwnerRef,
    *,
    evaluators: Sequence[str],
    judge_model_id: str | None = None,
    wait_seconds: int = TRAFFIC_WAIT_SECONDS,
    db: Session | None = None,
) -> tuple[JobSpec, dict[str, Assignment]]:
    """One ``batch_eval`` job for ``items`` (``(scenario_id, prompt | [turn prompts])``
    pairs): a fresh 64-char Runtime session per item — every turn of a multi-turn scenario
    in that one session (N17-obs321) — every session pre-assigned to an arm.

    ``agent`` is the owner's ``agent_meta`` (``arn``, ``resource_id``). The assignment
    rows are written here, before the caller dispatches — the worker only executes.
    """
    if not items:
        raise ValueError("dataset has no prompts to replay")
    settings = get_settings()
    bucket = str(ws.resources.get("artifacts_bucket") or "")
    if not bucket:
        raise RuntimeError("artifacts_bucket missing from this workspace's resource map")
    defs = evaluator_defs(list(evaluators), db=db)
    session_ids = [rt.new_session_id() for _ in items]
    assigned = routing.assign_batch(owner, session_ids, db=db)
    resource_id = str(agent.get("resource_id") or "")
    sessions = [
        {
            "session_id": sid,
            "scenario_id": str(scenario_id),
            "prompts": list(prompt) if isinstance(prompt, list) else [prompt],
            **assigned[sid],
            # N-SCN-bug3: the arm's own runtime log group — the worker waits for THIS
            # session's content there, not in the alphabetically first endpoint group
            "content_log_group": content_log_group_for(
                resource_id, assigned[sid].get("qualifier")),
        }
        for sid, (scenario_id, prompt) in zip(session_ids, items, strict=True)
    ]
    job_id = new_job_id()
    spec = {
        "run_id": job_id,
        "replay": True,
        "owner": {"kind": owner.kind, "id": owner.id},
        "target": {
            "runtime_arn": str(agent.get("arn") or ""),
            "protocol": "http",
            "runtime_user_id": TRAFFIC_USER_ID,
            "actor_id": TRAFFIC_USER_ID,
        },
        "invoke_concurrency": TRAFFIC_MAX_CONCURRENCY,
        "sessions": sessions,
        "wait": {
            "seconds": int(wait_seconds),
            "log_groups": log_groups_for(owner, resource_id),
            "spans_source": settings.observability_spans_source,
        },
        "evaluator_defs": defs,
        "ground_truth": {},
        "judge_model_id": judge_model_id or settings.eval_judge_model_id,
        "judge_fallback_model_id": judge_fallback_model_id(),  # N17-obs385
        "judge_deadline_s": settings.eval_judge_deadline_s,  # P2.2
    }
    job = JobSpec(
        job_id=job_id, job_type="batch_eval",
        workspace={"account_id": ws.account_id, "region": ws.region},
        sink={"bucket": bucket, "prefix": jobs.job_prefix(job_id, 1)},
        spec=spec, deadline_s=min(jobs.DEFAULT_DEADLINE_S, int(settings.eval_job_budget_s)),
    )
    return job, assigned


# ─── ingest ──────────────────────────────────────────────────────────────────────
def ingest_records(
    db: Session,
    owner: OwnerRef,
    job_id: str,
    records: Iterable[dict[str, Any]],
    assigned: dict[str, Assignment],
    *,
    config_id: str | None = None,
    judge_model_id: str | None = None,
    evaluator_defs: Sequence[dict[str, Any]] | None = None,
) -> int:
    """Write worker result records as ``eval_results`` rows (idempotent on ``id``), each
    stamped with the judge that produced it (P3.2: ``judge_model_id`` = the job spec's, a
    custom row its own model — ``judge_budget.result_judges``; a canary verdict refuses to
    compare arms its rows say were judged differently)."""
    from app.evaluation.judge_budget import result_judges, row_judge

    records = list(records)
    defs = list(evaluator_defs or [{"id": str(r.get("evaluator_id") or "")} for r in records])
    judges = result_judges(defs, judge_model_id) if judge_model_id else {}
    written = 0
    for rec in records:
        session_id = str(rec.get("session_id") or "")
        evaluator_id = str(rec.get("evaluator_id") or "")
        if not session_id or not evaluator_id:
            continue
        record_id = str(rec.get("id") or jobs.record_id(job_id, session_id, evaluator_id, 0))
        if db.get(EvalResult, record_id) is not None:
            continue
        assignment = assigned.get(session_id)
        score = rec.get("score")
        db.add(EvalResult(
            id=record_id, workspace_id=owner.workspace_id, source="run", run_id=job_id,
            config_id=config_id, session_id=session_id, trace_id=rec.get("trace_id"),
            # N17-obs387: the unit a row judged — the verdict dedupes per (session, unit)
            span_id=rec.get("span_id"), tool_name=rec.get("tool_name"),
            extraction_status=rec.get("extraction_status"),
            evaluator_id=evaluator_id, level=rec.get("level"),
            score=float(score) if score is not None else None,
            label=rec.get("label"), explanation=rec.get("explanation"),
            error_type=rec.get("error_type"), error_message=rec.get("error_message"),
            arm=(assignment["arm"] if assignment else rec.get("arm")),
            judge_model_id=(row_judge(rec, judges)[1] or {}).get("model_id"),
            judge_inference=row_judge(rec, judges)[1],
        ))
        written += 1
    db.commit()
    return written


def _invocations(
    executor: Any, ws: WorkspaceContext, job_id: str, records: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """``invocations.jsonl`` when the executor exposes it; otherwise a session with at
    least one result record counts as invoked (``status: ok``)."""
    reader = getattr(executor, "invocations", None)
    if callable(reader):
        try:
            found = reader(ws, job_id)
        except Exception:  # noqa: BLE001 — diagnostic channel, never fatal
            found = None
        if found is not None:
            return [dict(rec) for rec in found]
    seen: dict[str, dict[str, Any]] = {}
    for rec in records:
        sid = str(rec.get("session_id") or "")
        if sid and sid not in seen:
            seen[sid] = {"session_id": sid, "status": "ok"}
    return list(seen.values())


def run(
    ws: WorkspaceContext,
    owner: OwnerRef,
    job: JobSpec,
    assigned: dict[str, Assignment],
    *,
    executor: Any,
    progress: Progress = _noop,
    config_id: str | None = None,
    budget_s: float | None = None,
    poll_s: float = TRAFFIC_POLL_S,
) -> dict[str, Any]:
    """Dispatch ``job``, poll it to ``done.json``, ingest ``results.jsonl`` into
    ``eval_results`` and reconcile the assignments.

    Returns the ``traffic`` artifact (baseline keys ``session_ids``, ``sent``, ``failed``,
    ``status_counts`` kept — ``session_ids`` are the invoked-ok sessions in input order
    — plus ``job_id``, ``job_state``, ``result_count``, ``arms``, ``dropped_sessions``,
    ``summary``). A FAILED job raises :class:`ReplayJobFailed` (a ``RuntimeError``) with the
    worker's error after the partial results were kept (as the baseline kept a STOPPED
    batch's partial scores); its ``result`` is the artifact built from what completed.
    """
    planned = [s["session_id"] for s in job.spec.get("sessions", [])]
    budget = float(budget_s if budget_s is not None else get_settings().eval_batch_wait_s)
    progress(step("dispatching_replay",
                  f"dispatching replay of {len(planned)} sessions to the evaluation worker…",
                  sessions=len(planned)))
    job_id = job.job_id
    deadline = _monotonic() + budget
    since = 0
    records: list[dict[str, Any]] = []
    done = None
    db = SessionLocal()
    try:
        # inside the try: a submit that fails still discards the job's tokens (D5 review 4)
        job_id = executor.submit(ws, job)
        while True:
            status = executor.status(ws, job_id)
            fresh = executor.ingest(ws, job_id, since)
            if fresh:
                since += len(fresh)
                records.extend(fresh)
                ingest_records(db, owner, job_id, fresh, assigned, config_id=config_id,
                               judge_model_id=job.spec.get("judge_model_id"),
                               evaluator_defs=job.spec.get("evaluator_defs"))
            done = executor.done(ws, job_id)
            if done is not None:
                # R-18: results.jsonl is flushed before done.json; re-read until the count
                # the worker reported has arrived (bounded).
                for _ in range(3):
                    if len(records) >= int(done.result_count or 0):
                        break
                    fresh = executor.ingest(ws, job_id, since)
                    if not fresh:
                        _sleep(min(poll_s, 2.0))
                        continue
                    since += len(fresh)
                    records.extend(fresh)
                    ingest_records(db, owner, job_id, fresh, assigned, config_id=config_id,
                                   judge_model_id=job.spec.get("judge_model_id"),
                                   evaluator_defs=job.spec.get("evaluator_defs"))
                break
            if status is not None:
                prog = status.progress or {}
                text = str(prog.get("message") or "")
                progress(step(
                    "worker_progress",
                    f"worker · {status.phase} {prog.get('done', 0)}/{prog.get('total', 0)}"
                    + (f" · {text}" if text else ""),
                    phase=str(status.phase), done=int(prog.get("done", 0) or 0),
                    total=int(prog.get("total", 0) or 0), message=text))
            if get_settings().policy_gateway_interceptor:
                # D5 (I9 v2): keep the job's one System::"canary" token fresh
                eval_tokens.tick(ws, [(job_id, False)])
            if _monotonic() >= deadline:
                executor.cancel(ws, job_id)
                raise TimeoutError(
                    f"replay job {job_id} did not finish within {int(budget)} s — cancelled")
            _sleep(poll_s)

        invocations = _invocations(executor, ws, job_id, records)
        dropped = routing.reconcile(owner, invocations, planned=planned, db=db)
    finally:
        db.close()
        _discard_tokens(ws, job_id)

    by_session = {str(inv.get("session_id")): inv for inv in invocations}
    status_counts: dict[str, int] = {}
    session_ids: list[str] = []
    arms: dict[str, int] = {}
    for sid in planned:
        inv = by_session.get(sid)
        if inv is None:
            continue
        state = str(inv.get("status") or "ok")
        status_counts[state] = status_counts.get(state, 0) + 1
        arm = assigned.get(sid, {}).get("arm") or str(inv.get("arm") or "")
        if arm:
            arms[arm] = arms.get(arm, 0) + 1
        if state == "ok":
            session_ids.append(sid)
    result = {
        "job_id": job_id,
        "job_state": done.state if done is not None else None,
        "session_ids": session_ids,
        "sent": len(session_ids),
        "failed": sum(n for state, n in status_counts.items() if state != "ok"),
        "status_counts": status_counts,
        "arms": arms,
        "result_count": len(records),
        "dropped_sessions": dropped,
        "summary": dict(done.summary or {}) if done is not None else {},
    }
    # eval accuracy item 1: the worker reconciled every judged session's extraction with the
    # answers it collected while replaying — the run-level summary rides on the artifact
    checks = (done.summary or {}).get("extraction_checks") if done is not None else None
    result["extraction_check"] = extraction_check.summarise(checks) if checks else None
    if done is not None and done.state == "FAILED":
        raise ReplayJobFailed(
            f"replay job {job_id} failed: {done.error or 'unknown error'}", result)
    return result


def _discard_tokens(ws: WorkspaceContext, job_id: str) -> None:
    """D5 (I9 v2): the job reached a terminal state (or its watcher gave up) — its token file
    goes; a failed delete is retried by the startup sweep, and the token expires anyway."""
    try:
        eval_tokens.discard_jobs(ws, [job_id])
    except Exception:  # noqa: BLE001
        pass


Turns = str | list[str]  # one prompt, or a multi-turn scenario's prompts in order


def resolve_traffic_items(dataset: Any) -> list[tuple[str, Turns]]:
    """``(scenario label, prompt | [turn prompts])`` per sendable item — the label names a
    question in a paired verdict (``scenario_id``, else ``item_<n>``). N17-obs321: a
    multi-turn predefined scenario keeps EVERY turn (replayed in order in one session, as
    a dataset run does); a single-turn one stays a plain prompt."""
    if dataset.kind == "simulated":
        raise ValueError("simulated datasets need an actor loop — pick a "
                         "predefined or legacy prompt dataset")
    prompts: list[tuple[str, Turns]] = []
    for index, item in enumerate(dataset.items or []):
        label = str(item.get("scenario_id") or f"item_{index + 1}")
        if dataset.kind == "predefined":
            # the scenario's user turns — reuse the eval replay extractor so dict inputs
            # ({"content"|"prompt": …}, imported JSON) unwrap the same way here as when
            # the dataset is replayed for evaluation
            turn_prompts = [p.strip() for p in scenario_prompts(item) if p.strip()]
            if len(turn_prompts) > 1:
                prompts.append((label, turn_prompts))
                continue
            text = turn_prompts[0] if turn_prompts else ""
        else:
            text = str(item.get("prompt") or "")
        if text.strip():
            prompts.append((label, text.strip()))
    if not prompts:
        raise ValueError("dataset has no usable prompts")
    return prompts
