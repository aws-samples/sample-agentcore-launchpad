"""Spend budgets for long-running jobs (child 2): the payer per job and per action,
exact rows where a job's work reports usage, flagged estimates where it does not,
the Skill Lab true-up, and the boundary stops.

Shapes reuse the core child's live probe (tests/test_budgets.py): Harness sends one
``metadata`` usage per model call; the stub CLIs below emulate the vendored Skill
Lab producers' on-disk contracts (``results.json`` usage, ``history.json`` steps).
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import app.evaluation.service as svc
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import simulation
from app.evaluation.models import EvalDataset, EvalRecommendation, EvalRun, OnlineEvalConfig
from app.main import create_app
from app.models.budgets import UsageEvent, UsageSession
from app.models.ledger import Agent
from app.optimization import canary_service as canary_svc
from app.optimization import service as exp_svc
from app.optimization.models import Experiment, RuntimeCanary
from app.services import budgets, job_budget, rates, usage_meter
from app.services import users as users_service
from app.services.creators import Creator
from app.services.job_budget import BudgetStop, JobBudget
from tests.conftest import set_default_resources, ws_ctx

ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)
SONNET = "global.anthropic.claude-sonnet-5"  # $3 / $15 per 1M
UNPRICED = "acme.unpriced-model-v1"
HARNESS_ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/h-1"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/r-1"
# one model call of 100K input / 1K output tokens on Sonnet: $0.315
BIG_CALL = {"usage": {"inputTokens": 100_000, "outputTokens": 1_000, "totalTokens": 101_000}}
BIG_CALL_USD = (100_000 * 3 + 1_000 * 15) / 1e6


# ── ledger helpers ───────────────────────────────────────────────────────────


def _user(username="mia", role="member") -> str:
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, username)
        if user is None:
            from app.models.ledger import User

            user = User(username=username, username_key=username.lower(),
                        email=f"{username}@acme-corp.com", password_hash="x", role=role,
                        status="active")
            db.add(user)
            db.commit()
        return user.id
    finally:
        db.close()


def _limit(user_id: str, usd: float) -> None:
    db = SessionLocal()
    try:
        budgets.assign_user(db, user_id, level_id=None, override_usd=usd)
    finally:
        db.close()


def _spent(user_id, usd, *, source="chat", agent_id=None, kind="tokens",
           estimated=False, session_id=None) -> str:
    db = SessionLocal()
    try:
        row = UsageEvent(user_id=user_id, period=budgets.period_of(), source=source,
                         usd=usd, kind=kind, estimated=estimated, agent_id=agent_id,
                         session_id=session_id)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _rows(**filters) -> list[UsageEvent]:
    db = SessionLocal()
    try:
        query = db.query(UsageEvent)
        for key, value in filters.items():
            query = query.filter(getattr(UsageEvent, key) == value)
        rows = query.order_by(UsageEvent.created_at).all()
        for row in rows:
            db.expunge(row)
        return rows
    finally:
        db.close()


def _spend(user_id) -> float:
    db = SessionLocal()
    try:
        return budgets.spend(db, user_id, budgets.period_of())
    finally:
        db.close()


def _agent(method="harness", model=SONNET, name="job-agent", created_by=None) -> str:
    db = SessionLocal()
    try:
        spec = {"name": name}
        if model is not None:
            spec["model_id"] = model
        agent = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, name=name, method=method, status="active",
            arn=HARNESS_ARN if method == "harness" else RUNTIME_ARN,
            resource_id="h-1" if method == "harness" else "r-1", spec=spec,
            created_by=created_by,
        )
        db.add(agent)
        db.commit()
        return agent.id
    finally:
        db.close()


def _settings(**values) -> None:
    db = SessionLocal()
    try:
        budgets.set_budget_settings(db, values, "test")
    finally:
        db.close()


def _run(created_by=None, name=None, agent_id="a1", **fields) -> str:
    db = SessionLocal()
    try:
        values = {"mode": "evaluators", "evaluators": ["Builtin.Correctness"],
                  "status": "queued", **fields}
        run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                      agent_name="eval-target", created_by=created_by,
                      created_by_name=name, **values)
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _get_run(run_id) -> EvalRun:
    db = SessionLocal()
    try:
        return db.get(EvalRun, run_id)
    finally:
        db.close()


# ── eval runs ────────────────────────────────────────────────────────────────


def _eval_env(monkeypatch):
    data = MagicMock()
    data.start_batch_evaluation.return_value = {"batchEvaluationId": "be-1"}
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: data)
    monkeypatch.setattr(svc, "_wait_for_fresh_telemetry", lambda **_kw: None)
    monkeypatch.setattr(svc, "_poll_batch", lambda *_a, **_kw: {"status": "COMPLETED"})
    monkeypatch.setattr(svc, "_finish_from_result",
                        lambda run_id, *_a, **_kw: svc._update(run_id, status="completed"))
    return data


def _harness_invoke(calls: list, metadata=(BIG_CALL,)):
    def invoke(client, arn, prompt, session_id=None, actor_id="default", on_usage=None,
               **_kw):
        session_id = session_id or svc.hc.new_session_id()  # as the real wrapper does
        calls.append({"prompt": prompt, "sid": session_id})
        for event in metadata:
            if on_usage is not None:
                on_usage(event["usage"])
        return {"text": "ok", "session_id": session_id}

    return invoke


def _execute(run_id, agent_id, items, *, method="harness", evaluators=None, **kwargs):
    svc.execute_run(
        run_id, workspace=ws_ctx(), agent_arn=HARNESS_ARN if method == "harness" else RUNTIME_ARN,
        method=method, service_name="svc.DEFAULT", log_group="/lg", items=items,
        evaluators=evaluators or ["Builtin.Correctness"], mode="evaluators",
        wait_seconds=0, agent_id=agent_id, **kwargs,
    )


def test_eval_harness_replay_turns_are_exact_rows_charged_to_the_run_creator(monkeypatch):
    member = _user()
    agent = _agent()
    run_id = _run(created_by=member, name="mia", agent_id=agent)
    data = _eval_env(monkeypatch)
    calls: list = []
    monkeypatch.setattr(svc.hc, "invoke_harness_text", _harness_invoke(calls))

    _execute(run_id, agent, [{"prompt": "a"}, {"prompt": "b"}])

    run = _get_run(run_id)
    assert run.status == "completed" and len(run.session_ids) == 2
    turns = _rows(ref_type="eval_run", ref_id=run_id, kind="tokens")
    assert len(turns) == 2
    for row in turns:
        assert row.user_id == member and row.user_name == "mia" and row.source == "eval"
        assert row.estimated is False and row.model_id == SONNET
        assert row.usd == pytest.approx(BIG_CALL_USD) and row.agent_id == agent
    assert {row.session_id for row in turns} == set(run.session_ids)
    # no double counting: exact turns get no estimate; the only estimate is the judges'
    estimates = _rows(ref_type="eval_run", ref_id=run_id, kind="job_estimate")
    assert [row.detail["estimate"] for row in estimates] == ["judges"]
    assert estimates[0].quantity == 2 and estimates[0].estimated is True
    data.start_batch_evaluation.assert_called_once()
    db = SessionLocal()
    try:
        sessions = db.query(UsageSession).filter(
            UsageSession.session_id.in_(run.session_ids)).all()
        assert {(s.user_id, s.source) for s in sessions} == {(member, "eval")}
    finally:
        db.close()


def test_eval_opaque_replay_turns_are_estimated_at_the_agents_recent_average(monkeypatch):
    member = _user()
    agent = _agent(method="zip_runtime")
    # two measured turns of this agent, any payer
    _spent(None, 0.04, agent_id=agent, session_id="chat-sess-1")
    _spent(None, 0.06, agent_id=agent, session_id="chat-sess-2")
    run_id = _run(created_by=member, agent_id=agent)
    _eval_env(monkeypatch)
    counter = {"n": 0}

    def invoke(client, arn, prompt, session_id=None, runtime_user_id=None, on_usage=None):
        counter["n"] += 1
        return {"text": "ok", "session_id": session_id or f"rt-{counter['n']}" + "x" * 30}

    monkeypatch.setattr(svc.rt, "invoke_runtime_text", invoke)

    _execute(run_id, agent, [{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}],
             method="zip_runtime")

    turns = [r for r in _rows(ref_type="eval_run", ref_id=run_id, kind="job_estimate")
             if r.unit == "turn"]
    assert len(turns) == 3 and all(r.usd == pytest.approx(0.05) for r in turns)
    assert all(r.estimated and r.user_id == member for r in turns)
    assert not _rows(ref_type="eval_run", ref_id=run_id, kind="tokens")


def test_a_recommendation_job_is_not_averaged_as_an_agent_turn():
    # a gepa_lite job is one exact tokens row on the agent with no session: a whole
    # optimization, not a turn — it must not become the agent's per-turn estimate
    agent = _agent(method="zip_runtime")
    _spent(None, 1.50, source="eval", agent_id=agent)
    db = SessionLocal()
    try:
        assert job_budget.recent_turn_cost(db, agent) is None
        assert job_budget.turn_estimate(db, agent, fallback="opaque_turn_estimate_usd") == (
            budgets.DEFAULT_SETTINGS["opaque_turn_estimate_usd"], "opaque_turn_estimate_usd")
        _spent(None, 0.02, agent_id=agent, session_id="chat-sess-1")
        assert job_budget.recent_turn_cost(db, agent) == pytest.approx(0.02)
    finally:
        db.close()


def test_a_claude_sdk_turn_that_reports_usage_gets_no_estimate(monkeypatch):
    member = _user()
    agent = _agent(method="container")
    run_id = _run(created_by=member, agent_id=agent)
    _eval_env(monkeypatch)

    def invoke(client, arn, prompt, session_id=None, runtime_user_id=None, on_usage=None):
        on_usage({"input_tokens": 1000, "output_tokens": 100}, "turn")
        return {"text": "ok", "session_id": "cc-" + "x" * 30}

    monkeypatch.setattr(svc.rt, "invoke_runtime_text", invoke)
    _execute(run_id, agent, [{"prompt": "a"}], method="container")

    assert len(_rows(ref_type="eval_run", ref_id=run_id, kind="tokens")) == 1
    assert [r.unit for r in _rows(ref_type="eval_run", ref_id=run_id,
                                  kind="job_estimate")] == ["evaluation"]


def test_an_eval_run_stops_at_the_next_scenario_once_its_creator_crosses(monkeypatch):
    member = _user()
    _limit(member, 0.5)
    agent = _agent()
    run_id = _run(created_by=member, agent_id=agent)
    data = _eval_env(monkeypatch)
    calls: list = []
    monkeypatch.setattr(svc.hc, "invoke_harness_text", _harness_invoke(calls))

    # each scenario costs $0.315: after two the creator is over $0.50
    _execute(run_id, agent, [{"scenario_id": f"s{i}", "turns": [{"input": f"q{i}"}]}
                             for i in range(4)])

    run = _get_run(run_id)
    assert run.status == "stopped"
    assert run.error.startswith("budget_exceeded: monthly budget reached")
    assert job_budget.stop_reason_of(run.error) == "budget_exceeded"
    assert len(calls) == 2 and len(run.session_ids) == 2  # the work done is kept
    data.start_batch_evaluation.assert_not_called()  # stopped before the judges
    assert not _rows(ref_type="eval_run", ref_id=run_id, kind="job_estimate")


def test_an_eval_run_checks_the_budget_before_each_prompt_of_a_scenario(monkeypatch):
    member = _user()
    _limit(member, 0.3)
    agent = _agent()
    run_id = _run(created_by=member, agent_id=agent)
    _eval_env(monkeypatch)
    calls: list = []
    monkeypatch.setattr(svc.hc, "invoke_harness_text", _harness_invoke(calls))

    _execute(run_id, agent, [{"scenario_id": "multi", "turns": [
        {"input": "one"}, {"input": "two"}, {"input": "three"}]}])

    assert [c["prompt"] for c in calls] == ["one"]  # the in-flight turn finished
    assert _get_run(run_id).status == "stopped"


def test_an_exempt_run_creator_is_metered_but_never_stopped(monkeypatch):
    agent = _agent()
    run_id = _run(created_by=None, name="operator", agent_id=agent)
    data = _eval_env(monkeypatch)
    calls: list = []
    monkeypatch.setattr(svc.hc, "invoke_harness_text", _harness_invoke(calls))
    _spent(None, 10_000.0)  # nothing limits a principal with no account

    _execute(run_id, agent, [{"prompt": f"p{i}"} for i in range(3)])

    assert _get_run(run_id).status == "completed" and len(calls) == 3
    rows = _rows(ref_type="eval_run", ref_id=run_id, kind="tokens")
    assert len(rows) == 3 and {(r.user_id, r.user_name) for r in rows} == {(None, "operator")}
    data.start_batch_evaluation.assert_called_once()


def test_the_judge_estimate_prices_builtin_and_custom_evaluators(monkeypatch):
    db = SessionLocal()
    try:
        usd, detail = job_budget.judge_estimate(db, ["Builtin.Correctness", "my-judge"], 4)
        cal = rates.calibration(db)
        fee = rates.agentcore_rates(db)
    finally:
        db.close()
    builtin = (cal["builtin_eval_input_tokens"] / 1000 * fee["evaluation_batch_input_per_1k"]
               + cal["builtin_eval_output_tokens"] / 1000 * fee["evaluation_batch_output_per_1k"])
    judge, _ = rates.price_tokens(get_settings().judge_model_id,
                                  cal["builtin_eval_input_tokens"],
                                  cal["builtin_eval_output_tokens"])
    assert detail["per_evaluation_usd"]["Builtin.Correctness"] == pytest.approx(builtin)
    assert detail["per_evaluation_usd"]["my-judge"] == pytest.approx(
        fee["evaluation_custom"] + (judge or 0.0))
    assert usd == pytest.approx(4 * (builtin + fee["evaluation_custom"] + (judge or 0.0)))


def test_an_insights_run_debits_no_judge_estimate(monkeypatch):
    member = _user()
    run_id = _run(created_by=member, mode="insights", session_ids=["s1", "s2"])
    _eval_env(monkeypatch)
    svc.execute_run(
        run_id, workspace=ws_ctx(), agent_arn=RUNTIME_ARN, method="zip_runtime",
        service_name="svc.DEFAULT", log_group="/lg", items=[], evaluators=[],
        mode="insights", wait_seconds=0, existing_session_ids=["s1", "s2"],
    )
    assert _get_run(run_id).status == "completed"
    assert not _rows(ref_type="eval_run", ref_id=run_id)


# ── simulated personas ───────────────────────────────────────────────────────


class _Executor:
    """The SDK executor, calling the invoker ``turns`` times."""

    turns = 3

    def __init__(self, *, agent_invoker, simulation_config):
        self.agent_invoker = agent_invoker

    def run_scenario(self, scenario):
        from bedrock_agentcore.evaluation import AgentInvokerInput

        for turn in range(self.turns):
            self.agent_invoker(AgentInvokerInput(payload=f"turn-{turn}", session_id="k"))
        return SimpleNamespace(status="COMPLETED", error=None)


PERSONA = {
    "scenario_id": "leave", "input": "I need time off",
    "actor_profile": {"traits": {}, "context": "", "goal": "Book leave"},
    "max_turns": 8,
}


def test_a_persona_debits_max_actor_turns_then_trues_up_to_the_turns_taken(monkeypatch):
    member = _user()
    agent = _agent()
    run_id = _run(created_by=member, agent_id=agent)
    _eval_env(monkeypatch)
    monkeypatch.setattr(simulation, "SimulatedScenarioExecutor", _Executor)
    calls: list = []
    monkeypatch.setattr(simulation.hc, "invoke_harness_text", _harness_invoke(calls))

    _execute(run_id, agent, [PERSONA], actor_model_id=SONNET)

    run = _get_run(run_id)
    assert run.status == "completed", (run.error, run.scenario_failures)
    assert len(calls) == 3
    # the agent side of every persona turn is exact
    assert len(_rows(ref_type="eval_run", ref_id=run_id, kind="tokens")) == 3
    actor = [r for r in _rows(ref_type="eval_run", ref_id=run_id, kind="job_estimate")
             if r.unit == "actor_turn"]
    assert [r.quantity for r in actor] == [8, 3]
    upfront, actual = actor
    assert upfront.superseded_by == actual.id and actual.superseded_by is None
    settings = budgets.DEFAULT_SETTINGS
    per_turn, _ = rates.price_tokens(SONNET, settings["persona_actor_input_tokens"],
                                     settings["persona_actor_output_tokens"])
    assert actual.usd == pytest.approx(3 * per_turn) and actual.model_id == SONNET
    assert upfront.usd == pytest.approx(8 * per_turn)


def test_an_unpriced_actor_model_is_estimated_without_a_price():
    budget = JobBudget(user_id=None, user_name="operator", source="eval",
                       ref_type="eval_run", ref_id="r-unpriced")
    row_id = svc._persona_estimate(budget, PERSONA, UNPRICED, 8)
    row = next(r for r in _rows(ref_id="r-unpriced") if r.id == row_id)
    assert row.usd is None and row.detail["unpriced"] is True and row.model_id == UNPRICED


# ── experiment and canary traffic ────────────────────────────────────────────


@pytest.fixture
def gateway(monkeypatch):
    """A fake gateway: every prompt answers 200 (status per prompt overridable)."""
    sent: list[str] = []
    statuses: dict[str, int] = {}

    def post(url, body, workspace, *, session_id=None, user_id=None, **_kw):
        sent.append(body["prompt"])
        return SimpleNamespace(status_code=statuses.get(body["prompt"], 200))

    monkeypatch.setattr(exp_svc, "sigv4_post", post)
    return SimpleNamespace(sent=sent, statuses=statuses)


def test_traffic_debits_each_answered_prompt_and_aborts_the_rest_once_over(gateway):
    member = _user()
    _limit(member, 0.25)
    budget = JobBudget(user_id=member, user_name="mia", source="experiment",
                       agent_id="ag-1", ref_type="experiment", ref_id="exp-1")
    prompts = [f"p{i}" for i in range(10)]
    gateway.statuses["p1"] = 500

    result = exp_svc.send_gateway_traffic(
        "https://gw", "t1", prompts, ws_ctx(), concurrency=1, budget=budget,
        estimate_usd=0.1,
    )

    # p0 ($0.10), p1 (500: no estimate), p2 ($0.20), p3 ($0.30 ≥ $0.25) → stop
    assert gateway.sent == ["p0", "p1", "p2", "p3"]
    assert result["sent"] == 3 and result["failed"] == 1 and result["skipped"] == 6
    assert result["stop_reason"] == "budget_exceeded"
    assert result["stop_message"].startswith("budget_exceeded: monthly budget reached")
    assert result["status_counts"] == {"200": 3, "500": 1}
    rows = _rows(ref_type="experiment", ref_id="exp-1")
    assert [r.kind for r in rows] == ["job_estimate"] * 3
    assert {r.session_id for r in rows} == set(result["session_ids"])
    assert all(r.user_id == member and r.estimated and r.unit == "prompt" for r in rows)


def test_traffic_without_a_budget_keeps_its_result_shape(gateway):
    result = exp_svc.send_gateway_traffic("https://gw", "t1", ["a", "b"], ws_ctx(),
                                          concurrency=1)
    assert set(result) == {"session_ids", "sent", "failed", "status_counts"}
    assert not _rows(ref_type="experiment")


def _experiment(created_by: str | None, agent_id: str) -> str:
    db = SessionLocal()
    try:
        exp = Experiment(
            workspace_id=DEFAULT_WORKSPACE_ID, name="EXP", agent_id=agent_id,
            agent_name="job-agent", created_by=created_by, stage="abtest",
            artifacts={"gateway": {"gateway_url": "https://gw", "target_v1": "t1"},
                       "agent_meta": {"id": agent_id}},
        )
        db.add(exp)
        db.commit()
        return exp.id
    finally:
        db.close()


def test_experiment_traffic_is_paid_by_the_action_caller_not_the_creator(gateway, monkeypatch):
    creator = _user("creator")
    caller = _user("caller")
    agent = _agent(method="zip_runtime")
    exp_id = _experiment(creator, agent)
    monkeypatch.setattr(exp_svc, "_spawn", lambda target: target())

    exp_svc.run_action(
        exp_id, "traffic",
        lambda progress: exp_svc.act_traffic(exp_id, ["q1", "q2"], {"dataset_id": "d"},
                                             progress, caller=Creator(caller, "caller")),
        caller=Creator(caller, "caller"),
    )

    db = SessionLocal()
    try:
        exp = db.get(Experiment, exp_id)
        assert exp.artifacts["action_callers"]["traffic"]["user_id"] == caller
        assert exp.artifacts["traffic"]["sent"] == 2 and exp.error is None
    finally:
        db.close()
    rows = _rows(ref_type="experiment", ref_id=exp_id)
    assert len(rows) == 2 and {r.user_id for r in rows} == {caller}
    # no measured turns for the agent yet: the agent-turn fallback setting
    assert rows[0].usd == budgets.DEFAULT_SETTINGS["agent_turn_estimate_usd"]


def test_a_budget_stopped_experiment_traffic_keeps_its_prompts_and_says_why(
        gateway, monkeypatch):
    monkeypatch.setattr(exp_svc, "TRAFFIC_MAX_CONCURRENCY", 1)  # one prompt at a time
    caller = _user("caller")
    _limit(caller, 0.03)
    agent = _agent(method="zip_runtime")
    exp_id = _experiment(None, agent)
    exp_svc.act_traffic(exp_id, ["a", "b", "c", "d"], {"dataset_id": "d"},
                        lambda _m: None, caller=Creator(caller, "caller"))
    db = SessionLocal()
    try:
        exp = db.get(Experiment, exp_id)
        traffic = exp.artifacts["traffic"]
        assert exp.stage == "traffic" and traffic["stop_reason"] == "budget_exceeded"
        # $0.02 per prompt: the second answer crosses $0.03, the rest never go out
        assert (traffic["sent"], traffic["skipped"]) == (2, 2)
        assert traffic["stop_message"].startswith("budget_exceeded: ")
        assert exp.error is None  # not a failure to retry: a retry would be refused
    finally:
        db.close()


def _paired_row(champion: str, edited_model: str | None = None, *,
                harness: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id="can-1", workspace_id=DEFAULT_WORKSPACE_ID, champion_agent_id=champion,
        artifacts={
            **({"kind": "harness"} if harness else {}),
            "setup": {"stable_endpoint": "ctl", "treatment_endpoint": "trt"},
            "agent_meta": {"arn": HARNESS_ARN if harness else RUNTIME_ARN},
            **({"edited_spec": {"model_id": edited_model}} if edited_model else {}),
        },
    )


def test_paired_canary_calls_are_exact_rows_priced_per_arm(monkeypatch):
    caller = _user("caller")
    agent = _agent()
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: MagicMock())
    calls: list = []
    monkeypatch.setattr(canary_svc.hc, "invoke_harness_text", _harness_invoke(calls))
    budget = JobBudget(user_id=caller, user_name="caller", source="canary",
                       agent_id=agent, ref_type="canary", ref_id="can-1")

    result = canary_svc._paired_traffic(
        _paired_row(agent, edited_model="global.anthropic.claude-haiku-4-5"), ws_ctx(),
        ["q1", "q2"], None, lambda _m: None, budget=budget)

    assert result["sent"] == 4 and "stop_reason" not in result and len(calls) == 4
    rows = _rows(ref_type="canary", ref_id="can-1")
    assert len(rows) == 4 and all(r.kind == "tokens" and not r.estimated for r in rows)
    assert {r.session_id for r in rows} == set(result["session_ids"])
    assert sorted(r.model_id for r in rows) == sorted(
        [SONNET, SONNET, "global.anthropic.claude-haiku-4-5",
         "global.anthropic.claude-haiku-4-5"])


def test_paired_canary_traffic_skips_whole_pairs_once_the_caller_is_over(monkeypatch):
    caller = _user("caller")
    _limit(caller, 0.5)
    agent = _agent()
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: MagicMock())
    monkeypatch.setattr(exp_svc, "TRAFFIC_MAX_CONCURRENCY", 1)  # one call at a time
    calls: list = []
    monkeypatch.setattr(canary_svc.hc, "invoke_harness_text", _harness_invoke(calls))
    budget = JobBudget(user_id=caller, user_name="caller", source="canary",
                       agent_id=agent, ref_type="canary", ref_id="can-2")

    result = canary_svc._paired_traffic(
        _paired_row(agent), ws_ctx(), ["q1", "q2", "q3"], None, lambda _m: None,
        budget=budget)

    # pair 1 ($0.63) runs both arms, then pairs 2 and 3 are skipped whole
    assert len(calls) == 2 and result["sent"] == 2 and result["skipped"] == 4
    assert result["stop_reason"] == "budget_exceeded"
    pairs = result["pairs"]
    assert pairs[0]["control_session_id"] and pairs[0]["treatment_session_id"]
    assert all(p["skipped"] == "budget_exceeded" and not p["control_session_id"]
               and not p["treatment_session_id"] for p in pairs[1:])


def _runtime_invoke(calls: list, reports_usage=lambda qualifier: False):
    """``rt.invoke_runtime_text``: a side whose ``reports_usage(qualifier)`` is true
    reports one model call (a Claude SDK runtime), any other answers silently (Strands)."""
    def invoke(client, arn, prompt, session_id=None, actor_id="default", on_usage=None,
               qualifier=None, **_kw):
        calls.append({"prompt": prompt, "sid": session_id, "qualifier": qualifier})
        if on_usage is not None and reports_usage(qualifier):
            on_usage(BIG_CALL["usage"])
        return {"text": "ok", "session_id": session_id}

    return invoke


def test_paired_runtime_canary_calls_settle_one_meter_each(monkeypatch):
    """A Runtime arm that reports usage is charged its tokens; a silent one the
    agent's turn estimate as a flagged ``job_estimate`` — one row per call, never
    both (the rule an eval replay turn follows)."""
    caller = _user("caller")
    agent = _agent(method="zip_runtime")
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: MagicMock())
    calls: list = []
    monkeypatch.setattr(canary_svc.rt, "invoke_runtime_text",
                        _runtime_invoke(calls, reports_usage=lambda q: q == "ctl"))
    budget = JobBudget(user_id=caller, user_name="caller", source="canary",
                       agent_id=agent, ref_type="canary", ref_id="can-r1")

    result = canary_svc._paired_traffic(
        _paired_row(agent, harness=False), ws_ctx(), ["q1", "q2"], None, lambda _m: None,
        budget=budget)

    assert result["sent"] == 4 and len(calls) == 4
    rows = _rows(ref_type="canary", ref_id="can-r1")
    assert len(rows) == 4 and {r.session_id for r in rows} == set(result["session_ids"])
    by_side = {c["sid"]: c["qualifier"] for c in calls}
    exact = [r for r in rows if by_side[r.session_id] == "ctl"]
    estimated = [r for r in rows if by_side[r.session_id] == "trt"]
    assert all(r.kind == "tokens" and not r.estimated for r in exact)
    assert all(r.usd == pytest.approx(BIG_CALL_USD) for r in exact)
    # read once per action, before any call: the agent had no measured turn yet
    assert all(r.kind == "job_estimate" and r.estimated for r in estimated)
    assert all(r.usd == budgets.DEFAULT_SETTINGS["agent_turn_estimate_usd"] for r in estimated)
    assert all(r.user_id == caller for r in rows)


def test_paired_runtime_canary_traffic_skips_whole_pairs_once_the_caller_is_over(monkeypatch):
    caller = _user("caller")
    _limit(caller, 0.03)
    agent = _agent(method="zip_runtime")
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: MagicMock())
    monkeypatch.setattr(exp_svc, "TRAFFIC_MAX_CONCURRENCY", 1)  # one call at a time
    calls: list = []
    monkeypatch.setattr(canary_svc.rt, "invoke_runtime_text", _runtime_invoke(calls))
    budget = JobBudget(user_id=caller, user_name="caller", source="canary",
                       agent_id=agent, ref_type="canary", ref_id="can-r2")

    result = canary_svc._paired_traffic(
        _paired_row(agent, harness=False), ws_ctx(), ["q1", "q2", "q3"], None,
        lambda _m: None, budget=budget)

    # pair 1 runs both arms ($0.02 each crosses $0.03), then pairs 2 and 3 are skipped whole
    assert len(calls) == 2 and result["sent"] == 2 and result["skipped"] == 4
    assert result["stop_reason"] == "budget_exceeded"
    assert result["stop_message"].startswith("budget_exceeded: ")
    pairs = result["pairs"]
    assert pairs[0]["control_session_id"] and pairs[0]["treatment_session_id"]
    assert all(p["skipped"] == "budget_exceeded" and not p["control_session_id"]
               and not p["treatment_session_id"] for p in pairs[1:])


# ── recommendations (gepa_lite) ──────────────────────────────────────────────


def test_a_provider_recommendation_charges_its_converse_usage_exactly(monkeypatch):
    from app.optimization.providers.base import OptimizeResult

    member = _user()
    meter = usage_meter.UsageMeter.for_user(
        member, "eval", model_id=SONNET, ref_type="eval_recommendation", ref_id="rec-1")

    class Provider:
        id = "gepa_lite"

        def default_model_id(self):
            return SONNET

        def optimize(self, req, progress, converse=None):
            converse(req.model_id, "sys", [{"role": "user", "text": "u"}], 100)
            converse(req.model_id, "sys", [{"role": "user", "text": "u2"}], 100)
            return OptimizeResult(status="COMPLETED", recommended_prompt="better",
                                  explanation="x", meta={})

    def fake_converse(model_id, system, messages, max_tokens):
        return "{}", {"input_tokens": 1000, "output_tokens": 100, "stop_reason": "end"}

    monkeypatch.setattr(exp_svc.rec_providers, "get_provider", lambda _pid: Provider())
    monkeypatch.setattr(exp_svc.rec_evidence, "collect_evidence",
                        lambda **_kw: ([], SimpleNamespace(tool_calls_seen=0)))
    monkeypatch.setattr(exp_svc.rec_evidence, "default_transcript", lambda *_a: None)
    out = exp_svc._third_party_prompt_recommendation(
        "", {"id": "a1", "system_prompt": "cur"}, ws_ctx(), lambda _m: None,
        provider_id="gepa_lite", model_id=SONNET,
        source={"results_log_group": "g", "results_log_stream": "s", "run_id": "r"},
        converse=fake_converse, meter=meter,
    )
    assert out["system_prompt_status"] == "COMPLETED"
    rows = _rows(ref_type="eval_recommendation", ref_id="rec-1")
    assert len(rows) == 1 and rows[0].kind == "tokens" and rows[0].user_id == member
    assert (rows[0].tokens_in, rows[0].tokens_out) == (2000, 200)
    assert rows[0].usd == pytest.approx((2000 * 3 + 200 * 15) / 1e6)


def test_recommendation_requester_columns_are_migrated(tmp_path):
    import sqlalchemy as sa

    from app.core import db as db_module
    from app.core.db import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'rec.db'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE eval_recommendations DROP COLUMN requested_by"))
        conn.execute(sa.text("ALTER TABLE eval_recommendations DROP COLUMN requested_by_name"))
    db_module._migrate(engine)
    columns = {c["name"] for c in sa.inspect(engine).get_columns("eval_recommendations")}
    assert {"requested_by", "requested_by_name"} <= columns
    db_module._migrate(engine)  # idempotent


def test_a_recommendation_row_records_its_requester(monkeypatch):
    from app.evaluation import recommendations

    run_id = _run(status="completed", batch_eval_id="be-1", agent_id="a1")
    monkeypatch.setattr(recommendations, "_provider_preflight", lambda *_a: {"run_id": run_id})
    monkeypatch.setattr(recommendations, "_spawn", lambda fn: None)
    requester = Creator(_user("asker"), "asker")
    db = SessionLocal()
    try:
        rows = recommendations.start(
            db, db.get(EvalRun, run_id), ws_ctx(), kinds=["system_prompt"],
            input_source="manual", system_prompt="cur", evaluator=None, tools=None,
            provider="gepa_lite", model_id=SONNET, requester=requester,
        )
        out = recommendations.out(rows[0])
        assert (out["requested_by"], out["requested_by_name"]) == (requester.user_id, "asker")
        assert db.get(EvalRecommendation, rows[0].id).requested_by == requester.user_id
    finally:
        db.close()


# ── the HTTP surfaces: entry checks, the notice, the canary ramp ─────────────


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def _member(app, username: str) -> tuple[str, TestClient]:
    password = "sufficient-pass"
    client = TestClient(app, client=PEER)
    registered = client.post(
        "/api/auth/register",
        json={"username": username, "email": f"{username}@acme-corp.com", "password": password},
    )
    assert registered.status_code == 201, registered.text
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, username)
        user.status = users_service.STATUS_ACTIVE
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
        db.commit()
        user_id = user.id
    finally:
        db.close()
    assert client.post("/api/auth/login",
                       json={"username": username, "password": password}).status_code == 200
    return user_id, client


def _dataset(items, created_by=None) -> str:
    db = SessionLocal()
    try:
        dataset = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="ds", items=items,
                              created_by=created_by)
        db.add(dataset)
        db.commit()
        return dataset.id
    finally:
        db.close()


@pytest.fixture
def queued(monkeypatch):
    """Eval runs are accepted but never executed (the notice is the subject)."""
    import app.evaluation.routers as eval_routers

    queue = SimpleNamespace(submit=lambda run_id, fn: 0, position=lambda run_id: 0,
                            cancel=lambda run_id: False)
    monkeypatch.setattr(svc, "run_queue", queue)
    monkeypatch.setattr(eval_routers, "run_queue", queue)
    monkeypatch.setattr(svc, "resolve_telemetry", lambda agent, ws: ("svc.DEFAULT", "/lg"))


def test_creating_a_run_whose_forecast_exceeds_what_is_left_carries_a_notice(app, queued):
    member, client = _member(app, "forecaster")
    _limit(member, 1.0)
    _spent(member, 0.99)
    agent = _agent(created_by=member)
    dataset = _dataset([{"prompt": f"q{i}"} for i in range(5)], created_by=member)
    response = client.post("/api/eval/runs", json={
        "agent_id": agent, "dataset_id": dataset, "evaluators": ["Builtin.Correctness"],
        "wait_seconds": 0})
    assert response.status_code == 201, response.text
    body = response.json()
    notice = body["budget_notice"]
    assert notice["remaining_usd"] == pytest.approx(0.01) and notice["limit_usd"] == 1.0
    assert notice["estimate_usd"] > notice["remaining_usd"]
    assert body["stop_reason"] is None


def test_a_run_that_fits_the_budget_has_no_notice(app, queued):
    member, client = _member(app, "fits")
    _limit(member, 100.0)
    agent = _agent(created_by=member)
    dataset = _dataset([{"prompt": "q"}], created_by=member)
    body = client.post("/api/eval/runs", json={
        "agent_id": agent, "dataset_id": dataset, "evaluators": ["Builtin.Correctness"],
        "wait_seconds": 0}).json()
    assert body["budget_notice"] is None


def test_a_persona_run_on_an_unpriced_actor_model_is_refused_for_a_budgeted_member(
        app, queued):
    member, client = _member(app, "actor")
    _limit(member, 10.0)
    agent = _agent(created_by=member)
    dataset = _dataset([PERSONA], created_by=member)
    response = client.post("/api/eval/runs", json={
        "agent_id": agent, "dataset_id": dataset, "evaluators": ["Builtin.GoalSuccessRate"],
        "actor_model_id": UNPRICED, "wait_seconds": 0})
    assert response.status_code == 402, response.text
    assert response.json()["code"] == "budget.model_unpriced"
    assert response.json()["detail"]["model_id"] == UNPRICED


def _canary(created_by: str | None, agent_id: str) -> str:
    db = SessionLocal()
    try:
        row = RuntimeCanary(
            workspace_id=DEFAULT_WORKSPACE_ID, name="c", champion_agent_id=agent_id,
            champion_agent_name="job-agent", challenger_agent_id=agent_id,
            challenger_agent_name="job-agent", created_by=created_by, stage="verdict",
            artifacts={"setup": {"ramp_stage": 0}},
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def test_a_canary_stage_advance_is_refused_for_a_caller_over_budget(app, monkeypatch):
    member, client = _member(app, "rampant")
    _limit(member, 1.0)
    _spent(member, 1.0)
    canary_id = _canary(member, _agent(created_by=member))
    dispatched: list = []
    monkeypatch.setattr(canary_svc, "run_action",
                        lambda cid, action, fn, caller=None: dispatched.append(action))
    for action in ("advance", "traffic", "setup"):
        response = client.post(f"/api/runtime-canaries/{canary_id}/action",
                               json={"action": action, "dataset_id": "d"})
        assert response.status_code == 402, (action, response.text)
        assert response.json()["code"] == "budget.exceeded"
    # winding down stays possible
    monkeypatch.setattr(canary_svc, "assert_verdict_allows", lambda *_a, **_kw: None)
    monkeypatch.setattr(canary_svc, "stage_not_ready_reason", lambda *_a: None)
    for action in ("rollback", "complete"):
        response = client.post(f"/api/runtime-canaries/{canary_id}/action",
                               json={"action": action})
        assert response.status_code == 202, (action, response.text)
    assert dispatched == ["rollback", "complete"]


def _harness_canary_model(canary_id: str, model: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(RuntimeCanary, canary_id)
        row.artifacts = {**row.artifacts, "kind": "harness", "edited_spec": {"model_id": model}}
        db.commit()
    finally:
        db.close()


def test_paired_canary_traffic_on_an_unpriced_arm_is_refused_for_a_budgeted_caller(
        app, monkeypatch):
    # paired calls are metered by tokens at each arm's model: an unpriced candidate
    # would otherwise cost a budgeted caller nothing (usd NULL rows)
    member, client = _member(app, "pairer")
    _limit(member, 10.0)
    canary_id = _canary(member, _agent(created_by=member))
    _harness_canary_model(canary_id, UNPRICED)
    dataset = _dataset([{"prompt": "q1"}], created_by=member)
    dispatched: list = []
    monkeypatch.setattr(canary_svc, "run_action",
                        lambda cid, action, fn, caller=None: dispatched.append(action))
    monkeypatch.setattr(canary_svc, "stage_not_ready_reason", lambda *_a: None)

    refused = client.post(f"/api/runtime-canaries/{canary_id}/action",
                          json={"action": "traffic", "dataset_id": dataset})
    assert refused.status_code == 402, refused.text
    assert refused.json()["code"] == "budget.model_unpriced"
    assert refused.json()["detail"]["model_id"] == UNPRICED and dispatched == []

    _harness_canary_model(canary_id, SONNET)
    allowed = client.post(f"/api/runtime-canaries/{canary_id}/action",
                          json={"action": "traffic", "dataset_id": dataset})
    assert allowed.status_code == 202, allowed.text
    assert dispatched == ["traffic"]


def test_resuming_an_online_config_is_refused_while_its_creator_is_over_budget(monkeypatch):
    from app.evaluation import online

    creator = _user("judge-owner")
    _limit(creator, 1.0)
    _spent(creator, 2.0)
    db = SessionLocal()
    try:
        db.add(OnlineEvalConfig(
            workspace_id=DEFAULT_WORKSPACE_ID, agent_id="a1", agent_name="a",
            config_id="cfg-owned-1", config_arn="arn:cfg", name="cfg", service_name="s",
            log_group="/lg", created_by=creator, created_by_name="judge-owner"))
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(online, "get_config", lambda *_a, **_kw: {
        "config_id": "cfg-owned-1", "owner": {"kind": "agent"},
        "created_by": creator, "created_by_name": "judge-owner"})
    monkeypatch.setattr(online, "_require_owner", lambda *_a: None)
    updated: list = []
    monkeypatch.setattr(online.ac, "update_online_eval_config",
                        lambda control, **kw: updated.append(kw))
    ws = SimpleNamespace(id=DEFAULT_WORKSPACE_ID, context=ws_ctx())
    db = SessionLocal()
    try:
        with pytest.raises(AppError) as refused:
            online.set_execution_status(db, ws, "cfg-owned-1", enabled=True,
                                        control=MagicMock())
        assert refused.value.code == "budget.exceeded" and refused.value.status_code == 402
        assert updated == []
        online.set_execution_status(db, ws, "cfg-owned-1", enabled=False, control=MagicMock())
        assert updated == [{"config_id": "cfg-owned-1", "executionStatus": "DISABLED"}]
    finally:
        db.close()


# ── JobBudget ────────────────────────────────────────────────────────────────


def test_job_budget_rereads_the_limit_at_every_boundary():
    member = _user()
    _limit(member, 1.0)
    _spent(member, 1.5)
    budget = JobBudget.for_user(member, "eval")
    assert budget.user_name == "mia"
    with pytest.raises(BudgetStop) as stopped:
        budget.check()
    assert stopped.value.message.startswith("budget_exceeded: ")
    assert stopped.value.detail["limit_usd"] == 1.0
    _limit(member, 5.0)  # an admin raised it mid-job
    budget.check()


def test_a_budget_check_that_cannot_read_the_ledger_fails_open(monkeypatch):
    member = _user()
    _limit(member, 0.0)
    monkeypatch.setattr(budgets, "payer_for_user_id",
                        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("db gone")))
    assert JobBudget.for_user(member, "eval", user_name="mia").exceeded() is None


def test_supersede_replaces_estimates_once():
    first = _spent("u1", 1.0, kind="job_estimate", estimated=True)
    second = _spent("u1", 0.4)
    third = _spent("u1", 0.3)
    assert usage_meter.supersede([first], second) == 1
    assert usage_meter.supersede([first], third) == 0  # its first replacement stays
    assert _spend("u1") == pytest.approx(0.7)


# ── Skill Lab ────────────────────────────────────────────────────────────────

WORKER_RESOURCES = {
    "skill_lab_worker_runtime_arn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/rt",
    "skill_lab_worker_role_arn": "arn:aws:iam::111122223333:role/worker",
    "skill_lab_worker_image_digest": "sha256:x",
    "artifacts_bucket": "bkt",
}
TARGET = "us.anthropic.claude-sonnet-5"
# claude rollouts report every counter, a codex rollout only a total
TARGET_USAGE = {"input": 10, "cache_write": 1000, "cache_read": 5000, "output": 200,
                "total": 6210}
CODEX_USAGE = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "total": 3000}
JUDGE_USAGE = {"input": 100, "output": 20}

STUB_EVAL = '''
import argparse, json, pathlib
parser = argparse.ArgumentParser()
for flag in ("--skill","--tasks","--out_root","--target_backend","--model",
             "--optimizer_backend","--optimizer_model","--judge_mode",
             "--judge_exec_backend","--judge_exec_model","--judge_exec_effort",
             "--judge_sandbox_command","--workers","--timeout","--limit","--assets-dir"):
    parser.add_argument(flag)
args = parser.parse_args()
tasks = json.loads(pathlib.Path(args.tasks).read_text())
out = pathlib.Path(args.out_root); out.mkdir(parents=True, exist_ok=True)
rows = []
for task in tasks:
    usage = json.loads(task["question"])
    rows.append({"id": task["id"], "hard": 1, "soft": 1.0, "score_valid": True,
                 "usage": usage["target"], "judge_usage": usage["judge"]})
(out / "results.json").write_text(json.dumps(rows))
'''

STUB_TRAIN = '''
import argparse, json, pathlib, time
import yaml
parser = argparse.ArgumentParser()
parser.add_argument("--config"); parser.add_argument("--out_root")
args = parser.parse_args()
config = yaml.safe_load(pathlib.Path(args.config).read_text())
seed = pathlib.Path(config["env"]["skill_init"]).read_text()
out = pathlib.Path(args.out_root); (out / "skills").mkdir(parents=True, exist_ok=True)
(out / "skills" / "skill_v0000.md").write_text(seed)
total = 4
(out / "config.json").write_text(json.dumps({"num_epochs": 1, "steps_per_epoch": total}))
path = out / "history.json"
history = json.loads(path.read_text()) if path.is_file() else []
for step in range(len(history) + 1, total + 1):
    time.sleep(0.5)
    history.append({"step": step, "epoch": 1, "action": "accept", "best_score": 0.5,
                    "best_step": step, "skill_len": len(seed), "wall_time_s": 0.5})
    path.write_text(json.dumps(history))
    (out / "runtime_state.json").write_text(json.dumps({"last_completed_step": step}))
    print("[STEP %d done]" % step, flush=True)
(out / "best_skill.md").write_text(seed + "\\n\\nIMPROVED")
(out / "summary.json").write_text(json.dumps({"total_steps": total, "test_hard": 0.9}))
'''


@pytest.fixture
def lab(tmp_path, monkeypatch):
    from app.skill_lab import artifacts, jobs, runner
    from app.skill_lab import tasksets as taskset_svc

    (tmp_path / "eval.py").write_text(STUB_EVAL)
    (tmp_path / "train.py").write_text(STUB_TRAIN)
    fake_settings = get_settings().model_copy(update={
        "skill_lab_python": sys.executable,
        "skill_lab_target_model_id": TARGET, "skill_lab_judge_model_id": TARGET,
    })
    monkeypatch.setattr(runner, "EVAL_SCRIPT", tmp_path / "eval.py")
    monkeypatch.setattr(runner, "TRAIN_SCRIPT", tmp_path / "train.py")
    monkeypatch.setattr(runner, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(artifacts, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(taskset_svc, "TASKSETS_DIR", tmp_path / "tasksets")
    monkeypatch.setattr(taskset_svc, "get_settings",
                        lambda: SimpleNamespace(skill_lab_python=sys.executable))
    monkeypatch.setattr(runner, "sweep_exec_jobs_prefix", lambda ws, log: None)
    monkeypatch.setattr(jobs, "STEP_POLL_S", 0.05)

    def fake_materialize(workspace, record_id, dest_parent, log):
        skill_dir = dest_parent / "skills" / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# demo\n")
        return skill_dir, {"kind": "registry", "record_id": record_id, "name": "demo-skill"}

    monkeypatch.setattr(runner, "materialize_registry_skill", fake_materialize)
    set_default_resources(WORKER_RESOURCES)
    return SimpleNamespace(jobs=jobs, taskset_svc=taskset_svc)


def _taskset(lab, tasks_by_split, mode) -> str:
    db = SessionLocal()
    try:
        return lab.taskset_svc.create_taskset(
            db, DEFAULT_WORKSPACE_ID, name=f"ts-{time.monotonic_ns()}", mode=mode,
            tasks_by_split=tasks_by_split)["id"]
    finally:
        db.close()


def _wait_job(job_id, statuses=("succeeded", "failed", "cancelled", "interrupted"),
              timeout=30.0):
    from app.skill_lab.models import SkillLabJob

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db = SessionLocal()
        try:
            row = db.get(SkillLabJob, job_id)
            if row.status in statuses:
                db.expunge(row)
                return row
        finally:
            db.close()
        time.sleep(0.05)
    pytest.fail(f"job {job_id} never reached {statuses}")


def _context():
    from app.services.workspace import context_for_workspace

    return context_for_workspace(DEFAULT_WORKSPACE_ID)


def _eval_task(i: int, target=TARGET_USAGE) -> dict:
    return {"id": f"t{i}", "question": json.dumps({"target": target, "judge": JUDGE_USAGE}),
            "rubric": "PASS"}


def test_a_skill_lab_eval_estimate_is_trued_up_from_results_json(lab):
    member = _user()
    ts = _taskset(lab, {"tasks": [_eval_task(1), _eval_task(2, CODEX_USAGE)]}, "single")
    db = SessionLocal()
    try:
        out = lab.jobs.submit_eval_job(
            db, DEFAULT_WORKSPACE_ID, _context(),
            skill_source={"kind": "registry", "record_id": "rec-1"}, taskset_id=ts,
            creator=Creator(member, "mia"))
    finally:
        db.close()
    assert _wait_job(out["id"]).status == "succeeded"
    assert out["budget_notice"] is None and out["stop_reason"] is None

    rows = _rows(ref_type="skill_lab_job", ref_id=out["id"])
    estimate = next(r for r in rows if r.kind == "job_estimate")
    assert estimate.quantity == 2 and estimate.unit == "task"
    assert estimate.usd == pytest.approx(2 * budgets.DEFAULT_SETTINGS["skill_lab_eval_task_usd"])
    measured = {r.detail["side"]: r for r in rows if r.kind == "tokens"}
    assert set(measured) == {"target", "judge"} and estimate.superseded_by in {
        measured["target"].id, measured["judge"].id}
    target = measured["target"]
    # claude counters as reported, the codex total at the output rate
    assert (target.tokens_in, target.cache_write, target.cache_read, target.tokens_out) == (
        10, 1000, 5000, 200 + 3000)
    assert target.usd == pytest.approx(rates.price_tokens(TARGET, 10, 3200, 5000, 1000)[0])
    judge = measured["judge"]
    assert (judge.tokens_in, judge.tokens_out) == (200, 40) and judge.model_id == TARGET
    assert all(r.user_id == member and r.source == "skill_lab" for r in rows)
    assert _spend(member) == pytest.approx(target.usd + judge.usd)


def test_a_skill_lab_job_with_an_unpriced_model_is_refused_for_a_budgeted_creator(lab):
    member = _user()
    _limit(member, 10.0)
    ts = _taskset(lab, {"tasks": [_eval_task(1)]}, "single")
    db = SessionLocal()
    try:
        with pytest.raises(AppError) as refused:
            lab.jobs.submit_eval_job(
                db, DEFAULT_WORKSPACE_ID, _context(),
                skill_source={"kind": "registry", "record_id": "rec-1"}, taskset_id=ts,
                params={"target_model": UNPRICED}, creator=Creator(member, "mia"))
        assert refused.value.code == "budget.model_unpriced"
    finally:
        db.close()


def _train_taskset(lab) -> str:
    def tasks(prefix):
        return [{"id": f"{prefix}{i}", "question": "q", "rubric": "PASS"} for i in range(2)]

    return _taskset(lab, {"train": tasks("tr"), "val": tasks("va")}, "split")


def test_a_skill_lab_train_job_stops_between_steps_and_resumes_later(lab):
    member = _user()
    _limit(member, 2.5)
    _settings(skill_lab_train_step_usd=1.0)
    ts = _train_taskset(lab)
    db = SessionLocal()
    try:
        out = lab.jobs.submit_train_job(
            db, DEFAULT_WORKSPACE_ID, _context(),
            skill_source={"kind": "registry", "record_id": "rec-1"}, taskset_id=ts,
            creator=Creator(member, "mia"))
    finally:
        db.close()

    row = _wait_job(out["id"])
    # steps 1-3 were debited as each started; after step 3 the $3.00 crossed $2.50
    assert row.status == "interrupted"
    assert row.error.startswith("budget_exceeded: monthly budget reached")
    assert lab.jobs.job_out(row)["stop_reason"] == "budget_exceeded"
    from app.skill_lab import artifacts

    history = json.loads((artifacts.out_root(row.id) / "history.json").read_text())
    assert len(history) == 3  # the completed steps are kept (checkpointed)
    steps = [r.detail["step"] for r in _rows(ref_type="skill_lab_job", ref_id=row.id)]
    assert steps == [1, 2, 3]

    db = SessionLocal()
    try:
        with pytest.raises(AppError) as refused:  # still over: the creator pays
            lab.jobs.resume_job(db, DEFAULT_WORKSPACE_ID, _context(), row.id)
        assert refused.value.code == "budget.exceeded"
        _limit(member, 100.0)  # an admin raised it
        lab.jobs.resume_job(db, DEFAULT_WORKSPACE_ID, _context(), row.id)
    finally:
        db.close()
    resumed = _wait_job(row.id, statuses=("succeeded", "failed"))
    assert resumed.status == "succeeded" and resumed.error is None
    assert [r.detail["step"] for r in _rows(ref_type="skill_lab_job", ref_id=row.id)] == [
        1, 2, 3, 4]


def test_an_exempt_skill_lab_creator_is_never_stopped(lab):
    _settings(skill_lab_train_step_usd=1.0)
    ts = _train_taskset(lab)
    db = SessionLocal()
    try:
        out = lab.jobs.submit_train_job(
            db, DEFAULT_WORKSPACE_ID, _context(),
            skill_source={"kind": "registry", "record_id": "rec-1"}, taskset_id=ts,
            creator=Creator(None, "operator"))
    finally:
        db.close()
    row = _wait_job(out["id"])
    assert out["budget_notice"] is None
    assert row.status == "succeeded"
    rows = _rows(ref_type="skill_lab_job", ref_id=row.id)
    assert [r.detail["step"] for r in rows] == [1, 2, 3, 4]
    assert {(r.user_id, r.user_name) for r in rows} == {(None, "operator")}


def test_a_queued_skill_lab_job_whose_creator_crossed_meanwhile_never_starts(lab, monkeypatch):
    member = _user()
    _limit(member, 1.0)
    ts = _taskset(lab, {"tasks": [_eval_task(1)]}, "single")
    held: list = []
    monkeypatch.setattr(lab.jobs.job_queue, "submit", lambda job_id, fn: held.append(fn) or 0)
    db = SessionLocal()
    try:
        out = lab.jobs.submit_eval_job(
            db, DEFAULT_WORKSPACE_ID, _context(),
            skill_source={"kind": "registry", "record_id": "rec-1"}, taskset_id=ts,
            creator=Creator(member, "mia"))
    finally:
        db.close()
    _spent(member, 1.0)  # a chat turn crossed the limit while the job waited
    held[0]()
    row = _wait_job(out["id"])
    assert row.status == "failed" and row.error.startswith("budget_exceeded")
    assert row.started_at is None
    assert not _rows(ref_type="skill_lab_job", ref_id=out["id"])


def test_a_skill_lab_submit_warns_when_the_estimate_exceeds_what_is_left(lab, monkeypatch):
    member = _user()
    _limit(member, 0.2)
    ts = _taskset(lab, {"tasks": [_eval_task(i) for i in range(4)]}, "single")
    monkeypatch.setattr(lab.jobs.job_queue, "submit", lambda job_id, fn: 0)
    db = SessionLocal()
    try:
        out = lab.jobs.submit_eval_job(
            db, DEFAULT_WORKSPACE_ID, _context(),
            skill_source={"kind": "registry", "record_id": "rec-1"}, taskset_id=ts,
            creator=Creator(member, "mia"))
    finally:
        db.close()
    notice = out["budget_notice"]
    assert notice["estimate_usd"] == pytest.approx(
        4 * budgets.DEFAULT_SETTINGS["skill_lab_eval_task_usd"])
    assert notice["remaining_usd"] == pytest.approx(0.2)


def test_skill_lab_units_follow_the_trainer_batch():
    from app.services.job_budget import skill_lab_units

    assert skill_lab_units("train", [], {"epochs": 2}, {"train": 9}) == 2 * 3
    assert skill_lab_units("taskgen", [], {}, {}) == 1
    assert skill_lab_units("eval", [], {"limit": 2}, {"tasks": 5}) == 2


def test_admins_edit_the_job_estimates_through_the_budget_settings(app):
    with TestClient(app, client=PEER) as admin:
        assert admin.post("/api/auth/login", json=ADMIN).status_code == 200
        view = admin.get("/api/budgets/settings").json()
        assert view["defaults"]["skill_lab_train_step_usd"] == 1.0
        saved = admin.put("/api/budgets/settings", json={
            "skill_lab_train_step_usd": 2.5, "persona_actor_input_tokens": 4000}).json()
        assert saved["skill_lab_train_step_usd"] == 2.5
        assert saved["persona_actor_input_tokens"] == 4000
        assert saved["opaque_turn_estimate_usd"] == 0.01  # untouched
        reset = admin.put("/api/budgets/settings",
                          json={"skill_lab_train_step_usd": None}).json()
        assert reset["skill_lab_train_step_usd"] == 1.0
        assert admin.put("/api/budgets/settings",
                         json={"skill_lab_eval_task_usd": 101}).status_code == 422
    _member_id, member = _member(app, "nosy")
    assert member.get("/api/budgets/settings").status_code == 403
