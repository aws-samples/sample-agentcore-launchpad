"""N13-obs18 (E11 NC-10, lead ruling 2026-10-18) — ``POST /api/eval/runs`` with an explicit
``evaluators: []`` in evaluators mode is refused with 422 ``run.no_evaluators`` before any row:
a run that scores nothing is not a run. Omitting the field keeps the built-in default set, and
an insights run (which the V2 wizard submits with ``evaluators: []``) is untouched."""

from app.core.db import SessionLocal
from app.evaluation import evaluators_local
from app.evaluation.models import EvalRun
from tests.evaluation.fakes import FakeJudge
from tests.evaluation.test_runs_flow import make_agent, stub_environment, wait_terminal


def _runs() -> int:
    db = SessionLocal()
    try:
        return db.query(EvalRun).count()
    finally:
        db.close()


def test_an_explicit_empty_evaluator_list_is_422_before_any_row(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="empty-evaluators")
    db.close()
    env, _calls = stub_environment(monkeypatch)
    for scope in ({"session_ids": ["s1" + "x" * 32]}, {"lookback_hours": 1}):
        res = client.post("/api/eval/runs", json={
            "agent_id": agent.id, "evaluators": [], "wait_seconds": 0, **scope,
        })
        assert res.status_code == 422, res.text
        assert res.json()["code"] == "run.no_evaluators"
    # explicit mode, same answer
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "mode": "evaluators", "evaluators": [],
        "session_ids": ["s1" + "x" * 32], "wait_seconds": 0,
    })
    assert (res.status_code, res.json()["code"]) == (422, "run.no_evaluators")
    assert _runs() == 0 and env.submitted == []


def test_omitting_evaluators_still_applies_the_builtin_set(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="default-evaluators")
    db.close()
    env, _calls = stub_environment(monkeypatch)
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "session_ids": ["s1" + "x" * 32], "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    assert res.json()["evaluators"] == list(evaluators_local.BUILTIN_EVALUATORS)
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert len(env.submitted) == 1


def test_an_insights_run_may_carry_an_empty_evaluator_list(client, monkeypatch):
    """The V2 task wizard sends ``evaluators: []`` alongside ``insights`` — insights runs
    score with insight types, so the empty list is not a refusal there."""
    db = SessionLocal()
    agent = make_agent(db, name="insights-empty-evaluators")
    db.close()
    judge = FakeJudge(text='{"errors": []}', by_tool={"SessionSummary": {
        "intent_label": "Leave balance", "intent": "asks for PTO left",
        "summary_label": "HR lookup", "summary": "looked up HR"}})
    stub_environment(monkeypatch, judge=judge)
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "mode": "insights", "evaluators": [],
        "insights": ["Builtin.Insight.UserIntent"],
        "session_ids": ["s1" + "x" * 32], "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    assert res.json()["evaluators"] == ["Builtin.Insight.UserIntent"]
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
