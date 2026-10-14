"""Threshold alerts over data the platform already collects.

The smallest honest mechanism:

* **A rule is a threshold on a value the platform can already compute** — error rate and
  p95 latency from the spans the Observability dashboard reads, online quality from the
  evaluation results it already aggregates, month-to-date spend from the price map
  (`services/costs.py`). No new telemetry path, no agent-side change.
* **Evaluation is explicit.** Every read is billed (Logs Insights) or cached, so the
  whole set is evaluated in one pass: by an administrator's "Re-check now" (which never
  notifies) or by the opt-in background thread (`services/alert_scheduler.py`, the only
  caller that notifies).
* **A transition notifies, not a state.** `state` is remembered on the row, so a rule that
  stays firing does not re-notify on every pass. One notification is owed per firing
  episode (`last_fired_at` = when the rule last *started* firing): a manual re-check that
  saw the transition first records it without posting, and the next background pass
  still delivers it — a console refresh must not swallow the page.
* **Delivery is one generic JSON webhook** (`services/webhook_safety.py`). A Slack or
  Feishu incoming hook already *is* one. A failed delivery never fails the evaluation:
  the state is still recorded and the outcome lands in `last_detail`.

A value that cannot be read (no telemetry yet, a query failure) yields `unknown`, never a
silent `ok` — an alert that reports health it did not measure is worse than no alert.

Kinds and their windows:

| kind | value | window |
|---|---|---|
| `error_rate` | failed traces / traces (0–1) | the rule's window, 1h/6h/24h/7d |
| `latency_p95_ms` | p95 trace latency, ms | the rule's window, 1h/6h/24h/7d |
| `online_quality` | polarity-normalised online-eval mean (0–1) | fixed 24 h (ignores window) |
| `spend_usd` | estimated spend over the window | the rule's window, 1h/6h/24h/7d/30d |
| `cost_mtd_usd` | estimated spend since the UTC month start | the month (ignores window) |
"""

import math
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.errors import AppError
from app.models.ledger import AlertRule
from app.services import costs, creators, webhook_safety
from app.services.workspace import WorkspaceContext

KINDS = ("error_rate", "latency_p95_ms", "online_quality", "spend_usd", "cost_mtd_usd")
Kind = Literal["error_rate", "latency_p95_ms", "online_quality", "spend_usd", "cost_mtd_usd"]
COMPARISONS = ("above", "below")
STATE_OK = "ok"
STATE_FIRING = "firing"
STATE_UNKNOWN = "unknown"
MAX_DETAIL_CHARS = 500

# Which direction each kind is bad in, so a rule created with the wrong comparison is
# refused rather than silently never firing.
NATURAL_COMPARISON: dict[str, str] = {
    "error_rate": "above",
    "latency_p95_ms": "above",
    "online_quality": "below",
    "spend_usd": "above",
    "cost_mtd_usd": "above",
}

WINDOWS = ("1h", "6h", "24h", "7d", "30d")
# Error rate and latency come from the observability dashboard, which has no 30-day range.
DASHBOARD_KINDS = ("error_rate", "latency_p95_ms")
DASHBOARD_WINDOWS = ("1h", "6h", "24h", "7d")


def validate(kind: str, comparison: str, window: str, threshold: float) -> None:
    if kind not in KINDS:
        raise AppError("alert.unknown_kind", f"kind must be one of {', '.join(KINDS)}")
    if comparison not in COMPARISONS:
        raise AppError("alert.bad_comparison", "comparison must be 'above' or 'below'")
    if comparison != NATURAL_COMPARISON[kind]:
        raise AppError(
            "alert.comparison_never_fires",
            f"a '{kind}' rule is only meaningful as "
            f"'{NATURAL_COMPARISON[kind]}' — the other direction would never fire",
        )
    if window not in WINDOWS:
        raise AppError("alert.bad_window", f"window must be one of {', '.join(WINDOWS)}")
    if kind in DASHBOARD_KINDS and window not in DASHBOARD_WINDOWS:
        raise AppError(
            "alert.bad_window",
            f"a '{kind}' rule reads the observability dashboard, whose windows are "
            f"{', '.join(DASHBOARD_WINDOWS)}",
        )
    if not math.isfinite(threshold):
        raise AppError("alert.bad_threshold", "threshold must be a finite number")
    if kind == "online_quality" and not 0.0 <= threshold <= 1.0:
        raise AppError("alert.bad_threshold", "online_quality is a 0–1 score")
    # a fraction, not a percentage: 5 would mean 500 % and never fire
    if kind == "error_rate" and not 0.0 <= threshold <= 1.0:
        raise AppError("alert.bad_threshold", "error_rate is a 0–1 fraction (0.05 = 5 %)")
    if threshold < 0:
        raise AppError("alert.bad_threshold", "threshold must not be negative")


# ── reading the values ───────────────────────────────────────────────────────────


def _dashboard_value(
    kind: str, window: str, workspace: WorkspaceContext
) -> tuple[float | None, str]:
    """Error rate (0–1) or p95 latency (ms) from the observability dashboard."""
    from app.services import observability

    if window not in observability.RANGE_HOURS:
        return None, f"window '{window}' is not an observability range"
    try:
        data = observability.get_dashboard(window, workspace)
    except Exception as exc:  # noqa: BLE001 - any read failure is `unknown`, never `ok`
        return None, f"could not read telemetry: {type(exc).__name__}"
    # the dashboard nests its numbers under `tiles` (observability.get_dashboard)
    tiles = data.get("tiles") or {}
    trace_tile = tiles.get("traces") or {}
    traces = int(trace_tile.get("total") or 0)
    if traces == 0:
        return None, "no traffic in this window"
    if kind == "error_rate":
        errors = int(trace_tile.get("error") or 0)
        return errors / traces, f"{errors}/{traces} traces failed"
    value = (tiles.get("latency") or {}).get("p95_ms")
    if value is None:
        return None, "no latency percentile in this window"
    return float(value), f"p95 over {traces} traces"


def _quality_value(db: Session, workspace: WorkspaceContext) -> tuple[float | None, str]:
    from app.evaluation.online import online_quality

    try:
        # Viewer.ALL: an aggregate over every agent-owned config; every alerts route
        # is ADMIN, and admins see every agent under isolation anyway.
        snapshot = online_quality(db, workspace)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not read online evaluation: {type(exc).__name__}"
    mean = snapshot.get("mean")
    if mean is None:
        return None, "no online evaluation scores in the last 24 h"
    return (
        float(mean),
        f"{snapshot.get('scores')} score(s) over {snapshot.get('sessions')} session(s)",
    )


def _cost_value(db: Session, workspace: WorkspaceContext) -> tuple[float | None, str]:
    try:
        usd = costs.month_to_date_usd(db, workspace)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not estimate spend: {type(exc).__name__}"
    return usd, "month-to-date estimate from the price map"


def _spend_value(
    window: str, db: Session, workspace: WorkspaceContext
) -> tuple[float | None, str]:
    try:
        report = costs.cost_report(db, workspace, range_key=window)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not estimate spend: {type(exc).__name__}"
    return float(report.get("total_est_cost_usd") or 0.0), f"{window} estimate from the price map"


def read_value(
    rule: AlertRule, db: Session, workspace: WorkspaceContext
) -> tuple[float | None, str]:
    if rule.kind in DASHBOARD_KINDS:
        return _dashboard_value(rule.kind, rule.window or "24h", workspace)
    if rule.kind == "online_quality":
        return _quality_value(db, workspace)
    if rule.kind == "spend_usd":
        return _spend_value(rule.window or "24h", db, workspace)
    if rule.kind == "cost_mtd_usd":
        return _cost_value(db, workspace)
    return None, f"unknown kind '{rule.kind}'"


def breached(rule: Any, value: float) -> bool:
    return value > rule.threshold if rule.comparison == "above" else value < rule.threshold


# ── delivery ─────────────────────────────────────────────────────────────────────


def notify(rule: AlertRule, value: float, detail: str, workspace_id: str) -> str | None:
    """Deliver the transition, if the rule has somewhere to send it.

    Returns a short outcome string, or None when the rule has no webhook.
    """
    if not rule.webhook_url:
        return None
    detail = detail[:MAX_DETAIL_CHARS]
    text = (
        f"[Launchpad/{workspace_id}] {rule.name or rule.kind} is firing: "
        f"{rule.kind} = {value:g} ({rule.comparison} {rule.threshold:g}) — {detail}"
    )
    # `text` is what a Slack or Feishu incoming hook renders; the structured fields ride
    # alongside for anything else consuming the same hook.
    _, outcome = webhook_safety.post_json(
        rule.webhook_url,
        {
            "text": text,
            "workspace_id": workspace_id,
            "rule_id": rule.id,
            "kind": rule.kind,
            "value": value,
            "threshold": rule.threshold,
            "comparison": rule.comparison,
            "detail": detail,
        },
    )
    return outcome


# ── evaluation ───────────────────────────────────────────────────────────────────


def evaluate_rules(
    db: Session,
    workspace: WorkspaceContext,
    *,
    notify_transitions: bool = False,
) -> dict[str, Any]:
    """Re-read every enabled rule in this workspace and record what it saw.

    Notifies only when ``notify_transitions`` (the scheduler), once per firing episode
    (an ok/unknown → firing transition), even when a manual pass saw the transition
    first. A delivery result is recorded in `last_detail` rather than raised: a broken
    webhook must not hide the breach.

    A read can take tens of seconds (Logs Insights), and a rule may be deleted from the
    console meanwhile. Each rule is therefore re-read before its read, checked again
    after it, and committed on its own: a rule that vanished is skipped (not written
    back, not notified) instead of failing the whole pass with a stale UPDATE.
    """
    rule_ids = list(
        db.scalars(
            select(AlertRule.id)
            .where(AlertRule.workspace_id == workspace.id)
            .order_by(AlertRule.created_at)
        )
    )
    now = datetime.now(UTC)
    evaluated: list[dict[str, Any]] = []
    for rule_id in rule_ids:
        rule = db.get(AlertRule, rule_id)
        if rule is None:
            continue  # deleted since the list was read
        if not rule.enabled:
            evaluated.append(rule_out(rule, skipped="disabled"))
            continue
        value, detail = read_value(rule, db, workspace)
        if not _still_exists(db, rule_id):
            db.expunge(rule)  # deleted while its value was being read
            continue
        previous = rule.state
        if value is None:
            rule.state = STATE_UNKNOWN
        else:
            rule.state = STATE_FIRING if breached(rule, value) else STATE_OK
        rule.last_value = value
        rule.last_detail = detail
        rule.last_checked_at = now
        if rule.state == STATE_FIRING:
            if previous != STATE_FIRING or rule.last_fired_at is None:
                rule.last_fired_at = now  # this firing episode starts here
            if notify_transitions and value is not None and notification_owed(rule):
                outcome = notify(rule, value, detail, workspace.id)
                if outcome is not None:
                    rule.last_notified_at = now
                    rule.last_detail = f"{detail} · webhook {outcome}"
        row = rule_out(rule)
        try:
            db.commit()
        except StaleDataError:
            db.rollback()  # deleted between the check and the write
            continue
        evaluated.append(row)
    return {
        "workspace_id": workspace.id,
        "evaluated_at": now.isoformat(),
        "rules": evaluated,
        "firing": len([row for row in evaluated if row["state"] == STATE_FIRING]),
        "unknown": len([row for row in evaluated if row["state"] == STATE_UNKNOWN]),
    }


def _still_exists(db: Session, rule_id: str) -> bool:
    """A fresh query, not the identity map: has the rule been deleted meanwhile?"""
    return db.scalar(select(AlertRule.id).where(AlertRule.id == rule_id)) is not None


def _utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; every stored one is UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def notification_owed(rule: AlertRule) -> bool:
    """A firing rule with a webhook that has not been notified for this episode.

    The episode starts at `last_fired_at` (the transition into firing). A manual pass
    records the transition without posting, so the owed notification is still sent by
    the next background pass rather than lost.
    """
    if rule.state != STATE_FIRING or not rule.webhook_url or rule.last_fired_at is None:
        return False
    notified = _utc(rule.last_notified_at)
    return notified is None or notified < _utc(rule.last_fired_at)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def rule_out(rule: AlertRule, skipped: str | None = None) -> dict[str, Any]:
    """The API shape of a rule. Never the webhook URL — only whether there is one."""
    return {
        "id": rule.id,
        "kind": rule.kind,
        "name": rule.name,
        "comparison": rule.comparison,
        "threshold": rule.threshold,
        "window": rule.window,
        "enabled": rule.enabled,
        "has_webhook": bool(rule.webhook_url),
        "state": rule.state,
        "last_value": rule.last_value,
        "last_detail": rule.last_detail,
        "last_checked_at": _iso(rule.last_checked_at),
        "last_fired_at": _iso(rule.last_fired_at),
        "last_notified_at": _iso(rule.last_notified_at),
        **creators.fields(rule),
        "created_at": _iso(rule.created_at),
        "updated_at": _iso(rule.updated_at),
        **({"skipped": skipped} if skipped else {}),
    }
