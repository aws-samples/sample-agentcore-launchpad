"""Architect assistant API (SE-039) — the member-facing conversation with the
protected ``aws-agent-solution-architect`` preset and the separate approval that
turns one reviewed proposal into one regular managed-Harness deploy job.

Authorization comes from ``ROUTE_POLICY`` (member for discussion — parity with Chat;
``perm:agents.deploy`` for approval — parity with ``POST /api/agents``) and is
re-asserted inside the approval, where the caller's account, permission and
workspace grant are ALSO re-resolved from the database inside the write
transaction. Every route is workspace-scoped; conversations are additionally bound
to the caller's immutable principal (``app.assistant.principal``).
"""

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.assistant import proposal as proposal_contract
from app.assistant import service
from app.assistant.principal import principal_of
from app.core.db import SessionLocal, get_db
from app.core.errors import AppError
from app.models.ledger import Workspace
from app.routers.agents import _agent_out
from app.routers.auth import Identity, require_identity, require_permission, resolve_identity
from app.routers.auth import enabled as auth_enabled
from app.routers.workspaces import WorkspaceScope, _authorize, require_workspace
from app.services.chat import sse_encode

router = APIRouter(prefix="/api/assistant/architect", tags=["assistant"])


def _caller(request: Request) -> Identity:
    """The resolved caller. With the login gate off every request is the fixed
    ``river`` operator (parity with the Chat playground's actor), so display names,
    approver stamps and memory actor all agree with the rest of the console."""
    identity = require_identity(request)
    return identity if auth_enabled() else replace(identity, username="river")


def _recheck_factory(request: Request, ws: WorkspaceScope) -> service.Recheck:
    """Re-resolve the principal and the workspace from the database at the claim
    boundary: an account disabled/expired, a permission revoked or a grant removed
    while the approval was reading the catalog is honoured; the config admin stays
    the stable separate principal."""

    def recheck(db: Session) -> tuple[Identity, Workspace]:
        if auth_enabled():
            identity = resolve_identity(request, db=db)
            if identity is None:
                raise AppError("auth.required", "Authentication required", status_code=401)
        else:
            identity = _caller(request)
        if not identity.can(service.PERMISSION_DEPLOY):
            raise AppError("auth.permission_required",
                           f"This action requires the '{service.PERMISSION_DEPLOY}' permission",
                           {"permission": service.PERMISSION_DEPLOY}, status_code=403)
        row = db.get(Workspace, ws.id)
        if row is None:
            raise AppError("workspace.not_found", "workspace not found", status_code=404)
        _authorize(db, identity, row)  # 403 workspace.forbidden when the grant is gone
        return (identity if auth_enabled() else replace(identity, username="river")), row

    return recheck


class NewConversation(BaseModel):
    title: str = Field(default="", max_length=200)


class TurnRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=service.MAX_PROMPT_CHARS)


class ProposalEdit(BaseModel):
    # Validated by the proposal contract, not here: the same allowlist and byte cap
    # apply to a member edit as to a model emission.
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
    return {"conversations": service.list_conversations(db, ws.id, principal_of(_caller(request)))}


@router.post("/conversations", status_code=201)
def create_conversation(
    req: NewConversation,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Opens a conversation and snapshots the workspace catalog (registry, gateway,
    S3 skill content and KB reads). Refused (409 ``assistant.unavailable``) while
    the preset is not active."""
    row = service.create_conversation(db, ws.row, ws.context, _caller(request), req.title)
    return service.conversation_detail(db, row)


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    row = service.owned_conversation(db, ws.id, principal_of(_caller(request)), conversation_id)
    return service.conversation_detail(db, row)


@router.post("/conversations/{conversation_id}/catalog")
def refresh_catalog(
    conversation_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    row = service.owned_conversation(db, ws.id, principal_of(_caller(request)), conversation_id)
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
    becomes an inert revision. Refusals (404/409/413) happen before the stream opens."""
    identity = _caller(request)
    principal = principal_of(identity)
    conversation = service.owned_conversation(db, ws.id, principal, conversation_id)
    service._require_available(db, ws.row)
    service.require_turn_capacity(conversation)
    service.check_prompt(conversation, req.prompt)
    workspace_row, workspace = ws.row, ws.context

    def generate():
        # The stream outlives the request scope → its own session.
        session = SessionLocal()
        try:
            conversation = service.owned_conversation(session, ws.id, principal, conversation_id)
            for event in service.run_turn(
                session, conversation, workspace_row, workspace, identity, req.prompt
            ):
                yield sse_encode(event)
        except AppError as exc:  # a claim refusal after the response started
            yield sse_encode({"event": "error", "data": {"code": exc.code,
                                                        "message": exc.message}})
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
    approval; an approved revision is never mutated. 413 above the byte cap."""
    identity = _caller(request)
    if proposal_contract.serialized_bytes(req.content) > proposal_contract.PROPOSAL_MAX_BYTES:
        raise AppError(
            "assistant.proposal_too_large",
            f"the proposal exceeds {proposal_contract.PROPOSAL_MAX_BYTES} bytes",
            {"max_bytes": proposal_contract.PROPOSAL_MAX_BYTES}, status_code=413,
        )
    row = service.owned_conversation(db, ws.id, principal_of(identity), conversation_id)
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
    row = service.owned_conversation(db, ws.id, principal_of(identity), conversation_id)
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
    concurrent calls return the same recorded outcome (200) and re-wake a job whose
    starter died; a fresh claim is 202."""
    from app.deployer.pipeline import live_deploy_worker, start_deploy_async

    require_permission(request, service.PERMISSION_DEPLOY)  # belt and braces
    identity = _caller(request)
    row = service.owned_conversation(db, ws.id, principal_of(identity), conversation_id)
    outcome = service.approve_proposal(
        db, row, ws.row, ws.context, identity,
        revision=req.revision, content_hash=req.content_hash,
        recheck=_recheck_factory(request, ws),
    )
    if outcome.job_id and (
        outcome.started
        or (outcome.job_status == "queued" and live_deploy_worker(outcome.job_id) is None)
    ):
        try:
            start_deploy_async(outcome.job_id)
        except Exception:  # the job is durable; a later retry or startup resume re-wakes it
            service.logger.exception("assistant: deploy starter failed for job %s",
                                     outcome.job_id)
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
