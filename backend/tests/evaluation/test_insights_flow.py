"""Insights flow (N12 E1-4): an ``insights`` job over an earlier run's sessions, the three
trees read from the worker's ``insights.json`` artefact onto ``EvalRun.insights``."""

import app.evaluation.service as svc
from app.core.db import SessionLocal
from app.evaluation.jobs import JobDone
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeExecutor, FakeJudge
from tests.evaluation.test_resume import get_run, make_run
from tests.evaluation.test_runs_flow import make_agent, stub_environment, wait_terminal


def test_insights_run_over_sessions(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="insights-agent")
    db.close()
    judge = FakeJudge(text='{"errors": []}', by_tool={"SessionSummary": {
        "intent_label": "Leave balance", "intent": "asks for PTO left",
        "summary_label": "HR lookup", "summary": "looked up HR"}})
    env, calls = stub_environment(monkeypatch, judge=judge)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id,
        "mode": "insights",
        "session_ids": ["s1" + "x" * 32, "s2" + "x" * 32],
        "wait_seconds": 0,
    })
    assert res.status_code == 201
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert calls["n"] == 0  # existing sessions: nothing to replay
    (job,) = env.submitted
    assert job.job_type == "insights"
    assert job.spec["kinds"] == ["Builtin.Insight.FailureAnalysis", "Builtin.Insight.UserIntent",
                                 "Builtin.Insight.ExecutionSummary"]
    assert run["insights"]["failures"] == []
    intents = run["insights"]["userIntents"]
    assert intents[0]["name"] == "Leave balance" and intents[0]["affectedSessionCount"] == 2
    assert {s["sessionId"] for s in intents[0]["affectedSessions"]} == {
        "s1" + "x" * 32, "s2" + "x" * 32}
    assert run["insights"]["executionSummaries"][0]["name"] == "HR lookup"
    assert run["scores"] == [] and run["evaluators"] == job.spec["kinds"]
    # the artefact is what the row carries
    assert env.executor.artifact(ws_ctx(), run["id"], "insights.json") == run["insights"]


def test_finish_merges_the_shards_insight_trees():
    run_id = make_run(status="evaluating", batch_eval_id="big")
    fake = FakeExecutor()
    dones = [
        JobDone(state="COMPLETED", summary={"insights": {
            "failures": [{"category": "A"}], "userIntents": [{"name": "x"}]}}),
        JobDone(state="COMPLETED", summary={"insights": {
            "failures": [{"category": "B"}], "executionSummaries": [{"name": "y"}]}}),
    ]
    svc._finish(run_id, "insights", dones, workspace=ws_ctx(), executor=fake,
                job_ids=["big-s0", "big-s1"])
    run = get_run(run_id)
    assert run.status == "completed"
    assert run.insights == {"failures": [{"category": "A"}, {"category": "B"}],
                            "userIntents": [{"name": "x"}], "executionSummaries": [{"name": "y"}]}
