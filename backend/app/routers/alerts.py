"""Spend attribution and threshold alerts (roadmap T28/T29).

Two read surfaces and one small CRUD: what this workspace spent and who spent it, and the
thresholds it watches. Both compute from data the platform already collects — there is no
new telemetry path here, only attribution and a comparison.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import current_project
from app.models.ledger import AlertRule
from app.routers.auth import Identity, current_identity, require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import access
from app.services import alerts as alert_service
from app.services import costs as cost_service

router = APIRouter(prefix="/api", tags=["costs-alerts"])


class RuleCreate(BaseModel):
    kind: Literal["error_rate", "latency_p95_ms", "online_quality", "cost_mtd_usd"]
    name: str = Field(default="", max_length=64)
    threshold: float
    # Defaults to the only direction the kind can meaningfully fire in, so the common
    # case needs no thought and the wrong one is refused rather than silently inert.
    comparison: Literal["above", "below"] | None = None
    window: Literal["1h", "6h", "24h", "7d", "30d"] = "24h"
    enabled: bool = True
    webhook_url: str | None = Field(default=None, max_length=512)
    # What the rule measures (design §5.9): the whole workspace (the default, and the
    # only meaning a rule had before projects), or one project's agents — the
    # request's project unless `project_id` names another one bound here.
    scope: Literal["workspace", "project"] = "workspace"
    project_id: str | None = Field(default=None, max_length=32)


class RulePatch(BaseModel):
    """Only the fields present change; `webhook_url: null` clears the destination."""

    name: str | None = Field(default=None, max_length=64)
    threshold: float | None = None
    window: Literal["1h", "6h", "24h", "7d", "30d"] | None = None
    enabled: bool | None = None
    webhook_url: str | None = Field(default=None, max_length=512)


def _rule_in(db: Session, ws: WorkspaceScope, rule_id: str) -> AlertRule:
    rule = db.get(AlertRule, rule_id)
    if rule is None or rule.workspace_id != ws.id:
        raise AppError("alert.not_found", "alert rule not found", status_code=404)
    return rule


def _bound_project(db: Session, ws: WorkspaceScope, project_id: str) -> str:
    """A project id the caller named, when it is bound to this workspace — else the
    404 an unknown project answers everywhere (`project.not_found`)."""
    if project_id not in {b.project_id for b in access.workspace_bindings(db, ws.id)}:
        raise NotFoundError("project.not_found", f"project '{project_id}' not found",
                            {"project_id": project_id})
    return project_id


def _check_webhook(url: str | None) -> str | None:
    """Only an https destination, and only when one is given.

    An http webhook would put the alert text — which names the workspace and the breach —
    on the wire in clear, and the platform has no reason to allow that.
    """
    if url is None:
        return None
    cleaned = url.strip()
    if not cleaned:
        return None
    if not cleaned.startswith("https://"):
        raise AppError("alert.webhook_not_https", "a webhook URL must be https://")
    return cleaned


# ── T28: spend ───────────────────────────────────────────────────────────────────


@router.get("/costs")
def get_costs(
    request: Request,
    range: str = Query(default=cost_service.DEFAULT_RANGE),
    project: str | None = Query(default=None, max_length=32),
    start: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Estimated spend for the window, by agent, by person, by project and by model.

    `range` is a trailing window (1h…30d) or a calendar period in UTC, like the AWS
    bill: today, this_week / last_week (Monday first), this_month / last_month, or
    custom with `start` / `end` dates (inclusive, at most 92 days).

    Advisory by construction: prices come from the config map matched by substring, and a
    model missing from it contributes tokens but no dollars — those models are named in
    `unpriced_models` so the total is never quietly short. `project` narrows it to one
    project's agents and sessions (the route runs outside any project: an
    administrator's cross-project view).
    """
    if range not in cost_service.RANGES and range not in cost_service.PERIODS:
        raise AppError(
            "costs.bad_range",
            f"range must be one of {', '.join((*cost_service.RANGES, *cost_service.PERIODS))}",
            status_code=422,
        )
    if project is not None:
        _bound_project(db, ws, project)
    report = cost_service.cost_report(
        db, ws.context, range_key=range, force=force, project=project, start=start, end=end
    )
    from app.services import steward as steward_service

    if steward_service.is_steward(require_identity(request)):
        # an organization admin sees the totals, never who or what spent them (§11.4)
        report = {k: v for k, v in report.items() if not k.startswith("by_")}
    return report


@router.get("/costs/month-to-date")
def get_month_to_date(
    project: str | None = Query(default=None, max_length=32),
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """The figure a `cost_mtd_usd` alert compares against (one project's with `project`)."""
    if project is not None:
        _bound_project(db, ws, project)
    usd = cost_service.month_to_date_usd(db, ws.context, force=force, project=project)
    return {
        "workspace_id": ws.id,
        **({"project": project} if project is not None else {}),
        "est_cost_usd": usd,
    }


# ── T29: alert rules ─────────────────────────────────────────────────────────────


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


@router.post("/alerts", status_code=201)
def create_alert(
    req: RuleCreate,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    identity: Identity = Depends(current_identity),
) -> dict[str, Any]:
    comparison = req.comparison or alert_service.NATURAL_COMPARISON[req.kind]
    alert_service.validate(req.kind, comparison, req.window, req.threshold)
    project_id = None
    if req.scope == "project":
        project_id = _bound_project(db, ws, req.project_id or current_project() or "")
    rule = AlertRule(
        workspace_id=ws.id,
        kind=req.kind,
        name=req.name.strip(),
        comparison=comparison,
        threshold=req.threshold,
        window=req.window,
        enabled=req.enabled,
        webhook_url=_check_webhook(req.webhook_url),
        created_by=identity.username,
        project_id=project_id,
    )
    db.add(rule)
    if project_id is None:
        # Flush stamps a new row with the request's project; a workspace-level rule
        # is NULL by choice, so it is written back once the row exists (an UPDATE is
        # never restamped).
        db.flush()
        rule.project_id = None
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
            sent.get("window", rule.window),
            sent.get("threshold", rule.threshold),
        )
    if "name" in sent and req.name is not None:
        rule.name = req.name.strip()
    if "threshold" in sent and req.threshold is not None:
        rule.threshold = req.threshold
    if "window" in sent and req.window is not None:
        rule.window = req.window
    if "enabled" in sent and req.enabled is not None:
        rule.enabled = req.enabled
    if "webhook_url" in sent:
        rule.webhook_url = _check_webhook(req.webhook_url)
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
    notify: bool = True,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Re-read every enabled rule now.

    Each read is billed or cached, so evaluation is explicit rather than a poll behind
    every page load. `notify=false` re-reads without delivering — what a console refresh
    wants, so opening the page cannot page anyone.
    """
    return alert_service.evaluate_rules(db, ws.context, notify_transitions=notify)
