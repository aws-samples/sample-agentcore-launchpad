"""The A/B verdict a Runtime canary decides on (N12 W-E3; canary-owned since N13-obs2).

``compute_verdict`` reads the ``GetABTest``-shaped statistics ``routing.ab_results`` builds
from ``eval_results`` × ``experiment_assignments`` and answers honestly about small n. It
was born in the configuration-experiment service; that flow is retired (N13-obs2) and the
verdict now lives with its only caller, ``canary_service.act_verdict``.
"""

from typing import Any

from app.core.config import get_settings
from app.optimization import routing, stats

# How long VERDICT keeps re-reading the local statistics for late online scores before
# it settles on what it has (the baseline waited ~15 min for the A/B service).
VERDICT_WAIT_S = 900
VERDICT_POLL_S = 45


def reason_of(code: str, text: str, **params: Any) -> dict[str, Any]:
    """N17-obs443: a verdict's reason as ``reason`` (the English sentence, kept) plus the
    stable ``reason_code`` and its ``reason_params`` the console words in either language."""
    return {"reason": text, "reason_code": code, "reason_params": params}


def metric_evaluator_id(metric: dict[str, Any]) -> str:
    """The evaluator id a normalized metric belongs to: the tail of its evaluator ARN
    (``…:evaluator/<id>``) or its bare id / label (CN metrics carry bare ids)."""
    return str(metric.get("evaluatorId") or metric.get("label") or "").rsplit("/", 1)[-1]


def compute_verdict(
    metrics: list[dict[str, Any]], min_n: int = 3, min_arm_n: int | None = None, *,
    primary: str | None = None, design: dict[str, Any] | None = None,
    decided: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Honest small-n verdict from normalized A/B metrics.

    R3-d — with a ``design`` (``{mde, sd, alpha, power, planned_n_per_arm}``, stored at
    create) the canary is a fixed-n experiment: :func:`_designed_verdict` tests the PRIMARY
    once both arms hold ``planned_n_per_arm`` sessions (no peeking at interim rounds), needs
    ``p < alpha`` for a direction, and ``decided`` (the stored first decision) is returned
    as it is — frozen — on every later call.

    With ``primary`` set (upstream b5eb3dc8 parity, N12-obs46), that ONE evaluator decides
    (see :func:`_primary_verdict`); every other metric is reference only — the aggregate
    ``avg_delta`` still rides on the payload for display. Without it, the aggregate below
    decides.

    Evaluators do not all point the same way: ``Builtin.Refusal`` and the other
    penalty scores are better *lower*, so each raw ``t_mean - c_mean`` is
    oriented by :func:`routing.evaluator_polarity` before it is averaged. ``avg_delta``
    is therefore polarity-normalized — positive always favours treatment,
    whatever the evaluator measures — and custom judges count as higher-is-better
    (AWS exposes no direction for them).

    The average is weighted by ``min`` of the two arms' sample sizes: a delta is
    only as trustworthy as its smaller arm, so an evaluator that produced two
    scores no longer outvotes one that produced forty.

    N-SCN-obs14 — a *directional* verdict (``treatment-wins`` / ``control-wins`` /
    ``tie``) additionally needs **each arm** to carry at least ``min_arm_n`` scored
    records (``Settings.verdict_min_arm_n`` when not given). A record is one evaluator
    score of one session (``routing.ab_results``), so an arm's count is its largest
    per-evaluator ``sampleSize`` ≈ the sessions of that arm that reached the judge.
    Below the bar the outcome is ``insufficient-n`` whatever the p-value says — on four
    treatment sessions a "significant" Welch test is noise — and the reason names both
    arms and the bar. ``arms_n`` / ``min_arm_n`` ride on every verdict that saw means,
    so the console and the e2e can show "T1 4/10 records — run another round".
    """
    if min_arm_n is None:
        min_arm_n = int(get_settings().verdict_min_arm_n)
    # P3.2: an evaluator whose rows come from more than one judge is not a comparison —
    # it is kept out of the aggregate, and a mixed primary (or nothing clean) refuses
    mixed = [m for m in metrics if m.get("judgeMismatch")]
    if mixed:
        excluded = [metric_evaluator_id(m) for m in mixed]
        clean = [m for m in metrics if not m.get("judgeMismatch")]
        if (primary is not None and primary in excluded) or not clean:
            return _judge_mismatch(mixed, primary)
        result = compute_verdict(clean, min_n, min_arm_n, primary=primary, design=design,
                                 decided=decided)
        return {**result, "excluded_judge_mismatch": excluded}
    if design is not None:
        return _designed_verdict(metrics, primary, design, decided)
    if not metrics:
        if primary is not None:
            return _primary_verdict(metrics, primary, min_n=min_n, min_arm_n=min_arm_n,
                                    avg_delta=None)
        return {"verdict": "insufficient-data",
                **reason_of("no_metrics", "no evaluator metrics yet")}
    weighted_delta = 0.0
    weight_total = 0.0
    total_n = 0
    control_n = 0
    treatment_n = 0
    significant = False
    for metric in metrics:
        polarity = routing.evaluator_polarity(
            metric.get("evaluatorId") or metric.get("label") or ""
        )
        control = metric.get("control", {})
        for variant in metric.get("variants", []):
            c_mean, t_mean = control.get("mean"), variant.get("mean")
            c_n = int(control.get("sampleSize") or 0)
            v_n = int(variant.get("sampleSize") or 0)
            total_n += c_n + v_n
            control_n = max(control_n, c_n)
            treatment_n = max(treatment_n, v_n)
            if c_mean is not None and t_mean is not None:
                # means with no reported sample size still carry evidence — keep
                # them in the average at unit weight rather than dropping them
                weight = float(min(c_n, v_n)) or 1.0
                weighted_delta += polarity * (t_mean - c_mean) * weight
                weight_total += weight
            if variant.get("isSignificant"):
                significant = True
    if primary is not None:
        avg = round(weighted_delta / weight_total, 4) if weight_total else None
        return _primary_verdict(metrics, primary, min_n=min_n, min_arm_n=min_arm_n,
                                avg_delta=avg)
    if not weight_total:
        return {"verdict": "insufficient-data", **reason_of("no_means", "arms have no means yet")}
    avg_delta = weighted_delta / weight_total
    arms_n = {"control": control_n, "treatment": treatment_n}
    if total_n < min_n * 2 or min(control_n, treatment_n) < min_arm_n:
        return {
            "verdict": "insufficient-n",
            **reason_of("arm_n_below_bar",
                   f"a directional verdict needs at least {min_arm_n} scored records per arm "
                   f"(one evaluator score per session) — control has {control_n}, treatment "
                   f"has {treatment_n}; send another traffic round",
                   min_arm_n=min_arm_n, control=control_n, treatment=treatment_n),
            "avg_delta": round(avg_delta, 4),
            "n": total_n,
            "arms_n": arms_n,
            "min_arm_n": min_arm_n,
        }
    winner = _winner(avg_delta)
    return {
        "verdict": f"{winner}-wins" if winner != "tie" else "tie",
        "avg_delta": round(avg_delta, 4),
        "n": total_n,
        "significant": significant,
        "arms_n": arms_n,
        "min_arm_n": min_arm_n,
    }


def _judge_mismatch(mixed: list[dict[str, Any]], primary: str | None) -> dict[str, Any]:
    """The refusal when the deciding evidence was scored by more than one judge (P3.2)."""
    judges = sorted({j for m in mixed for j in m.get("judges") or []})
    names = ", ".join(metric_evaluator_id(m) for m in mixed)
    return {
        "verdict": "judge-mismatch",
        **reason_of("judge_mismatch",
               f"{names} scored by more than one judge model ({', '.join(judges)}); "
               "arms judged by different models are not comparable — send a traffic "
               "round scored by one judge",
               evaluators=[metric_evaluator_id(m) for m in mixed], judges=judges),
        "judges": judges,
        **({"primary": primary} if primary else {}),
    }


def _primary_verdict(
    metrics: list[dict[str, Any]], primary: str, *, min_n: int, min_arm_n: int,
    avg_delta: float | None,
) -> dict[str, Any]:
    """The verdict of the primary evaluator alone: winner = the sign of its
    polarity-oriented delta, ``significant`` = its own Welch test, ``n`` / ``arms_n`` = its
    own scored records.

    The N-SCN-obs14 per-arm bar applies to the PRIMARY's arm counts (``min_arm_n`` scored
    records of this evaluator in each arm), not to the aggregate: the evaluator that decides
    is the one whose evidence must be sufficient. ``avg_delta`` (the all-evaluator
    aggregate) is kept for display only. A primary that produced no comparable means is
    ``insufficient-data`` — the other evaluators never stand in for it."""
    base: dict[str, Any] = {"primary": primary, "min_arm_n": min_arm_n}
    if avg_delta is not None:
        base["avg_delta"] = avg_delta
    metric = next((m for m in metrics if metric_evaluator_id(m) == primary), None)
    control = (metric or {}).get("control") or {}
    variant = next(iter((metric or {}).get("variants") or []), None) or {}
    c_mean, t_mean = control.get("mean"), variant.get("mean")
    if metric is None or c_mean is None or t_mean is None:
        return {**base, "verdict": "insufficient-data",
                **reason_of("primary_no_scores",
                       f"primary evaluator {primary} has no scores on both arms yet",
                       primary=primary),
                "n": 0}
    c_n, t_n = int(control.get("sampleSize") or 0), int(variant.get("sampleSize") or 0)
    n = c_n + t_n
    delta = routing.evaluator_polarity(primary) * (t_mean - c_mean)
    base["primary_delta"] = round(delta, 4)
    base["n"] = n
    base["arms_n"] = {"control": c_n, "treatment": t_n}
    if n < min_n * 2 or min(c_n, t_n) < min_arm_n:
        return {
            **base,
            "verdict": "insufficient-n",
            **reason_of("primary_arm_n_below_bar",
                   f"a directional verdict needs at least {min_arm_n} scored records per arm "
                   f"from the primary evaluator {primary} — control has {c_n}, treatment has "
                   f"{t_n}; send another traffic round",
                   primary=primary, min_arm_n=min_arm_n, control=c_n, treatment=t_n),
        }
    winner = _winner(delta)
    return {
        **base,
        "verdict": f"{winner}-wins" if winner != "tie" else "tie",
        "significant": bool(variant.get("isSignificant")),
        **secondary_block(metrics, primary),
    }


MULTIPLE_COMPARISONS_NOTE = (
    "only the primary evaluator decides; the other evaluators are reference only — their "
    "p-values are Holm-adjusted across the {k} secondary evaluator(s), and a secondary "
    "'significant' result is a lead to investigate, not a verdict")


def secondary_block(metrics: list[dict[str, Any]], primary: str,
                    alpha: float = routing.SIGNIFICANCE_ALPHA) -> dict[str, Any]:
    """``{secondary: [{evaluator, delta, p, p_holm, significant_holm}], multiple_comparisons}``
    for every evaluator but the primary that has means on both arms (R3-d)."""
    rows: list[dict[str, Any]] = []
    for metric in metrics:
        evaluator = metric_evaluator_id(metric)
        if evaluator == primary:
            continue
        control = metric.get("control") or {}
        variant = next(iter(metric.get("variants") or []), None) or {}
        if control.get("mean") is None or variant.get("mean") is None:
            continue
        delta = routing.evaluator_polarity(evaluator) * (variant["mean"] - control["mean"])
        rows.append({"evaluator": evaluator, "delta": round(delta, 4),
                     "p": variant.get("pValue")})
    with_p = [r for r in rows if r["p"] is not None]
    for row, adjusted in zip(with_p, stats.holm([float(r["p"]) for r in with_p]),
                             strict=True):
        row["p_holm"] = round(adjusted, 6)
        row["significant_holm"] = adjusted < alpha
    for row in rows:
        row.setdefault("p_holm", None)
        row.setdefault("significant_holm", False)
    if not rows:
        return {}
    return {"secondary": rows,
            "multiple_comparisons": MULTIPLE_COMPARISONS_NOTE.format(k=len(rows)),
            "multiple_comparisons_k": len(rows)}  # N17-obs443: the console words it


def _designed_verdict(metrics: list[dict[str, Any]], primary: str | None,
                      design: dict[str, Any], decided: dict[str, Any] | None,
                      ) -> dict[str, Any]:
    """The fixed-n verdict (R3-d). ``insufficient-n`` until BOTH arms of the primary hold
    ``planned_n_per_arm`` sessions, whatever an interim p-value says; then ONE test: a
    direction needs ``p < alpha`` (else ``tie`` — no significant difference); the first
    decision is stored by the caller and returned frozen afterwards."""
    if decided:
        return {**decided, "frozen": True}
    planned = int(design.get("planned_n_per_arm") or 0)
    alpha = float(design.get("alpha") or routing.SIGNIFICANCE_ALPHA)
    base: dict[str, Any] = {"design": dict(design)}
    if primary is None:
        return {**base, "verdict": "insufficient-data",
                **reason_of("design_no_primary",
                       "a fixed-n design decides on one primary evaluator — none chosen")}
    base["primary"] = primary
    metric = next((m for m in metrics if metric_evaluator_id(m) == primary), None)
    control = (metric or {}).get("control") or {}
    variant = next(iter((metric or {}).get("variants") or []), None) or {}
    c_mean, t_mean = control.get("mean"), variant.get("mean")
    if metric is None or c_mean is None or t_mean is None:
        return {**base, "verdict": "insufficient-data",
                **reason_of("primary_no_scores",
                       f"primary evaluator {primary} has no scores on both arms yet",
                       primary=primary),
                "n": 0}
    c_n, t_n = int(control.get("sampleSize") or 0), int(variant.get("sampleSize") or 0)
    delta = routing.evaluator_polarity(primary) * (t_mean - c_mean)
    base.update({"primary_delta": round(delta, 4), "n": c_n + t_n,
                 "arms_n": {"control": c_n, "treatment": t_n}, "min_arm_n": planned})
    if min(c_n, t_n) < planned:
        return {**base, "verdict": "insufficient-n",
                **reason_of("design_below_planned_n",
                       f"fixed-n design: the primary evaluator {primary} is tested once "
                       f"both arms hold {planned} per arm (no peeking at interim "
                       f"rounds) — control has {c_n}, treatment has {t_n}; send "
                       "another traffic round",
                       primary=primary, planned=planned, control=c_n, treatment=t_n)}
    p = variant.get("pValue")
    if p is None:
        return {**base, "verdict": "insufficient-data",
                **reason_of("primary_no_p_value",
                       f"primary evaluator {primary} has no p-value (fewer than two "
                       "observations in an arm)", primary=primary)}
    significant = float(p) < alpha
    winner = _winner(delta) if significant else "tie"
    out = {**base, "verdict": f"{winner}-wins" if winner != "tie" else "tie",
           "significant": significant, "p_value": round(float(p), 6),
           "decided_at_n": {"control": c_n, "treatment": t_n},
           **secondary_block(metrics, primary, alpha)}
    if winner == "tie":
        out.update(reason_of("not_significant",
                        f"no significant difference on {primary} at α={alpha:g} "
                        f"(p={float(p):.3g}, Δ={delta:+.3f}) at the planned "
                        f"{planned} per arm",
                        primary=primary, alpha=alpha, p=round(float(p), 6),
                        delta=round(delta, 4), planned=planned))
    return out


def _winner(delta: float) -> str:
    """``treatment`` / ``control`` by the sign of a polarity-oriented delta — ``tie`` when
    it is float noise (N17-obs388: identical constant arms 0.833 vs 0.8329999999999999
    gave Δ −1.1e-16, a "control-wins" that ``complete`` refuses, so an identical candidate
    could never be promoted)."""
    if abs(delta) < stats.MEAN_TOLERANCE:
        return "tie"
    return "treatment" if delta > 0 else "control"


HELD_OUT_NOTE = "reported only — the verdict decides on all replayed scenarios"


def held_out_block(metrics: list[dict[str, Any]], primary: str | None) -> dict[str, Any] | None:
    """R3-c: the deciding evaluator over the HELD-OUT scenarios' sessions only —
    ``{evaluator, arms_n, delta, p, significant, note}``; ``None`` without such sessions or
    means. The primary when set, else the first evaluator with means on both arms."""
    candidates = [m for m in metrics
                  if (primary is None or metric_evaluator_id(m) == primary)
                  and (m.get("control") or {}).get("mean") is not None
                  and (next(iter(m.get("variants") or []), None) or {}).get("mean") is not None]
    if not candidates:
        return None
    metric = candidates[0]
    evaluator = metric_evaluator_id(metric)
    control, variant = metric["control"], metric["variants"][0]
    delta = routing.evaluator_polarity(evaluator) * (variant["mean"] - control["mean"])
    p = variant.get("pValue")
    return {"evaluator": evaluator,
            "arms_n": {"control": int(control.get("sampleSize") or 0),
                       "treatment": int(variant.get("sampleSize") or 0)},
            "delta": round(delta, 4), "p": None if p is None else round(float(p), 6),
            "significant": bool(variant.get("isSignificant")), "note": HELD_OUT_NOTE}

