"""Chat playground endpoints — SSE streaming over the shared invoke chain."""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import StreamingResponse
from pydantic import ConfigDict
from sqlalchemy import exists
from sqlalchemy.orm import Session

from app.assistant.sessions import refuse_assistant_session
from app.core.db import SQLITE_MAX_INT, get_db
from app.core.errors import AppError, NotFoundError, mapped_aws_error
from app.models.ledger import Agent, ChatMessage, ChatSession
from app.routers.auth import Identity, require_identity
from app.routers.auth import enabled as auth_enabled
from app.routers.memory import _unavailable
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.attachments import AttachmentRequest
from app.services import (
    agent_access,
    chat_turns,
    creators,
    live_turns,
    memory_backend,
    memory_ownership,
    runtime_sessions,
    session_visibility,
)
from app.services.agentcore.runtime import new_session_id
from app.services.attachments import prepare_attachments
from app.services.chat import chat_stream
from app.services.chat_ledger import save_message, track_session
from app.services.invoke import (
    stop_agent_session,
    validate_session_id,
    validate_session_id_syntax,
)
from app.services.policy_entities import Principal
from app.services.runtime_discovery import require_invoke_capability
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(AttachmentRequest):
    # N16-obs78: the console sends exactly {prompt, attachments?, session_id?}; an unknown
    # key (a typo, `created_by`, …) is 422 `validation.invalid_request` naming it
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None


def _caller(request: Request) -> tuple[Identity, str]:
    """The signed-in identity and the ledger's human actor for it (``"river"`` when the
    gate is off, as every page assumes)."""
    identity = require_identity(request)
    return identity, (identity.username if auth_enabled() else "river")


def _session_for(
    request: Request,
    db: Session,
    ws: WorkspaceScope,
    agent_id: str,
    session_id: str,
    *,
    required: bool = False,
) -> ChatSession | None:
    """THE session resolver of every by-id chat route (N16-obs70 / N16-obs71): the ledger
    row of ``session_id`` under this agent and workspace, which the caller may act on —
    their own (``session_visibility.may_list``), or any as an administrator — in BOTH
    visibility modes. Another member's session, another agent's or workspace's, and an
    unknown id all answer ``404 chat.session_not_found`` (never 403: ids are not
    probeable). ``required=False`` lets a row-less id through as ``None`` — a session whose
    first turn is still running, or an empty history — which stays open to whoever holds
    the id, as before. An id that could never name a runtime session (N16-obs72:
    ``validate_session_id_syntax``) is 422 before the ledger is read."""
    validate_session_id_syntax(session_id)
    row = chat_turns.session_row(db, ws.id, agent_id, session_id)
    if row is None:
        if required:
            raise NotFoundError("chat.session_not_found", "chat session not found")
        return None
    identity, human_actor = _caller(request)
    return session_visibility.require_own(identity, human_actor, row)


def _agent_in(
    db: Session, ws: WorkspaceScope, agent_id: str, viewer: Viewer = Viewer.ALL
) -> Agent:
    """The agent, or 404 — including when it belongs to another workspace or is hidden
    by workspace isolation, which the caller must not be able to tell apart from a
    missing one, and when it has been deleted (N10v2-bug3: ``/v1`` already answered 404
    for the same row; the console entrance used to say 409 ``agent.not_active``). The
    404 lands before the D20 policy gate, so a denial never discloses a hidden agent."""
    agent = agent_access.agent_in(db, ws.id, agent_id, viewer=viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    return agent


def _get_active_agent(
    db: Session, ws: WorkspaceScope, agent_id: str, viewer: Viewer = Viewer.ALL
) -> Agent:
    agent = _agent_in(db, ws, agent_id, viewer)
    require_invoke_capability(agent)
    return agent


# The transcript ledger is shared with the public /v1 entrance
# (services/chat_ledger.py); the console keeps its local names.
_save_message = save_message
_track_session = track_session


@router.post("/chat/{agent_id}")
def chat(
    agent_id: str,
    req: ChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> StreamingResponse:
    validate_session_id(req.session_id)  # N-SCN-obs16: 422 before the gate and any AWS call
    agent = _agent_in(db, ws, agent_id, viewer)  # hidden / deleted / foreign → 404
    # An assistant-owned session of the preset is private to the assistant page.
    refuse_assistant_session(agent, req.session_id)
    # N16-obs71: a supplied id continues a session only when it is the caller's (or the
    # caller is an administrator) — another member's idle session is 404, so no turn is
    # ever appended into it; an id with no row yet starts / continues it as before.
    # N17-obs12 (ownership-first ruling, api.md "Refusal precedence"): the session's ownership
    # 404 answers BEFORE the agent-status 409 below, so a non-owner never learns that the
    # agent is deploying; the owner's send on a deploying agent is still 409 agent.not_active.
    existing = (
        _session_for(request, db, ws, agent.id, req.session_id) if req.session_id else None
    )
    require_invoke_capability(agent)  # 409 agent.not_active while deploying / failed
    prepared = prepare_attachments(
        agent, req.attachments, prompt=req.prompt, session_id=req.session_id,
    )
    identity, human_actor = _caller(request)

    # Memory partitions per agent: the runtime writes short-term events and the
    # extractor lands long-term records under this compound actor. The ledger
    # still records the bare authenticated username. A continuing session keeps
    # its original Memory owner.
    actor_id = (existing.actor_id if existing is not None and existing.actor_id
                else human_actor)
    mem_actor = memory_backend.scoped_actor(agent.id, actor_id)
    # N-ARCH-obs15 v2: the session id is fixed before the turn starts so the detached
    # turn has its key (``chat_stream`` would have generated the same kind of id at
    # ``meta``); a supplied id was validated above. One running turn per session.
    session_id = req.session_id or new_session_id()
    chat_turns.require_turn_capacity(db, ws.id, agent.id, session_id)
    # N16-obs79: a turn under an ENDED session id is 409 chat.session_ended, never a re-open
    chat_turns.require_session_open(db, ws.id, agent.id, session_id)

    # The stream outlives the request scope, so it carries the plain id and the
    # already-built context rather than reaching back for the resolved scope.
    workspace_id = ws.id
    workspace = ws.context

    stream_kwargs: dict[str, Any] = {}
    if prepared:
        stream_kwargs["attachments"] = prepared
    stream_kwargs["runtime_user_id"] = identity.username
    # D20: the signed-in console user is the policy principal (Launchpad::User)
    stream_kwargs["principal"] = Principal.from_identity(identity)

    def open_stream(sid: str):
        return chat_stream(
            agent,
            req.prompt,
            session_id=sid,
            actor_id=mem_actor,
            workspace=workspace,
            **stream_kwargs,
        )

    # Thread items are persisted in event order (int-pk = replay order) so the
    # playground can restore a session's history exactly as rendered — by the same
    # reducer on the driver thread, whatever happens to this response.
    recorder = chat_turns.TranscriptRecorder(
        workspace_id, agent.id, prompt=req.prompt, actor_id=actor_id,
        attachments=prepared.metadata if prepared else None, runtime_version=agent.version,
        save=_save_message, track=_track_session,
        # a NEW session records who opened it (CN extra: members list their own sessions)
        creator=creators.Creator.of(identity),
    )
    # ``username`` = the turn's SENDER in the ledger's human-actor convention ("river"
    # when the gate is off, as the pages assume): the reattach ownership of this turn,
    # distinct from the session's first owner (``actor_id``) on a shared session.
    live = chat_turns.start_turn(
        agent.id, workspace_id, session_id, actor_id=actor_id, username=human_actor,
        stream=open_stream, recorder=recorder,
    )
    return live_turns.TurnStreamResponse(
        live_turns.sse_body(live.subscribe(0)),
        media_type="text/event-stream",
        headers=live_turns.SSE_HEADERS,
    )


@router.get("/chat/{agent_id}/sessions/{session_id}/turns/{turn}/stream")
def turn_stream(
    agent_id: str,
    session_id: str,
    # N16-obs86: bounded like every integer parameter (r12-h) — a value past SQLite's range
    # used to reach the event-log query as a Python int and raise OverflowError (500)
    turn: Annotated[int, PathParam(ge=0, le=SQLITE_MAX_INT)],
    request: Request,
    from_seq: Annotated[str | None, Query(
        alias="from",
        description="Replay the persisted events with seq > from (default 0); an integer in "
                    "0..2^63-1, else 422 chat.invalid_seq",
    )] = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> StreamingResponse:
    """Reattach to a console turn (N-ARCH-obs15 v2): replays the persisted events with
    ``seq > from`` (``?from=<seq>``, default 0), then tails the turn while it runs, then
    its terminal event (``done`` / ``error``). ``404 chat.session_not_found`` for a session
    of another agent or workspace, for an unknown id, and — N16-obs71, both visibility
    modes — for another member's session (the session is resolved through ``_session_for``
    BEFORE any turn is looked at, so neither its existence nor its author leaks);
    ``404 chat.turn_not_found`` for a turn the session never reached; ``409
    chat.turn_not_yours {author}`` is reachable only on a session the caller may read
    (their own, or any as an administrator) when THAT turn was sent by a different account
    — an administrator's turn in a member's session, or a member's turn for the
    administrator (ownership is the turn's sender, not the session's first owner);
    ``422 chat.invalid_seq`` for a ``from`` that is not an integer in ``0..2^63-1``
    (N16-obs86), ``422 validation.invalid_request`` for a ``turn`` past that range."""
    agent = _agent_in(db, ws, agent_id, viewer)
    row = _session_for(request, db, ws, agent.id, session_id, required=True)
    assert row is not None  # ``required`` — for the type checker
    _, human_actor = _caller(request)
    chat_turns.require_turn(db, row, turn, human_actor)
    after = live_turns.parse_from(from_seq, "chat.invalid_seq")
    return live_turns.TurnStreamResponse(
        live_turns.sse_body(chat_turns.stream_turn(ws.id, agent.id, session_id, turn, after)),
        media_type="text/event-stream",
        headers=live_turns.SSE_HEADERS,
    )


def own_sessions_clause(identity, human_actor: str):
    """The CN member rule for the session list (Family B step 1, user decision): a
    member sees the sessions THEY opened — `created_by` = their `users.id`, or, for a
    row older than the column, one whose first actor is their username — an
    administrator every session. Returns a SQLAlchemy predicate, or None (no filter).
    One definition: `services/session_visibility.py` (step 2 reuses it by id)."""
    return session_visibility.own_clause(identity, human_actor)


@router.get("/chat/{agent_id}/sessions")
def list_sessions(
    agent_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _agent_in(db, ws, agent_id, viewer)
    identity = require_identity(request)
    human_actor = identity.username if auth_enabled() else "river"
    # Only sessions with a replayable transcript: rows that predate the
    # ChatMessage ledger have nothing to open (clicking them showed an empty
    # thread with an id-only preview), so they are filtered out here.
    # The correlated exists matches on session_id alone, so it carries the
    # workspace predicate itself — a bare id match can cross environments.
    query = db.query(ChatSession).filter(
        ChatSession.workspace_id == ws.id,
        ChatSession.agent_id == agent_id,
        exists().where(
            ChatMessage.session_id == ChatSession.session_id,
            ChatMessage.workspace_id == ws.id,
        ),
    )
    own = own_sessions_clause(identity, human_actor)
    if own is not None:
        query = query.filter(own)
    rows = query.order_by(ChatSession.last_at.desc()).limit(50).all()

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
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                "preview": preview(r.session_id),
                # console | public_api (N-SCN-obs1); rows older than the column are console
                "source": r.source or "console",
                # N-ARCH-obs15 v2: the console turn still running server-side, else null
                "live_turn": chat_turns.live_turn_view(db, r),
                # who opened the session (CN extra); nulls on rows older than the column
                **creators.fields(r),
            }
            for r in rows
        ]
    }


@router.post("/chat/{agent_id}/sessions/{session_id}/stop")
def stop_session(
    agent_id: str,
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """End the live AgentCore Runtime session behind a conversation.

    The ledger row is kept (its transcript stays replayable) and stamped
    `ended_at`; only the runtime session is terminated, so the next prompt starts
    a fresh session on the runtime's current version. A session already gone on
    the AWS side is a success with `already_ended: true`. Harness agents have no
    session-stop operation → 409 `chat.session_stop_unsupported`. Another agent's,
    another workspace's and — N16-obs70, in BOTH visibility modes — another member's
    session look like a missing one (`_session_for`): a member ends only the sessions
    they opened, an administrator any.
    """
    agent = _agent_in(db, ws, agent_id, viewer)
    row = _session_for(request, db, ws, agent.id, session_id, required=True)
    assert row is not None  # ``required`` — for the type checker
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


def _tool_call_fields(tool_call: dict[str, Any] | None) -> dict[str, Any]:
    """The additive history keys of a ``tool`` row's bounded call summary (N16-obs85):
    ``tool_input`` (≤ 512 chars), ``tool_result_summary`` (≤ 256 chars),
    ``tool_result_status`` — each only when the runtime streamed it."""
    if not tool_call:
        return {}
    out: dict[str, Any] = {}
    if tool_call.get("input") is not None:
        out["tool_input"] = str(tool_call["input"])
    if tool_call.get("result_summary") is not None:
        out["tool_result_summary"] = str(tool_call["result_summary"])
    if tool_call.get("status"):
        out["tool_result_status"] = str(tool_call["status"])
    if isinstance(tool_call.get("policy"), dict):
        # A6: a `policy` row's deny-card facts (policy id / name, stage, source, decision id)
        out["policy"] = {k: str(v) for k, v in tool_call["policy"].items() if v}
    return out


@router.get("/chat/{agent_id}/history")
def session_history(
    agent_id: str,
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Replay a session's thread exactly as it was rendered (int pk = order). N-ARCH-obs15
    v2: ``live_turn`` (additive) names the console turn still running server-side —
    ``{turn, seq, started_at, state, author}`` — so a page load reattaches to it."""
    _agent_in(db, ws, agent_id, viewer)
    # own session or administrator, both modes; a row-less id reads as an empty thread
    session = _session_for(request, db, ws, agent_id, session_id)
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
                "attachments": r.attachments or [],
                "at": r.created_at.isoformat() if r.created_at else None,
                # N-SCN-bug4: additive; only on an agent row the runtime closed at the
                # model's token limit
                **({"truncated": True} if r.truncated else {}),
                # N16-obs85: additive; only on a tool row whose runtime streamed a summary
                **_tool_call_fields(r.tool_call),
            }
            for r in rows
        ],
        "live_turn": chat_turns.live_turn_view(db, session) if session is not None else None,
    }


def _merge_memory_summaries(first: dict[str, Any],
                            more: list[dict[str, Any]]) -> dict[str, Any]:
    """One rail over a conversation's runtime sessions: event counts summed, records deduped
    in order (the actor-wide facts / preferences repeat in every session's summary)."""
    out = dict(first)
    count = int(first.get("event_count") or 0)
    records = list(first.get("records") or [])
    seen = {(r.get("namespace"), r.get("text")) for r in records}
    for extra in more:
        count += int(extra.get("event_count") or 0)
        for record in extra.get("records") or []:
            key = (record.get("namespace"), record.get("text"))
            if key not in seen:
                seen.add(key)
                records.append(record)
    out["event_count"], out["records"] = count, records
    return out


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
    # N16-obs71: the memory rail of a session the member could not list is 404 in BOTH
    # modes (it used to echo the owner's actor under `shared`); a row-less id stays open
    session = _session_for(request, db, ws, agent.id, session_id)
    # an agent that pins its own memory writes there — read it back, but only
    # from a memory this workspace manages (issue #55)
    pinned = memory_ownership.readable_spec_memory_id(db, ws.context, agent.spec)
    try:
        # Read back the same agent-scoped partition the chat write path uses.
        _, human_actor = _caller(request)
        session_actor = (session.actor_id if session is not None and session.actor_id
                         else human_actor)
        mem_actor = memory_backend.scoped_actor(agent_id, session_actor)
        summary = memory_backend.get_backend().session_memory_summary(
            ws.context, mem_actor, session_id, memory_id=pinned,
        )
        # per-user policy: the conversation's turns may have run in derived per-principal
        # runtime sessions (`lpp-…`), whose events and summaries are part of it
        derived = runtime_sessions.runtime_sessions_for(db, ws.id, session_id, viewer,
                                                        agent_id=agent.id)
        if derived:
            summary = _merge_memory_summaries(summary, [
                memory_backend.get_backend().session_memory_summary(
                    ws.context, mem_actor, extra, memory_id=pinned)
                for extra in derived
            ])
        # Echo the compound actor so the rail can deep-link into the Memory
        # console at the exact partition it just summarised — the console keys
        # on this id, and re-deriving it in the frontend would fork the scoping
        # rule (a session may have recorded a different human actor entirely).
        return {**summary, "actor_id": mem_actor}
    except AppError:
        raise  # feature.unavailable / backend domain errors keep their own code
    except Exception as exc:
        if mapped_aws_error(exc):
            raise  # translatable AWS error → 4xx envelope via app.core.errors
        # N17-obs92: a static message — never str(exc), which may be a raw OpenSearch body
        raise _unavailable(
            "memory lookup failed", memory_backend.get_backend().session_memory_summary, exc
        ) from exc
