"""API key management — keys are stored as sha256 hashes, never plaintext.

A key records the console principal that minted it (`created_by` = `users.id`,
`created_by_name`); once that account is offboarded the key is disabled and cannot
be re-enabled (`services.api_keys`, upstream #259). CN member rule (Family B step 1):
a member lists, toggles and deletes only the keys they minted — another member's key
reads like a missing one (404 `apikey.not_found`, the kind's not-found, so a key id is
never probeable); an administrator reaches every key of the workspace.
"""

import hashlib
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.models.ledger import ApiKey
from app.routers.auth import Identity, require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import api_keys as api_keys_service
from app.services import creators
from app.services.users import as_utc

router = APIRouter(prefix="/api", tags=["api-keys"])


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CreateKeyRequest(BaseModel):
    # `extra="forbid"`: the creator is server-derived — a payload naming `created_by` /
    # `created_by_name` is refused (422), never honoured.
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    # Optional, display only (N-SCN-obs4). Keys stay workspace-wide: the label does
    # not bind the key to an agent and `/v1` ignores it.
    label: str | None = Field(default=None, max_length=120)


def _key_out(key: ApiKey, offboarded: str | None = None) -> dict[str, Any]:
    last_used = as_utc(key.last_used_at)
    return {
        "id": key.id,
        "name": key.name,
        "prefix": key.prefix,
        "enabled": key.enabled,
        # NULL on keys minted before creators were recorded by id — "unknown"
        **creators.fields(key),
        "label": key.label,
        "created_at": key.created_at.isoformat() if key.created_at else None,
        "last_used_at": last_used.isoformat() if last_used else None,
        # why the creator may no longer hold this key, or null
        "creator_offboarded": offboarded,
    }


def _key_in(db: Session, ws: WorkspaceScope, identity: Identity, key_id: str) -> ApiKey:
    """The key, but only if it lives in this workspace AND `identity` may manage it:
    a member's own key, or any key for an administrator. Everything else is 404."""
    key = db.get(ApiKey, key_id)
    if key is None or key.workspace_id != ws.id or not api_keys_service.may_manage(identity, key):
        raise NotFoundError("apikey.not_found", "api key not found")
    return key


@router.get("/apikeys")
def list_keys(
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    identity = require_identity(request)
    query = db.query(ApiKey).filter(ApiKey.workspace_id == ws.id)
    keys = (
        api_keys_service.visible_keys(query, identity)
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
    creator = creators.Creator.of_request(request)
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
        label=(req.label or "").strip() or None,
    )
    creators.stamp(key, creator)
    db.add(key)
    db.commit()
    # The full key is returned exactly once; only the hash is at rest.
    return {**_key_out(key), "key": raw}


@router.post("/apikeys/{key_id}/disable")
def disable_key(
    key_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    key = _key_in(db, ws, require_identity(request), key_id)
    key.enabled = False
    db.commit()
    return _key_out(key, api_keys_service.creator_offboarded(db, key))


@router.post("/apikeys/{key_id}/enable")
def enable_key(
    key_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    key = _key_in(db, ws, require_identity(request), key_id)
    reason = api_keys_service.creator_offboarded(db, key)
    if reason is not None:
        # An administrator may still toggle the key, but not revive one whose creator
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


@router.delete("/apikeys/{key_id}")
def delete_key(
    key_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Remove the key row for good (disable/enable keep it; offboarding never deletes —
    only the console's explicit DELETE does). Creator-or-admin like every other key
    route (N10v2-obs11, re-pointed from the username to `created_by` = `users.id`): a
    key the caller may not manage is 404. The hash is gone with the row, so a client
    still holding the plaintext is 401 on ``/v1`` from the next request."""
    key = _key_in(db, ws, require_identity(request), key_id)
    db.delete(key)
    db.commit()
    return {"deleted": True, "key_id": key_id}
