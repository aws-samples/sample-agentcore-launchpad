"""Session/client factory — the only place boto3 clients are constructed.

Every AWS call in the app resolves its credentials and region here, keyed by the
workspace it runs in, so that targeting a second account/region is a matter of
handing down a different context rather than editing call sites. Sessions are
cached per key because a session carries the credential resolver (and, later,
refreshable assume-role credentials) that must be shared across threads.
"""

import threading
from typing import TYPE_CHECKING, Any

import boto3

if TYPE_CHECKING:  # importing it at runtime would be a cycle: workspace imports this
    from app.services.workspace import WorkspaceContext

SessionKey = tuple[str, str, str | None, str | None]

_SESSIONS: dict[SessionKey, boto3.Session] = {}
_CLIENTS: dict[tuple[SessionKey, str, str | None], Any] = {}
_LOCK = threading.Lock()

# Passing any of these to botocore would silently override the workspace the
# client claims to target — and the cache key would not reflect it either.
FORBIDDEN_CFG = (
    "region_name",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
)


def get_session(
    account_id: str,
    region: str,
    role_arn: str | None = None,
    external_id: str | None = None,
) -> boto3.Session:
    """Cached session for one (account, region, role) target.

    ``role_arn=None`` means the hub's own ambient credentials. Cross-account
    AssumeRole is not wired yet — the parameters exist so the cache key and every
    caller are already shaped for it.
    """
    if role_arn:
        raise NotImplementedError(
            "cross-account workspaces are not supported yet — role_arn must be None"
        )
    key: SessionKey = (account_id, region, role_arn, external_id)
    with _LOCK:
        session = _SESSIONS.get(key)
        if session is None:
            session = boto3.Session(region_name=region)
            _SESSIONS[key] = session
        return session


def client(
    service: str, ctx: "WorkspaceContext", cache_token: str | None = None, **cfg: Any
) -> Any:
    """boto3 client for ``service`` in ``ctx``'s account/region.

    Call sites go through ``WorkspaceContext.client`` rather than here, so that a
    client can never be built without naming the environment it targets.

    ``cfg`` is forwarded to botocore verbatim (e.g. ``config=Config(...)``) and
    normally opts the client out of the cache, since botocore ``Config`` objects
    compare by identity and would grow the cache without ever hitting it. A hot
    call site that passes cfg can supply ``cache_token`` — a string standing in
    for its cfg in the cache key — to get the cached client back.

    Construction always happens under the lock: building clients off a shared
    session is not thread-safe in boto3.
    """
    overrides = [name for name in FORBIDDEN_CFG if name in cfg]
    if overrides:
        raise ValueError(
            f"{', '.join(overrides)} defeat workspace targeting — set the region and "
            "credentials on the WorkspaceContext instead of on the client"
        )
    key: SessionKey = (ctx.account_id, ctx.region, ctx.role_arn, ctx.external_id)
    session = get_session(*key)
    with _LOCK:
        if cfg and cache_token is None:
            return session.client(service, **cfg)
        cached = _CLIENTS.get((key, service, cache_token))
        if cached is None:
            cached = session.client(service, **cfg)
            _CLIENTS[(key, service, cache_token)] = cached
        return cached


def reset_cache() -> None:
    """Drop cached sessions/clients (config reload, tests)."""
    with _LOCK:
        _SESSIONS.clear()
        _CLIENTS.clear()
