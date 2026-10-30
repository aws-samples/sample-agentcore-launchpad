"""N13-obs19 (E14 NC-02, lead ruling 2026-10-18) — an insights run whose lookback window
resolves to zero sessions is refused with 409 ``run.no_sessions`` BEFORE any row or job:
``submit_run`` resolves the window synchronously (one Logs Insights query at request time)
and pins the ids it found on the run; ``plan_run`` keeps the same guard for any caller that
hands it a bare window. Evaluators-mode windows and explicit session lists are untouched."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.evaluation import service as svc
from app.evaluation.models import EvalRun
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeJudge
from tests.evaluation.test_runs_flow import make_agent, stub_environment, wait_terminal

SESSIONS = ["window-session-1" + "x" * 30, "window-session-2" + "x" * 30]


def _runs() -> list[EvalRun]:
    db = SessionLocal()
    try:
        return db.query(EvalRun).all()
    finally:
        db.close()


def _insights_judge() -> FakeJudge:
    return FakeJudge(text='{"errors": []}', by_tool={"SessionSummary": {
        "intent_label": "Leave balance", "intent": "asks for PTO left",
        "summary_label": "HR lookup", "summary": "looked up HR"}})


def test_an_insights_window_with_no_sessions_is_409_before_any_row(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="empty-window")
    db.close()
    env, _calls = stub_environment(monkeypatch, judge=_insights_judge())
    queries: list[dict] = []
    monkeypatch.setattr(svc, "discover_sessions", lambda ws, **kw: queries.append(kw) or [])

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "mode": "insights", "lookback_hours": 1, "wait_seconds": 0,
    })
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "run.no_sessions"
    assert body["detail"] == {"lookback_hours": 1}
    assert "last 1 h" in body["message"]
    # resolved at request time, against the agent's telemetry, over the requested window
    (query,) = queries
    assert query["service_name"] == "eval_agent_abc123.DEFAULT"
    assert query["time_range"]["endTime"] - query["time_range"]["startTime"] == timedelta(hours=1)
    assert _runs() == [] and env.submitted == []


def test_an_insights_window_with_sessions_pins_the_ids_it_found(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="window-insights")
    db.close()
    env, _calls = stub_environment(monkeypatch, judge=_insights_judge())

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "mode": "insights", "lookback_hours": 24, "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    # the row carries the resolved ids from its first read, not only once the job starts
    assert res.json()["session_ids"] == SESSIONS
    assert res.json()["dataset_name"] == "window:24h"
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert run["session_ids"] == SESSIONS
    # one Logs Insights query in total: plan_run does not discover the window again
    assert len(env.discovered) == 1
    (job,) = env.submitted
    assert job.job_type == "insights" and len(job.spec["sessions"]) == 2


def test_an_evaluators_window_is_still_resolved_at_plan_time(client, monkeypatch):
    """Evaluators-mode windows keep today's path: discovered on the queue worker, in
    ``plan_run`` (``test_run_scopes`` pins the full flow)."""
    db = SessionLocal()
    agent = make_agent(db, name="window-evaluators")
    db.close()
    env, _calls = stub_environment(monkeypatch)
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "lookback_hours": 24,
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert run["session_ids"] == SESSIONS and len(env.discovered) == 1


def test_plan_run_refuses_a_bare_insights_window_with_no_sessions(monkeypatch):
    monkeypatch.setattr(svc, "discover_sessions", lambda ws, **kw: [])
    now = datetime.now(UTC)
    with pytest.raises(AppError) as err:
        svc.plan_run(
            "run-x", workspace=ws_ctx(), agent_arn="arn", agent_id="a1", protocol="http",
            service_name="svc.DEFAULT", log_group="/aws/bedrock-agentcore/runtimes/rt-DEFAULT",
            log_groups=["aws/spans", "/aws/bedrock-agentcore/runtimes/rt-DEFAULT"],
            items=[], evaluators=[], mode="insights", wait_seconds=0,
            existing_session_ids=None,
            time_range={"startTime": now - timedelta(hours=1), "endTime": now},
            insights=None, actor_model_id=None, runtime_user_id=None,
        )
    assert (err.value.code, err.value.status_code) == ("run.no_sessions", 409)
    assert err.value.detail == {"lookback_hours": None}
