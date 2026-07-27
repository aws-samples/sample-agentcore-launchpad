"""Admin-only console user management (registered accounts + statistics).

Every route requires an administrator identity (`Depends(require_admin)`), which
resolves to the built-in admin, an admin-role registered account, or — when the
console gate is disabled entirely — the implicit local operator.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError
from app.routers.auth import Identity, require_admin
from app.services import users as users_service

router = APIRouter(prefix="/api/users", tags=["users"])


class UserPatch(BaseModel):
    """Partial admin update. Only the keys actually sent are applied, so
    `{"expires_at": null}` means "never expires" while omitting it keeps the
    current validity."""

    status: str | None = None
    role: str | None = None
    extend_days: int | None = Field(default=None, ge=1, le=3650)
    expires_at: datetime | None = None
    password: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _require_one_field(self) -> "UserPatch":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


@router.get("")
def list_users(
    q: str | None = None,
    status: str = "all",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
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
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    return users_service.compute_stats(db, get_settings())


@router.patch("/{user_id}")
def update_user(
    user_id: str,
    patch: UserPatch,
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    user = users_service.get_user(db, user_id)
    sent = {key: getattr(patch, key) for key in patch.model_fields_set}
    generated = users_service.apply_patch(db, user, sent, get_settings())
    payload = users_service.serialize(user)
    if generated is not None:
        # shown once in the console; only the hash is persisted
        payload["generated_password"] = generated
    return payload


@router.delete("/{user_id}")
def delete_user(
    user_id: str,
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, bool]:
    users_service.delete_user(db, users_service.get_user(db, user_id))
    return {"ok": True}
