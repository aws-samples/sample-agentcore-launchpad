"""Ramp weights + the honest small-n verdict (N12 W-E3; canary-owned since N13-obs2) —
adapted from agentcore_eva_opt tests/test_weights_pause_resume.py."""

from app.optimization.canary_service import RAMP_WEIGHTS
from app.optimization.verdict import compute_verdict


def test_ramp_steps_progression():
    assert RAMP_WEIGHTS == ((90, 10), (50, 50), (1, 99))


def test_verdict_honest_about_small_n():
    assert compute_verdict([])["verdict"] == "insufficient-data"
    tiny = [{
        "control": {"mean": 0.8, "sampleSize": 1},
        "variants": [{"mean": 0.9, "sampleSize": 1}],
    }]
    assert compute_verdict(tiny)["verdict"] == "insufficient-n"
    # N-SCN-obs14: each arm needs Settings.verdict_min_arm_n (10) scored records
    enough = [{
        "control": {"mean": 0.8, "sampleSize": 10},
        "variants": [{"mean": 0.9, "sampleSize": 10, "isSignificant": True}],
    }]
    verdict = compute_verdict(enough)
    assert verdict["verdict"] == "treatment-wins" and verdict["significant"] is True


# ── N17-obs388 follow-up: a float-noise delta is a tie, not a winner ───────────────


def _constant_metrics() -> list[dict]:
    """eval-wb C3-nochange r1 after obs387/obs388: constant arms 45 / 12 sessions whose
    means differ only by float summation (0.833 vs 0.8329999999999999)."""
    from app.optimization import stats

    control, treatment = [0.833] * 45, [0.833] * 12
    c_mean = sum(control) / len(control)  # the plain sum ab_results used to report
    _t, _df, p = stats.welch_two_sided(control, treatment)
    return [{"evaluatorId": "Builtin.Helpfulness", "label": "Builtin.Helpfulness",
             "polarity": 1,
             "control": {"name": "C", "mean": c_mean, "sampleSize": 45},
             "variants": [{"name": "T1", "mean": stats.mean(treatment), "sampleSize": 12,
                           "pValue": p, "isSignificant": p < 0.05}]}]


def test_constant_identical_arms_are_a_tie_on_both_verdict_paths():
    metrics = _constant_metrics()
    c, t = metrics[0]["control"]["mean"], metrics[0]["variants"][0]["mean"]
    assert c != t  # the bare sign of this delta used to name a winner
    assert compute_verdict(metrics)["verdict"] == "tie"
    assert compute_verdict(metrics, primary="Builtin.Helpfulness")["verdict"] == "tie"


def test_a_tie_on_identical_arms_may_complete_under_override():
    from types import SimpleNamespace

    import pytest

    from app.core.errors import AppError
    from app.optimization import canary_service

    verdict = compute_verdict(_constant_metrics(), primary="Builtin.Helpfulness")
    row = SimpleNamespace(artifacts={"setup": {"ramp_stage": 1},
                                     "rounds": [{"ramp_stage": 1, "verdict": verdict}]})
    canary_service.assert_verdict_allows(row, allow_non_significant=True, terminal=True)
    with pytest.raises(AppError) as exc:  # a tie still needs the explicit override
        canary_service.assert_verdict_allows(row, allow_non_significant=False, terminal=True)
    assert exc.value.code == "canary.override_required"
