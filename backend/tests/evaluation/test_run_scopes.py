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


def test_run_carries_task_name_and_description(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="named-agent")
    db.close()
    stub_environment(monkeypatch)
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "lookback_hours": 6, "name": "daily sample",
        "description": "V2 task", "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 201
    body = res.json()
    assert (body["name"], body["description"]) == ("daily sample", "V2 task")
    assert body["updated_at"]
    listed = client.get("/api/eval/runs").json()
    rows = listed["runs"] if isinstance(listed, dict) else listed
    assert any(r["id"] == body["id"] and r["name"] == "daily sample" for r in rows)
    wait_terminal(client, body["id"])


def test_log_stream_sessions_run_records_logs_scope(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="logs-agent")
    db.close()
    data, calls = stub_environment(monkeypatch)
    sessions = ["s1" + "x" * 32, "s2" + "y" * 32]
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "session_ids": sessions, "session_source": "logs",
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert run["dataset_name"] == "logs:2" and run["dataset_id"] is None
    assert calls["n"] == 0  # the picked sessions are evaluated as they are
    fc = data.start_batch_evaluation.call_args.kwargs["dataSourceConfig"]["cloudWatchLogs"]
    assert fc["filterConfig"]["sessionIds"] == sessions


def test_session_source_needs_session_ids(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="logs-noscope-agent")
    db.close()
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "lookback_hours": 6, "session_source": "logs",
    })
    assert res.status_code == 422
    assert res.json()["code"] == "run.session_source_scope"


class _Logs:
    """describe_log_groups for the log-source existence check."""

    def __init__(self, existing):
        self.existing = set(existing)

    def describe_log_groups(self, logGroupNamePrefix, limit):
        return {"logGroups": [{"logGroupName": g} for g in self.existing
                              if g.startswith(logGroupNamePrefix)]}


LOG_SOURCE = {"service_name": "clawbot-agent-runtime",
              "log_group_names": ["aws/spans", "/ecs/clawbot"]}


def test_log_source_run_evaluates_cloudwatch_without_an_agent(client, monkeypatch):
    from app.services import aws_clients

    data, calls = stub_environment(monkeypatch)
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: _Logs(LOG_SOURCE["log_group_names"]))
    res = client.post("/api/eval/runs", json={
        "log_source": LOG_SOURCE, "lookback_hours": 24, "name": "off-runtime",
        "evaluators": ["Builtin.Helpfulness"], "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert (body["agent_id"], body["agent_name"]) == ("", "clawbot-agent-runtime")
    assert body["log_source"] == LOG_SOURCE
    run = wait_terminal(client, body["id"])
    assert run["status"] == "completed", run.get("error")
    assert calls["n"] == 0
    cw = data.start_batch_evaluation.call_args.kwargs["dataSourceConfig"]["cloudWatchLogs"]
    assert cw["serviceNames"] == ["clawbot-agent-runtime"]
    assert cw["logGroupNames"] == ["aws/spans", "/ecs/clawbot"]
    assert cw["filterConfig"]["timeRange"]["endTime"] - cw["filterConfig"]["timeRange"][
        "startTime"] == timedelta(hours=24)


def test_log_source_insights_over_picked_sessions(client, monkeypatch):
    from app.services import aws_clients

    data, _ = stub_environment(monkeypatch)
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: _Logs(LOG_SOURCE["log_group_names"]))
    sessions = [f"01KK{i}#feishu#oc_1" for i in range(3)]
    res = client.post("/api/eval/runs", json={
        "log_source": LOG_SOURCE, "mode": "insights", "session_ids": sessions,
        "session_source": "logs", "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    assert res.json()["dataset_name"] == "logs:3"
    wait_terminal(client, res.json()["id"])
    cw = data.start_batch_evaluation.call_args.kwargs["dataSourceConfig"]["cloudWatchLogs"]
    assert cw["filterConfig"]["sessionIds"] == sessions
    assert cw["logGroupNames"] == ["aws/spans", "/ecs/clawbot"]


def test_log_source_target_and_scope_rules(client, monkeypatch):
    from app.services import aws_clients

    db = SessionLocal()
    agent = make_agent(db, name="both-targets-agent")
    db.close()
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "log_source": LOG_SOURCE, "lookback_hours": 6})
    assert res.status_code == 422 and res.json()["code"] == "run.target_required"
    res = client.post("/api/eval/runs", json={"lookback_hours": 6})
    assert res.status_code == 422 and res.json()["code"] == "run.target_required"
    res = client.post("/api/eval/runs", json={"log_source": LOG_SOURCE, "dataset_id": "ds-1"})
    assert res.status_code == 422 and res.json()["code"] == "run.log_source_scope"
    res = client.post("/api/eval/runs", json={
        "log_source": {**LOG_SOURCE, "service_name": 'bad "name"'}, "lookback_hours": 6})
    assert res.status_code == 422

    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: _Logs(["aws/spans"]))
    res = client.post("/api/eval/runs", json={"log_source": LOG_SOURCE, "lookback_hours": 6})
    assert res.status_code == 422
    assert res.json()["code"] == "run.log_group_missing"
    assert res.json()["detail"]["missing"] == ["/ecs/clawbot"]
