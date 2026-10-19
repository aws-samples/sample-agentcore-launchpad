"""COA context layer (`/api/context-layer`): connect a COA deployment, a member's
own authorization, the namespaces COA lets that member see (with their sources,
governed metrics and ontology), COA's health, and the streaming playground.

Thin HTTP shell over `services.context_layer` (connect/status/disconnect),
`services.context_layer_tokens` (the member's COA token),
`services.context_layer_api` (COA REST reads, as the member) and
`services.context_layer_playground` (the Serve runtime proxy). No COA token, client
secret or authorization session uri ever appears in a response; an authorization
URL goes only to the member it was minted for.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import CONNECTION_NAME_RE
from app.services import (
    agent_access,
    context_layer,
    context_layer_api,
    context_layer_playground,
    oauth_sessions,
)
from app.services import context_layer_tokens as tokens
from app.services.creators import Creator
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api/context-layer", tags=["context-layer"])


class ConnectRequest(BaseModel):
    prefix: str = Field(min_length=2, max_length=31, pattern=context_layer.PREFIX_RE.pattern)
    env: str = Field(
        default="dev", min_length=1, max_length=16, pattern=context_layer.ENV_RE.pattern
    )
    region: str = Field(min_length=9, max_length=32, pattern=context_layer.REGION_RE.pattern)
    # adopt an existing OAuth2 Connection; omitted ⇒ create (or re-adopt) coa-<prefix>
    connection: str | None = Field(default=None, pattern=CONNECTION_NAME_RE)


NAMESPACE_PATTERN = context_layer_api.NAMESPACE_ID_RE.pattern
NamespaceId = Annotated[
    str, Path(pattern=NAMESPACE_PATTERN, description="a COA namespace id (UUID)")
]


class PlaygroundRequest(BaseModel):
    namespace_id: str = Field(pattern=NAMESPACE_PATTERN)
    query: str = Field(min_length=1, max_length=4000)
    # COA's conversation id from the previous turn's ``done`` (omitted = a new one)
    session_id: str | None = Field(
        default=None, pattern=context_layer_playground.SESSION_ID_RE.pattern
    )
    mode: Literal["standard", "deep-reasoning"] | None = None
    strategy: Literal[
        "best", "ontop", "nl_to_sql", "ontop_first", "nl_to_sql_first", "deep-reasoning"
    ] | None = None


def _username(request: Request) -> str:
    return getattr(require_identity(request), "username", "") or ""


def _mask(refs: list[dict[str, Any]], hidden: set[str]) -> list[dict[str, Any]]:
    """The referencing agents the caller may see (the count stays truthful)."""
    return [
        ref if ref.get("id") not in hidden else {**ref, "id": None, "name": None}
        for ref in refs
    ]


@router.get("")
def get_status(
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    identity = require_identity(request)
    out = context_layer.status(
        db, ws.context, username=identity.username, is_admin=identity.is_admin
    )
    if out.get("referenced_by"):
        out["referenced_by"] = _mask(
            out["referenced_by"], agent_access.hidden_agent_ids(db, viewer, ws.id)
        )
    return out


@router.put("")
def connect(
    req: ConnectRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    identity = require_identity(request)
    context_layer.connect(
        db,
        ws.context,
        prefix=req.prefix,
        env=req.env,
        region=req.region,
        connection=req.connection,
        return_url=get_settings().resolved_oauth_return_url(),
        creator=Creator.of_request(request),
    )
    return context_layer.status(db, ws.context, username=identity.username, is_admin=True)


@router.delete("")
def disconnect(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    try:
        return context_layer.disconnect(db, ws.context)
    except AppError as exc:
        # the guard counts every agent; the 409 names only the visible ones
        if exc.code == "context_layer.referenced" and isinstance(exc.detail, dict):
            exc.detail["referenced_by"] = _mask(
                exc.detail.get("referenced_by") or [],
                agent_access.hidden_agent_ids(db, viewer, ws.id),
            )
        raise


@router.post("/authorize")
def authorize(
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Start (or confirm) the caller's consent: ``{authorized: true}`` when the vault
    already holds their COA token, else the single-use authorization URL."""
    grant = tokens.user_token(db, ws.context, _username(request))
    if isinstance(grant, tokens.AuthNeeded):
        return {
            "authorized": False,
            "authorization_url": grant.url,
            "connection": grant.layer.connection,
        }
    return {"authorized": True, "connection": grant.layer.connection}


@router.post("/revoke")
def revoke(
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Force re-consent: the next COA call through the Connection — the console's and
    every agent's — asks this member again (``user_token_revocations``)."""
    layer = context_layer.require(db, ws.id)
    username = _username(request)
    result = oauth_sessions.revoke(db, ws.id, user_id=username, provider=layer.connection)
    tokens.forget(ws.id, username)
    return result


@router.get("/namespaces")
def list_namespaces(
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """The namespaces COA shows this member (COA ``GET /namespaces`` as them)."""
    items = context_layer_api.call_as_member(
        db, ws.context, _username(request), context_layer_api.list_namespaces
    )
    return {"namespaces": items}


@router.get("/health")
def health(
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Both COA runtimes' status (IAM) and COA's ``/system-health`` (as the caller)."""
    return context_layer_api.health(db, ws.context, _username(request))


@router.get("/namespaces/{namespace_id}")
def get_namespace(
    request: Request,
    namespace_id: NamespaceId,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """COA ``GET /namespaces/{id}`` as the caller."""
    namespace = context_layer_api.call_as_member(
        db, ws.context, _username(request),
        lambda layer, token: context_layer_api.get_namespace(layer, token, namespace_id),
    )
    return {"namespace": namespace}


@router.get("/namespaces/{namespace_id}/sources")
def list_sources(
    request: Request,
    namespace_id: NamespaceId,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """COA ``GET /namespaces/{id}/sources`` as the caller (every source type)."""
    sources = context_layer_api.call_as_member(
        db, ws.context, _username(request),
        lambda layer, token: context_layer_api.list_sources(layer, token, namespace_id),
    )
    return {"namespace_id": namespace_id, "sources": sources}


@router.get("/namespaces/{namespace_id}/metrics")
def list_metrics(
    request: Request,
    namespace_id: NamespaceId,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """COA ``GET /namespaces/{id}/metrics`` as the caller: the governed metrics."""
    metrics = context_layer_api.call_as_member(
        db, ws.context, _username(request),
        lambda layer, token: context_layer_api.list_metrics(layer, token, namespace_id),
    )
    return {"namespace_id": namespace_id, "metrics": metrics}


@router.get("/namespaces/{namespace_id}/ontology")
def get_ontology(
    request: Request,
    namespace_id: NamespaceId,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """COA ``GET …/ontologies`` + one ``GET …/graph/ontology-overview`` per ontology,
    as the caller: ``{ontologies, classes, failed, truncated}``."""
    out = context_layer_api.call_as_member(
        db, ws.context, _username(request),
        lambda layer, token: context_layer_api.ontology(layer, token, namespace_id),
    )
    return {"namespace_id": namespace_id, **out}


@router.post("/playground")
def playground(
    req: PlaygroundRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> StreamingResponse:
    """Ask COA one question (Serve runtime, ``stream: true``) as the caller; SSE.

    The token is fetched before the stream opens, so "not connected" or a vault
    failure answer as an ordinary error response. Missing consent is one
    ``auth_required`` event (the Chat card's shape); COA's own refusals and outages
    are ``error`` events. FREE in the billing table: COA's spend is in COA's account,
    and Launchpad does not meter it."""
    username = _username(request)
    grant = tokens.user_token(db, ws.context, username)
    if isinstance(grant, tokens.AuthNeeded):
        events = context_layer_playground.auth_required(grant)
    else:
        events = context_layer_playground.stream(
            layer=grant.layer,
            token=grant.value,
            workspace_id=ws.id,
            username=username,
            namespace_id=req.namespace_id,
            query=req.query,
            session_id=req.session_id,
            mode=req.mode,
            strategy=req.strategy,
            is_disconnected=request.is_disconnected,
        )
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
