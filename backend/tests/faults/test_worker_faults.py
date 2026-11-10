"""The eval worker's replay and judge under injected upstream faults (quality pack Q4).

The worker library (``services/strands_eval``) is pure, so the faults land on the two
injected callables: ``WorkerEnv.invoke`` (the scenario replay) and the judge model.
Replay: a transient is retried on the SAME pre-assigned session id up to
``TRANSIENT_SCENARIO_RETRIES`` times and the invocation line records the attempts
(N-SCN-bug1); the judge: a throttle is retried with a bounded back-off, a 5xx is an error
record — the recovery after a judge 5xx is the spec and ``xfail(strict)`` on a behaviour
probe until the shared classifier (r12-f) retries it.
"""

from __future__ import annotations

import pytest

from app.services.strands_eval import engine_core, jobs_batch
from app.services.strands_eval.jobs_batch import TRANSIENT_SCENARIO_ATTEMPTS, WorkerEnv
from tests.evaluation.test_jobs_replay_failures import S1, S2, Sink, _spec
from tests.faults.conftest import BEDROCK_5XX_CLIENT, judge_retries

HELPFULNESS = {"id": "Builtin.Helpfulness"}


def _env(invoke) -> WorkerEnv:
    return WorkerEnv(logs_client=None, data_client=None, judge_factory=lambda *a, **k: None,
                     default_model_id="judge", sleep=lambda s: None, invoke=invoke)


@pytest.mark.parametrize("n", [1, 2])
def test_replay_recovers_after_transients_on_the_same_session_and_records_attempts(
    worker_replay_5xx, n
):
    """``n`` 5xx then the answer: the scenario is ``ok``, ``retries`` counts the replays,
    every attempt reused the pre-assigned session id, and ``on_retry`` announced each
    next attempt before it ran."""
    invoke = worker_replay_5xx(n, S1)
    spec = _spec()
    session = spec["sessions"][0]
    announced: list[tuple[int, str]] = []
    record = jobs_batch._replay_session(
        _env(invoke), spec, session, Sink(),
        lambda next_attempt, exc: announced.append((next_attempt, type(exc).__name__)),
    )
    assert record["status"] == "ok" and record["code"] is None
    assert record["retries"] == n and "error" not in record
    assert invoke.calls == [(S1, "hi")] * (n + 1)  # same session id, every attempt
    assert announced == [(k + 2, "ClientError") for k in range(n)]
    assert announced[-1][0] <= TRANSIENT_SCENARIO_ATTEMPTS


def test_replay_gives_up_after_the_attempt_budget_as_an_errored_scenario(worker_replay_5xx):
    """Every attempt a 5xx: an ERRORED SCENARIO (``error_kind: infrastructure``), never a
    crashed run — ``attempts`` is the number actually made and the message agrees."""
    invoke = worker_replay_5xx(TRANSIENT_SCENARIO_ATTEMPTS + 1, S1)
    spec = _spec()
    record = jobs_batch._replay_session(_env(invoke), spec, spec["sessions"][0], Sink())
    assert record["status"] == "failed" and record["code"] == "ClientError"
    assert record["error_kind"] == jobs_batch.REPLAY_ERROR_KIND
    assert record["attempts"] == TRANSIENT_SCENARIO_ATTEMPTS
    assert record["retries"] == TRANSIENT_SCENARIO_ATTEMPTS - 1
    assert "InternalServerException" in record["error"]
    assert len(invoke.calls) == TRANSIENT_SCENARIO_ATTEMPTS
    text = jobs_batch._replay_failure_text(record)
    assert f"{TRANSIENT_SCENARIO_ATTEMPTS}/{TRANSIENT_SCENARIO_ATTEMPTS} attempts" in text


def test_a_faulty_session_does_not_touch_its_neighbours(worker_replay_5xx):
    """Faults are per session: S2 answers first time while S1 replays."""
    invoke = worker_replay_5xx(1, S1)
    spec = _spec()
    s1, s2 = spec["sessions"][0], spec["sessions"][1]
    assert jobs_batch._replay_session(_env(invoke), spec, s2, Sink())["retries"] == 0
    assert jobs_batch._replay_session(_env(invoke), spec, s1, Sink())["retries"] == 1
    assert [sid for sid, _ in invoke.calls] == [S2, S1, S1]


# ─── the judge ───────────────────────────────────────────────────────────────


def _judge_records(judge, provider, session_id):
    evaluators = engine_core.EvaluatorSet([HELPFULNESS], lambda model_id, **kw: judge,
                                          default_model_id="judge")
    return engine_core.evaluate_session(provider, session_id, evaluators, None,
                                        job_id="j-faults", sleep=lambda s: None)


@pytest.mark.parametrize("n", [1, 2])
def test_judge_recovers_after_throttles(worker_judge_throttle, spans_session, n):
    """A throttled judge call is retried (bounded back-off): ``n`` throttles then a
    verdict → one scored record, ``n + 1`` model calls."""
    provider, sid = spans_session
    judge = worker_judge_throttle(n)
    records = _judge_records(judge, provider, sid)
    assert judge.attempts == n + 1
    assert len(records) == 1 and records[0]["error_type"] is None
    assert records[0]["score"] is not None


JUDGE_RETRIES_5XX = judge_retries(BEDROCK_5XX_CLIENT)


@pytest.mark.xfail(
    condition=not JUDGE_RETRIES_5XX, strict=True,
    reason=("engine_core.evaluate_with_retry retries judge calls on throttling only "
            "(strands_evals.is_throttling_error); a model 5xx becomes an error record after "
            "one attempt — expected to flip with the shared classifier of r12-f"),
)
def test_judge_recovers_after_a_5xx(worker_judge_5xx, spans_session):
    """The spec: one Bedrock 5xx on the judge is a hiccup like a throttle — retried, the
    record scored. xfail while the engine answers an error record instead."""
    provider, sid = spans_session
    judge = worker_judge_5xx(1)
    records = _judge_records(judge, provider, sid)
    assert judge.attempts == 2
    assert records[0]["error_type"] is None and records[0]["score"] is not None


def test_a_persistent_judge_5xx_is_an_error_record_never_a_crashed_run(
    worker_judge_5xx, spans_session
):
    """Whatever the retry budget, a judge that keeps failing yields ONE error record per
    evaluator naming the exception — the session's other evaluators and the run go on."""
    provider, sid = spans_session
    judge = worker_judge_5xx(10)
    records = _judge_records(judge, provider, sid)
    assert len(records) == 1
    (record,) = records
    assert record["score"] is None and record["error_type"] == "ClientError"
    assert "InternalServerException" in record["error_message"]
    assert judge.attempts >= 1 and judge.faults  # it never got to a verdict
