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

import json as _json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.assistant import evaluation_repair, service
from app.assistant import proposal as proposal_contract
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


class TurnResponse(StreamingResponse):
    """A streaming response that owns its turn: whatever way the response ends —
    completion, ASGI 2.0 disconnect (task-group cancel) or ASGI 2.4 send error — the
    turn is finalized deterministically (upstream stream closed, generator closed,
    partial answer persisted as interrupted, claim released), never left to GC."""

    def __init__(self, run: service.TurnRun, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._run = run

    async def __call__(self, scope, receive, send) -> None:
        run = self._run

        async def receive_watch():
            message = await receive()
            if message.get("type") == "http.disconnect":
                run.signal()  # close upstream NOW; the blocked worker read returns
            return message

        async def send_watch(message):
            try:
                await send(message)
            except BaseException:
                # ASGI 2.4: a failed send (OSError, or the cancellation it turns into
                # further up the stack) IS the disconnect signal — close the upstream
                # now, before anything waits for the worker thread to unwind
                run.signal()
                raise

        try:
            await super().__call__(scope, receive_watch, send_watch)
        finally:
            import anyio

            with anyio.CancelScope(shield=True):  # cleanup survives a cancelled scope
                await anyio.to_thread.run_sync(self._run.cancel)


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
    prompt = req.prompt
    if req.evaluation_plan_repair is not None:
        prompt = evaluation_repair.repair_prompt(
            db, conversation, prompt,
            plan_revision=req.evaluation_plan_repair.plan_revision,
            plan_hash=req.evaluation_plan_repair.plan_hash,
        )
    workspace_row, workspace = ws.row, ws.context
    run = service.TurnRun()

    def generate() -> Iterator[str]:
        # The stream outlives the request scope → its own session.
        session = SessionLocal()
        try:
            conversation = service.owned_conversation(session, ws.id, principal, conversation_id)
            run.inner = service.run_turn(
                session, conversation, workspace_row, workspace, identity, prompt, run=run
            )
            for event in run.inner:
                yield sse_encode(event)
        except AppError as exc:  # a claim refusal after the response started
            yield sse_encode({"event": "error", "data": {"code": exc.code,
                                                        "message": exc.message}})
        finally:
            session.close()

    run.generator = generate()
    return TurnResponse(
        run,
        run.generator,
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


# ---------------------------------------------------------------------------
# SE-047 — the reviewed evaluation-assets plan and its materialization
# ---------------------------------------------------------------------------

from app.assistant import evaluation_assets as assets  # noqa: E402
from app.routers.auth import require_admin  # noqa: E402


class PlanPrepare(RevisionRef):
    pass


class PlanEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: dict[str, Any]


class LambdaRevisionReview(BaseModel):
    """SE-049 reviewed recovery of the first-initialization RevisionId conflict. Every
    field names what the administrator verified; the CloudTrail event is read
    server-side, never trusted from the client."""

    model_config = ConfigDict(extra="forbid")
    plan_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    expected_created_revision_id: str = Field(min_length=1, max_length=128)
    expected_current_revision_id: str = Field(min_length=1, max_length=128)
    cloudtrail_event_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)


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
            "AWS Datasets or invoked; created assets are registered — they have not passed."
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
    materialization operations (no AWS read, no AWS write)."""
    identity = _caller(request)
    row = service.owned_conversation(db, ws.id, principal_of(identity), conversation_id)
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
    row = service.owned_conversation(db, ws.id, principal_of(identity), conversation_id)
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
    row = service.owned_conversation(db, ws.id, principal_of(identity), conversation_id)
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
    """Administrator + conversation owner: claim exactly one plan revision/hash for
    materialization. 202 when this call created the operation (worker launched), 200
    with the recorded operation for a repeated / concurrent request. Creates a local
    Dataset, AgentCore evaluators and — for code rules — one Lambda + its role; never
    deploys, runs, syncs or invokes anything."""
    require_admin(request)  # belt and braces with ROUTE_POLICY
    identity = _caller(request)
    if not identity.is_admin:
        raise AppError("auth.admin_required", "administrator role required", status_code=403)
    if not req.acknowledge_disclosure:
        raise AppError("assistant.disclosure_required",
                       "acknowledge that selected test content becomes visible in the "
                       "workspace Evaluation console", status_code=422)
    row = service.owned_conversation(db, ws.id, principal_of(identity), conversation_id)
    # fresh re-resolution of the caller and the workspace grant at the claim boundary
    if auth_enabled():
        fresh = resolve_identity(request, db=db)
        if fresh is None or not fresh.is_admin or principal_of(fresh) != principal_of(identity):
            raise AppError("auth.required", "Authentication required", status_code=401)
    ws_row = db.get(Workspace, ws.id)
    if ws_row is None:
        raise AppError("workspace.not_found", "workspace not found", status_code=404)
    _authorize(db, identity, ws_row)
    def recheck(session: Session) -> Identity:
        # re-resolved from the database INSIDE the claim transaction: a demotion,
        # disablement or grant removal between the route check and the claim is honoured
        fresh = resolve_identity(request, db=session) if auth_enabled() else _caller(request)
        if fresh is None:
            raise AppError("auth.required", "Authentication required", status_code=401)
        if not fresh.is_admin:
            raise AppError("auth.admin_required", "administrator role required", status_code=403)
        fresh_ws = session.get(Workspace, ws.id)
        if fresh_ws is None:
            raise AppError("workspace.not_found", "workspace not found", status_code=404)
        _authorize(session, fresh, fresh_ws)
        return fresh if auth_enabled() else replace(fresh, username="river")

    outcome = assets.approve_plan(
        db, row, ws_row, plan_revision=req.plan_revision, plan_hash=req.plan_hash,
        approved_by=identity.username, approver_user_id=identity.user_id, recheck=recheck,
    )
    if outcome.started or (
        outcome.operation.status in ("queued", "running")
        and assets.live_worker(outcome.operation.id) is None
    ):
        assets.start_async(outcome.operation.id)
    return JSONResponse(
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
    """Ledger-only status (no AWS call, no mutation)."""
    identity = _caller(request)
    row = service.owned_conversation(db, ws.id, principal_of(identity), conversation_id)
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
    require_admin(request)
    identity = _caller(request)
    row = service.owned_conversation(db, ws.id, principal_of(identity), conversation_id)
    op = assets.owned_operation(db, row, operation_id)
    started = assets.retry_operation(db, op)
    db.expire_all()
    return {"operation": assets.operation_out(assets.owned_operation(db, row, operation_id)),
            "started": started}


@router.post("/conversations/{conversation_id}/evaluation-plan/operations/{operation_id}"
             "/lambda-revision-review")
def review_lambda_revision(
    conversation_id: str,
    operation_id: str,
    req: LambdaRevisionReview,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Administrator + owner reviewed recovery of ONE conflict: the Lambda's RevisionId
    moved between CreateFunction (Pending) and Active. Reads the nominated CloudTrail
    CreateFunction event and the settled function server-side, records an append-only
    review and re-queues the ordinary worker. No cloud write happens in this route."""
    require_admin(request)
    identity = _caller(request)
    if not identity.is_admin:
        raise AppError("auth.admin_required", "administrator role required", status_code=403)
    row = service.owned_conversation(db, ws.id, principal_of(identity), conversation_id)
    op = assets.owned_operation(db, row, operation_id)
    ws_row = db.get(Workspace, ws.id)
    if ws_row is None:
        raise AppError("workspace.not_found", "workspace not found", status_code=404)
    _authorize(db, identity, ws_row)

    def recheck(session: Session) -> Identity:
        fresh = resolve_identity(request, db=session) if auth_enabled() else _caller(request)
        if fresh is None:
            raise AppError("auth.required", "Authentication required", status_code=401)
        if not fresh.is_admin or principal_of(fresh) != principal_of(identity):
            raise AppError("auth.admin_required", "administrator role required", status_code=403)
        fresh_ws = session.get(Workspace, ws.id)
        if fresh_ws is None:
            raise AppError("workspace.not_found", "workspace not found", status_code=404)
        _authorize(session, fresh, fresh_ws)
        service.owned_conversation(session, ws.id, principal_of(fresh), conversation_id)
        return fresh if auth_enabled() else replace(fresh, username="river")

    outcome = assets.review_lambda_initial_revision(
        db, op, plan_hash=req.plan_hash, expected_created=req.expected_created_revision_id,
        expected_current=req.expected_current_revision_id, event_id=req.cloudtrail_event_id,
        reason=req.reason, reviewer=identity.username, reviewer_user_id=identity.user_id,
        recheck=recheck,
    )
    db.expire_all()
    return {"operation": assets.operation_out(assets.owned_operation(db, row, operation_id)),
            "review": outcome.review, "started": outcome.started}


@router.delete("/conversations/{conversation_id}/evaluation-plan/operations/{operation_id}/assets")
def cleanup_evaluation_operation(
    conversation_id: str,
    operation_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Delete exactly the cloud artifacts the operation created (evaluators, Lambda,
    its role/log group, the additive role policy). The local Dataset stays."""
    require_admin(request)
    identity = _caller(request)
    row = service.owned_conversation(db, ws.id, principal_of(identity), conversation_id)
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
    approvals, evaluation-assets operations with their cloud resources, the local
    Datasets they created) and what currently blocks it. Ledger read only; owner-bound."""
    row = service.owned_conversation(db, ws.id, principal_of(_caller(request)), conversation_id)
    return purge_mod.footprint(db, row)


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Delete the conversation and everything it created: fenced cleanup of every
    evaluation-assets operation, the local Datasets, every deployed Agent (the same
    teardown as DELETE /api/agents/{id}), then the ledger rows. Owner-bound; an
    administrator is required as soon as cloud assets or an Agent are involved.
    Refuses (409, nothing deleted) while a turn, an operation or a deployment job is
    still running, and stops (409) if an operation cannot be fully cleaned."""
    identity = _caller(request)
    row = service.owned_conversation(db, ws.id, principal_of(identity), conversation_id)
    return purge_mod.purge(db, row, ws.context, is_admin=identity.is_admin)
