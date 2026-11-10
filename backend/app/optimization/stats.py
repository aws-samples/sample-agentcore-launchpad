"""Welch's two-sided t-test without scipy (N12 E3-1, plan §B.7 / R-10).

The Global A/B service reported ``pValue`` / ``isSignificant`` per evaluator; the CN
verdict computes them locally from ``eval_results`` × ``experiment_assignments``. The
Student-t CDF is evaluated through the regularised incomplete beta function
``I_x(a, b)`` with the Numerical-Recipes continued fraction (``betacf``); the six
critical-value pins in ``tests/optimization/test_stats.py`` hold it to 1e-3.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

_MAX_ITER = 300
_EPS = 3.0e-14
_FPMIN = 1.0e-300
# N17-obs388: evaluator scores live in [0, 1] on a rating scale; a mean difference or a
# variance below these is float noise (0.833 vs 0.8329999999999999, var ~1e-32), not data
MEAN_TOLERANCE = 1e-9
VARIANCE_TOLERANCE = 1e-12


def mean(values: Sequence[float]) -> float | None:
    """Exactly rounded (``math.fsum``): N copies of a value average to that value."""
    return math.fsum(values) / len(values) if values else None


def _variance(values: Sequence[float], mu: float) -> float:
    """Unbiased sample variance (n ≥ 2)."""
    return math.fsum((v - mu) ** 2 for v in values) / (len(values) - 1)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (NR §6.4)."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, _MAX_ITER + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """``I_x(a, b)`` for 0 ≤ x ≤ 1."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(ln_beta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_two_sided_p(t: float, df: float) -> float:
    """P(|T| ≥ |t|) for a Student-t variable with ``df`` degrees of freedom."""
    if df <= 0:
        raise ValueError("df must be positive")
    if math.isinf(t):
        return 0.0
    if t == 0.0:
        return 1.0
    x = df / (df + t * t)
    return min(1.0, max(0.0, regularized_incomplete_beta(df / 2.0, 0.5, x)))


def welch_two_sided(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float] | None:
    """Welch's unequal-variance t-test of ``mean(a) - mean(b)``.

    Returns ``(t, df, p)`` or ``None`` when either arm has fewer than two observations.
    A mean difference below :data:`MEAN_TOLERANCE` is no difference (``t = 0, p = 1``,
    N17-obs388 — float summation made two identical constant arms 0.833 vs
    0.8329999999999999 with variance ~1e-32, "p 7.7e-05"). Two arms whose variances are
    both below :data:`VARIANCE_TOLERANCE` are constant: ``t = ±inf, p = 0`` when their
    means differ — a deterministic difference is "significant" in the only sense available.
    """
    if len(a) < 2 or len(b) < 2:
        return None
    mu_a, mu_b = mean(a), mean(b)
    assert mu_a is not None and mu_b is not None
    var_a, var_b = _variance(a, mu_a), _variance(b, mu_b)
    diff = mu_a - mu_b
    pooled_df = float(len(a) + len(b) - 2)
    if abs(diff) < MEAN_TOLERANCE:
        return 0.0, pooled_df, 1.0
    if var_a < VARIANCE_TOLERANCE and var_b < VARIANCE_TOLERANCE:
        return math.copysign(math.inf, diff), pooled_df, 0.0
    se2_a = (var_a if var_a >= VARIANCE_TOLERANCE else 0.0) / len(a)
    se2_b = (var_b if var_b >= VARIANCE_TOLERANCE else 0.0) / len(b)
    se2 = se2_a + se2_b
    t = diff / math.sqrt(se2)
    # Welch–Satterthwaite degrees of freedom
    denom = 0.0
    if se2_a > 0:
        denom += se2_a**2 / (len(a) - 1)
    if se2_b > 0:
        denom += se2_b**2 / (len(b) - 1)
    df = se2**2 / denom if denom > 0 else float(len(a) + len(b) - 2)
    return t, df, student_t_two_sided_p(t, df)


# ─── experiment design (R3-d) ─────────────────────────────────────────────────────────
# Acklam's rational approximation of the standard normal quantile (|error| < 1.2e-9 after
# one Halley refinement step) — stdlib only, like the t-test above.
_A = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
      1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
_B = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
      6.680131188771972e01, -1.328068155288572e01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
      -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
      3.754408661907416e00)
_P_LOW = 0.02425


def normal_quantile(p: float) -> float:
    """``z`` with ``Φ(z) = p`` for 0 < p < 1."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    if p < _P_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    elif p <= 1.0 - _P_LOW:
        q = p - 0.5
        r = q * q
        x = (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q / \
            (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    # one Halley step against the exact CDF
    e = 0.5 * math.erfc(-x / math.sqrt(2.0)) - p
    u = e * math.sqrt(2.0 * math.pi) * math.exp(x * x / 2.0)
    return x - u / (1.0 + x * u / 2.0)


DEFAULT_SD = 0.3  # a typical spread of [0, 1] rating-scale scores when no baseline is known


def sessions_per_arm(mde: float, *, sd: float = DEFAULT_SD, alpha: float = 0.05,
                     power: float = 0.8) -> int:
    """Sessions per arm a two-sided two-sample test needs to detect a mean difference of
    ``mde`` (score units, scores in [0, 1]) with standard deviation ``sd``, false-positive
    rate ``alpha`` and the given ``power``: ``⌈2 (z₁₋α/₂ + z_power)² sd² / mde²⌉`` (normal
    approximation; Welch at that n is slightly conservative)."""
    if not 0.0 < mde <= 1.0:
        raise ValueError("mde must be in (0, 1] (score units)")
    if not 0.0 < sd <= 1.0:
        raise ValueError("sd must be in (0, 1]")
    if not 0.0 < alpha <= 0.5:
        raise ValueError("alpha must be in (0, 0.5]")
    if not 0.5 <= power < 1.0:
        raise ValueError("power must be in [0.5, 1)")
    z = normal_quantile(1.0 - alpha / 2.0) + normal_quantile(power)
    return max(2, math.ceil(2.0 * z * z * sd * sd / (mde * mde)))


def holm(p_values: Sequence[float]) -> list[float]:
    """Holm–Bonferroni step-down adjusted p-values, in the input order (capped at 1)."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, index in enumerate(order):
        running = max(running, min(1.0, (m - rank) * float(p_values[index])))
        adjusted[index] = running
    return adjusted
