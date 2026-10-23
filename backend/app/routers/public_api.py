"""Public /v1 API — the system-integration entrance.

Same invoke chain as the Chat playground (services.chat / services.invoke);
auth is an X-Api-Key header checked against hashed keys in the ledger.

No `X-Workspace` header is involved here: the key itself names the environment
it was minted for, so it both authenticates the caller and scopes what it can
reach. Targeting stays the agent row's business (`invoke._agent_workspace`) —
the two agree because a key only ever resolves agents from its own workspace.
"""

import time
from typing import Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.assistant.sessions import PrivateSessions, refuse_assistant_session
from app.core.billing_policy import SOURCE_V1
from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent, ApiKey
from app.routers.apikeys import hash_key
from app.schemas.attachments import AttachmentRequest
from app.services import agent_access, budgets, outbound_attachments, visibility
from app.services import api_keys as api_keys_service
from app.services.attachments import attachment_capability, prepare_attachments
from app.services.chat import chat_stream, sse_encode
from app.services.invoke import invoke_agent_text
from app.services.memory import scoped_actor
from app.services.runtime_discovery import invoke_capability, require_invoke_capability
from app.services.usage_meter import UsageMeter
from app.services.workspace import context_for_workspace, get_workspace_row

router = APIRouter(prefix="/v1", tags=["public-v1"])


def require_api_key(
    x_api_key: str | None = Header(default=None), db: Session = Depends(get_db)
) -> ApiKey:
    """The key behind this call — and with it, the workspace it is scoped to.

    The hash lookup is deliberately global: it is the only way to learn which
    workspace the caller is in, since /v1 carries no header. Everything after it
    filters on `key.workspace_id`.

    A key whose creator has been offboarded is refused (and disabled) here — see
    `services.api_keys`. On success `last_used_at` is stamped, throttled.
    """
    if not x_api_key:
        raise AppError("auth.missing_api_key", "X-Api-Key header required", status_code=401)
    key = db.query(ApiKey).filter(ApiKey.key_hash == hash_key(x_api_key)).first()
    if key is None or not key.enabled:
        raise AppError("auth.invalid_api_key", "invalid or disabled API key", status_code=401)
    if get_workspace_row(db, key.workspace_id or "") is None:
        # Its environment is gone, so it names no agents and nothing downstream
        # could resolve an invoke target. Answered like any other unusable key:
        # /v1 never reports whether a workspace once existed.
        raise AppError("auth.invalid_api_key", "invalid or disabled API key", status_code=401)
    if api_keys_service.creator_offboarded(db, key) is not None:
        # The member who minted it is gone (deleted, disabled, expired, or no
        # longer granted this workspace). Disabled for good in the same request
        # — expiry has no write event, so this is where it lands — and answered
        # like any other unusable key: /v1 never says why.
        key.enabled = False
        db.commit()
        raise AppError("auth.invalid_api_key", "invalid or disabled API key", status_code=401)
    if api_keys_service.touch_last_used(key):
        db.commit()
    return key


class InvokeV1Request(AttachmentRequest):
    session_id: str | None = None
    actor_id: str = "api"


def _active_agent(db: Session, key: ApiKey, agent_id: str) -> Agent:
    """The agent this key may invoke. One from another workspace — or one its
    creator cannot see under workspace isolation — reads exactly like a missing
    one: the key's scope must not be probeable."""
    agent = agent_access.agent_in(
        db, key.workspace_id, agent_id, viewer=visibility.viewer_for_key(db, key)
    )
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    require_invoke_capability(agent)
    return agent


def _metered(db: Session, key: ApiKey, agent: Agent) -> UsageMeter:
    """The billing gate of a /v1 turn, and its meter. The payer is the key's
    creator (``ApiKey.created_by``); a key with no recorded creator is exempt.
    Refused with ``402 budget.exceeded`` / ``budget.model_unpriced`` before any
    work starts (core/billing_policy.py classifies both invoke routes)."""
    payer = budgets.payer_for_api_key(db, key)
    budgets.check_agent(db, payer, agent)
    return UsageMeter.for_agent(
        payer, SOURCE_V1, agent, workspace_id=key.workspace_id, ref_type="api_key",
        ref_id=key.id,
    )


@router.get("/agents", summary="List active agents")
def v1_list_agents(
    db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)
) -> dict[str, Any]:
    agents = [
        agent
        for agent in db.query(Agent)
        .filter(
            Agent.status == "active",
            Agent.workspace_id == key.workspace_id,
            agent_access.agent_clause(visibility.viewer_for_key(db, key)),
        )
        .all()
        if invoke_capability(agent)["eligible"]
    ]
    return {
        "agents": [
            {"id": a.id, "name": a.name, "method": a.method, "version": a.version,
             "attachment_capability": attachment_capability(a)}
            for a in agents
        ]
    }


@router.post("/agents/{agent_id}/invoke", summary="Invoke an agent (sync)")
def v1_invoke(
    agent_id: str,
    req: InvokeV1Request,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
) -> dict[str, Any]:
    agent = _active_agent(db, key, agent_id)
    meter = _metered(db, key, agent)
    prepared = prepare_attachments(
        agent, req.attachments, prompt=req.prompt, session_id=req.session_id,
    )
    extra = {"attachments": prepared} if prepared else {}
    started = time.monotonic()
    # No signed-in console user behind an API key: an as_user tool's consent
    # could never complete, so the invoke layer answers such an ask with 409
    # identity.as_user_requires_console instead of a dead-end consent URL.
    meter.session_id = req.session_id
    result: dict[str, Any] | None = None
    try:
        result = invoke_agent_text(
            agent, req.prompt, session_id=req.session_id,
            actor_id=scoped_actor(agent.id, req.actor_id),
            console_user=False,
            on_usage=meter.on_usage,
            attachment_owner=outbound_attachments.api_key_owner(key.id),
            **extra,
        )
    finally:
        # the session ledger /v1 never had: who pays for this session
        meter.settle(failed=result is None,
                     session_id=result.get("session_id") if result else None)
    body = {
        "agent": agent.name,
        "text": result["text"],
        "session_id": result["session_id"],
        # reply images: metadata + this key's download `url`, never the bytes
        "attachments": result.get("attachments") or [],
        "latency_ms": int((time.monotonic() - started) * 1000),
    }
    return body


@router.post("/agents/{agent_id}/invoke-stream", summary="Invoke an agent (SSE stream)")
def v1_invoke_stream(
    agent_id: str,
    req: InvokeV1Request,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
) -> StreamingResponse:
    agent = _active_agent(db, key, agent_id)
    refuse_assistant_session(agent, req.session_id)  # 404 before the stream opens
    meter = _metered(db, key, agent)
    prepared = prepare_attachments(
        agent, req.attachments, prompt=req.prompt, session_id=req.session_id,
    )
    extra = {"attachments": prepared} if prepared else {}
    mem_actor = scoped_actor(agent.id, req.actor_id)

    def generate():
        # console_user=False: an as_user consent ask ends the stream with an
        # `error` event (identity.as_user_requires_console) — see v1_invoke
        for event in chat_stream(
            agent, req.prompt, session_id=req.session_id, actor_id=mem_actor,
            console_user=False, meter=meter,
            attachment_owner=outbound_attachments.api_key_owner(key.id), **extra,
        ):
            yield sse_encode(event)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/agents/{agent_id}/attachments/{attachment_id}",
    summary="Download an image the agent answered with",
)
def v1_reply_attachment(
    agent_id: str,
    attachment_id: str,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
) -> StreamingResponse:
    """The bytes behind an ``attachments[].url`` of a /v1 reply. Only the API key
    whose turn produced the image may read it; another key, workspace, agent or a
    private session reads exactly like a missing id (404)."""
    agent = agent_access.agent_in(
        db, key.workspace_id, agent_id, viewer=visibility.viewer_for_key(db, key),
    )
    row = (
        outbound_attachments.find(
            db, key.workspace_id, agent_id, attachment_id,
            outbound_attachments.api_key_owner(key.id),
        )
        if agent is not None else None
    )
    private = PrivateSessions(db, key.workspace_id, None)
    body = (
        outbound_attachments.open_object(context_for_workspace(key.workspace_id), row)
        if row is not None and private.visible(row.session_id) else None
    )
    if body is None:
        raise NotFoundError("chat.attachment_not_found", "attachment not found")
    return outbound_attachments.image_response(row, body)
