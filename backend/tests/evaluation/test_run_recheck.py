"""A run whose poller gave up is re-read from AWS instead of staying failed.

Live 2026-09-27 (prod, run f70a3878a4c2): a 16-session x 10-evaluator batch was
still IN_PROGRESS when the poll budget ran out, the row was failed with
"batch evaluation ended IN_PROGRESS", and AWS completed the batch 25 minutes
later — with no way to read the result back into the ledger.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.evaluation.service as svc
from app.core.errors import AppError
from tests.evaluation.test_resume import get_run, make_run, wait_status

COMPLETED = {
    "status": "COMPLETED",
    "evaluationResults": {"evaluatorSummaries": [
        {"evaluatorId": "Builtin.Correctness", "statistics": {"averageScore": 0.71}},
    ]},
}


def _data(result):
    client = MagicMock()
    client.get_batch_evaluation.return_value = result
    return client


def test_poll_budget_follows_the_configured_wait(monkeypatch):
    seen = {}
    monkeypatch.setattr(svc, "get_settings", lambda: SimpleNamespace(eval_batch_wait_s=2700))
    monkeypatch.setattr(svc.ac, "poll_batch_evaluation",
                        lambda client, **kw: seen.update(kw) or COMPLETED)

    svc._poll_batch(MagicMock(), "be-1")

    assert seen == {"batch_id": "be-1", "max_polls": 90, "interval": 30.0}


def test_default_wait_outlasts_the_measured_45_minute_batch():
    assert svc.get_settings().eval_batch_wait_s >= 45 * 60


def test_batch_still_running_after_the_wait_says_so():
    run_id = make_run(status="evaluating", batch_eval_id="run_slow-1")

    message = r"still IN_PROGRESS after the \d+-minute wait.*re-check"
    with pytest.raises(RuntimeError, match=message):
        svc._finish_from_result(run_id, "evaluators", {"status": "IN_PROGRESS"})


def test_recheck_settles_a_failed_run_whose_batch_completed(monkeypatch):
    run_id = make_run(status="failed", batch_eval_id="run_done-1",
                      error="RuntimeError: batch evaluation ended IN_PROGRESS")
    db = svc.SessionLocal()
    db.get(svc.EvalRun, run_id).mode = "evaluators"
    db.commit()
    db.close()
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: _data(COMPLETED))

    run = svc.recheck_run(run_id, workspace=MagicMock())

    assert run.status == "completed"
    assert run.error is None
    assert run.scores == [{"evaluatorId": "Builtin.Correctness", "score": 0.71}]


def test_recheck_resumes_polling_while_the_batch_still_runs(monkeypatch):
    run_id = make_run(status="failed", batch_eval_id="run_busy-1", error="gave up")
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: _data({"status": "IN_PROGRESS"}))
    monkeypatch.setattr(svc.ac, "poll_batch_evaluation", lambda client, **_: {
        "status": "COMPLETED_WITH_ERRORS", "errorDetails": ["clustering: need 3"],
        "failureAnalysisResult": {"failures": []},
    })

    run = svc.recheck_run(run_id, workspace=MagicMock())

    assert run.status in ("evaluating", "completed")
    assert wait_status(run_id, "completed").error == "clustering: need 3"


@pytest.mark.parametrize("fields", [
    {"status": "completed", "batch_eval_id": "run_x-1"},
    {"status": "failed", "batch_eval_id": None},
    {"status": "evaluating", "batch_eval_id": "run_x-2"},
])
def test_recheck_refuses_runs_without_a_failed_batch(monkeypatch, fields):
    run_id = make_run(**fields)
    client = _data(COMPLETED)
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: client)

    with pytest.raises(AppError) as err:
        svc.recheck_run(run_id, workspace=MagicMock())

    assert err.value.code == "run.not_recheckable"
    client.get_batch_evaluation.assert_not_called()
    assert get_run(run_id).status == fields["status"]
