"""Observability API — read-only aggregations over aws/spans + AgentCore metrics.

All endpoints are GET, cached 60s per (view, range) inside the service layer;
`force=true` bypasses the cache. Input validation is strict: range whitelist,
trace ids are 32 lowercase hex chars, session ids match the platform id shape.
Violations return the standard {code, message, detail} envelope (422).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import model_prices, observability

router = APIRouter(prefix="/api/observability", tags=["observability"])

RangeParam = Annotated[str, Query(pattern="^(1h|6h|24h|7d)$")]
TraceIdParam = Annotated[str, Path(pattern="^[0-9a-f]{32}$")]
SessionIdParam = Annotated[str, Path(pattern="^[A-Za-z0-9_-]{8,128}$")]
AgentParam = Annotated[str | None, Query(max_length=64, pattern=r"^[A-Za-z0-9._-]+$")]
StatusParam = Annotated[str | None, Query(pattern="^(ok|error)$")]
SessionSearchParam = Annotated[str | None, Query(max_length=128, pattern="^[A-Za-z0-9_-]+$")]


@router.get("/dashboard")
def dashboard(range: RangeParam = "24h", force: bool = False) -> dict[str, Any]:
    return observability.get_dashboard(range, force=force)


@router.get("/prices")
def prices() -> dict[str, Any]:
    settings = get_settings()
    return {
        "prices": settings.model_prices,
        "meta": settings.model_prices_meta,
        "source": settings.model_prices_source_url,
        "refresh_hours": settings.model_prices_refresh_hours,
    }


@router.post("/prices/refresh")
def refresh_prices() -> dict[str, Any]:
    """Pull the latest model prices from the configured litellm source.

    Writes only the local config file — no AWS resource is touched."""
    return model_prices.refresh_model_prices()


@router.get("/traces")
def traces(
    range: RangeParam = "24h",
    agent: AgentParam = None,
    status: StatusParam = None,
    session: SessionSearchParam = None,
    force: bool = False,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    payload = observability.list_traces(range, db, force=force)
    rows = payload["traces"]
    if agent:
        rows = [r for r in rows if r["agent"] == agent or r["service"] == agent]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if session:
        rows = [
            r for r in rows
            if session in (r["session_id"] or "") or session in (r["trace_id"] or "")
        ]
    return {**payload, "traces": rows, "count": len(rows)}


@router.get("/traces/{trace_id}")
def trace_detail(
    trace_id: TraceIdParam,
    range: RangeParam = "24h",
    force: bool = False,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return observability.get_trace(trace_id, range, db, force=force)


@router.get("/sessions")
def sessions(
    range: RangeParam = "24h",
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return observability.list_sessions(range, db, force=force, workspace_id=ws.id)


@router.get("/sessions/{session_id}")
def session_detail(
    session_id: SessionIdParam,
    range: RangeParam = "24h",
    force: bool = False,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return observability.get_session(session_id, range, db, force=force)
