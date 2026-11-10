"""A run whose poller gave up is re-attached to its job instead of staying failed (N12 E1-4).

Live 2026-09-27 (prod, run f70a3878a4c2): a 16-session x 10-evaluator job was still running
when the poll budget ran out, the row was failed, and the job completed 25 minutes later —
with no way to read the result back into the ledger. ``recheck`` re-reads the job's
artefacts: a finished job settles the row, a running one resumes polling, one that left no
artefacts is lost for good.
"""

import pytest

import app.evaluation.service as svc
from app.core.errors import AppError
from app.evaluation.jobs import JobDone, JobStatus
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeExecutor, strip_ci
from tests.evaluation.test_resume import get_run, make_run, wait_status

RECORDS = [{"session_id": "s1", "evaluator_id": "Builtin.Correctness", "score": 0.71},
           {"session_id": "s2", "evaluator_id": "Builtin.Correctness", "score": 0.71}]


def _fake(monkeypatch) -> FakeExecutor:
    fake = FakeExecutor()
    monkeypatch.setattr(svc, "select_executor", lambda settings, job_type: fake)
    monkeypatch.setattr(svc, "POLL_INTERVAL_S", 0.02)
    return fake


def test_default_wait_outlasts_the_measured_45_minute_batch():
    assert svc.get_settings().eval_batch_wait_s >= 45 * 60


def test_job_still_running_after_the_wait_fails_with_a_recheck_hint(monkeypatch):
    fake = _fake(monkeypatch)
    clock = {"t": 0.0}
    monkeypatch.setattr(svc, "_now", lambda: clock["t"])
    monkeypatch.setattr(svc, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + 3600.0))
    run_id = make_run(status="queued", mode="evaluators")
    plan = svc.RunPlan(run_id=run_id, job_type="batch_eval", target={},
                       sessions=[{"session_id": "s1"}], replay=False, wait={})
    svc.execute_run(run_id, workspace=ws_ctx(), plan=plan)
    run = get_run(run_id)
    assert run.status == "failed"
    assert "still running after the" in run.error and "re-check" in run.error
    assert fake.submitted and run.batch_eval_id == run_id


def test_recheck_settles_a_failed_run_whose_job_completed(monkeypatch):
    fake = _fake(monkeypatch)
    run_id = make_run(status="failed", batch_eval_id="job-done-1", mode="evaluators",
                      error="RuntimeError: evaluation job still running")
    fake.complete("job-done-1", RECORDS)

    run = svc.recheck_run(run_id, workspace=ws_ctx())

    # re-attached; the queue worker settles it (it may already have by the time we read)
    assert run.status in ("evaluating", "completed")
    run = wait_status(run_id, "completed")
    assert run.error is None
    assert strip_ci(run.scores) == [{"evaluatorId": "Builtin.Correctness", "score": 0.71,
        "count": 2, "n": 2, "unit": "turn"}]


def test_recheck_resumes_polling_while_the_job_still_runs(monkeypatch):
    fake = _fake(monkeypatch)
    run_id = make_run(status="failed", batch_eval_id="job-busy-1", error="gave up")
    fake.statuses["job-busy-1"] = JobStatus(phase="evaluating")

    run = svc.recheck_run(run_id, workspace=ws_ctx())
    assert run.status in ("evaluating", "completed")
    fake.complete("job-busy-1", [], summary={"insights": {"failures": []}})
    fake.dones["job-busy-1"] = JobDone(state="COMPLETED", summary={"insights": {"failures": []}})
    run = wait_status(run_id, "completed")
    assert run.insights == {"failures": []}


def test_recheck_of_a_job_without_artefacts_is_lost_for_good(monkeypatch):
    fake = _fake(monkeypatch)
    run_id = make_run(status="failed", batch_eval_id="job-ghost", error="gave up")

    run = svc.recheck_run(run_id, workspace=ws_ctx())

    assert run.status == "failed" and "submit the run again" in run.error
    assert fake.submitted == []  # a recheck never dispatches


@pytest.mark.parametrize("fields", [
    {"status": "completed", "batch_eval_id": "job-x-1"},
    {"status": "failed", "batch_eval_id": None},
    {"status": "evaluating", "batch_eval_id": "job-x-2"},
])
def test_recheck_refuses_runs_without_a_failed_job(monkeypatch, fields):
    fake = _fake(monkeypatch)
    run_id = make_run(**fields)

    with pytest.raises(AppError) as err:
        svc.recheck_run(run_id, workspace=ws_ctx())

    assert err.value.code == "run.not_recheckable"
    assert fake.submitted == [] and get_run(run_id).status == fields["status"]
