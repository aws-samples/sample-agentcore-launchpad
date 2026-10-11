"""Workspace resource visibility — who may see a user-managed resource.

A workspace is either **shared** (the default, and the only behavior before this
setting existed: every member sees every resource of the workspace) or
**isolated**, where a member sees a resource only when it is theirs, when its
creator is unknown (`created_by IS NULL` — legacy rows, platform samples, the
config admin, resources made outside the console), or when its creator is a
*current* admin (admins act as publishers). Admins always see everything. A
dangling creator id (the user was deleted) matches none of these, so the
resource is visible to admins only.

The ownership key is the `created_by` recorded by `services/creators.py`.

Enforcement lives in the per-kind choke points (`_dataset_in`, `_run_in`,
`online.get_config`, `jobs.get_job`, …), each taking a keyword-only
`viewer: Viewer = Viewer.ALL`. The default sees everything, so workers,
materializers, purge and deploy paths stay unfiltered; only routers pass the
request's viewer (`require_viewer`). A hidden resource raises the kind's
**existing** not-found error, so it is indistinguishable from a missing one.

The setting lives in `Workspace.settings["resource_visibility"]` and is re-read
with the workspace row on every request — a change applies to the next request.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from fastapi import Request
from sqlalchemy import or_, select, true
from sqlalchemy.orm import Session

from app.models.ledger import ApiKey, ResourceCreator, User, Workspace

if TYPE_CHECKING:
    from app.core.errors import AppError

SETTINGS_KEY = "resource_visibility"
SHARED = "shared"
ISOLATED = "isolated"
MODES = (SHARED, ISOLATED)

_ROLE_ADMIN = "admin"  # == routers.auth.ROLE_ADMIN (not imported: import cycle)


def mode(row: Workspace | None) -> str:
    """The workspace's visibility mode; absent or unknown reads as shared."""
    value = ((row.settings if row is not None else None) or {}).get(SETTINGS_KEY)
    return ISOLATED if value == ISOLATED else SHARED


def set_mode(db: Session, row: Workspace, value: str) -> None:
    """Persist the mode. Reassigned, not mutated — SQLAlchemy does not track
    in-place JSON edits. Changes no resource: it only decides what members see."""
    if value not in MODES:
        raise ValueError(f"unknown resource visibility {value!r}")
    row.settings = {**(row.settings or {}), SETTINGS_KEY: value}
    db.commit()


@dataclass(frozen=True)
class Viewer:
    """Whose eyes a request reads the workspace with.

    `filters` is True only for a non-admin in an isolated workspace; every other
    viewer (admins, the auth-disabled operator, a shared workspace, internal
    callers) sees everything and pays no extra query.
    """

    user_id: str | None
    filters: bool

    ALL: ClassVar[Viewer]

    @classmethod
    def of_request(cls, request: Request) -> Viewer:
        """The caller's viewer in the request's resolved workspace."""
        cached = getattr(request.state, "viewer", None)
        if isinstance(cached, Viewer):
            return cached
        from app.routers.auth import require_identity
        from app.routers.workspaces import require_workspace

        identity = require_identity(request)
        scope = require_workspace(request)
        viewer = cls(
            user_id=identity.user_id,
            filters=mode(scope.row) == ISOLATED and not identity.is_admin,
        )
        request.state.viewer = viewer
        return viewer

    def principal_key(self) -> str:
        """A cache key separating viewers that may see different sets."""
        return f"u:{self.user_id or ''}" if self.filters else "all"


Viewer.ALL = Viewer(user_id=None, filters=False)


def require_viewer(request: Request) -> Viewer:
    """FastAPI dependency: the request's viewer (workspace already resolved by
    the app-level route-policy dependency)."""
    return Viewer.of_request(request)


def viewer_for_principal(db: Session, workspace: Workspace | None, principal: str | None) -> Viewer:
    """The viewer of an assistant-conversation owner (`assistant/principal.py`).

    `user:<id>` → that account's current role decides; `config-admin` /
    `local-operator` are admins. Used where the acting human is not the
    resource's audience — a collaborator refreshing a shared conversation still
    gets the owner's catalog.
    """
    if mode(workspace) != ISOLATED:
        return Viewer.ALL
    if principal is None or not principal.startswith("user:"):
        return Viewer.ALL if principal is not None else Viewer(user_id=None, filters=True)
    user_id = principal.removeprefix("user:")
    user = db.get(User, user_id)
    if user is not None and user.role == _ROLE_ADMIN:
        return Viewer.ALL
    return Viewer(user_id=user_id, filters=True)


def viewer_for_key(db: Session, key: ApiKey) -> Viewer:
    """The viewer of a `/v1` API key: its **creator's** current visibility.

    Shared workspace → everything. A key with no recorded creator (config admin,
    auth-disabled operator, legacy keys) → everything. A creator who is currently
    an admin → everything. Otherwise the member's own view — re-evaluated on every
    call, so switching the workspace to isolated narrows existing member keys at
    once. (A deleted creator's key is refused before this by offboarding.)
    """
    workspace = db.get(Workspace, key.workspace_id) if key.workspace_id else None
    if mode(workspace) != ISOLATED or key.created_by is None:
        return Viewer.ALL
    user = db.get(User, key.created_by)
    if user is not None and user.role == _ROLE_ADMIN:
        return Viewer.ALL
    return Viewer(user_id=key.created_by, filters=True)


# ── predicates: a row check paired with its SQL clause ────────────────────────


def _admin_ids():
    return select(User.id).where(User.role == _ROLE_ADMIN)


def sees(viewer: Viewer, db: Session, created_by: str | None) -> bool:
    """May `viewer` see a resource created by `created_by`?"""
    if not viewer.filters or created_by is None or created_by == viewer.user_id:
        return True
    user = db.get(User, created_by)
    return user is not None and user.role == _ROLE_ADMIN


def clause(viewer: Viewer, column: Any) -> Any:
    """`sees` as a SQL predicate on a `created_by` column."""
    if not viewer.filters:
        return true()
    terms = [column.is_(None), column.in_(_admin_ids())]
    if viewer.user_id:
        terms.append(column == viewer.user_id)
    return or_(*terms)


def require_seen(viewer: Viewer, db: Session, created_by: str | None, error: AppError) -> None:
    """Raise `error` (the kind's not-found) when the resource is hidden."""
    if not sees(viewer, db, created_by):
        raise error


def hidden_aws_ids(
    viewer: Viewer, db: Session, workspace_id: str, kind: str, ids: Iterable[str]
) -> set[str]:
    """The ids of an AWS-only kind (`resource_creators`) hidden from `viewer`.

    An id with no creator row — created outside the console, or before creators
    were recorded — has an unknown creator and stays visible.
    """
    if not viewer.filters:
        return set()
    wanted = [i for i in set(ids) if i]
    if not wanted:
        return set()
    rows = db.execute(
        select(ResourceCreator.resource_id).where(
            ResourceCreator.workspace_id == workspace_id,
            ResourceCreator.kind == kind,
            ResourceCreator.resource_id.in_(wanted),
            ResourceCreator.created_by.is_not(None),
            ~clause(viewer, ResourceCreator.created_by),
        )
    )
    return {rid for (rid,) in rows}


def aws_id_visible(
    viewer: Viewer, db: Session, workspace_id: str, kind: str, resource_id: str
) -> bool:
    return resource_id not in hidden_aws_ids(viewer, db, workspace_id, kind, [resource_id])
