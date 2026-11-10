"""Worker-side handler for ``online_eval`` jobs (N12 E2-2; plan §B.0/§B.3).

Pure library code, like the rest of ``strands_eval``: every AWS / model dependency is
injected by the dispatcher (W-E1's worker template, or the in-process executor in dev):

- ``logs_client`` — CloudWatch Logs client the spans provider queries with;
- ``judge_factory(model_id, *, max_tokens, temperature) -> strands.models.Model`` — on the
  backend ``functools.partial(aws_clients.global_bedrock_model, region=…)``, on the worker
  the template's bearer-based factory (risk R-3: never ``None`` / a ``str``);
- ``lambda_client`` — only for ``code`` evaluators.

Spec (built by ``evaluation/online_scheduler.py``)::

    {config_id, mode: "scores", sessions: [{session_id}], log_groups: [...], spans_source,
     lookback_hours, evaluator_defs: [{"id": "Builtin.X"} | custom row dict],
     judge_model_id, judge_max_tokens, judge_max_tokens_cap (CN additive, N-SCN-bug2)}

Per session: ``LaunchpadSpansProvider.get_evaluation_data`` → ``EvaluationData(input=first
user prompt, actual_output, actual_trajectory=Session)`` → every evaluator (throttling
retried 3× with 2/4/8 s backoff; a judge that hits its output limit is re-run on a fresh
judge with the budget doubled up to ``judge_max_tokens_cap``, at most twice — N-SCN-bug2;
N13-obs14: a transient UPSTREAM error — ``runtime_invoke.is_upstream_transient``, the one
predicate batch / SCORE NOW / the replay use — is retried through the SAME
``engine_core.evaluate_with_retry`` as the batch judge path: ``JUDGE_TRANSIENT_ATTEMPTS`` = 3
attempts, ``transient_backoff`` (the hub's ``eval_transient_retry_backoff_seconds``, 5 s / 15 s)
slept in ≤ 1 s slices that poll the cancel marker; exhausting the budget is ONE error record
typed as the cause naming the attempts (``UpstreamTransientExhausted``), a stop mid-wait one
typed error record (``UpstreamRetryAbandoned``) and the job ends STOPPED; any other failure is
an error record at once) → one result record per ``EvaluationOutput`` on the sink. The cancel
marker is honoured between sessions, between evaluators and during a transient backoff;
``done.json`` carries the per-evaluator means and (eval accuracy item 1) the per-session
``extraction_checks`` against the spec's ``extraction_references``. Sampling / window
semantics are the scheduler's; a session item's ``after`` / ``until`` (ISO, N17-obs422) bound
the activity segment the sample covers — only the traces starting in ``(after, until]`` are
scored (and reconciled), so an idle-resumed session's earlier segment is never re-scored.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from strands_evals.types.evaluation import EvaluationData
from strands_evals.types.trace import AgentInvocationSpan, Session

from . import code_evaluators, deadlines, engine_core, extraction_check, model_calls, runtime_invoke
from . import evaluators as evaluators_lib
from .results import aggregate_scores, error_record
from .spans_provider import LaunchpadSpansProvider

JudgeFactory = Callable[..., Any]
# The online job's throttling schedule (2 / 4 / 8 s, then one last try) — unchanged by
# N13-obs14, passed explicitly so the shared retry never drifts it.
RETRY_DELAYS_S: tuple[float, ...] = (2.0, 4.0, 8.0)


def first_user_prompt(session: Session) -> str:
    """The first user prompt recorded in the session's agent-invocation spans."""
    for trace in session.traces:
        for span in trace.spans:
            if isinstance(span, AgentInvocationSpan) and span.user_prompt:
                return str(span.user_prompt)
    return ""


# a trace's first span starts at or before its last record's @timestamp (millisecond
# precision): ``until`` gets this much slack; the next segment starts a session timeout later
SEGMENT_SLACK = timedelta(seconds=1)


def _instant(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def segment_of(data: Any, after: Any = None, until: Any = None,
               ) -> tuple[Any, list[int]]:
    """N17-obs422: the evaluation data restricted to the traces that START in the sampled
    segment ``(after, until]`` (``until`` with :data:`SEGMENT_SLACK`), and their positions
    in the session (the reconciliation reference pairs turns by position). No bound → the
    data unchanged. Nothing in the segment (its records not readable yet) raises — a
    telemetry readiness failure, which the scheduler retries (P4.6)."""
    low, high = _instant(after), _instant(until)
    session = data["trajectory"]
    if low is None and high is None:
        return data, list(range(len(session.traces)))
    kept: list[int] = []
    for index, trace in enumerate(session.traces):
        starts = [s.span_info.start_time for s in trace.spans]
        start = min(starts) if starts else None
        if start is None:
            continue
        start = start if start.tzinfo is not None else start.replace(tzinfo=UTC)
        if (low is None or start > low) and (high is None or start <= high + SEGMENT_SLACK):
            kept.append(index)
    if not kept:
        raise LookupError(f"telemetry: no turn of the session starts in its sampled segment "
                          f"(after {after}, until {until}) yet")
    traces = [session.traces[i] for i in kept]
    output = next((s.agent_response for t in reversed(traces) for s in reversed(t.spans)
                   if isinstance(s, AgentInvocationSpan)), "")
    return {**data, "trajectory": session.model_copy(update={"traces": traces}),
            "output": output}, kept


def first_trace_id(session: Session) -> str | None:
    for trace in session.traces:
        if trace.trace_id:
            return str(trace.trace_id)
    return None


def evaluate_with_retry(
    evaluator: Any, case: EvaluationData, *, sleep: Callable[[float], Any] = time.sleep,
    transient_backoff: tuple[float, ...] = runtime_invoke.DEFAULT_TRANSIENT_BACKOFF_S,
    stop: Callable[[], bool] | None = None, label: str = "judge",
) -> list[Any]:
    """``evaluator.evaluate(case)`` with the online job's throttling schedule
    (``RETRY_DELAYS_S``, unchanged) and — N13-obs14 — the SAME transient-upstream retry as the
    batch / SCORE NOW judge path: ``engine_core.evaluate_with_retry`` (3 attempts on
    ``runtime_invoke.is_upstream_transient``, ``transient_backoff[k]`` before retry *k* in
    ≤ 1 s slices polling ``stop()``; exhausted → ``UpstreamTransientExhausted``, stopped
    mid-wait → ``UpstreamRetryAbandoned``, both typed as the cause on the error record). A
    non-transient, non-throttling error propagates at once (one attempt)."""
    return list(engine_core.evaluate_with_retry(
        evaluator, case, sleep=sleep, delays=RETRY_DELAYS_S,
        transient_delays=tuple(float(x) for x in transient_backoff), label=label, stop=stop,
    ))


def run(
    job: Any,  # evaluation.jobs.JobSpec — duck-typed: the library imports nothing from app.*
    sink: Any,
    *,
    logs_client: Any,
    judge_factory: JudgeFactory,
    lambda_client: Any = None,
    spans_source: str | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    build: Callable[..., Any] = evaluators_lib.build,
    transient_backoff: tuple[float, ...] = runtime_invoke.DEFAULT_TRANSIENT_BACKOFF_S,
) -> None:
    """``transient_backoff`` (N13-obs14) is the dispatcher's ``WorkerEnv.transient_backoff_s``
    — the hub's ``eval_transient_retry_backoff_seconds`` rendered into the worker as
    ``TRANSIENT_RETRY_BACKOFF_S`` / read live by the in-process executor."""
    spec = job.spec
    if str(spec.get("mode") or "scores") != "scores":
        raise ValueError(
            "online_eval jobs score sessions; insights-mode configs run their reports as "
            "insights jobs (online.start_report)"
        )
    sessions = [
        str(item["session_id"]) if isinstance(item, dict) else str(item)
        for item in spec.get("sessions") or []
    ]
    # N17-obs422: each sample's activity segment (absent = the whole session)
    bounds = {str(item["session_id"]): (item.get("after"), item.get("until"))
              for item in spec.get("sessions") or [] if isinstance(item, dict)}
    definitions = [dict(d) for d in spec.get("evaluator_defs") or []]
    default_model_id = str(spec.get("judge_model_id") or "")
    max_tokens = int(spec.get("judge_max_tokens") or model_calls.DEFAULT_BUDGET)
    budget_cap = int(spec.get("judge_max_tokens_cap") or model_calls.DEFAULT_BUDGET_CAP)
    total = len(sessions)
    sink.status("evaluating", done=0, total=total, message="building evaluators")

    provider = LaunchpadSpansProvider(
        logs_client, list(spec.get("log_groups") or []),
        lookback_hours=int(spec.get("lookback_hours") or 24),
        spans_source=spans_source or spec.get("spans_source") or "both",
    )
    # N17-obs327: code evaluators get Global's envelope (the session's documents)
    lambda_client = code_evaluators.runtime_for(
        lambda_client, logs_client=logs_client, log_groups=list(spec.get("log_groups") or []),
        lookback_hours=int(spec.get("lookback_hours") or 24))
    judges: dict[str, Any] = {}

    def judge_for(model_id: str | None) -> Any:
        key = str(model_id or default_model_id)
        if key not in judges:
            judges[key] = judge_factory(key, max_tokens=max_tokens, temperature=0.0)
        return judges[key]

    def needs_judge(definition: dict[str, Any]) -> bool:
        return str(definition.get("id") or "") not in evaluators_lib.TRAJECTORY_LEVELS and (
            definition.get("definition") != "code")

    # P2.2: every judge evaluator this job builds runs under the hub's wall-clock deadline
    deadline_s = deadlines.deadline_of(spec)
    usage = deadlines.UsageLedger()  # P8.3: the judges' tokens per unit and in total
    judge_factory = deadlines.counting_factory(judge_factory)
    unbounded_build = build

    def build(definition: dict[str, Any], judge: Any, **kwargs: Any) -> Any:  # noqa: F811
        built_one = unbounded_build(definition, judge, **kwargs)
        if not needs_judge(definition):
            return built_one
        return deadlines.DeadlineEvaluator(built_one, deadline_s=deadline_s,
                                           label=f"judge {definition.get('id')}",
                                           usage=usage, definition=definition)

    built: list[tuple[dict[str, Any], Any]] = []
    for definition in definitions:
        try:
            judge = judge_for(definition.get("model_id")) if needs_judge(definition) else None
            built.append((definition, build(definition, judge, lambda_client=lambda_client)))
        except Exception as exc:  # noqa: BLE001 — recorded per session below
            built.append((definition, exc))

    def evaluate_budgeted(definition: dict[str, Any], evaluator: Any, case: EvaluationData,
                          ) -> list[Any]:
        """N-SCN-bug2: the shared judge keeps ``max_tokens``; a retry builds the evaluator
        again on a fresh judge with the doubled budget. The transient retry (N13-obs14) runs
        INSIDE each budgeted call, exactly like ``engine_core.evaluate_with_budget``."""
        evaluator_id = str(definition.get("id") or "")
        if not needs_judge(definition):
            return evaluate_with_retry(evaluator, case, sleep=sleep,
                                       transient_backoff=transient_backoff,
                                       stop=sink.cancelled, label=f"evaluator {evaluator_id}")
        model_id = str(definition.get("model_id") or default_model_id)

        def call(budget: int) -> list[Any]:
            chosen = evaluator if budget == max_tokens else build(
                definition, judge_factory(model_id, max_tokens=budget, temperature=0.0),
                lambda_client=lambda_client,
            )
            return evaluate_with_retry(chosen, case, sleep=sleep,
                                       transient_backoff=transient_backoff,
                                       stop=sink.cancelled, label=f"judge {evaluator_id}")

        try:
            return model_calls.call_with_budget(
                call, budget=max_tokens, cap=budget_cap,
                label=f"judge {definition.get('id')}", model_id=model_id,
            )
        except model_calls.JudgeBudgetExceeded as exc:

            def judge_at(model: str | None, budget: int, smaller: EvaluationData) -> list[Any]:
                rebuilt = build(definition, judge_factory(model or model_id, max_tokens=budget,
                                                          temperature=0.0),
                                lambda_client=lambda_client)
                return evaluate_with_retry(rebuilt, smaller, sleep=sleep,
                                           transient_backoff=transient_backoff,
                                           stop=sink.cancelled, label=f"judge {evaluator_id}")

            # N17-obs385: truncated context, then the judge_fallback model
            return engine_core.rescue_judgement(
                exc, case=case, judge_at=judge_at, model_id=model_id,
                fallback_model_id=spec.get("judge_fallback_model_id") or None, cap=budget_cap)

    all_records: list[dict[str, Any]] = []
    # eval accuracy item 1: each sampled session's extraction is reconciled against the
    # platform chat ledger's answers the hub put in the spec (``unverifiable`` without one)
    references = dict(spec.get("extraction_references") or {})
    checks: dict[str, dict[str, Any]] = {}

    def summary() -> dict[str, Any]:
        return {"scores": aggregate_scores(all_records), "token_usage": usage.totals(),
                **({"extraction_checks": dict(checks)} if checks else {})}

    def emit(record: dict[str, Any]) -> None:
        sink.result(record)
        all_records.append(record)

    for index, session_id in enumerate(sessions):
        if sink.cancelled():
            sink.finish("STOPPED", summary=summary())
            return
        try:
            data, positions = segment_of(provider.get_evaluation_data(session_id),
                                         *bounds.get(session_id, (None, None)))
            session: Session = data["trajectory"]
            output = data["output"]
        except Exception as exc:  # noqa: BLE001 — the session is the failure unit
            for definition, _evaluator in built:
                emit(error_record(
                    job_id=job.job_id, session_id=session_id,
                    evaluator_id=str(definition.get("id") or ""),
                    level=evaluators_lib.level_of(definition), error=exc,
                ))
            sink.status("evaluating", done=index + 1, total=total,
                        message=f"evaluated {index + 1}/{total}")
            continue
        reference = references.get(session_id)
        if reference is None and hasattr(provider, "origin_reference"):
            reference = provider.origin_reference(session_id)  # N17-obs316
        if reference and any(bounds.get(session_id, ())):
            # the segment's turns, at their positions in the session (N17-obs422)
            turns = list(reference.get("turns") or [])
            reference = {**reference, "turns": [turns[i] for i in positions if i < len(turns)]}
        checks[session_id] = extraction_check.session_check(data, reference)
        case = EvaluationData(
            input=first_user_prompt(session), actual_output=output,
            actual_trajectory=session, name=session_id,
        )
        trace_id = first_trace_id(session)
        turns = engine_core.turn_cases(case)  # N17-obs324: TRACE evaluators run per turn
        statuses = extraction_check.turn_statuses(extraction_check.extracted_turns(data),
                                                  reference)
        for definition, evaluator in built:
            # N13-obs14: a stop that landed during a transient backoff (the retry was
            # abandoned → one typed error record) ends the session here, like the batch
            # engine's ``evaluate_session`` — no first attempt for the remaining evaluators.
            if sink.cancelled():
                break
            evaluator_id = str(definition.get("id") or "")
            level = evaluators_lib.level_of(definition)
            if isinstance(evaluator, Exception):
                emit(error_record(job_id=job.job_id, session_id=session_id,
                                  evaluator_id=evaluator_id, level=level, error=evaluator,
                                  trace_id=trace_id))
                continue
            for unit_case, stamp in engine_core.scoring_units(
                    level, case, turns, trace_id, statuses, checks[session_id].get("status"),
                    definition):
                if sink.cancelled():
                    break
                records = engine_core.unit_records(
                    lambda c, d=definition, e=evaluator: evaluate_budgeted(d, e, c),
                    unit_case, stamp, job_id=job.job_id, session_id=session_id,
                    evaluator_id=evaluator_id, level=level)
                usage.stamp(records)  # P8.3: the unit's judge tokens on its first row
                for record in records:
                    emit(record)
        sink.status("evaluating", done=index + 1, total=total,
                    message=f"evaluated {index + 1}/{total}")
    # a stop that arrived inside the last session (N13-obs14) still ends the job STOPPED
    state = "STOPPED" if sink.cancelled() else "COMPLETED"
    sink.finish(state, summary=summary())


def make_runner(**dependencies: Any) -> Callable[[Any, Any], None]:
    """Bind the dispatcher's dependencies: ``runner(job, sink)`` for the job registry."""

    def runner(job: Any, sink: Any) -> None:
        run(job, sink, **dependencies)

    return runner
