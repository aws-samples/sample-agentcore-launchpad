"""Observability API — read-only aggregations over aws/spans + AgentCore metrics.

Read endpoints are GET, cached 60s per (view, range) inside the service layer;
`force=true` bypasses the cache. The one scoring endpoint
(`POST /sessions/{id}/evaluate`) is synchronous, uncached and persists nothing.
Input validation is strict: range whitelist, trace ids are 32 lowercase hex chars,
session ids match the platform id shape.
Violations return the standard {code, message, detail} envelope (422).
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.assistant.principal import principal_of
from app.assistant.sessions import PrivateSessions
from app.core.config import get_settings
from app.core.db import SessionLocal, get_db
from app.core.errors import NotFoundError
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import model_prices, observability

router = APIRouter(prefix="/api/observability", tags=["observability"])

RangeParam = Annotated[str, Query(pattern="^(1h|6h|24h|7d)$")]
TraceIdParam = Annotated[str, Path(pattern="^[0-9a-f]{32}$")]
# Same alphabet as observability.SESSION_ID_RE — external callers compose
# runtimeSessionIds like `<ulid>#feishu#<chat_id>`, so `#` `:` `.` `@` are
# legitimate; quote / backslash / pipe stay excluded (Logs Insights literals).
SessionIdParam = Annotated[str, Path(pattern=r"^[A-Za-z0-9_\-#:.@]{8,256}$")]
AgentParam = Annotated[str | None, Query(max_length=64, pattern=r"^[A-Za-z0-9._-]+$")]
StatusParam = Annotated[str | None, Query(pattern="^(ok|error)$")]
SessionSearchParam = Annotated[
    str | None, Query(max_length=256, pattern=r"^[A-Za-z0-9_\-#:.@]+$")
]


def _private(request: Request, ws: WorkspaceScope, db: Session | None = None) -> PrivateSessions:
    """Assistant sessions of OTHER principals in this workspace: filtered out of
    every view below, after the per-workspace cache (see app.assistant.sessions)."""
    principal = principal_of(require_identity(request))
    if db is not None:
        return PrivateSessions(db, ws.id, principal)
    own = SessionLocal()
    try:
        return PrivateSessions(own, ws.id, principal)
    finally:
        own.close()


@router.get("/dashboard")
def dashboard(
    range: RangeParam = "24h",
    force: bool = False,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return observability.get_dashboard(range, ws.context, force=force)


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
    request: Request,
    range: RangeParam = "24h",
    agent: AgentParam = None,
    status: StatusParam = None,
    session: SessionSearchParam = None,
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    private = _private(request, ws, db)
    payload = observability.list_traces(range, db, ws.context, force=force)
    rows = [r for r in payload["traces"] if private.visible(r.get("session_id"))]
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
    request: Request,
    range: RangeParam = "24h",
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    private = _private(request, ws, db)
    payload = observability.get_trace(trace_id, range, db, ws.context, force=force)
    if private.mentions_hidden(payload):
        raise NotFoundError("observability.trace_not_found", "trace not found")
    return payload


@router.get("/sessions")
def sessions(
    request: Request,
    range: RangeParam = "24h",
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    private = _private(request, ws, db)
    payload = observability.list_sessions(range, db, ws.context, force=force)
    rows = [r for r in payload["sessions"] if private.visible(r.get("session_id"))]
    return {**payload, "sessions": rows, "count": len(rows)}


@router.get("/sessions/{session_id}")
def session_detail(
    session_id: SessionIdParam,
    request: Request,
    range: RangeParam = "24h",
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    private = _private(request, ws, db)
    private.require_visible(session_id)  # before any query, cached or not
    payload = observability.get_session(session_id, range, db, ws.context, force=force)
    if private.mentions_hidden(payload):
        raise NotFoundError("observability.session_not_found", "session not found")
    return payload


class SessionEvaluateBody(BaseModel):
    """SCORE NOW: 1..5 evaluator ids (built-in `Builtin.*`, third-party
    `ThirdParty.*` or a custom evaluator id) applied to one session's spans."""

    evaluator_ids: list[
        Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")]
    ] = Field(min_length=1, max_length=observability.MAX_ON_DEMAND_EVALUATORS)
    range: Literal["1h", "6h", "24h", "7d"] = "24h"


@router.post("/sessions/{session_id}/evaluate")
def session_evaluate(
    session_id: SessionIdParam,
    body: SessionEvaluateBody,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _private(request, ws).require_visible(session_id)  # another principal's turn: 404
    """Score the session on demand through the data-plane `Evaluate` API.

    Synchronous (one judge inference per evaluator), results are returned
    inline and never written to the ledger; 409
    `observability.session_spans_missing` when no spans have landed yet."""
    return observability.evaluate_session(
        session_id, body.range, body.evaluator_ids, ws.context
    )
