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
from app.core.billing_policy import SOURCE_OBSERVABILITY
from app.core.config import get_settings
from app.core.db import SessionLocal, get_db
from app.core.errors import NotFoundError
from app.evaluation.online_evaluators import require_visible_evaluators
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import budgets, model_prices, observability
from app.services.agent_access import HiddenAgents
from app.services.usage_meter import UsageMeter
from app.services.visibility import Viewer, require_viewer

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


def _hidden(db: Session, ws: WorkspaceScope, viewer: Viewer) -> HiddenAgents:
    """Agents workspace isolation hides from the caller: their rows are dropped and
    their details answer 404 — after the per-workspace cache, like `_private`."""
    return HiddenAgents(db, viewer, ws.id)


def _drop_hidden(rows: list[dict[str, Any]], hidden: HiddenAgents) -> list[dict[str, Any]]:
    if not hidden:
        return rows
    return [
        r for r in rows
        if not hidden.hides_service(r.get("service"))
        and not hidden.hides_session(r.get("session_id"))
    ]


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
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    private = _private(request, ws, db)
    payload = observability.list_traces(range, db, ws.context, force=force)
    rows = [r for r in payload["traces"] if private.visible(r.get("session_id"))]
    rows = _drop_hidden(rows, _hidden(db, ws, viewer))
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
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    private = _private(request, ws, db)
    payload = observability.get_trace(trace_id, range, db, ws.context, force=force)
    if private.mentions_hidden(payload) or _hidden(db, ws, viewer).mentions(payload):
        raise NotFoundError("observability.trace_not_found", "trace not found")
    return payload


@router.get("/sessions")
def sessions(
    request: Request,
    range: RangeParam = "24h",
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    private = _private(request, ws, db)
    payload = observability.list_sessions(range, db, ws.context, force=force)
    rows = [r for r in payload["sessions"] if private.visible(r.get("session_id"))]
    rows = _drop_hidden(rows, _hidden(db, ws, viewer))
    return {**payload, "sessions": rows, "count": len(rows)}


@router.get("/sessions/{session_id}")
def session_detail(
    session_id: SessionIdParam,
    request: Request,
    range: RangeParam = "24h",
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    private = _private(request, ws, db)
    private.require_visible(session_id)  # before any query, cached or not
    hidden = _hidden(db, ws, viewer)
    if hidden.hides_session(session_id):  # a hidden agent's ledger session
        raise NotFoundError("observability.session_not_found", "session not found")
    payload = observability.get_session(session_id, range, db, ws.context, force=force)
    if private.mentions_hidden(payload) or hidden.mentions(payload):
        raise NotFoundError("observability.session_not_found", "session not found")
    return payload


@router.get("/sessions/{session_id}/transcript")
def session_transcript(
    session_id: SessionIdParam,
    request: Request,
    agent_id: AgentParam = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Conversation only (no span query) — the evaluation result drawer's read;
    `agent_id` attributes a session no ledger row claims."""
    private = _private(request, ws, db)
    private.require_visible(session_id)
    hidden = _hidden(db, ws, viewer)
    if hidden.hides_session(session_id) or hidden.hides(agent_id):
        raise NotFoundError("observability.session_not_found", "session not found")
    payload = observability.get_session_transcript(session_id, db, ws.context, agent_id)
    if private.mentions_hidden(payload) or hidden.mentions(payload):
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
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Score the session on demand through the data-plane `Evaluate` API.

    Synchronous (one judge inference per evaluator), results are returned
    inline and never written to the ledger; 409
    `observability.session_spans_missing` when no spans have landed yet. A custom
    evaluator hidden by workspace isolation answers like a missing one."""
    _private(request, ws).require_visible(session_id)  # another principal's turn: 404
    if viewer.filters:
        db = SessionLocal()
        try:
            # a ledger-attributed session of a hidden agent answers like a missing one
            if HiddenAgents(db, viewer, ws.id).hides_session(session_id):
                raise NotFoundError("observability.session_not_found", "session not found")
            require_visible_evaluators(db, ws.id, viewer, body.evaluator_ids)
        finally:
            db.close()
    # every judge inference is charged to the caller (Evaluate reports exact tokens)
    meter = UsageMeter.for_payer(
        budgets.payer_of_request(request), SOURCE_OBSERVABILITY, origin_workspace_id=ws.id,
    )
    return observability.evaluate_session(
        session_id, body.range, body.evaluator_ids, ws.context, meter=meter,
    )
