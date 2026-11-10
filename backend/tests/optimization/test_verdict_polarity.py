"""Verdict maths must respect evaluator polarity and sample weight.

``Builtin.Refusal`` (and DeepEval Bias / Toxicity) score a penalty — the
lower-mean arm is the better arm — so a raw ``treatment - control`` average
declares the winning arm the loser. These tests pin the orientation and the
sample-size weighting for both the experiment verdict and the canary
auto-decision that shares ``compute_verdict``.
"""

from app.optimization import routing as ac
from app.optimization.verdict import compute_verdict


# n=12 clears the N-SCN-obs14 per-arm bar (Settings.verdict_min_arm_n = 10) so these
# tests keep exercising the *direction* of the verdict, not the sample gate.
def _metric(evaluator, c_mean, t_mean, n=12, significant=False):
    return {
        "evaluatorId": evaluator,
        "label": evaluator.rsplit("/", 1)[-1],
        "control": {"mean": c_mean, "sampleSize": n},
        "variants": [{"mean": t_mean, "sampleSize": n, "isSignificant": significant}],
    }


def test_polarity_map_matches_builtin_score_direction():
    assert ac.evaluator_polarity("Builtin.Refusal") == -1
    # live scale: "Not Harmful" / "Not Stereotyping" = 1.0 — higher is better
    assert ac.evaluator_polarity("Builtin.Harmfulness") == 1
    assert ac.evaluator_polarity("Builtin.Stereotyping") == 1
    assert ac.evaluator_polarity("Builtin.Helpfulness") == 1
    assert ac.evaluator_polarity("Builtin.GoalSuccessRate") == 1
    assert ac.evaluator_polarity("ThirdParty.DeepEval.Toxicity") == -1
    # DeepEval PIILeakage = non-PII statements / all statements: 1.0 is no leakage
    assert ac.evaluator_polarity("ThirdParty.DeepEval.PIILeakage") == 1
    # a custom judge has no direction on AWS's side — assume higher-is-better
    assert ac.evaluator_polarity("my_domain_judge") == 1
    assert ac.evaluator_polarity("") == 1


def test_polarity_resolves_from_evaluator_arn():
    arn = "arn:aws:bedrock-agentcore:::evaluator/Builtin.Refusal"
    assert ac.evaluator_polarity(arn) == -1


def test_refusal_drop_is_a_treatment_win():
    """control 0.2 → treatment 0.0 refusals is an improvement, not a loss."""
    verdict = compute_verdict([_metric("Builtin.Refusal", 0.2, 0.0)])
    assert verdict["verdict"] == "treatment-wins"
    assert verdict["avg_delta"] > 0


def test_refusal_rise_is_a_treatment_loss():
    verdict = compute_verdict([_metric("Builtin.Refusal", 0.0, 0.3)])
    assert verdict["verdict"] == "control-wins"
    assert verdict["avg_delta"] < 0


def test_higher_is_better_evaluator_unchanged():
    verdict = compute_verdict([_metric("Builtin.Helpfulness", 0.4, 0.6)])
    assert verdict["verdict"] == "treatment-wins"
    assert verdict["avg_delta"] > 0


def test_harmfulness_and_stereotyping_are_higher_is_better():
    """1.0 = "Not Harmful" / "Not Stereotyping": a drop is the treatment losing."""
    for evaluator in ("Builtin.Harmfulness", "Builtin.Stereotyping"):
        verdict = compute_verdict([_metric(evaluator, 1.0, 0.6)])
        assert verdict["verdict"] == "control-wins", evaluator


def test_mixed_polarity_set_does_not_cancel_out():
    """Both arms improved (helpfulness up, refusals down) → treatment wins.

    Pre-fix these two deltas were +0.2 and -0.2 and summed to a tie.
    """
    verdict = compute_verdict([
        _metric("Builtin.Helpfulness", 0.4, 0.6),
        _metric("Builtin.Refusal", 0.2, 0.0),
    ])
    assert verdict["verdict"] == "treatment-wins"
    assert verdict["avg_delta"] > 0


def test_average_is_weighted_by_the_smaller_arm():
    """A 2-sample evaluator must not outvote a 40-sample one.

    Helpfulness regresses hard on 40 samples; Refusal improves on 2. Unweighted,
    the oriented deltas (-0.2 and +0.3) would hand the win to treatment.
    """
    verdict = compute_verdict([
        _metric("Builtin.Helpfulness", 0.6, 0.4, n=40),
        _metric("Builtin.Refusal", 0.3, 0.0, n=2),
    ])
    assert verdict["verdict"] == "control-wins"
    assert verdict["avg_delta"] < 0


def test_lopsided_arms_weight_by_the_smaller_side():
    """weight is min(control n, variant n), so 40-vs-2 counts as 2."""
    lopsided = {
        "evaluatorId": "Builtin.Helpfulness",
        "label": "Helpfulness",
        "control": {"mean": 0.5, "sampleSize": 40},
        "variants": [{"mean": 1.0, "sampleSize": 2}],
    }
    solid = _metric("Builtin.Correctness", 0.6, 0.5, n=20)
    verdict = compute_verdict([lopsided, solid])
    assert verdict["verdict"] == "control-wins"


def test_means_without_sample_size_still_count_at_unit_weight():
    metric = {
        "evaluatorId": "Builtin.Refusal",
        "label": "Refusal",
        "control": {"mean": 0.5},
        "variants": [{"mean": 0.1}],
    }
    # no sampleSize anywhere → insufficient-n, but the delta is still oriented
    verdict = compute_verdict([metric])
    assert verdict["verdict"] == "insufficient-n"
    assert verdict["avg_delta"] > 0


def test_custom_evaluator_treated_as_higher_is_better():
    verdict = compute_verdict([_metric("my_domain_judge", 0.3, 0.7)])
    assert verdict["verdict"] == "treatment-wins"
    assert verdict["avg_delta"] > 0


def test_verdict_contract_keys_are_stable():
    """The console reads these keys — orientation must not rename anything."""
    verdict = compute_verdict([_metric("Builtin.Helpfulness", 0.4, 0.6, significant=True)])
    # N-SCN-obs14 added the additive ``arms_n`` / ``min_arm_n`` — nothing was renamed
    assert set(verdict) == {"verdict", "avg_delta", "n", "significant", "arms_n", "min_arm_n"}
    assert verdict["n"] == 24 and verdict["significant"] is True
    assert verdict["arms_n"] == {"control": 12, "treatment": 12}
    assert verdict["min_arm_n"] == 10
    assert compute_verdict([])["verdict"] == "insufficient-data"
    no_means = [{"evaluatorId": "Builtin.Refusal", "control": {}, "variants": [{}]}]
    # N17-obs443 added the additive ``reason_code`` / ``reason_params`` — ``reason`` is kept
    assert compute_verdict(no_means) == {
        "verdict": "insufficient-data", "reason": "arms have no means yet",
        "reason_code": "no_means", "reason_params": {},
    }


# ─── N-SCN-obs14: per-arm minimum for a directional verdict ─────────────────────
def _arms(
    c_n, t_n, *, c_mean=0.625, t_mean=0.5, significant=False,
    evaluator="Builtin.GoalSuccessRate",
):
    return {
        "evaluatorId": evaluator,
        "label": evaluator,
        "control": {"mean": c_mean, "sampleSize": c_n},
        "variants": [{"mean": t_mean, "sampleSize": t_n, "isSignificant": significant}],
    }


def test_treatment_arm_below_the_bar_is_insufficient_n_not_control_wins():
    """The live obs14 round: C 0.625 n 32 vs T1 0.50 n 4 used to be a terminal
    ``control-wins``; four treatment sessions cannot carry a direction."""
    verdict = compute_verdict([
        _arms(32, 4),
        _arms(31, 4, c_mean=0.753, t_mean=0.75, evaluator="Builtin.Helpfulness"),
    ])
    assert verdict["verdict"] == "insufficient-n"
    assert verdict["arms_n"] == {"control": 32, "treatment": 4}
    assert verdict["min_arm_n"] == 10
    assert verdict["n"] == 71
    # the reason names both arms, the bar and the unit
    assert "10 scored records per arm" in verdict["reason"]
    assert "control has 32" in verdict["reason"] and "treatment has 4" in verdict["reason"]
    assert "significant" not in verdict  # no direction → no significance claim either


def test_control_arm_below_the_bar_is_insufficient_n_too():
    verdict = compute_verdict([_arms(3, 40, c_mean=0.2, t_mean=0.9)])
    assert verdict["verdict"] == "insufficient-n"
    assert verdict["arms_n"] == {"control": 3, "treatment": 40}


def test_significance_does_not_bypass_the_per_arm_bar():
    """A p < 0.05 on n = 4 is noise — the bar applies before the direction is read."""
    verdict = compute_verdict([_arms(32, 4, significant=True)])
    assert verdict["verdict"] == "insufficient-n"


def test_both_arms_at_the_bar_keep_the_directional_outcomes():
    control = compute_verdict([_arms(10, 10, significant=True)])
    assert control["verdict"] == "control-wins" and control["significant"] is True
    assert control["arms_n"] == {"control": 10, "treatment": 10}
    treatment = compute_verdict([_arms(10, 10, c_mean=0.5, t_mean=0.8)])
    assert treatment["verdict"] == "treatment-wins" and treatment["significant"] is False
    tie = compute_verdict([_arms(10, 10, c_mean=0.5, t_mean=0.5)])
    assert tie["verdict"] == "tie"


def test_per_arm_count_is_the_largest_evaluator_sample_of_that_arm():
    """One evaluator that scored fewer sessions (judge hiccup) must not starve the arm:
    the arm's n is the sessions that reached ANY judge, i.e. the max over evaluators."""
    verdict = compute_verdict([
        _arms(12, 11, c_mean=0.5, t_mean=0.8, evaluator="Builtin.Helpfulness"),
        _arms(12, 3, c_mean=0.5, t_mean=0.8, evaluator="Builtin.Coherence"),
    ])
    assert verdict["verdict"] == "treatment-wins"
    assert verdict["arms_n"] == {"control": 12, "treatment": 11}


def test_min_arm_n_argument_overrides_the_settings_bar():
    assert compute_verdict([_arms(32, 4)], min_arm_n=4)["verdict"] == "control-wins"
    assert compute_verdict([_arms(32, 4)], min_arm_n=5)["verdict"] == "insufficient-n"
    assert compute_verdict([_arms(32, 4)], min_arm_n=4)["min_arm_n"] == 4


def test_settings_knob_is_the_default_bar(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "verdict_min_arm_n", 4)
    verdict = compute_verdict([_arms(32, 4)])
    assert verdict["verdict"] == "control-wins" and verdict["min_arm_n"] == 4
    monkeypatch.setattr(get_settings(), "verdict_min_arm_n", 33)
    assert compute_verdict([_arms(32, 40)])["verdict"] == "insufficient-n"


def test_normalize_ab_results_carries_polarity():
    normalized = ac.normalize_ab_results({
        "results": {
            "evaluatorMetrics": [
                {
                    "evaluatorArn": "arn:aws:bedrock-agentcore:::evaluator/Builtin.Refusal",
                    "controlStats": {"name": "C", "mean": 0.2, "sampleSize": 14},
                    "variantResults": [{"name": "T1", "mean": 0.0, "sampleSize": 14}],
                },
                {
                    "evaluatorArn": "arn:aws:bedrock-agentcore:1234:evaluator/my_judge",
                    "controlStats": {"name": "C", "mean": 0.5, "sampleSize": 14},
                    "variantResults": [{"name": "T1", "mean": 0.9, "sampleSize": 14}],
                },
            ]
        }
    })
    assert [m["polarity"] for m in normalized] == [-1, 1]
    # …and the verdict computed from a real normalized payload agrees
    assert compute_verdict(normalized)["verdict"] == "treatment-wins"
