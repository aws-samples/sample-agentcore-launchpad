"""Welch's t-test without scipy (N12 E3-1, plan §B.7 / R-10).

The two-sided p-value comes from the Student-t CDF via the regularised incomplete
beta function (continued fraction). Pinned against the standard critical-value
table: ``t_crit(df, α)`` must give ``p == α`` within 1e-3.
"""

import math

import pytest

from app.optimization import stats

# (t, df, two-sided p) — textbook Student-t critical values.
TABULATED = [
    (12.7062, 1, 0.05),
    (4.302653, 2, 0.05),
    (2.570582, 5, 0.05),
    (2.228139, 10, 0.05),
    (1.812461, 10, 0.10),
    (1.962339, 1000, 0.05),
]


@pytest.mark.parametrize("t,df,expected", TABULATED)
def test_student_t_two_sided_p_matches_the_table(t, df, expected):
    assert stats.student_t_two_sided_p(t, df) == pytest.approx(expected, abs=1e-3)


def test_t_zero_is_p_one_and_sign_does_not_matter():
    assert stats.student_t_two_sided_p(0.0, 7) == pytest.approx(1.0)
    assert stats.student_t_two_sided_p(-2.228139, 10) == pytest.approx(0.05, abs=1e-3)


def test_welch_two_sided_on_identical_samples_is_not_significant():
    a = [0.8, 0.9, 0.7, 0.85, 0.75]
    t, df, p = stats.welch_two_sided(a, list(a))
    assert t == 0.0
    assert p == pytest.approx(1.0)
    assert df > 0


def test_welch_two_sided_detects_a_clear_shift():
    control = [0.50, 0.55, 0.45, 0.52, 0.48, 0.51]
    treatment = [0.90, 0.95, 0.85, 0.92, 0.88, 0.91]
    t, df, p = stats.welch_two_sided(control, treatment)
    assert t < 0  # a - b
    assert 5 <= df <= 10.01  # equal variances → Welch df == pooled df (10)
    assert p < 0.001


def test_welch_handles_zero_variance_and_tiny_samples():
    # both arms constant and different → infinite t, p → 0
    t, df, p = stats.welch_two_sided([1.0, 1.0, 1.0], [0.0, 0.0, 0.0])
    assert math.isinf(t) and p == 0.0
    # both arms constant and equal → t 0, p 1
    t, df, p = stats.welch_two_sided([1.0, 1.0], [1.0, 1.0])
    assert t == 0.0 and p == 1.0
    # fewer than two observations in an arm → no test
    assert stats.welch_two_sided([1.0], [0.0, 0.5]) is None
    assert stats.welch_two_sided([], []) is None


def test_mean_helper():
    assert stats.mean([]) is None
    assert stats.mean([1.0, 2.0, 3.0]) == pytest.approx(2.0)


# ── N17-obs388: identical arms are never "significant" through float noise ───────────


def test_identical_constant_arms_are_not_significant():
    # the C3 no-change canary: 0.833 vs 0.8329999999999999 by float summation, variance
    # ~1e-32 — was t 4.80, p 7.7e-05 (control-wins → rollback of an identical candidate)
    t, _df, p = stats.welch_two_sided([0.833] * 90, [0.833] * 24)
    assert p == 1.0 and t == 0.0
    assert math.isclose(stats.mean([0.833] * 90), 0.833, abs_tol=stats.MEAN_TOLERANCE)


def test_identical_arms_with_spread_are_not_significant():
    arm = [0.0, 0.5, 1.0, 0.833] * 6
    _t, _df, p = stats.welch_two_sided(arm * 3, arm)
    assert p > 0.99


def test_a_genuine_difference_is_still_significant():
    _t, _df, p = stats.welch_two_sided([0.833] * 30, [0.0] * 6)
    assert p == 0.0
    _t, _df, p = stats.welch_two_sided([0.9, 0.8, 0.85, 0.95] * 5, [0.1, 0.2, 0.15, 0.05] * 5)
    assert p < 1e-6
    # a real but small difference between noisy arms is not swallowed by the tolerance
    _t, _df, p = stats.welch_two_sided([0.50, 0.52] * 50, [0.48, 0.50] * 50)
    assert p < 0.05
