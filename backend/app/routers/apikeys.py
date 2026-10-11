"""API key management — keys are stored as sha256 hashes, never plaintext.

A key records the console principal that minted it; once that account is
offboarded the key is disabled and cannot be re-enabled (`services.api_keys`).
"""

import hashlib
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import false, true
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.models.ledger import ApiKey
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import api_keys as api_keys_service
from app.services.users import as_utc
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api", tags=["api-keys"])


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CreateKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


def _key_out(key: ApiKey, offboarded: str | None = None) -> dict[str, Any]:
    last_used = as_utc(key.last_used_at)
    return {
        "id": key.id,
        "name": key.name,
        "prefix": key.prefix,
        "enabled": key.enabled,
        "created_at": key.created_at.isoformat() if key.created_at else None,
        # NULL on keys minted before creators were recorded — "unknown".
        "created_by": key.created_by,
        "created_by_name": key.created_by_name,
        "last_used_at": last_used.isoformat() if last_used else None,
        # why the creator may no longer hold this key, or null
        "creator_offboarded": offboarded,
    }


def _own_clause(viewer: Viewer) -> Any:
    """Under workspace isolation a member manages only the keys they minted.

    Stricter than the resource rule on purpose: a key is a credential, not a
    shared resource, so a key with no recorded creator (config admin, legacy) or
    an admin's key is not the member's to list or toggle. Admins and shared
    workspaces see every key (`true()`)."""
    if not viewer.filters:
        return true()
    return ApiKey.created_by == viewer.user_id if viewer.user_id else false()


def _key_in(
    db: Session, ws: WorkspaceScope, key_id: str, viewer: Viewer = Viewer.ALL
) -> ApiKey:
    key = db.get(ApiKey, key_id)
    if key is None or key.workspace_id != ws.id:
        raise NotFoundError("apikey.not_found", "api key not found")
    if viewer.filters and (not viewer.user_id or key.created_by != viewer.user_id):
        raise NotFoundError("apikey.not_found", "api key not found")
    return key


@router.get("/apikeys")
def list_keys(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    keys = (
        db.query(ApiKey)
        .filter(ApiKey.workspace_id == ws.id, _own_clause(viewer))
        .order_by(ApiKey.created_at.desc())
        .all()
    )
    # one predicate evaluation per creator (the list is a single workspace's)
    reasons: dict[str | None, str | None] = {}
    out = []
    for key in keys:
        if key.created_by not in reasons:
            reasons[key.created_by] = api_keys_service.creator_offboarded(db, key)
        out.append(_key_out(key, reasons[key.created_by]))
    return {"keys": out}


@router.post("/apikeys", status_code=201)
def create_key(
    req: CreateKeyRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    identity = require_identity(request)
    raw = f"lp_live_{secrets.token_hex(16)}"
    # The key's workspace is what scopes it on /v1 — a key minted here reaches
    # this environment's agents only. Its creator is what keeps it alive: the
    # `users.id` when the principal has a row (the config admin and the
    # auth-disabled operator do not, so their keys are never offboarded).
    key = ApiKey(
        workspace_id=ws.id,
        name=req.name,
        prefix=raw[:12] + "…",
        key_hash=hash_key(raw),
        created_by=identity.user_id,
        created_by_name=identity.username,
    )
    db.add(key)
    db.commit()
    # The full key is returned exactly once; only the hash is at rest.
    return {**_key_out(key), "key": raw}


@router.post("/apikeys/{key_id}/disable")
def disable_key(
    key_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    key = _key_in(db, ws, key_id, viewer)
    key.enabled = False
    db.commit()
    return _key_out(key, api_keys_service.creator_offboarded(db, key))


@router.post("/apikeys/{key_id}/enable")
def enable_key(
    key_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    key = _key_in(db, ws, key_id, viewer)
    reason = api_keys_service.creator_offboarded(db, key)
    if reason is not None:
        # A member may toggle a key they can manage, but not revive one whose creator
        # was offboarded — /v1 would refuse (and re-disable) it on first use.
        raise AppError(
            "apikey.creator_offboarded",
            "this key's creator no longer has access to this workspace; mint a new key",
            {"reason": reason},
            status_code=409,
        )
    key.enabled = True
    db.commit()
    return _key_out(key)
