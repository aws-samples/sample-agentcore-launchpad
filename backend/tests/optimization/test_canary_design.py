"""R3-d — a canary is a designed experiment: power before start, a fixed n, one deciding
evaluator, multiple-comparison honesty for the rest.

* **Power.** ``stats.sessions_per_arm(mde, sd, alpha, power)`` is the two-sample normal
  approximation ``n = 2 (z₁₋α/₂ + z₁₋β)² σ² / Δ²`` per arm; the console shows it before start
  and ``POST /api/runtime-canaries`` stores the design (``planned_n_per_arm``) on the row.
* **Fixed n, no peeking.** With a design the primary evaluator is tested ONCE, when both arms
  reach the planned n: before that the verdict is ``insufficient-n`` whatever the p-value
  says; at the planned n a directional verdict needs ``p < alpha`` (else ``tie``: no
  significant difference); the decision is frozen — later rounds never re-test.
* **Primary only.** The primary decides; the other evaluators ride as ``secondary`` with
  Holm-adjusted p-values and a note that they are reference only.
"""

from __future__ import annotations

import math

import pytest

import app.optimization.canary_service as canary_svc
from app.optimization import stats
from app.optimization.verdict import compute_verdict
from tests.optimization.test_runtime_canaries import (
    _live_artifacts,
    _mk_canary,
    _reload,
    _round,
)

PRIMARY = "Builtin.Helpfulness"


def _metric(evaluator, c_mean, t_mean, n, p):
    return {"evaluatorId": evaluator, "label": evaluator,
            "control": {"mean": c_mean, "sampleSize": n},
            "variants": [{"mean": t_mean, "sampleSize": n, "pValue": p,
                          "isSignificant": p < 0.05}]}


DESIGN = {"mde": 0.2, "sd": 0.3, "alpha": 0.05, "power": 0.8, "planned_n_per_arm": 36}


# ─── power ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("p", "z"), [(0.975, 1.959964), (0.8, 0.841621), (0.5, 0.0),
                                      (0.05, -1.644854), (0.995, 2.575829)])
def test_normal_quantile(p, z):
    assert stats.normal_quantile(p) == pytest.approx(z, abs=1e-5)


@pytest.mark.parametrize(("mde", "sd", "alpha", "power", "n"), [
    (0.2, 0.3, 0.05, 0.8, 36),     # 2·(1.96+0.84)²·0.09/0.04 = 35.3
    (0.1, 0.3, 0.05, 0.8, 142),    # half the effect → four times the sessions
    (0.2, 0.3, 0.05, 0.9, 48),
    (0.3, 0.5, 0.01, 0.8, 65),     # 2·(2.576+0.842)²·0.25/0.09 = 64.9
])
def test_sessions_per_arm(mde, sd, alpha, power, n):
    assert stats.sessions_per_arm(mde, sd=sd, alpha=alpha, power=power) == n


@pytest.mark.parametrize("bad", [{"mde": 0}, {"mde": 1.5}, {"mde": 0.2, "sd": 0},
                                 {"mde": 0.2, "alpha": 0.6}, {"mde": 0.2, "power": 0.3}])
def test_sessions_per_arm_refuses_meaningless_inputs(bad):
    with pytest.raises(ValueError):
        stats.sessions_per_arm(**bad)


def test_holm_adjusts_in_step_down_order():
    assert stats.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert stats.holm([]) == []
    assert stats.holm([0.9, 0.8]) == pytest.approx([1.0, 1.0])  # capped at 1, monotone


# ─── fixed n ─────────────────────────────────────────────────────────────────────────


def test_before_the_planned_n_a_significant_result_is_not_a_verdict():
    """No peeking: 20 sessions per arm with p = 0.001 is still insufficient-n."""
    v = compute_verdict([_metric(PRIMARY, 0.4, 0.9, 20, 0.001)], primary=PRIMARY,
                        design=DESIGN)
    assert v["verdict"] == "insufficient-n"
    assert "36 per arm" in v["reason"] and "control has 20" in v["reason"]


def test_at_the_planned_n_a_non_significant_difference_is_a_tie():
    v = compute_verdict([_metric(PRIMARY, 0.60, 0.66, 36, 0.21)], primary=PRIMARY,
                        design=DESIGN)
    assert v["verdict"] == "tie" and v["significant"] is False
    assert "no significant difference" in v["reason"] and "0.21" in v["reason"]


def test_at_the_planned_n_a_significant_difference_decides_by_its_sign():
    up = compute_verdict([_metric(PRIMARY, 0.5, 0.8, 40, 0.003)], primary=PRIMARY,
                         design=DESIGN)
    down = compute_verdict([_metric(PRIMARY, 0.8, 0.5, 40, 0.003)], primary=PRIMARY,
                           design=DESIGN)
    assert (up["verdict"], down["verdict"]) == ("treatment-wins", "control-wins")
    assert up["decided_at_n"] == {"control": 40, "treatment": 40}


def test_a_design_needs_a_primary():
    v = compute_verdict([_metric(PRIMARY, 0.5, 0.8, 40, 0.003)], primary=None, design=DESIGN)
    assert v["verdict"] == "insufficient-data" and "primary" in v["reason"]


# ─── primary only + Holm ─────────────────────────────────────────────────────────────


def test_secondary_evaluators_are_reference_only_with_holm_adjusted_p():
    metrics = [_metric(PRIMARY, 0.6, 0.62, 40, 0.5),
               _metric("Builtin.Correctness", 0.5, 0.7, 40, 0.02),
               _metric("Builtin.Conciseness", 0.5, 0.6, 40, 0.03)]
    v = compute_verdict(metrics, primary=PRIMARY, design=DESIGN)
    assert v["verdict"] == "tie"  # the secondaries never decide, whatever their p
    secondary = {s["evaluator"]: s for s in v["secondary"]}
    assert set(secondary) == {"Builtin.Correctness", "Builtin.Conciseness"}
    assert secondary["Builtin.Correctness"]["p_holm"] == pytest.approx(0.04)
    assert secondary["Builtin.Conciseness"]["p_holm"] == pytest.approx(0.04)
    assert all(s["significant_holm"] for s in secondary.values())
    assert "reference only" in v["multiple_comparisons"] and "Holm" in v["multiple_comparisons"]


def test_a_legacy_canary_without_a_design_keeps_its_verdict_and_gains_the_note():
    metrics = [_metric(PRIMARY, 0.5, 0.8, 12, 0.003), _metric("Builtin.Correctness",
                                                              0.5, 0.6, 12, 0.2)]
    v = compute_verdict(metrics, primary=PRIMARY)
    assert v["verdict"] == "treatment-wins" and "design" not in v
    assert [s["evaluator"] for s in v["secondary"]] == ["Builtin.Correctness"]


# ─── the canary row: design stored at create, decision frozen ────────────────────────


def test_the_decision_is_frozen_once_taken(monkeypatch):
    artifacts = _live_artifacts(rounds=[_round(0, baseline_n=0)], primary_evaluator=PRIMARY,
                                design=dict(DESIGN))
    row = _mk_canary(artifacts=artifacts)
    reads = [[_metric(PRIMARY, 0.5, 0.8, 36, 0.003)]]
    monkeypatch.setattr(canary_svc, "ab_metrics",
                        lambda row: ({"executionStatus": "RUNNING"}, reads[-1]))
    monkeypatch.setattr(canary_svc, "_sleep", lambda *_a, **_k: None)
    first = canary_svc.act_verdict(row.id, lambda message: None)
    assert first["verdict"] == "treatment-wins"
    assert _reload(row.id).artifacts["design"]["decision"]["verdict"] == "treatment-wins"
    # more data that would flip it (a peek) is never re-tested
    reads.append([_metric(PRIMARY, 0.8, 0.5, 80, 0.0001)])
    _bump_baseline(row.id)
    again = canary_svc.act_verdict(row.id, lambda message: None)
    assert again["verdict"] == "treatment-wins" and again["frozen"] is True


def _bump_baseline(canary_id: str) -> None:
    from app.core.db import SessionLocal
    from app.optimization.models import RuntimeCanary

    with SessionLocal() as db:
        row = db.get(RuntimeCanary, canary_id)
        arts = dict(row.artifacts)
        rounds = [dict(r) for r in arts["rounds"]]
        attempt = dict(rounds[-1]["traffic_attempts"][-1])
        attempt["baseline_n"] = 72
        rounds[-1]["traffic_attempts"] = [attempt]
        arts["rounds"] = rounds
        row.artifacts = arts
        db.commit()


def _create_payload_and_stubs(monkeypatch) -> dict:
    from unittest.mock import MagicMock

    from tests.optimization.test_runtime_canaries import _agent, _persist_agents

    (agent_id,) = _persist_agents(_agent("subject"))
    control = MagicMock()
    control.get_agent_runtime.side_effect = lambda agentRuntimeId: {
        "agentRuntimeName": f"runtime-{agentRuntimeId}"}
    monkeypatch.setattr(canary_svc, "control_client", lambda _ws=None: control)
    return {"agent_id": agent_id, "candidate": {"system_prompt": "new prompt"}}


def test_create_stores_the_design_and_its_planned_n(client, monkeypatch):
    payload = _create_payload_and_stubs(monkeypatch)
    res = client.post("/api/runtime-canaries", json={
        **payload, "online_evaluators": [PRIMARY], "primary_evaluator": PRIMARY,
        "design": {"mde": 0.2, "sd": 0.3}})
    assert res.status_code == 201, res.text
    design = res.json()["artifacts"]["design"]
    assert design == {"mde": 0.2, "sd": 0.3, "alpha": 0.05, "power": 0.8,
                      "planned_n_per_arm": 36}
    bad = client.post("/api/runtime-canaries", json={
        **payload, "online_evaluators": [PRIMARY], "design": {"mde": 0}})
    assert bad.status_code == 422
    assert math.isfinite(design["planned_n_per_arm"])
