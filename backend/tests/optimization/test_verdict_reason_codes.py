"""N17-obs443 — every canary verdict reason carries a stable code and its params.

The insufficient / tie / judge-mismatch reasons were English sentences only, shown verbatim
on the zh console. Each reason now also carries ``reason_code`` + ``reason_params``
(additive; the ``reason`` text is kept for older consoles and the API). The console words
the code in both languages (``canaryVerdict.reason.<code>``); the codes below are the
contract the frontend's ``VERDICT_REASON_CODES`` mirrors.
"""

import pytest

from app.optimization.verdict import compute_verdict

REASON_CODES = {
    "no_metrics", "no_means", "arm_n_below_bar", "primary_arm_n_below_bar", "judge_mismatch",
    "primary_no_scores", "design_no_primary", "design_below_planned_n", "primary_no_p_value",
    "not_significant", "no_new_samples", "no_new_primary_samples",
}
H = "Builtin.Helpfulness"
DESIGN = {"mde": 0.1, "sd": 0.2, "alpha": 0.05, "power": 0.8, "planned_n_per_arm": 10}


def _metric(evaluator=H, c=0.5, t=0.6, n=12, p=None, significant=False, judges=None):
    variant = {"mean": t, "sampleSize": n, "isSignificant": significant}
    if p is not None:
        variant["pValue"] = p
    out = {"evaluatorId": evaluator, "label": evaluator, "control": {"mean": c, "sampleSize": n},
           "variants": [variant]}
    if judges:
        out.update(judgeMismatch=True, judges=judges)
    return out


@pytest.mark.parametrize(("kwargs", "code", "params"), [
    ({"metrics": []}, "no_metrics", {}),
    ({"metrics": [_metric(c=None, t=None)]}, "no_means", {}),
    ({"metrics": [_metric(n=2)], "min_arm_n": 5}, "arm_n_below_bar",
     {"min_arm_n": 5, "control": 2, "treatment": 2}),
    ({"metrics": [_metric(n=2)], "min_arm_n": 5, "primary": H}, "primary_arm_n_below_bar",
     {"primary": H, "min_arm_n": 5, "control": 2, "treatment": 2}),
    ({"metrics": [_metric(judges=["a", "b"])]}, "judge_mismatch",
     {"evaluators": [H], "judges": ["a", "b"]}),
    ({"metrics": [], "primary": H}, "primary_no_scores", {"primary": H}),
    ({"metrics": [_metric()], "design": DESIGN}, "design_no_primary", {}),
    ({"metrics": [_metric(n=4)], "design": DESIGN, "primary": H}, "design_below_planned_n",
     {"primary": H, "planned": 10, "control": 4, "treatment": 4}),
    ({"metrics": [_metric()], "design": DESIGN, "primary": H}, "primary_no_p_value",
     {"primary": H}),
    ({"metrics": [_metric(p=0.4)], "design": DESIGN, "primary": H}, "not_significant",
     {"primary": H, "alpha": 0.05, "p": 0.4, "delta": 0.1, "planned": 10}),
])
def test_each_reason_has_its_code_params_and_keeps_the_text(kwargs, code, params):
    verdict = compute_verdict(**kwargs)
    assert verdict["reason_code"] == code and code in REASON_CODES
    assert verdict["reason_params"] == params
    assert isinstance(verdict["reason"], str) and verdict["reason"]  # the old field stays


def test_a_directional_verdict_has_no_reason():
    verdict = compute_verdict([_metric(c=0.2, t=0.9, n=20, significant=True)], min_arm_n=5)
    assert verdict["verdict"] == "treatment-wins"
    assert "reason_code" not in verdict and "reason" not in verdict


def test_the_secondary_note_carries_its_count():
    verdict = compute_verdict([_metric(c=0.2, t=0.9, n=20, significant=True),
                               _metric("Builtin.Correctness", n=20, p=0.3)],
                              min_arm_n=5, primary=H)
    assert verdict["multiple_comparisons_k"] == 1
    assert "1 secondary" in verdict["multiple_comparisons"]


def test_the_frontend_mirrors_every_code():
    import pathlib
    import re

    src = (pathlib.Path(__file__).parents[3] / "frontend/src/v2/pages/canary/common.ts").read_text()
    block = re.search(r"VERDICT_REASON_CODES = \[(.*?)\]", src, re.S)
    assert block, "common.ts must export VERDICT_REASON_CODES"
    assert set(re.findall(r'"([a-z_]+)"', block.group(1))) == REASON_CODES
