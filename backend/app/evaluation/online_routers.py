"""Console API for per-agent online evaluation configs (`/api/eval/online`)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.evaluation import online
from app.evaluation.online_evaluators import ONLINE_EVAL_MAX
from app.models.ledger import Agent
from app.routers.workspaces import WorkspaceScope, require_workspace

router = APIRouter(prefix="/api/eval/online", tags=["evaluation-online"])

Operator = Literal[
    "Equals", "NotEquals", "GreaterThan", "LessThan",
    "GreaterThanOrEqual", "LessThanOrEqual", "Contains", "NotContains",
]


class FilterValue(BaseModel):
    stringValue: str | None = Field(default=None, min_length=1, max_length=1024)
    doubleValue: float | None = None
    booleanValue: bool | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> FilterValue:
        given = [v for v in (self.stringValue, self.doubleValue, self.booleanValue)
                 if v is not None]
        if len(given) != 1:
            raise ValueError("exactly one of stringValue / doubleValue / booleanValue")
        return self


class FilterSpec(BaseModel):
    key: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,256}$")
    operator: Operator
    value: FilterValue

    def payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "operator": self.operator,
            "value": self.value.model_dump(exclude_none=True),
        }


class OnlineConfigCreate(BaseModel):
    agent_id: str
    mode: Literal["scores", "insights"] = "scores"
    # scores mode: 1..10 evaluators; insights mode: 1..3 insight ids (+ frequencies)
    evaluators: list[str] | None = Field(default=None, max_length=ONLINE_EVAL_MAX)
    insights: list[str] | None = Field(default=None, max_length=3)
    clustering_frequencies: list[str] = Field(default_factory=list, max_length=3)
    # None → 10 (scores, console default) / 100 (insights, AWS default)
    sampling_percentage: float | None = Field(default=None, ge=0.01, le=100.0)
    session_timeout_minutes: int = Field(default=15, ge=1, le=1440)
    filters: list[FilterSpec] = Field(default_factory=list, max_length=online.MAX_FILTERS)
    description: str | None = Field(default=None, max_length=200)
    enable_on_create: bool = True

    @model_validator(mode="after")
    def _one_analysis_kind(self) -> OnlineConfigCreate:
        if self.mode == "insights":
            if self.evaluators:
                raise ValueError("insights mode takes no evaluators")
            if not self.insights:
                raise ValueError("insights mode needs 1..3 insights")
        else:
            if self.insights or self.clustering_frequencies:
                raise ValueError("scores mode takes no insights / clustering_frequencies")
            if not self.evaluators:
                raise ValueError("scores mode needs 1..10 evaluators")
        return self


class OnlineConfigPatch(BaseModel):
    description: str | None = Field(default=None, max_length=200)
    evaluators: list[str] | None = Field(default=None, min_length=1, max_length=ONLINE_EVAL_MAX)
    insights: list[str] | None = Field(default=None, min_length=1, max_length=3)
    clustering_frequencies: list[str] | None = Field(default=None, max_length=3)
    sampling_percentage: float | None = Field(default=None, ge=0.01, le=100.0)
    session_timeout_minutes: int | None = Field(default=None, ge=1, le=1440)
    filters: list[FilterSpec] | None = Field(default=None, max_length=online.MAX_FILTERS)


class ReportStart(BaseModel):
    range: Literal["1h", "6h", "24h", "7d"] = "24h"


def _agent_in(db: Session, ws: WorkspaceScope, agent_id: str) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is not None and agent.workspace_id != ws.id:
        agent = None
    if agent is None or agent.status != "active":
        raise AppError("agent.not_active", "agent must be active", status_code=400)
    return agent


@router.get("")
def list_configs(
    db: Session = Depends(get_db), ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    configs = online.list_configs(db, ws)
    return {"configs": configs, "total": len(configs)}


@router.post("", status_code=201)
def create_config(
    req: OnlineConfigCreate,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = _agent_in(db, ws, req.agent_id)
    default_sampling = 100.0 if req.mode == "insights" else 10.0
    return online.create_config(
        db, ws, agent,
        mode=req.mode,
        evaluators=req.evaluators,
        insights=req.insights,
        clustering_frequencies=req.clustering_frequencies,
        sampling_percentage=(
            default_sampling if req.sampling_percentage is None else req.sampling_percentage
        ),
        session_timeout_minutes=req.session_timeout_minutes,
        filters=[f.payload() for f in req.filters],
        description=req.description,
        enable_on_create=req.enable_on_create,
    )


@router.get("/{config_id}")
def get_config(
    config_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return online.get_config(db, ws, config_id)


@router.patch("/{config_id}")
def patch_config(
    config_id: str,
    req: OnlineConfigPatch,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    patch = req.model_dump(exclude_none=True)
    if "filters" in patch:
        patch["filters"] = [f.payload() for f in req.filters or []]
    return online.patch_config(db, ws, config_id, patch)


@router.get("/{config_id}/reports")
def list_reports(
    config_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return online.reports(db, ws, config_id)


@router.post("/{config_id}/reports", status_code=202)
def run_report(
    config_id: str,
    req: ReportStart,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return online.start_report(db, ws, config_id, req.range)


@router.get("/{config_id}/reports/{batch_id}")
def report_detail(
    config_id: str,
    batch_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return online.report_detail(db, ws, config_id, batch_id)


@router.post("/{config_id}/pause")
def pause_config(
    config_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return online.set_execution_status(db, ws, config_id, enabled=False)


@router.post("/{config_id}/resume")
def resume_config(
    config_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return online.set_execution_status(db, ws, config_id, enabled=True)


@router.delete("/{config_id}")
def delete_config(
    config_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return online.delete_config(db, ws, config_id)


@router.get("/{config_id}/results")
def config_results(
    config_id: str,
    range: str = Query(default="24h"),  # noqa: A002 — mirrors the observability API
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    # An insights-mode config never writes score records, so this is simply empty
    # for it; the console reads `mode` from the detail and shows REPORTS instead.
    return online.results(ws, config_id, range)
