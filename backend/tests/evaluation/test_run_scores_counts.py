"""Run scores carry their judgement counts and come from the ingested ``eval_results``
rows (N12 E1-4) — the twin of the baseline's results-stream fallback: an evaluator's mean
is computed from its scored records; NOT_APPLICABLE and error records count for nothing."""

import app.evaluation.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import results_store
from app.evaluation.jobs import JobDone
from app.services.strands_eval.results import aggregate_scores
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeExecutor
from tests.evaluation.test_resume import get_run, make_run


def _record(sid, evaluator_id, score, **extra):
    return {"session_id": sid, "evaluator_id": evaluator_id, "score": score, **extra}


RECORDS = [
    _record("s1", "KbToolBoundary-f56DD97uvp", 1.0),
    _record("s2", "KbToolBoundary-f56DD97uvp", 0.0),
    _record("s3", "KbToolBoundary-f56DD97uvp", 1.0),
    _record("s1", "Builtin.Refusal", 0.0), _record("s2", "Builtin.Refusal", 0.52),
    _record("s1", "Builtin.TrajectoryInOrderMatch", None, label="NOT_APPLICABLE"),
    _record("s1", "Builtin.Helpfulness", None, error_type="RuntimeError", error_message="boom"),
]


def test_counts_ride_along_and_means_come_from_the_records():
    assert aggregate_scores(RECORDS) == [
        {"evaluatorId": "Builtin.Refusal", "score": 0.26, "count": 2, "n": 2, "unit": "turn"},
        # a custom row without a level counts as a session evaluator
        {"evaluatorId": "KbToolBoundary-f56DD97uvp", "score": 0.6667, "count": 3, "n": 3,
         "unit": "session"},
    ]


def test_tool_call_fan_out_counts_sessions_not_records():
    records = [_record("s1", "Builtin.ToolSelectionAccuracy", 1.0),
               _record("s1", "Builtin.ToolSelectionAccuracy", 0.0),
               _record("s2", "Builtin.ToolSelectionAccuracy", 1.0)]
    assert aggregate_scores(records) == [
        {"evaluatorId": "Builtin.ToolSelectionAccuracy", "score": 0.6667, "count": 2, "n": 3,
         "unit": "tool_call"}]


def test_a_finished_run_records_the_means_of_every_scored_evaluator():
    run_id = make_run(status="evaluating", batch_eval_id="run_code-1", mode="evaluators")
    with SessionLocal() as db:
        results_store.persist(db, DEFAULT_WORKSPACE_ID, source="run", owner_id=run_id,
                              records=RECORDS)
    svc._finish(run_id, "evaluators", [JobDone(state="COMPLETED", result_count=len(RECORDS))],
                workspace=ws_ctx(), executor=FakeExecutor(), job_ids=["run_code-1"])
    run = get_run(run_id)
    assert run.status == "completed"
    assert {s["evaluatorId"]: s["count"] for s in run.scores} == {
        "Builtin.Refusal": 2, "KbToolBoundary-f56DD97uvp": 3}
    assert "Builtin.Helpfulness" not in {s["evaluatorId"] for s in run.scores}  # only errors


def test_budget_stops_are_collected_from_the_jobs():
    run_id = make_run(status="evaluating", batch_eval_id="run_bs", mode="evaluators")
    svc._finish(run_id, "evaluators", [
        JobDone(state="COMPLETED", summary={"budget_stops": [{"scenario_id": "a"}]}),
        JobDone(state="COMPLETED", summary={"budget_stops": [{"scenario_id": "b"}]}),
    ], workspace=ws_ctx(), executor=FakeExecutor(), job_ids=["run_bs-s0", "run_bs-s1"])
    assert get_run(run_id).budget_stops == [{"scenario_id": "a"}, {"scenario_id": "b"}]


def test_scenario_failures_fold_the_skipped_personas_from_the_jobs():
    """Upstream #251 parity: ``eval_runs.scenario_failures`` lists the SIMULATED persona
    scenarios the worker skipped (``replay_failures[].simulated``), in upstream's wire shape
    (``retries`` a string, ``error`` = ``<code>: <message>`` bounded); a predefined scenario
    that failed to replay stays an N-SCN-bug1 errored scenario and is not listed."""
    run_id = make_run(status="evaluating", batch_eval_id="run_sf", mode="evaluators")
    svc._finish(run_id, "evaluators", [
        JobDone(state="COMPLETED", summary={"replay_failures": [
            {"session_id": "s1", "scenario_id": "p1", "code": "ReadTimeoutError",
             "attempts": 3, "error": "Read timeout on endpoint URL", "arm": None,
             "simulated": True},
            {"session_id": "s2", "scenario_id": "s2", "code": "ValueError", "attempts": 1,
             "error": "bad prompt", "arm": None},  # a predefined scenario: no key at all
        ]}),
        JobDone(state="COMPLETED", summary={"replay_failures": [
            {"session_id": "s3", "scenario_id": "p3", "code": "RuntimeError", "attempts": 1,
             "error": "x" * 400, "arm": "T1", "simulated": True},
        ]}),
    ], workspace=ws_ctx(), executor=FakeExecutor(), job_ids=["run_sf-s0", "run_sf-s1"])
    run = get_run(run_id)
    assert run.scenario_failures == [
        {"scenario_id": "p1", "retries": "2", "code": "ReadTimeoutError",
         "error": "ReadTimeoutError: Read timeout on endpoint URL"},
        {"scenario_id": "p3", "retries": "0", "code": "RuntimeError",
         "error": ("RuntimeError: " + "x" * 400)[:svc.SCENARIO_ERROR_CHARS]},
    ]


def test_a_run_without_skipped_personas_records_no_scenario_failures():
    run_id = make_run(status="evaluating", batch_eval_id="run_nsf", mode="evaluators")
    svc._finish(run_id, "evaluators", [JobDone(state="COMPLETED", summary={
        "replay_failures": [{"session_id": "s2", "scenario_id": "s2", "code": "ValueError",
                             "attempts": 1, "error": "bad", "arm": None}],
    })], workspace=ws_ctx(), executor=FakeExecutor(), job_ids=["run_nsf"])
    assert get_run(run_id).scenario_failures is None
