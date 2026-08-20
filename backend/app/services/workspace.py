"""The environment a piece of work runs against: one (account, region) pair with
its own AgentCore resource map.

A context is built either from a ``Workspace`` ledger row
(``workspace_context``) or, for hub-scoped work and call sites not yet threaded,
from ``Settings`` (``default_workspace_context``). Contexts are cheap and are not
cached: the client cache keys on the (account, region, role, external id) tuple,
so a fresh context shares cached clients with every other context naming the same
target.
"""

import threading
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import UserWorkspace, Workspace
from app.services import aws_clients


@dataclass(frozen=True, eq=False)
class WorkspaceContext:
    """Where AWS work lands. Compared by identity so it can key caches even
    though ``resources`` is a mutable map."""

    account_id: str
    region: str
    resources: dict[str, Any] = field(default_factory=dict)
    role_arn: str | None = None
    external_id: str | None = None
    # The ledger handle for this environment, so a service that scopes both AWS
    # calls and ledger queries needs one argument rather than two. Both builders
    # below set it from the row they read; the default only serves contexts built
    # by hand (tests, the hub bootstrap probe), which target the default row.
    id: str = DEFAULT_WORKSPACE_ID

    def client(self, service: str, cache_token: str | None = None, **cfg: Any) -> Any:
        return aws_clients.client(service, self, cache_token=cache_token, **cfg)

    def credentials(self) -> Any:
        """Frozen credentials for this workspace — for callers that sign requests
        themselves (SigV4) rather than use a service client.

        Deliberately narrow: handing out the session instead would let a caller
        build clients outside the factory's lock and cache, which the funnel
        guard test cannot see.
        """
        session = aws_clients.get_session(
            self.account_id, self.region, self.role_arn, self.external_id
        )
        return session.get_credentials().get_frozen_credentials()

    def sdk_session(self) -> Any:
        """Session-shaped handle for an AWS SDK class that insists on building its
        own clients (`aws_clients.FunnelSession`).

        Not a boto3 session: it routes every `client()` back through the factory,
        so an SDK's internal clients target this workspace and stay inside the
        lock and cache.
        """
        return aws_clients.sdk_session(self)


def workspace_context(row: Workspace) -> WorkspaceContext:
    """The context a ``Workspace`` ledger row describes.

    The row is what requests read for every workspace — but for ``default`` it
    is a MIRROR of ``config/launchpad.yaml``, re-converged from settings on
    every startup (core/db.init_db). Anything durably provisioned onto the
    default workspace must therefore land in the yaml, which
    ``merge_workspace_resources`` does automatically.
    """
    return WorkspaceContext(
        id=row.id,
        account_id=row.account_id,
        region=row.region,
        resources=row.resources or {},
        role_arn=row.role_arn,
        external_id=row.external_id,
    )


def get_workspace_row(db: Session, workspace_id: str) -> Workspace | None:
    return db.get(Workspace, workspace_id)


def context_for_workspace(workspace_id: str | None) -> WorkspaceContext:
    """The context a background worker rebuilds from a persisted `workspace_id`.

    Reads the row through its own short-lived session on purpose: worker threads
    receive ids, not ORM objects — a request's `WorkspaceScope.row` is detached by
    the time a thread runs, and refreshing it from another session's identity map
    is how stale-object bugs get in.

    A row that has gone missing raises rather than falling back to `default`:
    silently retargeting persisted work at another account is worse than the job
    failing with the id it could not resolve.
    """
    resolved = workspace_id or DEFAULT_WORKSPACE_ID
    db = SessionLocal()
    try:
        row = get_workspace_row(db, resolved)
        if row is None:
            raise LookupError(f"workspace '{resolved}' no longer exists")
        return workspace_context(row)
    finally:
        db.close()


_MERGE_LOCK = threading.Lock()


def merge_workspace_resources(
    workspace: WorkspaceContext, values: dict[str, Any]
) -> None:
    """Record newly provisioned resource identifiers on the workspace row.

    For resources provisioned outside a bootstrap pass (the lazy KB gateway) and
    for each stage of the bootstrap job, so the row — the resource map of record —
    learns about them as they are made. The in-memory context's map is updated in
    place as well: the caller holds that context for the rest of the request (or
    the rest of the job) and would otherwise keep reading a map without the ids it
    just created.

    Serialised because it is a read-modify-write of one JSON column and there are
    now concurrent writers: the bootstrap job writes it per stage on its own
    thread while a request can lazily provision the KB gateway. Two unlocked
    merges would interleave and the later commit would drop the earlier stage's
    identifiers.

    For the DEFAULT workspace the same keys are also mirrored into
    ``config/launchpad.yaml``: init_db's startup mirror re-converges the default
    row on settings every boot, so a row-only write would silently evaporate at
    the next restart. That trap was paid for twice — first by the lazy KB
    gateway (which used to carry this mirror itself), then by a prod Skill Lab
    provisioning whose worker keys vanished on restart (2026-08-20) — hence it
    lives here now, where every caller gets it.
    """
    with _MERGE_LOCK:
        db = SessionLocal()
        try:
            row = get_workspace_row(db, workspace.id)
            if row is None:
                raise LookupError(f"workspace '{workspace.id}' no longer exists")
            # Reassigned, not mutated: SQLAlchemy does not track in-place edits of a
            # JSON column.
            row.resources = {**(row.resources or {}), **values}
            db.commit()
        finally:
            db.close()
        workspace.resources.update(values)
        if workspace.id == DEFAULT_WORKSPACE_ID:
            # Late import: services.bootstrap reaches back into workspace-adjacent
            # modules, so a top-level import would cycle.
            from app.services import bootstrap

            bootstrap.write_config({"resources": values})
            get_settings.cache_clear()


def granted_workspace_ids(db: Session, user_id: str) -> list[str]:
    """Workspace ids this member may operate in, sorted for a stable fallback.

    Admins are not represented here: the built-in admin has no ``users`` row and
    so can never own a grant — admin access is a bypass, not a grant.
    """
    rows = db.execute(
        select(UserWorkspace.workspace_id).where(UserWorkspace.user_id == user_id)
    ).scalars()
    return sorted(rows)


def default_workspace_context() -> WorkspaceContext:
    """The single implicit workspace, read live from settings.

    Deliberately uncached: ``get_settings.cache_clear()`` is called at runtime
    (price refresher, kb_gateway) and a second-level cache would keep serving the
    superseded region/resource map.
    """
    settings = get_settings()
    return WorkspaceContext(
        id=DEFAULT_WORKSPACE_ID,
        account_id=settings.account_id,
        region=settings.region,
        resources=settings.resources,
    )
