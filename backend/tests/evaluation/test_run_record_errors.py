"""N12-bug4: a run whose evaluator records ALL errored on the worker (``error_type`` on
every ``results.jsonl`` record) used to settle as a green ``completed`` with ``scores: []``.
Every record errored → ``failed`` with a readable reason; some → ``completed`` with the
summary on ``error`` + ``errors`` (the console's completed-with-errors state); none / zero
records → unchanged."""

from __future__ import annotations

import app.evaluation.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import results_store
from app.evaluation.jobs import JobDone
from app.evaluation.routers import _run_out
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeExecutor, strip_ci
from tests.evaluation.test_resume import get_run, make_run

TOKEN_ERROR = ("no workload access token: the invoke must carry runtimeUserId (the platform "
               "always sends it) or LAUNCHPAD_WORKLOAD_NAME must name this runtime's identity")


def _record(session: str, evaluator: str, *, error: str | None = None, score: float = 0.9):
    return {
        "session_id": session, "trace_id": None, "evaluator_id": evaluator, "level": "TRACE",
        "score": None if error else score, "label": None if error else "pass",
        "explanation": None if error else "fine",
        "error_type": "RuntimeError" if error else None, "error_message": error,
    }


def _settle(records: list[dict], mode: str = "evaluators") -> str:
    run_id = make_run(mode=mode, status="evaluating", evaluators=["Builtin.Helpfulness"],
                      session_ids=sorted({r["session_id"] for r in records}) or ["s1"])
    db = SessionLocal()
    try:
        results_store.persist(db, DEFAULT_WORKSPACE_ID, source="run", owner_id=run_id,
                              records=records)
    finally:
        db.close()
    svc._finish(run_id, mode, [JobDone(state="COMPLETED", result_count=len(records))],
                workspace=ws_ctx(), executor=FakeExecutor(), job_ids=[run_id])
    return run_id


def test_every_record_errored_fails_the_run_with_the_first_error():
    records = [_record(f"s{i}", ev, error=TOKEN_ERROR)
               for i in range(5) for ev in ("Builtin.Helpfulness", "Builtin.Correctness")]
    run = get_run(_settle(records))
    assert run.status == "failed"
    assert run.error.startswith("all 10 evaluator records failed — first error: RuntimeError: ")
    assert "no workload access token" in run.error
    assert run.scores == []
    assert run.record_errors == {
        "count": 10, "total": 10, "first_type": "RuntimeError", "first_message": TOKEN_ERROR,
    }
    out = _run_out(run, snapshot=None)
    assert out["errors"] == run.record_errors and out["status"] == "failed"


def test_some_records_errored_keeps_completed_with_the_summary():
    records = [_record("s1", "Builtin.Helpfulness"), _record("s2", "Builtin.Helpfulness"),
               _record("s3", "Builtin.Helpfulness", error="judge timed out")]
    run = get_run(_settle(records))
    assert run.status == "completed"
    assert run.error == ("1 of 3 evaluator records errored — first error: "
                         "RuntimeError: judge timed out")
    assert run.record_errors == {"count": 1, "total": 3, "first_type": "RuntimeError",
                                 "first_message": "judge timed out"}
    # the scored records still aggregate; the errored one has no score to average
    assert strip_ci(run.scores) == [{"evaluatorId": "Builtin.Helpfulness", "score": 0.9,
        "count": 2, "n": 2, "unit": "turn"}]


def test_clean_records_and_zero_records_are_unchanged():
    clean = get_run(_settle([_record("s1", "Builtin.Helpfulness")]))
    assert (clean.status, clean.error, clean.record_errors) == ("completed", None, None)
    assert _run_out(clean, snapshot=None)["errors"] is None
    empty = get_run(_settle([]))
    assert (empty.status, empty.error, empty.record_errors) == ("completed", None, None)
    assert empty.scores == []


def test_a_failed_job_keeps_its_own_reason_but_records_the_summary():
    records = [_record("s1", "Builtin.Helpfulness", error=TOKEN_ERROR)]
    run_id = make_run(mode="evaluators", status="evaluating", session_ids=["s1"])
    db = SessionLocal()
    try:
        results_store.persist(db, DEFAULT_WORKSPACE_ID, source="run", owner_id=run_id,
                              records=records)
    finally:
        db.close()
    svc._finish(run_id, "evaluators", [JobDone(state="FAILED", error="job lost")],
                workspace=ws_ctx(), executor=FakeExecutor(), job_ids=[run_id])
    run = get_run(run_id)
    assert run.status == "failed" and run.error == "job lost"
    assert run.record_errors["count"] == 1


def _settle_insights(errors: list[dict], sessions: list[str]) -> str:
    run_id = make_run(mode="insights", status="evaluating", session_ids=sessions)
    blob = {"failures": [], "userIntents": [], "executionSummaries": []}
    if errors:
        blob["errors"] = errors
    svc._finish(run_id, "insights", [JobDone(state="COMPLETED", summary={"insights": blob})],
                workspace=ws_ctx(), executor=FakeExecutor(), job_ids=[run_id])
    return run_id


def test_insights_run_whose_every_session_errored_fails():
    errors = [{"session_id": s, "error": f"RuntimeError: {TOKEN_ERROR}"} for s in ("s1", "s2")]
    run = get_run(_settle_insights(errors, ["s1", "s2"]))
    assert run.status == "failed"
    assert run.error.startswith("all 2 insight sessions failed — first error: RuntimeError: ")
    assert run.record_errors["count"] == 2 and run.record_errors["first_type"] == "RuntimeError"
    assert run.insights["errors"] == errors  # the per-session detail stays on the blob


def test_insights_run_with_some_errored_sessions_stays_completed():
    errors = [{"session_id": "s2", "error": "FailureAnalysis ValueError: empty transcript"}]
    run = get_run(_settle_insights(errors, ["s1", "s2", "s3"]))
    assert run.status == "completed"
    assert run.error == ("1 of 3 insight sessions errored — first error: "
                         "FailureAnalysis ValueError: empty transcript")
    assert run.record_errors == {"count": 1, "total": 3, "first_type": "FailureAnalysis ValueError",
                                 "first_message": "empty transcript"}
    clean = get_run(_settle_insights([], ["s1"]))
    assert (clean.status, clean.error, clean.record_errors) == ("completed", None, None)


def test_insights_run_without_listed_sessions_never_fails_on_an_unknown_denominator():
    """Review #4: sessions resolved on the worker leave `run.session_ids` empty; one errored
    session must not read as "all sessions failed" — the run stays completed with the
    summary (and the summary says the denominator is unknown)."""
    errors = [{"session_id": "s9", "error": "RuntimeError: judge down"}]
    run = get_run(_settle_insights(errors, []))
    assert run.status == "completed"
    assert run.error == "1 of 1 insight sessions errored — first error: RuntimeError: judge down"
    assert run.record_errors == {"count": 1, "total": 1, "first_type": "RuntimeError",
                                 "first_message": "judge down", "total_known": False}
