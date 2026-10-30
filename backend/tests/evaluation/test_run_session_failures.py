"""N12-obs47 (upstream 00eb359b parity) — a partially failed run records WHICH sessions it
left unscored and WHY, per session, so the console can explain "N of M sessions" instead of
echoing the first error, and so a telemetry-only partial run can seed a recommendation.

CN has no AWS batch evaluation: a partial run is a ``completed`` row with ``error`` +
``record_errors`` (N12-bug4) whose errored records live in ``eval_results``. The failure kind
is read off the worker's shared wording (``jobs_batch._replay_failure_text``): a session that
answered but whose spans never became ready → ``telemetry_incomplete``; a scenario the target
never answered → ``replay_failed``; a judge that errored on a session it did see →
``evaluator_error``.
"""

from __future__ import annotations

import pytest

import app.evaluation.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import results_store
from app.evaluation.jobs import JobDone
from app.evaluation.routers import _run_out
from app.services.strands_eval import jobs_batch
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeExecutor
from tests.evaluation.test_resume import get_run, make_run

SIDS = ["s1", "s2", "s3", "s4"]
HELP, CORR = "Builtin.Helpfulness", "Builtin.Correctness"


def _telemetry_text(scenario: str = "q1") -> str:
    return jobs_batch._replay_failure_text({
        "scenario_id": scenario, "failure": jobs_batch.TELEMETRY_FAILURE,
        "code": "TelemetryReadinessTimeout", "error": "no spans for session after 120s",
    })


def _replay_text(scenario: str = "q2") -> str:
    return jobs_batch._replay_failure_text({
        "scenario_id": scenario, "code": "ReadTimeoutError", "attempts": 3,
        "error": "Read timeout on endpoint URL",
    })


def _record(session: str, evaluator: str = HELP, *, error: str | None = None,
            error_type: str = "RuntimeError", score: float = 0.9) -> dict:
    return {
        "session_id": session, "trace_id": None, "evaluator_id": evaluator, "level": "TRACE",
        "score": None if error else score, "label": None if error else "pass",
        "explanation": None if error else "fine",
        "error_type": error_type if error else None, "error_message": error,
    }


# ─── the fold ─────────────────────────────────────────────────────────────────
def test_markers_match_the_workers_actual_wording():
    """The hub reads the kind off the message, so the markers must track the worker's text."""
    assert svc.TELEMETRY_FAILURE_MARKER in _telemetry_text()
    assert svc.REPLAY_FAILURE_MARKER in _replay_text() and " attempts: " in _replay_text()
    assert svc.TELEMETRY_FAILURE_MARKER not in _replay_text()
    telemetry = svc._failure_kind("TelemetryReadinessTimeout", _telemetry_text())
    assert telemetry == "telemetry_incomplete"
    assert svc._failure_kind("ReadTimeoutError", _replay_text()) == "replay_failed"
    assert svc._failure_kind("RuntimeError", "judge timed out") == "evaluator_error"


def test_telemetry_only_failures_are_classified_and_ordered_by_scenario_position():
    records = [
        _record("s1"),
        _record("s3", error=_telemetry_text("q3"), error_type="TelemetryReadinessTimeout"),
        _record("s3", CORR, error=_telemetry_text("q3"), error_type="TelemetryReadinessTimeout"),
        _record("s2", error=_telemetry_text("q2"), error_type="TelemetryReadinessTimeout"),
        _record("s4"),
    ]
    summary = svc.summarize_session_failures(records, SIDS)
    assert summary["total"] == 4 and summary["failed"] == 2
    assert summary["kind"] == "telemetry_incomplete"
    assert [(s["session_id"], s["index"], s["kind"]) for s in summary["sessions"]] == [
        ("s2", 2, "telemetry_incomplete"), ("s3", 3, "telemetry_incomplete"),
    ]
    assert summary["sessions"][0]["error_type"] == "TelemetryReadinessTimeout"
    assert "telemetry never became ready" in summary["sessions"][0]["message"]


def test_a_judge_error_on_a_session_makes_it_an_evaluator_failure_and_the_run_mixed():
    records = [
        _record("s1", error=_telemetry_text(), error_type="TelemetryReadinessTimeout"),
        _record("s2", error="placeholder missing", error_type="ValidationException"),
        _record("s2", CORR, error=_telemetry_text(), error_type="TelemetryReadinessTimeout"),
    ]
    summary = svc.summarize_session_failures(records, SIDS)
    assert summary["kind"] == "mixed"
    assert [s["kind"] for s in summary["sessions"]] == ["telemetry_incomplete", "evaluator_error"]


def test_unanswered_replays_are_their_own_kind():
    records = [_record("s4", error=_replay_text("q4"), error_type="ReadTimeoutError"),
               _record("s4", CORR, error=_replay_text("q4"), error_type="ReadTimeoutError")]
    summary = svc.summarize_session_failures(records, SIDS)
    assert summary["kind"] == "replay_failed" and summary["failed"] == 1
    assert summary["sessions"][0]["index"] == 4


def test_a_session_the_row_does_not_list_has_no_index_and_sorts_last():
    records = [_record("ghost", error="judge timed out"), _record("s1", error="judge timed out")]
    summary = svc.summarize_session_failures(records, SIDS)
    assert [(s["session_id"], s["index"]) for s in summary["sessions"]] == [
        ("s1", 1), ("ghost", None)]
    assert summary["total"] == 4  # the row's session count, never below the failed count
    # a row that lists no sessions (window run) counts the records' sessions
    assert svc.summarize_session_failures(records, [])["total"] == 2


def test_nothing_errored_is_no_summary():
    assert svc.summarize_session_failures([_record("s1"), _record("s2")], SIDS) is None
    assert svc.summarize_session_failures([], SIDS) is None


# ─── finish stores it; the API serves it ─────────────────────────────────────
def _settle(records: list[dict]) -> str:
    run_id = make_run(mode="evaluators", status="evaluating", evaluators=[HELP, CORR],
                      session_ids=list(SIDS), batch_eval_id="run_x")
    db = SessionLocal()
    try:
        results_store.persist(db, DEFAULT_WORKSPACE_ID, source="run", owner_id=run_id,
                              records=records)
    finally:
        db.close()
    svc._finish(run_id, "evaluators", [JobDone(state="COMPLETED", result_count=len(records))],
                workspace=ws_ctx(), executor=FakeExecutor(), job_ids=[run_id])
    return run_id


def test_a_partial_finish_stores_the_session_summary_next_to_the_record_summary():
    run = get_run(_settle([
        _record("s1"), _record("s2"),
        _record("s3", error=_telemetry_text("q3"), error_type="TelemetryReadinessTimeout"),
        _record("s3", CORR, error=_telemetry_text("q3"), error_type="TelemetryReadinessTimeout"),
        _record("s4"),
    ]))
    assert run.status == "completed" and run.error.startswith("2 of 5 evaluator records errored")
    assert run.record_errors["count"] == 2  # N12-bug4's record summary is untouched
    assert run.session_failures == {
        "total": 4, "failed": 1, "kind": "telemetry_incomplete",
        "sessions": [{"session_id": "s3", "index": 3, "kind": "telemetry_incomplete",
                      "error_type": "TelemetryReadinessTimeout",
                      "message": _telemetry_text("q3"), "arm": None}],  # arm: N12-obs62
    }
    out = _run_out(run, snapshot=None)
    assert out["session_failures"] == run.session_failures and out["errors"] == run.record_errors


def test_a_clean_finish_and_an_all_failed_finish_store_no_session_summary():
    clean = get_run(_settle([_record("s1"), _record("s2")]))
    assert clean.session_failures is None
    assert _run_out(clean, snapshot=None)["session_failures"] is None
    # every record errored → `failed` (N12-bug4); the per-session fold still rides along so
    # the console can list which scenarios never answered
    failed = get_run(_settle([
        _record("s1", error=_replay_text("q1"), error_type="ReadTimeoutError"),
        _record("s2", error=_replay_text("q2"), error_type="ReadTimeoutError"),
    ]))
    assert failed.status == "failed"
    assert failed.session_failures["kind"] == "replay_failed"
    assert failed.session_failures["failed"] == 2


# ─── legacy rows: backfill on results read, or via re-check ──────────────────
def _legacy_partial() -> str:
    run_id = make_run(mode="evaluators", status="completed", evaluators=[HELP],
                      session_ids=list(SIDS), batch_eval_id="run_legacy",
                      error="1 of 3 evaluator records errored — first error: RuntimeError: x",
                      record_errors={"count": 1, "total": 3, "first_type": "RuntimeError",
                                     "first_message": "x"})
    db = SessionLocal()
    try:
        results_store.persist(db, DEFAULT_WORKSPACE_ID, source="run", owner_id=run_id, records=[
            _record("s1"), _record("s2"),
            _record("s4", error=_telemetry_text("q4"), error_type="TelemetryReadinessTimeout"),
        ])
    finally:
        db.close()
    assert get_run(run_id).session_failures is None
    return run_id


def test_reading_a_legacy_partial_runs_results_backfills_the_summary():
    run_id = _legacy_partial()
    res = svc.run_results(get_run(run_id), workspace=ws_ctx())
    assert res["available"] is True and len(res["sessions"]) == 3
    stored = get_run(run_id).session_failures
    assert stored["kind"] == "telemetry_incomplete" and stored["failed"] == 1
    assert stored["sessions"][0]["index"] == 4
    # a second read leaves the stored summary alone (no longer legacy)
    svc.run_results(get_run(run_id), workspace=ws_ctx())
    assert get_run(run_id).session_failures == stored


def test_recheck_of_a_legacy_partial_run_backfills_without_touching_the_worker():
    run_id = _legacy_partial()
    run = svc.recheck_run(run_id, workspace=ws_ctx())
    assert run.status == "completed" and run.error  # the row is not re-opened
    assert run.session_failures["kind"] == "telemetry_incomplete"


def test_recheck_of_a_clean_completed_run_is_still_refused():
    run_id = make_run(mode="evaluators", status="completed", batch_eval_id="run_clean")
    with pytest.raises(AppError) as exc:
        svc.recheck_run(run_id, workspace=ws_ctx())
    assert exc.value.code == "run.not_recheckable" and exc.value.status_code == 409
    # …and so is a partial run that already carries its summary
    settled = make_run(mode="evaluators", status="completed", batch_eval_id="run_s",
                       error="1 of 2 evaluator records errored — first error: RuntimeError: x",
                       session_failures={"total": 2, "failed": 1, "kind": "evaluator_error",
                                         "sessions": []})
    with pytest.raises(AppError):
        svc.recheck_run(settled, workspace=ws_ctx())
