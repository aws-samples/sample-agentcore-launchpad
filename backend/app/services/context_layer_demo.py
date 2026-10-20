"""The COA demo account: one shared, read-only COA identity members may sign in as.

Most members have no COA account (COA's user pool is admin-create-only), so an
operator creates one shared account with ``samples/coa-demo/demo_account.py``,
grants its group read-only access in COA, and stores its credentials as an SSM
SecureString ``/<coa prefix>/launchpad/<name>`` in the COA region:
``{"username", "password", "sub"}``. An admin names that parameter on the connect
form; the ``context_layers`` row keeps only the name (``resources.demo_account_param``).

**Choosing it is the member's act (D1).** The console reveals the credentials and
the member types them at COA's own login page, through the ordinary consent
(``POST /api/context-layer/authorize {account: "demo"}`` only checks that an account
exists and logs the choice). Launchpad never signs in for anyone and never falls back
to the account; each member keeps their own vault binding, and revoking one touches
nobody else. **The consent carries no ``customParameters``** (no ``login_hint``): the
token vault keys a token by them (live, 2026-10-20), so a consent made with any would
land in a vault slot that the broker, the Harness override, the playground and every
Strands runtime never read. The username is not prefilled; the panel's copy buttons
are the way.

**Custody.** The password lives in SSM, in this process for at most
``CACHE_TTL_S``, in the reveal response (``no-store``) and in the member's browser
while the panel is open. Never the ledger, a log line, a status payload or an
error: ``DemoAccount`` hides it from ``repr``, and every refusal names the problem,
never the value. The ``/<prefix>/launchpad/`` scope keeps the reveal route from
becoming a reader of any other secret in the account.

**Switching accounts.** A browser that still holds a COA session consents silently
as that session's user (COA's classic hosted UI, and no ``prompt=login`` to send).
So after a consent the status compares the vaulted token's ``sub`` with the
account's (``identity_of``), and the console links COA's web-client ``/logout``
(``sign_out_url``) for the revoke → sign out → authorize again sequence.
"""

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.orm import Session

from app.core.errors import AppError, aws_error_code
from app.services import context_layer
from app.services import context_layer_tokens as tokens
from app.services.workspace import WorkspaceContext

logger = logging.getLogger("launchpad.context_layer.demo")

PARAM_KEY = "demo_account_param"
# an SSM parameter name, as the connect form accepts it
PARAM_RE = re.compile(r"^/[A-Za-z0-9_.\-/]{1,500}$")
CACHE_TTL_S = 300

IDENTITY_DEMO = "demo"
IDENTITY_OWN = "own"
ACCOUNTS = (IDENTITY_DEMO, IDENTITY_OWN)

_PROBLEMS = {
    "not_found": "does not exist in the COA region",
    "not_secure_string": "is not a SecureString",
    "bad_shape": "is not JSON with a non-empty username and password",
}


@dataclass(frozen=True)
class DemoAccount:
    """The shared account's credentials. ``repr`` never shows the password."""

    username: str
    password: str = field(repr=False)
    sub: str = ""


# (workspace id, parameter name) -> (monotonic expiry, account); failures never cached
_CACHE: dict[tuple[str, str], tuple[float, DemoAccount]] = {}
_LOCK = threading.Lock()


def scope(prefix: str) -> str:
    """Where a demo account parameter must live: COA's own SSM tree, Launchpad's branch."""
    return f"/{prefix}/launchpad/"


def in_scope(prefix: str, name: str) -> bool:
    root = scope(prefix)
    if not PARAM_RE.fullmatch(name or "") or not name.startswith(root):
        return False
    rest = name[len(root):].split("/")
    return all(segment not in ("", ".", "..") for segment in rest)


def check_scope(prefix: str, name: str) -> str:
    """``name`` when it is a parameter under ``/<prefix>/launchpad/``, else 422."""
    name = (name or "").strip()
    if not in_scope(prefix, name):
        raise AppError(
            "context_layer.demo_param_scope",
            f"the demo account parameter must be an SSM parameter under {scope(prefix)}",
            {"scope": scope(prefix)},
            status_code=422,
        )
    return name


def _invalid(name: str, problem: str) -> AppError:
    return AppError(
        "context_layer.demo_param_invalid",
        f"the demo account parameter {name} {_PROBLEMS[problem]}",
        {"parameter": name, "problem": problem},
        status_code=409,
    )


def read(ctx: WorkspaceContext, name: str) -> DemoAccount:
    """``GetParameter(WithDecryption)`` in the COA region (``ctx``), checked for type
    and shape. Refusals name the problem only — never the value."""
    try:
        parameter = ctx.client("ssm").get_parameter(Name=name, WithDecryption=True).get(
            "Parameter"
        ) or {}
    except ClientError as exc:
        code = aws_error_code(exc)
        if code == "ParameterNotFound":
            raise _invalid(name, "not_found") from None
        raise AppError(
            "context_layer.discovery_failed",
            f"could not read the demo account parameter {name} ({code})",
            {"source": "demo account parameter", "code": code},
            status_code=502,
        ) from None
    except BotoCoreError as exc:
        code = type(exc).__name__
        raise AppError(
            "context_layer.discovery_failed",
            f"could not read the demo account parameter {name} ({code})",
            {"source": "demo account parameter", "code": code},
            status_code=502,
        ) from None
    if parameter.get("Type") != "SecureString":
        raise _invalid(name, "not_secure_string")
    try:
        value = json.loads(str(parameter.get("Value") or ""))
    except ValueError:
        value = None
    finally:
        parameter.clear()
    if not isinstance(value, dict):
        raise _invalid(name, "bad_shape")
    username, password, sub = value.get("username"), value.get("password"), value.get("sub")
    if not (isinstance(username, str) and username.strip() and isinstance(password, str)
            and password):
        raise _invalid(name, "bad_shape")
    return DemoAccount(
        username=username.strip(), password=password, sub=sub if isinstance(sub, str) else ""
    )


def param_of(layer: context_layer.ContextLayer) -> str:
    return str(layer.resources.get(PARAM_KEY) or "")


def _cached(
    workspace: WorkspaceContext, layer: context_layer.ContextLayer, now: Callable[[], float]
) -> DemoAccount:
    name = param_of(layer)
    key = (layer.workspace_id, name)
    with _LOCK:
        hit = _CACHE.get(key)
    if hit is not None and hit[0] > now():
        return hit[1]
    account = read(context_layer.coa_context(workspace, layer.coa_region), name)
    with _LOCK:
        _CACHE[key] = (now() + CACHE_TTL_S, account)
    return account


def forget(workspace_id: str) -> None:
    """Drop the cached account of a workspace (connect / disconnect)."""
    with _LOCK:
        for key in [key for key in _CACHE if key[0] == workspace_id]:
            _CACHE.pop(key, None)


def unavailable() -> AppError:
    return AppError(
        "context_layer.demo_unavailable",
        "this workspace's context layer has no demo account — an administrator sets "
        "its SSM parameter on the Context Layer page",
        status_code=404,
    )


def require(
    workspace: WorkspaceContext,
    layer: context_layer.ContextLayer,
    *,
    now: Callable[[], float] = time.monotonic,
) -> DemoAccount:
    """The account, or the refusal: 404 when none is configured, else ``read``'s."""
    if not param_of(layer):
        raise unavailable()
    return _cached(workspace, layer, now)


def load(
    workspace: WorkspaceContext,
    layer: context_layer.ContextLayer,
    errors: list[dict[str, str]] | None = None,
    *,
    now: Callable[[], float] = time.monotonic,
) -> DemoAccount | None:
    """The account, or None when none is configured or it cannot be read (then a
    ``demo_account`` entry in ``errors``)."""
    if not param_of(layer):
        return None
    try:
        return _cached(workspace, layer, now)
    except AppError as exc:
        logger.warning("the COA demo account parameter could not be read (%s)", exc.code)
        if errors is not None:
            errors.append({"probe": "demo_account", "code": exc.code})
        return None


def public_view(account: DemoAccount | None) -> dict[str, Any]:
    """What any member may see: whether an account exists, and its username."""
    return {"available": account is not None, "username": account.username if account else None}


def identity_of(token: str, demo: DemoAccount | None) -> str | None:
    """Who a vaulted COA token belongs to: ``demo``, ``own``, or None (unknown).

    The claims are decoded without verification: they come straight from the vault
    and are only compared here, never trusted for authorization."""
    if demo is None:
        return None
    claims = tokens.claims(token)
    sub = claims.get("sub")
    if demo.sub and isinstance(sub, str) and sub:
        return IDENTITY_DEMO if sub == demo.sub else IDENTITY_OWN
    # no stored sub: only a username match is conclusive (Cognito's username claim
    # may be the sub itself, so a mismatch proves nothing)
    names = {
        str(claims.get(key)).casefold()
        for key in ("username", "cognito:username")
        if isinstance(claims.get(key), str)
    }
    return IDENTITY_DEMO if demo.username.casefold() in names else None


def sign_out_url(layer: context_layer.ContextLayer) -> str | None:
    """COA's web-client ``/logout``, which clears the hosted-UI session for every
    client on the domain and lands on the COA console. ``logout_uri`` must equal one
    of the web client's LogoutURLs, trailing slash included. None without a web
    client id (a row connected before it was discovered: re-connect fills it)."""
    domain = str(layer.resources.get("hosted_ui_domain") or "").rstrip("/")
    client_id = str(layer.resources.get(context_layer.WEB_CLIENT_KEY) or "")
    console = layer.console_url
    if not (domain and client_id and console):
        return None
    query = urlencode({"client_id": client_id, "logout_uri": console.rstrip("/") + "/"})
    return f"{domain}/logout?{query}"


def status_fields(
    db: Session,
    workspace: WorkspaceContext,
    layer: context_layer.ContextLayer,
    *,
    username: str,
    authorization: dict[str, Any],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    """``demo_account`` and ``sign_out_url`` for the status, and — for an authorized
    caller while a demo account exists — ``authorization.identity``: the broker's
    token (cached, else one vault read that never starts a consent) compared with the
    account. Best-effort: a failure reads ``identity: null``."""
    account = load(workspace, layer, errors)
    if authorization.get("status") == "authorized":
        identity = None
        if account is not None and username:
            try:
                grant = tokens.user_token(db, workspace, username, record_consent=False)
            except AppError as exc:
                errors.append({"probe": "identity", "code": exc.code})
            else:
                if isinstance(grant, tokens.Token):
                    identity = identity_of(grant.value, account)
        authorization["identity"] = identity
    return {"demo_account": public_view(account), "sign_out_url": sign_out_url(layer)}


def check_choice(
    workspace: WorkspaceContext,
    layer: context_layer.ContextLayer,
    account: str,
    *,
    username: str,
) -> None:
    """Accept an explicit account choice on ``authorize``: ``demo`` needs a demo
    account (404 ``demo_unavailable``, else ``read``'s refusal) and is logged. It
    changes nothing in the consent request — never ``customParameters``, which the
    vault keys tokens by — the member types the credentials at COA's login page."""
    if account == IDENTITY_DEMO:
        require(workspace, layer)
        logger.info(
            "%s started a COA consent as the demo account (workspace %s)",
            username, layer.workspace_id,
        )


def reveal(
    workspace: WorkspaceContext, layer: context_layer.ContextLayer, *, username: str
) -> dict[str, str]:
    """The credentials, for the member who asked. Logged without the password."""
    account = require(workspace, layer)
    # WARNING, not INFO: the app configures no logging, so only WARNING and above reach
    # the service journal, and this line is the only record of who read the password.
    logger.warning(
        "%s revealed the COA demo account (workspace %s)", username, layer.workspace_id
    )
    return {"username": account.username, "password": account.password}
