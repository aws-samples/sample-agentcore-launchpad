"""P4.6 — online evaluation retries a transiently FAILED sample, boundedly.

A sample whose evaluation failed for a transient reason (the job failed or was lost, the
worker wrote nothing for the session, a row failed on throttling / a timeout / telemetry
readiness) is dispatched again after each ``online_eval_retry_backoff_s`` wait, as its own
job, while the job window still reaches the session; ``attempts`` counts the dispatches.
An agent failure (``failed_turn``), an operator stop and other errors are final. Before,
every failure was final: one throttled minute lost the session for good.
"""

from __future__ import annotations

from datetime import timedelta

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.evaluation import jobs
from app.evaluation import online_scheduler as sched
from app.evaluation.jobs import JobDone
from app.evaluation.models import EvalResult, OnlineSample
from tests.evaluation.test_online_configs import ledger_row, make_agent
from tests.evaluation.test_online_scheduler import NOW, _again, harness, scan_row

CONFIG = "oe_retry-1"
SID = "session-retry-1"
EVALUATOR = "Builtin.Helpfulness"


def _rec(job_id: str, *, score=None, error_type=None, error_message=None, ordinal=0):
    return {"session_id": SID, "trace_id": "t" * 32, "evaluator_id": EVALUATOR,
            "level": "TRACE", "score": score, "label": None if score is None else "ok",
            "explanation": "", "error_type": error_type, "error_message": error_message,
            "id": jobs.record_id(job_id, SID, EVALUATOR, ordinal)}


def _throttled(job_id: str) -> dict:
    return _rec(job_id, error_type="UpstreamTransientExhausted",
                error_message="ThrottlingException: Too many requests (3 attempts)")


def _setup(settings=None, *, config=CONFIG):
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, config, sampling_percentage=100.0, session_timeout_minutes=1)
    executor, _, kwargs = harness(rows=[scan_row(SID, NOW - timedelta(minutes=5))],
                                  settings=settings)
    sched.tick(db, NOW, **kwargs)
    return db, executor, kwargs


def _sample(db, config=CONFIG) -> OnlineSample:
    db.expire_all()
    return db.query(OnlineSample).filter_by(config_id=config).one()


def test_a_throttled_sample_is_retried_after_the_backoff_and_its_judgement_replaces_the_error():
    db, executor, kwargs = _setup()
    first = executor.submitted[0].job_id
    executor.complete(first, [_throttled(first)])
    sched.tick(db, NOW + timedelta(minutes=1), **_again(kwargs))
    sample = _sample(db)
    assert (sample.status, sample.attempts) == ("failed", 1)
    assert sample.retry_at is not None and "throttling" in sample.error
    wait = get_settings().online_eval_retry_backoff_s[0]
    assert sched._aware(sample.retry_at) == NOW + timedelta(minutes=1, seconds=wait)
    # not yet due: nothing is dispatched
    sched.tick(db, NOW + timedelta(minutes=1, seconds=wait - 5), **_again(kwargs))
    assert len(executor.submitted) == 1 and _sample(db).status == "failed"
    # due: the second attempt is its own job
    sched.tick(db, NOW + timedelta(minutes=1, seconds=wait + 5), **_again(kwargs))
    assert [j.job_id for j in executor.submitted] == [first, f"{first}-a2"]
    assert [s["session_id"] for s in executor.submitted[1].spec["sessions"]] == [SID]
    sample = _sample(db)
    assert (sample.status, sample.attempts, sample.retry_at) == ("dispatched", 2, None)
    retry = executor.submitted[1].job_id
    executor.complete(retry, [_rec(retry, score=0.75)])
    sched.tick(db, NOW + timedelta(minutes=10), **_again(kwargs))
    sample = _sample(db)
    assert (sample.status, sample.error, sample.attempts) == ("evaluated", None, 2)
    rows = db.query(EvalResult).all()
    assert len(rows) == 1  # the error row was replaced, not duplicated
    assert (rows[0].score, rows[0].error_type, rows[0].error_message) == (0.75, None, None)
    assert rows[0].id == jobs.record_id(CONFIG, SID, EVALUATOR, 0)
    db.close()


def test_retries_are_bounded_by_the_backoff_list():
    db, executor, kwargs = _setup()
    waits = get_settings().online_eval_retry_backoff_s
    at = NOW
    for attempt in range(1, len(waits) + 2):
        job_id = executor.submitted[-1].job_id
        executor.complete(job_id, [])  # the worker wrote nothing for the session
        at += timedelta(minutes=1)
        sched.tick(db, at, **_again(kwargs))
        sample = _sample(db)
        assert (sample.status, sample.attempts) == ("failed", attempt)
        if attempt <= len(waits):
            at = sched._aware(sample.retry_at) + timedelta(seconds=1)
            sched.tick(db, at, **_again(kwargs))
    sample = _sample(db)
    assert sample.retry_at is None and "retries exhausted" in sample.error
    assert len(executor.submitted) == 1 + len(waits)
    sched.tick(db, at + timedelta(days=1), **_again(kwargs))
    assert len(executor.submitted) == 1 + len(waits)
    db.close()


def test_a_failed_turn_is_the_agents_failure_and_never_retried():
    db, executor, kwargs = _setup()
    job_id = executor.submitted[0].job_id
    executor.complete(job_id, [_rec(job_id, error_type="span_status_error",
                                    error_message="the agent's turn failed (span ERROR)")])
    sched.tick(db, NOW + timedelta(minutes=1), **_again(kwargs))
    sample = _sample(db)
    assert (sample.status, sample.retry_at) == ("evaluated", None)
    sched.tick(db, NOW + timedelta(hours=2), **_again(kwargs))
    assert len(executor.submitted) == 1
    db.close()


def test_a_non_transient_evaluator_error_and_an_operator_stop_are_final():
    db, executor, kwargs = _setup()
    job_id = executor.submitted[0].job_id
    executor.complete(job_id, [_rec(job_id, error_type="ValueError",
                                    error_message="rating scale has no such label")])
    sched.tick(db, NOW + timedelta(minutes=1), **_again(kwargs))
    assert (_sample(db).status, _sample(db).retry_at) == ("evaluated", None)
    db.close()

    db, executor, kwargs = _setup(config="oe_retry-stop")
    job_id = executor.submitted[-1].job_id
    executor.dones[job_id] = JobDone(state="STOPPED", error="stopped by the operator")
    sched.tick(db, NOW + timedelta(minutes=1), **_again(kwargs))
    sample = _sample(db, "oe_retry-stop")
    assert (sample.status, sample.retry_at) == ("failed", None)
    db.close()


def test_readiness_a_lost_job_and_a_failed_job_are_transient():
    for outcome in ("readiness", "lost", "failed"):
        config = f"oe_retry-{outcome}"
        db, executor, kwargs = _setup(config=config)
        job_id = executor.submitted[-1].job_id
        if outcome == "readiness":
            executor.complete(job_id, [_rec(
                job_id, error_type="SessionNotFoundError",
                error_message="CloudWatch: no spans found for session_id")])
            at = NOW + timedelta(minutes=1)
        elif outcome == "lost":
            executor.statuses.clear()
            at = NOW + timedelta(seconds=get_settings().eval_job_stale_s + 60)
        else:
            executor.dones[job_id] = JobDone(state="FAILED", error="RuntimeError: worker died")
            at = NOW + timedelta(minutes=1)
        sched.tick(db, at, **_again(kwargs))
        sample = _sample(db, config)
        assert sample.status == "failed" and sample.retry_at is not None, outcome
        db.close()


def test_no_retry_once_the_job_window_cannot_reach_the_session_or_with_no_backoff():
    db, executor, kwargs = _setup()
    job_id = executor.submitted[0].job_id
    executor.complete(job_id, [_throttled(job_id)])
    # the result arrives after the session aged past the job window (MAX_SCAN_HOURS)
    sched.tick(db, NOW + timedelta(hours=sched.MAX_SCAN_HOURS - 2), **_again(kwargs))
    sample = _sample(db)
    assert (sample.status, sample.retry_at) == ("failed", None)
    db.close()

    no_retry = get_settings().model_copy(update={"online_eval_retry_backoff_s": []})
    db, executor, kwargs = _setup(no_retry, config="oe_retry-none")
    job_id = executor.submitted[-1].job_id
    executor.complete(job_id, [_throttled(job_id)])
    sched.tick(db, NOW + timedelta(minutes=1), **_again(kwargs))
    sample = _sample(db, "oe_retry-none")
    assert (sample.status, sample.retry_at, sample.attempts) == ("failed", None, 1)
    db.close()


def test_a_retry_job_id_is_derivable_from_the_sample_rows():
    stamp = NOW
    assert sched.job_id_for("oe_a", stamp) == sched.job_id_for("oe_a", stamp, 1)
    assert sched.job_id_for("oe_a", stamp, 3) == f"{sched.job_id_for('oe_a', stamp)}-a3"
