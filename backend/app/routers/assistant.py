"""Architect assistant API (SE-039) — the member-facing conversation with the
protected ``aws-agent-solution-architect`` preset and the separate approval that
turns one reviewed proposal into one regular self-built-Harness deploy job.

Authorization comes from ``ROUTE_POLICY`` (member for discussion — parity with Chat;
``perm:agents.deploy`` for approval — parity with ``POST /api/agents``) and is
re-asserted inside the approval, where the caller's account, permission and
workspace grant are ALSO re-resolved from the database inside the write
transaction. Every route is workspace-scoped; conversations are additionally bound
to the caller's immutable principal (``app.assistant.principal``), except that an
admin may share one with the whole workspace: every member may then work on it
(each action under its usual permission), and only its owner may delete it.
"""

import json as _json
import logging
from dataclasses import replace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.assistant import evaluation_repair, preparation, service
from app.assistant import proposal as proposal_contract
from app.assistant.principal import principal_of
from app.core.db import SQLITE_MAX_INT, get_db
from app.core.display_time import DisplayTimeJSONResponse
from app.core.errors import AppError
from app.models.ledger import Job, Workspace
from app.routers.agents import _agent_out
from app.routers.auth import Identity, require_identity, require_permission, resolve_identity
from app.routers.auth import enabled as auth_enabled
from app.routers.workspaces import WorkspaceScope, _authorize, require_workspace
from app.services import live_turns

router = APIRouter(prefix="/api/assistant/architect", tags=["assistant"])
logger = logging.getLogger("launchpad.assistant")

# Ingress cap for every assistant write (bytes actually received, whatever
# Content-Length says): the largest legitimate body is a 100k-char prompt.
ASSISTANT_BODY_MAX_BYTES = 512_000
_CAPPED_PREFIX = "/api/assistant/"


class AssistantBodyCap:
    """Pure ASGI middleware: refuse an assistant request body above the cap while it
    is still being received — before FastAPI/Pydantic ever parse it, and without
    trusting a missing or lying Content-Length."""

    def __init__(self, app, max_bytes: int = ASSISTANT_BODY_MAX_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(_CAPPED_PREFIX) \
                or scope.get("method") not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        received = 0
        tripped = False
        started = False

        async def capped_receive():
            nonlocal received, tripped
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    tripped = True
                    # the app sees a disconnect and stops reading; its own reaction
                    # (a 400) is swallowed below in favour of the 413
                    return {"type": "http.disconnect"}
            return message

        async def capped_send(message):
            nonlocal started
            if tripped:
                return
            started = True
            await send(message)

        try:
            await self.app(scope, capped_receive, capped_send)
        except Exception:
            if not tripped:
                raise
        if tripped and not started:
            body = _json.dumps({
                "code": "assistant.request_too_large",
                "message": f"request body exceeds {self.max_bytes} bytes",
                "detail": {"max_bytes": self.max_bytes},
            }).encode()
            await send({"type": "http.response.start", "status": 413,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})


# N-ARCH-obs15: the streaming response owns its SUBSCRIPTION to a turn — never the turn;
# the class and the SSE framing are shared with the Chat playground (services/live_turns.py).
TurnStreamResponse = live_turns.TurnStreamResponse
_sse_body = live_turns.sse_body
_SSE_HEADERS = live_turns.SSE_HEADERS


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


class EvaluationPlanRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_revision: int = Field(ge=1, strict=True)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class TurnRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=service.MAX_PROMPT_CHARS)
    evaluation_plan_repair: EvaluationPlanRepair | None = None
    # retry the latest FAILED turn (usually with an edited message): that exchange is
    # kept in the thread but no longer replayed to the model
    retry_of_turn: int | None = Field(default=None, ge=1)


class ProposalEdit(BaseModel):
    # Validated by the proposal contract, not here: the same allowlist and byte cap
    # apply to a member edit as to a model emission. Unknown outer members are
    # refused rather than silently ignored (they would otherwise ride under the cap).
    model_config = ConfigDict(extra="forbid")
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
    """The caller's own conversations plus the shared ones; an administrator lists
    every member's (another member's unshared one carries ``read_only``)."""
    return {"conversations": service.list_conversations(db, ws.id, _caller(request))}


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
    """The owner's, a shared one for any member, or — read-only — any conversation of
    the workspace for an administrator (``read_only: true`` while unshared)."""
    identity = _caller(request)
    row = service.viewable_conversation(db, ws.id, identity, conversation_id)
    return service.conversation_detail(db, row, viewer=principal_of(identity))


class ShareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shared: bool


@router.put("/conversations/{conversation_id}/sharing")
def set_sharing(
    conversation_id: str,
    req: ShareRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """The owner or an administrator opens (or closes) one conversation to every
    member of the workspace (an admin may share any conversation it reads — that is
    how a read-only one becomes a collaboration). Deleting it stays with the owner."""
    identity = _caller(request)
    row = service.set_shared(db, ws.id, identity, conversation_id, req.shared)
    return service.conversation_summary(db, row, viewer=principal_of(identity))


@router.post("/conversations/{conversation_id}/catalog")
def refresh_catalog(
    conversation_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    principal = principal_of(_caller(request))
    row = service.accessible_conversation(db, ws.id, principal, conversation_id)
    catalog = service.refresh_catalog(db, row, ws.context)
    return {"catalog": catalog,
            "conversation": service.conversation_detail(db, row, viewer=principal)}


@router.post("/conversations/{conversation_id}/turns")
def turn(
    conversation_id: str,
    req: TurnRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> StreamingResponse:
    """One discussion turn as SSE. Nothing here can create AWS resources: the only
    cloud call is ``InvokeAgentRuntime`` on the preset's self-built Harness runtime; a
    proposal block in the reply becomes an inert revision. Refusals (404/409/413)
    happen before the stream opens. N-ARCH-obs15: the turn runs on a server-side driver
    thread; this response is its first subscriber (each frame carries ``id: <seq>``) —
    closing it detaches the subscriber only, and ``GET …/turns/{turn}/stream`` reattaches."""
    identity = _caller(request)
    principal = principal_of(identity)
    conversation = service.accessible_conversation(db, ws.id, principal, conversation_id)
    service._require_available(db, ws.row)
    service.require_turn_capacity(conversation)
    service.check_prompt(conversation, req.prompt)
    if req.retry_of_turn is not None:
        service.check_retry(db, conversation, req.retry_of_turn)
    prompt = req.prompt
    if req.evaluation_plan_repair is not None:
        prompt = evaluation_repair.repair_prompt(
            db, conversation, prompt,
            plan_revision=req.evaluation_plan_repair.plan_revision,
            plan_hash=req.evaluation_plan_repair.plan_hash,
        )
    live = service.start_turn(conversation_id, ws.id, ws.row, ws.context, identity, prompt,
                              retry_of=req.retry_of_turn)
    return TurnStreamResponse(_sse_body(live.subscribe(0)), media_type="text/event-stream",
                              headers=_SSE_HEADERS)


@router.get("/conversations/{conversation_id}/turns/{turn}/stream")
def turn_stream(
    conversation_id: str,
    # N16-obs86 (same class as the chat reattach): bounded integer parameters
    turn: Annotated[int, PathParam(ge=0, le=SQLITE_MAX_INT)],
    request: Request,
    from_seq: Annotated[str | None, Query(
        alias="from",
        description="Replay the persisted events with seq > from (default 0); an integer in "
                    "0..2^63-1, else 422 assistant.invalid_seq",
    )] = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> StreamingResponse:
    """Reattach to a turn (N-ARCH-obs15): replays the persisted events with
    ``seq > from`` (``?from=<seq>``, default 0), then tails the turn while it runs,
    then its terminal event (``done`` / ``error``). ``404 assistant.turn_not_found`` for
    a turn the conversation never reached; ``409 assistant.turn_not_yours`` when another
    member sent it. A read-only viewer (an administrator on another member's unshared
    conversation) tails any turn: the stream writes nothing, and that viewer has no
    turn of its own to reattach to. ``422 assistant.invalid_seq`` for a ``from`` outside
    ``0..2^63-1`` (N16-obs86; ``validation.invalid_request`` for such a ``turn``)."""
    identity = _caller(request)
    conversation = service.viewable_conversation(db, ws.id, identity, conversation_id)
    service.require_turn(db, conversation, turn, identity,
                         read_only=service.is_read_only_viewer(conversation, identity))
    after = live_turns.parse_from(from_seq, "assistant.invalid_seq")
    return TurnStreamResponse(_sse_body(service.stream_turn(conversation_id, turn, after)),
                              media_type="text/event-stream", headers=_SSE_HEADERS)


@router.put("/conversations/{conversation_id}/preparation")
def update_preparation(
    conversation_id: str, req: preparation.SelectionRequest, request: Request,
    db: Session = Depends(get_db), ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    identity = _caller(request)
    row = service.accessible_conversation(db, ws.id, principal_of(identity), conversation_id)
    row = preparation.save_selection(db, row, ws.context, identity, req)
    return service.conversation_detail(db, row, viewer=principal_of(identity))


@router.post("/conversations/{conversation_id}/preparation/skills")
def import_preparation_skills(
    conversation_id: str, req: preparation.ImportRequest, request: Request,
    db: Session = Depends(get_db), ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    identity = _caller(request)
    row = service.accessible_conversation(db, ws.id, principal_of(identity), conversation_id)
    row, results = preparation.import_skills(
        db, row, ws.context, identity, req, recheck=_recheck_factory(request, ws),
    )
    return {"conversation": service.conversation_detail(db, row, viewer=principal_of(identity)),
            "results": results}


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
    row = service.accessible_conversation(db, ws.id, principal_of(identity), conversation_id)
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
    row = service.accessible_conversation(db, ws.id, principal_of(identity), conversation_id)
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
    row = service.accessible_conversation(db, ws.id, principal_of(identity), conversation_id)
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
    return DisplayTimeJSONResponse(
        status_code=202 if outcome.started else 200,
        content={
            "proposal": service.proposal_out(db, outcome.proposal),
            "agent": _agent_out(outcome.agent),
            "job_id": outcome.job_id,
            "deployment_id": outcome.deployment_id,
            "started": outcome.started,
        },
    )


# ---------------------------------------------------------------------------
# SE-047 — the reviewed evaluation-assets plan and its materialization
# ---------------------------------------------------------------------------

from app.assistant import evaluation_assets as assets  # noqa: E402


class PlanPrepare(RevisionRef):
    pass


class PlanEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: dict[str, Any]


class PlanMaterialize(BaseModel):
    plan_revision: int = Field(ge=1)
    plan_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    # the member saw the disclosure: selected test content / rubrics become visible in
    # the workspace Evaluation console (the transcript does not)
    acknowledge_disclosure: bool


def _plan_state(db: Session, row) -> dict[str, Any]:
    ops = {op.plan_id: op for op in assets.operations_of(db, row.id)}
    plans = assets.plans_of(db, row.id)
    return {
        "plans": [assets.plan_out(p, ops.get(p.id)) for p in plans],
        "operations": [assets.operation_out(op) for op in ops.values()],
        "disclosure": (
            "Creating assets publishes the SELECTED golden-test inputs, expected responses, "
            "assertions and evaluator rubrics of this plan to the workspace Evaluation console "
            "(Datasets / Evaluators), where every member of the workspace can read them. The "
            "conversation transcript itself stays private. Nothing is deployed, run, synced to "
            "a dataset snapshot or invoked; created assets are registered — they have not passed."
        ),
    }


@router.get("/conversations/{conversation_id}/evaluation-plan")
def get_evaluation_plan(
    conversation_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Ledger-only: every plan revision of the caller's conversation and the recorded
    materialization operations (no AWS read, no AWS write). An administrator reads
    any conversation's plan; the writes below still need a collaboration."""
    identity = _caller(request)
    row = service.viewable_conversation(db, ws.id, identity, conversation_id)
    return _plan_state(db, row)


@router.post("/conversations/{conversation_id}/evaluation-plan/prepare", status_code=201)
def prepare_evaluation_plan(
    conversation_id: str,
    req: PlanPrepare,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Draft a plan revision from one proposal revision (any shape-valid revision,
    including an already-approved one). No resource side effects."""
    identity = _caller(request)
    row = service.accessible_conversation(db, ws.id, principal_of(identity), conversation_id)
    plan = assets.prepare_plan(db, row, revision=req.revision, created_by=identity.username)
    return {"plan": assets.plan_out(plan, None), **_plan_state(db, row)}


@router.put("/conversations/{conversation_id}/evaluation-plan")
def edit_evaluation_plan(
    conversation_id: str,
    req: PlanEdit,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """A member edit is a NEW plan revision (draft or invalid with its errors)."""
    identity = _caller(request)
    row = service.accessible_conversation(db, ws.id, principal_of(identity), conversation_id)
    plan = assets.edit_plan(db, row, req.content, created_by=identity.username)
    return {"plan": assets.plan_out(plan, None), **_plan_state(db, row)}


@router.post("/conversations/{conversation_id}/evaluation-plan/materialize")
def materialize_evaluation_plan(
    conversation_id: str,
    req: PlanMaterialize,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> Any:
    """A collaborator holding ``agents.deploy``: claim exactly one plan revision/hash for
    materialization. 202 when this call created the operation (worker launched), 200
    with the recorded operation for a repeated / concurrent request. Creates a local
    Dataset, local evaluator rows and — only with ``eval_code_evaluators_enabled`` — one
    Lambda + role per code evaluator; never deploys, runs, publishes or invokes anything."""
    require_permission(request, assets.PERMISSION)  # belt and braces with ROUTE_POLICY
    identity = _caller(request)
    if not req.acknowledge_disclosure:
        raise AppError("assistant.disclosure_required",
                       "acknowledge that selected test content becomes visible in the "
                       "workspace Evaluation console", status_code=422)
    row = service.accessible_conversation(db, ws.id, principal_of(identity), conversation_id)
    # fresh re-resolution of the caller and the workspace grant at the claim boundary
    if auth_enabled():
        fresh = resolve_identity(request, db=db)
        if fresh is None or principal_of(fresh) != principal_of(identity):
            raise AppError("auth.required", "Authentication required", status_code=401)
        if not assets.may_materialize(fresh):
            raise assets.permission_error()
    ws_row = db.get(Workspace, ws.id)
    if ws_row is None:
        raise AppError("workspace.not_found", "workspace not found", status_code=404)
    _authorize(db, identity, ws_row)
    def recheck(session: Session) -> Identity:
        # re-resolved from the database INSIDE the claim transaction: a permission
        # revocation, disablement or grant removal between the route check and the
        # claim is honoured
        fresh = resolve_identity(request, db=session) if auth_enabled() else _caller(request)
        if fresh is None:
            raise AppError("auth.required", "Authentication required", status_code=401)
        if not assets.may_materialize(fresh):
            raise assets.permission_error()
        fresh_ws = session.get(Workspace, ws.id)
        if fresh_ws is None:
            raise AppError("workspace.not_found", "workspace not found", status_code=404)
        _authorize(session, fresh, fresh_ws)
        return fresh if auth_enabled() else replace(fresh, username="river")

    outcome = assets.approve_plan(
        db, row, ws_row, plan_revision=req.plan_revision, plan_hash=req.plan_hash,
        approved_by=identity.username, approver_user_id=identity.user_id, recheck=recheck,
        approver_is_admin=identity.is_admin,  # N17-obs227: the judges' visibility gate
    )
    if outcome.started or (
        outcome.operation.status in ("queued", "running")
        and assets.live_worker(outcome.operation.id) is None
    ):
        assets.start_async(outcome.operation.id)
    return DisplayTimeJSONResponse(
        status_code=202 if outcome.started else 200,
        content={"operation": assets.operation_out(outcome.operation),
                 "started": outcome.started},
    )


@router.get("/conversations/{conversation_id}/evaluation-plan/operations/{operation_id}")
def get_evaluation_operation(
    conversation_id: str,
    operation_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Ledger-only status (no AWS call, no mutation); readable by any viewer."""
    identity = _caller(request)
    row = service.viewable_conversation(db, ws.id, identity, conversation_id)
    return {"operation": assets.operation_out(assets.owned_operation(db, row, operation_id))}


@router.post("/conversations/{conversation_id}/evaluation-plan/operations/{operation_id}/retry")
def retry_evaluation_operation(
    conversation_id: str,
    operation_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Explicit, bounded retry of a partial/failed operation: resumes the persisted
    intents (same tokens/requests); never a fresh create."""
    require_permission(request, assets.PERMISSION)
    identity = _caller(request)
    row = service.accessible_conversation(db, ws.id, principal_of(identity), conversation_id)
    op = assets.owned_operation(db, row, operation_id)
    started = assets.retry_operation(db, op)
    db.expire_all()
    return {"operation": assets.operation_out(assets.owned_operation(db, row, operation_id)),
            "started": started}


@router.delete("/conversations/{conversation_id}/evaluation-plan/operations/{operation_id}/assets")
def cleanup_evaluation_operation(
    conversation_id: str,
    operation_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Delete exactly the artifacts the operation created (evaluator rows and, when a
    code chain was built, the Lambda with its role/log group). The local Dataset stays."""
    require_permission(request, assets.PERMISSION)
    identity = _caller(request)
    row = service.accessible_conversation(db, ws.id, principal_of(identity), conversation_id)
    op = assets.owned_operation(db, row, operation_id)
    op = assets.cleanup_operation(db, op, ws.context)
    return {"operation": assets.operation_out(op)}


# ---------------------------------------------------------------------------
# clearing a conversation together with what it created (History panel → CLEAR)
# ---------------------------------------------------------------------------

from app.assistant import purge as purge_mod  # noqa: E402


@router.get("/conversations/{conversation_id}/footprint")
def conversation_footprint(
    conversation_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """What CLEAR would remove for this conversation (Agents deployed from its
    approvals, the Runtime canaries run on them, evaluation-assets operations
    with their cloud resources, the local Datasets they created), what currently blocks
    it, and the newest clear attempt (``purge``). Ledger read only; owner-bound."""
    identity = _caller(request)
    row = service.owned_conversation(db, ws.id, principal_of(identity), conversation_id)
    fp = purge_mod.footprint(db, row)
    return {**fp, "can_clear": all(identity.can(p) for p in fp["required_permissions"])}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Delete the conversation and everything it created. Owner-bound; the caller also
    needs ``agents.deploy`` for cloud assets and ``agents.delete`` for an Agent, and must
    be the creator (or an administrator) of every canary and Agent the clear tears down
    (403 before any side effect). Refuses (409, nothing deleted) while a turn, an
    operation, a deployment job or a canary action is still running.

    A transcript-only conversation is deleted inline (200, ``deleted: true``). Anything
    with cloud resources becomes a durable clear job (202, ``job_id``; poll
    ``GET /api/jobs/{job_id}``): canaries first, then evaluation assets, Datasets and
    Agents, each read back gone before the next — see ``assistant.purge``.
    A repeated DELETE while that job is live returns the same job; after a failed job it
    starts a new attempt that resumes from the verified checkpoints."""
    identity = _caller(request)
    row = service.owned_conversation(db, ws.id, principal_of(identity), conversation_id)
    job, done = purge_mod.request_purge(db, row, identity=identity)
    if done is not None:
        return done
    purge_mod.start_purge_async(job.id)
    response.status_code = 202
    db.expire_all()
    return purge_mod.job_result(db.get(Job, job.id))
