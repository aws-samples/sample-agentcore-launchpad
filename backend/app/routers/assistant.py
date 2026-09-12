"""Architect assistant API (SE-039) — the member-facing conversation with the
protected ``aws-agent-solution-architect`` preset and the separate approval that
turns one reviewed proposal into one regular managed-Harness deploy job.

Authorization comes from ``ROUTE_POLICY`` (member for discussion — parity with Chat;
``perm:agents.deploy`` for approval — parity with ``POST /api/agents``) and is
re-asserted inside the approval. Every route is workspace-scoped; conversations are
additionally owner-bound (see ``app.assistant.service.owned_conversation``).
"""

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.assistant import service
from app.core.db import SessionLocal, get_db
from app.routers.agents import _agent_out
from app.routers.auth import Identity, require_identity, require_permission
from app.routers.auth import enabled as auth_enabled
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services.chat import sse_encode

router = APIRouter(prefix="/api/assistant/architect", tags=["assistant"])


def _caller(request: Request) -> Identity:
    """The resolved caller. With the login gate off every request is the fixed
    ``river`` operator (parity with the Chat playground's actor), so ownership,
    approver stamps and memory actor all agree with the rest of the console."""
    identity = require_identity(request)
    return identity if auth_enabled() else replace(identity, username="river")


def _owner(request: Request) -> str:
    return _caller(request).username


class NewConversation(BaseModel):
    title: str = Field(default="", max_length=200)


class TurnRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=service.MAX_PROMPT_CHARS)


class ProposalEdit(BaseModel):
    # Validated by the proposal contract, not here: the same allowlist applies to
    # a member edit as to a model emission, and errors are reported as a revision.
    content: dict[str, Any]


class RevisionRef(BaseModel):
    revision: int = Field(ge=1)


class ApproveRequest(RevisionRef):
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


@router.get("")
def assistant_status(
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Ledger-only availability: preset state, deploy permission, workspace readiness."""
    return service.availability(db, ws.row, _caller(request))


@router.get("/conversations")
def list_conversations(
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return {"conversations": service.list_conversations(db, ws.id, _owner(request))}


@router.post("/conversations", status_code=201)
def create_conversation(
    req: NewConversation,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Opens a conversation and snapshots the workspace catalog (registry + KB
    reads). Refused (409 ``assistant.unavailable``) while the preset is not active."""
    row = service.create_conversation(db, ws.row, ws.context, _owner(request), req.title)
    return service.conversation_detail(db, row)


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    row = service.owned_conversation(db, ws.id, _owner(request), conversation_id)
    return service.conversation_detail(db, row)


@router.post("/conversations/{conversation_id}/catalog")
def refresh_catalog(
    conversation_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    row = service.owned_conversation(db, ws.id, _owner(request), conversation_id)
    return {"catalog": service.refresh_catalog(db, row, ws.context)}


@router.post("/conversations/{conversation_id}/turns")
def turn(
    conversation_id: str,
    req: TurnRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> StreamingResponse:
    """One discussion turn as SSE. Nothing here can create AWS resources: the only
    cloud call is ``InvokeHarness`` on the preset; a proposal block in the reply
    becomes an inert revision."""
    identity = _caller(request)
    owner = identity.username
    conversation = service.owned_conversation(db, ws.id, owner, conversation_id)  # 404 first
    service._require_available(db, ws.row)  # 409 before streaming
    service.require_turn_capacity(conversation)  # 409 before streaming
    workspace_row, workspace = ws.row, ws.context

    def generate():
        # The stream outlives the request scope → its own session.
        session = SessionLocal()
        try:
            conversation = service.owned_conversation(session, ws.id, owner, conversation_id)
            for event in service.run_turn(
                session, conversation, workspace_row, workspace, identity, req.prompt
            ):
                yield sse_encode(event)
        finally:
            session.close()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.put("/conversations/{conversation_id}/proposal")
def edit_proposal(
    conversation_id: str,
    req: ProposalEdit,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """A member edit creates a NEW revision (draft or invalid) that needs its own
    approval; an approved revision is never mutated."""
    identity = _caller(request)
    row = service.owned_conversation(db, ws.id, identity.username, conversation_id)
    revision = service.edit_proposal(db, row, req.content, identity)
    return {"proposal": service.proposal_out(db, revision)}


@router.post("/conversations/{conversation_id}/proposal/reject")
def reject_proposal(
    conversation_id: str,
    req: RevisionRef,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    identity = _caller(request)
    row = service.owned_conversation(db, ws.id, identity.username, conversation_id)
    revision = service.reject_proposal(db, row, req.revision, identity)
    return {"proposal": service.proposal_out(db, revision)}


@router.post("/conversations/{conversation_id}/proposal/approve")
def approve_proposal(
    conversation_id: str,
    req: ApproveRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> Any:
    """The ONLY executor. Names an exact revision + content hash; on success one
    regular agent/deployment/job exists and the normal pipeline runs. Repeated and
    concurrent calls return the same recorded outcome (200); a fresh claim is 202."""
    from app.deployer.pipeline import start_deploy_async

    require_permission(request, service.PERMISSION_DEPLOY)  # belt and braces
    identity = _caller(request)
    row = service.owned_conversation(db, ws.id, identity.username, conversation_id)
    outcome = service.approve_proposal(
        db, row, ws.row, ws.context, identity,
        revision=req.revision, content_hash=req.content_hash,
    )
    if outcome.started and outcome.job_id:
        start_deploy_async(outcome.job_id)
    return JSONResponse(
        status_code=202 if outcome.started else 200,
        content={
            "proposal": service.proposal_out(db, outcome.proposal),
            "agent": _agent_out(outcome.agent),
            "job_id": outcome.job_id,
            "deployment_id": outcome.deployment_id,
            "started": outcome.started,
        },
    )
