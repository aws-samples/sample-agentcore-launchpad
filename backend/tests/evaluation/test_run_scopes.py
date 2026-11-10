"""Run scopes — dataset XOR session_ids XOR lookback_hours (time window) (N12 E1-4).

Window runs are passive: the backend resolves the window to explicit session ids (the
worker evaluates sessions, never a time range) and nothing is replayed.
"""

from datetime import timedelta

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import service as svc
from app.evaluation.models import EvalRun
from tests.conftest import ws_ctx
from tests.evaluation.test_runs_flow import make_agent, stub_environment, wait_terminal


def test_lookback_run_resolves_the_window_to_sessions_and_skips_replay(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="window-agent")
    db.close()
    env, calls = stub_environment(monkeypatch)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "lookback_hours": 24,
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 201
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert calls["n"] == 0  # passive — no runtime invocations
    assert run["dataset_name"] == "window:24h"
    (discovery,) = env.discovered
    assert discovery["service_name"] == "eval_agent_abc123.DEFAULT"
    assert discovery["log_groups"] == ["aws/spans", "/aws/bedrock-agentcore/runtimes/rt-1-DEFAULT"]
    window = discovery["time_range"]
    assert window["endTime"] - window["startTime"] == timedelta(hours=24)
    assert run["session_ids"] == ["window-session-1" + "x" * 30, "window-session-2" + "x" * 30]
    (job,) = env.submitted
    assert job.spec["replay"] is False and len(job.spec["sessions"]) == 2
    assert env.telemetry_waits == []


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


def test_insights_subset_only_selected_kinds(client, monkeypatch):
    from tests.evaluation.fakes import FakeJudge

    db = SessionLocal()
    agent = make_agent(db, name="subset-agent")
    db.close()
    judge = FakeJudge(by_tool={"SessionSummary": {
        "intent_label": "Leave", "intent": "x", "summary_label": "Lookup", "summary": "y"}})
    env, calls = stub_environment(monkeypatch, judge=judge)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "mode": "insights", "lookback_hours": 6,
        "insights": ["Builtin.Insight.UserIntent"], "wait_seconds": 0,
    })
    assert res.status_code == 201
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert calls["n"] == 0
    (job,) = env.submitted
    assert job.job_type == "insights" and job.spec["kinds"] == ["Builtin.Insight.UserIntent"]
    assert "evaluator_defs" not in job.spec or job.spec["evaluator_defs"] == []
    assert set(run["insights"]) == {"userIntents"}
    assert run["insights"]["userIntents"][0]["name"] == "Leave"
    assert run["evaluators"] == ["Builtin.Insight.UserIntent"]  # the applied insight ids
    assert env.discovered[0]["time_range"]["endTime"] - env.discovered[0]["time_range"][
        "startTime"] == timedelta(hours=6)


def test_dataset_ground_truth_rides_in_the_job_spec(client, monkeypatch):
    """The baseline's sessionMetadata twin: ground truth keyed by the minted session id."""
    db = SessionLocal()
    agent = make_agent(db, name="meta-agent")
    run = EvalRun(
        workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, agent_name=agent.name,
        mode="evaluators", evaluators=["Builtin.Correctness"], status="queued")
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()
    env, calls = stub_environment(monkeypatch)
    items = [{"scenario_id": "refund", "turns": [{"input": "q", "expected_response": "42"}],
              "assertions": ["says 42"], "expected_trajectory": ["lookup"]},
             {"prompt": "plain"}]
    plan = svc.plan_run(
        run_id, workspace=ws_ctx(id=DEFAULT_WORKSPACE_ID), agent_arn=agent.arn,
        agent_id=agent.id, protocol="http", service_name="svc.DEFAULT", log_group="/lg",
        log_groups=["aws/spans", "/lg"], items=items, evaluators=["Builtin.Correctness"],
        mode="evaluators", wait_seconds=0, existing_session_ids=None, time_range=None,
        insights=None, actor_model_id=None, runtime_user_id="user-1",
    )
    assert len(plan.sessions) == 2 and plan.replay is True
    refund_sid = plan.sessions[0]["session_id"]
    assert plan.ground_truth == {refund_sid: {
        "scenario_id": "refund", "input": "q", "expected_response": "42",
        "assertions": ["says 42"], "expected_trajectory": ["lookup"],
        "turns": [{"input": "q", "expected_response": "42"}]},
        plan.sessions[1]["session_id"]: {
        "scenario_id": "item_2", "input": "plain", "expected_response": None,
        # N17-obs376: no expected_trajectory field = no expectation (None), not [] (= "no
        # tools expected")
        "assertions": [], "expected_trajectory": None,
        "turns": [{"input": "plain", "expected_response": None}]}}
    spec = plan.spec()
    assert spec["ground_truth"][refund_sid]["expected_response"] == "42"
    assert spec["target"] == {"runtime_arn": agent.arn, "protocol": "http",
                              "runtime_user_id": "user-1"}
    assert spec["sessions"][0]["prompts"] == ["q"] and spec["sessions"][1]["prompts"] == ["plain"]
    assert spec["wait"]["content_log_group"] == "/lg" and spec["wait"]["seconds"] == 0
    assert spec["judge_model_id"] == "global.zai.glm-5.3"


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
    env, calls = stub_environment(monkeypatch)
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
    assert [s["session_id"] for s in env.submitted[0].spec["sessions"]] == sessions


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

    env, calls = stub_environment(monkeypatch)
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
    (discovery,) = env.discovered
    assert discovery["service_name"] == "clawbot-agent-runtime"
    assert discovery["log_groups"] == ["aws/spans", "/ecs/clawbot"]
    assert discovery["time_range"]["endTime"] - discovery["time_range"][
        "startTime"] == timedelta(hours=24)
    (job,) = env.submitted
    assert job.spec["wait"]["log_groups"] == ["aws/spans", "/ecs/clawbot"]
    assert job.spec["target"]["runtime_arn"] == ""


def test_log_source_insights_over_picked_sessions(client, monkeypatch):
    from app.services import aws_clients
    from tests.evaluation.fakes import FakeJudge

    judge = FakeJudge(text='{"errors": []}', by_tool={"SessionSummary": {
        "intent_label": "a", "intent": "b", "summary_label": "c", "summary": "d"}})
    env, _ = stub_environment(monkeypatch, judge=judge)
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: _Logs(LOG_SOURCE["log_group_names"]))
    sessions = [f"01KK{i}#feishu#oc_1" for i in range(3)]
    res = client.post("/api/eval/runs", json={
        "log_source": LOG_SOURCE, "mode": "insights", "session_ids": sessions,
        "session_source": "logs", "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    assert res.json()["dataset_name"] == "logs:3"
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    (job,) = env.submitted
    assert [s["session_id"] for s in job.spec["sessions"]] == sessions
    assert job.spec["wait"]["log_groups"] == ["aws/spans", "/ecs/clawbot"]
    assert set(run["insights"]) == {"failures", "userIntents", "executionSummaries",
                                    "failureDetection"}  # N17-obs449


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
