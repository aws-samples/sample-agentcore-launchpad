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

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.assistant.sessions import refuse_assistant_session
from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent, ApiKey
from app.routers.apikeys import hash_key
from app.schemas.attachments import AttachmentRequest
from app.services import agent_access, creators, login_guard, visibility
from app.services import api_keys as api_keys_service
from app.services.attachments import attachment_capability, prepare_attachments
from app.services.chat import chat_stream, sse_encode
from app.services.chat_ledger import SOURCE_PUBLIC_API, record_stream, record_turn
from app.services.invoke import (
    invoke_agent_text,
    require_session_open,
    validate_session_id,
)
from app.services.memory_backend import scoped_actor
from app.services.policy_entities import Principal
from app.services.runtime_discovery import invoke_capability, require_invoke_capability
from app.services.workspace import get_workspace_row

router = APIRouter(prefix="/v1", tags=["public-v1"])


def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> ApiKey:
    """The key behind this call — and with it, the workspace it is scoped to.

    The hash lookup is deliberately global: it is the only way to learn which
    workspace the caller is in, since /v1 carries no header. Everything after it
    filters on `key.workspace_id`.

    A key whose creator has been offboarded is refused (and disabled) here — see
    `services.api_keys` (#259). On success `last_used_at` is stamped, throttled.

    r12-g (N12-obs57, rev-r11): every 401 from this dependency counts against the
    caller's source IP in a sliding window (`auth_api_key_ip_limit` per
    `auth_api_key_ip_window_seconds`); once tripped, further INVALID attempts from that
    peer answer 429 `auth.rate_limited` with `Retry-After`. The window is consulted only
    after a failed check — behind nginx / the NLB every caller shares one peer address,
    so a valid key is never refused by it and never counts.
    """
    peer = login_guard.peer_address(request)
    try:
        return _resolve_api_key(x_api_key, db)
    except AppError as exc:
        if exc.status_code == 401:
            waiting = login_guard.api_key_retry_after(peer)
            login_guard.record_api_key_failure(peer)
            if waiting:
                raise login_guard.rate_limited_error(waiting, what="API-key") from None
        raise


def _resolve_api_key(x_api_key: str | None, db: Session) -> ApiKey:
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


def _key_creator(key: ApiKey) -> creators.Creator | None:
    """A `/v1` chat session belongs to the key's creator (CN extra: members list their
    own sessions). A legacy key with no recorded creator opens an unowned session."""
    if key.created_by is None and key.created_by_name is None:
        return None
    return creators.Creator(user_id=key.created_by, name=key.created_by_name)


class InvokeV1Request(AttachmentRequest):
    session_id: str | None = None
    actor_id: str = "api"


def _active_agent(db: Session, key: ApiKey, agent_id: str) -> Agent:
    """The agent this key may invoke. One from another workspace — or one its
    creator cannot see under workspace isolation (`visibility.viewer_for_key`) —
    reads exactly like a missing one: the key's scope must not be probeable. The 404
    lands before the D20 policy gate, so a denial never discloses a hidden agent."""
    agent = agent_access.agent_in(
        db, key.workspace_id, agent_id, viewer=visibility.viewer_for_key(db, key)
    )
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    require_invoke_capability(agent)
    return agent


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
            # the key follows its creator's CURRENT visibility (workspace isolation)
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
    validate_session_id(req.session_id)  # N-SCN-obs16: 422 before the gate and any AWS call
    agent = _active_agent(db, key, agent_id)
    prepared = prepare_attachments(
        agent, req.attachments, prompt=req.prompt, session_id=req.session_id,
    )
    extra = {"attachments": prepared} if prepared else {}
    started = time.monotonic()
    # No signed-in console user behind an API key: an as_user tool's consent
    # could never complete, so the invoke layer answers such an ask with 409
    # identity.as_user_requires_console instead of a dead-end consent URL.
    # D20: the key is the policy principal (Launchpad::ApiKey) — a `forbid … invoke`
    # naming it answers 403 policy.denied before any AWS call.
    result = invoke_agent_text(
        agent, req.prompt, session_id=req.session_id,
        actor_id=scoped_actor(agent.id, req.actor_id),
        console_user=False,
        principal=Principal.from_api_key(key),
        **extra,
    )
    # N-SCN-obs1: the console lists only sessions with a transcript, so the /v1
    # turn is ledgered like a console turn, marked `source: public_api`. The
    # ledger keeps the caller's bare actor (default "api"), as the console does.
    # latency is the agent's answer time, not the ledger writes that follow (rev-r8 nit)
    latency_ms = int((time.monotonic() - started) * 1000)
    if result.get("session_id"):
        record_turn(
            agent.workspace_id or key.workspace_id or "", agent.id, result["session_id"],
            req.actor_id, req.prompt, result.get("text") or "",
            runtime_version=agent.version,
            attachments=prepared.metadata if prepared else None,
            source=SOURCE_PUBLIC_API,
            truncated=bool(result.get("truncated")),  # N-SCN-bug4: the ledger row too
            creator=_key_creator(key),
        )
    body = {
        "agent": agent.name,
        "text": result["text"],
        "session_id": result["session_id"],
        "latency_ms": latency_ms,
    }
    if result.get("truncated"):
        body["truncated"] = True  # N-SCN-bug4: additive; absent on a complete answer
    return body


@router.post("/agents/{agent_id}/invoke-stream", summary="Invoke an agent (SSE stream)")
def v1_invoke_stream(
    agent_id: str,
    req: InvokeV1Request,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
) -> StreamingResponse:
    validate_session_id(req.session_id)  # N-SCN-obs16: 422 before the stream opens
    agent = _active_agent(db, key, agent_id)
    refuse_assistant_session(agent, req.session_id)  # 404 before the stream opens
    # N16-obs88: an ENDED session id is 409 chat.session_ended as a plain response, before
    # the stream opens (and before `record_stream` would upsert the row) — the same gate
    # the shared chain applies inside `chat_stream` for every entrance
    require_session_open(agent, req.session_id)
    prepared = prepare_attachments(
        agent, req.attachments, prompt=req.prompt, session_id=req.session_id,
    )
    extra = {"attachments": prepared} if prepared else {}
    mem_actor = scoped_actor(agent.id, req.actor_id)
    principal = Principal.from_api_key(key)  # D20: resolved before the stream opens

    workspace_id = agent.workspace_id or key.workspace_id or ""
    creator = _key_creator(key)  # resolved before the stream outlives the request

    def generate():
        # console_user=False: an as_user consent ask ends the stream with an
        # `error` event (identity.as_user_requires_console) — see v1_invoke.
        # record_stream persists the transcript in event order (N-SCN-obs1).
        events = chat_stream(
            agent, req.prompt, session_id=req.session_id, actor_id=mem_actor,
            console_user=False, principal=principal, **extra,
        )
        for event in record_stream(
            events, workspace_id=workspace_id, agent_id=agent.id, actor_id=req.actor_id,
            prompt=req.prompt, runtime_version=agent.version,
            attachments=prepared.metadata if prepared else None, source=SOURCE_PUBLIC_API,
            creator=creator,
        ):
            yield sse_encode(event)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
