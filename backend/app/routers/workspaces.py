"""Workspaces: the environment a request operates in, and their administration.

Two things live here, the way `routers/auth.py` owns both the login surface and
`require_identity`:

* **the request boundary** — `resolve_workspace` runs for every workspace-scoped
  route (from `enforce_route_policy`, so a handler cannot forget it), authorizes
  the caller against the workspace, and caches the result on
  `request.state.workspace`; handlers read it back with `require_workspace`;
* **the admin CRUD surface** — register / rename / detach (or purge) a workspace,
  and read or edit its grants. Grants have two shapes for two consoles: the
  per-user full replacement `PATCH /api/users/{id}` (`UserPatch.workspaces`) and
  the workspace-side bulk `PUT /api/workspaces/{id}/grants` here. Both write
  nothing but `user_workspaces`, so there is still one source of truth (see
  `replace_grants` for why the second shape is not a convenience).

A workspace in another account carries the `role_arn` the hub assumes and the
`external_id` the spoke's trust policy requires; both are validated here, and the
bootstrap job's `validate-access` stage is what proves they actually work.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import (
    DEFAULT_WORKSPACE_ID,
    WORKSPACE_SCOPED_TABLES,
    SessionLocal,
    get_db,
)
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent, User, UserWorkspace, Workspace
from app.routers.auth import ROLE_MEMBER, Identity, require_admin, require_identity
from app.services import aws_clients, workspace_bootstrap
from app.services import users as users_service
from app.services.workspace import (
    WorkspaceContext,
    default_workspace_context,
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
# The role the hub assumes in the spoke account. The account is read back out of
# the ARN and checked against `account_id`, so the two cannot disagree.
_ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:iam::(\d{12}):role/.+$")
_ROLE_ARN_MAX = 256  # the ledger column's width — reject rather than truncate
# STS's own ExternalId pattern, capped at the ledger column's width (STS allows
# 1224) so a longer one is rejected rather than silently truncated on insert.
# ASCII because `\w` would otherwise accept letters STS itself rejects.
_EXTERNAL_ID = re.compile(r"^[\w+=,.@:/-]{2,128}$", re.ASCII)
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
    # Absent for a workspace in the hub's own account (ambient credentials);
    # both together for a cross-account one.
    role_arn: str | None = None
    external_id: str | None = None


class WorkspacePatch(BaseModel):
    name: str = Field(min_length=1, max_length=64)


def _out(row: Workspace, *, reveal_role: bool = True) -> dict[str, Any]:
    """Console shape. `resources` is deliberately absent: it carries ARNs and
    identity-provider ids, and no console surface needs them.

    Every caller sees `cross_account`; only an admin sees which role
    (`reveal_role=False` for the member-reachable list), because the admin
    Workspaces page reads the row it manages out of that same list and has to be
    able to show what the spoke stack must be deployed as. `external_id` is a
    shared secret and never leaves the ledger either way.
    """
    payload = {
        "id": row.id,
        "name": row.name,
        "account_id": row.account_id,
        "region": row.region,
        "cross_account": row.role_arn is not None,
        "bootstrap_status": row.bootstrap_status,
        "is_default": row.id == DEFAULT_WORKSPACE_ID,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if reveal_role:
        payload["role_arn"] = row.role_arn
    return payload


def _validated_account(account_id: str) -> str:
    value = account_id.strip()
    if not _ACCOUNT.match(value):
        raise AppError(
            "workspace.invalid_account",
            "account_id must be a 12-digit AWS account id",
            {"account_id": account_id},
            status_code=400,
        )
    return value


def _validated_region(region: str) -> str:
    value = region.strip()
    if not _REGION.match(value):
        raise AppError(
            "workspace.invalid_region",
            "region must be an AWS region name (e.g. us-west-2)",
            {"region": value},
            status_code=400,
        )
    return value


def _cross_account_fields(
    role_arn_in: str | None, external_id_in: str | None, account_id: str
) -> tuple[str | None, str | None]:
    """The validated `(role_arn, external_id)` a registration carries, or `(None,
    None)` for a workspace in the hub's own account.

    Takes plain values rather than the request model so the access probe
    (`POST /preflight`) validates its input through exactly this function — the
    two must agree, or a preflight would pass values registration then rejects.

    Validated at registration rather than left to the bootstrap job: an operator
    who mistypes the role can be told so immediately, while a job would only fail
    on its first signed request — and by then the workspace looks half-broken.
    The pair is inseparable because the spoke's trust policy requires an
    ExternalId, so a role without one can never be assumed.
    """
    role_arn = (role_arn_in or "").strip() or None
    external_id = (external_id_in or "").strip() or None
    if role_arn is None and external_id is None:
        return None, None
    if role_arn is None or external_id is None:
        raise AppError(
            "workspace.role_and_external_id_required",
            "a cross-account workspace needs both role_arn and external_id — the "
            "spoke role's trust policy requires the ExternalId to be presented",
            status_code=400,
        )
    match = _ROLE_ARN.match(role_arn)
    if match is None or len(role_arn) > _ROLE_ARN_MAX:
        raise AppError(
            "workspace.invalid_role_arn",
            "role_arn must be an IAM role ARN (arn:aws:iam::<account>:role/<name>)",
            {"role_arn": role_arn},
            status_code=400,
        )
    if match.group(1) != account_id:
        raise AppError(
            "workspace.role_account_mismatch",
            f"the role lives in account {match.group(1)} but the workspace names "
            f"{account_id} — one of the two is a typo",
            {"role_account_id": match.group(1), "account_id": account_id},
            status_code=400,
        )
    if not _EXTERNAL_ID.match(external_id):
        raise AppError(
            "workspace.invalid_external_id",
            "external_id must be 2-128 characters of letters, digits or "
            "+=,.@:/-_ (it must match the ExternalId the spoke's stack was "
            "deployed with)",
            status_code=400,
        )
    return role_arn, external_id


@router.get("")
def list_workspaces(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Every workspace for an admin; the granted ones for a member."""
    identity = require_identity(request)
    rows = list(db.scalars(select(Workspace).order_by(Workspace.id)))
    if not identity.is_admin:
        granted = set(granted_workspace_ids(db, identity.user_id or ""))
        rows = [row for row in rows if row.id in granted]
    return {
        "workspaces": [_out(row, reveal_role=identity.is_admin) for row in rows],
        "all_workspaces": identity.is_admin,
    }


_ASSUMED_ROLE_ARN = re.compile(
    r"^arn:(?P<partition>aws[a-z-]*):sts::(?P<account>\d{12}):assumed-role/(?P<role>[^/]+)/"
)
# The hub's own principal cannot change while the process runs, and the console
# reads it on every visit to the registration form. Two concurrent first requests
# would each call STS once, which is why this needs no lock.
_HUB_IDENTITY: dict[str, Any] | None = None


def hub_role_arn(caller_arn: str) -> str:
    """The IAM role ARN a spoke's trust policy must name, from a caller ARN.

    `sts:GetCallerIdentity` reports an assumed role as
    `arn:aws:sts::<account>:assumed-role/<role>/<session>`, which is not a valid
    trust-policy principal — the role's own `arn:aws:iam::<account>:role/<role>`
    is. Anything already in `iam:` form (an IAM user hub, a role read some other
    way) is returned untouched.

    Caveat worth knowing when it disagrees with reality: the assumed-role form
    omits the role's *path*, so a hub role at `/team/launchpad-hub` reconstructs
    as `role/launchpad-hub`. Roles at the default path — every deployment this
    ships with — are exact.
    """
    match = _ASSUMED_ROLE_ARN.match(caller_arn)
    if match is None:
        return caller_arn
    return (
        f"arn:{match.group('partition')}:iam::{match.group('account')}"
        f":role/{match.group('role')}"
    )


@router.get("/hub-identity")
def hub_identity(_: Identity = Depends(require_admin)) -> dict[str, Any]:
    """The hub's own account and role — the two values a spoke stack is deployed with.

    Declared before the `/{workspace_id}` routes so the literal path wins, and
    workspace-exempt like the rest of this router: it describes the hub, not an
    environment.
    """
    global _HUB_IDENTITY
    if _HUB_IDENTITY is None:
        try:
            identity = default_workspace_context().client("sts").get_caller_identity()
        except Exception as exc:
            raise AppError(
                "workspace.hub_identity_unavailable",
                "this backend could not read its own AWS identity, so it cannot "
                "say which principal a spoke role should trust",
                status_code=502,
            ) from exc
        caller_arn = str(identity.get("Arn") or "")
        _HUB_IDENTITY = {
            "account_id": str(identity.get("Account") or ""),
            "caller_arn": caller_arn,
            "role_arn": hub_role_arn(caller_arn),
        }
    return _HUB_IDENTITY


class WorkspacePreflight(BaseModel):
    """The cross-account pair to probe, before any of it is recorded."""

    account_id: str = Field(min_length=1, max_length=16)
    region: str = Field(min_length=1, max_length=32)
    role_arn: str | None = None
    external_id: str | None = None


@router.post("/preflight")
def preflight_workspace(
    req: WorkspacePreflight,
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    """Can this hub actually assume that role? Answered before registration.

    The trap this closes: a wrong ExternalId (or a trust policy naming a
    different hub) is invisible until the bootstrap job's first signed request,
    which is minutes later and leaves a failed registration behind — the state
    `purge` now exists to clean up. One AssumeRole + `GetCallerIdentity` says the
    same thing in a second.

    A refusal is a **result, not an error**: `ok: false` with the diagnostic comes
    back as 200, because the operator asked a question and got its answer. Only
    an AssumeRole failure is caught that way; any other `ClientError` propagates
    to the central handlers as it would anywhere else.

    Writes nothing and probes nothing else — no ledger row, no resource
    discovery. Input is validated through the very functions registration uses,
    so a preflight can never pass values `POST /api/workspaces` would reject.
    """
    account_id = _validated_account(req.account_id)
    region = _validated_region(req.region)
    role_arn, external_id = _cross_account_fields(req.role_arn, req.external_id, account_id)
    if role_arn is None:
        raise AppError(
            "workspace.role_and_external_id_required",
            "preflight tests an assumed role, so it needs both role_arn and "
            "external_id — a workspace in the hub's own account has nothing to assume",
            status_code=400,
        )
    try:
        caller_account = aws_clients.probe_caller_identity(
            account_id, region, role_arn, external_id
        )
    except ClientError as exc:
        if not aws_clients.is_assume_role_failure(exc):
            raise
        return {
            "ok": False,
            "caller_account": None,
            "diagnostic": aws_clients.assume_role_diagnostic(exc),
        }
    return {"ok": True, "caller_account": caller_account, "diagnostic": None}


@router.post("", status_code=201)
def create_workspace(
    req: WorkspaceCreate,
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
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
    account_id = _validated_account(req.account_id)
    region = _validated_region(req.region)
    role_arn, external_id = _cross_account_fields(req.role_arn, req.external_id, account_id)
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
        account_id=account_id,
        region=region,
        role_arn=role_arn,
        external_id=external_id,
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
            f"a workspace already covers {account_id} / {region}",
            {"account_id": account_id, "region": region},
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


# The two states a purge accepts: a registration that never became a usable
# environment. `ready` is deliberately absent — retiring a working environment
# means deleting its AWS resources too, which this endpoint does not do.
PURGEABLE_STATUSES = frozenset({"registered", "failed"})


def _purge_refused(row: Workspace, reason: str, message: str, status_code: int) -> AppError:
    """One code for every refusal, with `reason` naming which rule stopped it —
    the console has copy per reason, and the reason values for a state conflict
    are the bootstrap statuses themselves."""
    return AppError(
        "workspace.purge_refused",
        message,
        {"workspace_id": row.id, "reason": reason, "bootstrap_status": row.bootstrap_status},
        status_code=status_code,
    )


def _assert_purgeable(db: Session, row: Workspace) -> tuple[dict[str, int], list[str]]:
    """Refuse anything that is not registration residue, then report what a purge
    would take with it: the referencing rows per table, and which resource keys
    the row carries.

    The resource keys are names only (`gateway_id`, `memory_id`), never their
    values: they tell the operator that a failed run had already provisioned
    something in AWS which purging the ledger row will not remove.
    """
    if row.id == DEFAULT_WORKSPACE_ID:
        raise _purge_refused(
            row,
            "default",
            "the default workspace describes the hub itself and cannot be purged",
            400,
        )
    if row.bootstrap_status not in PURGEABLE_STATUSES:
        raise _purge_refused(
            row,
            row.bootstrap_status,
            f"workspace '{row.id}' is {row.bootstrap_status} — purge only removes a "
            "registration that never became a usable environment",
            409,
        )
    live_agents = (
        db.query(Agent)
        .filter(Agent.workspace_id == row.id, Agent.status != "deleted")
        .count()
    )
    if live_agents:
        raise _purge_refused(
            row,
            "agents",
            f"workspace '{row.id}' still owns {live_agents} agent(s) — purging would "
            "orphan their AgentCore runtimes with nothing left to name them",
            409,
        )
    return _referencing_rows(db, row.id), sorted((row.resources or {}).keys())


@router.post("/{workspace_id}/purge")
def purge_workspace(
    workspace_id: str,
    dry_run: bool = False,
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    """Delete a failed or abandoned registration outright, rows and all.

    The escape hatch from `DELETE`'s guard: a bootstrap that failed after thirty
    seconds leaves one FAILED job row behind, which is enough to make the
    workspace undetachable *and* to keep its `UNIQUE(account_id, region)` slot
    occupied — so the operator cannot re-register the environment they were
    trying to fix without editing the ledger by hand. Purge is admissible
    exactly because those rows describe an environment that never worked; see
    `_assert_purgeable` for what disqualifies one.

    `?dry_run=true` runs the guardrails and reports what would go, deleting
    nothing: the console's confirm dialog calls it on open, so the operator reads
    the row counts (and any provisioned resource keys) before committing rather
    than after.

    AWS is untouched either way. What a failed run provisioned stays in the
    account, and `resource_keys` is what says so.
    """
    row = _requested_row(db, workspace_id)
    # Captured up front: after the rollback below, reading `row.id` would re-query
    # for an object this request may just have deleted.
    target = row.id
    rows, resource_keys = _assert_purgeable(db, row)
    payload = {
        "purged": not dry_run,
        "dry_run": dry_run,
        "workspace_id": target,
        "rows": rows,
        "resource_keys": resource_keys,
    }
    if dry_run:
        return payload
    # Raw per-table DELETEs, in reverse table order so a child row goes before its
    # parent: the ORM path would load every row (and `PolicyChange`'s immutability
    # listener refuses to delete an audit row at all), which is the same reason the
    # workspace_id backfill is raw SQL. One transaction — a half-purged workspace
    # would be worse than the residue this removes.
    for table in reversed(WORKSPACE_SCOPED_TABLES):
        db.execute(
            text(f"DELETE FROM {table} WHERE workspace_id = :id"),  # noqa: S608
            {"id": target},
        )
    db.query(UserWorkspace).filter(UserWorkspace.workspace_id == target).delete()
    # The workspace row goes with a **conditional** DELETE, the mirror image of
    # `create_bootstrap_job`'s conditional claim: the guardrails above are a read,
    # and sync handlers run on a threadpool, so a bootstrap POST can claim this
    # very row between them and the deletes. Only one of the two statements can
    # match — if this one does not, the run owns the workspace and everything above
    # rolls back, rather than a job provisioning AWS resources into a workspace
    # whose rows (the only record of what it created) have just been deleted.
    claimed = db.execute(
        delete(Workspace).where(
            Workspace.id == target,
            Workspace.bootstrap_status.in_(sorted(PURGEABLE_STATUSES)),
        )
    ).rowcount
    if not claimed:
        db.rollback()
        current = db.scalar(select(Workspace.bootstrap_status).where(Workspace.id == target))
        raise AppError(
            "workspace.purge_refused",
            f"workspace '{target}' changed while this purge was preparing "
            f"(now {current or 'gone'}) — nothing was deleted; re-read it and retry",
            {"workspace_id": target, "reason": current or "gone", "bootstrap_status": current},
            status_code=409,
        )
    db.commit()
    return payload


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


@router.get("/{workspace_id}/bootstrap")
def bootstrap_status(
    workspace_id: str,
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    """The latest bootstrap run for this workspace, if any.

    Exists so a browser (or admin) that did not start the run can still find and
    watch it — live polling stays on `GET /api/jobs/{job_id}`, this only answers
    "which job is it".
    """
    row = _requested_row(db, workspace_id)
    job = workspace_bootstrap.latest_job(db, workspace_id)
    payload: dict[str, Any] = {
        "workspace_id": row.id,
        "bootstrap_status": row.bootstrap_status,
        "job": None,
    }
    if job is not None:
        payload["job"] = {
            "id": job.id,
            "status": job.status,
            "stages": workspace_bootstrap.job_stages(job),
        }
    return payload


GRANT_FILTERS = ("all", "granted", "ungranted")
_GRANTS_LIMIT_DEFAULT = 20
# Matches the console's largest page size (`PAGE_SIZES` in components/Pager.tsx)
# and `/api/users`' own ceiling: a cap *below* what the page-size selector offers
# would silently drop rows, since the pager computes its offsets from the size it
# asked for, not from the `limit` reported back.
_GRANTS_LIMIT_MAX = 200


def _granted_total(db: Session, workspace_id: str) -> int:
    """How many member accounts hold this workspace, ignoring search and filter.

    Joined to `users` and restricted to members rather than counting
    `user_workspaces` rows: promoting a member to admin leaves its grant rows
    behind (admins reach every workspace by role, so nothing clears them), and a
    total that counted those would not match the table below it.
    """
    return int(
        db.scalar(
            select(func.count())
            .select_from(UserWorkspace)
            .join(User, User.id == UserWorkspace.user_id)
            .where(UserWorkspace.workspace_id == workspace_id, User.role == ROLE_MEMBER)
        )
        or 0
    )


@router.get("/{workspace_id}/grants")
def list_grants(
    workspace_id: str,
    q: str | None = None,
    granted: str = "all",
    limit: int = _GRANTS_LIMIT_DEFAULT,
    offset: int = 0,
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    """The member accounts this workspace can be granted to, and which hold it.

    Admins are absent on purpose: they reach every workspace by role, so listing
    them here would suggest a grant exists that could be revoked.

    Search and filter run in SQL over a LEFT JOIN on `user_workspaces` rather
    than in Python over every account, because the console pages this table and
    a deployment's account list is unbounded. `total` follows the current search
    and filter (it drives the pager); `granted_total` deliberately does not — it
    is a property of the workspace, so it must not move while the operator types.
    """
    row = _requested_row(db, workspace_id)
    if granted not in GRANT_FILTERS:
        raise AppError(
            "workspace.invalid_grant_filter",
            f"granted must be one of {', '.join(GRANT_FILTERS)}",
            {"granted": granted},
            status_code=400,
        )
    limit = max(1, min(limit, _GRANTS_LIMIT_MAX))
    offset = max(0, offset)
    # The workspace predicate belongs in the JOIN, not in WHERE: as a WHERE it
    # would drop every account that holds some *other* workspace's grant.
    grant_of_this_workspace = and_(
        UserWorkspace.user_id == User.id, UserWorkspace.workspace_id == row.id
    )
    base = (
        select(User, UserWorkspace.workspace_id)
        .outerjoin(UserWorkspace, grant_of_this_workspace)
        .where(User.role == ROLE_MEMBER)
    )
    needle = (q or "").strip().lower()
    if needle:
        # `username_key` is the stored lowercase form; email has no lowered column,
        # so it is lowered per query. `autoescape` matters because `_` is legal in
        # both a username and an email: unescaped it is a LIKE wildcard, and a
        # search for `ada_b` would quietly match `adaXb`.
        base = base.where(
            or_(
                User.username_key.contains(needle, autoescape=True),
                func.lower(User.email).contains(needle, autoescape=True),
            )
        )
    if granted == "granted":
        base = base.where(UserWorkspace.workspace_id.is_not(None))
    elif granted == "ungranted":
        base = base.where(UserWorkspace.workspace_id.is_(None))
    total = int(
        db.scalar(select(func.count()).select_from(base.order_by(None).subquery())) or 0
    )
    window = db.execute(
        base.order_by(User.username_key).limit(limit).offset(offset)
    ).all()
    now = datetime.now(UTC)
    return {
        "workspace_id": row.id,
        "users": [
            {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": user.role,
                "status": users_service.account_state(user, now),
                "granted": held is not None,
            }
            for user, held in window
        ],
        "total": total,
        "granted_total": _granted_total(db, row.id),
        "limit": limit,
        "offset": offset,
    }


class GrantsPatch(BaseModel):
    """A batch of grants to add and remove, from the workspace's side."""

    grant: list[str] = Field(default_factory=list)
    revoke: list[str] = Field(default_factory=list)


@router.put("/{workspace_id}/grants")
def replace_grants(
    workspace_id: str,
    req: GrantsPatch,
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    """Grant and revoke this workspace for several accounts at once.

    The workspace-side BULK complement to `PATCH /api/users/{id}`
    (`UserPatch.workspaces`, a per-user full replacement). Both write nothing but
    `user_workspaces`, so P2's "one write path for grants" rule holds in
    substance — what this adds is the shape the Workspaces console needs, where
    the workspace is fixed and the accounts vary. A per-user full replacement
    cannot express that without the console first reading every selected
    account's whole grant list and risking a lost update between two admins.

    Deliberately additive/subtractive rather than a replacement of the whole
    member set: an operator acts on the rows they selected, and accounts on other
    pages (or matching a different search) must not be revoked as a side effect.

    Allowed at any `bootstrap_status` — granting access before an environment is
    ready is how an operator hands it over, and the readiness gate on mutating
    traffic is enforced per request anyway.
    """
    row = _requested_row(db, workspace_id)
    # No falsy-filtering: a blank id is as much "not something this workspace can
    # be granted to" as an unknown one, and silently dropping it would make the
    # same garbage input succeed here and 400 one line below (live-found).
    grant = {value.strip() for value in req.grant}
    revoke = {value.strip() for value in req.revoke}
    both = sorted(grant & revoke)
    if both:
        raise AppError(
            "workspace.grant_conflict",
            f"the same account cannot be granted and revoked in one call: {', '.join(both)}",
            {"user_ids": both},
            status_code=400,
        )
    requested = grant | revoke
    if requested:
        members = set(
            db.scalars(
                select(User.id).where(
                    User.id.in_(sorted(requested)), User.role == ROLE_MEMBER
                )
            )
        )
        offenders = sorted(requested - members)
        if offenders:
            # Unknown ids and administrators land on the same code: an admin
            # cannot hold a grant it does not need, and both are "not something
            # this workspace can be granted to".
            raise AppError(
                "workspace.invalid_grant_targets",
                "these accounts cannot hold a workspace grant (unknown, or an "
                f"administrator, which reaches every workspace by role): {', '.join(offenders)}",
                {"user_ids": offenders},
                status_code=400,
            )
    added = 0
    if grant:
        # Idempotent: re-granting is a no-op rather than an IntegrityError on the
        # composite primary key.
        held = set(
            db.scalars(
                select(UserWorkspace.user_id).where(
                    UserWorkspace.workspace_id == row.id,
                    UserWorkspace.user_id.in_(sorted(grant)),
                )
            )
        )
        for user_id in sorted(grant - held):
            db.add(UserWorkspace(user_id=user_id, workspace_id=row.id))
        added = len(grant - held)
    removed = 0
    if revoke:
        removed = (
            db.query(UserWorkspace)
            .filter(
                UserWorkspace.workspace_id == row.id,
                UserWorkspace.user_id.in_(sorted(revoke)),
            )
            .delete(synchronize_session=False)
        )
    db.commit()
    return {
        "workspace_id": row.id,
        # What actually changed, which a re-grant of an already-granted account
        # reports as 0 — the console counts what the operator selected.
        "added": added,
        "removed": int(removed),
        "granted_total": _granted_total(db, row.id),
    }
