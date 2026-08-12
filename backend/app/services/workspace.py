"""The environment a piece of work runs against: one (account, region) pair with
its own AgentCore resource map.

There is exactly one workspace today, derived from ``Settings``, and
``default_workspace_context()`` is the only bridge from settings to context —
later phases resolve the context per request (from a header + grants) without
touching the call sites that consume it.
"""

from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
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

    def session(self) -> Any:
        """The underlying boto3 session — for callers that need credentials
        themselves (SigV4 signing) rather than a service client."""
        return aws_clients.get_session(
            self.account_id, self.region, self.role_arn, self.external_id
        )


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
