"""N17-obs453 — a recommendation names an evaluator only when its provider optimised toward it.

GEPA-lite reflects on EVERY evaluator record of the run and takes no target, yet its
recommendations stored ``evaluator = Builtin.GoalSuccessRate`` — the form's hidden default —
and the card showed it (live: recommendation 01d2ce55dbba on run 645048cbe321, whose
evaluators did not include GoalSuccessRate). Each provider now declares
``target_evaluator`` (gepa_lite False; harness_optimizer True — its ``optimize`` job is
rewarded by ``evaluator_def``). A provider without a target stores ``evaluator`` null (a sent
one is ignored); one with a target validates and stores it (the default when none is sent —
that default IS what it optimises). The payload's additive ``optimized_toward`` is computed on
read from the row's provider, so existing rows display right with no migration.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.evaluation import recommendations as recs
from app.evaluation.models import EvalRecommendation
from app.optimization import providers as rec_providers
from tests.evaluation.test_run_recommendations import _agent, _rec, _rows, _run, _stub


def test_providers_declare_whether_they_take_a_target():
    listed = {p["id"]: p for p in rec_providers.describe_providers()}
    assert listed["gepa_lite"]["target_evaluator"] is False
    assert listed["harness_optimizer"]["target_evaluator"] is True


def test_gepa_lite_stores_no_evaluator_even_when_one_is_sent(client, monkeypatch):
    _stub(monkeypatch)
    run_id = _run(_agent())
    for body in ({}, {"evaluator": "Builtin.Helpfulness"}):
        res = client.post(f"/api/eval/runs/{run_id}/recommendations", json={
            "kinds": ["system_prompt"], "system_prompt": "x", **body})
        assert res.status_code == 201, res.text
        row = res.json()["recommendations"][0]
        assert (row["evaluator"], row["optimized_toward"]) == (None, None)


def test_a_target_provider_stores_and_names_what_it_optimised(client, monkeypatch):
    executor = _stub(monkeypatch)
    monkeypatch.setattr(get_settings(), "prompt_opt_harness_optimizer_enabled", True)
    run_id = _run(_agent())
    picked = client.post(f"/api/eval/runs/{run_id}/recommendations", json={
        "kinds": ["system_prompt"], "system_prompt": "x", "provider": "harness_optimizer",
        "evaluator": "Builtin.Helpfulness"}).json()["recommendations"][0]
    assert (picked["evaluator"], picked["optimized_toward"]) == (
        "Builtin.Helpfulness", "Builtin.Helpfulness")
    assert executor.submitted[-1].spec["evaluator_def"]["id"] == "Builtin.Helpfulness"
    default = client.post(f"/api/eval/runs/{run_id}/recommendations", json={
        "kinds": ["system_prompt"], "system_prompt": "x", "provider": "harness_optimizer",
    }).json()["recommendations"][0]
    assert default["optimized_toward"] == recs.DEFAULT_EVALUATOR  # what it really optimised
    assert executor.submitted[-1].spec["evaluator_def"]["id"] == recs.DEFAULT_EVALUATOR


def test_an_existing_gepa_lite_row_keeps_its_data_but_names_no_target(client, monkeypatch):
    _stub(monkeypatch)
    run_id = _run(_agent())
    _rec(run_id, evaluator="Builtin.GoalSuccessRate",
         result={"provider": "gepa_lite", "recommended_prompt": "new"})
    row = _rows(client, run_id)[0]
    assert row["evaluator"] == "Builtin.GoalSuccessRate"  # stored as it was — no migration
    assert row["optimized_toward"] is None


def test_a_row_without_a_provider_keeps_its_evaluator():
    db = SessionLocal()
    try:
        row = EvalRecommendation(run_id="r", kind="system_prompt", recommendation_id="x",
                                 name="n", status="COMPLETED", evaluator="Builtin.Correctness",
                                 result={})
        assert recs.out(row)["optimized_toward"] == "Builtin.Correctness"
        tools = EvalRecommendation(run_id="r", kind="tool_descriptions", recommendation_id="y",
                                   name="n", status="COMPLETED", evaluator=None,
                                   result={"provider": "harness_optimizer"})
        assert recs.out(tools)["optimized_toward"] is None
    finally:
        db.close()
