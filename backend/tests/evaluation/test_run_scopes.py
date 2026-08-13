"""Run scopes — dataset XOR session_ids XOR lookback_hours (time window).

Window runs are passive: no runtime invocation, the batch evaluation is
scoped with filterConfig.timeRange over the agent's existing traffic.
"""

import time
from datetime import datetime, timedelta

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import service as svc
from app.evaluation.models import EvalRun
from tests.conftest import ws_ctx
from tests.evaluation.test_runs_flow import make_agent, stub_environment


def wait_terminal(client, run_id):
    for _ in range(50):
        run = client.get(f"/api/eval/runs/{run_id}").json()
        if run["status"] in ("completed", "failed"):
            return run
        time.sleep(0.1)
    return run


def test_lookback_run_passes_time_range_and_skips_invoke(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="window-agent")
    db.close()
    data, calls = stub_environment(monkeypatch)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "lookback_hours": 24,
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 201
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert calls["n"] == 0  # passive — no runtime invocations
    assert run["dataset_name"] == "window:24h"

    kwargs = data.start_batch_evaluation.call_args.kwargs
    fc = kwargs["dataSourceConfig"]["cloudWatchLogs"]["filterConfig"]
    assert "sessionIds" not in fc
    assert isinstance(fc["timeRange"]["startTime"], datetime)
    assert isinstance(fc["timeRange"]["endTime"], datetime)
    delta = fc["timeRange"]["endTime"] - fc["timeRange"]["startTime"]
    assert delta == timedelta(hours=24)


def test_scope_dataset_plus_lookback_rejected(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="xor-agent")
    db.close()
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": "ds-1", "lookback_hours": 24,
    })
    assert res.status_code == 422
    assert res.json()["code"] == "run.scope_required"


def test_scope_missing_rejected(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="noscope-agent")
    db.close()
    res = client.post("/api/eval/runs", json={"agent_id": agent.id})
    assert res.status_code == 422
    assert res.json()["code"] == "run.scope_required"


def test_invalid_insight_rejected(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="badinsight-agent")
    db.close()
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "mode": "insights",
        "session_ids": ["s1" + "x" * 32],
        "insights": ["Builtin.Insight.Bogus"],
    })
    assert res.status_code == 422
    assert res.json()["code"] == "run.invalid_insight"


def test_insights_subset_only_selected_ids(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="subset-agent")
    db.close()
    data, calls = stub_environment(monkeypatch)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "mode": "insights", "lookback_hours": 6,
        "insights": ["Builtin.Insight.UserIntent"], "wait_seconds": 0,
    })
    assert res.status_code == 201
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert calls["n"] == 0

    kwargs = data.start_batch_evaluation.call_args.kwargs
    assert kwargs["insights"] == [{"insightId": "Builtin.Insight.UserIntent"}]
    assert "evaluators" not in kwargs
    fc = kwargs["dataSourceConfig"]["cloudWatchLogs"]["filterConfig"]
    assert fc["timeRange"]["endTime"] - fc["timeRange"]["startTime"] == timedelta(hours=6)


def test_session_metadata_passthrough(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="meta-agent")
    run = EvalRun(
        workspace_id=DEFAULT_WORKSPACE_ID,
        agent_id=agent.id, agent_name=agent.name, mode="evaluators",
                  evaluators=["Builtin.Correctness"], status="queued")
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()
    data, _ = stub_environment(monkeypatch)

    metadata = [{"sessionId": "s1" + "x" * 32,
                 "groundTruth": {"expectedResponse": "42"}}]
    svc.execute_run(
        run_id,
        workspace=ws_ctx(),
        agent_arn=agent.arn, method="zip_runtime", service_name="svc.DEFAULT", log_group="/lg",
        items=[], evaluators=["Builtin.Correctness"], mode="evaluators",
        wait_seconds=0, existing_session_ids=["s1" + "x" * 32],
        session_metadata=metadata,
    )
    kwargs = data.start_batch_evaluation.call_args.kwargs
    assert kwargs["evaluationMetadata"]["sessionMetadata"] == metadata
