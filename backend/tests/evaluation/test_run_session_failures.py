"""A partially failed batch records which sessions it skipped, and why.

Live 2026-10-09 (prod, runs 7b02644fbdcb / db19fbfeec45): COMPLETED_WITH_ERRORS with
"2 of 16" / "5 of 16 sessions failed during batch evaluation." — every failed session
carried ``LogEventMissingException`` (Harness runtime log events of whole event-loop
cycles never arrived) and one code evaluator ``NO_SPANS``. The row only kept the AWS
sentence, so the console could neither say why nor that a retry cannot help.
"""

from unittest.mock import MagicMock

import pytest

import app.evaluation.agentcore_eval as ac
import app.evaluation.service as svc
from app.core.errors import AppError
from tests.evaluation.test_resume import get_run, make_run

MISSING = ("Session span data is incomplete. Span with ID: 141288c7b97bdc64 and name: "
           "execute_tool web-search-tool___WebSearch is missing a corresponding log event.")


def _record(sid, evaluator="Builtin.Correctness", error_type=None, message=None, score=None):
    attrs = {"session.id": sid, "gen_ai.evaluation.name": evaluator}
    if message:
        attrs["error.type"] = error_type
        attrs["error.message"] = message
    else:
        attrs["gen_ai.evaluation.score.value"] = score
    return attrs


def _result(failed, total=4, status="COMPLETED_WITH_ERRORS"):
    return {
        "status": status,
        "errorDetails": [f"{failed} of {total} sessions failed during batch evaluation."]
        if failed else [],
        "evaluationResults": {
            "numberOfSessionsFailed": failed, "totalNumberOfSessions": total,
            "evaluatorSummaries": [
                {"evaluatorId": "Builtin.Correctness", "statistics": {"averageScore": 0.5},
                 "totalEvaluated": total, "totalFailed": failed},
            ],
        },
        "outputConfig": {"cloudWatchConfig": {"logGroupName": "g", "logStreamName": "s"}},
    }


SIDS = ["s1", "s2", "s3", "s4"]


def test_telemetry_only_failures_are_classified_and_ordered():
    records = [
        _record("s1", score=1.0),
        _record("s3", error_type="LogEventMissingException", message=MISSING),
        _record("s3", "Custom.ToolSurface", error_type="NO_SPANS",
                message="evaluationInput.sessionSpans is empty or missing"),
        _record("s2", error_type="LogEventMissingException", message=MISSING),
    ]

    summary = ac.summarize_session_failures(_result(2), records, SIDS)

    assert summary["total"] == 4 and summary["failed"] == 2
    assert summary["kind"] == "telemetry_incomplete"
    assert [(s["session_id"], s["index"], s["kind"]) for s in summary["sessions"]] == [
        ("s2", 2, "telemetry_incomplete"), ("s3", 3, "telemetry_incomplete"),
    ]
    assert summary["sessions"][0]["error_type"] == "LogEventMissingException"


def test_an_evaluator_error_on_a_session_makes_it_an_evaluator_failure():
    records = [
        _record("s1", error_type="LogEventMissingException", message=MISSING),
        _record("s2", error_type="ValidationException", message="placeholder missing"),
        _record("s2", error_type="LogEventMissingException", message=MISSING),
    ]

    summary = ac.summarize_session_failures(_result(2), records, SIDS)

    assert summary["kind"] == "mixed"
    assert [s["kind"] for s in summary["sessions"]] == ["telemetry_incomplete", "evaluator_error"]


def test_evaluator_errors_only():
    records = [_record("s4", error_type="ThrottlingException", message="Rate exceeded")]

    summary = ac.summarize_session_failures(_result(1), records, SIDS)

    assert summary["kind"] == "evaluator_error"
    assert summary["sessions"][0]["index"] == 4


def test_failures_the_stream_does_not_show_are_unknown():
    summary = ac.summarize_session_failures(_result(3), [], SIDS)

    assert summary == {"total": 4, "failed": 3, "kind": "unknown", "sessions": []}


def test_nothing_failed_is_no_summary():
    assert ac.summarize_session_failures(
        _result(0, status="COMPLETED"), [_record("s1", score=1.0)], SIDS) is None


def _stream(monkeypatch, records):
    reads = []

    def read(_logs, group, stream, **_):
        reads.append((group, stream))
        return records

    monkeypatch.setattr(svc.ac, "read_result_records", read)
    return reads


def _evaluator_run(**fields):
    run_id = make_run(**fields)
    db = svc.SessionLocal()
    run = db.get(svc.EvalRun, run_id)
    run.mode = "evaluators"
    run.session_ids = list(SIDS)
    db.commit()
    db.close()
    return run_id


def test_a_partial_finish_stores_the_summary_with_one_stream_read(monkeypatch):
    run_id = _evaluator_run(status="evaluating", batch_eval_id="run_p-1")
    reads = _stream(monkeypatch, [
        _record("s1", score=1.0),
        _record("s2", error_type="LogEventMissingException", message=MISSING),
    ])
    result = _result(1)
    # a code evaluator without an average forces parse_eval_scores to read too
    result["evaluationResults"]["evaluatorSummaries"].append(
        {"evaluatorId": "Custom.Code", "statistics": {}, "totalEvaluated": 3})

    svc._finish_from_result(run_id, "evaluators", result, workspace=MagicMock())

    run = get_run(run_id)
    assert run.status == "completed"
    assert run.error == "1 of 4 sessions failed during batch evaluation."
    assert run.session_failures["kind"] == "telemetry_incomplete"
    assert run.session_failures["sessions"][0]["session_id"] == "s2"
    assert len(reads) == 1


def test_a_clean_finish_stores_no_summary(monkeypatch):
    run_id = _evaluator_run(status="evaluating", batch_eval_id="run_c-1")
    reads = _stream(monkeypatch, [_record("s1", score=1.0)])

    svc._finish_from_result(run_id, "evaluators", _result(0, status="COMPLETED"),
                            workspace=MagicMock())

    assert get_run(run_id).session_failures is None
    assert reads == []


def test_an_unreadable_stream_still_records_the_counts(monkeypatch):
    run_id = _evaluator_run(status="evaluating", batch_eval_id="run_u-1")

    def boom(*_a, **_k):
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(svc.ac, "read_result_records", boom)

    svc._finish_from_result(run_id, "evaluators", _result(2), workspace=MagicMock())

    assert get_run(run_id).session_failures == {
        "total": 4, "failed": 2, "kind": "unknown", "sessions": []}


def _data(result):
    client = MagicMock()
    client.get_batch_evaluation.return_value = result
    return client


def test_reading_a_legacy_partial_runs_results_backfills_once(monkeypatch):
    run_id = _evaluator_run(status="completed", batch_eval_id="run_l-1",
                            error="1 of 4 sessions failed during batch evaluation.")
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: _data(_result(1)))
    _stream(monkeypatch, [_record("s3", error_type="LogEventMissingException", message=MISSING)])

    out = svc.run_results(get_run(run_id), workspace=MagicMock())

    assert out["available"] is True
    first = get_run(run_id).session_failures
    assert first["sessions"][0]["index"] == 3

    _stream(monkeypatch, [_record("s4", error_type="ThrottlingException", message="x")])
    svc.run_results(get_run(run_id), workspace=MagicMock())
    assert get_run(run_id).session_failures == first


def test_recheck_backfills_a_legacy_partial_run(monkeypatch):
    run_id = _evaluator_run(status="completed", batch_eval_id="run_r-1",
                            error="1 of 4 sessions failed during batch evaluation.")
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: _data(_result(1)))
    _stream(monkeypatch, [_record("s2", error_type="LogEventMissingException", message=MISSING)])

    run = svc.recheck_run(run_id, workspace=MagicMock())

    assert run.status == "completed"
    assert run.session_failures["kind"] == "telemetry_incomplete"
    assert run.scores == [{"evaluatorId": "Builtin.Correctness", "score": 0.5, "count": 4}]


@pytest.mark.parametrize("fields", [
    {"status": "completed", "error": None},
    {"status": "completed", "error": "1 of 4 sessions failed",
     "session_failures": {"total": 4, "failed": 1, "kind": "unknown", "sessions": []}},
])
def test_recheck_still_refuses_clean_or_summarized_completed_runs(monkeypatch, fields):
    run_id = _evaluator_run(batch_eval_id="run_n-1", **fields)
    client = _data(_result(1))
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: client)

    with pytest.raises(AppError) as err:
        svc.recheck_run(run_id, workspace=MagicMock())

    assert err.value.code == "run.not_recheckable"
    client.get_batch_evaluation.assert_not_called()


def test_recheck_never_puts_a_finished_run_back_to_polling(monkeypatch):
    run_id = _evaluator_run(status="completed", batch_eval_id="run_b-1",
                            error="1 of 4 sessions failed during batch evaluation.")
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: _data({"status": "IN_PROGRESS"}))

    with pytest.raises(AppError) as err:
        svc.recheck_run(run_id, workspace=MagicMock())

    assert err.value.code == "run.not_recheckable"
    run = get_run(run_id)
    assert run.status == "completed" and run.error.startswith("1 of 4")
