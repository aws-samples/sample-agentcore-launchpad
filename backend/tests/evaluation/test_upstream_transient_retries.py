"""N12-obs53 / N12-obs55 — the evaluation worker retries transient UPSTREAM errors with
backoff, on BOTH the scenario replay and the judge call, through ONE shared predicate.

Live 2026-10-16, three incidents on the same Global-Bedrock cross-partition path, all a
Bedrock ``internalServerException`` on ConverseStream: 02:13Z an architect turn (cn/r11-j:
replay with 15 s backoff), 09:44Z an eval JUDGE call (record errored with
``EventStreamError``, run completed with errors — never retried), 13:29Z an eval AGENT
REPLAY (``failed after 1/3 attempts: RuntimeError: runtime returned error: An error occurred
(internalServerException)…`` — a 3-attempt budget existed but this error class was not
classified transient). Now ``runtime_invoke.is_upstream_transient`` (re-exported by
``services/agentcore/runtime.py`` for the assistant) classifies all three, the replay and
the judge wait ``Settings.eval_transient_retry_backoff_seconds`` (5 s, 15 s) between their
3 attempts, and a failure that exhausts the budget records every retry's cause.
"""

from __future__ import annotations

import dataclasses
import types

import pytest
from botocore.exceptions import ClientError, EventStreamError, ReadTimeoutError
from pydantic import ValidationError

from app.assistant import runtime as architect_runtime
from app.assistant import service as assistant_service
from app.core.config import Settings, get_settings, parse_backoff_seconds
from app.evaluation.telemetry import TelemetryReadinessTimeout
from app.services.agentcore import runtime as rt
from app.services.strands_eval import engine_core, jobs_batch, model_calls, runtime_invoke
from app.services.strands_eval.results import error_record
from tests.evaluation.test_jobs_replay_failures import (
    S1,
    S2,
    S3,
    Sink,
    _job,
    _spec,
    harness,  # noqa: F401 — registered here
)

N = jobs_batch.TRANSIENT_SCENARIO_ATTEMPTS
SLICE = runtime_invoke.BACKOFF_SLICE_S


def _sliced(sleeps: list[float], total: float) -> bool:
    """N13-obs8: the waits are slept in ≤ 1 s slices (the ORDER of the configured waits is
    pinned by ``retried[].backoff_s``); the slices add up to the schedule."""
    return abs(sum(sleeps) - total) < 1e-9 and all(0 < s <= SLICE for s in sleeps)
BACKOFF = (5.0, 15.0)

# The 13:29Z replay error, verbatim shape: `runtime_invoke._collect` raising on the SSE
# `{"error": …}` payload the runtime answered with.
REPLAY_5XX = RuntimeError(
    "runtime returned error: An error occurred (internalServerException) when calling the "
    "ConverseStream operation: Internal Server Error. Try your request again."
)


def _health_check_4xx(operation: str = "InvokeAgentRuntime") -> ClientError:
    """Upstream #227 (live 2026-10-08): the invoke CALL itself failing a runtime health
    check — the capitalised ``RuntimeClientError`` code with a 4xx status, not the
    mid-stream ``runtimeClientError`` event and not a 5xx."""
    return ClientError(
        {"Error": {"Code": "RuntimeClientError",
                   "Message": "Runtime health check failed or timed out."},
         "ResponseMetadata": {"HTTPStatusCode": 424}},
        operation,
    )


def _judge_5xx() -> EventStreamError:
    """The 09:44Z judge error: botocore surfaces a mid-stream Bedrock 5xx as EventStreamError."""
    return EventStreamError(
        {"Error": {"Code": "internalServerException",
                   "Message": "Internal Server Error. Try your request again."}},
        "ConverseStream",
    )


# ─── the shared predicate ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("exc_or_text", [
    REPLAY_5XX,
    _judge_5xx(),
    ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-west-2.test"),
    ClientError({"Error": {"Code": "ThrottlingException"}, "ResponseMetadata": {}}, "Converse"),
    ClientError({"Error": {"Code": "Weird"}, "ResponseMetadata": {"HTTPStatusCode": 502}}, "X"),
    RuntimeError("runtime client error: {'message': 'Read timed out'}"),
    # the SDK's terminal error frame as the architect sees it (error_type + text)
    rt.SdkStreamError("runtime returned error: EventStreamError: An error occurred "
                      "(internalServerException) when calling the ConverseStream operation",
                      error_type="EventStreamError"),
    architect_runtime.RuntimeStreamError("runtime returned error: upstream hiccup",
                                         error_type="ModelThrottledException"),
    "ServiceUnavailableException: service unavailable",
    "HTTP 503 from the model endpoint",
    "upstream status 500",
    "model_error: the model endpoint failed",
    # upstream #227: the call-level health-check failure (4xx, capitalised code) and its
    # bare message as an SDK frame would carry it
    _health_check_4xx(),
    "Runtime health check failed or timed out.",
])
def test_transient_upstream_errors_are_recognised_from_exceptions_and_text(exc_or_text):
    assert runtime_invoke.is_upstream_transient(exc_or_text) is True
    assert rt.is_upstream_transient(exc_or_text) is True  # the hub's re-export agrees


@pytest.mark.parametrize("exc_or_text", [
    ValueError("the agent is gone"),
    ClientError({"Error": {"Code": "ValidationException", "Message": "bad prompt"},
                 "ResponseMetadata": {"HTTPStatusCode": 400}}, "Invoke"),
    RuntimeError("runtime returned error: bad prompt"),
    model_calls.JudgeBudgetExceeded("judge Builtin.Helpfulness", 4096, 3),
    model_calls.OutputTruncated(1024),
    TelemetryReadinessTimeout("no content log for the session within 120 s"),
    rt.SdkStreamError("runtime returned error: ContextWindowOverflowException: too long",
                      error_type="ContextWindowOverflowException"),
    "scored 15000 sessions",  # the bare " 500 " never matches inside a longer number
    "",
    None,
])
def test_caller_errors_output_limits_and_telemetry_timeouts_are_not_transient(exc_or_text):
    assert runtime_invoke.is_upstream_transient(exc_or_text) is False
    assert rt.is_upstream_transient(exc_or_text) is False


def test_the_marker_list_is_shared_and_anchored():
    assert rt.UPSTREAM_TRANSIENT_MARKERS is runtime_invoke.UPSTREAM_TRANSIENT_MARKERS
    assert "internalserverexception" in rt.UPSTREAM_TRANSIENT_MARKERS
    bare = [m for m in rt.UPSTREAM_TRANSIENT_MARKERS if m.strip() in ("500", "503")]
    assert bare == [" 500 ", " 503 "]  # the bare codes exist only space-anchored
    # N-SCN-bug1's replay-loop name is the same predicate
    assert runtime_invoke.is_transient_invoke_error(REPLAY_5XX) is True


def test_the_architect_turn_replays_a_call_level_runtime_health_check_failure(monkeypatch):
    """Upstream #227 on the architect side: ``_replayable_turn_error``'s ClientError branch
    is the shared predicate, so the ``RuntimeClientError`` health-check failure raised by
    the InvokeAgentRuntime call replays (after the backoff) like a throttle / 5xx; a 4xx
    caller error on the same call still does not."""
    monkeypatch.setattr(get_settings(), "assistant_replay_backoff_seconds", 15.0)
    health = _health_check_4xx()
    assert assistant_service._replayable_turn_error(health) is True
    assert assistant_service._replay_backoff_s(health) == 15.0
    for code in ("ThrottlingException", "ServiceUnavailableException", "InternalServerException"):
        assert assistant_service._replayable_turn_error(
            ClientError({"Error": {"Code": code}, "ResponseMetadata": {}}, "InvokeAgentRuntime")
        ) is True
    caller = ClientError({"Error": {"Code": "ValidationException", "Message": "bad payload"},
                          "ResponseMetadata": {"HTTPStatusCode": 400}}, "InvokeAgentRuntime")
    assert assistant_service._replayable_turn_error(caller) is False


def test_the_assistant_replay_backoff_uses_the_shared_predicate(monkeypatch):
    monkeypatch.setattr(get_settings(), "assistant_replay_backoff_seconds", 15.0)
    transient = architect_runtime.RuntimeStreamError(
        "runtime returned error: EventStreamError: An error occurred (internalServerException)",
        error_type="EventStreamError")
    other = architect_runtime.RuntimeStreamError(
        "runtime returned error: ValidationException: too many tokens",
        error_type="ValidationException")
    assert assistant_service._replay_backoff_s(transient) == 15.0
    assert assistant_service._replay_backoff_s(other) == 0.0
    assert not hasattr(assistant_service, "_UPSTREAM_TRANSIENT_MARKERS")  # moved, not copied


# ─── the setting ─────────────────────────────────────────────────────────────────────
def test_backoff_setting_parses_to_the_wait_tuple(monkeypatch):
    assert parse_backoff_seconds("5,15") == (5.0, 15.0)
    assert parse_backoff_seconds(" 5 , 15 ") == (5.0, 15.0)
    assert parse_backoff_seconds("0") == (0.0,)
    assert parse_backoff_seconds("2.5") == (2.5,)
    for bad in ("", "5,", "five", "-1", "nan"):
        with pytest.raises(ValueError):
            parse_backoff_seconds(bad)
    monkeypatch.delenv("LAUNCHPAD_EVAL_TRANSIENT_RETRY_BACKOFF_SECONDS", raising=False)
    assert Settings().eval_transient_retry_backoff == (5.0, 15.0)  # the shipped default
    monkeypatch.setenv("LAUNCHPAD_EVAL_TRANSIENT_RETRY_BACKOFF_SECONDS", "1,2,3")
    assert Settings().eval_transient_retry_backoff == (1.0, 2.0, 3.0)
    monkeypatch.setenv("LAUNCHPAD_EVAL_TRANSIENT_RETRY_BACKOFF_SECONDS", "soon")
    with pytest.raises(ValidationError):
        Settings()
    assert get_settings().eval_transient_retry_backoff == (0.0,)  # conftest: tests retry at once


def test_backoff_for_repeats_the_last_wait_and_is_zero_when_empty():
    assert runtime_invoke.backoff_for(BACKOFF, 0) == 5.0
    assert runtime_invoke.backoff_for(BACKOFF, 1) == 15.0
    assert runtime_invoke.backoff_for(BACKOFF, 7) == 15.0
    assert runtime_invoke.backoff_for((), 0) == 0.0
    assert runtime_invoke.backoff_for(None, 0) == 0.0
    assert runtime_invoke.DEFAULT_TRANSIENT_BACKOFF_S == BACKOFF
    assert jobs_batch.WorkerEnv.__dataclass_fields__["transient_backoff_s"].default == BACKOFF


# ─── the replay path ─────────────────────────────────────────────────────────────────
@pytest.fixture
def replay(request):
    """``test_jobs_replay_failures.harness`` (spans ready, engine / detectors stubbed, a
    scripted ``env.invoke``), looked up by name so the import stays a registration only."""
    return request.getfixturevalue("harness")


def _env_with_backoff(h, sleeps: list[float]) -> jobs_batch.WorkerEnv:
    return dataclasses.replace(h.env, sleep=sleeps.append, transient_backoff_s=BACKOFF)


def test_a_replay_5xx_is_retried_after_the_backoff_and_the_scenario_recovers(replay):
    """13:29Z: `runtime returned error: … internalServerException` got ONE attempt."""
    h = replay({S2: [REPLAY_5XX, REPLAY_5XX, "ok"]})
    sleeps: list[float] = []
    sink = Sink()

    jobs_batch.run_job(_job(_spec()), sink, _env_with_backoff(h, sleeps))

    assert [c[0] for c in h.runtime.calls] == [S1, S2, S2, S2, S3]
    assert _sliced(sleeps, 20.0)  # the configured waits (5 s, 15 s) before attempts 2 and 3
    line = sink.invocations[1]
    assert line["status"] == "ok" and line["retries"] == 2 and "error_kind" not in line
    assert [(r["attempt"], r["backoff_s"]) for r in line["retried"]] == [(1, 5.0), (2, 15.0)]
    assert all(r["reason"].startswith("RuntimeError: runtime returned error: An error occurred "
                                      "(internalServerException)") for r in line["retried"])
    assert h.evaluated == [[S1, S2, S3]]
    assert sink.finished[0] == "COMPLETED" and "replay_failures" not in sink.finished[2]
    retry_msgs = [m for _p, _d, m in sink.statuses if "attempt" in m]
    assert retry_msgs == [f"replaying 2/3: scenario s2 attempt 2/{N} after RuntimeError",
                          f"replaying 2/3: scenario s2 attempt 3/{N} after RuntimeError"]


def test_a_failed_runtime_health_check_on_the_call_replays_the_scenario(replay):
    """Upstream #227's second test on the CN seam: the invoke CALL raises the 4xx
    ``RuntimeClientError`` health-check failure (no stream at all) — the scenario is
    replayed after the backoff in its pre-assigned session and the run stays clean."""
    h = replay({S2: [_health_check_4xx(), "ok"]})
    sleeps: list[float] = []
    sink = Sink()

    jobs_batch.run_job(_job(_spec()), sink, _env_with_backoff(h, sleeps))

    assert [c[0] for c in h.runtime.calls] == [S1, S2, S2, S3]
    assert _sliced(sleeps, 5.0)  # one wait, before attempt 2
    line = sink.invocations[1]
    assert line["status"] == "ok" and line["retries"] == 1 and "error_kind" not in line
    assert [(r["attempt"], r["backoff_s"]) for r in line["retried"]] == [(1, 5.0)]
    assert line["retried"][0]["reason"].startswith(
        "ClientError: An error occurred (RuntimeClientError)")
    assert "Runtime health check failed" in line["retried"][0]["reason"]
    assert h.evaluated == [[S1, S2, S3]]
    assert sink.finished[0] == "COMPLETED" and "replay_failures" not in sink.finished[2]
    assert sink.finished[1] is None


def test_a_replay_that_stays_5xx_records_every_retry_and_the_run_counts_it_once(replay):
    h = replay({S2: [REPLAY_5XX]})
    sleeps: list[float] = []
    sink = Sink()

    jobs_batch.run_job(_job(_spec()), sink, _env_with_backoff(h, sleeps))

    assert [c[0] for c in h.runtime.calls] == [S1] + [S2] * N + [S3]
    assert _sliced(sleeps, 20.0)  # no wait after the final attempt
    errored = sink.invocations[1]
    assert errored["status"] == "failed" and errored["code"] == "RuntimeError"
    assert errored["attempts"] == N and errored["retries"] == N - 1
    assert errored["error_kind"] == "infrastructure"
    assert [(r["attempt"], r["backoff_s"]) for r in errored["retried"]] == [(1, 5.0), (2, 15.0)]
    # the error records carry the attempt count AND each retry's cause; the hub still
    # reads the line as an unanswered scenario (`failed after k/N attempts:`)
    bad = [r for r in sink.result_rows if r["session_id"] == S2]
    assert len(bad) == 2 and all(r["error_kind"] == "infrastructure" for r in bad)
    message = bad[0]["error_message"]
    assert f"scenario s2 failed after {N}/{N} attempts: RuntimeError: runtime returned error: " \
           "An error occurred (internalServerException)" in message
    assert "(retried after: #1 RuntimeError: runtime returned error" in message
    assert ", waited 5 s; #2 RuntimeError" in message and ", waited 15 s)" in message
    from app.evaluation.service import summarize_session_failures
    summary = summarize_session_failures(sink.result_rows, [S1, S2, S3])
    assert summary["failed"] == 1 and summary["kind"] == "replay_failed"
    [session] = summary["sessions"]
    assert session["session_id"] == S2 and session["error_type"] == "RuntimeError"
    assert "retried after" in session["message"]
    # the run summary lists the scenario ONCE (no double count for its retries)
    state, error, done_summary = sink.finished
    assert state == "COMPLETED" and error is None
    assert [f["session_id"] for f in done_summary["replay_failures"]] == [S2]
    assert done_summary["replay_failures"][0]["attempts"] == N
    assert done_summary["scores"][0]["count"] == 2


def test_a_non_transient_replay_error_is_not_retried_and_never_waits(replay):
    h = replay({S2: [ValueError("the agent is gone")]})
    sleeps: list[float] = []
    sink = Sink()

    jobs_batch.run_job(_job(_spec()), sink, _env_with_backoff(h, sleeps))

    assert [c[0] for c in h.runtime.calls] == [S1, S2, S3] and sleeps == []
    errored = sink.invocations[1]
    assert errored["attempts"] == 1 and errored["retries"] == 0 and "retried" not in errored
    bad = [r for r in sink.result_rows if r["session_id"] == S2]
    assert all(r["error_message"].endswith("attempts: ValueError: the agent is gone")
               for r in bad)  # no retry suffix on a one-attempt failure


def test_the_replay_backoff_honours_the_hub_wait_list_and_a_zero_list(replay):
    h = replay({S2: [REPLAY_5XX, "ok"]})
    sleeps: list[float] = []
    env = dataclasses.replace(h.env, sleep=sleeps.append, transient_backoff_s=(0.0,))
    jobs_batch.run_job(_job(_spec()), Sink(), env)
    assert sleeps == []  # "0": retry at once, no sleep call at all

    h = replay({S2: [REPLAY_5XX, REPLAY_5XX, "ok"]})
    sleeps = []
    env = dataclasses.replace(h.env, sleep=sleeps.append, transient_backoff_s=(2.0,))
    jobs_batch.run_job(_job(_spec()), Sink(), env)
    assert _sliced(sleeps, 4.0)  # the last entry repeats (2 s + 2 s)


def test_a_cancel_during_the_backoff_stops_the_job_before_the_retry(replay):
    h = replay({S1: [REPLAY_5XX]})
    calls = h.runtime.calls
    sleeps: list[float] = []

    class CancelOnceWaited(Sink):
        def cancelled(self) -> bool:
            return bool(sleeps)

    sink = CancelOnceWaited()
    jobs_batch.run_job(_job(_spec()), sink, _env_with_backoff(h, sleeps))

    # N13-obs8: one ≤ 1 s slice, then the marker was seen — not the whole 5 s wait
    assert [c[0] for c in calls] == [S1] and sleeps == [SLICE]
    assert sink.invocations == [] and sink.finished[0] == "STOPPED"


def test_a_cancel_during_a_15_s_replay_backoff_is_honoured_within_a_second(replay):
    """N13-obs8 (r12-f nit): the backoff used to be ONE `sleep(15)`, so a cancel written at
    second 1 was only seen at second 15. The wait is now sliced and polls the marker."""
    h = replay({S1: [REPLAY_5XX]})
    clock = {"now": 0.0}
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    class CancelAtHalfASecond(Sink):
        def cancelled(self) -> bool:
            return clock["now"] >= 0.5  # the operator stopped the run during the wait

    sink = CancelAtHalfASecond()
    env = dataclasses.replace(h.env, sleep=sleep, transient_backoff_s=(15.0,))
    jobs_batch.run_job(_job(_spec()), sink, env)

    assert [c[0] for c in h.runtime.calls] == [S1]  # no retry after the stop
    assert clock["now"] <= 1.0 + 1e-9 and all(s <= SLICE for s in sleeps)  # ~1 s, not 15 s
    assert sink.invocations == [] and sink.finished[0] == "STOPPED"
    assert sink.finished[1] == "stopped by operator"


def test_sliced_wait_covers_the_schedule_and_stops_between_slices():
    sleeps: list[float] = []
    assert runtime_invoke.sliced_wait(2.5, sleeps.append) is True
    assert sleeps == [1.0, 1.0, 0.5]
    assert runtime_invoke.sliced_wait(0.0, sleeps.append) is True and len(sleeps) == 3
    # stop seen before the third slice: nothing more is slept
    sleeps = []
    assert runtime_invoke.sliced_wait(15.0, sleeps.append, stop=lambda: len(sleeps) >= 2) is False
    assert sleeps == [1.0, 1.0]
    # stop raised right after the last slice is still a stop
    assert runtime_invoke.sliced_wait(1.0, sleeps.append, stop=lambda: len(sleeps) >= 3) is False
    assert sleeps == [1.0, 1.0, 1.0]


# ─── the judge path ──────────────────────────────────────────────────────────────────
class FlakyEvaluator:
    """``evaluate(case)`` raises the queued exceptions in order, then answers."""

    def __init__(self, *failures: BaseException, answer=("ok",)) -> None:
        self.failures = list(failures)
        self.answer = list(answer)
        self.calls = 0

    def evaluate(self, case):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return list(self.answer)


class Throttled(Exception):
    pass


def _case() -> types.SimpleNamespace:
    """A minimal ``EvaluationData`` stand-in for ``evaluate_session`` (no spans needed)."""
    return types.SimpleNamespace(actual_trajectory=types.SimpleNamespace(traces=[]))


def test_a_judge_5xx_is_retried_after_the_backoff_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    evaluator = FlakyEvaluator(_judge_5xx(), _judge_5xx())

    out = engine_core.evaluate_with_retry(evaluator, case=None, sleep=sleeps.append,
                                          transient_delays=BACKOFF)

    assert out == ["ok"] and evaluator.calls == 3 and _sliced(sleeps, 20.0)


def test_a_judge_that_stays_5xx_is_an_error_record_typed_as_the_cause_with_attempts():
    sleeps: list[float] = []
    evaluator = FlakyEvaluator(_judge_5xx(), _judge_5xx(), _judge_5xx(), _judge_5xx())

    with pytest.raises(engine_core.UpstreamTransientExhausted) as info:
        engine_core.evaluate_with_retry(evaluator, case=None, sleep=sleeps.append,
                                        transient_delays=BACKOFF, label="judge Builtin.X")

    exc = info.value
    assert evaluator.calls == engine_core.JUDGE_TRANSIENT_ATTEMPTS == 3
    assert _sliced(sleeps, 20.0)  # no wait after the final attempt
    assert exc.error_type == "EventStreamError" and exc.attempts == 3
    assert [(r["attempt"], r["backoff_s"]) for r in exc.retried] == [(1, 5.0), (2, 15.0)]
    assert isinstance(exc.__cause__, EventStreamError)
    message = str(exc)
    assert message.startswith("judge Builtin.X errored on every one of 3 attempts "
                              "(transient upstream; waited 5 s, 15 s): EventStreamError: "
                              "An error occurred (internalServerException)")
    # the error record shows the CAUSE's class; the hub classifies it as a judge error,
    # not as an unanswered scenario (no "failed after k/N attempts:" wording)
    record = error_record(job_id="j", session_id="s", evaluator_id="Builtin.X", level="TRACE",
                          error=exc)
    assert record["error_type"] == "EventStreamError" and record["score"] is None
    assert "errored on every one of 3 attempts" in record["error_message"]
    from app.evaluation.service import _failure_kind
    assert _failure_kind(record["error_type"], record["error_message"]) == "evaluator_error"


def test_a_non_transient_judge_error_propagates_at_once_and_throttling_keeps_its_delays(
        monkeypatch):
    sleeps: list[float] = []
    evaluator = FlakyEvaluator(ValueError("bad rubric"))
    with pytest.raises(ValueError):
        engine_core.evaluate_with_retry(evaluator, case=None, sleep=sleeps.append,
                                        transient_delays=BACKOFF)
    assert evaluator.calls == 1 and sleeps == []

    # a max_tokens stop is the N-SCN-bug2 budget retry's business, never retried here
    evaluator = FlakyEvaluator(model_calls.OutputTruncated(1024))
    with pytest.raises(model_calls.OutputTruncated):
        engine_core.evaluate_with_retry(evaluator, case=None, sleep=sleeps.append,
                                        transient_delays=BACKOFF)
    assert evaluator.calls == 1 and sleeps == []

    # throttling: the existing 2 / 4 / 8 s schedule, one last try, then the error propagates
    monkeypatch.setattr(engine_core, "is_throttling_error",
                        lambda exc: isinstance(exc, Throttled))
    evaluator = FlakyEvaluator(Throttled(), Throttled(), Throttled())
    assert engine_core.evaluate_with_retry(evaluator, case=None, sleep=sleeps.append,
                                           transient_delays=BACKOFF) == ["ok"]
    assert sleeps == [2.0, 4.0, 8.0] and evaluator.calls == 4
    sleeps.clear()
    evaluator = FlakyEvaluator(Throttled(), Throttled(), Throttled(), Throttled())
    with pytest.raises(Throttled):
        engine_core.evaluate_with_retry(evaluator, case=None, sleep=sleeps.append,
                                        transient_delays=BACKOFF)
    assert sleeps == [2.0, 4.0, 8.0] and evaluator.calls == 4


def test_the_evaluator_set_carries_the_backoff_into_the_budgeted_judge_call(monkeypatch):
    """`evaluate_with_budget` (the batch / SCORE NOW / optimize path) passes the set's
    `transient_backoff` — the hub setting — into the retry, inside the N-SCN-bug2 budget."""
    evaluator = FlakyEvaluator(_judge_5xx(), answer=("judged",))
    evaluators = engine_core.EvaluatorSet(
        [{"id": "Builtin.Helpfulness"}], lambda model_id, **kw: f"judge:{model_id}",
        default_model_id="glm", transient_backoff=(7.0, 9.0),
    )
    monkeypatch.setattr(engine_core.evaluator_lib, "build",
                        lambda definition, judge, **kw: evaluator)
    sleeps: list[float] = []

    out = engine_core.evaluate_with_budget(evaluators, {"id": "Builtin.Helpfulness"},
                                           case=None, sleep=sleeps.append)

    assert out == ["judged"] and evaluator.calls == 2 and _sliced(sleeps, 7.0)


def test_a_cancel_during_the_judge_backoff_abandons_the_retry_within_a_second():
    """N13-obs8: the judge's transient backoff polls the job's stop predicate between ≤ 1 s
    slices; a stop mid-wait abandons the retry at once — an error record typed as the
    cause, classified by the hub as a judge error (not an unanswered scenario)."""
    clock = {"now": 0.0}
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    evaluator = FlakyEvaluator(_judge_5xx(), _judge_5xx())
    with pytest.raises(engine_core.UpstreamRetryAbandoned) as info:
        engine_core.evaluate_with_retry(evaluator, case=None, sleep=sleep,
                                        transient_delays=(15.0,), label="judge Builtin.X",
                                        stop=lambda: clock["now"] >= 0.5)
    exc = info.value
    assert evaluator.calls == 1 and clock["now"] <= 1.0 + 1e-9  # ~1 s, not 15 s; no retry
    assert all(s <= SLICE for s in sleeps)
    assert exc.error_type == "EventStreamError" and exc.attempts == 1
    assert [(r["attempt"], r["backoff_s"]) for r in exc.retried] == [(1, 15.0)]
    assert isinstance(exc.__cause__, EventStreamError)
    assert str(exc).startswith("judge Builtin.X retry abandoned — the job was stopped during the "
                               "15 s wait before attempt 2/3 (transient upstream): "
                               "EventStreamError: An error occurred (internalServerException)")
    record = error_record(job_id="j", session_id="s", evaluator_id="Builtin.X", level="TRACE",
                          error=exc)
    assert record["error_type"] == "EventStreamError" and record["score"] is None
    from app.evaluation.service import _failure_kind
    assert _failure_kind(record["error_type"], record["error_message"]) == "evaluator_error"
    # without a stop predicate the wait simply completes and the judge is retried
    evaluator = FlakyEvaluator(_judge_5xx())
    sleeps = []
    assert engine_core.evaluate_with_retry(evaluator, case=None, sleep=sleeps.append,
                                           transient_delays=(15.0,)) == ["ok"]
    assert _sliced(sleeps, 15.0) and evaluator.calls == 2


def test_the_session_evaluation_threads_the_stop_predicate_into_the_judge_backoff(monkeypatch):
    """``evaluate_session(stop=)`` is the hub's / worker's ``sink.cancelled``: it reaches the
    budgeted judge call and, from there, the retry's sliced wait (N13-obs8)."""
    evaluator = FlakyEvaluator(_judge_5xx(), answer=("judged",))
    evaluators = engine_core.EvaluatorSet(
        [{"id": "Builtin.Helpfulness"}], lambda model_id, **kw: f"judge:{model_id}",
        default_model_id="glm", transient_backoff=(15.0,),
    )
    monkeypatch.setattr(engine_core.evaluator_lib, "build",
                        lambda definition, judge, **kw: evaluator)
    monkeypatch.setattr(engine_core, "evaluation_data", lambda sid, out, truth: _case())
    provider = types.SimpleNamespace(get_evaluation_data=lambda sid: {"output": "x"})
    clock = {"now": 0.0}

    def sleep(seconds: float) -> None:
        clock["now"] += seconds

    records = engine_core.evaluate_session(provider, "s1", evaluators, None, job_id="j",
                                           sleep=sleep, stop=lambda: clock["now"] >= 0.5)

    [record] = records
    assert record["error_type"] == "EventStreamError"
    assert "retry abandoned" in record["error_message"]
    assert evaluator.calls == 1 and clock["now"] <= 1.0 + 1e-9  # stopped within ~1 s, no retry


def test_the_hub_wires_its_setting_into_the_in_process_env_and_score_now(monkeypatch):
    from app.evaluation import runtime_executor, strands_engine

    monkeypatch.setattr(get_settings(), "eval_transient_retry_backoff_seconds", "3,4")
    ws = types.SimpleNamespace(client=lambda service, **kw: object(), resources={}, id="w")
    monkeypatch.setattr(runtime_executor, "data_client", lambda ws: object())
    env = runtime_executor.worker_env_for(ws)
    assert env.transient_backoff_s == (3.0, 4.0)

    engine = strands_engine.StrandsEngine.__new__(strands_engine.StrandsEngine)
    engine._judge_factory = lambda model_id, **kw: f"judge:{model_id}"
    engine._lambda_client = lambda ws: None
    monkeypatch.setattr(strands_engine.evaluators_local, "resolve_definitions",
                        lambda db, ws, ids: [{"id": i} for i in ids])
    evaluators = engine._evaluator_set(ws, None, ["Builtin.Helpfulness"], None)
    assert evaluators.transient_backoff == (3.0, 4.0)
