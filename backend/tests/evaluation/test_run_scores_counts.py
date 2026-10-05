"""Run scores carry their judgement counts, and a code-based evaluator AWS
summarises without an average is scored from the batch's own results stream.

Live 2026-10-05 (dev run dec2706812cd): ``DjiKbToolBoundary`` judged 18 sessions,
``totalFailed`` 0, but its ``evaluatorSummaries`` entry had ``statistics: {}`` —
so the run's scores silently held 7 of its 8 evaluators."""

from unittest.mock import MagicMock

import app.evaluation.agentcore_eval as ac
import app.evaluation.service as svc
from tests.evaluation.test_resume import get_run, make_run

ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:evaluator/"
RESULT = {
    "status": "COMPLETED",
    "outputConfig": {"cloudWatchConfig": {"logGroupName": "g", "logStreamName": "s"}},
    "evaluationResults": {"evaluatorSummaries": [
        {"evaluatorId": "Builtin.Refusal", "statistics": {"averageScore": 0.26},
         "totalEvaluated": 19, "totalFailed": 0},
        {"evaluatorId": "KbToolBoundary-f56DD97uvp", "statistics": {},
         "totalEvaluated": 3, "totalFailed": 0},
        {"evaluatorId": "Builtin.Helpfulness", "statistics": {"averageScore": 0.7}},
    ]},
}


def _record(evaluator_id, score, name="KbToolBoundary"):
    return {"gen_ai.evaluation.name": name, "gen_ai.evaluation.score.value": score,
            "aws.bedrock_agentcore.evaluator.arn": ARN + evaluator_id}


RECORDS = [_record("KbToolBoundary-f56DD97uvp", 1.0), _record("KbToolBoundary-f56DD97uvp", 0.0),
           _record("KbToolBoundary-f56DD97uvp", 1.0),
           _record("Builtin.Refusal", 0.0, name="Builtin.Refusal"),
           {"gen_ai.evaluation.name": "KbToolBoundary", "error.message": "boom",
            "aws.bedrock_agentcore.evaluator.arn": ARN + "KbToolBoundary-f56DD97uvp"}]


def test_counts_ride_along_and_an_unsummarised_evaluator_is_read_from_the_stream():
    reads = []
    scores = ac.parse_eval_scores(RESULT, records_reader=lambda: reads.append(1) or RECORDS)

    assert scores == [
        {"evaluatorId": "Builtin.Refusal", "score": 0.26, "count": 19},
        {"evaluatorId": "KbToolBoundary-f56DD97uvp", "score": 0.67, "count": 3},
        {"evaluatorId": "Builtin.Helpfulness", "score": 0.7},
    ]
    assert reads == [1]


def test_the_stream_is_not_read_when_every_evaluator_has_an_average():
    result = {"evaluationResults": {"evaluatorSummaries": [RESULT["evaluationResults"][
        "evaluatorSummaries"][0]]}}

    def boom():
        raise AssertionError("no read needed")

    assert ac.parse_eval_scores(result, records_reader=boom) == [
        {"evaluatorId": "Builtin.Refusal", "score": 0.26, "count": 19}]


def test_an_unreadable_stream_leaves_the_evaluator_out():
    def broken():
        raise RuntimeError("AccessDenied")

    ids = [s["evaluatorId"] for s in ac.parse_eval_scores(RESULT, records_reader=broken)]
    assert ids == ["Builtin.Refusal", "Builtin.Helpfulness"]


def test_a_finished_run_records_the_code_evaluators_mean(monkeypatch):
    run_id = make_run(status="evaluating", batch_eval_id="run_code-1")
    seen = {}

    def read(logs, group, stream, **_kw):
        seen["location"] = (group, stream)
        return RECORDS

    monkeypatch.setattr(svc.ac, "read_result_records", read)
    svc._finish_from_result(run_id, "evaluators", RESULT, workspace=MagicMock())

    run = get_run(run_id)
    assert run.status == "completed" and seen["location"] == ("g", "s")
    assert {s["evaluatorId"]: s.get("count") for s in run.scores} == {
        "Builtin.Refusal": 19, "KbToolBoundary-f56DD97uvp": 3, "Builtin.Helpfulness": None}
