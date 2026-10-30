"""N13-obs14 — the ONLINE evaluation judge path retries transient upstream errors like batch.

Acceptance NC-E12-12 (E12-online-eval, cn/q-acc-2) found that ``jobs_online`` retried only
throttling (2 / 4 / 8 s) and turned a judge 5xx into an immediate error record while the
batch / SCORE NOW path (cn/r12-f, N12-obs53) retried it 3× with the hub's
``eval_transient_retry_backoff_seconds``. Now the online handler delegates to the SAME
``engine_core.evaluate_with_retry``: ``is_upstream_transient`` predicate, configured backoff
slept in ≤ 1 s cancel-aware slices (N13-obs8), exhausted → one error record typed as the
cause naming the attempts, stop mid-wait → one typed error record and a STOPPED job. The
throttling schedule, the sampling / window semantics (the scheduler's) and the per-record
accounting (one record per evaluator per session) are unchanged.
"""

from __future__ import annotations

from botocore.exceptions import EventStreamError

from app.evaluation import jobs
from app.evaluation.executor import JobSink, MemoryStore
from app.evaluation.service import _failure_kind
from app.services.strands_eval import engine_core, jobs_online, runtime_invoke
from tests.evaluation.fakes import FakeModel
from tests.evaluation.test_online_scheduler import (
    FIX_SID,
    JOB_ID,
    _done,
    _fixture_logs,
    _job,
    _results,
)

SLICE = runtime_invoke.BACKOFF_SLICE_S


def _sliced(sleeps: list[float], total: float) -> bool:
    """The configured waits add up, slept in ≤ 1 s slices (N13-obs8)."""
    return abs(sum(sleeps) - total) < 1e-9 and all(0 < s <= SLICE for s in sleeps)


def _judge_5xx() -> EventStreamError:
    """The live 2026-10-16 09:44Z judge error: a mid-stream Bedrock 5xx as EventStreamError."""
    return EventStreamError(
        {"Error": {"Code": "internalServerException",
                   "Message": "Internal Server Error. Try your request again."}},
        "ConverseStream",
    )


def _judge_factory(model_id, **kw):
    return FakeModel(structured=[{"reasoning": "ok", "score": "Very helpful"}] * 4)


def _flaky_build(failures: dict[str, list[BaseException]], calls: dict[str, int]):
    """A ``build`` seam: the named evaluators raise their queued exceptions first (like the
    throttling test in ``test_online_scheduler``), every call is counted per evaluator."""
    real_build = jobs_online.evaluators_lib.build

    def build(definition, judge, **kw):
        evaluator = real_build(definition, judge, **kw)
        evaluator_id = definition["id"]
        queue = failures.get(evaluator_id)
        if queue is None:
            return evaluator
        inner = evaluator.evaluate

        def flaky(case):
            calls[evaluator_id] = calls.get(evaluator_id, 0) + 1
            if queue:
                raise queue.pop(0)
            return inner(case)

        evaluator.evaluate = flaky  # type: ignore[method-assign]
        return evaluator

    return build


def test_an_online_judge_5xx_is_retried_with_the_configured_backoff_and_the_session_scores():
    """×2 transient → the third attempt scores; the waits are the DISPATCHER's list
    (``WorkerEnv.transient_backoff_s`` = the hub setting), slept in ≤ 1 s slices."""
    store = MemoryStore()
    job = _job([FIX_SID], [{"id": "Builtin.Helpfulness"}])
    calls: dict[str, int] = {}
    sleeps: list[float] = []

    jobs_online.run(job, JobSink(store, job), logs_client=_fixture_logs(),
                    judge_factory=_judge_factory, sleep=sleeps.append,
                    build=_flaky_build({"Builtin.Helpfulness": [_judge_5xx(), _judge_5xx()]},
                                       calls),
                    transient_backoff=(3.0, 4.0))

    assert calls == {"Builtin.Helpfulness": 3} and _sliced(sleeps, 7.0)
    [record] = _results(store)
    assert record["score"] == 0.833 and record["error_type"] is None
    assert record["id"] == jobs.record_id(JOB_ID, FIX_SID, "Builtin.Helpfulness", 0)
    done = _done(store)
    assert done.state == "COMPLETED" and done.result_count == 1
    assert done.summary["scores"] == [
        {"evaluatorId": "Builtin.Helpfulness", "score": 0.833, "count": 1, "n": 1, "unit": "turn"}]


def test_an_online_judge_that_stays_5xx_is_one_error_record_typed_as_the_cause_with_attempts():
    """×3 transient → exhausted: ONE record (single-counted), ``error_type`` is the cause's
    class, the message names the attempts and the default 5 s / 15 s waits; the hub reads it
    as a judge error, not an unanswered scenario; the job still COMPLETES (parity with batch)."""
    store = MemoryStore()
    control = "Builtin.TrajectoryInOrderMatch"  # no judge; NOT_APPLICABLE without ground truth
    job = _job([FIX_SID], [{"id": "Builtin.Helpfulness"}, {"id": control}])
    calls: dict[str, int] = {}
    sleeps: list[float] = []

    jobs_online.run(job, JobSink(store, job), logs_client=_fixture_logs(),
                    judge_factory=_judge_factory, sleep=sleeps.append,
                    build=_flaky_build({"Builtin.Helpfulness": [_judge_5xx()] * 10,
                                        control: []}, calls))

    assert calls["Builtin.Helpfulness"] == engine_core.JUDGE_TRANSIENT_ATTEMPTS == 3
    assert calls[control] == 1  # the next evaluator still runs
    # the default DEFAULT_TRANSIENT_BACKOFF_S (5 s, 15 s); no wait after the last attempt
    assert _sliced(sleeps, 20.0)
    by_key = {r["evaluator_id"]: r for r in _results(store)}
    assert len(by_key) == 2
    failed = by_key["Builtin.Helpfulness"]
    assert failed["score"] is None and failed["error_type"] == "EventStreamError"
    assert failed["error_message"].startswith(
        "judge Builtin.Helpfulness errored on every one of 3 attempts (transient upstream; "
        "waited 5 s, 15 s): EventStreamError: An error occurred (internalServerException)")
    assert "failed after" not in failed["error_message"]
    assert _failure_kind(failed["error_type"], failed["error_message"]) == "evaluator_error"
    assert by_key[control]["error_type"] is None and by_key[control]["label"] == "NOT_APPLICABLE"
    done = _done(store)
    assert done.state == "COMPLETED" and done.result_count == 2
    assert done.summary["scores"] == []


def test_a_non_transient_online_judge_error_is_one_attempt_and_throttling_keeps_its_schedule():
    store = MemoryStore()
    job = _job([FIX_SID], [{"id": "Builtin.Helpfulness"}, {"id": "Builtin.Correctness"}])
    calls: dict[str, int] = {}
    sleeps: list[float] = []

    class Throttle(Exception):
        pass

    Throttle.__name__ = "ThrottlingException"

    failures = {"Builtin.Helpfulness": [Throttle("slow"), Throttle("slow")],
                "Builtin.Correctness": [ValueError("judge returned garbage")]}
    jobs_online.run(job, JobSink(store, job), logs_client=_fixture_logs(),
                    judge_factory=_judge_factory, sleep=sleeps.append,
                    build=_flaky_build(failures, calls))

    assert calls["Builtin.Helpfulness"] == 3
    assert calls["Builtin.Correctness"] == 1  # never retried, never waited
    assert sleeps == [2.0, 4.0]  # the online throttling schedule — not sliced, not the backoff
    by_key = {r["evaluator_id"]: r for r in _results(store)}
    assert by_key["Builtin.Helpfulness"]["score"] == 0.833
    assert by_key["Builtin.Correctness"]["error_type"] == "ValueError"
    assert "garbage" in by_key["Builtin.Correctness"]["error_message"]
    assert _done(store).state == "COMPLETED"


def test_a_cancel_during_the_online_judge_backoff_stops_the_job_within_a_second():
    """N13-obs8 applied to the online job: the backend writes the cancel marker half a second
    into a 15 s wait → the retry is abandoned (one error record typed as the cause), the
    remaining evaluators of the session are not attempted, ``done.json`` is STOPPED."""
    store = MemoryStore()
    job = _job([FIX_SID], [{"id": "Builtin.Helpfulness"}, {"id": "Builtin.Correctness"}])
    calls: dict[str, int] = {}
    clock = {"now": 0.0}
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds
        if clock["now"] >= 0.5:
            store.put(jobs.cancel_key(JOB_ID), b"cancel")

    jobs_online.run(job, JobSink(store, job), logs_client=_fixture_logs(),
                    judge_factory=_judge_factory, sleep=sleep,
                    build=_flaky_build({"Builtin.Helpfulness": [_judge_5xx(), _judge_5xx()],
                                        "Builtin.Correctness": []}, calls),
                    transient_backoff=(15.0,))

    assert calls == {"Builtin.Helpfulness": 1}  # no retry, Correctness never started
    assert clock["now"] <= 1.0 + 1e-9 and all(s <= SLICE for s in sleeps), sleeps
    [record] = _results(store)
    assert record["evaluator_id"] == "Builtin.Helpfulness"
    assert record["score"] is None and record["error_type"] == "EventStreamError"
    assert record["error_message"].startswith(
        "judge Builtin.Helpfulness retry abandoned — the job was stopped during the 15 s wait "
        "before attempt 2/3 (transient upstream): EventStreamError")
    assert _failure_kind(record["error_type"], record["error_message"]) == "evaluator_error"
    done = _done(store)
    assert done.state == "STOPPED" and done.result_count == 1 and done.summary["scores"] == []


def test_the_online_retry_is_the_engine_retry_with_the_online_throttle_schedule():
    """The online module keeps no retry loop of its own: its ``evaluate_with_retry`` is the
    engine's, pinned to ``RETRY_DELAYS_S`` (2 / 4 / 8 s) and the caller's backoff / stop."""
    seen: dict[str, object] = {}

    def spy(evaluator, case, **kw):
        seen.update(kw)
        return ["judged"]

    original = engine_core.evaluate_with_retry
    engine_core.evaluate_with_retry = spy  # type: ignore[assignment]
    try:
        out = jobs_online.evaluate_with_retry("E", "case", sleep="SLEEP", transient_backoff=(1, 2),
                                              stop="STOP", label="judge X")
    finally:
        engine_core.evaluate_with_retry = original  # type: ignore[assignment]
    assert out == ["judged"]
    assert seen == {"sleep": "SLEEP", "delays": jobs_online.RETRY_DELAYS_S,
                    "transient_delays": (1.0, 2.0), "label": "judge X", "stop": "STOP"}
    assert jobs_online.RETRY_DELAYS_S == (2.0, 4.0, 8.0) == engine_core.THROTTLE_DELAYS_S
