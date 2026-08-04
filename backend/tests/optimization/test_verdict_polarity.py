"""Verdict maths must respect evaluator polarity and sample weight.

``Builtin.Refusal`` / ``Harmfulness`` / ``Stereotyping`` score a penalty — the
lower-mean arm is the better arm — so a raw ``treatment - control`` average
declares the winning arm the loser. These tests pin the orientation and the
sample-size weighting for both the experiment verdict and the canary
auto-decision that shares ``compute_verdict``.
"""

from app.evaluation import agentcore_eval as ac
from app.optimization.service import compute_verdict


def _metric(evaluator, c_mean, t_mean, n=6, significant=False):
    return {
        "evaluatorId": evaluator,
        "label": evaluator.rsplit("/", 1)[-1],
        "control": {"mean": c_mean, "sampleSize": n},
        "variants": [{"mean": t_mean, "sampleSize": n, "isSignificant": significant}],
    }


def test_polarity_map_matches_builtin_score_direction():
    assert ac.evaluator_polarity("Builtin.Refusal") == -1
    assert ac.evaluator_polarity("Builtin.Harmfulness") == -1
    assert ac.evaluator_polarity("Builtin.Stereotyping") == -1
    assert ac.evaluator_polarity("Builtin.Helpfulness") == 1
    assert ac.evaluator_polarity("Builtin.GoalSuccessRate") == 1
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


def test_harmfulness_and_stereotyping_are_oriented_too():
    for evaluator in ("Builtin.Harmfulness", "Builtin.Stereotyping"):
        verdict = compute_verdict([_metric(evaluator, 0.4, 0.1)])
        assert verdict["verdict"] == "treatment-wins", evaluator


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
    assert set(verdict) == {"verdict", "avg_delta", "n", "significant"}
    assert verdict["n"] == 12 and verdict["significant"] is True
    assert compute_verdict([])["verdict"] == "insufficient-data"
    no_means = [{"evaluatorId": "Builtin.Refusal", "control": {}, "variants": [{}]}]
    assert compute_verdict(no_means) == {
        "verdict": "insufficient-data", "reason": "arms have no means yet",
    }


def test_normalize_ab_results_carries_polarity():
    normalized = ac.normalize_ab_results({
        "results": {
            "evaluatorMetrics": [
                {
                    "evaluatorArn": "arn:aws:bedrock-agentcore:::evaluator/Builtin.Refusal",
                    "controlStats": {"name": "C", "mean": 0.2, "sampleSize": 4},
                    "variantResults": [{"name": "T1", "mean": 0.0, "sampleSize": 4}],
                },
                {
                    "evaluatorArn": "arn:aws:bedrock-agentcore:1234:evaluator/my_judge",
                    "controlStats": {"name": "C", "mean": 0.5, "sampleSize": 4},
                    "variantResults": [{"name": "T1", "mean": 0.9, "sampleSize": 4}],
                },
            ]
        }
    })
    assert [m["polarity"] for m in normalized] == [-1, 1]
    # …and the verdict computed from a real normalized payload agrees
    assert compute_verdict(normalized)["verdict"] == "treatment-wins"
