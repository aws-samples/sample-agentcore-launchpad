"""Runs flow — adapted from agentcore_eva_opt tests/test_runs_flow.py.

Stubbed clients drive the full pipeline: invoking → waiting → evaluating →
completed with parsed scores.
"""

from unittest.mock import ANY, MagicMock

import app.evaluation.service as svc
from app.core.db import SessionLocal
from app.evaluation.models import EvalDataset
from app.models.ledger import Agent


def make_agent(db, name="eval-agent", method="zip_runtime") -> Agent:
    agent = Agent(
        name=name, method=method, status="active",
        arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/rt-1",
        resource_id="rt-1", spec={"name": name},
    )
    db.add(agent)
    db.commit()
    return agent


def stub_environment(monkeypatch, batch_status="COMPLETED"):
    data = MagicMock()
    call_count = {"n": 0}

    def invoke_runtime_text(
        client, arn, prompt, session_id=None, actor_id="default", runtime_user_id=None
    ):
        call_count["n"] += 1
        return {"text": "42", "session_id": f"sess-{call_count['n']:03d}" + "x" * 30}

    monkeypatch.setattr(svc.rt, "invoke_runtime_text", invoke_runtime_text)
    monkeypatch.setattr(
        svc.rt, "get_runtime",
        lambda client, rid: {"agentRuntimeName": "eval_agent_abc123"},
    )
    monkeypatch.setattr(svc, "control_client", lambda: MagicMock())
    monkeypatch.setattr(svc, "data_client", lambda: data)
    telemetry_wait = MagicMock()
    monkeypatch.setattr(svc, "_wait_for_fresh_telemetry", telemetry_wait)
    data.start_batch_evaluation.return_value = {"batchEvaluationId": "be-123"}
    data.get_batch_evaluation.return_value = {
        "status": batch_status,
        "evaluationResults": {
            "evaluatorSummaries": [
                {"evaluatorId": "Builtin.Correctness",
                 "statistics": {"averageScore": 0.91}},
                {"evaluatorId": "Builtin.Helpfulness",
                 "statistics": {"averageScore": 0.83}},
            ]
        },
        "failureAnalysisResult": {"failures": [
            {"category": "Tool misuse", "percentage": 42,
             "subCategories": []},
        ]},
    }
    return data, call_count


def test_active_run_completes_with_scores(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    dataset = EvalDataset(name="mini", items=[{"prompt": "2+2?"}, {"prompt": "3+3?"},
                                              {"prompt": "4+4?"}])
    db.add(dataset)
    db.commit()
    data, calls = stub_environment(monkeypatch)
    telemetry_wait = svc._wait_for_fresh_telemetry

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": dataset.id,
        "evaluators": ["Builtin.Correctness", "Builtin.Helpfulness"],
        "wait_seconds": 0,
    })
    assert res.status_code == 201
    run_id = res.json()["id"]

    import time
    for _ in range(50):
        run = client.get(f"/api/eval/runs/{run_id}").json()
        if run["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert run["status"] == "completed", run.get("error")
    assert calls["n"] == 3  # one session per dataset item
    assert len(run["session_ids"]) == 3
    telemetry_wait.assert_called_once_with(
        session_id=run["session_ids"][-1],
        content_log_group="/aws/bedrock-agentcore/runtimes/rt-1-DEFAULT",
        start_time_ms=ANY,
        stability_seconds=0,
    )
    assert run["batch_eval_id"] == "be-123"
    assert {s["evaluatorId"]: s["score"] for s in run["scores"]} == {
        "Builtin.Correctness": 0.91, "Builtin.Helpfulness": 0.83,
    }
    # batch eval scoped to exactly this run's sessions
    kwargs = data.start_batch_evaluation.call_args.kwargs
    assert kwargs["dataSourceConfig"]["cloudWatchLogs"]["filterConfig"]["sessionIds"] == run[
        "session_ids"
    ]
    assert kwargs["dataSourceConfig"]["cloudWatchLogs"]["serviceNames"] == [
        "eval_agent_abc123.DEFAULT"
    ]
    db.close()


def test_run_failure_recorded(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="eval-agent-fail")
    dataset = EvalDataset(name="mini2", items=[{"prompt": "x"}])
    db.add(dataset)
    db.commit()
    stub_environment(monkeypatch, batch_status="FAILED")

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": dataset.id, "wait_seconds": 0,
    })
    run_id = res.json()["id"]
    import time
    for _ in range(50):
        run = client.get(f"/api/eval/runs/{run_id}").json()
        if run["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert run["status"] == "failed"
    assert "FAILED" in run["error"]
    db.close()


def test_telemetry_timeout_fails_without_starting_batch(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="eval-agent-telemetry-timeout")
    dataset = EvalDataset(name="telemetry-timeout", items=[{"prompt": "x"}])
    db.add(dataset)
    db.commit()
    data, _ = stub_environment(monkeypatch)
    telemetry_wait = svc._wait_for_fresh_telemetry
    telemetry_wait.side_effect = svc.telemetry.TelemetryReadinessTimeout(
        "evaluation telemetry did not become ready"
    )

    res = client.post(
        "/api/eval/runs",
        json={"agent_id": agent.id, "dataset_id": dataset.id, "wait_seconds": 0},
    )
    run_id = res.json()["id"]
    import time

    for _ in range(50):
        run = client.get(f"/api/eval/runs/{run_id}").json()
        if run["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert run["status"] == "failed"
    assert "telemetry did not become ready" in run["error"]
    data.start_batch_evaluation.assert_not_called()
    db.close()


class StubLogs:
    """describe_log_groups stub — harness backing-runtime group discovery."""

    def __init__(self, groups):
        self.groups = groups
        self.prefixes: list[str] = []

    def describe_log_groups(self, logGroupNamePrefix):
        self.prefixes.append(logGroupNamePrefix)
        return {"logGroups": [g for g in self.groups
                              if g["logGroupName"].startswith(logGroupNamePrefix)]}


def _stub_harness_logs(monkeypatch, groups):
    logs = StubLogs(groups)
    monkeypatch.setattr(svc.boto3, "client", lambda *a, **k: logs)
    return logs


def test_harness_run_completes(client, monkeypatch):
    """Harness agents are eval-supported since 07-13: service name derives from
    the harnessId base, the content-log group is prefix-discovered (backing
    runtime id ≠ harnessId), and invoking goes through InvokeHarness."""
    db = SessionLocal()
    agent = make_agent(db, name="harness-agent", method="harness")
    agent.resource_id = "harness_agent-Flr7ibmASq"
    agent.arn = "arn:aws:bedrock-agentcore:us-west-2:1:harness/harness_agent-Flr7ibmASq"
    dataset = EvalDataset(name="mini3", items=[{"prompt": "x"}, {"prompt": "y"}])
    db.add(dataset)
    db.commit()
    data, calls = stub_environment(monkeypatch)
    telemetry_wait = svc._wait_for_fresh_telemetry
    logs = _stub_harness_logs(monkeypatch, [
        {"logGroupName": "/aws/bedrock-agentcore/runtimes/harness_harness_agent-OLD111-DEFAULT",
         "creationTime": 1},
        {"logGroupName": "/aws/bedrock-agentcore/runtimes/harness_harness_agent-GIRksPB4NZ-DEFAULT",
         "creationTime": 2},
    ])
    harness_calls = {"n": 0}

    def invoke_harness_text(client_, arn, prompt, session_id=None, actor_id="default"):
        harness_calls["n"] += 1
        return {"text": "ok", "session_id": f"hsess-{harness_calls['n']:03d}" + "x" * 30}

    monkeypatch.setattr(svc.hc, "invoke_harness_text", invoke_harness_text)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": dataset.id,
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 201
    run_id = res.json()["id"]
    import time
    for _ in range(50):
        run = client.get(f"/api/eval/runs/{run_id}").json()
        if run["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert run["status"] == "completed", run.get("error")
    assert harness_calls["n"] == 2 and calls["n"] == 0  # InvokeHarness, not runtime
    telemetry_wait.assert_called_once_with(
        session_id=run["session_ids"][-1],
        content_log_group=(
            "/aws/bedrock-agentcore/runtimes/"
            "harness_harness_agent-GIRksPB4NZ-DEFAULT"
        ),
        start_time_ms=ANY,
        stability_seconds=0,
    )
    assert logs.prefixes == ["/aws/bedrock-agentcore/runtimes/harness_harness_agent-"]
    cw = data.start_batch_evaluation.call_args.kwargs["dataSourceConfig"]["cloudWatchLogs"]
    assert cw["serviceNames"] == ["harness_harness_agent.DEFAULT"]
    # newest backing-runtime group wins over the stale one
    assert cw["logGroupNames"] == [
        "aws/spans",
        "/aws/bedrock-agentcore/runtimes/harness_harness_agent-GIRksPB4NZ-DEFAULT",
    ]
    db.close()


def test_harness_without_telemetry_group_rejected(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="harness-cold", method="harness")
    agent.resource_id = "harness_cold-Abc123XYZ0"
    db.commit()
    dataset = EvalDataset(name="mini4", items=[{"prompt": "x"}])
    db.add(dataset)
    db.commit()
    _stub_harness_logs(monkeypatch, [])
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": dataset.id,
    })
    assert res.status_code == 400
    assert res.json()["code"] == "eval.harness_no_telemetry"
    db.close()


def test_existing_sessions_skip_fresh_telemetry_wait(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="existing-session-agent")
    stub_environment(monkeypatch)
    telemetry_wait = svc._wait_for_fresh_telemetry
    session_id = "existing-session-" + "x" * 24

    res = client.post(
        "/api/eval/runs",
        json={
            "agent_id": agent.id,
            "session_ids": [session_id],
            "evaluators": ["Builtin.Correctness"],
            "wait_seconds": 180,
        },
    )
    assert res.status_code == 201
    run_id = res.json()["id"]
    import time

    for _ in range(50):
        run = client.get(f"/api/eval/runs/{run_id}").json()
        if run["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert run["status"] == "completed", run.get("error")
    telemetry_wait.assert_not_called()
    db.close()
