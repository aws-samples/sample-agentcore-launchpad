"""Startup reconciliation of eval runs orphaned by a backend restart (N12 E1-4).

The queue worker polls the job's artefacts in-memory; a restart (or dev --reload) kills it
mid-poll while the worker Runtime keeps going. Rows with a dispatched job are re-attached by
``batch_eval_id``; rows killed before dispatch lost their plan and are failed honestly.
"""

import time

import app.evaluation.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.jobs import JobDone, JobStatus
from app.evaluation.models import EvalRun
from tests.evaluation.fakes import FakeExecutor, strip_ci


def make_run(**fields) -> str:
    db = SessionLocal()
    try:
        values = dict(
            workspace_id=DEFAULT_WORKSPACE_ID, agent_id="a1", agent_name="eval-target-v2",
            mode="insights", evaluators=[], session_ids=["s1", "s2"],
        )
        values.update(fields)
        run = EvalRun(**values)
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def get_run(run_id: str) -> EvalRun:
    db = SessionLocal()
    try:
        return db.get(EvalRun, run_id)
    finally:
        db.close()


def wait_status(run_id: str, target: str, timeout: float = 5.0) -> EvalRun:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = get_run(run_id)
        if run.status == target:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached {target}: {get_run(run_id).status}")


def _fake(monkeypatch) -> FakeExecutor:
    fake = FakeExecutor()
    monkeypatch.setattr(svc, "select_executor", lambda settings, job_type: fake)
    monkeypatch.setattr(svc, "POLL_INTERVAL_S", 0.02)
    return fake


def test_evaluating_run_with_a_job_is_reconciled(monkeypatch):
    fake = _fake(monkeypatch)
    run_id = make_run(status="evaluating", batch_eval_id="run_abc-123")
    fake.complete("run_abc-123", [], summary={"insights": {"failures": [{"category": "none"}]}})
    resumed = svc.resume_interrupted_runs()
    assert run_id in resumed
    run = wait_status(run_id, "completed")
    assert run.insights == {"failures": [{"category": "none"}]}
    assert run.error is None


def test_every_dispatched_phase_is_reattached(monkeypatch):
    fake = _fake(monkeypatch)
    ids = {phase: make_run(status=phase, batch_eval_id=f"job-{phase}", mode="evaluators")
           for phase in ("invoking", "waiting", "evaluating")}
    for job_id in (f"job-{p}" for p in ids):
        fake.complete(job_id, [{"session_id": "s1", "evaluator_id": "Builtin.Correctness",
                                "score": 1.0}])
    resumed = svc.resume_interrupted_runs()
    assert set(resumed) == set(ids.values())
    for run_id in ids.values():
        assert strip_ci(wait_status(run_id, "completed").scores) == [
            {"evaluatorId": "Builtin.Correctness", "score": 1.0,
                "count": 1, "n": 1, "unit": "turn"}]


def test_pre_dispatch_runs_are_failed_honestly(monkeypatch):
    _fake(monkeypatch)
    queued = make_run(status="queued")
    waiting = make_run(status="waiting")  # no batch_eval_id: never dispatched
    resumed = svc.resume_interrupted_runs()
    assert resumed == []
    for run_id in (queued, waiting):
        run = get_run(run_id)
        assert run.status == "failed"
        assert "backend restart" in run.error


def test_terminal_runs_untouched(monkeypatch):
    _fake(monkeypatch)
    done = make_run(status="completed")
    failed = make_run(status="failed", error="boom")
    assert svc.resume_interrupted_runs() == []
    assert get_run(done).status == "completed"
    assert get_run(failed).error == "boom"


def test_a_lost_job_is_failed_with_its_last_status(monkeypatch):
    """Re-attached, but the worker never moves again: stale → failed (a resumed poller
    has no plan to re-dispatch with)."""
    fake = _fake(monkeypatch)
    clock = {"t": 10_000.0}
    monkeypatch.setattr(svc, "_now", lambda: clock["t"])
    monkeypatch.setattr(svc, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + 120.0))
    run_id = make_run(status="evaluating", batch_eval_id="run_gone-1")
    stale = JobStatus(phase="evaluating",
                      progress={"done": 1, "total": 3, "message": "evaluating 1/3"})
    stale.updated_at = "2020-01-01T00:00:00+00:00"
    fake.statuses["run_gone-1"] = stale
    svc.resume_interrupted_runs()
    run = wait_status(run_id, "failed")
    assert "lost" in run.error and "evaluating 1/3" in run.error


def test_reconcile_failure_marks_run_failed(monkeypatch):
    fake = _fake(monkeypatch)
    run_id = make_run(status="evaluating", batch_eval_id="run_boom-1")

    def boom(ws, job_id):
        raise RuntimeError("ResourceNotFound")

    fake.status = boom  # type: ignore[method-assign]
    svc.resume_interrupted_runs()
    run = wait_status(run_id, "failed")
    assert "ResourceNotFound" in run.error


def test_done_json_shards_are_all_required(monkeypatch):
    """A sharded run (job ids <run>-s<i>) completes only when every shard has finished;
    one FAILED shard fails the run with its reason."""
    fake = _fake(monkeypatch)
    fake.jobs_with_prefix = lambda ws, prefix: [  # type: ignore[attr-defined]
        j for j in ("big-s0", "big-s1") if j.startswith(prefix)]
    run_id = make_run(status="evaluating", batch_eval_id="big", mode="evaluators")
    fake.complete("big-s0", [{"session_id": "s1", "evaluator_id": "Builtin.Correctness",
                              "score": 1.0}])
    fake.statuses["big-s1"] = JobStatus(phase="evaluating", state="FAILED")
    fake.dones["big-s1"] = JobDone(state="FAILED", error="judge quota exhausted")
    svc.resume_interrupted_runs()
    run = wait_status(run_id, "failed")
    assert "judge quota exhausted" in run.error
    assert strip_ci(run.scores) == [{"evaluatorId": "Builtin.Correctness", "score": 1.0,
        "count": 1, "n": 1, "unit": "turn"}]
