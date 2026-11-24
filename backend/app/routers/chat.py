"""Chat playground endpoints — SSE streaming over the shared invoke chain."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import exists
from sqlalchemy.orm import Session

from app.assistant.sessions import refuse_assistant_session
from app.core.db import get_db
from app.core.errors import AppError, NotFoundError, envelope, mapped_aws_error
from app.core.project_scope import current_project, project_context
from app.models.ledger import Agent, ChatMessage, ChatSession
from app.routers.auth import enabled as auth_enabled
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.attachments import AttachmentRequest, SessionIdField
from app.services import (
    agent_access,
    agent_files,
    chat_turns,
    context_layer_gateway,
    memory_ownership,
    oauth_sessions,
    policy_identity,
)
from app.services import feedback as feedback_service
from app.services import inbound_auth as inbound_auth_service
from app.services import memory as memory_service
from app.services.attachments import prepare_attachments
from app.services.chat import StopSignal, chat_stream, sse_encode
from app.services.chat_ledger import fold_structured, persist_events, visible_rows
from app.services.invoke import (
    CONSOLE_TOOL_APPROVALS,
    console_tool_approvals,
    stop_agent_session,
)
from app.services.runtime_discovery import require_invoke_capability
from app.services.visibility import Viewer, require_viewer
from app.templates import context_layer_support, gateway_support

router = APIRouter(prefix="/api", tags=["chat"])


class ToolApprovalsIn(BaseModel):
    approved: list[str] = Field(default_factory=list, max_length=50)


class ChatRequest(AttachmentRequest):
    session_id: str | None = SessionIdField
    # "Invoke as me" for JWT-inbound agents (identity P3): True presents the
    # signed-in user's own Cognito JWT, False the workspace M2M token, None
    # (older clients) the user JWT when one can be minted, else M2M. Ignored
    # by IAM agents, whose caller is always the platform's SigV4 role.
    as_user: bool | None = None
    # chat-streaming §4.1: this turn retries a failed one — any ledger id of that
    # turn (its user row, as `meta.user_message_id` names it, or its error row). It
    # must be the session's latest turn and have failed or answered nothing; the
    # failed turn then stays in the ledger but /history no longer replays it.
    retry_of: int | None = None
    # strands-builder SB-2 tool hooks: the approval-list tools the user approved on
    # the chat's approval card for this turn (one entry = one call)
    approved_tools: list[str] | None = Field(default=None, max_length=50)
    # the same, in the shape the console chat sends (`{approved: [tool]}`)
    tool_approvals: ToolApprovalsIn | None = None

    def approved(self) -> list[str] | None:
        """Every approved tool name of the turn, from either field (None: none sent)."""
        if self.approved_tools is None and self.tool_approvals is None:
            return None
        names = [*(self.approved_tools or []),
                 *(self.tool_approvals.approved if self.tool_approvals else [])]
        return list(dict.fromkeys(names))[:50]


def _turn_tool_approvals(agent: Agent, approved: list[str] | None) -> dict[str, Any] | None:
    """``tool_approvals`` for a console turn of an agent with an approval list: the
    console user can approve (the runtime pauses the tool and asks), plus what was
    approved. Agents without one get no new payload field."""
    hooks = (agent.spec or {}).get("tool_hooks") or {}
    if not hooks.get("approval_tools"):
        return None
    return console_tool_approvals([name[:128] for name in approved or []])


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


def _refuse_foreign_session(
    db: Session, workspace_id: str, agent_id: str, session_id: str | None,
) -> None:
    """A session id another project already holds for this agent is not continued
    here. Only a system preset (shared by every project) can reach this: without the
    check the turn would open a second ledger row in this project on the other
    project's AgentCore runtime session. It answers like a missing session."""
    pid = current_project()
    if not session_id or pid is None:
        return
    with project_context(None):
        foreign = (
            db.query(ChatSession.id)
            .filter(
                ChatSession.workspace_id == workspace_id,
                ChatSession.agent_id == agent_id,
                ChatSession.session_id == session_id,
                ChatSession.project_id != pid,
            )
            .first()
        )
    if foreign is not None:
        raise NotFoundError("chat.session_not_found", "chat session not found")


def _agent_in(
    db: Session, ws: WorkspaceScope, agent_id: str, viewer: Viewer = Viewer.ALL
) -> Agent:
    """The agent, or 404 — including when it belongs to another workspace or is
    hidden by workspace isolation, which the caller must not be able to tell apart
    from a missing one."""
    agent = agent_access.agent_in(db, ws.id, agent_id, viewer=viewer, include_deleted=True)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    return agent


def _get_active_agent(
    db: Session, ws: WorkspaceScope, agent_id: str, viewer: Viewer = Viewer.ALL
) -> Agent:
    agent = _agent_in(db, ws, agent_id, viewer)
    require_invoke_capability(agent)
    return agent


def _chat_bearer_token(
    agent: Agent,
    as_user: bool | None,
    ws: WorkspaceScope,
    identity: Any,
    gateway_access_token: str | None,
) -> str | None:
    """The bearer a JWT-inbound agent's chat turn presents, or None (= M2M).

    The user JWT is the same Cognito token the Gateway policy path mints
    (console app client, groups per role) — reused when this turn already
    minted it. The Memory/session actor is unaffected either way: it stays
    the compound ``scoped_actor`` the payload carries, so toggling mid-session
    keeps one conversation and one memory partition.
    """
    if not inbound_auth_service.is_jwt_mode(agent) or as_user is False:
        return None
    token = gateway_access_token or policy_identity.gateway_user_token(
        ws.context, identity.username, identity.role, identity.email,
    )
    if token is None and as_user:
        # An explicit "as me" with no signed-in pool user (auth gate off) must
        # not quietly become the machine identity.
        raise AppError(
            "chat.as_user_unavailable",
            "invoking as yourself needs a console sign-in through the workspace "
            "user pool — turn off \"invoke as me\" to call with the workspace "
            "machine token",
            status_code=409,
        )
    return token


def _console_events(events: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Rewrite an as_user refusal for the console: its fix is the "invoke as me"
    toggle, not a user JWT. Runs before persistence, so the saved error matches."""
    for event in events:
        data = event["data"]
        code = data.get("code")
        if event["event"] == "error" and code == oauth_sessions.AS_USER_REQUIRES_USER_JWT:
            detail = data.get("detail") or {}
            refusal = oauth_sessions.as_user_requires_user_jwt(
                provider=detail.get("provider", ""), tool=detail.get("tool", ""),
                agent_id=detail.get("agent_id", ""), console=True,
            )
            data = envelope(refusal.code, refusal.message, refusal.detail)
            event = {"event": "error", "data": data}
        yield event


def _retry_turn(
    db: Session, workspace_id: str, agent_id: str, session_id: str | None,
    retry_of: int | None,
) -> int | None:
    """The user row of the turn a retry replaces (chat-streaming §4.1), or None.

    ``retry_of`` may name any row of that turn. A row outside this session — another
    session's, agent's, workspace's or project's — answers 404 like a missing one;
    a turn that is not the latest, or that answered without an error, is 409."""
    if retry_of is None:
        return None
    scope = (
        ChatMessage.workspace_id == workspace_id,
        ChatMessage.agent_id == agent_id,
        ChatMessage.session_id == session_id,
    )
    target = (
        db.query(ChatMessage.id).filter(*scope, ChatMessage.id == retry_of).first()
        if session_id else None
    )
    if target is None:
        raise NotFoundError("chat.message_not_found", "chat message not found")
    user = (
        db.query(ChatMessage.id)
        .filter(*scope, ChatMessage.role == "user", ChatMessage.id <= retry_of)
        .order_by(ChatMessage.id.desc())
        .first()
    )
    roles = set() if user is None else {
        role for (role,) in db.query(ChatMessage.role).filter(*scope, ChatMessage.id > user[0])
    }
    # an empty turn (no answer) is as retryable as a failed one; a later user row
    # means the turn is no longer the latest
    if user is None or "user" in roles or ("error" not in roles and "agent" in roles):
        raise AppError(
            "chat.retry_not_allowed",
            "only the latest turn, and only a failed or empty one, can be retried",
            {"retry_of": retry_of}, status_code=409,
        )
    return user[0]


# chat-streaming §4.2: events after which a replay could repeat what the user saw or
# a tool's side effect — once one passed, a failure is retried only by the user
OUTPUT_EVENTS = frozenset({
    "delta", "tool", "auth_required", "policy_denied", "approval_required",
})
# the type names of transport failures (botocore's and httpx's): the connection to
# the runtime dropped or timed out, rather than the runtime answering an error
_TRANSPORT_ERRORS = frozenset({
    "ReadTimeoutError", "ConnectTimeoutError", "ConnectionClosedError",
    "EndpointConnectionError", "TransportError", "NetworkError", "TimeoutException",
    "ConnectError", "ConnectTimeout", "ReadError", "ReadTimeout", "WriteError",
    "WriteTimeout", "PoolTimeout", "RemoteProtocolError",
})


def _rebuilt_error(data: dict[str, Any]) -> BaseException:
    """The exception an ``error`` event was made from, rebuilt from its payload, so
    the evaluation runner's ``transient_invoke_error`` decides for chat too.
    ``chat_stream`` writes an ``AppError`` as its envelope and anything else as
    ``"<TypeName>: <message>"``."""
    import httpx
    from botocore.exceptions import ClientError

    from app.services.agentcore import runtime as rt

    message = str(data.get("message") or "")
    if data.get("code"):
        return AppError(str(data["code"]), message, data.get("detail"))
    name, _, text = message.partition(": ")
    if name in _TRANSPORT_ERRORS:
        return httpx.TransportError(text)
    if name in ("ClientError", "EventStreamError"):
        found = re.search(r"An error occurred \((\w+)\)", text)
        code = found.group(1) if found else ""
        return ClientError({"Error": {"Code": code, "Message": text}}, "invoke")
    if name == "RuntimeBearerHttpError":
        found = re.search(r"HTTP (\d{3})", text)
        return rt.RuntimeBearerHttpError(int(found.group(1)) if found else 0, text)
    if name == "RuntimeError":
        return RuntimeError(text)
    return Exception(message)


def retry_hint(
    data: dict[str, Any], *, produced: bool, timeout_s: Any = None,
) -> dict[str, Any]:
    """How the console offers to retry a failed turn (chat-streaming §4.2-4.3).

    ``category``: ``timeout`` (the agent used its whole budget), ``connection``
    (the transport to the runtime dropped), ``busy`` (throttling, 5xx, an upstream
    hiccup ``transient_invoke_error`` recognises), ``config`` (any other named
    error — permissions, parameters, limits, a policy) or ``other``. ``manual``: the
    retry button is offered (never for ``config`` / ``timeout``, which a replay
    cannot fix). ``auto``: retried once without asking — only a transient failure of
    a turn that produced no output, so a replay repeats nothing."""
    from botocore.exceptions import ClientError

    from app.evaluation.service import transient_invoke_error
    from app.services.agentcore import runtime as rt

    error = _rebuilt_error(data)
    flagged = data.get("transient")
    transient = flagged if isinstance(flagged, bool) else transient_invoke_error(error)
    if data.get("code") == "harness.execution_timeout":
        category = "timeout"
    elif transient:
        transport = str(data.get("message") or "").partition(": ")[0] in _TRANSPORT_ERRORS
        category = "connection" if transport else "busy"
    elif isinstance(error, (AppError, ClientError, rt.RuntimeBearerHttpError)):
        category = "config"  # the upstream named a final error: a 4xx, a limit, a policy
    else:
        category = "other"
    hint: dict[str, Any] = {
        "category": category,
        "manual": category in ("connection", "busy", "other"),
        "auto": category in ("connection", "busy") and not produced,
    }
    if category == "timeout" and isinstance(timeout_s, int):
        hint["timeout_s"] = timeout_s
    return hint


def _retry_hints(
    events: Iterator[dict[str, Any]], *, timeout_s: Any = None,
) -> Iterator[dict[str, Any]]:
    """Add ``retry`` (``retry_hint``) to the console's ``error`` events."""
    produced = False
    try:
        for event in events:
            kind = event["event"]
            if kind in OUTPUT_EVENTS:
                produced = True
            elif kind == "error":
                hint = retry_hint(event["data"], produced=produced, timeout_s=timeout_s)
                event = {"event": "error", "data": {**event["data"], "retry": hint}}
            yield event
    finally:
        close = getattr(events, "close", None)
        if close is not None:
            close()  # closing the chat stream closes the upstream invocation


@router.post("/chat/{agent_id}")
def chat(
    agent_id: str,
    req: ChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> StreamingResponse:
    agent = _get_active_agent(db, ws, agent_id, viewer)
    # An assistant-owned session of the preset is private to the assistant page.
    refuse_assistant_session(agent, req.session_id)
    _refuse_foreign_session(db, ws.id, agent.id, req.session_id)
    retry_of = _retry_turn(db, ws.id, agent.id, req.session_id, req.retry_of)
    prepared = prepare_attachments(
        agent, req.attachments, prompt=req.prompt, session_id=req.session_id,
    )
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
    # A COA context layer acts as the signed-in member, so the turn names them — on
    # a Harness too, whose COA token the backend fetches for exactly this username.
    names_user = needs_gateway_identity or context_layer_support.attached(agent.spec)
    # Gateway mode: the managed COA gateway's consent preflight presents this very JWT
    # (policy_identity's cache), and a consent completes only with it (F8), so it is
    # taken with the margin the gate asks for: one token for the preflight and the turn.
    coa_gateway = context_layer_support.uses_gateway(agent.spec)
    gateway_access_token = (
        policy_identity.gateway_user_token(
            ws.context,
            identity.username,
            identity.role,
            identity.email,
            **(
                {"min_valid_s": context_layer_gateway.CONSENT_JWT_MIN_VALID_S}
                if coa_gateway else {}
            ),
        )
        if needs_gateway_identity
        else None
    )
    bearer_token = _chat_bearer_token(agent, req.as_user, ws, identity, gateway_access_token)

    # The stream outlives the request scope, so it carries the plain id and the
    # already-built context rather than reaching back for the resolved scope.
    workspace_id = ws.id
    workspace = ws.context
    timeout_s = (agent.spec or {}).get("timeout_seconds")
    tool_approvals = _turn_tool_approvals(agent, req.approved())

    def run(stop: StopSignal) -> Iterator[dict[str, Any]]:
        # the worker's own context: the invoke reads the turn's approvals from it
        CONSOLE_TOOL_APPROVALS.set(tool_approvals)
        stream_kwargs: dict[str, Any] = {}
        if prepared:
            stream_kwargs["attachments"] = prepared
        if names_user:
            stream_kwargs["runtime_user_id"] = identity.username
        if coa_gateway:
            # the gate mints the member's pool JWT for the role they signed in with
            stream_kwargs["user_role"] = identity.role
            stream_kwargs["user_email"] = identity.email
        if gateway_access_token:
            stream_kwargs["gateway_access_token"] = gateway_access_token
        if bearer_token:
            stream_kwargs["bearer_token"] = bearer_token
        events = _retry_hints(_console_events(chat_stream(
            agent,
            req.prompt,
            session_id=req.session_id,
            actor_id=mem_actor,
            workspace=workspace,
            status_events=True,  # console only: phase / thinking / tool_end (chat-streaming S2)
            stop=stop,
            **stream_kwargs,
        )), timeout_s=timeout_s)
        # Thread items are persisted in event order (int-pk = replay order) so
        # the playground can restore a session's history exactly as rendered.
        return persist_events(
            events,
            workspace_id=workspace_id,
            agent_id=agent.id,
            prompt=req.prompt,
            actor_id=actor_id,
            session_id=req.session_id,
            runtime_version=agent.version,
            attachments=prepared.metadata if prepared else None,
            retry_of=retry_of,
        )

    # chat-streaming §5.2: the turn runs on a background worker, so a dropped
    # connection no longer ends it; this response is the turn's first subscriber.
    # A system preset (no project of its own) runs as the caller's project.
    turn = chat_turns.start_turn(
        run, workspace_id=workspace_id, agent_id=agent.id,
        project_id=agent.project_id or current_project(),
        session_id=req.session_id, timeout_s=timeout_s,
    )
    return _turn_response(turn, after=0)


def _turn_response(turn: chat_turns.Turn, after: int) -> StreamingResponse:
    def generate() -> Iterator[str]:
        for seq, event in turn.subscribe(after):
            # a named heartbeat (with `elapsed_ms`) lets the page time the turn and
            # tell a live connection from a dead one; /v1 keeps the comment. The SSE
            # `id` (the frame's last field) is the event's seq, what a resume continues
            # after.
            frame = sse_encode(event, named_heartbeat=True)
            yield frame if seq is None else f"{frame[:-1]}id: {seq}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _turn_in(db: Session, ws: WorkspaceScope, agent: Agent, turn_id: str) -> chat_turns.Turn:
    """A turn of this agent, workspace and project whose session the caller can see,
    or 404 — a turn of another project must not be told apart from a missing one."""
    turn = chat_turns.registry.get(turn_id)
    pid = current_project()
    if (
        turn is None or turn.workspace_id != ws.id or turn.agent_id != agent.id
        or (pid is not None and turn.project_id != pid)
        or not turn.session_id
        or db.query(ChatSession.id).filter(
            ChatSession.workspace_id == ws.id,
            ChatSession.agent_id == agent.id,
            ChatSession.session_id == turn.session_id,
        ).first() is None
    ):
        raise NotFoundError("chat.turn_not_found", "chat turn not found",
                            {"turn_id": turn_id})
    return turn


@router.get("/chat/{agent_id}/turns/{turn_id}/events")
def turn_events(
    agent_id: str,
    turn_id: str,
    after: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> StreamingResponse:
    """Resume a console chat turn (chat-streaming §5.2): the events after ``after``
    (the SSE ``id`` last received; 0 = the whole turn), then the live ones, closing
    when the turn has ended. A ``gap {after, first_seq}`` first when some of them
    already left the buffer. 404 ``chat.turn_not_found`` once the turn is gone from
    this process (it finished a while ago, or the server restarted) — the ledger
    (``GET …/history``) then has what it saved."""
    agent = _agent_in(db, ws, agent_id, viewer)
    return _turn_response(_turn_in(db, ws, agent, turn_id), after=after)


@router.post("/chat/{agent_id}/turns/{turn_id}/stop")
def stop_turn(
    agent_id: str,
    turn_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """The stop button: end a running turn (the upstream is closed, the partial answer
    and an ``interrupted: stopped by the user`` row are saved). Answers once the
    worker recorded it, so the session's next turn is accepted at once.
    ``stopped: false`` when the turn had already ended."""
    agent = _agent_in(db, ws, agent_id, viewer)
    turn = _turn_in(db, ws, agent, turn_id)
    return {"turn_id": turn.id, "stopped": chat_turns.stop_turn(turn)}


@router.get("/chat/{agent_id}/sessions")
def list_sessions(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _agent_in(db, ws, agent_id, viewer)
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
                "tokens": {
                    "input": r.input_tokens or 0,
                    "output": r.output_tokens or 0,
                    "total": r.total_tokens or 0,
                },
                "last_at": r.last_at.isoformat() if r.last_at else None,
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                "preview": preview(r.session_id),
            }
            for r in rows
        ]
    }


def _own_session(
    db: Session, ws: WorkspaceScope, agent: Agent, session_id: str, request: Request,
) -> ChatSession:
    """The ledger row of a session the caller may read the files of: the session's own
    user or an administrator. Anyone else — and another agent's or workspace's session —
    gets the missing-session 404, indistinguishable from a session that does not exist
    (stricter than the transcript list, which every viewer of the agent sees)."""
    row = (
        db.query(ChatSession)
        .filter(
            ChatSession.workspace_id == ws.id,
            ChatSession.agent_id == agent.id,
            ChatSession.session_id == session_id,
        )
        .first()
    )
    identity = require_identity(request)
    caller = identity.username if auth_enabled() else "river"
    if row is None or not (identity.is_admin or row.actor_id == caller):
        raise NotFoundError("chat.session_not_found", "chat session not found")
    return row


@router.get("/chat/{agent_id}/sessions/{session_id}/files")
def session_files(
    agent_id: str,
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """The files the agent wrote to its session output folder (``/mnt/outputs``),
    newest first, at most 1000. ``enabled: false`` for an agent without the folder."""
    agent = _agent_in(db, ws, agent_id, viewer)
    _own_session(db, ws, agent, session_id, request)
    return agent_files.list_files(agent, session_id, ws.context)


@router.get("/chat/{agent_id}/sessions/{session_id}/files/download")
def session_file_download(
    agent_id: str,
    session_id: str,
    request: Request,
    path: str = Query(..., min_length=1, max_length=agent_files.MAX_PATH_CHARS),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """A five-minute presigned download URL for one file of the session folder."""
    agent = _agent_in(db, ws, agent_id, viewer)
    _own_session(db, ws, agent, session_id, request)
    return agent_files.presign(agent, session_id, path, ws.context)


@router.post("/chat/{agent_id}/sessions/{session_id}/stop")
def stop_session(
    agent_id: str,
    session_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """End the live AgentCore Runtime session behind a conversation.

    The ledger row is kept (its transcript stays replayable) and stamped
    `ended_at`; only the runtime session is terminated, so the next prompt starts
    a fresh session on the runtime's current version. A session already gone on
    the AWS side is a success with `already_ended: true`. Harness agents have no
    session-stop operation → 409 `chat.session_stop_unsupported`.
    """
    agent = _agent_in(db, ws, agent_id, viewer)
    row = (
        db.query(ChatSession)
        .filter(
            ChatSession.workspace_id == ws.id,
            ChatSession.agent_id == agent.id,
            ChatSession.session_id == session_id,
        )
        .first()
    )
    if row is None:
        # Another agent's or workspace's session must look like a missing one.
        raise NotFoundError("chat.session_not_found", "chat session not found")
    running = chat_turns.registry.running_for(ws.id, agent.id, session_id)
    if running is not None:
        chat_turns.stop_turn(running)  # chat-streaming §5.2: the turn no longer ends with the page
    result = stop_agent_session(agent, session_id, workspace=ws.context)
    if row.ended_at is None:
        row.ended_at = datetime.now(UTC)
        db.commit()
        db.refresh(row)  # the rendered timestamp is the stored one, as the list reads it
    return {
        "session_id": session_id,
        "ended": result["ended"],
        "already_ended": result["already_ended"],
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
    }


@router.get("/chat/{agent_id}/history")
def session_history(
    agent_id: str,
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Replay a session's thread exactly as it was rendered (int pk = order).

    Each item carries its `id`; answers also carry the caller's thumbs `verdict`
    (`up` / `down` / null), so a reloaded thread restores the thumb state."""
    _agent_in(db, ws, agent_id, viewer)
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
    rows = visible_rows(rows)  # a retried turn is shown once, as its retry
    running = chat_turns.registry.running_for(ws.id, agent_id, session_id)
    pid = current_project()
    if running is not None and pid is not None and running.project_id != pid:
        running = None
    mine = feedback_service.verdicts_for(
        db, ws.id,
        require_identity(request).username if auth_enabled() else "river",
        [r.id for r in rows if r.role == "agent"],
    )
    return {
        "messages": [
            {
                "id": r.id,
                "verdict": mine.get(r.id),
                "role": r.role,
                "text": r.text,
                "name": r.name,
                "attachments": r.attachments or [],
                "answered_by": r.answered_by,
                "at": r.created_at.isoformat() if r.created_at else None,
                # SB-1: an output_schema agent's parsed answer, as the turn streamed it
                **({"structured": structured} if structured is not None else {}),
            }
            for r, structured in fold_structured(rows)
        ],
        # chat-streaming §5.2: a turn still answering — the page shows the thread up to
        # its user row and resumes it from `GET …/turns/{turn_id}/events?after=0`
        "running_turn": (
            {"turn_id": running.id, "user_message_id": running.user_message_id}
            if running is not None and running.user_message_id is not None else None
        ),
    }


@router.get("/chat/{agent_id}/memory")
def session_memory(
    agent_id: str,
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    agent = _agent_in(db, ws, agent_id, viewer)
    # an agent that pins its own memory writes there — read it back, but only
    # from a memory this workspace manages (issue #55)
    pinned = memory_ownership.readable_spec_memory_id(db, ws.context, agent.spec)
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
            **memory_service.session_memory_summary(
                ws.context,
                mem_actor,
                session_id,
                memory_id=pinned,
            ),
            "actor_id": mem_actor,
        }
    except Exception as exc:
        if mapped_aws_error(exc):
            raise  # translatable AWS error → 4xx envelope via app.core.errors
        raise AppError(
            "memory.unavailable", f"memory lookup failed: {exc}", status_code=502
        ) from exc
