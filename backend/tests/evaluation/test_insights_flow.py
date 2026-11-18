"""Modern batch findings and compatibility with historical results."""

import time

from botocore.exceptions import ClientError

from app.core.db import SessionLocal
from app.evaluation import agentcore_eval as ac
from app.evaluation import findings
from tests.evaluation.test_runs_flow import make_agent, stub_environment


def test_insights_run_over_sessions(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="insights-agent")
    db.close()
    data, _ = stub_environment(monkeypatch)
    monkeypatch.setattr(findings, "data_client", lambda _: data)
    pattern = {
        "type": "FAILURE", "name": "Tool misuse", "affectedSessionCount": 1,
        "rootCauses": [{
            "name": "Wrong tool", "suggestedFix": "Clarify tool descriptions",
            "sampleSessions": [{"sessionId": "s1" + "x" * 32}],
        }],
    }
    data.get_batch_evaluation.return_value.update(insightResults=[pattern])
    data.get_batch_evaluation.return_value["evaluationResults"]["evaluatorSummaries"] = [
        {"evaluatorId": ac.FAILURE_ANALYSIS, "statistics": {"averageScore": 0.2}},
        {"evaluatorId": "Builtin.GoalSuccessRate", "statistics": {"averageScore": 0.8}},
    ]
    evaluator_ids = [ac.FAILURE_ANALYSIS, "Builtin.GoalSuccessRate"]

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id,
        "evaluators": evaluator_ids,
        "session_ids": ["s1" + "x" * 32],
        "wait_seconds": 0,
    })
    assert res.status_code == 201
    run_id = res.json()["id"]
    for _ in range(50):
        run = client.get(f"/api/eval/runs/{run_id}").json()
        if run["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert run["status"] == "completed", run.get("error")
    kwargs = data.start_batch_evaluation.call_args.kwargs
    assert "insights" not in kwargs
    assert kwargs["evaluators"] == [{"evaluatorId": eid} for eid in evaluator_ids]
    assert ac.evaluator_polarity(ac.FAILURE_ANALYSIS) == -1
    assert {item["evaluatorId"]: item["score"] for item in run["scores"]} == {
        ac.FAILURE_ANALYSIS: 0.2, "Builtin.GoalSuccessRate": 0.8,
    }

    url = f"/api/eval/runs/{run_id}/findings"
    ready = client.get(url).json()
    assert ready == {
        "source": "batch", "state": "ready", "aws_status": "COMPLETED",
        "findings": [pattern], "diagnostics": [],
    }
    data.get_batch_evaluation.return_value.update(insightResults=[])
    assert client.get(url).json()["state"] == "ready"
    data.get_batch_evaluation.return_value.pop("insightResults")
    data.get_batch_evaluation.return_value.update(
        status="COMPLETED_WITH_ERRORS", errorDetails=["Insight generation failed"],
    )
    broken = client.get(url).json()
    assert broken["state"] == "error" and broken["diagnostics"] == ["Insight generation failed"]
    data.get_batch_evaluation.return_value.update(insightResults=[{
        "type": "FAILURE", "name": "No root cause",
        "sampleSessions": [{"sessionId": "s1" + "x" * 32}],
    }])
    partial = client.get(url).json()
    assert partial["state"] == "partial" and partial["findings"][0]["sampleSessions"]

    data.get_batch_evaluation.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "AWS-only detail"}},
        "GetBatchEvaluation",
    )
    unreadable = client.get(url).json()
    assert unreadable["state"] == "unavailable" and unreadable["findings"] == []
    assert unreadable["diagnostics"] == []


def test_parse_insights_shapes():
    result = {
        "failureAnalysisResult": {"failures": [{"category": "A"}]},
        "userIntentResult": {"userIntents": [{"intent": "B"}]},
        "executionSummaryResult": {"executionSummaries": [{"approachTaken": "C"}]},
    }
    parsed = ac.parse_insights(result)
    assert parsed["failures"] == [{"category": "A"}]
    assert parsed["userIntents"] == [{"intent": "B"}]
    assert parsed["executionSummaries"] == [{"approachTaken": "C"}]
