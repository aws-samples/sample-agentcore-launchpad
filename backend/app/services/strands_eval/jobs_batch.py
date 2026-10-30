"""The worker-side job runner for ``batch_eval`` and ``insights`` (N12 E1-3b; plan §B.0).

``run_job(job, sink, env)`` is the dispatcher the Runtime worker's entrypoint hands a job
to — and, through ``executor.set_job_runner``, the very same function the in-process dev
executor runs. ``WorkerEnv`` carries everything the worker has that the library may not
build itself: the Logs / data-plane / Lambda clients, the judge-model factory, the clock.

batch_eval phases (each mirrored into ``status.json``):
    invoking   — replay every ``spec.sessions[]`` entry with its pre-assigned session id,
                 ``qualifier`` and ``baggage`` (routing is the backend's); one line per
                 session in ``invocations.jsonl``
    waiting    — the CloudWatch readiness probe on the newest session
    evaluating — the shared engine core; records flushed per session to ``results.jsonl``
    done.json  — COMPLETED | STOPPED (cancel marker) | FAILED (a raised error)

N-SCN-bug1: a scenario whose replay still fails after ``TRANSIENT_SCENARIO_RETRIES`` retries
(or fails once on a non-transient error) is an ERRORED SCENARIO, not a failed run: its
``invocations.jsonl`` line carries ``error`` / ``error_kind: infrastructure`` / ``attempts``
(+ ``retried``: each retry's attempt number, cause and the backoff it waited — N12-obs55),
the run continues with the remaining scenarios, the errored session gets one error record
per evaluator (so the hub's N12-bug4 verdict reads completed-with-errors) and only a run
in which EVERY replayed scenario failed raises (→ FAILED with the first error).

N-SCN-bug3: the ``waiting`` probe runs on the newest answered session **per content log
group** — a canary writes one runtime log group per named endpoint (``…-stableXXX`` /
``…-treatXXX``), so the group of a session is resolved from its arm (``session.content_log_group``
stamped by the hub, else the ``wait.log_groups`` entry ending in ``-<qualifier>``), falling back
to probing EVERY listed endpoint group. A session whose telemetry never becomes ready is an
errored session (``code: TelemetryReadinessTimeout``, ``error_kind: infrastructure``), not a
failed job; the job fails only when nothing is left to evaluate.

insights shares the ``invoking`` → ``waiting`` prologue whenever the spec replays a dataset
(N12-bug9) and then runs the detectors over the sessions (``evaluating``).
"""

from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from strands_evals.providers import SessionNotFoundError
from strands_evals.types.trace import ToolExecutionSpan

from app.evaluation.jobs import JobSpec, KindUnsupportedError, validate_job_type
from app.evaluation.telemetry import (
    SPANS_LOG_GROUP,
    TelemetryReadinessTimeout,
    wait_for_evaluation_telemetry,
)
from app.services.strands_eval import (
    code_evaluators,
    deadlines,
    engine_core,
    extraction_check,
    insights,
    model_calls,
    principal_tokens,
    runtime_invoke,
    simulator,
)
from app.services.strands_eval import evaluators as evaluator_lib
from app.services.strands_eval.results import aggregate_scores, error_record
from app.services.strands_eval.spans_provider import LaunchpadSpansProvider

TRANSIENT_SCENARIO_RETRIES = 2
TRANSIENT_SCENARIO_ATTEMPTS = TRANSIENT_SCENARIO_RETRIES + 1  # what "attempt k/N" counts to
REPLAY_ERROR_KIND = "infrastructure"  # the target did not answer — never a judgement
TELEMETRY_FAILURE = "telemetry"  # invocation-line `failure` tag: answered, spans never ready
log = logging.getLogger(__name__)
TELEMETRY_READY_GRACE_SECONDS = 120
TELEMETRY_QUERY_LOOKBACK_MS = 60_000
STATUS_EVERY_S = 10.0
# N17-obs400: how often an explicit session's readiness / Logs Insights fetch is re-tried
EXPLICIT_POLL_S = 15.0


class JobCancelled(Exception):
    """The backend wrote the cancel marker; the job stops at the next checkpoint."""


@dataclass
class WorkerEnv:
    """What the host (worker main.py or the backend) hands the library."""

    logs_client: Any
    data_client: Any
    # judge_factory(model_id, *, max_tokens=None, temperature=0.0) -> strands Model
    judge_factory: Callable[..., Any]
    default_model_id: str | None = None
    lambda_client: Any = None
    code_evaluators_enabled: bool = False
    spans_source: str = "both"
    judge_concurrency: int = 4
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    # (prompt, session, session_spec) -> {"text", "session_id"}; None = runtime_invoke
    invoke: Callable[..., dict[str, Any]] | None = None
    # N-SCN-bug2: where the judge budget doubling stops (hub Settings.eval_judge_max_tokens_cap)
    judge_budget_cap: int = model_calls.DEFAULT_BUDGET_CAP
    # judge_budget(model_id) -> the budget judge_factory(model_id) builds with when the spec
    # carries no judge_max_tokens (the engine must know what the FIRST attempt ran at)
    judge_budget: Callable[[str | None], int] | None = None
    # N12-obs53 / N12-obs55: the waits before the 2nd / 3rd attempt on a transient upstream
    # error — scenario replay AND judge calls (hub Settings.eval_transient_retry_backoff_seconds,
    # rendered into the worker; the last entry repeats; an empty tuple retries at once)
    transient_backoff_s: tuple[float, ...] = runtime_invoke.DEFAULT_TRANSIENT_BACKOFF_S
    extra: dict[str, Any] = field(default_factory=dict)


def _spec(job: JobSpec | dict[str, Any]) -> tuple[JobSpec, dict[str, Any]]:
    if isinstance(job, dict):
        job = JobSpec.from_payload(job)
    return job, dict(job.spec or {})


def _check_cancel(sink: Any) -> None:
    if sink.cancelled():
        raise JobCancelled(sink.job.job_id)


def _invoke(env: WorkerEnv, target: dict[str, Any], session: dict[str, Any], prompt: str,
            session_id: str) -> dict[str, Any]:
    if env.invoke is not None:
        return env.invoke(prompt, session_id, session)
    return runtime_invoke.invoke_text(
        env.data_client, str(target.get("runtime_arn") or ""), prompt,
        session_id=session_id, qualifier=session.get("qualifier"),
        runtime_user_id=target.get("runtime_user_id"), baggage=session.get("baggage"),
        launchpad_meta=session.get("launchpad_meta"),
    )


def _replay_session(env: WorkerEnv, spec: dict[str, Any], session: dict[str, Any],
                    sink: Any, on_retry: Callable[[int, BaseException], None] | None = None,
                    answers: list[str] | None = None) -> dict[str, Any]:
    """One session: predefined turns in order, or the actor-simulator loop; a transient
    upstream failure (``runtime_invoke.is_upstream_transient`` — the one predicate the
    architect assistant and the judge calls share) replays the scenario (same pre-assigned
    session id) a bounded number of times, waiting ``env.transient_backoff_s[k]`` before
    retry k (N12-obs55: a retry fired straight into the same Bedrock
    ``internalServerException``) — ``on_retry(next_attempt, exc)`` fires before each wait.
    Returns the ``invocations.jsonl`` line; a failed one carries ``error`` / ``error_kind`` /
    ``attempts`` (the number actually made, so the message and the count agree); any line
    that retried carries ``retried`` = ``[{attempt, reason, backoff_s}]``. ``answers``
    (eval accuracy item 1) receives each turn's collected answer of the LAST attempt — the
    extraction reconciliation's ``replay`` reference; it never reaches the line."""
    target = dict(spec.get("target") or {})
    session_id = str(session["session_id"])
    scenario_id = session.get("scenario_id")
    record = {"session_id": session_id, "scenario_id": scenario_id,
              "arm": session.get("arm"), "status": "ok", "code": None, "response_chars": 0,
              "runtime_version": None}
    if session.get("actor_profile"):
        # upstream #251 parity: a persona scenario's failure is a skipped persona, which the
        # hub lists separately (``eval_runs.scenario_failures``); the actor loop below calls
        # our invoke directly, so the agent call's own exception reaches this loop as is
        record["simulated"] = True
    last_error: BaseException | None = None
    attempts = 0
    retried: list[dict[str, Any]] = []
    # D5 (I9 v2): a governed target's turns carry the job's one system token; None = no
    # token file for this job (or none readable), the session is sent exactly as before
    tokens = principal_tokens.for_sink(spec, sink)
    for attempt in range(TRANSIENT_SCENARIO_ATTEMPTS):
        attempts = attempt + 1
        _check_cancel(sink)
        collected: list[str] = []
        attempt_started_ms = int(env.clock() * 1000)
        try:
            chars = 0
            if session.get("actor_profile"):
                model = env.judge_factory(spec.get("actor_model_id") or env.default_model_id)
                actor = simulator.build_actor(
                    dict(session["actor_profile"]), str(session.get("input") or ""),
                    model=model, max_turns=int(session.get("max_turns") or 10),
                )

                def invoke(prompt: str, sid: str | None, _s=session,
                           _c=collected) -> dict[str, Any]:
                    reply = _invoke(env, target, principal_tokens.with_principal(_s, tokens),
                                    prompt, session_id)
                    _c.append(str(reply.get("text") or ""))
                    return {"text": reply.get("text", ""), "session_id": session_id}

                simulator.drive(actor, invoke)
            else:
                for prompt in session.get("prompts") or []:
                    _check_cancel(sink)
                    reply = _invoke(env, target,
                                    principal_tokens.with_principal(session, tokens),
                                    str(prompt), session_id)
                    chars += len(str(reply.get("text") or ""))
                    collected.append(str(reply.get("text") or ""))
            record["response_chars"] = chars
            # D3-c: the turns this (last) attempt actually sent — the hub compares it with the
            # scenario's defined turns and the turns that were judged
            record["turns_replayed"] = len(collected)
            if answers is not None:
                answers[:] = collected
            record["retries"] = attempt
            if retried:
                record["retried"] = retried
                # N17-obs323: the retry reused the pre-assigned session id, so its spans hold
                # every attempt — only the traces from this attempt on are scored
                record["last_attempt_started_ms"] = attempt_started_ms
            return record
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — classified below
            last_error = exc
            transient = runtime_invoke.is_transient_invoke_error(exc)
            final = attempts == TRANSIENT_SCENARIO_ATTEMPTS or not transient
            backoff = 0.0 if final else runtime_invoke.backoff_for(env.transient_backoff_s,
                                                                   attempts - 1)
            log.warning(
                "scenario %s (session %s) attempt %d/%d failed: %s: %s — %s",
                scenario_id, session_id, attempts, TRANSIENT_SCENARIO_ATTEMPTS,
                type(exc).__name__, str(exc)[:300],
                "giving up" if final else (f"retrying in {backoff:g} s" if transient
                                           else "not transient"),
            )
            if final:
                break
            retried.append({"attempt": attempts, "reason": runtime_invoke.retry_reason(exc),
                            "backoff_s": backoff})
            if on_retry is not None:
                on_retry(attempts + 1, exc)
            _transient_backoff(env, sink, backoff)
    record["status"] = "failed"
    record["turns_replayed"] = len(collected) if attempts else 0  # D3-c: before it failed
    record["code"] = type(last_error).__name__ if last_error else "error"
    record["error"] = str(last_error)[:500] if last_error else ""
    # CN additive (N-SCN-bug1): the hub and the console tell an unanswered scenario from a
    # bad judgement by `error_kind`; `attempts` is what the "attempt k/N" log counted to.
    record["error_kind"] = REPLAY_ERROR_KIND
    record["attempts"] = attempts
    record["retries"] = max(attempts - 1, 0)
    if retried:
        record["retried"] = retried
    return record


def _transient_backoff(env: WorkerEnv, sink: Any, seconds: float) -> None:
    """Wait ``seconds`` before a retry (N12-obs55), cancel-aware in slices of at most
    ``runtime_invoke.BACKOFF_SLICE_S`` (N13-obs8): the cancel marker is polled between the
    slices, so a stop during a 15 s wait raises ``JobCancelled`` within about a second
    instead of after the whole wait; the loop's checkpoint re-checks it right after."""
    if seconds <= 0:
        return
    if not runtime_invoke.sliced_wait(seconds, env.sleep, stop=sink.cancelled):
        raise JobCancelled(sink.job.job_id)


def _retried_suffix(record: dict[str, Any]) -> str:
    """`` (retried after: #1 <reason>, waited 5 s; #2 …)`` for a line that retried, else
    empty — the attempt count AND the cause of each retry reach the error record / the hub's
    ``session_failures`` message (N12-obs55)."""
    retried = record.get("retried") or []
    if not retried:
        return ""
    parts = []
    for entry in retried:
        waited = float(entry.get("backoff_s") or 0)
        parts.append(f"#{entry.get('attempt')} {entry.get('reason', '')}"
                     + (f", waited {waited:g} s" if waited > 0 else ""))
    return " (retried after: " + "; ".join(parts) + ")"


def _replay_failure_text(record: dict[str, Any]) -> str:
    """``scenario <id> failed after k/N attempts: <Code>: <error>`` — the one wording the
    invocation line, the error records and the all-failed reason share. A session that
    answered but whose telemetry never became ready (N-SCN-bug3) reads ``scenario <id>
    answered but its telemetry never became ready: <Code>: <error>``."""
    if record.get("failure") == TELEMETRY_FAILURE:
        return (
            f"scenario {record.get('scenario_id')} answered but its telemetry never became "
            f"ready: {record.get('code')}: {record.get('error', '')}"
        )
    return (
        f"scenario {record.get('scenario_id')} failed after "
        f"{record.get('attempts', 1)}/{TRANSIENT_SCENARIO_ATTEMPTS} attempts: "
        f"{record.get('code')}: {record.get('error', '')}" + _retried_suffix(record)
    )


def content_log_groups_for(wait: dict[str, Any], session: dict[str, Any]) -> list[str]:
    """The runtime log group(s) a replayed session's content log can be in (N-SCN-bug3).

    In order: the ``content_log_group`` the hub stamped on the session, the job-wide
    ``wait.content_log_group``, the ``wait.log_groups`` entry ending in ``-<qualifier>`` (a
    canary's named endpoint → its own group), else EVERY listed group but ``aws/spans`` — so
    an unresolvable arm probes all of them rather than the alphabetically first."""
    own = str(session.get("content_log_group") or "")
    if own:
        return [own]
    fixed = str(wait.get("content_log_group") or "")
    if fixed:
        return [fixed]
    groups = [str(g) for g in wait.get("log_groups") or [] if g != SPANS_LOG_GROUP]
    qualifier = str(session.get("qualifier") or "")
    if qualifier:
        matched = [g for g in groups if g.endswith(f"-{qualifier}")]
        if matched:
            return matched
    return groups


def _mark_telemetry_failed(sink: Any, failed: dict[str, dict[str, Any]], session_id: str,
                           record: dict[str, Any], groups: tuple[str, ...],
                           exc: BaseException) -> None:
    log.warning("session %s (scenario %s) telemetry never became ready in %s: %s",
                session_id, record.get("scenario_id"), list(groups), str(exc)[:300])
    errored = _telemetry_failure_record(record, exc)
    sink.invocation(errored)
    failed[session_id] = errored


def _telemetry_failure_record(record: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    """The updated ``invocations.jsonl`` line of a session that answered but whose spans /
    content log never became ready: the hub reads the LAST line per session."""
    failed = dict(record)
    failed["status"] = "failed"
    failed["code"] = type(exc).__name__
    failed["error"] = str(exc)[:500]
    failed["error_kind"] = REPLAY_ERROR_KIND
    failed["attempts"] = int(record.get("retries") or 0) + 1
    failed["failure"] = TELEMETRY_FAILURE
    return failed


def replay_error_records(job_id: str, record: dict[str, Any],
                         evaluator_defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One error record per evaluator for a session whose replay failed: ``error_type`` is
    the replay's exception class (``ReadTimeoutError``, …), the message is the shared
    failure text, and ``error_kind: infrastructure`` marks it as unanswered rather than
    judged. The hub's N12-bug4 summary counts these like any errored record. N12-obs62: the
    record also carries the session's ``arm`` (the hub's pre-assigned ``C`` / ``T1``, None
    for a plain run) so a canary's failures can be attributed without the assignment rows."""
    session_id = str(record["session_id"])
    text = _replay_failure_text(record)
    out: list[dict[str, Any]] = []
    for definition in evaluator_defs:
        rec = error_record(job_id=job_id, session_id=session_id,
                           evaluator_id=str(definition.get("id")),
                           level=evaluator_lib.level_of(definition), error=RuntimeError(text))
        rec["error_type"] = str(record.get("code") or "error")
        rec["error_kind"] = REPLAY_ERROR_KIND  # CN additive (N-SCN-bug1)
        rec["arm"] = record.get("arm")  # CN additive (N12-obs62)
        out.append(rec)
    return out


def _replay_dataset(env: WorkerEnv, sink: Any, spec: dict[str, Any],
                    telemetry_start_ms: int,
                    references: dict[str, dict[str, Any]] | None = None,
                    not_before: dict[str, int] | None = None,
                    turns: dict[str, int] | None = None,
                    ) -> dict[str, dict[str, Any]]:
    """Replay every ``spec.sessions[]`` entry flagged ``replay`` (phase ``invoking``), then
    wait for the newest successfully replayed session's spans to be queryable (phase
    ``waiting``) — the ONE replay path for both ``batch_eval`` and ``insights``. N12-bug9:
    the insights job used to read the sessions straight after the backend had minted their
    ids, so a dataset-scoped insights run died in seconds with ``SessionNotFoundError: no
    spans found``; a trace-window job carries ``replay: False`` entries and skips both
    phases.

    Returns ``{session_id: invocation record}`` of the scenarios that FAILED to replay
    (N-SCN-bug1): the run goes on without them; only when every replayed scenario failed
    does this raise, with the first failure as the reason. A cancel still raises
    ``JobCancelled`` at the next checkpoint. ``references`` (eval accuracy item 1) receives
    ``{session_id: extraction_check.reference("replay", answers)}`` per answered session;
    ``not_before`` (N17-obs323) ``{session_id: epoch ms}`` for a session answered on a retry —
    the evaluation scores only that last attempt's traces. ``turns`` (D3-c) receives
    ``{session_id: turns the last attempt sent}`` for every replayed session."""
    sessions = list(spec.get("sessions") or [])
    wait = dict(spec.get("wait") or {})
    to_replay = [s for s in sessions if s.get("replay", True)]
    total = len(to_replay)
    failed: dict[str, dict[str, Any]] = {}
    # N-SCN-bug3: the answered sessions PER content log group, oldest → newest (a canary arm
    # writes its own runtime log group); insertion order = the order the groups were first hit
    ok_by_group: dict[tuple[str, ...], list[tuple[str, dict[str, Any]]]] = {}
    sink.status("invoking", done=0, total=total, message=f"replaying 0/{total}")
    for index, session in enumerate(to_replay, 1):
        def on_retry(next_attempt: int, exc: BaseException, _i: int = index,
                     _s: dict[str, Any] = session) -> None:
            sink.status(
                "invoking", done=_i - 1, total=total,
                message=(f"replaying {_i}/{total}: scenario {_s.get('scenario_id')} attempt "
                         f"{next_attempt}/{TRANSIENT_SCENARIO_ATTEMPTS} after "
                         f"{type(exc).__name__}"),
            )

        answers: list[str] = []
        record = _replay_session(env, spec, session, sink, on_retry, answers)
        sink.invocation(record)
        if turns is not None:
            turns[str(session["session_id"])] = int(record.get("turns_replayed") or 0)
        if (record["status"] == "ok" and not_before is not None
                and record.get("last_attempt_started_ms")):
            not_before[str(session["session_id"])] = int(record["last_attempt_started_ms"])
        # N17-obs419: an empty collected answer is no reference — the session's own runtime
        # content records are (the engine's runtime_logs fallback), never ""
        if (record["status"] == "ok" and references is not None and answers
                and all(str(a).strip() for a in answers)):
            references[str(session["session_id"])] = extraction_check.reference(
                extraction_check.SOURCE_REPLAY, answers)
        if record["status"] == "ok":
            key = tuple(content_log_groups_for(wait, session))
            ok_by_group.setdefault(key, []).append((str(session["session_id"]), record))
        else:
            failed[str(session["session_id"])] = record
        suffix = f" ({len(failed)} failed)" if failed else ""
        sink.status("invoking", done=index, total=total,
                    message=f"replaying {index}/{total}{suffix}")
    if to_replay and len(failed) == total:
        first = next(iter(failed.values()))
        raise RuntimeError(
            f"all {total} scenarios failed to replay — first error: "
            f"{_replay_failure_text(first)}"
        )
    for index, (groups, answered) in enumerate(ok_by_group.items(), 1):
        suffix = f" · log group {index}/{len(ok_by_group)}" if len(ok_by_group) > 1 else ""
        sink.status("waiting", done=total, total=total, message=f"waiting for spans{suffix}")
        newest_id, newest_record = answered[-1]
        try:
            _wait_for_spans(env, sink, wait, newest_id, telemetry_start_ms, groups=list(groups))
            continue
        except TelemetryReadinessTimeout as exc:
            # N-SCN-bug3: the probed session is an errored session, not a failed job …
            _mark_telemetry_failed(sink, failed, newest_id, newest_record, groups, exc)
        # … and the group's older sessions get one probe each (the group has had the full
        # wait by now): those whose content landed are evaluated, the rest are errored too
        for session_id, record in answered[:-1]:
            try:
                _wait_for_spans(env, sink, wait, session_id, telemetry_start_ms,
                                groups=list(groups), single_probe=True)
            except TelemetryReadinessTimeout as exc:
                _mark_telemetry_failed(sink, failed, session_id, record, groups, exc)
    if to_replay and len(failed) == total:
        first = next(iter(failed.values()))  # session order: replay failures came first
        raise RuntimeError(
            f"all {total} scenarios failed — first error: {_replay_failure_text(first)}"
        )
    return failed


def run_deadline_rows(job_id: str, session_ids: list[str],
                            evaluator_defs: list[dict[str, Any]],
                            deadline_s: float) -> list[dict[str, Any]]:
    """One ``run_deadline`` error record per evaluator of each unreached session."""
    exc = deadlines.RunDeadlineReached(deadline_s)
    return [error_record(job_id=job_id, session_id=session_id,
                         evaluator_id=str(definition.get("id")),
                         level=evaluator_lib.level_of(definition), error=exc)
            for session_id in session_ids for definition in evaluator_defs]


def run_batch_eval(job: JobSpec | dict[str, Any], sink: Any, env: WorkerEnv) -> None:
    job, spec = _spec(job)
    job_started = env.clock()
    sessions = list(spec.get("sessions") or [])
    session_ids = [str(s["session_id"]) for s in sessions]
    wait = dict(spec.get("wait") or {})
    replay = bool(spec.get("replay", True))
    budget_stops: list[dict[str, Any]] = []
    telemetry_start_ms = int(env.clock() * 1000) - TELEMETRY_QUERY_LOOKBACK_MS
    failed: dict[str, dict[str, Any]] = {}
    # eval accuracy item 1: the hub's chat-ledger references (session scope), overridden by
    # what this worker collected while replaying; every evaluated session gets a check
    references: dict[str, dict[str, Any]] = dict(spec.get("extraction_references") or {})
    checks: dict[str, dict[str, Any]] = {}
    not_before: dict[str, int] = {}
    turns: dict[str, int] = {}
    try:
        if replay:
            failed = _replay_dataset(env, sink, spec, telemetry_start_ms, references,
                                     not_before, turns)
        # N17-obs400: sessions the run did NOT replay (explicit session ids, already-invoked
        # sessions) get the readiness wait too — it lived in the replay path only
        explicit = [s for s in sessions if not s.get("replay", True)
                    and str(s["session_id"]) not in failed]
        _await_explicit_sessions(env, sink, wait, explicit, failed)
        _check_cancel(sink)
        total = len(session_ids)
        sink.status("evaluating", done=0, total=total, message=f"evaluating 0/{total}")
        provider = LaunchpadSpansProvider(
            env.logs_client, list(wait.get("log_groups") or []),
            lookback_hours=int(wait.get("lookback_hours") or 48),
            spans_source=str(wait.get("spans_source") or env.spans_source),  # type: ignore[arg-type]
        )
        if explicit and int(wait.get("seconds") or 0) > 0:
            provider = PatientProvider(provider, {str(s["session_id"]) for s in explicit},
                                       window_s=int(wait.get("seconds") or 0),
                                       sleep=env.sleep, clock=env.clock)
        # P2.2: every judge call runs under the hub's wall-clock deadline; P8.3: its tokens
        # are counted per unit (P8.4: up to the job's token budget)
        usage = deadlines.UsageLedger()
        token_budget = int(spec.get("token_budget") or 0) or None
        # P2.2 follow-up: the run's wall-clock bound (the hub derives it from its own wait)
        run_deadline = float(spec.get("run_deadline_s") or 0) or None
        deadline_at = job_started + run_deadline if run_deadline else None

        def past_deadline() -> bool:
            return deadline_at is not None and env.clock() >= deadline_at
        evaluators = deadlines.DeadlineEvaluatorSet(
            list(spec.get("evaluator_defs") or []), env.judge_factory,
            deadline_s=deadlines.deadline_of(spec), usage=usage,
            # N17-obs327: code evaluators get Global's envelope (session documents + truth)
            lambda_client=code_evaluators.runtime_for(
                env.lambda_client, logs_client=env.logs_client,
                log_groups=list(wait.get("log_groups") or []),
                lookback_hours=int(wait.get("lookback_hours") or 48),
                ground_truth=spec.get("ground_truth") or {}),
            default_model_id=spec.get("judge_model_id") or env.default_model_id,
            code_evaluators_enabled=env.code_evaluators_enabled,
            budget_cap=int(spec.get("judge_max_tokens_cap") or env.judge_budget_cap),
            default_budget=_default_budget(env, spec),
            transient_backoff=env.transient_backoff_s,
            fallback_model_id=spec.get("judge_fallback_model_id"),  # N17-obs385
        )
        progress_state = {"done": 0}

        def progress(message: str) -> None:
            progress_state["done"] += 1
            sink.status("evaluating", done=progress_state["done"], total=total, message=message)

        # N-SCN-bug1: an unanswered scenario is scored as invalid up front — one error record
        # per evaluator — and the engine only sees the sessions that have spans to read.
        records: list[dict[str, Any]] = []
        for session_id in session_ids:
            if session_id in failed:
                recs = replay_error_records(job.job_id, failed[session_id],
                                            list(spec.get("evaluator_defs") or []))
                sink.results(recs)
                records.extend(recs)
                progress(f"evaluating {progress_state['done'] + 1}/{total}")
        to_evaluate = [s for s in session_ids if s not in failed]
        seen_tools: dict[str, list[str]] = {}  # N17-obs407: the hub's expected-tool hints
        tool_units: dict[str, int] = {}  # N17-obs416: each session's tool calls (coverage)
        skill_units: dict[str, int] = {}  # N17-obs435: each session's skill loads (coverage)
        records.extend(engine_core.evaluate_sessions(
            _ToolUnits(_ObservedTools(provider, seen_tools), tool_units, skill_units),
            to_evaluate,
            evaluators,
            spec.get("ground_truth") or {},
            job_id=job.job_id, sleep=env.sleep,
            # P8.4: a spent token budget starts no further session / evaluator
            stop=lambda: sink.cancelled() or usage.spent(token_budget) or past_deadline(),
            progress=lambda _m: progress(f"evaluating {progress_state['done'] + 1}/{total}"),
            on_records=lambda _sid, recs: (usage.stamp(recs), sink.results(recs),
                                           _write_usage(sink, usage)),
            concurrency=env.judge_concurrency,
            references=references, on_check=checks.__setitem__, not_before=not_before,
        ))
        deadline_hit = past_deadline() and not sink.cancelled()
        if deadline_hit:
            # sessions the job never reached: typed rows, never silently absent
            judged = {str(r.get("session_id")) for r in records}
            unreached = run_deadline_rows(job.job_id, [s for s in to_evaluate
                                                             if s not in judged],
                                                list(spec.get("evaluator_defs") or []),
                                                run_deadline or 0.0)
            if unreached:
                sink.results(unreached)
                records.extend(unreached)
        state = "STOPPED" if sink.cancelled() else "COMPLETED"
        sink.finish(state, error="stopped by operator" if state == "STOPPED" else None,
                    summary={"scores": aggregate_scores(records), "budget_stops": budget_stops,
                             "token_usage": usage.totals(),
                             **({"run_deadline_reached": True} if deadline_hit else {}),
                             **({"token_budget_exhausted": True}
                                if usage.spent(token_budget) else {}),
                             **_replay_failures_summary(failed), **_checks_summary(checks),
                             **({"replay_turns": turns} if turns else {}),
                             "observed_tools": seen_tools, "tool_units": tool_units,
                             "skill_units": skill_units})
    except JobCancelled:
        sink.finish("STOPPED", error="stopped by operator",
                    summary={"scores": [], "budget_stops": budget_stops,
                             **_checks_summary(checks)})


def _stable(env: WorkerEnv, wait: dict[str, Any], session_id: str, groups: list[str],
            start_ms: int, stability_s: int) -> bool:
    """One readiness probe of an explicit session (the replay gate, asked once): its newest
    root is paired with its content in one of ``groups`` and that content is
    ``stability_s`` old (Logs Insights indexes behind ingestion — the gate's watermark)."""
    if not groups:
        return True
    try:
        wait_for_evaluation_telemetry(
            env.logs_client, session_id=session_id, content_log_group=groups[0],
            content_log_groups=groups[1:], start_time_ms=start_ms,
            stability_seconds=stability_s, timeout_seconds=0, clock=env.clock,
            sleep=env.sleep, spans_source=str(wait.get("spans_source") or env.spans_source))
    except TelemetryReadinessTimeout:
        return False
    return True


def _await_explicit_sessions(env: WorkerEnv, sink: Any, wait: dict[str, Any],
                             sessions: list[dict[str, Any]],
                             failed: dict[str, dict[str, Any]]) -> None:
    """N17-obs400: the readiness wait for sessions the run does not replay. The probe window
    is the run's whole lookback (``lookback_hours``), not "since the job started": a session
    that ended long ago passes on the first probe and costs nothing. A fresh one is polled
    until its content is ``wait.seconds`` old, for at most ``wait.seconds`` + the grace; a
    session still unready then is a telemetry failure (error rows, ``failure: telemetry``)
    and the job goes on — exactly like an unready replayed session."""
    seconds = int(wait.get("seconds") or 0)
    if seconds <= 0 or not sessions:
        return
    start_ms = int((env.clock() - int(wait.get("lookback_hours") or 48) * 3600) * 1000)
    pending = {str(s["session_id"]): (s, content_log_groups_for(wait, s)) for s in sessions}
    deadline = env.clock() + seconds + TELEMETRY_READY_GRACE_SECONDS
    announced = False
    while True:
        for session_id, (_s, groups) in list(pending.items()):
            if _stable(env, wait, session_id, groups, start_ms, seconds):
                pending.pop(session_id)
        if not pending or env.clock() >= deadline:
            break
        if not announced:
            sink.status("waiting", done=len(sessions) - len(pending), total=len(sessions),
                        message=f"waiting for spans of {len(pending)} session(s)")
            announced = True
        _check_cancel(sink)
        env.sleep(min(EXPLICIT_POLL_S, max(0.0, deadline - env.clock())))
    for session_id, (session, groups) in pending.items():
        exc = TelemetryReadinessTimeout(
            f"evaluation telemetry did not become ready for session {session_id} within "
            f"{seconds + TELEMETRY_READY_GRACE_SECONDS} s (root span / content log missing "
            f"or younger than {seconds} s)")
        record = {"session_id": session_id, "scenario_id": session.get("scenario_id"),
                  "arm": session.get("arm"), "status": "skipped"}
        _mark_telemetry_failed(sink, failed, session_id, record, tuple(groups), exc)


class PatientProvider:
    """N17-obs400: the spans provider of a run with explicit sessions — a session of
    ``patient`` whose Logs Insights fetch answers ``SessionNotFoundError`` ("no spans
    found": the probe saw the records, the index has not caught up) is fetched again every
    ``EXPLICIT_POLL_S`` for up to ``window_s``, then the error stands (an error row, as
    before). Everything else is the wrapped provider's."""

    def __init__(self, inner: Any, patient: set[str], *, window_s: int,
                 sleep: Callable[[float], None], clock: Callable[[], float]) -> None:
        self._inner, self._patient = inner, set(patient)
        self._window_s, self._sleep, self._clock = int(window_s), sleep, clock

    def get_evaluation_data(self, session_id: str) -> Any:
        deadline = self._clock() + self._window_s
        while True:
            try:
                return self._inner.get_evaluation_data(session_id)
            except SessionNotFoundError:
                if session_id not in self._patient or self._clock() >= deadline:
                    raise
                log.info("session %s: no spans in Logs Insights yet — re-fetching", session_id)
                self._sleep(min(EXPLICIT_POLL_S, max(0.0, deadline - self._clock())))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ObservedTools:
    """N17-obs407: the spans provider, recording the tool names each evaluated session's spans
    show (``{session_id: [name]}``) for the hub's expected-tool warnings. Everything else is
    the provider's own."""

    def __init__(self, provider: Any, seen: dict[str, list[str]]) -> None:
        self._provider = provider
        self._seen = seen

    def get_evaluation_data(self, session_id: str) -> Any:
        data = self._provider.get_evaluation_data(session_id)
        try:
            names = evaluator_lib.actual_tool_names(data["trajectory"])
            self._seen[str(session_id)] = sorted({n for n in names if n})
        except Exception:  # noqa: BLE001 — a hint, never a reason to fail the session
            pass
        return data

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)


def _write_usage(sink: Any, usage: Any) -> None:
    """N17-obs417: the attempt's judge tokens so far (best effort)."""
    artifact = getattr(sink, "artifact", None)
    if artifact is None:
        return
    try:
        artifact(USAGE_ARTIFACT, usage.totals())
    except Exception:  # noqa: BLE001 — accounting never fails a job
        pass


class _ToolUnits:
    """N17-obs416: the spans provider, counting each evaluated session's tool calls (its
    ``execute_tool`` spans — the units a TOOL_CALL evaluator judges) for the hub's coverage,
    and its skill loads (``engine_core.skill_loads`` — the units the skill builtins judge,
    N17-obs435)."""

    def __init__(self, provider: Any, counts: dict[str, int],
                 skill_counts: dict[str, int] | None = None) -> None:
        self._provider = provider
        self._counts = counts
        self._skill_counts = skill_counts if skill_counts is not None else {}

    def get_evaluation_data(self, session_id: str) -> Any:
        data = self._provider.get_evaluation_data(session_id)
        try:
            self._counts[str(session_id)] = sum(
                isinstance(span, ToolExecutionSpan)
                for trace in data["trajectory"].traces for span in trace.spans)
        except Exception:  # noqa: BLE001 — a hint, never a reason to fail the session
            pass
        try:
            self._skill_counts[str(session_id)] = len(
                engine_core.skill_loads(data["trajectory"]))
        except Exception:  # noqa: BLE001 — a hint, never a reason to fail the session
            pass
        return data

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)


def _checks_summary(checks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """``done.json`` summary extra (eval accuracy item 1): ``extraction_checks`` =
    ``{session_id: extraction_check}`` of every evaluated session (the hub folds the shards
    and summarises); empty dict when no session was evaluated."""
    return {"extraction_checks": dict(checks)} if checks else {}


def _default_budget(env: WorkerEnv, spec: dict[str, Any]) -> int | None:
    """The budget the job's default judge starts at (N-SCN-bug2): the spec's
    ``judge_max_tokens`` when the hub sent one, else what the host's factory would build with."""
    if spec.get("judge_max_tokens"):
        return int(spec["judge_max_tokens"])
    if env.judge_budget is not None:
        return int(env.judge_budget(spec.get("judge_model_id") or env.default_model_id))
    return None


def _replay_failures_summary(failed: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """``done.json`` summary extra (CN additive, N-SCN-bug1): the scenarios the run went on
    without — empty dict when none, so an untouched run's summary stays byte-identical."""
    if not failed:
        return {}
    return {"replay_failures": [
        {"session_id": sid, "scenario_id": rec.get("scenario_id"), "code": rec.get("code"),
         "attempts": rec.get("attempts"), "error": rec.get("error", ""),
         # N12-obs62: the hub's pre-assigned arm (`C` / `T1`; None for a plain run)
         "arm": rec.get("arm"),
         # upstream #251: a skipped simulated persona (the hub's `scenario_failures`) — the
         # key exists only on persona entries, so a predefined scenario's entry is unchanged
         **({"simulated": True} if rec.get("simulated") else {})}
        for sid, rec in failed.items()
    ]}


def _wait_for_spans(env: WorkerEnv, sink: Any, wait: dict[str, Any], session_id: str,
                    start_ms: int, *, groups: list[str] | None = None,
                    single_probe: bool = False) -> None:
    """Readiness gate on ``session_id``. ``groups`` (N-SCN-bug3) are the content log groups
    the session may have written to — the first is probed as before, the rest as fallbacks
    on every poll; without it the job-wide ``content_log_group`` / first listed group is used
    (the optimize rollout path, one endpoint). ``single_probe`` asks once (zero stability,
    zero timeout) and raises ``TelemetryReadinessTimeout`` when the content is not there."""
    seconds = 0 if single_probe else int(wait.get("seconds") or 0)
    if groups is None:
        groups = content_log_groups_for(wait, {})[:1]
    if not groups:
        return

    def cancel_aware_sleep(delay: float) -> None:
        _check_cancel(sink)
        env.sleep(delay)

    wait_for_evaluation_telemetry(
        env.logs_client, session_id=session_id, content_log_group=groups[0],
        content_log_groups=groups[1:],
        start_time_ms=start_ms, stability_seconds=seconds,
        timeout_seconds=0 if single_probe else seconds + TELEMETRY_READY_GRACE_SECONDS,
        clock=env.clock, sleep=cancel_aware_sleep,
        spans_source=str(wait.get("spans_source") or env.spans_source),
    )


def run_insights(job: JobSpec | dict[str, Any], sink: Any, env: WorkerEnv) -> None:
    job, spec = _spec(job)
    session_ids = [str(s["session_id"]) for s in spec.get("sessions") or []]
    wait = dict(spec.get("wait") or {})
    telemetry_start_ms = int(env.clock() * 1000) - TELEMETRY_QUERY_LOOKBACK_MS
    failed: dict[str, dict[str, Any]] = {}
    try:
        if bool(spec.get("replay", True)):
            # N12-bug9: a dataset-scoped insights run replays its scenarios and waits for
            # their spans exactly as batch_eval does; a trace window has nothing to replay
            failed = _replay_dataset(env, sink, spec, telemetry_start_ms)
        _check_cancel(sink)
    except JobCancelled:
        sink.finish("STOPPED", error="stopped by operator",
                    summary={"insights": None, "scores": [], "budget_stops": []})
        return
    total = len(session_ids)
    sink.status("evaluating", done=0, total=total, message=f"analysing 0/{total}")
    provider = LaunchpadSpansProvider(
        env.logs_client, list(wait.get("log_groups") or []),
        lookback_hours=int(wait.get("lookback_hours") or 48),
        spans_source=str(wait.get("spans_source") or env.spans_source),  # type: ignore[arg-type]
    )
    judge = env.judge_factory(spec.get("judge_model_id") or env.default_model_id)
    # N17-obs449: FailureAnalysis on the `failure_detection` model role's judge (an older hub
    # names none: the run's judge does it, as before)
    detection_model = spec.get("failure_detection_model_id")
    detection_judge = env.judge_factory(detection_model) if detection_model else None
    state = {"done": 0}

    def progress(message: str) -> None:
        state["done"] += 1
        sink.status("evaluating", done=state["done"], total=total, message=message)

    to_diagnose = [s for s in session_ids if s not in failed]
    blob = insights.diagnose_sessions(
        provider, to_diagnose, list(spec.get("kinds") or []), judge, stop=sink.cancelled,
        progress=progress,
        budget_cap=int(spec.get("judge_max_tokens_cap") or env.judge_budget_cap),
        detection_judge=detection_judge,
    )
    if failed:
        # N-SCN-bug1: an unanswered scenario is an errored insight session, in the same
        # `{session_id, error: "Type: message"}` shape the detectors use (hub N12-bug4 summary)
        blob.setdefault("errors", []).extend(
            {"session_id": sid, "error": f"{rec.get('code')}: {_replay_failure_text(rec)}"[:300],
             "error_kind": REPLAY_ERROR_KIND, "arm": rec.get("arm")}  # arm: N12-obs62
            for sid, rec in failed.items()
        )
        for _ in failed:
            progress(f"analysing {state['done'] + 1}/{total}")
    sink.artifact("insights.json", blob)
    sink.finish("STOPPED" if sink.cancelled() else "COMPLETED",
                error="stopped by operator" if sink.cancelled() else None,
                summary={"insights": blob, "scores": [], "budget_stops": [],
                         **_replay_failures_summary(failed)})


HANDLERS: dict[str, Callable[[Any, Any, WorkerEnv], None]] = {
    "batch_eval": run_batch_eval,
    "insights": run_insights,
}


def register_handler(job_type: str, handler: Callable[[Any, Any, WorkerEnv], None]) -> None:
    """W-E2's ``online_eval`` / ``recommend`` / ``optimize`` handlers plug in here."""
    HANDLERS[job_type] = handler


# W-E2's worker-side handlers (``strands_eval/jobs_{online,recommend,optimize}.py``), keyed
# on the module names the inliner ships (templates/eval_worker._OPTIONAL_SOURCES). Each module
# exposes ``run(job, sink, *, <dependencies>)``; the dispatcher binds the dependencies from
# the ``WorkerEnv`` — per job for ``optimize``, whose ``invoke`` / ``score`` depend on the
# job's target and evaluator (evaluation-engine.md §13 / §19).
OPTIMIZE_FLUSH_SECONDS = 30  # spans stability before a rollout session is scored


def _online_handler(module: Any) -> Callable[[Any, Any, WorkerEnv], None]:
    def handler(job: Any, sink: Any, env: WorkerEnv) -> None:
        # N13-obs14: the online judge path takes the same transient backoff as the batch one
        module.run(job, sink, logs_client=env.logs_client, judge_factory=env.judge_factory,
                   lambda_client=env.lambda_client, spans_source=env.spans_source,
                   sleep=env.sleep, transient_backoff=env.transient_backoff_s)
    return handler


def _recommend_handler(module: Any) -> Callable[[Any, Any, WorkerEnv], None]:
    def handler(job: Any, sink: Any, env: WorkerEnv) -> None:
        module.run(job, sink, judge_factory=env.judge_factory,
                   converse=env.extra.get("converse"))
    return handler


def _optimize_handler(module: Any) -> Callable[[Any, Any, WorkerEnv], None]:
    def handler(job: Any, sink: Any, env: WorkerEnv) -> None:
        job, spec = _spec(job)
        invoke, score = optimize_bindings(env, sink, spec, job_id=job.job_id)
        module.run(job, sink, judge_factory=env.judge_factory,
                   invoke=env.extra.get("invoke") or invoke,
                   score=env.extra.get("score") or score,
                   s3_client=env.extra.get("s3_client"),
                   optimizer_factory=env.extra.get("optimizer_factory"))
    return handler


def optimize_bindings(
    env: WorkerEnv, sink: Any, spec: dict[str, Any], *, job_id: str,
) -> tuple[Callable[..., str], Callable[..., float | None]]:
    """``invoke(prompt, *, session_id, bundle_ref, baggage, launchpad_meta) -> str`` replays a
    rollout against ``spec.target`` (the same ``_invoke`` the batch replay uses, so the
    ``env.invoke`` seam and the bundle baggage ride along); ``score(session_id, prompt,
    output) -> float | None`` waits for the session's spans to flush, then evaluates it with
    ``spec.evaluator_def`` through the spans provider — mean of the evaluator's scores,
    ``None`` when the judge produced no score (``LaunchpadReward`` records a 0)."""
    target = dict(spec.get("target") or {})
    definition = dict(spec.get("evaluator_def") or {})
    log_groups = [str(g) for g in spec.get("log_groups") or []]
    wait = {"log_groups": log_groups, "seconds": int(spec.get("wait_seconds")
                                                   or OPTIMIZE_FLUSH_SECONDS),
            "spans_source": spec.get("spans_source")}
    started_ms: dict[str, int] = {}
    provider = LaunchpadSpansProvider(
        env.logs_client, log_groups, lookback_hours=int(spec.get("lookback_hours") or 48),
        spans_source=str(wait.get("spans_source") or env.spans_source),  # type: ignore[arg-type]
    )
    evaluators = deadlines.DeadlineEvaluatorSet(
        [definition] if definition else [], env.judge_factory,
        deadline_s=deadlines.deadline_of(spec),
        lambda_client=code_evaluators.runtime_for(
            env.lambda_client, logs_client=env.logs_client, log_groups=log_groups,
            lookback_hours=int(spec.get("lookback_hours") or 48)),
        default_model_id=spec.get("judge_model_id") or env.default_model_id,
        code_evaluators_enabled=env.code_evaluators_enabled,
        budget_cap=int(spec.get("judge_max_tokens_cap") or env.judge_budget_cap),
        default_budget=_default_budget(env, spec),
        transient_backoff=env.transient_backoff_s,
    )

    def invoke(prompt: str, *, session_id: str, bundle_ref: str, baggage: str | None,
               launchpad_meta: dict[str, Any] | None) -> str:
        _check_cancel(sink)
        started_ms[session_id] = int(env.clock() * 1000)
        session = {"session_id": session_id, "bundle_ref": bundle_ref, "baggage": baggage,
                   "launchpad_meta": launchpad_meta, "qualifier": target.get("qualifier")}
        reply = _invoke(env, target, session, prompt, session_id)
        return str(reply.get("text") or "")

    def score(session_id: str, prompt: str, output: str) -> float | None:
        if not definition:
            raise RuntimeError("optimize job without an evaluator_def: nothing to score with")
        _wait_for_spans(env, sink, wait, session_id,
                        started_ms.get(session_id) or int(env.clock() * 1000) - 60_000)
        records = engine_core.evaluate_session(
            provider, session_id, evaluators, None, job_id=job_id, sleep=env.sleep,
            stop=sink.cancelled,
        )
        _check_cancel(sink)
        scores = [float(r["score"]) for r in records if r.get("score") is not None]
        if not scores:
            errors = [str(r.get("error")) for r in records if r.get("error")]
            if errors:
                raise RuntimeError(errors[0])
            return None
        return sum(scores) / len(scores)

    return invoke, score


OPTIONAL_HANDLERS: tuple[tuple[str, str, Callable[[Any], Any]], ...] = (
    ("online_eval", "jobs_online", _online_handler),
    ("recommend", "jobs_recommend", _recommend_handler),
    ("optimize", "jobs_optimize", _optimize_handler),
)
_OPTIONAL_PROBED: set[str] = set()


def register_optional_handlers() -> list[str]:
    """Import the E2 handler modules that exist next to this one (``app.services.strands_eval``
    in the backend, ``launchpad_eval`` in the worker zip) and register their handlers.
    Idempotent; a missing module is skipped (probed once per process), any other import error
    propagates — returns the job types registered by this call."""
    registered: list[str] = []
    for job_type, module_name, build in OPTIONAL_HANDLERS:
        if job_type in HANDLERS or module_name in _OPTIONAL_PROBED:
            continue
        try:
            module = importlib.import_module(f"{__package__}.{module_name}")
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.endswith(module_name):
                _OPTIONAL_PROBED.add(module_name)
                continue
            raise
        HANDLERS[job_type] = build(module)
        registered.append(job_type)
    return registered


HEARTBEAT_S = 60.0  # N17-obs413: status.json is re-written this often while a job runs
# N17-obs417: the per-attempt judge-token sidecar, re-written as every session finishes, so
# an attempt that fails or is lost still says what it spent
USAGE_ARTIFACT = "usage.json"


def run_job(job: JobSpec | dict[str, Any], sink: Any, env: WorkerEnv) -> None:
    """Dispatch one job by ``job_type``; an unimplemented kind is a FAILED job with the
    protocol's ``evaluation.kind_unsupported`` code (the entrypoint refuses reserved kinds
    before a job ever reaches here). N17-obs413: the sink heartbeats its last status every
    ``HEARTBEAT_S`` until the job finishes."""
    job, _ = _spec(job)
    start_heartbeat = getattr(sink, "start_heartbeat", None)
    if start_heartbeat is not None:
        start_heartbeat(HEARTBEAT_S)
    try:
        validate_job_type(job.job_type)
    except KindUnsupportedError as exc:
        sink.finish("FAILED", error=f"{exc.code}: {exc}")
        return
    handler = HANDLERS.get(job.job_type)
    if handler is None:
        register_optional_handlers()
        handler = HANDLERS.get(job.job_type)
    if handler is None:
        sink.finish("FAILED", error=f"evaluation.kind_unsupported: no handler for "
                                    f"{job.job_type!r} is inlined into this worker")
        return
    handler(job, sink, env)
