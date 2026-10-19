"""COA context layer (`/api/context-layer`): connect a COA deployment, a member's
own authorization, and the namespaces COA lets that member see.

Thin HTTP shell over `services.context_layer` (connect/status/disconnect),
`services.context_layer_tokens` (the member's COA token) and
`services.context_layer_api` (COA REST reads). No COA token, client secret or
authorization session uri ever appears in a response; an authorization URL goes
only to the member it was minted for.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import CONNECTION_NAME_RE
from app.services import agent_access, context_layer, context_layer_api, oauth_sessions
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
