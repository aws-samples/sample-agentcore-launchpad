"""Console authentication: the built-in admin plus registered user accounts.

Two credential sources back one session cookie:

* the **built-in admin**, config-driven (`auth_username`/`auth_password`) so it
  can never be locked out from the console — it has no `users` row;
* **registered accounts** in the `users` table (self-service registration,
  7-day default validity), managed from the admin User Management module.

The cookie carries the subject (username) and its own expiry, signed as one
unit. The *role* is deliberately not in the cookie: authorization is resolved
per request from the ledger row, so demoting, disabling or expiring an account
takes effect immediately instead of when the cookie lapses.
"""

import base64
import binascii
import functools
import hashlib
import hmac
import ipaddress
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.actor import set_current_actor
from app.core.config import Settings, get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, envelope
from app.models.ledger import User
from app.services import admin_credential, auth_throttle
from app.services import users as users_service
from app.services.audit import record_audit_event

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "launchpad_session"
SESSION_TTL_SECONDS = 12 * 3600
# v2 binds a registered account's session to its IMMUTABLE ``users.id`` (v1 carried
# the username only, so a deleted-and-re-registered username kept an old cookie
# valid for the NEW account). v3 adds a stamp of the account's password hash, so an
# admin password reset ends the sessions opened with the old password (v2 kept them
# alive for up to SESSION_TTL_SECONDS). Older cookies are refused: re-login once.
_COOKIE_VERSION = "3"

_OPEN_API_PATHS = {
    "/api/auth/login",
    "/api/auth/status",
    "/api/auth/register",
    # the login page's identity-provider buttons (sso §2.6)
    "/api/auth/providers",
    "/api/health",
}
# the OIDC and SAML redirect legs (sso §2.2): the browser arrives without a session by
# design; SCIM (sso §2.4) carries its own bearer token, checked by its router
_OPEN_API_PATTERN = re.compile(
    r"^/api/auth/oidc/[A-Za-z0-9_-]{1,32}/(start|callback)$"
    r"|^/api/auth/saml/[A-Za-z0-9_-]{1,32}/(start|acs|metadata|sls)$"
    r"|^/api/scim/v2(/[^/]+){1,2}$"
)

ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_MEMBER = "member"

# Member-grantable agent-management capabilities (route_policy `perm:` values).
# A member holds every key by default; `users.permissions` stores only explicit
# denials. Admins (and the config-driven built-in admin) implicitly hold all.
AGENT_PERMISSIONS = users_service.AGENT_PERMISSIONS
# ...plus the account-only keys (`budget.manage`): what an account can hold
ACCOUNT_PERMISSIONS = users_service.ACCOUNT_PERMISSIONS


def granted_permissions(
    overrides: dict[str, bool] | None, role: str = ROLE_MEMBER
) -> frozenset[str]:
    """The granted keys for a row's `permissions` column value, given its role.

    T19: the defaults are per role (`users_service.default_permissions`) — an operator
    holds the promotion keys but not `agents.deploy`, a member the reverse — and an
    explicit override in the column wins either way.
    """
    defaults = users_service.default_permissions(role)
    return frozenset(
        key
        for key in ACCOUNT_PERMISSIONS
        if (overrides or {}).get(key, key in defaults)
    )


@dataclass(frozen=True)
class Identity:
    """The resolved caller behind a valid session cookie."""

    username: str
    role: str
    email: str | None = None
    account_expires_at: datetime | None = None
    user_id: str | None = None  # None for the config-driven admin
    permissions: frozenset[str] = frozenset(ACCOUNT_PERMISSIONS)
    # "sso" for an account that signs in through a console identity provider (sso
    # §2.2): no local password, so the top bar hides "change password"
    account_source: str = "local"

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_operator(self) -> bool:
        """T19 — the release role. Distinct from admin: an operator approves and runs
        releases but holds no user/workspace administration."""
        return self.role == ROLE_OPERATOR

    def can(self, permission: str) -> bool:
        return self.is_admin or permission in self.permissions


def _account_source(user: User) -> str:
    return "sso" if users_service.is_sso_account(user) else "local"


def _password(settings: Settings | None = None) -> str | None:
    secret = (settings or get_settings()).auth_password
    if secret is None:
        return None
    return secret.get_secret_value() or None


def enabled(settings: Settings | None = None) -> bool:
    return _password(settings) is not None


def cookie_secure(settings: Settings | None = None) -> bool:
    """Whether the session cookie carries `Secure`.

    Production is secure by default; `auth_cookie_secure` can still force it on
    in dev. Deliberately not a hardcoded True: local prod-mode smoke tests serve
    plain HTTP, and a `Secure` cookie there is silently never sent back.
    """
    current = settings or get_settings()
    return current.auth_cookie_secure or current.run_mode == "prod"


def registration_requires_approval(settings: Settings | None = None) -> bool:
    return (settings or get_settings()).auth_registration_require_approval


def registration_enabled(settings: Settings | None = None) -> bool:
    current = settings or get_settings()
    # With the gate disabled the console is already open, so there is nothing to
    # register an account for. With local login hidden (sso D-2) accounts come from
    # the identity provider, so self-service registration is off too.
    return (enabled(current) and current.auth_registration_enabled
            and not _local_login_hidden())


def _local_login_hidden() -> bool:
    """The administrators' "hide local login" switch (sso D-2), in effect only while
    an identity provider is enabled."""
    from app.services.sso_mapping import local_login_hidden

    db = SessionLocal()
    try:
        return local_login_hidden(db)
    finally:
        db.close()


def _signing_key(settings: Settings | None = None) -> bytes:
    current = settings or get_settings()
    return _derive_signing_key(current.auth_username, _password(current) or "")


# The cookie signing key is derived from the built-in admin password, so a session
# cookie (payload + HMAC) is enough to test password guesses offline. A single
# SHA-256 made each guess one hash; PBKDF2 makes it this many. Derived once per
# (username, password) per process — a password change still rotates the key (and
# signs every session out), as before.
_KDF_ITERATIONS = 210_000
_KDF_SALT = b"agentcore-launchpad-session-kdf-v1"


@functools.lru_cache(maxsize=8)
def _derive_signing_key(username: str, password: str) -> bytes:
    material = f"agentcore-launchpad-session:{username}:{password}"
    return hashlib.pbkdf2_hmac("sha256", material.encode("utf-8"), _KDF_SALT, _KDF_ITERATIONS)


def _encode_payload(
    subject: str, expiry: int, user_id: str | None = None, stamp: str | None = None
) -> str:
    # ``user_id`` and ``stamp`` are empty for the row-less config admin — its own
    # stable principal, whose password lives in settings (part of the signing key).
    raw = f"{_COOKIE_VERSION}:{subject}:{user_id or ''}:{stamp or ''}:{expiry}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _sign(payload: str, settings: Settings | None = None) -> str:
    signature = hmac.new(
        _signing_key(settings), payload.encode("ascii"), hashlib.sha256
    )
    return f"{payload}.{signature.hexdigest()}"


def _issue(
    subject: str,
    expiry: int,
    settings: Settings | None = None,
    user_id: str | None = None,
    stamp: str | None = None,
) -> str:
    return _sign(_encode_payload(subject, expiry, user_id, stamp), settings)


def _decode(
    cookie: str | None, settings: Settings | None = None
) -> tuple[str, str | None, str | None, int] | None:
    """Return `(subject, user_id | None, stamp | None, expiry)` for an authentic,
    unexpired v3 cookie."""
    if not cookie or "." not in cookie:
        return None
    payload = cookie.rpartition(".")[0]
    if not hmac.compare_digest(cookie, _sign(payload, settings)):
        return None
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    version, _, rest = raw.partition(":")
    parts = rest.rsplit(":", 3)
    if version != _COOKIE_VERSION or len(parts) != 4:
        return None
    subject, user_id, stamp, expiry_text = parts
    if not subject or not expiry_text.isdigit():
        return None
    expiry = int(expiry_text)
    if expiry <= time.time():
        return None
    return subject, user_id or None, stamp or None, expiry


def account_stamp(user: User) -> str:
    """The credential stamp a registered account's session cookie carries.

    The password hash, plus — once a forced logout has bumped it — the account's
    `session_epoch` (design §7.4): epoch 0 stamps exactly as before the column
    existed, so a deploy ends no session; any bump ends every open one.
    """
    # an SSO account has no password: its stamp is the IdP and subject it signs in as
    material = (
        users_service.sso_stamp_material(user)
        if users_service.is_sso_account(user)
        else user.password_hash
    )
    return users_service.credential_stamp(material, int(user.session_epoch or 0))


def resolve_identity(
    request: Request,
    settings: Settings | None = None,
    db: Session | None = None,
) -> Identity | None:
    """Resolve the caller, or None when the session must be rejected.

    Rejected: missing/tampered/expired cookie, unknown subject, disabled account,
    expired account, or a password changed since the cookie was issued. The config
    admin costs one `platform_settings` primary-key read (its console-set password
    stamp); registered accounts one `users` read.
    """
    current = settings or get_settings()
    decoded = _decode(request.cookies.get(COOKIE_NAME), current)
    if decoded is None:
        return None
    subject, user_id, stamp, _ = decoded
    is_builtin = user_id is None and hmac.compare_digest(
        subject.encode("utf-8"), current.auth_username.encode("utf-8")
    )
    if user_id is None and not is_builtin:
        return None  # a registered account's session always names its user id

    owned = db is None
    session = db or SessionLocal()
    try:
        if is_builtin:
            # One primary-key read: a password the built-in admin set on the console
            # (admin_credential) is stamped into its cookie like a registered
            # account's, so changing it — or clearing it (break-glass) — ends the
            # sessions opened with the previous one. No override ⇒ an empty stamp.
            expected = admin_credential.session_stamp(session, current)
            if not hmac.compare_digest(
                (stamp or "").encode("utf-8"), expected.encode("utf-8")
            ):
                return None
            return Identity(username=current.auth_username, role=ROLE_ADMIN)
        # Resolved by the immutable id, then the display name must still match: a
        # deleted account's cookie names an id that no longer exists, and never the
        # new account that re-registered the same username.
        user = session.get(User, user_id)
        if (
            user is None
            or user.username_key != users_service.normalize_username(subject).lower()
            or user.status != "active"
            or users_service.is_expired(user)
            # a password change (admin reset) or a forced logout (session_epoch
            # bump) ends the sessions opened before it
            or not hmac.compare_digest(
                (stamp or "").encode("utf-8"), account_stamp(user).encode("utf-8")
            )
        ):
            return None
        return Identity(
            username=user.username,
            role=user.role,
            email=user.email,
            account_expires_at=users_service.as_utc(user.expires_at),
            user_id=user.id,
            permissions=granted_permissions(user.permissions, user.role),
            account_source=_account_source(user),
        )
    finally:
        if owned:
            session.close()


def is_authenticated(request: Request, settings: Settings | None = None) -> bool:
    return resolve_identity(request, settings) is not None


OPEN_CONSOLE_REMEDY = (
    "This console has no authentication configured and refuses non-local "
    "requests. Set auth_password in config/launchpad.yaml (or "
    "LAUNCHPAD_AUTH_PASSWORD) to enable the login gate, or set "
    "LAUNCHPAD_ALLOW_OPEN_CONSOLE=true to accept an open console."
)


def _peer_is_loopback(request: Request) -> bool:
    """Whether the request's *transport peer* is loopback.

    Deliberately ignores `X-Forwarded-For`: a spoofable header would make this
    check decorative. The consequence is that a reverse proxy on the same host
    presents as 127.0.0.1 and is trusted — the same trust the console already
    places in localhost, and it never applies to the real production path, where
    authentication is enabled and this branch does not run.
    """
    client = request.client
    if client is None or not client.host:
        # ASGI transports without a peer (in-process calls) are local by nature.
        return True
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        # Not an IP literal (e.g. a unix socket or a test transport) — treat it
        # as non-local so the guard fails closed.
        return False


def _is_guarded_api_path(path: str) -> bool:
    return (
        (path == "/api" or path.startswith("/api/"))
        and path not in _OPEN_API_PATHS
        and not _OPEN_API_PATTERN.match(path)
    )


async def auth_middleware(request: Request, call_next: Any) -> Any:
    """Require a live console session while leaving health and /v1 intact.

    Two guards, in order: an unauthenticated console may only be reached from
    loopback (T1), and an authenticated one needs a live session. The first is
    checked per request rather than at startup because that is the only place the
    real peer is known — `create_app()` cannot see uvicorn's `--host`, so a
    startup-only check is bypassable by launching uvicorn directly.
    """
    settings = get_settings()
    set_current_actor(None, None)
    if request.method != "OPTIONS":
        path = request.url.path
        if _is_guarded_api_path(path):
            if not enabled(settings):
                if not settings.allow_open_console and not _peer_is_loopback(request):
                    return JSONResponse(
                        status_code=403,
                        content=envelope("auth.open_console_refused", OPEN_CONSOLE_REMEDY),
                    )
            else:
                identity = resolve_identity(request, settings)
                if identity is None:
                    return JSONResponse(
                        status_code=401,
                        content=envelope("auth.required", "Authentication required"),
                    )
                # Route-policy dependencies run after this middleware and need
                # the same live identity. Reuse it instead of opening a second
                # ledger session for every authenticated request.
                request.state.identity = identity
                # the id recorded beside the username by audit / SoD writers
                set_current_actor(identity.username, identity.user_id)
    return await call_next(request)


def require_identity(request: Request, settings: Settings | None = None) -> Identity:
    """Identity of the caller; the middleware has already rejected non-sessions."""
    current = settings or get_settings()
    if not enabled(current):
        # Gate disabled: the whole console is open, so the local operator is
        # treated as the built-in admin (matches pre-multi-user behavior).
        return Identity(username=current.auth_username, role=ROLE_ADMIN)
    cached = getattr(request.state, "identity", None)
    if isinstance(cached, Identity):
        return cached
    identity = resolve_identity(request, current)
    if identity is None:
        raise AppError("auth.required", "Authentication required", status_code=401)
    return identity


def current_identity(request: Request) -> Identity:
    """`require_identity` as a FastAPI dependency.

    `require_identity` takes an optional `settings` argument so tests can inject one.
    FastAPI reads that annotation as a second **body** parameter, which silently embeds
    a route's real body under a wrapper key (`{"req": {...}}`) — so a handler that wants
    an identity alongside a request body must depend on this instead.
    """
    return require_identity(request)


def require_admin(request: Request) -> Identity:
    identity = require_identity(request)
    if not identity.is_admin:
        raise AppError(
            "auth.forbidden",
            "This action requires an administrator account",
            status_code=403,
        )
    return identity


def require_permission(request: Request, permission: str) -> Identity:
    """Admin always passes; a member needs the (default-granted) permission.

    Inside a project (``request.state.project``, set by the route policy) the keys are
    that project's effective permissions (design §3.4); hub-global routes keep the
    account's own."""
    identity = require_identity(request)
    project = getattr(request.state, "project", None)
    allowed = project.can(permission) if project is not None else identity.can(permission)
    if not allowed:
        raise AppError(
            "auth.permission_required",
            f"This action requires the '{permission}' permission",
            {"permission": permission},
            status_code=403,
        )
    return identity


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=160)
    password: str = Field(min_length=1, max_length=256)


def _identity_fields(identity: Identity | None) -> dict[str, Any]:
    if identity is None:
        return {
            "username": None,
            "role": None,
            "email": None,
            "account_expires_at": None,
            "permissions": [],
            "builtin": False,
        }
    return {
        "username": identity.username,
        "role": identity.role,
        "email": identity.email,
        "account_expires_at": (
            identity.account_expires_at.isoformat()
            if identity.account_expires_at
            else None
        ),
        "permissions": sorted(identity.permissions),
        # the config-driven admin (no `users` row): its password is changed through
        # the same /api/auth/password, but replaces the configured one
        "builtin": identity.user_id is None and identity.is_admin,
        # `sso` — the session came from an identity provider (sso §2.7)
        "account_source": identity.account_source,
    }


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    settings = get_settings()
    required = enabled(settings)
    identity = (
        Identity(username=settings.auth_username, role=ROLE_ADMIN)
        if not required
        else resolve_identity(request, settings)
    )
    # the enabled identity providers (sso §2.7), the login page's buttons — present
    # only when there is one, so a console without SSO answers exactly as before
    providers = _sso_providers() if required else []
    # Nothing about the configured identity is disclosed before authentication.
    return {
        **({"sso_providers": providers} if providers else {}),
        "auth_required": required,
        "authenticated": identity is not None,
        "registration_enabled": registration_enabled(settings),
        "registration_requires_approval": registration_requires_approval(settings),
        **_closed_notice(settings),
        **_identity_fields(identity if required else None),
        # the running commit (online update §2.4): the updater's health check waits for
        # the restarted console to report the commit it just installed
        "version": {"sha": _running_sha()},
    }


def _sso_providers() -> list[dict[str, Any]]:
    from app.services.sso_oidc import public_providers

    db = SessionLocal()
    try:
        return public_providers(db)
    finally:
        db.close()


def _running_sha() -> str | None:
    from app.services.system_update import version_sha

    return version_sha()


@router.post("/login")
def login(req: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    settings = get_settings()
    password = _password(settings)
    if password is None:
        return {
            "ok": True,
            "auth_required": False,
            "expires_at": None,
            "registration_enabled": False,
            "registration_requires_approval": registration_requires_approval(settings),
            **_identity_fields(None),
        }

    # per-IP window + per (username, IP) backoff, before any password is checked
    # (services/auth_throttle.py); only a wrong password counts as a failure
    ip = auth_throttle.client_ip(request, settings)
    auth_throttle.check_login(ip, req.username, settings)
    try:
        identity, stamp = _authenticate(req, settings)
    except AppError as exc:
        if exc.code == "auth.invalid_credentials":
            auth_throttle.login_failed(ip, req.username, settings)
        raise
    auth_throttle.login_succeeded(ip, req.username)

    expiry = _set_session_cookie(response, identity, settings, stamp)
    return {
        "ok": True,
        "auth_required": True,
        "expires_at": expiry,
        "registration_enabled": registration_enabled(settings),
        "registration_requires_approval": registration_requires_approval(settings),
        # carried here too: after a sign-out the login page is rebuilt from this answer
        **_closed_notice(settings),
        **_identity_fields(identity),
    }


def _closed_notice(settings: Settings) -> dict[str, str]:
    """`registration_closed_notice` — present only while the gate is on and an operator
    closed registration (`auth_registration_enabled: false`); "" = the console's
    generic copy. Not with local login hidden (sso D-2): there the identity provider
    is the account source, so "ask an administrator" would be wrong."""
    if not enabled(settings) or settings.auth_registration_enabled or _local_login_hidden():
        return {}
    return {"registration_closed_notice": settings.auth_registration_closed_notice.strip()}


def _authenticate(req: LoginRequest, settings: Settings) -> tuple[Identity, str | None]:
    """The identity a username/password proves, and its session stamp; raises the
    `auth.*` refusal otherwise."""

    username_ok = hmac.compare_digest(
        req.username.strip().encode("utf-8"),
        settings.auth_username.encode("utf-8"),
    )
    stamp: str | None = None
    if username_ok:
        # the built-in admin never falls through to the ledger's users table; its
        # password is the console-set override when there is one, else the config's
        db = SessionLocal()
        try:
            if not admin_credential.verify(db, req.password, settings):
                raise AppError(
                    "auth.invalid_credentials",
                    "Invalid username or password",
                    status_code=401,
                )
            stamp = admin_credential.session_stamp(db, settings) or None
        finally:
            db.close()
        identity = Identity(username=settings.auth_username, role=ROLE_ADMIN)
    else:
        if _local_login_hidden():
            # local login hidden (sso D-2): the IdP is the way in for every account but
            # the built-in administrator (above), so a password is not even checked
            raise AppError(
                "auth.local_login_hidden",
                "Sign in with your organization's single sign-on",
                status_code=403,
            )
        db = SessionLocal()
        try:
            user = users_service.authenticate(db, req.username, req.password)
            users_service.record_login(db, user)
            stamp = account_stamp(user)
            identity = Identity(
                username=user.username,
                role=user.role,
                email=user.email,
                account_expires_at=users_service.as_utc(user.expires_at),
                user_id=user.id,
                permissions=granted_permissions(user.permissions, user.role),
                account_source=_account_source(user),
            )
        finally:
            db.close()
    return identity, stamp


@router.post("/register", status_code=201)
def register(req: RegisterRequest, request: Request) -> dict[str, Any]:
    settings = get_settings()
    if not registration_enabled(settings):
        raise AppError(
            "auth.registration_disabled",
            "Self-service registration is not available on this console",
        )
    auth_throttle.check_register(auth_throttle.client_ip(request, settings), settings)
    db = SessionLocal()
    try:
        _check_pending_capacity(db, settings)
        try:
            user = users_service.register_user(
                db, req.username, req.email, req.password, settings
            )
        except AppError as exc:
            if exc.code == "auth.email_taken" and registration_requires_approval(settings):
                # With approval required a new registration only ever answers
                # "submitted for review", so a taken address answers exactly that
                # too (same cost: one password hash) — the 409 let anyone probe
                # which emails hold an account. No row is written; the owner of
                # the address already has an account to sign in with.
                users_service.hash_password(req.password)
                return _pending_answer(
                    users_service.normalize_username(req.username),
                    users_service.normalize_email(req.email),
                    settings,
                )
            raise
        expires_at = users_service.as_utc(user.expires_at)
        return {
            "ok": True,
            "username": user.username,
            "email": user.email,
            # pending ⇒ no window yet: the clock starts when an admin approves
            "status": user.status,
            "requires_approval": user.status == users_service.STATUS_PENDING,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "valid_days": settings.auth_registration_valid_days,
        }
    finally:
        db.close()


def _pending_answer(username: str, email: str, settings: Settings) -> dict[str, Any]:
    return {
        "ok": True,
        "username": username,
        "email": email,
        "status": users_service.STATUS_PENDING,
        "requires_approval": True,
        "expires_at": None,
        "valid_days": settings.auth_registration_valid_days,
    }


def _check_pending_capacity(db: Session, settings: Settings) -> None:
    """Refuse a registration while `auth_registration_max_pending` accounts already
    wait for approval: the bound on a flood that rotates IPs past the per-IP limit."""
    cap = settings.auth_registration_max_pending
    if cap <= 0 or not registration_requires_approval(settings):
        return
    waiting = db.scalar(
        select(func.count()).select_from(User).where(
            User.status == users_service.STATUS_PENDING
        )
    ) or 0
    if waiting >= cap:
        raise AppError(
            "auth.registration_queue_full",
            "Registration is temporarily closed: too many accounts await approval",
            status_code=503,
        )


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


def user_identity(user: User) -> Identity:
    """The `Identity` of a registered account, as the login answers it."""
    return Identity(
        username=user.username,
        role=user.role,
        email=user.email,
        account_expires_at=users_service.as_utc(user.expires_at),
        user_id=user.id,
        permissions=granted_permissions(user.permissions, user.role),
        account_source=_account_source(user),
    )


def issue_user_session(response: Response, user: User, settings: Settings | None = None) -> int:
    """Set the session cookie of a registered account (the SSO callback's way in):
    the same cookie a password login sets, stamped with `account_stamp`."""
    current = settings or get_settings()
    return _set_session_cookie(response, user_identity(user), current, account_stamp(user))


def _set_session_cookie(
    response: Response,
    identity: Identity,
    settings: Settings,
    stamp: str | None,
) -> int:
    expiry = int(time.time()) + SESSION_TTL_SECONDS
    if identity.account_expires_at is not None:
        # never outlive the account itself
        expiry = min(expiry, int(identity.account_expires_at.timestamp()))
    max_age = max(1, expiry - int(time.time()))
    response.set_cookie(
        COOKIE_NAME,
        _issue(identity.username, expiry, settings, user_id=identity.user_id, stamp=stamp),
        max_age=max_age,
        httponly=True,
        secure=cookie_secure(settings),
        samesite="lax",
        path="/",
    )
    return expiry


@router.post("/password")
def change_password(
    req: PasswordChangeRequest, request: Request, response: Response
) -> dict[str, Any]:
    """The signed-in account changes its own password (current + new).

    Works for the built-in admin too: its new password is stored as a hash override
    (`services/admin_credential.py`) that takes precedence over the configured one.
    Every other session of the account ends (the cookie stamp changes); this one is
    re-issued with the new stamp so the caller stays signed in. Journaled; the
    password itself is never logged or returned.
    """
    settings = get_settings()
    if not enabled(settings):
        raise AppError(
            "auth.not_enabled",
            "This console has no login gate, so there is no password to change",
        )
    identity = require_identity(request, settings)
    db = SessionLocal()
    try:
        if identity.user_id is None:
            if not admin_credential.verify(db, req.current_password, settings):
                raise AppError(
                    "auth.invalid_current_password", "The current password is incorrect"
                )
            admin_credential.set_password(db, req.new_password, identity.username)
            db.commit()
            stamp: str | None = admin_credential.session_stamp(db, settings) or None
            target = f"{identity.username} (builtin)"
        else:
            user = db.get(User, identity.user_id)
            if user is None:
                raise AppError("auth.required", "Authentication required", status_code=401)
            if users_service.is_sso_account(user):
                raise AppError(
                    "auth.sso_account_no_password",
                    "This account signs in through single sign-on and has no local "
                    "password — change it at your identity provider",
                    status_code=409,
                )
            if not users_service.verify_password(req.current_password, user.password_hash):
                raise AppError(
                    "auth.invalid_current_password", "The current password is incorrect"
                )
            users_service.validate_password(req.new_password)
            user.password_hash = users_service.hash_password(req.new_password)
            db.commit()
            stamp = account_stamp(user)
            target = f"{user.username} ({user.id})"
    finally:
        db.close()
    record_audit_event(
        workspace_id=DEFAULT_WORKSPACE_ID,
        actor=identity.username,
        actor_user_id=identity.user_id,
        action="auth.password_change",
        target=f"{target} self-service",
    )
    expiry = _set_session_cookie(response, identity, settings, stamp)
    return {"ok": True, "expires_at": expiry}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict[str, Any]:
    """End the console session. An SSO account's answer also carries the IdP's
    `end_session_url` (OIDC RP-initiated logout, or a SAML SP-initiated LogoutRequest
    to the IdP's SLO endpoint, sso D-5) when the IdP has one; the browser goes there
    next, so the IdP session ends too."""
    settings = get_settings()
    end_session_url: str | None = None
    identity = getattr(request.state, "identity", None)
    if isinstance(identity, Identity) and identity.user_id is not None:
        from app.services.sso_saml import end_session_url as sso_end_session_url

        db = SessionLocal()
        try:
            user = db.get(User, identity.user_id)
            if user is not None and users_service.is_sso_account(user):
                end_session_url = sso_end_session_url(db, user, settings)
        finally:
            db.close()
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=cookie_secure(settings),
        httponly=True,
        samesite="lax",
    )
    if end_session_url:
        return {"ok": True, "end_session_url": end_session_url}
    return {"ok": True}
