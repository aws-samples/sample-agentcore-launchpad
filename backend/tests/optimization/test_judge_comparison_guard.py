"""P3.2 rest (Round 2 item 5) — a comparison never mixes judges silently.

A canary verdict pools every scored record of an assigned session, whichever job wrote it
(``routing.ab_results``); a recommendation's optimizer scored its candidates with the hub
judge of TODAY against a baseline run scored with the run's own judge; ``/runs/compare``
showed two runs side by side whatever judged them. Now: canary rows carry their judge, an
evaluator whose rows come from more than one judge is flagged and kept out of the verdict
(a mixed primary refuses the verdict), the optimizer uses the run's judge, and the run
compare names both judges with ``judge_mismatch``.
"""

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation.models import EvalResult
from app.optimization import routing, verdict
from app.optimization.models import ExperimentAssignment
from tests.evaluation.test_scorecard import (
    _agent,
    _run,
    results,  # noqa: F401  (fixture)
)
from tests.optimization.test_routing import AGENT, _mk_live_canary

GLM, KIMI = "global.zai.glm-5.3", "global.moonshotai.kimi-k3"


def _row(sid, evaluator, score, judge):
    return EvalResult(id=f"{sid}-{evaluator}-{judge}", workspace_id=DEFAULT_WORKSPACE_ID,
                      source="run", run_id="tr1", session_id=sid, evaluator_id=evaluator,
                      level="TRACE", score=score, judge_model_id=judge,
                      judge_inference={"model_id": judge} if judge else None)


def _seed(judges_c, judges_t, evaluator="Builtin.Helpfulness"):
    exp = _mk_live_canary()
    db = SessionLocal()
    try:
        for i, (jc, jt) in enumerate(zip(judges_c, judges_t, strict=True)):
            db.add(ExperimentAssignment(owner_kind="canary", owner_id=exp.id,
                                        session_id=f"c{i}", arm="C"))
            db.add(ExperimentAssignment(owner_kind="canary", owner_id=exp.id,
                                        session_id=f"t{i}", arm="T1"))
            db.add(_row(f"c{i}", evaluator, 0.5, jc))
            db.add(_row(f"t{i}", evaluator, 0.9, jt))
        db.commit()
        return routing.ab_results(db, routing.active_owner(db, AGENT))
    finally:
        db.close()


def test_one_judge_on_both_arms_is_not_flagged():
    out = _seed([GLM] * 6, [GLM] * 6)
    (metric,) = out["results"]["evaluatorMetrics"]
    assert metric["judges"] == [GLM] and metric["judgeMismatch"] is False


def test_arms_scored_by_different_judges_are_flagged_and_left_out_of_the_verdict():
    out = _seed([GLM] * 6, [KIMI] * 6)
    (metric,) = out["results"]["evaluatorMetrics"]
    assert metric["judges"] == sorted([GLM, KIMI]) and metric["judgeMismatch"] is True
    metrics = routing.normalize_ab_results(out)
    assert metrics[0]["judgeMismatch"] is True
    result = verdict.compute_verdict(metrics, min_arm_n=3)
    assert result["verdict"] == "judge-mismatch"
    assert GLM in result["reason"] and KIMI in result["reason"]
    primary = verdict.compute_verdict(metrics, min_arm_n=3, primary="Builtin.Helpfulness")
    assert primary["verdict"] == "judge-mismatch"


def test_unstamped_legacy_rows_count_as_unknown_not_as_another_judge():
    out = _seed([GLM, None, GLM, None, GLM, GLM], [None] * 6)
    (metric,) = out["results"]["evaluatorMetrics"]
    assert metric["judges"] == [GLM] and metric["judgeMismatch"] is False


def test_a_mixed_evaluator_is_excluded_while_the_clean_ones_decide():
    clean = {"evaluatorId": "Builtin.Correctness", "polarity": 1,
             "control": {"mean": 0.4, "sampleSize": 6},
             "variants": [{"mean": 0.9, "sampleSize": 6, "isSignificant": True}],
             "judgeMismatch": False}
    mixed = {"evaluatorId": "Builtin.Helpfulness", "polarity": 1,
             "control": {"mean": 0.9, "sampleSize": 6},
             "variants": [{"mean": 0.1, "sampleSize": 6, "isSignificant": True}],
             "judgeMismatch": True, "judges": [GLM, KIMI]}
    result = verdict.compute_verdict([clean, mixed], min_arm_n=3)
    assert result["verdict"] == "treatment-wins"
    assert result["excluded_judge_mismatch"] == ["Builtin.Helpfulness"]


def test_a_judge_mismatch_verdict_blocks_advance_and_complete():
    from app.optimization import canary_service

    row = _mk_live_canary()
    row.artifacts = {**row.artifacts, "rounds": [{"stage": 1, "verdict": {
        "verdict": "judge-mismatch", "reason": "mixed"}}]}
    for terminal in (False, True):
        with pytest.raises(AppError) as caught:
            canary_service.assert_verdict_allows(row, allow_non_significant=True,
                                                 terminal=terminal)
        assert caught.value.code == "canary.verdict_blocked"


def test_canary_traffic_rows_are_stamped_with_the_jobs_judge():
    from app.optimization import traffic
    from app.optimization.routing import OwnerRef

    owner = OwnerRef(kind="canary", id="can_x", workspace_id=DEFAULT_WORKSPACE_ID,
                     agent_id=AGENT, arms=(), execution_status="RUNNING")
    db = SessionLocal()
    try:
        traffic.ingest_records(db, owner, "tr_job1", [
            {"session_id": "s1", "evaluator_id": "Builtin.Helpfulness", "score": 1.0,
             "id": "rec-1"},
            {"session_id": "s1", "evaluator_id": "Builtin.TrajectoryAnyOrderMatch",
             "score": 1.0, "id": "rec-2"},
        ], {}, judge_model_id=KIMI)
        rows = {r.evaluator_id: r for r in db.query(EvalResult).filter_by(run_id="tr_job1")}
    finally:
        db.close()
    assert rows["Builtin.Helpfulness"].judge_model_id == KIMI
    assert rows["Builtin.Helpfulness"].judge_inference["temperature_applied"] is False
    assert rows["Builtin.TrajectoryAnyOrderMatch"].judge_model_id is None


# ── run compare + recommendation optimizer ─────────────────────────────────────────


@pytest.mark.parametrize("judge_b,mismatch", [(GLM, False), (KIMI, True), (None, False)])
def test_run_compare_names_both_judges(client, results, judge_b, mismatch):  # noqa: F811
    agent = _agent()
    a = _run(agent, dataset_name="window:24h", judge_model_id=GLM)
    b = _run(agent, dataset_name="window:24h", judge_model_id=judge_b)
    body = client.get(f"/api/eval/runs/compare?a={a}&b={b}").json()
    assert body["judges"] == {"a": GLM, "b": judge_b}
    assert body["judge_mismatch"] is mismatch


def test_the_optimizer_scores_with_the_runs_own_judge():
    from app.evaluation import recommendations
    from app.evaluation.models import EvalRun
    from tests.conftest import ws_ctx

    run = EvalRun(id="r_opt1", agent_id="a", agent_name="n", workspace_id=DEFAULT_WORKSPACE_ID,
                  evaluators=["Builtin.Helpfulness"], judge_model_id=KIMI)
    db = SessionLocal()
    try:
        extras = recommendations.optimize_extras(db, run, None, ws_ctx(resources={
            "artifacts_bucket": "b"}), "opt-job-1", "Builtin.Helpfulness")
    finally:
        db.close()
    assert extras["judge_model_id"] == KIMI and extras["judge_max_tokens"] == 2048
