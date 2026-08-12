"""Workspaces: the environment a request operates in, and their administration.

Two things live here, the way `routers/auth.py` owns both the login surface and
`require_identity`:

* **the request boundary** — `resolve_workspace` runs for every workspace-scoped
  route (from `enforce_route_policy`, so a handler cannot forget it), authorizes
  the caller against the workspace, and caches the result on
  `request.state.workspace`; handlers read it back with `require_workspace`;
* **the admin CRUD surface** — register / rename / delete a workspace and read
  its grants. Grants are *written* through `PATCH /api/users/{id}`
  (`UserPatch.workspaces`) so there is one write path for them, not two.

Cross-account workspaces (`role_arn`) are refused here until phase 3.
"""

import re
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import (
    DEFAULT_WORKSPACE_ID,
    WORKSPACE_SCOPED_TABLES,
    SessionLocal,
    get_db,
)
from app.core.errors import AppError, NotFoundError
from app.models.ledger import User, UserWorkspace, Workspace
from app.routers.auth import Identity, require_admin, require_identity
from app.services import workspace_bootstrap
from app.services.workspace import (
    WorkspaceContext,
    get_workspace_row,
    granted_workspace_ids,
    workspace_context,
)
from app.services.workspace_bootstrap import STATUS_BOOTSTRAPPING

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

WORKSPACE_HEADER = "X-Workspace"
READY = "ready"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
# AWS region shape (us-west-2, eu-central-1, ap-southeast-3). The real
# service-availability check is the bootstrap job's validate-access stage.
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_ACCOUNT = re.compile(r"^\d{12}$")
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class WorkspaceScope:
    """One request's resolved environment: the id ledger queries filter on, the
    row itself for display/state, and the context AWS calls target."""

    id: str
    row: Workspace
    context: WorkspaceContext


# ── request boundary ────────────────────────────────────────────────────────


def resolve_workspace(request: Request) -> WorkspaceScope:
    """Resolve + authorize the workspace this request operates in.

    Mirrors `request.state.identity`: the scope is cached on
    `request.state.workspace` for `require_workspace` to read, and the ledger
    session used to resolve it is short-lived (the row is only read).
    """
    identity = require_identity(request)
    requested = (request.headers.get(WORKSPACE_HEADER) or "").strip()
    db = SessionLocal()
    try:
        row = _requested_row(db, requested) if requested else _fallback_row(db, identity)
        _authorize(db, identity, row)
        _assert_accepts(request.method, row)
        scope = WorkspaceScope(id=row.id, row=row, context=workspace_context(row))
    finally:
        db.close()
    request.state.workspace = scope
    return scope


def require_workspace(request: Request) -> WorkspaceScope:
    """The resolved workspace of the current request."""
    scope = getattr(request.state, "workspace", None)
    if not isinstance(scope, WorkspaceScope):
        # Not a caller error: the route is workspace-exempt in ROUTE_POLICY while
        # its handler asks for a workspace, so one of the two is wrong.
        raise AppError(
            "workspace.unresolved",
            f"{request.method} {request.url.path} is workspace-exempt in "
            "ROUTE_POLICY (app/core/route_policy.py) but its handler requires a "
            "workspace",
            status_code=500,
        )
    return scope


def _requested_row(db: Session, workspace_id: str) -> Workspace:
    row = get_workspace_row(db, workspace_id)
    if row is None:
        raise NotFoundError(
            "workspace.not_found",
            f"workspace '{workspace_id}' does not exist",
            {"workspace_id": workspace_id},
        )
    return row


def _fallback_row(db: Session, identity: Identity) -> Workspace:
    """The workspace an omitted `X-Workspace` header means.

    Kept unambiguous rather than convenient: curl and the vendored studio
    sub-app send no header, so an admin lands on `default` and a member with a
    single grant on that one; anything else has to say which.
    """
    if identity.is_admin:
        return _requested_row(db, DEFAULT_WORKSPACE_ID)
    granted = granted_workspace_ids(db, identity.user_id or "")
    if not granted:
        raise AppError(
            "workspace.forbidden",
            "no workspace has been granted to this account",
            status_code=403,
        )
    if len(granted) > 1:
        raise AppError(
            "workspace.header_required",
            f"send {WORKSPACE_HEADER} — this account has access to several workspaces",
            {"available": granted},
            status_code=400,
        )
    return _requested_row(db, granted[0])


def _authorize(db: Session, identity: Identity, row: Workspace) -> None:
    """Admins reach every workspace; members need a grant.

    The admin bypass is mandatory, not a shortcut: the built-in admin is
    config-driven and has no `users` row, so it can never own a grant.
    """
    if identity.is_admin:
        return
    granted = identity.user_id is not None and (
        db.execute(
            select(UserWorkspace.workspace_id).where(
                UserWorkspace.user_id == identity.user_id,
                UserWorkspace.workspace_id == row.id,
            )
        ).first()
        is not None
    )
    if not granted:
        raise AppError(
            "workspace.forbidden",
            f"this account has no access to workspace '{row.id}'",
            {"workspace_id": row.id},
            status_code=403,
        )


def _assert_accepts(method: str, row: Workspace) -> None:
    """A workspace whose bootstrap has not finished serves reads only, so the
    console can watch its progress but nothing provisions into half an
    environment."""
    if method in _READ_METHODS or row.bootstrap_status == READY:
        return
    if row.id == DEFAULT_WORKSPACE_ID:
        # `default` mirrors settings, so it reads "registered" on a box where
        # `make bootstrap` never ran. The flows that legitimately work there —
        # studio local debug, code generation, everything AWS-free — must keep
        # working exactly as they did before workspaces existed. The gate is for
        # operator-registered workspaces awaiting their bootstrap job.
        return
    raise AppError(
        "workspace.not_ready",
        f"workspace '{row.id}' is {row.bootstrap_status}; run its bootstrap first",
        {"workspace_id": row.id, "bootstrap_status": row.bootstrap_status},
        status_code=409,
    )


# ── administration ─────────────────────────────────────────────────────────


class WorkspaceCreate(BaseModel):
    id: str = Field(min_length=2, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    account_id: str = Field(min_length=1, max_length=16)
    region: str = Field(min_length=1, max_length=32)
    # Present so the refusal below is explicit rather than a silently ignored
    # field; phase 3 turns it into the cross-account path.
    role_arn: str | None = None
    external_id: str | None = None


class WorkspacePatch(BaseModel):
    name: str = Field(min_length=1, max_length=64)


def _out(row: Workspace) -> dict[str, Any]:
    """Console shape. `resources` is deliberately absent: it carries ARNs and
    identity-provider ids, and no console surface needs them."""
    return {
        "id": row.id,
        "name": row.name,
        "account_id": row.account_id,
        "region": row.region,
        "bootstrap_status": row.bootstrap_status,
        "is_default": row.id == DEFAULT_WORKSPACE_ID,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("")
def list_workspaces(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Every workspace for an admin; the granted ones for a member."""
    identity = require_identity(request)
    rows = list(db.scalars(select(Workspace).order_by(Workspace.id)))
    if not identity.is_admin:
        granted = set(granted_workspace_ids(db, identity.user_id or ""))
        rows = [row for row in rows if row.id in granted]
    return {"workspaces": [_out(row) for row in rows], "all_workspaces": identity.is_admin}


@router.post("", status_code=201)
def create_workspace(
    req: WorkspaceCreate,
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    if req.role_arn or req.external_id:
        raise AppError(
            "workspace.cross_account_unsupported",
            "cross-account workspaces (role_arn) ship in phase 3; register an "
            "environment in this account for now",
            status_code=400,
        )
    # Validated as sent rather than normalized: the id travels back in every
    # `X-Workspace` header and in URLs, and a silently rewritten one turns into a
    # 404 for a client that kept what it typed.
    workspace_id = req.id.strip()
    if workspace_id == DEFAULT_WORKSPACE_ID:
        raise AppError(
            "workspace.reserved_id",
            f"'{DEFAULT_WORKSPACE_ID}' is the hub's own workspace",
            status_code=400,
        )
    if not _SLUG.match(workspace_id):
        raise AppError(
            "workspace.invalid_id",
            "id must be 2-32 characters of lowercase letters, digits or '-' and "
            "start with a letter or digit",
            {"id": req.id},
            status_code=400,
        )
    if not _ACCOUNT.match(req.account_id.strip()):
        raise AppError(
            "workspace.invalid_account",
            "account_id must be a 12-digit AWS account id",
            {"account_id": req.account_id},
            status_code=400,
        )
    region = req.region.strip()
    if not _REGION.match(region):
        raise AppError(
            "workspace.invalid_region",
            "region must be an AWS region name (e.g. us-west-2)",
            {"region": region},
            status_code=400,
        )
    if get_workspace_row(db, workspace_id) is not None:
        raise AppError(
            "workspace.exists",
            f"workspace '{workspace_id}' already exists",
            {"workspace_id": workspace_id},
            status_code=409,
        )
    row = Workspace(
        id=workspace_id,
        name=req.name.strip(),
        account_id=req.account_id.strip(),
        region=region,
        bootstrap_status="registered",
        resources={},
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        # UNIQUE(account_id, region): two workspaces on one environment would
        # collide on every region-scoped resource name Launchpad provisions.
        db.rollback()
        raise AppError(
            "workspace.environment_taken",
            f"a workspace already covers {req.account_id.strip()} / {region}",
            {"account_id": req.account_id.strip(), "region": region},
            status_code=409,
        ) from exc
    return _out(row)


@router.patch("/{workspace_id}")
def update_workspace(
    workspace_id: str,
    req: WorkspacePatch,
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    """Rename only: account/region are the workspace's identity, and its
    resource map belongs to the bootstrap job."""
    row = _requested_row(db, workspace_id)
    row.name = req.name.strip()
    db.commit()
    return _out(row)


def _referencing_rows(db: Session, workspace_id: str) -> dict[str, int]:
    """Rows that would dangle if this workspace row disappeared, per table.

    Counted straight off `WORKSPACE_SCOPED_TABLES` rather than per model, so a
    table added to that list is covered without touching this function. A
    soft-deleted agent counts as much as a live one: its row still names the
    workspace, and `context_for_workspace` raises on an id it cannot resolve —
    the no-NULL startup invariant sees nothing wrong with a reference that
    points nowhere.
    """
    counts: dict[str, int] = {}
    for table in WORKSPACE_SCOPED_TABLES:
        found = db.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE workspace_id = :id"),  # noqa: S608
            {"id": workspace_id},
        ).scalar_one()
        if found:
            counts[table] = int(found)
    return counts


@router.delete("/{workspace_id}")
def delete_workspace(
    workspace_id: str,
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    """Detach-only decommission: the AWS resources it provisioned stay put.

    Refused while ANY per-environment row still names it — agents, but also the
    deploy jobs, chat history, api keys, audit trail and evaluation rows that
    would otherwise reference a workspace nobody can resolve. The 409 names what
    remains so the operator knows what to clear.
    """
    row = _requested_row(db, workspace_id)
    if row.id == DEFAULT_WORKSPACE_ID:
        raise AppError(
            "workspace.reserved_id",
            "the default workspace describes the hub itself and cannot be deleted",
            status_code=400,
        )
    remaining = _referencing_rows(db, row.id)
    if remaining:
        listed = ", ".join(f"{table}: {count}" for table, count in remaining.items())
        raise AppError(
            "workspace.in_use",
            f"'{row.id}' still owns ledger rows ({listed}) — clear them first",
            {"rows": remaining},
            status_code=409,
        )
    # Grants go with it: a re-registered id would otherwise inherit them.
    db.query(UserWorkspace).filter(UserWorkspace.workspace_id == row.id).delete()
    db.delete(row)
    db.commit()
    return {"deleted": True, "workspace_id": workspace_id}


def _bootstrap_conflict(row: Workspace) -> AppError:
    return AppError(
        "workspace.bootstrap_conflict",
        f"workspace '{row.id}' is {row.bootstrap_status}",
        {"workspace_id": row.id, "bootstrap_status": row.bootstrap_status},
        status_code=409,
    )


@router.post("/{workspace_id}/bootstrap", status_code=202)
def bootstrap_workspace(
    workspace_id: str,
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    """Queue the staged bootstrap job for a registered workspace.

    Workspace-exempt on purpose (the whole `/api/workspaces` prefix is): it
    operates *on* an environment that is not usable yet, and the target comes from
    the path rather than the `X-Workspace` header. Progress is polled through
    `GET /api/jobs/{job_id}` — its `payload.stages` carry the per-stage records.
    """
    row = _requested_row(db, workspace_id)
    if row.id == DEFAULT_WORKSPACE_ID:
        raise AppError(
            "workspace.default_not_bootstrappable",
            "the default workspace describes the hub, which is provisioned by "
            "`make bootstrap` (CDK) — its resource map mirrors config/launchpad.yaml "
            "on every startup and would overwrite whatever this job wrote",
            status_code=400,
        )
    if row.bootstrap_status in (STATUS_BOOTSTRAPPING, READY):
        raise _bootstrap_conflict(row)
    try:
        job = workspace_bootstrap.create_bootstrap_job(db, row)
    except workspace_bootstrap.BootstrapConflict as exc:
        # The check above lost a race with a concurrent request; the service's
        # conditional claim is what actually decides, so report its verdict.
        raise _bootstrap_conflict(_requested_row(db, workspace_id)) from exc
    workspace_bootstrap.start_bootstrap_async(job.id)
    return {
        "job_id": job.id,
        "workspace_id": row.id,
        "bootstrap_status": row.bootstrap_status,
        "stages": workspace_bootstrap.job_stages(job),
    }


@router.get("/{workspace_id}/grants")
def list_grants(
    workspace_id: str,
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    """Members granted this workspace. Admins are absent on purpose: they reach
    every workspace by role, so listing them here would suggest a grant exists
    that could be revoked."""
    row = _requested_row(db, workspace_id)
    rows = db.scalars(
        select(User)
        .join(UserWorkspace, UserWorkspace.user_id == User.id)
        .where(UserWorkspace.workspace_id == row.id)
        .order_by(User.username_key)
    )
    return {
        "workspace_id": row.id,
        "users": [
            {"id": user.id, "username": user.username, "email": user.email, "role": user.role}
            for user in rows
        ],
    }
