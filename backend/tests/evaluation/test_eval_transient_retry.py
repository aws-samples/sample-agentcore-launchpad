"""A transient upstream error replays the scenario in a fresh session instead of failing the run."""

from unittest.mock import MagicMock

from botocore.exceptions import EventStreamError

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import service as evaluation
from app.evaluation.models import EvalRun
from tests.conftest import ws_ctx
from tests.test_harness_execution import ARN, Stream, stop, text


def _server_error():
    return EventStreamError(
        {"Error": {"Code": "runtimeClientError",
                   "Message": "The server had an error while processing your request."}},
        "InvokeHarness",
    )


def _run_row() -> str:
    with SessionLocal() as db:
        row = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id="agent-harness",
                      agent_name="test-harness", status="queued", evaluators=[])
        db.add(row)
        db.commit()
        return row.id


def _execute(monkeypatch, streams):
    client = MagicMock()
    client.invoke_harness.side_effect = [{"stream": s} for s in streams]
    client.start_batch_evaluation.return_value = {"batchEvaluationId": "batch-1"}
    monkeypatch.setattr(evaluation, "data_client", lambda _ws: client)
    monkeypatch.setattr(evaluation, "_wait_for_fresh_telemetry", lambda **_kw: None)
    monkeypatch.setattr(evaluation, "_poll_batch", lambda *_a, **_kw: {})
    finished = []
    monkeypatch.setattr(evaluation, "_finish_from_result",
                        lambda run_id, *_a, **_kw: finished.append(run_id))
    run_id = _run_row()
    evaluation.execute_run(
        run_id, workspace=ws_ctx(), agent_arn=ARN, method="harness",
        service_name="harness_test.DEFAULT", log_group="/test",
        items=[{"scenario_id": "first", "turns": [{"input": "first"}]},
               {"scenario_id": "second", "turns": [{"input": "second"}]}],
        evaluators=["Builtin.Correctness"], mode="evaluators", wait_seconds=0,
    )
    sessions = [c.kwargs["runtimeSessionId"] for c in client.invoke_harness.call_args_list]
    with SessionLocal() as db:
        return db.get(EvalRun, run_id), sessions, finished


def ok(value="answer"):
    return Stream([text(value), stop("end_turn")])


def test_a_transient_server_error_replays_the_scenario_in_a_fresh_session(monkeypatch):
    row, sessions, finished = _execute(
        monkeypatch, [ok(), Stream([text("par")], read_error=_server_error()), ok()])

    assert len(sessions) == 3 and sessions[1] != sessions[2]
    assert row.session_ids == [sessions[0], sessions[2]]  # the failed attempt is dropped
    assert row.batch_eval_id == "batch-1" and finished == [row.id]


def test_a_scenario_that_keeps_failing_fails_the_run_naming_it(monkeypatch):
    streams = [ok()] + [Stream([], read_error=_server_error())
                        for _ in range(evaluation.TRANSIENT_SCENARIO_RETRIES + 1)]
    row, sessions, finished = _execute(monkeypatch, streams)

    assert len(sessions) == 1 + evaluation.TRANSIENT_SCENARIO_RETRIES + 1
    assert row.status == "failed" and finished == []
    assert "runtimeClientError" in row.error and "scenario_id=second" in row.error
    assert f"retried={evaluation.TRANSIENT_SCENARIO_RETRIES}" in row.error


def test_a_budget_stop_is_scored_as_it_stands_not_retried(monkeypatch):
    row, sessions, finished = _execute(
        monkeypatch, [ok(), Stream([text("p"), stop("max_tokens")])])

    assert len(sessions) == 2 and row.session_ids == sessions  # the stopped session is kept
    assert row.batch_eval_id == "batch-1" and finished == [row.id] and row.error is None
    assert row.budget_stops == [{"scenario_id": "second", "session_id": sessions[1],
                                 "code": "harness.execution_limit", "stop_reason": "max_tokens"}]


def test_transient_classification():
    from botocore.exceptions import ClientError

    from app.core.errors import AppError

    assert evaluation.transient_invoke_error(_server_error())
    assert evaluation.transient_invoke_error(RuntimeError("internal server error: boom"))
    assert evaluation.transient_invoke_error(ClientError(
        {"Error": {"Code": "Other"}, "ResponseMetadata": {"HTTPStatusCode": 503}}, "Invoke"))
    assert not evaluation.transient_invoke_error(ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "Invoke"))
    assert evaluation.transient_invoke_error(AppError("harness.execution_timeout", "x"))
    assert not evaluation.transient_invoke_error(AppError("harness.execution_limit", "x"))
    # a loop cut short right after a tool step is replayed; an empty end_turn is not
    assert evaluation.transient_invoke_error(AppError(
        "harness.incomplete_response", "x", {"stop_reason": "tool_result"}))
    assert not evaluation.transient_invoke_error(AppError(
        "harness.incomplete_response", "x", {"stop_reason": "end_turn"}))
    assert not evaluation.transient_invoke_error(ValueError("bad"))


def test_a_loop_cut_short_after_a_tool_step_is_replayed(monkeypatch):
    cut = Stream([text("searching"), stop("tool_result")])
    row, sessions, finished = _execute(monkeypatch, [ok(), cut, ok()])

    assert len(sessions) == 3 and row.batch_eval_id == "batch-1" and finished == [row.id]


def test_a_timed_out_scenario_is_replayed_once(monkeypatch):
    timeout = Stream([stop("timeout_exceeded")])
    row, sessions, finished = _execute(monkeypatch, [ok(), timeout, ok()])

    assert len(sessions) == 3 and sessions[1] != sessions[2]
    assert row.batch_eval_id == "batch-1" and finished == [row.id]


def test_a_scenario_that_times_out_again_is_scored_as_a_budget_stop(monkeypatch):
    streams = [ok()] + [Stream([stop("timeout_exceeded")])
                        for _ in range(evaluation.TIMEOUT_SCENARIO_RETRIES + 1)]
    row, sessions, finished = _execute(monkeypatch, streams)

    assert len(sessions) == 1 + evaluation.TIMEOUT_SCENARIO_RETRIES + 1
    assert row.session_ids == [sessions[0], sessions[-1]]  # the last replay is scored
    assert finished == [row.id]
    assert [(b["scenario_id"], b["code"]) for b in row.budget_stops] == [
        ("second", "harness.execution_timeout")]


def test_an_ordinary_run_records_no_budget_stops(monkeypatch):
    row, _, _ = _execute(monkeypatch, [ok(), ok()])

    assert row.budget_stops is None
