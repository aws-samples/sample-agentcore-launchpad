"""Run-level sampling uncertainty (EVAL-TRUST 3.2): a 95 % confidence interval per evaluator
mean, and whether a difference between two runs is significant.

One observation per SESSION, as the canary verdict counts them (N17-obs387): an evaluator's
scored rows of a session (its turns, its tool calls) are averaged into that session's
score, and the interval is the Student-t interval over those session scores. Turns of one
session are not independent draws, so counting them as observations would make the
interval too narrow.

A difference between two runs is ``significant`` only when the two 95 % intervals do NOT
overlap AND Welch's two-sided p (on the same session scores, from the stored summaries) is
below 0.05; otherwise ``not_significant``; ``insufficient`` when either side has fewer
than two sessions. The t-distribution comes from ``optimization/stats`` (read-only here).
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from app.optimization import stats

ALPHA = 0.05
SIGNIFICANT = "significant"
NOT_SIGNIFICANT = "not_significant"
INSUFFICIENT = "insufficient"


def t_quantile(df: float, alpha: float = ALPHA) -> float:
    """The two-sided critical value ``t`` with ``P(|T| ≥ t) = alpha`` (bisection on the
    shared CDF; |error| < 1e-9)."""
    lo, hi = 0.0, 1.0
    while stats.student_t_two_sided_p(hi, df) > alpha:
        hi *= 2.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if stats.student_t_two_sided_p(mid, df) > alpha:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return (lo + hi) / 2.0


def session_scores(records: list[dict[str, Any]]) -> dict[str, list[float]]:
    """``{evaluator_id: [one score per session]}`` — the mean of each session's scored rows
    (error / not-applicable rows carry no score and are skipped)."""
    per: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for rec in records:
        if rec.get("score") is None:
            continue
        per[str(rec["evaluator_id"])][str(rec.get("session_id"))].append(float(rec["score"]))
    return {ev: [math.fsum(v) / len(v) for v in by_session.values()]
            for ev, by_session in per.items()}


Z95 = 1.959963984540054  # the normal 97.5 % quantile (Wilson)
DEFAULT_BOUNDS = (0.0, 1.0)  # built-in evaluators score in [0, 1]
METHOD_T = "t"
METHOD_WILSON = "wilson"
_BINARY_TOLERANCE = 1e-9


def wilson(successes: float, n: int) -> tuple[float, float]:
    """The 95 % Wilson score interval of a proportion (always inside [0, 1])."""
    p = successes / n
    z2 = Z95 * Z95
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = Z95 * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def _binary(values: list[float]) -> bool:
    return all(abs(v) < _BINARY_TOLERANCE or abs(v - 1.0) < _BINARY_TOLERANCE for v in values)


def summarize(values: list[float],
              bounds: tuple[float, float] = DEFAULT_BOUNDS) -> dict[str, Any]:
    """``{sessions_n, session_mean, sd, ci95: [lo, hi] | None, ci_method, scale}`` of one
    evaluator's session scores; no interval below two sessions. N17-obs423: a binary sample
    (every session score 0 or 1, the pass/fail evaluators) gets the Wilson interval; any
    other the Student-t one, CLAMPED to the evaluator's scale ``bounds`` — a mean of 0.714
    at n = 7 no longer reads [0.263, 1.166]. ``sd`` (Welch's input) is the plain sample SD
    either way. A constant non-binary sample has a zero-width interval."""
    n = len(values)
    mu = stats.mean(values)
    lo_b, hi_b = bounds
    base = {"scale": [lo_b, hi_b]}
    if mu is None:
        return {"sessions_n": 0, "session_mean": None, "sd": None, "ci95": None, **base}
    if n < 2:
        return {"sessions_n": n, "session_mean": round(mu, 4), "sd": None, "ci95": None,
                **base}
    sd = math.sqrt(stats._variance(values, mu))
    if _binary(values) and lo_b <= 0.0 and hi_b >= 1.0:
        lo, hi = wilson(math.fsum(values), n)
        method = METHOD_WILSON
    else:
        half = t_quantile(n - 1) * sd / math.sqrt(n)
        lo, hi = mu - half, mu + half
        method = METHOD_T
    lo, hi = max(lo_b, lo), min(hi_b, hi)
    return {"sessions_n": n, "session_mean": round(mu, 4), "sd": round(sd, 6),
            "ci95": [round(lo, 4), round(hi, 4)], "ci_method": method, **base}


def scale_bounds(db: Any, workspace_id: str | None, evaluator_ids: list[str],
                 observed: dict[str, list[float]] | None = None) -> dict[str, tuple[float, float]]:
    """``{evaluator_id: (low, high)}``: a custom judge's rating-scale range; every other
    evaluator (built-ins, derived, code) [0, 1] widened to any value actually observed — an
    interval is never clamped tighter than the data."""
    from app.evaluation.models import EvalEvaluator

    out: dict[str, tuple[float, float]] = {}
    for evaluator_id in evaluator_ids:
        lo, hi = DEFAULT_BOUNDS
        row = db.get(EvalEvaluator, evaluator_id) if db is not None else None
        values = [float(s["value"]) for s in (getattr(row, "rating_scale", None) or [])
                  if isinstance(s, dict) and isinstance(s.get("value"), int | float)]
        if row is not None and row.workspace_id == workspace_id and len(values) >= 2:
            lo, hi = min(values), max(values)
        seen = (observed or {}).get(evaluator_id) or []
        out[evaluator_id] = (min([lo, *seen]), max([hi, *seen]))
    return out


def with_confidence(scores: list[dict[str, Any]], records: list[dict[str, Any]],
                    bounds: dict[str, tuple[float, float]] | None = None,
                    ) -> list[dict[str, Any]]:
    """``EvalRun.scores`` entries with the session-level summary merged in (additive keys:
    ``sessions_n``, ``session_mean``, ``sd``, ``ci95``, ``ci_method``, ``scale``). The
    headline ``score`` stays the unit mean (N17-obs324); the interval is around
    ``session_mean``."""
    per = session_scores(records)
    out = []
    for entry in scores:
        ev = str(entry.get("evaluatorId"))
        values = per.get(ev, [])
        b = (bounds or {}).get(ev) or (min([0.0, *values]), max([1.0, *values]))
        out.append({**entry, **summarize(values, b)})
    return out


def has_confidence(scores: list[dict[str, Any]] | None) -> bool:
    """Summarised by the current rule (N17-obs423: ``scale`` marks it — an entry scored by
    the first, unclamped version is summarised again on read)."""
    return bool(scores) and all("sessions_n" in s and "scale" in s for s in scores or [])


def welch_from_summary(a: dict[str, Any], b: dict[str, Any]) -> float | None:
    """Welch's two-sided p of ``b - a`` from two :func:`summarize` results; None when a side
    has fewer than two sessions. Mirrors ``stats.welch_two_sided`` (float-noise and
    constant-sample rules included) without the raw values."""
    na, nb = int(a.get("sessions_n") or 0), int(b.get("sessions_n") or 0)
    if na < 2 or nb < 2 or a.get("session_mean") is None or b.get("session_mean") is None:
        return None
    diff = float(b["session_mean"]) - float(a["session_mean"])
    var_a, var_b = float(a.get("sd") or 0.0) ** 2, float(b.get("sd") or 0.0) ** 2
    if abs(diff) < stats.MEAN_TOLERANCE:
        return 1.0
    if var_a < stats.VARIANCE_TOLERANCE and var_b < stats.VARIANCE_TOLERANCE:
        return 0.0
    se2_a, se2_b = var_a / na, var_b / nb
    se2 = se2_a + se2_b
    t = diff / math.sqrt(se2)
    denom = (se2_a**2 / (na - 1) if se2_a else 0.0) + (se2_b**2 / (nb - 1) if se2_b else 0.0)
    df = se2**2 / denom if denom > 0 else float(na + nb - 2)
    return stats.student_t_two_sided_p(t, df)


def compare(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """One evaluator's ``{label, p, ci_overlap}`` between two runs (see the module doc)."""
    p = welch_from_summary(a, b)
    ca, cb = a.get("ci95"), b.get("ci95")
    if p is None or not ca or not cb:
        return {"label": INSUFFICIENT, "p": None, "ci_overlap": None}
    overlap = ca[0] <= cb[1] and cb[0] <= ca[1]
    label = SIGNIFICANT if (not overlap and p < ALPHA) else NOT_SIGNIFICANT
    return {"label": label, "p": round(p, 6), "ci_overlap": overlap}


def compare_runs(scores_a: list[dict[str, Any]] | None,
                 scores_b: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """``{evaluator_id: compare(...)}`` over the evaluators both runs scored."""
    by_a = {str(s.get("evaluatorId")): s for s in scores_a or []}
    by_b = {str(s.get("evaluatorId")): s for s in scores_b or []}
    return {ev: compare(by_a[ev], by_b[ev]) for ev in sorted(set(by_a) & set(by_b))}
