"""Memory console API — read-only views over the workspace's memory store.

Thin router over the ``MemoryBackend`` Protocol (``app.services.memory_backend``)
plus the ledger joins that turn raw identifiers into console-legible rows
(compound actor id → agent name, session id → chat-session metadata). Until N6
registers a backend every handler answers 503 ``feature.unavailable``.

**Read-only by construction, with one exception:** the only mutation here is
``DELETE /api/memory/records/{record_id}`` (N10v2-bug6), gated on ``perm:memory.manage``
and — for a member — on owning the agent the record's actor is scoped to.

**Workspace isolation (`services/visibility.py`; N17-obs531).** A member of an `isolated`
workspace reads only THEIR OWN memory: an actor id `<agent>__<human>` is theirs when its human
segment is their signed-in username (the ledger's human actor, `routers/chat._caller`); an
unscoped actor (no `__`, written outside the platform) is theirs only when it IS their username.
Every other actor — another member's, an administrator's, the legacy shared `default` — is
dropped from `GET /actors` and `GET /extraction-jobs`, and every route keyed by such an actor
or a namespace carrying it answers `404 memory.actor_not_found`, exactly like the actors of
agents isolation hides (`HiddenAgents`). Administrators and `shared` workspaces are unchanged.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import (
    AppError,
    NotFoundError,
    mapped_aws_error,
    mapped_opensearch_error,
    redacted_error,
)
from app.models.ledger import Agent, ChatMessage, ChatSession
from app.routers.auth import enabled as auth_enabled
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import memory_backend, runtime_sessions
from app.services.agent_access import HiddenAgents
from app.services.memory_backend import SCOPE_SEP
from app.services.visibility import Viewer, require_viewer

logger = logging.getLogger("launchpad.memory")

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

    Domain errors raised by the backend (and ``feature.unavailable`` from
    ``memory_backend.get_backend``) already carry a code, so they pass through
    untouched — mirrors ``routers/chat.py``. An AWS
    ``ClientError`` the console can translate (``ResourceNotFoundException`` for
    an unknown actor, ``AccessDeniedException``, …), or a self-hosted-substrate
    ``OpenSearchError`` the console can translate (N6 fix round 2 —
    throttled/unhealthy → 503, missing index/doc → 404), is left to the matching
    global handler in ``app.core.errors`` so the toast reads "not found" or
    "backend unavailable", not raw boto/OpenSearch text.
    """
    try:
        return fn(*args, **kwargs)
    except AppError:
        raise
    except Exception as exc:  # botocore ClientError, OpenSearchError, ...
        if mapped_aws_error(exc) or mapped_opensearch_error(exc):
            raise
        raise _unavailable("memory lookup failed", fn, exc) from exc


def _unavailable(message: str, fn: Any, exc: BaseException) -> AppError:
    """The 502 `memory.unavailable` fallback for a failure the console cannot translate.
    N17-obs92: the message is STATIC — `str(exc)` used to be embedded, and for an unmapped
    OpenSearch 403 / 400 that is the raw body (the IAM sentence with the instance role ARN,
    the `parsing_exception` root cause), exactly what `opensearch_error_handler` keeps out of
    every mapped envelope. The exception type travels in `detail`; the redacted line (type +
    status / error code, never a body) goes to the log."""
    logger.warning(
        "memory: %s failed: %s", getattr(fn, "__name__", "call"), redacted_error(exc)
    )
    return AppError(
        "memory.unavailable", message, {"error_type": type(exc).__name__}, status_code=502
    )


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


class _Hidden:
    """What the memory console hides from one caller: the actors of the agents workspace
    isolation hides (`HiddenAgents`) and — for a member of an isolated workspace — every
    actor that is not their own (N17-obs531: `own_human` is the caller's username; `None`
    for an administrator, a shared workspace or the open console, where nothing is
    hidden on that axis). Falsy when nothing is hidden, so callers skip all filtering."""

    def __init__(self, agents: HiddenAgents, own_human: str | None):
        self.agents = agents
        self.own_human = own_human

    def __bool__(self) -> bool:
        return bool(self.agents) or self.own_human is not None

    def _foreign(self, actor_id: str) -> bool:
        """A scoped actor of another human, or an unscoped actor that is not the caller's
        username (`decode_actor`: the human segment of an unscoped id is the id itself)."""
        return (
            self.own_human is not None
            and memory_backend.decode_actor(actor_id)["human_actor"] != self.own_human
        )

    def hides_actor(self, actor_id: str | None) -> bool:
        if not actor_id:
            return False
        return self.agents.hides_actor(actor_id) or self._foreign(actor_id)

    def hides_namespace(self, namespace: str | None) -> bool:
        """A long-term namespace (`/facts/<actor>`, `/summaries/<actor>/<session>`, …: the
        catalog's `{actorId}` is substituted as one whole segment) that carries a hidden
        agent's actor, or — for a scoped member — one that carries NO segment that is their
        own actor. The strategy words (`facts`, `summaries`, …) carry no `__`, so an unscoped
        foreign actor (`/facts/default`) cannot be told from one by shape: the member must
        name their own actor somewhere, which is exactly what their resolved namespaces do."""
        if not namespace:
            return False
        if self.agents.hides_namespace(namespace):
            return True
        if self.own_human is None:
            return False
        parts = namespace.split("/")
        if any(SCOPE_SEP in part and self._foreign(part) for part in parts):
            return True
        return not any(part and not self._foreign(part) for part in parts)

    def hides_session(self, session_id: str | None) -> bool:
        return self.agents.hides_session(session_id)


def _hidden(db: Session, ws: WorkspaceScope, viewer: Viewer, request: Request) -> _Hidden:
    """What the caller may not read (empty, no query, unless the viewer filters): the
    actors of hidden agents, plus — isolated workspace, member — every actor whose human
    segment is not the caller's own. Dropped from lists; every read keyed by such an actor
    — or a namespace carrying it — answers 404."""
    own_human = None
    if viewer.filters:
        # `filters` ⇒ a registered non-admin ⇒ the gate is on; the ledger's human actor is
        # the username (`routers/chat._caller`, `session_visibility`)
        identity = require_identity(request)
        own_human = identity.username if auth_enabled() else None
    return _Hidden(HiddenAgents(db, viewer, ws.id), own_human)


def _require_actor_visible(hidden: _Hidden, actor_id: str | None) -> None:
    if hidden.hides_actor(actor_id):
        raise NotFoundError("memory.actor_not_found", "memory actor not found")


def _require_namespace_visible(hidden: _Hidden, namespace: str | None) -> None:
    if hidden.hides_namespace(namespace):
        raise NotFoundError("memory.actor_not_found", "memory actor not found")


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
    candidates = _guard(memory_backend.get_backend().resolve_namespaces, ws.context, actor_id)
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
    return _guard(memory_backend.get_backend().memory_overview, ws.context)


@router.get("/actors")
def actors(
    request: Request,
    next_token: str | None = None,
    max_results: int | None = Query(default=None, ge=1, le=100),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    page = _guard(memory_backend.get_backend().list_actors, ws.context, next_token, max_results)
    hidden = _hidden(db, ws, viewer, request)
    if hidden:
        page["items"] = [a for a in page["items"] if not hidden.hides_actor(a["actor_id"])]
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
    request: Request,
    actor_id: str = Query(min_length=1),
    next_token: str | None = None,
    max_results: int | None = Query(default=None, ge=1, le=100),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    hidden = _hidden(db, ws, viewer, request)
    _require_actor_visible(hidden, actor_id)
    page = _guard(
        memory_backend.get_backend().list_sessions, ws.context, actor_id, next_token, max_results
    )
    if hidden:
        # an actor's sessions may be attributed by the ledger to a hidden agent (chat /
        # eval run): drop those

        page["items"] = [
            s for s in page["items"] if not hidden.hides_session(s.get("session_id"))
        ]
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
    # per-user policy: a derived per-principal runtime session (`lpp-…`) is one principal's
    # part of a console conversation — it carries that conversation's ledger entry and names
    # it (`conversation_session_id`, additive)
    derived = runtime_sessions.conversation_of(db, ws.id, session_ids)
    if derived:
        missing = sorted(set(derived.values()) - set(ledger))
        if missing:
            more_counts = dict(
                db.query(ChatMessage.session_id, func.count(ChatMessage.id))
                .filter(ChatMessage.workspace_id == ws.id, ChatMessage.session_id.in_(missing))
                .group_by(ChatMessage.session_id)
                .all()
            )
            for chat_session, agent_name in (
                db.query(ChatSession, Agent.name)
                .outerjoin(Agent, Agent.id == ChatSession.agent_id)
                .filter(ChatSession.workspace_id == ws.id, ChatSession.session_id.in_(missing))
                .all()
            ):
                ledger[chat_session.session_id] = {
                    "agent_id": chat_session.agent_id, "agent_name": agent_name,
                    "human_actor": chat_session.actor_id, "turns": chat_session.turns,
                    "message_count": more_counts.get(chat_session.session_id, 0),
                }
    for item in page["items"]:
        # None for sessions the console never wrote (eval runs, /v1 API callers).
        conversation = derived.get(item["session_id"])
        item["ledger"] = ledger.get(conversation or item["session_id"])
        if conversation:
            item["conversation_session_id"] = conversation
    return page


@router.get("/events")
def events(
    request: Request,
    actor_id: str = Query(min_length=1),
    session_id: str = Query(min_length=1),
    include_payloads: bool = True,
    next_token: str | None = None,
    max_results: int | None = Query(default=None, ge=1, le=100),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    hidden = _hidden(db, ws, viewer, request)
    _require_actor_visible(hidden, actor_id)
    if hidden.hides_session(session_id):
        raise NotFoundError("memory.actor_not_found", "memory actor not found")
    return _guard(
        memory_backend.get_backend().list_events,
        ws.context,
        actor_id,
        session_id,
        include_payloads,
        next_token,
        max_results,
    )


@router.get("/namespaces")
def namespaces(
    request: Request,
    actor_id: str = Query(min_length=1),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_actor_visible(_hidden(db, ws, viewer, request), actor_id)
    return {"items": _guard(memory_backend.get_backend().resolve_namespaces, ws.context, actor_id)}


@router.get("/records")
def records(
    request: Request,
    actor_id: str | None = None,
    strategy_id: str | None = None,
    namespace: str | None = None,
    next_token: str | None = None,
    max_results: int | None = Query(default=None, ge=1, le=100),
    include_superseded: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Long-term records of one namespace. r14-a (N14-obs1): records the merge strategy
    superseded, soft-deleted or expired are hidden by default — ``include_superseded=true``
    lists them too (each row carries ``active`` and the op-history fields)."""
    hidden = _hidden(db, ws, viewer, request)
    _require_actor_visible(hidden, actor_id)
    _require_namespace_visible(hidden, namespace)
    resolved = _resolve_namespace(ws, namespace, actor_id, strategy_id)
    _require_namespace_visible(hidden, resolved)
    return _guard(
        memory_backend.get_backend().list_records,
        ws.context,
        resolved,
        strategy_id,
        next_token,
        max_results,
        include_superseded=include_superseded,
    )


@router.post("/records/search")
def search_records(
    req: SearchRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    hidden = _hidden(db, ws, viewer, request)
    _require_actor_visible(hidden, req.actor_id)
    _require_namespace_visible(hidden, req.namespace)
    resolved = _resolve_namespace(ws, req.namespace, req.actor_id, req.strategy_id)
    _require_namespace_visible(hidden, resolved)
    return _guard(
        memory_backend.get_backend().search_records,
        ws.context,
        resolved,
        req.query,
        req.top_k,
        req.strategy_id,
    )


def _require_record_owner(db: Session, ws: WorkspaceScope, request: Request, actor_id: str) -> None:
    """N10v2-bug6 scoping: an administrator may delete any record of the workspace; a
    member only one whose actor is scoped to an agent of this workspace that THEY
    created (``agents.created_by`` = their ``users.id`` — Family B step 1 re-pointed this
    from the retired ``Agent.owner`` username). An unscoped actor (written outside the
    platform) and an agent with no recorded creator have no owner → admin only."""
    identity = require_identity(request)
    if identity.is_admin:
        return
    agent_id = memory_backend.decode_actor(actor_id)["agent_id"]
    created_by = None
    if agent_id:
        created_by = (
            db.query(Agent.created_by)
            .filter(Agent.workspace_id == ws.id, Agent.id == agent_id)
            .scalar()
        )
    if created_by is None or identity.user_id is None or created_by != identity.user_id:
        raise AppError(
            "memory.record_forbidden",
            "only an administrator or the creator of the agent this memory belongs to may "
            "delete its records",
            {"actor_id": actor_id, "agent_id": agent_id},
            status_code=403,
        )


@router.delete("/records/{record_id}")
def delete_record(
    record_id: str,
    request: Request,
    actor_id: str = Query(min_length=1),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Delete one long-term record (N10v2-bug6). ``actor_id`` is mandatory: the backend
    confirms the record belongs to that actor AND this workspace's store before deleting,
    so a record id alone never identifies — or removes — another actor's memory."""
    _require_actor_visible(_hidden(db, ws, viewer, request), actor_id)  # hidden → 404 before 403
    _require_record_owner(db, ws, request, actor_id)
    return _guard(memory_backend.get_backend().delete_record, ws.context, record_id, actor_id)


@router.get("/extraction-jobs")
def extraction_jobs(
    request: Request,
    actor_id: str | None = None,
    session_id: str | None = None,
    strategy_id: str | None = None,
    status: str | None = None,
    next_token: str | None = None,
    max_results: int | None = Query(default=None, ge=1, le=100),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    hidden = _hidden(db, ws, viewer, request)
    _require_actor_visible(hidden, actor_id)
    page = _guard(
        memory_backend.get_backend().list_extraction_jobs,
        ws.context,
        actor_id,
        session_id,
        strategy_id,
        status,
        next_token,
        max_results,
    )
    if hidden:
        page["items"] = [
            j for j in page["items"] if not hidden.hides_actor(j.get("actor_id"))
        ]
    return page
