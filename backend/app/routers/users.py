"""Admin-only console user management (registered accounts + statistics).

Every route is `ADMIN` in `core/route_policy.ROUTE_POLICY`, enforced by the app-level
`enforce_route_policy` dependency before any handler runs: an administrator identity is the
built-in admin, an admin-role registered account, or — when the console gate is disabled
entirely — the implicit local operator. The handlers do not repeat the check (N17-obs284).
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SQLITE_MAX_INT, get_db
from app.core.errors import AppError
from app.routers.auth import require_identity
from app.services import users as users_service

router = APIRouter(prefix="/api/users", tags=["users"])


class UserPatch(BaseModel):
    """Partial admin update. Only the keys actually sent are applied, so
    `{"expires_at": null}` means "never expires" while omitting it keeps the
    current validity. Unknown keys are 422 (N13-obs9): a mistyped field such as
    `expire_at` used to be silently ignored, so the admin saw 200 and no change."""

    model_config = ConfigDict(extra="forbid")

    status: str | None = None
    role: str | None = None
    extend_days: int | None = Field(default=None, ge=1, le=3650)
    expires_at: datetime | None = None
    password: str | None = Field(default=None, max_length=256)
    # {permission_key: bool}; unsent keys stay granted, null resets to
    # all-granted. Strict so "no"/"0" cannot coerce into a denial.
    permissions: dict[str, StrictBool] | None = None
    # Full replacement of the account's workspace grants (null clears them) —
    # the per-user shape. `PUT /api/workspaces/{id}/grants` is the workspace-side
    # bulk one; both write only `user_workspaces`.
    workspaces: list[str] | None = None
    # CN extra (Family B step 1): the approval dialog grants workspaces in the SAME
    # call — `{"status": "active", "workspace_ids": [...]}` — so a freshly approved
    # member never sits with an account and nothing to open. The same full
    # replacement as `workspaces` (one write path for grants); sending both is 422.
    workspace_ids: list[str] | None = None
    # r12-g (N12-obs57): `true` clears the account's sign-in lockout / failure counter
    # (`services/login_guard`); `false` is a no-op.
    unlock: StrictBool | None = None

    @model_validator(mode="after")
    def _require_one_field(self) -> "UserPatch":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        if {"workspaces", "workspace_ids"} <= self.model_fields_set:
            raise ValueError("send either workspaces or workspace_ids, not both")
        return self


@router.get("")
def list_users(
    q: str | None = None,
    status: str = "all",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=SQLITE_MAX_INT),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if status not in users_service.STATUS_FILTERS:
        raise AppError(
            "validation.invalid_request",
            f"status must be one of {', '.join(users_service.STATUS_FILTERS)}",
            status_code=422,
        )
    page = users_service.list_users(db, q=q, status=status, limit=limit, offset=offset)
    return {
        "items": page.items,
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
    }


@router.get("/stats")
def user_stats(
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return users_service.compute_stats(db, get_settings())


@router.patch("/{user_id}")
def update_user(
    user_id: str,
    patch: UserPatch,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user = users_service.get_user(db, user_id)
    sent = {key: getattr(patch, key) for key in patch.model_fields_set}
    if "workspace_ids" in sent:
        sent["workspaces"] = sent.pop("workspace_ids")
    generated = users_service.apply_patch(db, user, sent, get_settings())
    payload = users_service.serialize(
        user, workspaces=users_service.workspace_grants(db, [user.id]).get(user.id, [])
    )
    if generated is not None:
        # shown once in the console; only the hash is persisted
        payload["generated_password"] = generated
    return payload


@router.delete("/{user_id}")
def delete_user(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from app.assistant.principal import principal_of

    identity = require_identity(request)

    # the account's assistant conversations cascade: ledger-only ones are deleted,
    # ones with deployed/created resources pass to this administrator (N-ARCH-obs6)
    cascade = users_service.delete_user(
        db, users_service.get_user(db, user_id),
        successor=principal_of(identity), successor_name=identity.username,
        actor_user_id=identity.user_id,
    )
    # N-ARCH-obs6 (round-5 re-test): the cascade ran, but `{"ok": true}` said nothing
    # about it, so the admin's 404 on the member's (now deleted) conversation was read
    # as an orphaned row. The response names what happened to each conversation.
    return {"ok": True, "conversations": cascade}
