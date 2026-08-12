"""Chat playground endpoints — SSE streaming over the shared invoke chain."""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import exists
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, get_db
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent, ChatMessage, ChatSession
from app.routers.auth import enabled as auth_enabled
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import memory as memory_service
from app.services import policy_identity
from app.services.chat import chat_stream, sse_encode
from app.services.runtime_discovery import require_invoke_capability
from app.templates import gateway_support

router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=100000)
    session_id: str | None = None


def _session_actor(
    db: Session,
    workspace_id: str,
    agent_id: str,
    session_id: str | None,
    requested_actor: str,
) -> str:
    if not session_id:
        return requested_actor
    existing = (
        db.query(ChatSession.actor_id)
        .filter(
            ChatSession.workspace_id == workspace_id,
            ChatSession.agent_id == agent_id,
            ChatSession.session_id == session_id,
        )
        .first()
    )
    return existing[0] if existing and existing[0] else requested_actor


def _agent_in(db: Session, ws: WorkspaceScope, agent_id: str) -> Agent:
    """The agent, or 404 — including when it belongs to another workspace, which
    the caller must not be able to tell apart from a missing one."""
    agent = db.get(Agent, agent_id)
    if agent is None or agent.workspace_id != ws.id:
        raise NotFoundError("agent.not_found", "agent not found")
    return agent


def _get_active_agent(db: Session, ws: WorkspaceScope, agent_id: str) -> Agent:
    agent = _agent_in(db, ws, agent_id)
    require_invoke_capability(agent)
    return agent


def _save_message(
    workspace_id: str,
    agent_id: str,
    session_id: str,
    role: str,
    text: str,
    name: str | None = None,
) -> None:
    db = SessionLocal()
    try:
        db.add(ChatMessage(workspace_id=workspace_id, agent_id=agent_id,
                           session_id=session_id,
                           role=role, text=text[:100000], name=name))
        db.commit()
    finally:
        db.close()


def _track_session(
    workspace_id: str, agent_id: str, session_id: str, actor_id: str
) -> None:
    db = SessionLocal()
    try:
        row = (
            db.query(ChatSession)
            .filter(
                ChatSession.workspace_id == workspace_id,
                ChatSession.agent_id == agent_id,
                ChatSession.session_id == session_id,
            )
            .first()
        )
        if row is None:
            row = ChatSession(workspace_id=workspace_id, agent_id=agent_id,
                              session_id=session_id, actor_id=actor_id)
            db.add(row)
        row.turns = (row.turns or 0) + 1
        row.last_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()


@router.post("/chat/{agent_id}")
def chat(
    agent_id: str,
    req: ChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> StreamingResponse:
    agent = _get_active_agent(db, ws, agent_id)
    identity = require_identity(request)
    human_actor = identity.username if auth_enabled() else "river"

    # Memory partitions per agent: the runtime writes short-term events and the
    # extractor lands long-term records under this compound actor. The ledger
    # still records the bare authenticated username. A continuing session keeps
    # its original Memory owner, while policy authorization follows the current
    # signed-in caller.
    actor_id = _session_actor(db, ws.id, agent.id, req.session_id, human_actor)
    mem_actor = memory_service.scoped_actor(agent.id, actor_id)
    needs_gateway_identity = (
        gateway_support.runtime_user_id(agent.spec, identity.username) is not None
    )
    gateway_access_token = (
        policy_identity.gateway_user_token(
            identity.username,
            identity.role,
            identity.email,
        )
        if needs_gateway_identity
        else None
    )

    # The stream outlives the request scope, so it carries the plain id rather
    # than reaching back for the resolved scope.
    workspace_id = ws.id

    def generate():
        # Thread items are persisted in event order (int-pk = replay order) so
        # the playground can restore a session's history exactly as rendered.
        session_id = req.session_id
        answer_parts: list[str] = []
        stream_kwargs: dict[str, Any] = {}
        if needs_gateway_identity:
            stream_kwargs["runtime_user_id"] = identity.username
        if gateway_access_token:
            stream_kwargs["gateway_access_token"] = gateway_access_token
        for event in chat_stream(
            agent,
            req.prompt,
            session_id=session_id,
            actor_id=mem_actor,
            **stream_kwargs,
        ):
            kind, data = event["event"], event["data"]
            if kind == "meta":
                session_id = data["session_id"]
                _track_session(workspace_id, agent.id, session_id, actor_id)
                _save_message(workspace_id, agent.id, session_id, "user", req.prompt)
            elif kind == "tool" and session_id:
                if answer_parts:  # a tool call splits the answer bubble live — mirror it
                    _save_message(workspace_id, agent.id, session_id, "agent",
                                  "".join(answer_parts))
                    answer_parts.clear()
                _save_message(workspace_id, agent.id, session_id, "tool", "",
                              name=data.get("name"))
            elif kind == "delta":
                answer_parts.append(data.get("text", ""))
            elif kind == "error" and session_id:
                if answer_parts:  # keep the partial answer the user saw
                    _save_message(workspace_id, agent.id, session_id, "agent",
                                  "".join(answer_parts))
                    answer_parts.clear()
                _save_message(workspace_id, agent.id, session_id, "error",
                              data.get("message", ""))
            elif kind == "done" and session_id and answer_parts:
                _save_message(workspace_id, agent.id, session_id, "agent",
                              "".join(answer_parts))
            yield sse_encode(event)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/chat/{agent_id}/sessions")
def list_sessions(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _agent_in(db, ws, agent_id)
    # Only sessions with a replayable transcript: rows that predate the
    # ChatMessage ledger have nothing to open (clicking them showed an empty
    # thread with an id-only preview), so they are filtered out here.
    # The correlated exists matches on session_id alone, so it carries the
    # workspace predicate itself — a bare id match can cross environments.
    rows = (
        db.query(ChatSession)
        .filter(
            ChatSession.workspace_id == ws.id,
            ChatSession.agent_id == agent_id,
            exists().where(
                ChatMessage.session_id == ChatSession.session_id,
                ChatMessage.workspace_id == ws.id,
            ),
        )
        .order_by(ChatSession.last_at.desc())
        .limit(50)
        .all()
    )

    def preview(session_id: str) -> str:
        first = (
            db.query(ChatMessage.text)
            .filter(
                ChatMessage.workspace_id == ws.id,
                ChatMessage.session_id == session_id,
                ChatMessage.role == "user",
            )
            .order_by(ChatMessage.id.asc())
            .first()
        )
        return (first[0] if first else "")[:120]

    return {
        "sessions": [
            {
                "session_id": r.session_id,
                "actor_id": r.actor_id,
                "turns": r.turns,
                "last_at": r.last_at.isoformat() if r.last_at else None,
                "preview": preview(r.session_id),
            }
            for r in rows
        ]
    }


@router.get("/chat/{agent_id}/history")
def session_history(
    agent_id: str,
    session_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Replay a session's thread exactly as it was rendered (int pk = order)."""
    _agent_in(db, ws, agent_id)
    rows = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.workspace_id == ws.id,
            ChatMessage.agent_id == agent_id,
            ChatMessage.session_id == session_id,
        )
        .order_by(ChatMessage.id.asc())
        .limit(500)
        .all()
    )
    return {
        "messages": [
            {
                "role": r.role,
                "text": r.text,
                "name": r.name,
                "at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/chat/{agent_id}/memory")
def session_memory(
    agent_id: str,
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _agent_in(db, ws, agent_id)
    try:
        # Read back the same agent-scoped partition the chat write path uses.
        session_actor = _session_actor(
            db,
            ws.id,
            agent_id,
            session_id,
            require_identity(request).username if auth_enabled() else "river",
        )
        mem_actor = memory_service.scoped_actor(agent_id, session_actor)
        # Echo the compound actor so the rail can deep-link into the Memory
        # console at the exact partition it just summarised — the console keys
        # on this id, and re-deriving it in the frontend would fork the scoping
        # rule (a session may have recorded a different human actor entirely).
        return {
            **memory_service.session_memory_summary(mem_actor, session_id),
            "actor_id": mem_actor,
        }
    except Exception as exc:
        raise AppError(
            "memory.unavailable", f"memory lookup failed: {exc}", status_code=502
        ) from exc
