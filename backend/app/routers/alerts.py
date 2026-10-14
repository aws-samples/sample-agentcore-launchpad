"""Spend attribution and threshold alerts.

Two read surfaces and one small CRUD: what this workspace spent and who spent it, and the
thresholds it watches. Both compute from data the platform already collects — there is no
new telemetry path here, only attribution and a comparison. Every route is ADMIN
(`core/route_policy.py`): the per-person breakdown names who spent what, and a rule's
value aggregates over agents a member may not see under workspace isolation.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError
from app.models.ledger import AlertRule
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import alert_scheduler, creators
from app.services import alerts as alert_service
from app.services import costs as cost_service
from app.services.creators import Creator
from app.services.webhook_safety import check_webhook_url

router = APIRouter(prefix="/api", tags=["costs-alerts"])

Window = Literal["1h", "6h", "24h", "7d", "30d"]


class RuleCreate(BaseModel):
    kind: alert_service.Kind
    name: str = Field(default="", max_length=64)
    threshold: float
    # Defaults to the only direction the kind can meaningfully fire in, so the common
    # case needs no thought and the wrong one is refused rather than silently inert.
    comparison: Literal["above", "below"] | None = None
    window: Window = "24h"
    enabled: bool = True
    webhook_url: str | None = Field(default=None, max_length=512)


class RulePatch(BaseModel):
    """Only the fields present change; `webhook_url: null` or `""` clears it."""

    name: str | None = Field(default=None, max_length=64)
    threshold: float | None = None
    window: Window | None = None
    enabled: bool | None = None
    webhook_url: str | None = Field(default=None, max_length=512)


def _rule_in(db: Session, ws: WorkspaceScope, rule_id: str) -> AlertRule:
    rule = db.get(AlertRule, rule_id)
    if rule is None or rule.workspace_id != ws.id:
        raise AppError("alert.not_found", "alert rule not found", status_code=404)
    return rule


def _webhook(url: str | None) -> str | None:
    """None/blank clears; anything else must pass the SSRF guard."""
    if url is None or not url.strip():
        return None
    return check_webhook_url(url)


# ── spend ────────────────────────────────────────────────────────────────────────


@router.get("/costs")
def get_costs(
    range: str = Query(default=cost_service.DEFAULT_RANGE),
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Estimated spend for the window, by agent and by person.

    Advisory by construction: prices come from the config map matched by substring, and a
    model missing from it contributes tokens but no dollars — those models are named in
    `unpriced_models` so the total is never quietly short.
    """
    if range not in cost_service.RANGES:
        raise AppError(
            "costs.bad_range",
            f"range must be one of {', '.join(cost_service.RANGES)}",
            status_code=422,
        )
    return cost_service.cost_report(db, ws.context, range_key=range, force=force)


@router.get("/costs/month-to-date")
def get_month_to_date(
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Spend since the UTC month start — the figure a `cost_mtd_usd` alert compares."""
    mtd = cost_service.month_to_date(db, ws.context, force=force)
    return {
        "workspace_id": ws.id,
        "est_cost_usd": mtd["usd"],
        "hours": mtd["hours"],
        "unpriced_models": mtd.get("unpriced_models") or [],
        "estimate": True,
    }


# ── alert rules ──────────────────────────────────────────────────────────────────


@router.get("/alerts")
def list_alerts(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """The rules and their last known state — a ledger read, no AWS call."""
    rows = db.scalars(
        select(AlertRule)
        .where(AlertRule.workspace_id == ws.id)
        .order_by(AlertRule.created_at)
    ).all()
    return {
        "rules": [alert_service.rule_out(row) for row in rows],
        "firing": len([row for row in rows if row.state == alert_service.STATE_FIRING]),
        "kinds": list(alert_service.KINDS),
    }


@router.get("/alerts/scheduler")
def get_scheduler() -> dict[str, Any]:
    """Whether THIS box runs the background check (the only path that notifies)."""
    settings = get_settings()
    return {
        "enabled": bool(settings.alerts_scheduler_enabled),
        "interval_seconds": alert_scheduler.interval_seconds(settings),
    }


@router.post("/alerts", status_code=201)
def create_alert(
    req: RuleCreate,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    comparison = req.comparison or alert_service.NATURAL_COMPARISON[req.kind]
    alert_service.validate(req.kind, comparison, req.window, req.threshold)
    rule = AlertRule(
        workspace_id=ws.id,
        kind=req.kind,
        name=req.name.strip(),
        comparison=comparison,
        threshold=req.threshold,
        window=req.window,
        enabled=req.enabled,
        webhook_url=_webhook(req.webhook_url),
    )
    creators.stamp(rule, Creator.of_request(request))
    db.add(rule)
    db.commit()
    return alert_service.rule_out(rule)


@router.patch("/alerts/{rule_id}")
def update_alert(
    rule_id: str,
    req: RulePatch,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    rule = _rule_in(db, ws, rule_id)
    sent = req.model_dump(exclude_unset=True)
    if "threshold" in sent or "window" in sent:
        alert_service.validate(
            rule.kind,
            rule.comparison,
            sent.get("window") or rule.window,
            sent["threshold"] if sent.get("threshold") is not None else rule.threshold,
        )
    if req.name is not None:
        rule.name = req.name.strip()
    if req.threshold is not None:
        rule.threshold = req.threshold
    if req.window is not None:
        rule.window = req.window
    if req.enabled is not None:
        rule.enabled = req.enabled
    if "webhook_url" in sent:
        rule.webhook_url = _webhook(req.webhook_url)
    db.commit()
    return alert_service.rule_out(rule)


@router.delete("/alerts/{rule_id}")
def delete_alert(
    rule_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    rule = _rule_in(db, ws, rule_id)
    db.delete(rule)
    db.commit()
    return {"deleted": rule_id}


@router.post("/alerts/evaluate")
def evaluate_alerts(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Re-read every enabled rule now. Never notifies.

    A manual check is what a console refresh wants, so opening the page cannot page
    anyone; the opt-in background thread is the only notifier.
    """
    return alert_service.evaluate_rules(db, ws.context, notify_transitions=False)
