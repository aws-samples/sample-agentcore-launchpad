"""Run cost estimates (pass^k awareness) and run comparison."""

from types import SimpleNamespace

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.dlc import compare as compare_svc
from app.dlc import cost
from app.dlc import criteria as criteria_svc
from app.evaluation.models import EvalRun
from app.models.dlc import CriteriaSet, Criterion, CriterionResult
from app.models.ledger import Agent, Workspace

WS = DEFAULT_WORKSPACE_ID
ITEMS = [{"scenario_id": f"s{n}", "turns": [{"input": "hi"}, {"input": "more"}]}
         for n in range(20)]


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        for model in (CriterionResult, EvalRun, Criterion, CriteriaSet):
            session.query(model).delete()
        session.query(Agent).filter(Agent.name.like("cc-%")).delete()
        ws = session.get(Workspace, WS)
        ws.release_policy = {}
        session.commit()
        yield session
    finally:
        session.rollback()
        ws = session.get(Workspace, WS)
        ws.release_policy = {}
        session.commit()
        session.close()


def _agent(db):
    agent = Agent(workspace_id=WS, name="cc-a", method="harness", status="active",
                  spec={"name": "cc-a", "model_id": "anthropic.claude-sonnet-5-5"},
                  version="1")
    db.add(agent)
    db.commit()
    return agent


def test_estimate_scales_with_repeats_and_flags_confirmation(db, monkeypatch):
    agent = _agent(db)
    monkeypatch.setattr(cost, "_per_session_from_history", lambda *a, **k: (0.02, "history_7d"))
    one = cost.estimate(db, agent=agent, workspace=db.get(Workspace, WS),
                        workspace_ctx=SimpleNamespace(), items=ITEMS,
                        evaluators=["Builtin.Correctness"], repeats=1)
    assert one["sessions"] == 20 and one["agent_usd"] == pytest.approx(0.4)
    assert one["agent_basis"] == "history_7d"
    three = cost.estimate(db, agent=agent, workspace=db.get(Workspace, WS),
                          workspace_ctx=SimpleNamespace(), items=ITEMS,
                          evaluators=["Builtin.Correctness"], repeats=3)
    assert three["sessions"] == 60 and three["agent_usd"] == pytest.approx(1.2)
    assert three["duration_minutes"] > one["duration_minutes"]
    # a builtin judge is billed by AgentCore Evaluations, priced at its own rate
    assert three["judges"][0]["billed_by"] == "agentcore_evaluations"
    assert three["judges"][0]["usd"] > 0 and three["managed_usd"] > 0

    ws = db.get(Workspace, WS)
    ws.release_policy = {"eval_cost_confirm_usd": 1.0, "eval_cost_max_usd": 1.1}
    db.commit()
    gated = cost.estimate(db, agent=agent, workspace=db.get(Workspace, WS),
                          workspace_ctx=SimpleNamespace(), items=ITEMS,
                          evaluators=[], repeats=3)
    assert gated["confirm_required"] is True and gated["over_limit"] is True
    with pytest.raises(AppError) as exc:
        cost.assert_allowed(gated, confirmed=True, is_admin=False)
    assert exc.value.code == "run.cost_over_limit"
    cost.assert_allowed(gated, confirmed=True, is_admin=True)
    gated["over_limit"] = False
    with pytest.raises(AppError) as exc:
        cost.assert_allowed(gated, confirmed=False, is_admin=False)
    assert exc.value.code == "run.cost_confirm_required"
    cost.assert_allowed(gated, confirmed=True, is_admin=False)


def test_history_divides_the_agents_cost_by_its_own_sessions(db, monkeypatch):
    """Other agents' traffic must not dilute this agent's per-session cost."""
    from app.services import costs as cost_service

    agent = _agent(db)
    report = {
        "by_agent": [
            {"agent_id": agent.id, "service": "rt.DEFAULT", "est_cost_usd": 0.03, "sessions": 3},
            {"agent_id": agent.id, "service": "rt.candidate", "est_cost_usd": 0.01, "sessions": 1},
            {"agent_id": "someone-else", "service": "x.DEFAULT", "est_cost_usd": 5.0,
             "sessions": 76},
        ],
        # workspace-wide: 80 sessions across every agent
        "by_actor": [{"actor": "—", "sessions": 80, "tokens": 0, "est_cost_usd": 5.04}],
    }
    monkeypatch.setattr(cost_service, "cost_report", lambda *a, **k: report)
    per_session, basis = cost._per_session_from_history(db, agent, SimpleNamespace())
    assert basis == "history_7d"
    assert per_session == pytest.approx(0.01)  # 0.04 over its own 4 sessions, not 80

    report["by_agent"] = [{"agent_id": agent.id, "est_cost_usd": 0.04, "sessions": 0}]
    assert cost._per_session_from_history(db, agent, SimpleNamespace()) == (None, "no_history")


PRICES = {"global.openai.gpt-6.1-sol": {"input": 2.0, "output": 10.0},
          "claude-sonnet-5-5": {"input": 3.0, "output": 15.0}}


def test_a_custom_judge_is_priced_in_this_account(db, monkeypatch):
    agent = _agent(db)
    monkeypatch.setattr(cost, "_per_session_from_history", lambda *a, **k: (0.0, "history_7d"))
    monkeypatch.setattr(cost, "_price_of", lambda model: PRICES.get(model))
    rows = [Criterion(set_id="s", key="J1", text="t", dimension="quality", tier="observe",
                      level="trace", denominator="sessions",
                      executor={"kind": "evaluator", "evaluator_id": "custom-judge",
                                "evaluator_kind": "judge"})]
    out = cost.estimate(db, agent=agent, workspace=None, workspace_ctx=SimpleNamespace(),
                        items=ITEMS[:5], evaluators=["custom-judge"], repeats=2,
                        criteria_rows=rows)
    assert out["judges"][0]["billed_by"] == "your_account", out
    assert out["judge_usd"] is not None and out["judge_usd"] > 0, out
    # the replay also runs on AgentCore Runtime (eval-cost D-2): judge + runtime
    assert out["total_usd"] == pytest.approx(out["judge_usd"] + out["runtime_usd"], abs=1e-4)


def test_an_unpriced_judge_model_says_so(db, monkeypatch):
    """The test config has no price map: the estimate names that, it does not guess."""
    agent = _agent(db)
    monkeypatch.setattr(cost, "_per_session_from_history", lambda *a, **k: (0.01, "history_7d"))
    rows = [Criterion(set_id="s", key="J1", text="t", dimension="quality", tier="observe",
                      level="trace", denominator="sessions",
                      executor={"kind": "evaluator", "evaluator_id": "custom-judge",
                                "evaluator_kind": "judge"})]
    out = cost.estimate(db, agent=agent, workspace=None, workspace_ctx=SimpleNamespace(),
                        items=ITEMS[:3], evaluators=["custom-judge"], repeats=1,
                        criteria_rows=rows, judge_model_id="global.example.unpriced-judge")
    assert out["judge_usd"] is None
    assert "price map" in out["judges"][0]["note"]


def test_the_default_gpt6_sol_judge_is_priced(db, monkeypatch):
    """GPT-6 Sol is in the default price map (model card, 2026-10-12), so the default
    judge no longer shows up as "N judges not counted"."""
    agent = _agent(db)
    monkeypatch.setattr(cost, "_per_session_from_history", lambda *a, **k: (0.01, "history_7d"))
    rows = [Criterion(set_id="s", key="J1", text="t", dimension="quality", tier="observe",
                      level="trace", denominator="sessions",
                      executor={"kind": "evaluator", "evaluator_id": "custom-judge",
                                "evaluator_kind": "judge"})]
    out = cost.estimate(db, agent=agent, workspace=None, workspace_ctx=SimpleNamespace(),
                        items=ITEMS[:3], evaluators=["custom-judge"], repeats=1,
                        criteria_rows=rows)
    assert out["judge_usd"] is not None and out["judge_usd"] > 0, out
    assert all("price map" not in (j.get("note") or "") for j in out["judges"])
    assert cost._price_of("global.openai.gpt-6.1-sol")["input"] == 2.00
    assert cost._price_of("us.openai.gpt-6-sol")["output"] == 11.00


def test_code_evaluators_cost_nothing_to_judge(db, monkeypatch):
    agent = _agent(db)
    monkeypatch.setattr(cost, "_per_session_from_history", lambda *a, **k: (0.01, "history_7d"))
    rows = [Criterion(set_id="s", key="C1", text="t", dimension="quality", tier="gate",
                      threshold=0.9, level="session", denominator="sessions",
                      executor={"kind": "evaluator", "evaluator_id": "code-x",
                                "evaluator_kind": "code"})]
    out = cost.estimate(db, agent=agent, workspace=None, workspace_ctx=SimpleNamespace(),
                        items=ITEMS, evaluators=["code-x", "Builtin.TrajectoryInOrderMatch"],
                        repeats=1, criteria_rows=rows)
    assert out["judges"] == [] and out["judge_usd"] is None


def test_repeats_are_bounded(db):
    with pytest.raises(AppError):
        cost.estimate(db, agent=None, workspace=None, workspace_ctx=None, items=[],
                      evaluators=[], repeats=99)


# ── comparison ────────────────────────────────────────────────────────────────


def _set(db, agent_id, keys, version_note=""):
    s = criteria_svc.create_set(db, WS, kind="agent", agent_id=agent_id,
                                name=f"c{version_note}", actor="biz")
    criteria_svc.save_draft(db, s, [
        {"key": k, "text": f"agent {k}", "dimension": "quality",
         "tier": "redline" if index == 0 else "gate",
         "threshold": None if index == 0 else 0.9,
         "notes": "n/a:cognition n/a:responsibility n/a:cost n/a:performance (fixture)",
         "executor": {"kind": "evaluator", "evaluator_id": "code-x"}}
        for index, k in enumerate(keys)
    ], actor="biz", kind_of=lambda _e: "code")
    db.commit()
    return s


def _run(db, agent_id, cset, verdicts, *, name="r", status="completed"):
    run = EvalRun(workspace_id=WS, agent_id=agent_id, agent_name="a", status=status, name=name,
                  criteria_set_id=cset.id, criteria_set_version=cset.version, split="regression",
                  session_ids=[f"x{n}" for n in range(len(verdicts))])
    db.add(run)
    db.flush()
    summary = {}
    for (key, scenario), verdict in verdicts.items():
        db.add(CriterionResult(workspace_id=WS, run_id=run.id, criterion_key=key,
                               scenario_id=scenario, attempt=1, session_id=scenario,
                               evaluator_id="code-x", level="session", verdict=verdict))
        entry = summary.setdefault(key, {"kind": "evaluator", "tier": "gate", "pass": 0,
                                         "fail": 0, "n": 0})
        entry[verdict] = entry.get(verdict, 0) + 1
        entry["n"] += 1
    for entry in summary.values():
        entry["rate"] = entry["pass"] / entry["n"] if entry["n"] else None
    run.criteria_summary = {"criteria": summary}
    db.commit()
    return run


def test_compare_lists_fixed_new_and_still_failing(db):
    agent = _agent(db)
    cset = _set(db, agent.id, ["C1"])
    v1 = _run(db, agent.id, cset, {("C1", "s1"): "fail", ("C1", "s2"): "pass",
                                   ("C1", "s3"): "fail"}, name="v1")
    v2 = _run(db, agent.id, cset, {("C1", "s1"): "pass", ("C1", "s2"): "fail",
                                   ("C1", "s3"): "fail"}, name="v2")
    out = compare_svc.compare(db, WS, [v1.id, v2.id])
    assert out["comparable"] is True
    assert [i["scenario_id"] for i in out["fixed"]] == ["s1"]
    assert [i["scenario_id"] for i in out["new_failures"]] == ["s2"]
    assert [i["scenario_id"] for i in out["still_failing"]] == ["s3"]
    entry = out["criteria"][0]
    assert entry["before"]["rate"] == pytest.approx(1 / 3)
    assert entry["after"]["rate"] == pytest.approx(1 / 3)
    assert out["runs"][0]["mean_gate_rate"] == pytest.approx(1 / 3)


def test_the_ladder_warns_when_more_than_one_layer_changed(db):
    agent = _agent(db)
    cset = _set(db, agent.id, ["C1"])
    runs = [_run(db, agent.id, cset, {("C1", "s1"): "fail"}, name=f"r{n}") for n in range(3)]
    specs = {
        runs[0].id: {"system_prompt": "a", "model_id": "m1"},
        runs[1].id: {"system_prompt": "b", "model_id": "m1"},     # one layer
        runs[2].id: {"system_prompt": "c", "model_id": "m2"},     # two layers
    }
    out = compare_svc.compare(db, WS, [r.id for r in runs], specs=specs)
    assert out["runs"][1]["layers_changed"] == ["01"]
    assert out["runs"][2]["layers_changed"] == ["01", "06"]
    assert "more than one layer changed" in out["warning"]


def test_two_numbers_when_the_standard_got_harder(db):
    agent = _agent(db)
    old = _set(db, agent.id, ["C1"])
    v1 = _run(db, agent.id, old, {("C1", "s1"): "pass", ("C1", "s2"): "fail"}, name="v1")
    criteria_svc.publish(db, old, actor="biz")
    db.commit()
    new = criteria_svc.new_version(db, WS, old.lineage_id, actor="biz")
    criteria_svc.save_draft(db, new, [
        {"key": "C1", "text": "agent C1", "dimension": "quality", "tier": "redline",
         "notes": "n/a:cognition n/a:responsibility n/a:cost n/a:performance (fixture)",
         "executor": {"kind": "evaluator", "evaluator_id": "code-x"}},
        {"key": "C2", "text": "agent C2", "dimension": "quality", "tier": "gate",
         "threshold": 0.9, "executor": {"kind": "evaluator", "evaluator_id": "code-x"}},
    ], actor="biz", kind_of=lambda _e: "code")
    db.commit()
    v2 = _run(db, agent.id, new, {("C1", "s1"): "pass", ("C1", "s2"): "pass",
                                  ("C2", "s1"): "fail", ("C2", "s2"): "fail"}, name="v2")
    out = compare_svc.compare(db, WS, [v1.id, v2.id])
    assert out["comparable"] is False and "criteria versions" in out["incomparable_reason"]
    two = out["two_numbers"]
    assert two["added_criteria"] == ["C2"]
    assert two["current_under_old"]["rate"] == pytest.approx(1.0)   # agent improved
    assert two["current_under_new"]["rate"] == pytest.approx(0.5)   # the ruler got longer
    assert two["agent_delta"] == pytest.approx(0.5)
    assert two["standard_delta"] == pytest.approx(-0.5)


def test_comparing_needs_two_runs(db):
    with pytest.raises(AppError):
        compare_svc.compare(db, WS, ["only-one"])


def test_the_fix_ladder_route_compares_the_agents_runs(db, client):
    """Live: GET /agents/{id}/ladder answered 500 on every call — it called the
    `compare_runs` route function directly, so its `Depends(...)` defaults reached
    the visibility check as objects ('Depends' object has no attribute 'filters')."""
    agent = _agent(db)
    cset = _set(db, agent.id, ["C1"])
    v1 = _run(db, agent.id, cset, {("C1", "s1"): "fail"}, name="v1")
    v2 = _run(db, agent.id, cset, {("C1", "s1"): "pass"}, name="v2")
    response = client.get(f"/api/agents/{agent.id}/ladder")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["comparable"] is True
    assert [r["run_id"] for r in body["runs"]] == [v1.id, v2.id]
    assert [i["scenario_id"] for i in body["fixed"]] == ["s1"]


def test_the_fix_ladder_with_one_run_is_not_comparable(db, client):
    agent = _agent(db)
    cset = _set(db, agent.id, ["C1"])
    only = _run(db, agent.id, cset, {("C1", "s1"): "fail"}, name="v1")
    response = client.get(f"/api/agents/{agent.id}/ladder")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["comparable"] is False
    assert body["runs"] == [] and body["run_ids"] == [only.id]  # rungs, never bare ids
