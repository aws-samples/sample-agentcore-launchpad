"""Memory console API — read-only views over the shared AgentCore Memory.

Thin router over ``app.services.memory_console`` plus the ledger joins that turn
raw AWS identifiers into console-legible rows (compound actor id → agent name,
session id → chat-session metadata). The service layer stays AWS-only so tests
can inject stub clients.

**Read-only by construction:** there is no handler here that mutates memory. See
``app/services/memory_console.py`` for the matching guarantee on the service side.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, mapped_aws_error
from app.models.ledger import Agent, ChatMessage, ChatSession
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import memory_console

router = APIRouter(prefix="/api/memory", tags=["memory"])


class SearchRequest(BaseModel):
    """Semantic retrieval over one namespace (RetrieveMemoryRecords)."""

    query: str = Field(min_length=1, max_length=2000)
    actor_id: str | None = None
    strategy_id: str | None = None
    namespace: str | None = None
    top_k: int = Field(default=5, ge=1, le=100)


def _guard(fn, *args, **kwargs):
    """Map AWS/botocore failures onto the ``memory.unavailable`` envelope.

    Domain errors raised by the service (``memory.not_configured``) already carry
    a code, so they pass through untouched — mirrors ``routers/chat.py``. An AWS
    ``ClientError`` the console can translate (``ResourceNotFoundException`` for
    an unknown actor, ``AccessDeniedException``, …) is left to the global handler
    in ``app.core.errors`` so the toast reads "not found", not raw boto text.
    """
    try:
        return fn(*args, **kwargs)
    except AppError:
        raise
    except Exception as exc:  # botocore ClientError, endpoint errors, ...
        if mapped_aws_error(exc):
            raise
        raise AppError(
            "memory.unavailable", f"memory lookup failed: {exc}", status_code=502
        ) from exc


def _agent_names(db: Session, ws: WorkspaceScope, agent_ids: list[str]) -> dict[str, str]:
    """One batched lookup per page — never N+1 across the actor list.

    Scoped to the request's workspace: an actor id names an agent of *this*
    environment, and an id that resolves elsewhere stays nameless rather than
    leaking a foreign agent's name.
    """
    ids = [i for i in dict.fromkeys(agent_ids) if i]
    if not ids:
        return {}
    rows = (
        db.query(Agent.id, Agent.name)
        .filter(Agent.workspace_id == ws.id, Agent.id.in_(ids))
        .all()
    )
    return {row[0]: row[1] for row in rows}


def _resolve_namespace(
    ws: WorkspaceScope,
    namespace: str | None,
    actor_id: str | None,
    strategy_id: str | None,
) -> str:
    """Explicit namespace wins; otherwise derive it from (actor, strategy)."""
    if namespace:
        return namespace
    if not actor_id:
        raise AppError(
            "memory.namespace_required",
            "Provide either `namespace`, or `actor_id` (+ optional `strategy_id`).",
            status_code=400,
        )
    candidates = _guard(memory_console.resolve_namespaces, ws.context, actor_id)
    usable = [c for c in candidates if c["resolvable"]]
    if strategy_id:
        usable = [c for c in usable if c["strategy_id"] == strategy_id]
    if not usable:
        raise AppError(
            "memory.namespace_required",
            "No resolvable namespace for this actor/strategy combination.",
            status_code=400,
        )
    return usable[0]["namespace"]


@router.get("/overview")
def overview(ws: WorkspaceScope = Depends(require_workspace)) -> dict[str, Any]:
    return _guard(memory_console.memory_overview, ws.context)


@router.get("/actors")
def actors(
    next_token: str | None = None,
    max_results: int | None = Query(default=None, ge=1, le=100),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    page = _guard(memory_console.list_actors, ws.context, next_token, max_results)
    names = _agent_names(
        db, ws, [a["agent_id"] for a in page["items"] if a["agent_id"]]
    )
    for item in page["items"]:
        # A scoped actor whose agent row is gone keeps scoped=True with a null
        # name — the memory partition outlives the agent it belonged to.
        item["agent_name"] = names.get(item["agent_id"] or "")
    return page


@router.get("/sessions")
def sessions(
    actor_id: str = Query(min_length=1),
    next_token: str | None = None,
    max_results: int | None = Query(default=None, ge=1, le=100),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    page = _guard(
        memory_console.list_sessions, ws.context, actor_id, next_token, max_results
    )
    session_ids = [s["session_id"] for s in page["items"] if s["session_id"]]
    ledger: dict[str, dict[str, Any]] = {}
    if session_ids:
        rows = (
            db.query(ChatSession, Agent.name)
            .outerjoin(Agent, Agent.id == ChatSession.agent_id)
            .filter(
                ChatSession.workspace_id == ws.id,
                ChatSession.session_id.in_(session_ids),
            )
            .all()
        )
        counts = {
            row[0]: row[1]
            for row in db.query(ChatMessage.session_id, func.count(ChatMessage.id))
            .filter(
                ChatMessage.workspace_id == ws.id,
                ChatMessage.session_id.in_(session_ids),
            )
            .group_by(ChatMessage.session_id)
            .all()
        }
        for chat_session, agent_name in rows:
            ledger[chat_session.session_id] = {
                "agent_id": chat_session.agent_id,
                "agent_name": agent_name,
                "human_actor": chat_session.actor_id,
                "turns": chat_session.turns,
                "message_count": counts.get(chat_session.session_id, 0),
            }
    for item in page["items"]:
        # None for sessions the console never wrote (eval runs, /v1 API callers).
        item["ledger"] = ledger.get(item["session_id"])
    return page


@router.get("/events")
def events(
    actor_id: str = Query(min_length=1),
    session_id: str = Query(min_length=1),
    include_payloads: bool = True,
    next_token: str | None = None,
    max_results: int | None = Query(default=None, ge=1, le=100),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return _guard(
        memory_console.list_events,
        ws.context,
        actor_id,
        session_id,
        include_payloads,
        next_token,
        max_results,
    )


@router.get("/namespaces")
def namespaces(
    actor_id: str = Query(min_length=1),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return {"items": _guard(memory_console.resolve_namespaces, ws.context, actor_id)}


@router.get("/records")
def records(
    actor_id: str | None = None,
    strategy_id: str | None = None,
    namespace: str | None = None,
    next_token: str | None = None,
    max_results: int | None = Query(default=None, ge=1, le=100),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    resolved = _resolve_namespace(ws, namespace, actor_id, strategy_id)
    return _guard(
        memory_console.list_records,
        ws.context,
        resolved,
        strategy_id,
        next_token,
        max_results,
    )


@router.post("/records/search")
def search_records(
    req: SearchRequest,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    resolved = _resolve_namespace(ws, req.namespace, req.actor_id, req.strategy_id)
    return _guard(
        memory_console.search_records,
        ws.context,
        resolved,
        req.query,
        req.top_k,
        req.strategy_id,
    )


@router.get("/extraction-jobs")
def extraction_jobs(
    actor_id: str | None = None,
    session_id: str | None = None,
    strategy_id: str | None = None,
    status: str | None = None,
    next_token: str | None = None,
    max_results: int | None = Query(default=None, ge=1, le=100),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return _guard(
        memory_console.list_extraction_jobs,
        ws.context,
        actor_id,
        session_id,
        strategy_id,
        status,
        next_token,
        max_results,
    )
