"""Session/client factory — the only place boto3 clients are constructed.

Every AWS call in the app resolves its credentials and region here, keyed by the
workspace it runs in, so that targeting a second account/region is a matter of
handing down a different context rather than editing call sites. Sessions are
cached per key because a session carries the credential resolver — for a
cross-account workspace, the refreshable assume-role credentials — that must be
shared across threads.
"""

import threading
from typing import TYPE_CHECKING, Any

import boto3
import botocore.session
from botocore.credentials import (
    AssumeRoleCredentialFetcher,
    DeferredRefreshableCredentials,
    _get_client_creator,
)
from botocore.exceptions import ClientError

if TYPE_CHECKING:  # importing it at runtime would be a cycle: workspace imports this
    from app.services.workspace import WorkspaceContext

SessionKey = tuple[str, str, str | None, str | None]

_SESSIONS: dict[SessionKey, boto3.Session] = {}
_CLIENTS: dict[tuple[SessionKey, str, str | None], Any] = {}
_LOCK = threading.Lock()

# The hub's own credentials are themselves an assumed role, and role chaining caps
# a chained session at one hour regardless of the spoke role's MaxSessionDuration
# — asking for more is rejected. Long jobs survive on refresh, not on duration.
ASSUME_ROLE_DURATION_S = 3600

ASSUME_ROLE_OPERATION = "AssumeRole"
# `AccessDenied` is not a modelled STS exception, so it can only be matched on the
# error code. A wrong ExternalId and a missing trust statement both return it.
_ASSUME_ROLE_DENIED_CODES = frozenset({"AccessDenied", "AccessDeniedException"})

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

    ``role_arn=None`` means the hub's own ambient credentials; a ``role_arn``
    means the hub assumes that role in the spoke account (``external_id`` is the
    workspace's shared secret with the spoke's trust policy).

    The cache is what makes assumed credentials correct, not just cheap: one
    session per key owns exactly one credentials object, and the refresh lock
    lives on that object. Two sessions for one target would refresh independently
    and neither would serialise the other, so ``reset_cache()`` must not run
    while a job is in flight.
    """
    key: SessionKey = (account_id, region, role_arn, external_id)
    with _LOCK:
        session = _SESSIONS.get(key)
        if session is None:
            session = (
                _assumed_role_session(account_id, region, role_arn, external_id)
                if role_arn
                else boto3.Session(region_name=region)
            )
            _SESSIONS[key] = session
        return session


def _role_session_name(account_id: str, region: str) -> str:
    """What the spoke account's CloudTrail records this hub as.

    The workspace id would read better, but a session is cached per
    (account, region, role) and does not know which workspace asked for it — and
    the UNIQUE(account_id, region) constraint makes this pair name the workspace
    just as precisely. Always within the 64-char / ``[\\w+=,.@-]*`` STS limits.
    """
    return f"launchpad-{account_id}-{region}"


def _assumed_role_session(
    account_id: str, region: str, role_arn: str, external_id: str | None
) -> boto3.Session:
    """A session whose credentials come from ``sts:AssumeRole``, auto-refreshing.

    Refresh matters operationally, so the semantics botocore gives us:

    * no AssumeRole call happens here — the first request that *signs* triggers it;
    * with more than 900 s left, a failed refresh is swallowed and the still-valid
      credentials are served (a transient STS error cannot kill a long job);
    * with less than 600 s left, a failed refresh propagates verbatim, at whatever
      call site happened to be signing — which is why `AssumeRole` failures are
      mapped centrally (`app/core/errors.py`) rather than at one route.

    Two private botocore APIs are used deliberately. ``inner._credentials``: the
    public ``set_credentials`` only takes static keys and cannot install a
    refreshable object; every cross-account recipe assigns this attribute and it
    has been stable across botocore 1.x. ``_get_client_creator``: the fetcher
    builds its STS client with no region of its own, so a plain
    ``session.create_client`` would sign against the *hub's* region — this is the
    helper botocore passes to its own AssumeRoleProvider, and it pins STS to the
    spoke's region (required for opt-in regions).
    """
    hub = botocore.session.get_session()
    source = hub.get_credentials()
    if source is None:
        # The fetcher would keep the None and fail at the first signed request with
        # an AttributeError from inside botocore, naming neither the hub nor the role.
        raise RuntimeError(
            "this backend has no AWS credentials of its own, so it cannot assume "
            f"{role_arn} — a cross-account workspace needs the hub's own role first"
        )
    extra_args: dict[str, Any] = {
        "RoleSessionName": _role_session_name(account_id, region),
        "DurationSeconds": ASSUME_ROLE_DURATION_S,
    }
    if external_id:
        extra_args["ExternalId"] = external_id
    fetcher = AssumeRoleCredentialFetcher(
        client_creator=_get_client_creator(hub, region),
        source_credentials=source,
        role_arn=role_arn,
        extra_args=extra_args,
    )
    inner = botocore.session.Session()
    inner._credentials = DeferredRefreshableCredentials(
        refresh_using=fetcher.fetch_credentials, method="assume-role"
    )
    inner.set_config_variable("region", region)
    return boto3.Session(botocore_session=inner)


def is_assume_role_failure(exc: BaseException) -> bool:
    """True for the ClientError STS raises when the hub cannot assume a spoke role.

    It surfaces wherever a request was being signed — a router, a bootstrap stage,
    a background deploy — not at session construction.
    """
    return isinstance(exc, ClientError) and exc.operation_name == ASSUME_ROLE_OPERATION


def assume_role_diagnostic(exc: ClientError) -> str:
    """Operator-actionable text for a failed AssumeRole.

    AccessDenied has three causes worth naming, because STS reports all three
    identically: the trust policy does not name this hub's role, the ExternalId
    does not match the one the spoke's stack was deployed with, or the hub role
    was recreated (AWS pins a role-ARN principal to the role's unique id, so a
    same-named replacement is a different principal).
    """
    error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
    code = str(error.get("Code") or "Unknown")
    message = str(error.get("Message") or "")
    if code in _ASSUME_ROLE_DENIED_CODES:
        cause = (
            "check the spoke role's trust policy (it must name this hub's role), "
            "the workspace's ExternalId (it must match the stack parameter), and "
            "whether either role was recreated"
        )
    else:
        cause = "STS refused the role assumption"
    return f"cannot assume this workspace's role ({code}) — {cause}. {message}".strip()


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
    """Drop cached sessions/clients (config reload, tests).

    Not safe to call while work is in flight against a cross-account workspace: a
    dropped session takes its credentials object — and the refresh lock on it —
    with it, so threads still holding the old session and threads getting a new
    one would refresh the same role in parallel.
    """
    with _LOCK:
        _SESSIONS.clear()
        _CLIENTS.clear()
