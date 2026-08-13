"""Startup reconciliation of eval runs orphaned by a backend restart.

The account-lock worker polls batch evaluations in-memory; a restart (or dev
--reload) kills it mid-poll and the row stays "evaluating" forever even though
AWS finishes the batch — observed live with run 7c8fda73 (COMPLETED_WITH_ERRORS
on the AWS side, stuck EVALUATING in the UI).
"""

import time
from unittest.mock import MagicMock

import app.evaluation.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalRun


def make_run(**fields) -> str:
    db = SessionLocal()
    try:
        run = EvalRun(

                workspace_id=DEFAULT_WORKSPACE_ID,
                agent_id="a1", agent_name="eval-target-v2", mode="insights",
            evaluators=[], session_ids=["s1", "s2"], **fields,
        )
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


def test_evaluating_run_with_batch_is_reconciled(monkeypatch):
    run_id = make_run(status="evaluating", batch_eval_id="run_abc-123")
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: MagicMock())
    monkeypatch.setattr(
        svc.ac, "poll_batch_evaluation",
        lambda client, batch_id, max_polls=60: {
            "status": "COMPLETED_WITH_ERRORS",
            "errorDetails": ["EXECUTION_SUMMARY clustering failed: got 2, need 3"],
            "failureAnalysisResult": {"failures": [{"category": "none"}]},
        },
    )
    resumed = svc.resume_interrupted_runs()
    assert run_id in resumed
    run = wait_status(run_id, "completed")
    assert run.insights == {"failures": [{"category": "none"}]}
    assert "clustering failed" in run.error  # partial-success reason surfaced


def test_pre_batch_runs_are_failed_honestly():
    queued = make_run(status="queued")
    waiting = make_run(status="waiting")
    resumed = svc.resume_interrupted_runs()
    assert resumed == []
    for run_id in (queued, waiting):
        run = get_run(run_id)
        assert run.status == "failed"
        assert "backend restart" in run.error


def test_terminal_runs_untouched():
    done = make_run(status="completed")
    failed = make_run(status="failed", error="boom")
    assert svc.resume_interrupted_runs() == []
    assert get_run(done).status == "completed"
    assert get_run(failed).error == "boom"


def test_reconcile_failure_marks_run_failed(monkeypatch):
    run_id = make_run(status="evaluating", batch_eval_id="run_gone-1")
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: MagicMock())

    def boom(client, batch_id, max_polls=60):
        raise RuntimeError("ResourceNotFound")

    monkeypatch.setattr(svc.ac, "poll_batch_evaluation", boom)
    svc.resume_interrupted_runs()
    run = wait_status(run_id, "failed")
    assert "ResourceNotFound" in run.error
