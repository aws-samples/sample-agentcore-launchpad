"""Paired replay: every question answered by both Harness versions, compared pair by pair."""

import re
from unittest.mock import MagicMock

import pytest

from app.core.db import SessionLocal
from app.optimization import canary_harness
from app.optimization import canary_service as canary_svc
from app.optimization.models import RuntimeCanary
from tests.optimization.test_harness_canaries import _agent, _canary, _setup_artifact

GSR = "Builtin.GoalSuccessRate"
HELP = "Builtin.Helpfulness"


def test_sign_flip_p_value_is_exact_for_small_sets():
    assert canary_harness.paired_sign_flip_p([]) == 1.0
    assert canary_harness.paired_sign_flip_p([0.0, 0.0]) == 1.0
    # 5 identical positive diffs: only the all-+ / all-- patterns reach |sum|
    assert canary_harness.paired_sign_flip_p([0.2] * 5) == pytest.approx(2 / 32)
    # balanced diffs are no evidence at all
    assert canary_harness.paired_sign_flip_p([0.5, -0.5]) == 1.0


def test_sign_flip_p_value_is_stable_for_large_sets():
    diffs = [0.3] * 20 + [-0.1] * 5
    first = canary_harness.paired_sign_flip_p(diffs)
    assert first == canary_harness.paired_sign_flip_p(diffs) and first < 0.01


def _pairs(n):
    return [{"scenario_id": f"S{i:02d}", "prompt": f"q{i}", "control_session_id": f"c{i}",
             "treatment_session_id": f"t{i}"} for i in range(n)]


def test_paired_metrics_count_only_questions_scored_on_both_sides():
    pairs = _pairs(3)
    scores = {"c0": {GSR: 0.5}, "t0": {GSR: 1.0}, "c1": {GSR: 1.0}, "t1": {GSR: 1.0},
              "c2": {GSR: 0.0}}  # t2 never scored → left out
    metrics, rows = canary_harness.paired_metrics(pairs, scores, polarity=lambda _e: 1)
    (m,) = metrics
    variant = m["variants"][0]
    assert m["control"] == {"name": "C", "mean": 0.75, "sampleSize": 2}
    assert variant["mean"] == 1.0 and variant["pairs"] == 2
    assert (variant["wins"], variant["losses"], variant["ties"]) == (1, 0, 1)
    assert variant["meanDiff"] == 0.25 and m["paired"] is True
    assert rows[2]["treatment"] == {} and rows[0]["treatment"] == {GSR: 1.0}


def test_polarity_orients_wins_for_lower_is_better_evaluators():
    pairs = _pairs(2)
    scores = {"c0": {"Builtin.Refusal": 1.0}, "t0": {"Builtin.Refusal": 0.0},
              "c1": {"Builtin.Refusal": 1.0}, "t1": {"Builtin.Refusal": 0.0}}
    (m,), _ = canary_harness.paired_metrics(pairs, scores, polarity=lambda _e: -1)
    assert m["variants"][0]["wins"] == 2 and m["variants"][0]["meanDiff"] == -1.0


def test_the_scores_query_reads_both_arms_by_session():
    q = canary_harness.paired_scores_query(["oe-c", "oe-t"], ["c0", "t0"])
    assert 'onlineEvaluationConfigId in ["oe-c", "oe-t"]' in q and 'sid in ["c0", "t0"]' in q
    assert canary_harness.parse_paired_scores(
        [{"sid": "c0", "evaluator": GSR, "mean": "0.5"}, {"sid": "c0", "evaluator": HELP,
                                                          "mean": "x"}]) == {"c0": {GSR: 0.5}}


def test_verdict_of_a_paired_round_compares_question_by_question(monkeypatch):
    agent_id = _agent()
    canary_id = _canary(agent_id)
    pairs = _pairs(6)
    pairs[5]["error"] = "control: harness.incomplete_response"
    db = SessionLocal()
    row = db.get(RuntimeCanary, canary_id)
    row.artifacts = {**row.artifacts, "setup": {**_setup_artifact(canary_id), "ramp_stage": 1},
                     "rounds": [{"ramp_stage": 1, "weights": {}, "traffic_attempts": [
                         {"mode": "paired", "pairs": pairs, "sent": 11, "failed": 1,
                          "baseline_n": 0, "completed_at": "2026-10-04T05:00:00+00:00"}]}]}
    db.commit()
    db.close()
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: MagicMock())
    seen: list[str] = []

    def insights(queries, hours, **_kw):
        seen.extend(queries.values())
        rows = []
        for i in range(5):
            rows += [{"sid": f"c{i}", "evaluator": GSR, "mean": "0.4"},
                     {"sid": f"t{i}", "evaluator": GSR, "mean": "0.9"}]
        return {"q0": rows}

    monkeypatch.setattr("app.services.observability.run_insights_queries", insights)

    verdict = canary_svc.act_verdict(canary_id, lambda _m: None)

    assert verdict["mode"] == "paired" and verdict["pairs_sent"] == 6
    assert verdict["pairs_complete"] == 5  # the failed pair is left out
    assert verdict["verdict"] == "treatment-wins" and verdict["significant"] is False
    variant = verdict["metrics"][0]["variants"][0]
    assert variant["pairs"] == 5 and variant["wins"] == 5
    assert variant["pValue"] == pytest.approx(0.0625)
    assert "c5" not in seen[0] and '"oe-c", "oe-t"' in seen[0]
    db = SessionLocal()
    stored = db.get(RuntimeCanary, canary_id).artifacts["rounds"][0]["verdict"]
    db.close()
    assert stored["pairs"][0]["scenario_id"] == "S00"


def test_budget_stop_only_reads_every_side_of_a_pair_error():
    assert canary_harness.budget_stop_only("treatment: harness.execution_limit")
    assert canary_harness.budget_stop_only(
        "control: harness.execution_timeout, treatment: harness.execution_limit")
    assert not canary_harness.budget_stop_only(
        "control: harness.execution_limit, treatment: ThrottlingException")
    assert not canary_harness.budget_stop_only(None)
    assert not canary_harness.budget_stop_only("")


def test_a_budget_stopped_side_keeps_its_pair_in_the_verdict(monkeypatch):
    """A version that ran out of its own budget lost that question: its session is
    scored as it stands, never dropped (that would hide a regression)."""
    agent_id = _agent()
    canary_id = _canary(agent_id)
    pairs = _pairs(4)
    pairs[3]["error"] = "treatment: harness.execution_limit"
    db = SessionLocal()
    row = db.get(RuntimeCanary, canary_id)
    row.artifacts = {**row.artifacts, "setup": {**_setup_artifact(canary_id), "ramp_stage": 1},
                     "rounds": [{"ramp_stage": 1, "weights": {}, "traffic_attempts": [
                         {"mode": "paired", "pairs": pairs, "sent": 8, "failed": 1,
                          "baseline_n": 0, "completed_at": "2026-10-04T05:00:00+00:00"}]}]}
    db.commit()
    db.close()
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: MagicMock())

    def insights(queries, hours, **_kw):
        rows = []
        for i in range(4):
            rows += [{"sid": f"c{i}", "evaluator": GSR, "mean": "0.8"},
                     {"sid": f"t{i}", "evaluator": GSR, "mean": "0.0" if i == 3 else "0.8"}]
        return {"q0": rows}

    monkeypatch.setattr("app.services.observability.run_insights_queries", insights)

    verdict = canary_svc.act_verdict(canary_id, lambda _m: None)

    assert verdict["pairs_complete"] == 4
    variant = verdict["metrics"][0]["variants"][0]
    assert variant["pairs"] == 4 and variant["losses"] == 1 and variant["ties"] == 3
    stopped = [r for r in verdict["pairs"] if r["budget_stop"]]
    assert [(r["scenario_id"], r["error"]) for r in stopped] == [("S03", None)]


def test_paired_scores_query_never_redefines_a_field():
    """Logs Insights refuses an alias that names an existing ephemeral field
    (live 2026-10-04: `stats avg(score) as score` failed the first paired verdict)."""
    query = canary_harness.paired_scores_query(["oe-c", "oe-t"], ["s1", "s2"])
    aliases = re.findall(r"\bas (\w+)", query)
    assert len(aliases) == len(set(aliases)), aliases
    assert "avg(score) as mean" in query
