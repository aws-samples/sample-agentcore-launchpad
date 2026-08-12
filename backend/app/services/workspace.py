"""The environment a piece of work runs against: one (account, region) pair with
its own AgentCore resource map.

A context is built either from a ``Workspace`` ledger row
(``workspace_context``) or, for hub-scoped work and call sites not yet threaded,
from ``Settings`` (``default_workspace_context``). Contexts are cheap and are not
cached: the client cache keys on the (account, region, role, external id) tuple,
so a fresh context shares cached clients with every other context naming the same
target.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
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


def workspace_context(row: Workspace) -> WorkspaceContext:
    """The context a ``Workspace`` ledger row describes.

    The row is authoritative for every workspace, including ``default`` — the
    settings resource map only seeds it at migration time.
    """
    return WorkspaceContext(
        account_id=row.account_id,
        region=row.region,
        resources=row.resources or {},
        role_arn=row.role_arn,
        external_id=row.external_id,
    )


def get_workspace_row(db: Session, workspace_id: str) -> Workspace | None:
    return db.get(Workspace, workspace_id)


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
        account_id=settings.account_id,
        region=settings.region,
        resources=settings.resources,
    )
