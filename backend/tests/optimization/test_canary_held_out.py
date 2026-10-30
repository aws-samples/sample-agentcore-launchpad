"""R3-c / D3-d — the canary and the dataset's held-out split (with r17-bj's
``scenarios.item_splits``).

Canary traffic replays ALL scenarios (it is acceptance, lead ruling 2026-10-24); the verdict
decides on all of them and REPORTS the held-out subset separately when the dataset has one:
the traffic attempt records which of its sessions replayed a held-out scenario, and the
verdict carries ``held_out`` — the primary's arms, delta and p over those sessions only.
"""

from __future__ import annotations

from types import SimpleNamespace

import app.optimization.canary_routers as canary_routers
import app.optimization.canary_service as canary_svc
from app.core.db import SessionLocal
from app.evaluation.models import EvalResult
from app.optimization import routing
from app.optimization.models import ExperimentAssignment
from app.optimization.verdict import held_out_block
from tests.optimization.test_runtime_canaries import (
    _live_artifacts,
    _mk_canary,
    _round,
)

PRIMARY = "Builtin.Helpfulness"


def test_the_held_out_labels_come_from_the_dataset_splits(monkeypatch):
    items = [{"scenario_id": "s1"}, {"scenario_id": "s2"}, {"prompt": "legacy"}]
    monkeypatch.setattr(canary_routers.scenarios, "item_splits",
                        lambda its: ["tuning", "held_out", "held_out"], raising=False)
    dataset = SimpleNamespace(items=items)
    assert canary_routers.held_out_labels(dataset) == {"s2", "item_3"}
    monkeypatch.delattr(canary_routers.scenarios, "item_splits", raising=False)
    assert canary_routers.held_out_labels(dataset) == set()  # no split support: nothing held out


def test_a_cycled_session_keeps_its_scenarios_split():
    sessions = [{"session_id": "a", "scenario_id": "s1"}, {"session_id": "b", "scenario_id": "s2"},
                {"session_id": "c", "scenario_id": "s2#2"}]
    assert canary_svc.held_out_sessions(sessions, {"s2"}) == ["b", "c"]
    assert canary_svc.held_out_sessions(sessions, set()) == []


def _assign(owner_id, session, arm, score):
    with SessionLocal() as db:
        db.add(ExperimentAssignment(owner_kind="canary", owner_id=owner_id,
                                    session_id=session, arm=arm))
        db.add(EvalResult(id=f"r-{session}", source="run", run_id="tr", session_id=session,
                          evaluator_id=PRIMARY, score=score))
        db.commit()


def test_ab_results_can_be_restricted_to_a_session_subset():
    row = _mk_canary(artifacts=_live_artifacts(rounds=[_round(0)]))
    for i in range(4):
        _assign(row.id, f"c{i}", "C", 0.5)
        _assign(row.id, f"t{i}", "T1", 0.9 if i < 2 else 0.5)
    owner = routing.owner_from_canary(canary_svc._get(row.id))
    with SessionLocal() as db:
        every = routing.normalize_ab_results(routing.ab_results(db, owner))
        subset = routing.normalize_ab_results(routing.ab_results(
            db, owner, sessions={"c0", "c1", "t0", "t1"}))
    assert every[0]["control"]["sampleSize"] == 4
    assert subset[0]["control"]["sampleSize"] == 2
    assert subset[0]["variants"][0]["mean"] == 0.9


def test_the_verdict_reports_the_held_out_subset_separately(monkeypatch):
    attempt_round = _round(0, baseline_n=0)
    attempt_round["traffic_attempts"][0]["held_out_sessions"] = ["c0", "t0"]
    row = _mk_canary(artifacts=_live_artifacts(rounds=[attempt_round],
                                               primary_evaluator=PRIMARY))
    full = [{"evaluatorId": PRIMARY, "label": PRIMARY,
             "control": {"mean": 0.5, "sampleSize": 12},
             "variants": [{"mean": 0.8, "sampleSize": 12, "pValue": 0.001,
                           "isSignificant": True}]}]
    held = [{"evaluatorId": PRIMARY, "label": PRIMARY,
             "control": {"mean": 0.6, "sampleSize": 3},
             "variants": [{"mean": 0.55, "sampleSize": 3, "pValue": 0.7,
                           "isSignificant": False}]}]
    seen: list = []

    def fake(row, sessions=None):
        seen.append(sessions)
        return {"executionStatus": "RUNNING"}, (held if sessions else full)

    monkeypatch.setattr(canary_svc, "ab_metrics", fake)
    monkeypatch.setattr(canary_svc, "_sleep", lambda *_a, **_k: None)
    verdict = canary_svc.act_verdict(row.id, lambda message: None)
    assert verdict["verdict"] == "treatment-wins"  # decided on ALL traffic
    assert set(seen[-1]) == {"c0", "t0"}
    assert verdict["held_out"] == {
        "evaluator": PRIMARY, "arms_n": {"control": 3, "treatment": 3},
        "delta": -0.05, "p": 0.7, "significant": False,
        "note": "reported only — the verdict decides on all replayed scenarios"}


def test_held_out_block_is_absent_without_held_out_sessions():
    assert held_out_block([], PRIMARY) is None


def test_with_the_real_splits_the_labels_follow_the_dataset_defaults():
    """r17-bj's ``item_splits`` resolves over the WHOLE item list (a dataset of fewer than
    five scenarios holds nothing out by default); an explicit split always wins."""
    from app.evaluation import scenarios

    items = [{"scenario_id": f"sc-{i}", "turns": [{"input": f"q{i}"}]} for i in range(20)]
    items[0]["split"] = "held_out"
    expected = {str(s["scenario_id"]) for s, split in zip(
        scenarios.normalize_scenarios(items), scenarios.item_splits(items), strict=True)
        if split == "held_out"}
    labels = canary_routers.held_out_labels(SimpleNamespace(items=items))
    assert labels == expected and "sc-0" in labels
    small = [{"scenario_id": f"x{i}", "turns": [{"input": "q"}]} for i in range(3)]
    assert canary_routers.held_out_labels(SimpleNamespace(items=small)) == set()
