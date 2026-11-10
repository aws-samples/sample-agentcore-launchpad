"""Observability API — read-only aggregations over aws/spans + AgentCore metrics.

Read endpoints are GET, cached 60s per (view, range) inside the service layer;
`force=true` bypasses the cache. The one scoring endpoint
(`POST /sessions/{id}/evaluate`) is synchronous, uncached and persists nothing.
Input validation is strict: range whitelist, trace ids are 32 lowercase hex chars,
session ids match the platform id shape.
Violations return the standard {code, message, detail} envelope (422).
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.assistant.principal import principal_of
from app.assistant.sessions import PrivateSessions
from app.core.db import SessionLocal, get_db
from app.core.errors import NotFoundError
from app.evaluation.models import EvalEvaluator
from app.models.ledger import ChatSession
from app.routers.auth import enabled as auth_enabled
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import observability, runtime_sessions, session_visibility, visibility
from app.services import traces as trace_service
from app.services.agent_access import HiddenAgents
from app.services.observability_access import TelemetryAccess
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api/observability", tags=["observability"])
# The Chat page's trace rail keeps upstream's path (`/api/traces/{session_id}`), outside
# the `/api/observability` prefix; it reads the same spans source as the views above.
traces_router = APIRouter(prefix="/api", tags=["observability"])

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


def _access(request: Request, db: Session, ws: WorkspaceScope, viewer: Viewer) -> TelemetryAccess:
    return TelemetryAccess(
        db, ws.context, viewer, admin=require_identity(request).is_admin,
        denied_sessions=frozenset(_private(request, ws, db).hidden)
        | _hidden_sessions(request, db, ws, viewer) | _hidden(db, ws, viewer).session_ids,
    )


def _hidden_sessions(
    request: Request, db: Session, ws: WorkspaceScope, viewer: Viewer
) -> frozenset[str]:
    """Under isolation: the ledger chat sessions the member could not list
    (`services/session_visibility.py`) — dropped from the span-derived lists, 404 by id.
    Empty without a query for every viewer who does not filter."""
    if not viewer.filters:
        return frozenset()
    identity = require_identity(request)
    human_actor = identity.username if auth_enabled() else "river"
    return session_visibility.hidden_session_ids(db, ws.id, identity, human_actor, viewer)


def _drop_hidden(
    rows: list[dict[str, Any]], hidden: HiddenAgents, sessions: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    if not hidden and not sessions:
        return rows
    return [
        r for r in rows
        if not hidden.hides_service(r.get("service"))
        and not hidden.hides_session(r.get("session_id"))
        and (r.get("session_id") or "") not in sessions
    ]


@router.get("/dashboard")
def dashboard(
    range: RangeParam = "24h",
    force: bool = False,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return observability.get_dashboard(range, ws.context, force=force)


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
    rows = _drop_hidden(rows, _hidden(db, ws, viewer), _hidden_sessions(request, db, ws, viewer))
    access = _access(request, db, ws, viewer)
    rows = [access.trace_row(row) for row in rows if access.row(row)]
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
    access = _access(request, db, ws, viewer)
    access.require_remote_trace(trace_id, observability.RANGE_HOURS[range])
    payload = observability.get_trace(trace_id, range, db, ws.context, force=force)
    if (
        not access.trace(payload)
        or private.mentions_hidden(payload)
        or _hidden(db, ws, viewer).mentions(payload)
        or session_visibility.mentions(payload, _hidden_sessions(request, db, ws, viewer))
    ):
        raise NotFoundError("observability.trace_not_found", "trace not found")
    return payload


@traces_router.get("/traces/{session_id}")
def chat_session_trace(
    session_id: SessionIdParam,
    request: Request,
    lookback_hours: Annotated[int, Query(ge=1, le=336)] = 3,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """The Chat Inspector's trace rail: `{span_count, spans, cloudwatch_url, …}` for one
    chat session (`services.traces.session_trace`).

    Ownership is the chat-history rule: the id must be a chat session of THIS workspace
    (any agent of it) — otherwise 404 `chat.session_not_found`, indistinguishable from a
    missing one; another principal's private assistant session is 404 too. Fail-soft on
    the Logs Insights side (`unavailable_reason`), never an error for the side panel."""
    _private(request, ws, db).require_visible(session_id)
    owned = (
        db.query(ChatSession.id)
        .filter(ChatSession.workspace_id == ws.id, ChatSession.session_id == session_id)
        .first()
    )
    # a hidden agent's session — or, under isolation, a session the member could not
    # list — reads as missing too
    if (
        owned is None
        or _hidden(db, ws, viewer).hides_session(session_id)
        or session_id in _hidden_sessions(request, db, ws, viewer)
    ):
        raise NotFoundError("chat.session_not_found", "chat session not found")
    agent_id = db.query(ChatSession.agent_id).filter(
        ChatSession.workspace_id == ws.id, ChatSession.session_id == session_id).scalar()
    from app.services import principal_mint

    # re-review defect 2: a conversation the principal carrier ran also lives under each
    # principal's derived runtime session — the rail reads their spans too
    derived = principal_mint.runtime_sessions_for(agent_id, session_id) if agent_id else []
    return trace_service.session_trace(
        session_id, ws.context, lookback_hours=lookback_hours, also=derived,
        records_guard=_access(request, db, ws, viewer).records_guard(),
    )


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
    rows = _drop_hidden(rows, _hidden(db, ws, viewer), _hidden_sessions(request, db, ws, viewer))
    access = _access(request, db, ws, viewer)
    rows = [row for row in rows if access.row(row)]
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
    sessions = _hidden_sessions(request, db, ws, viewer)
    if hidden.hides_session(session_id) or session_id in sessions:  # not the viewer's
        raise NotFoundError("observability.session_not_found", "session not found")
    access = _access(request, db, ws, viewer)
    sources = runtime_sessions.source_session_ids(db, ws.id, session_id, viewer)
    access.require_remote_conversation(
        session_id, observability.RANGE_HOURS[range], source_ids=sources)
    payload = observability.get_session(
        session_id, range, db, ws.context, force=force, viewer=viewer,
        records_guard=access.records_guard(session_ids=frozenset(sources)),
        source_session_ids=sources,
    )
    payload = {**payload, "traces": [access.trace_row(row) for row in payload.get("traces") or []]}
    if (
        not access.session(payload)
        or private.mentions_hidden(payload)
        or hidden.mentions(payload)
        or session_visibility.mentions(payload, sessions)
    ):
        raise NotFoundError("observability.session_not_found", "session not found")
    return payload


@router.get("/sessions/{session_id}/transcript")
def session_transcript(
    session_id: SessionIdParam,
    request: Request,
    agent_id: AgentParam = None,
    since: datetime | None = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Conversation only (no span query) — the evaluation result drawer's read;
    `agent_id` attributes a session no ledger row claims, `since` (its first
    span) lets such a session fall back to that agent's content logs."""
    private = _private(request, ws, db)
    private.require_visible(session_id)
    hidden = _hidden(db, ws, viewer)
    sessions = _hidden_sessions(request, db, ws, viewer)
    if hidden.hides_session(session_id) or hidden.hides(agent_id) or session_id in sessions:
        raise NotFoundError("observability.session_not_found", "session not found")
    # N17-obs432: a replayed session is read through its evaluation run — one the
    # caller cannot see reads exactly like a missing session
    run = observability.replay_run_of(db, ws.id, session_id)
    if run is not None and not visibility.sees(viewer, db, run.created_by):
        raise NotFoundError("observability.session_not_found", "session not found")
    access = _access(request, db, ws, viewer)
    access.require_session(session_id)
    payload = observability.get_session_transcript(
        session_id, db, ws.context, agent_id, started_at=since, viewer=viewer,
        records_guard=access.records_guard(),
    )
    if (
        private.mentions_hidden(payload)
        or hidden.mentions(payload)
        or session_visibility.mentions(payload, sessions)
    ):
        raise NotFoundError("observability.session_not_found", "session not found")
    return payload


class PreviewSession(BaseModel):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_\-#:.@]{8,256}$")
    # the session list's `first` (first span): where a session no ledger row
    # claims starts reading the agent's content logs
    first: datetime | None = None


class SessionPreviewsBody(BaseModel):
    sessions: list[PreviewSession] = Field(
        min_length=1, max_length=observability.MAX_PREVIEW_SESSIONS
    )
    agent_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


@router.post("/session-previews")
def session_previews(
    body: SessionPreviewsBody,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """First input / last output of each session (the task wizard's trajectory
    picker, one page of rows per call; a read, POST only for the per-session
    start times). A session the caller may not see reads as `not_found` — the
    row stays, its content does not."""
    private = _private(request, ws, db)
    hidden = _hidden(db, ws, viewer)
    # CN: a member's chat sessions are their own (users-and-ownership.md §7) — the same
    # rule the transcript route applies, so a preview never discloses another member's chat
    sessions = _hidden_sessions(request, db, ws, viewer)
    if hidden.hides(body.agent_id):
        raise NotFoundError("observability.session_not_found", "session not found")
    first: dict[str, datetime | None] = {}
    for item in body.sessions:
        first.setdefault(item.session_id, item.first)
    ids = list(first)
    readable = {
        sid: first[sid] for sid in ids
        if private.visible(sid) and not hidden.hides_session(sid) and sid not in sessions
    }
    access = _access(request, db, ws, viewer)
    for sid in list(readable):
        try:
            access.require_session(sid)
        except NotFoundError:
            del readable[sid]
    transcripts = observability.session_transcripts(
        readable, ws.context, body.agent_id, viewer=viewer, records_guard=access.records_guard(),
    )
    gone = {"available": False, "reason": "not_found"}
    previews = []
    for sid in ids:
        transcript = transcripts.get(sid, gone)
        if (
            private.mentions_hidden(transcript)
            or hidden.mentions(transcript)
            or session_visibility.mentions(transcript, sessions)
        ):
            transcript = gone
        previews.append(observability.transcript_preview(sid, transcript))
    return {"previews": previews}


class SessionEvaluateBody(BaseModel):
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
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _private(request, ws).require_visible(session_id)  # another principal's turn: 404
    from app.evaluation.strands_engine import evaluation_lookback_hours

    if viewer.filters:
        # a ledger-attributed session of a hidden agent answers like a missing one
        if HiddenAgents(db, viewer, ws.id).hides_session(session_id) or (
            session_id in _hidden_sessions(request, db, ws, viewer)
        ):
            raise NotFoundError("observability.session_not_found", "session not found")
        # Reject a hidden evaluator before probing any AWS telemetry.
        custom = [e for e in body.evaluator_ids if not e.startswith("Builtin.")]
        for row in db.query(EvalEvaluator).filter(
            EvalEvaluator.workspace_id == ws.id, EvalEvaluator.id.in_(custom)
        ) if custom else []:
            if not visibility.sees(viewer, db, row.created_by):
                raise NotFoundError(
                    "evaluator.not_found", f"evaluator {row.id} not found"
                )
    access = _access(request, db, ws, viewer)
    sources = runtime_sessions.source_session_ids(db, ws.id, session_id, viewer)
    hours = max(observability.RANGE_HOURS[body.range], evaluation_lookback_hours())
    if len(sources) > 1:
        access.require_remote_conversation(session_id, hours, source_ids=sources)
    else:
        access.require_remote_session(session_id, hours)
    """Score the session on demand through the evaluation engine (SCORE NOW, in-process).

    Synchronous (one judge inference per evaluator), results are returned
    inline and never written to the ledger; 409
    `observability.session_spans_missing` when no spans have landed yet."""
    return observability.evaluate_session(
        session_id, body.range, body.evaluator_ids, ws.context,
        records_guard=access.records_guard(session_ids=frozenset(sources)),
        **({"source_session_ids": sources} if len(sources) > 1 else {}),
    )
