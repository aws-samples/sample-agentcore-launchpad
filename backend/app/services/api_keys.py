"""Public /v1 API key lifecycle: who minted a key, and whether they still may (#259).

A key carries its creator (`ApiKey.created_by`, a `users.id`). Once that account
is offboarded the key stops working — `creator_offboarded` is the ONE definition
of "offboarded", used in three places:

* `/v1` auth (`routers.public_api.require_api_key`) refuses the key with the same
  401 as any unusable key and flips it to disabled in that request;
* `POST /api/apikeys/{id}/enable` refuses with 409 `apikey.creator_offboarded`;
* `GET /api/apikeys` marks such keys for the console.

Offboarding has several paths and one of them never writes: account expiry is
checked lazily per request, nothing fires when `expires_at` passes. So the check
at use is what guarantees the rule; the explicit write paths (admin disable /
delete, demotion, grant removal) additionally call `disable_offboarded_keys` so the
console shows those keys disabled at once. Keys are only ever disabled here, never
deleted, and a key whose creator has no `users` row (the config admin, the
auth-disabled operator, or a legacy NULL creator) is never offboarded.

`visible_keys` is the CN member rule the user decided for step 1: a member lists
and toggles only the keys they minted; an administrator sees every key of the
workspace. Step 2 (r12-d) builds the general visibility clause on the same column.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Query, Session

from app.models.ledger import ApiKey, User, UserWorkspace
from app.services import users as users_service

if TYPE_CHECKING:
    from app.routers.auth import Identity

# A hot key writes `last_used_at` at most this often.
LAST_USED_RESOLUTION = timedelta(minutes=1)

# `creator_offboarded` reasons, surfaced to the console (never on /v1).
REASON_DELETED = "deleted"
REASON_DISABLED = "disabled"
REASON_EXPIRED = "expired"
REASON_NO_GRANT = "no_workspace_grant"


def creator_offboarded(
    db: Session, key: ApiKey, now: datetime | None = None
) -> str | None:
    """Why this key's creator may no longer hold it, or None.

    Mirrors what the creator's own console session would be refused for: a
    missing, non-active or expired account, or — for a member — no grant to the
    key's workspace. Admins reach every workspace by role, so for them only the
    account state counts.
    """
    if key.created_by is None:
        return None
    user = db.get(User, key.created_by)
    if user is None:
        return REASON_DELETED
    if user.status != users_service.STATUS_ACTIVE:
        return REASON_DISABLED
    if users_service.is_expired(user, now):
        return REASON_EXPIRED
    if user.role != "admin" and not _granted(db, user.id, key.workspace_id):
        return REASON_NO_GRANT
    return None


def _granted(db: Session, user_id: str, workspace_id: str | None) -> bool:
    if workspace_id is None:
        return False
    return (
        db.execute(
            select(UserWorkspace.workspace_id).where(
                UserWorkspace.user_id == user_id,
                UserWorkspace.workspace_id == workspace_id,
            )
        ).first()
        is not None
    )


def disable_offboarded_keys(db: Session, user_id: str) -> int:
    """Disable every enabled key `user_id` minted that `creator_offboarded` now
    refuses. Staged on the session (flushed first, so pending grant/status
    changes are what the predicate sees); the caller commits."""
    db.flush()
    keys = db.scalars(
        select(ApiKey).where(ApiKey.created_by == user_id, ApiKey.enabled.is_(True))
    ).all()
    disabled = 0
    for key in keys:
        if creator_offboarded(db, key) is not None:
            key.enabled = False
            disabled += 1
    return disabled


def touch_last_used(key: ApiKey, now: datetime | None = None) -> bool:
    """Stamp `last_used_at` unless it was stamped within `LAST_USED_RESOLUTION`.
    Returns whether the row changed (the caller commits)."""
    moment = now or datetime.now(UTC)
    last = users_service.as_utc(key.last_used_at)
    if last is not None and moment - last < LAST_USED_RESOLUTION:
        return False
    key.last_used_at = moment
    return True


def visible_keys(query: Query, identity: Identity) -> Query:
    """Narrow a workspace's key query to what `identity` may list and toggle: every
    key for an administrator, only the keys they minted for a member. A legacy
    NULL-creator key is an administrator's (nobody else can claim it)."""
    if identity.is_admin:
        return query
    return query.filter(ApiKey.created_by == identity.user_id)


def may_manage(identity: Identity, key: ApiKey) -> bool:
    """The per-row form of `visible_keys`."""
    return identity.is_admin or (
        key.created_by is not None and key.created_by == identity.user_id
    )
